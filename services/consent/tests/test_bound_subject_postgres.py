"""The bound subject's consents reach Policy through the real authorities.

Binding offers become grants in the PostgreSQL consent authority (RLS,
provisioning, offer/evidence heads), and the persistent Session Runtime then
issues the device's own profile from them. Identity reads for the resolver are
served from the seeded rows; ``test_bound_subject.py`` covers the resolver
against the real Identity service.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
from services.consent.bound_subject import (
    MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectConsentService,
    BoundSubjectGrant,
)
from services.consent.postgres_store import PostgresConsentStore
from services.identity.domain import DeviceBinding, PersonSubject, Relationship
from services.policy.engine import PolicyEngine
from services.session_runtime.postgres_store import PostgresSessionRuntimeStore
from services.session_runtime.service import (
    StartPersistentSessionCommand,
    SwitchPersistentSubjectCommand,
    build_postgres_session_runtime_service,
)
from services.session_runtime.tests.test_postgres_store import (
    _SIGNING_KEY,
    _dsn_with,
    _seed_delegated_binding,
    _seed_verified_device,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)

pytestmark = pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the real consent authority",
)


class _SeededIdentity:
    """Identity reads the resolver makes, answered from the seeded rows."""

    def __init__(self, connection_dsn: str) -> None:
        self._dsn = connection_dsn

    async def _fetch(self, sql: str, *args: object) -> list[asyncpg.Record]:
        connection = await asyncpg.connect(self._dsn)
        try:
            return list(await connection.fetch(sql, *args))
        finally:
            await connection.close()

    async def get_person(self, person_id: str, actor_person_id: str | None = None) -> PersonSubject:
        del actor_person_id
        (row,) = await self._fetch("SELECT * FROM identity_persons WHERE person_id=$1", person_id)
        return PersonSubject(
            person_id=row["person_id"],
            display_name=row["display_name"],
            subject_category=row["subject_category"],
            age_band=row["age_band"],
            age_evidence_status=row["age_evidence_status"],
        )

    async def get_binding(self, binding_id: str, actor_person_id: str | None = None) -> DeviceBinding:
        del actor_person_id
        (row,) = await self._fetch(
            "SELECT * FROM identity_device_bindings WHERE binding_id=$1", binding_id
        )
        subjects = await self._fetch(
            "SELECT person_id FROM identity_device_binding_roles "
            "WHERE binding_id=$1 AND role='primary_subject' AND status='active'",
            binding_id,
        )
        return DeviceBinding(
            binding_id=row["binding_id"],
            device_id=row["device_id"],
            declared_mode=row["declared_mode"],
            account_owner_person_id=row["account_owner_person_id"],
            primary_subject_ids=tuple(item["person_id"] for item in subjects),
            binding_version=row["binding_version"],
            status=row["status"],
            valid_from=row["valid_from"],
            valid_until=row["valid_until"],
        )

    async def list_relationships(
        self,
        *,
        person_id: str,
        statuses: tuple[str, ...] | None = None,
        actor_person_id: str | None = None,
    ) -> tuple[Relationship, ...]:
        del actor_person_id
        rows = await self._fetch(
            "SELECT * FROM identity_relationships WHERE target_person_id=$1", person_id
        )
        return tuple(
            Relationship(
                relationship_id=row["relationship_id"],
                source_person_id=row["source_person_id"],
                target_person_id=row["target_person_id"],
                relation_type=row["relation_type"],
                status=row["status"],
                valid_from=row["valid_from"],
                valid_until=row["valid_until"],
                established_evidence_id=row["established_evidence_id"],
                confirmed_by_source_at=row["confirmed_by_source_at"],
                confirmed_by_target_at=row["confirmed_by_target_at"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
            if statuses is None or row["status"] in statuses
        )


async def _attest(
    admin: asyncpg.Connection, *, source: str, target: str, relation_type: str
) -> None:
    now = datetime.now(UTC)
    await admin.execute(
        """
        INSERT INTO identity_relationships (
            relationship_id, source_person_id, target_person_id, relation_type,
            status, valid_from, established_evidence_id, confirmed_by_source_at,
            requires_confirmation, can_delegate, created_at, updated_at
        ) VALUES ($1, $2, $3, $4, 'active', $5, $6, $5, TRUE, FALSE, $5, $5)
        """,
        f"rel-{uuid.uuid4().hex[:12]}",
        source,
        target,
        relation_type,
        now,
        f"{relation_type.split('_')[0]}_attestation_v1:device_binding",
    )


async def _consent_service(bootstrap_dsn: str) -> tuple[BoundSubjectConsentService, PostgresConsentStore]:
    password = uuid.uuid4().hex
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(f"ALTER ROLE memoria_consent PASSWORD '{password}'")
    finally:
        await admin.close()
    database = bootstrap_dsn.rsplit("/", 1)[-1].split("?", 1)[0]
    root = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    store = PostgresConsentStore(
        _dsn_with(root, database=database, user="memoria_consent", password=password)
    )
    await store.initialize()
    service = BoundSubjectConsentService(
        store=store,
        identity=_SeededIdentity(bootstrap_dsn),  # type: ignore[arg-type]
        provision=store.authorize_bound_subject,
    )
    return service, store


def _start(**overrides: Any) -> StartPersistentSessionCommand:
    values: dict[str, Any] = {
        "session_id": f"session-{uuid.uuid4().hex[:10]}",
        "expected_binding_version": 1,
        "idempotency_key": f"start-{uuid.uuid4().hex[:10]}",
        "now": datetime.now(UTC),
        "requested_capabilities": ("chat", "memory_recall_private"),
    }
    values.update(overrides)
    return StartPersistentSessionCommand(**values)


def _capabilities(profile: Any) -> set[str]:
    return {item.value for item in profile.capabilities}


@pytest.mark.asyncio
async def test_child_device_gets_chat_and_memory_from_the_guardians_binding_consent(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="parent-1",
            subject_id="child-1",
            device_id="device-child",
            binding_id="binding-child",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
        await _attest(admin, source="parent-1", target="child-1", relation_type="guardian_of")
        await _seed_verified_device(
            admin, device_id="device-child", binding_id="binding-child", now=now
        )
    finally:
        await admin.close()
    runtime = build_postgres_session_runtime_service(
        store=store, signing_key=_SIGNING_KEY, policy=PolicyEngine()
    )
    service, consent_store = await _consent_service(bootstrap_dsn)
    try:
        before = await runtime.start(
            _start(actor_id="parent-1", device_id="device-child", device_bound_subject_id="child-1")
        )
        # No consent yet: the minor device session gets nothing at all.
        assert _capabilities(before) == set()

        grant = BoundSubjectGrant(
            actor_person_id="parent-1",
            subject_person_id="child-1",
            binding_id="binding-child",
            kind="guardian",
            capabilities=MINOR_SESSION_CAPABILITIES,
            source_key="snapshot-1",
        )
        await service.grant(grant)
        await service.grant(replace(grant, capabilities=MEMORY_CAPABILITIES))
        # A retry of the same binding replays instead of re-granting.
        await service.grant(grant)
        chains = await service.active(
            subject_person_id="child-1", binding_id="binding-child", binding_version=1
        )
        assert {chain.capability for chain in chains} == {
            *MINOR_SESSION_CAPABILITIES,
            *MEMORY_CAPABILITIES,
        }
        assert {chain.actor_kind for chain in chains} == {"guardian"}

        device = await runtime.start(
            _start(actor_id="parent-1", device_id="device-child", device_bound_subject_id="child-1")
        )
        assert device.active_subject_id == "child-1"
        assert {"chat", "memory_recall_private"} <= _capabilities(device)

        # The guardian's own app session switched to the child is not the
        # child talking: it may chat, never read the child's memory.
        app = await runtime.start(_start(actor_id="parent-1", device_id="device-child"))
        switched = await runtime.switch_subject(
            SwitchPersistentSubjectCommand(
                session_id=app.session_id,
                actor_id="parent-1",
                subject_id="child-1",
                now=datetime.now(UTC),
                requested_capabilities=("chat", "memory_recall_private"),
            )
        )
        assert "memory_recall_private" not in _capabilities(switched)

        revoked = await service.revoke(
            actor_person_id="parent-1",
            subject_person_id="child-1",
            binding_id="binding-child",
            capabilities=MEMORY_CAPABILITIES,
            reason="guardian_toggle_off",
        )
        assert revoked == 2
        after = await runtime.start(
            _start(actor_id="parent-1", device_id="device-child", device_bound_subject_id="child-1")
        )
        assert "chat" in _capabilities(after)
        assert "memory_recall_private" not in _capabilities(after)
    finally:
        await consent_store.close()


@pytest.mark.asyncio
async def test_elder_device_gets_memory_from_the_adult_childs_delegate_consent(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811 - pytest fixture injection shadows import
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="son-1",
            subject_id="elder-1",
            device_id="device-elder",
            binding_id="binding-elder",
            declared_mode="child_for_parent",
            subject_category="adult",
            age_band="adult",
        )
        await _seed_verified_device(
            admin, device_id="device-elder", binding_id="binding-elder", now=now
        )
    finally:
        await admin.close()
    runtime = build_postgres_session_runtime_service(
        store=store, signing_key=_SIGNING_KEY, policy=PolicyEngine()
    )
    service, consent_store = await _consent_service(bootstrap_dsn)
    grant = BoundSubjectGrant(
        actor_person_id="son-1",
        subject_person_id="elder-1",
        binding_id="binding-elder",
        kind="delegate",
        capabilities=MEMORY_CAPABILITIES,
        source_key="snapshot-elder",
    )
    try:
        # Without the attested delegation the authority refuses to provision.
        with pytest.raises(asyncpg.RaiseError):
            await service.grant(grant)

        admin = await asyncpg.connect(bootstrap_dsn)
        try:
            await _attest(admin, source="son-1", target="elder-1", relation_type="delegate_for")
        finally:
            await admin.close()
        await service.grant(grant)
        chains = await service.active(
            subject_person_id="elder-1", binding_id="binding-elder", binding_version=1
        )
        assert {(chain.actor_kind, chain.actor_id) for chain in chains} == {("delegate", "son-1")}

        device = await runtime.start(
            _start(actor_id="son-1", device_id="device-elder", device_bound_subject_id="elder-1")
        )
        assert device.service_mode.value == "senior_companion"
        assert {"chat", "memory_recall_private"} <= _capabilities(device)
    finally:
        await consent_store.close()
