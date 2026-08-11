"""Live FORCE-RLS PostgreSQL contract for the consent store.

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.  Static schema contracts
run unconditionally in ``test_postgres_schema_static.py``; this test verifies
PostgreSQL's real privilege and transition behavior in an isolated database.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.consent.authority import (
    AsyncConsentAuthority,
    AsyncEvidenceResolverPort,
    ConsentDeniedError,
    SubjectProof,
)
from services.consent.evidence import (
    BindingEvidence,
    ConsentOffer,
    ConsentParams,
    ConsentSnapshot,
    RelationshipEvidence,
)
from services.consent.postgres_store import PostgresConsentStore
from services.consent.store import ConsentConflictError
from services.consent.tests.async_safety import (
    await_task_bounded,
    cancel_and_monitor,
    wait_for_event_or_task,
)
from services.consent.transaction_authorizer import (
    ConsentFenceMismatchError,
    ExpectedConsentFence,
    TransactionBoundConsentAuthorizer,
)

ROLES = (
    "memoria_consent_owner",
    "memoria_consent",
    "memoria_policy_projector",
    "memoria_consent_outbox",
    "memoria_consent_audit",
    "memoria_consent_maintenance",
)


class LiveResolver(AsyncEvidenceResolverPort):
    """The live test's explicit stand-in for the future Identity adapter."""

    async def resolve_subject(self, candidate: SubjectProof) -> SubjectProof:
        return candidate

    async def resolve_binding(self, candidate: BindingEvidence, subject_id: str) -> BindingEvidence:
        del subject_id
        return candidate

    async def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]:
        del actor_id, subject_id, binding_id
        return candidates


def _postgres_dsn(dsn: str, *, database: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(parsed.username or "postgres")
    password = quote(parsed.password or "")
    credentials = f"{username}:{password}" if password else username
    return urlunsplit(
        (
            parsed.scheme,
            f"{credentials}@{host}",
            f"/{database}",
            parsed.query,
            "",
        )
    )


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


async def _session_as(dsn: str, role: str) -> asyncpg.Connection:
    connection = await asyncpg.connect(dsn)
    await connection.execute(f"SET SESSION AUTHORIZATION {role}")
    return connection


async def _wait_for_database_lock(
    observer: asyncpg.Connection,
    blocked_pid: int,
    blocked_task: asyncio.Task[object],
) -> None:
    """Synchronize on PostgreSQL lock evidence, never elapsed-time guessing."""
    try:
        async with asyncio.timeout(10.0):
            while True:
                if blocked_task.done():
                    await blocked_task
                    raise AssertionError("revoker completed before entering a lock wait")
                blockers = await observer.fetchval(
                    "SELECT pg_blocking_pids($1)", blocked_pid
                )
                if blockers:
                    return
                await asyncio.sleep(0)
    except TimeoutError as exc:
        raise AssertionError("revoker never entered a PostgreSQL lock wait") from exc


async def _append_revoke_on_locked_heads(
    connection: asyncpg.Connection,
    evidence: object,
    snapshot: ConsentSnapshot,
    *,
    now: datetime,
    request_actor_id: str | None = None,
    remaining_grants: tuple[object, ...] = (),
    started: asyncio.Event | None = None,
) -> tuple[object, ConsentSnapshot]:
    """Test-only DB revoker using the exact production head functions."""
    from services.consent.evidence import ConsentEvidence

    assert isinstance(evidence, ConsentEvidence)
    resolved_request_actor = request_actor_id or evidence.actor_id
    if started is not None:
        started.set()
    locked = await connection.fetchrow(
        "SELECT * FROM consent_ensure_authority_head($1,$2,$3,$4,$5,$6,$7)",
        resolved_request_actor,
        evidence.actor_id,
        evidence.subject_id,
        evidence.binding_id,
        evidence.binding_version,
        evidence.capability,
        evidence.purpose,
    )
    assert locked is not None
    locked_snapshot = await connection.fetchrow(
        "SELECT * FROM consent_ensure_snapshot_head($1,$2,$3,$4)",
        resolved_request_actor,
        evidence.subject_id,
        evidence.binding_id,
        evidence.binding_version,
    )
    assert locked_snapshot is not None

    snapshot_id = f"revoke-snapshot-{uuid.uuid4()}"
    revoked = replace(
        evidence,
        version=evidence.version + 1,
        snapshot_id=snapshot_id,
        status="revoked",
        evidence_id=f"revoke-evidence-{uuid.uuid4()}",
        idempotency_key=None,
        canonical_hash="",
    )
    next_snapshot = ConsentSnapshot(
        snapshot_id=snapshot_id,
        version=snapshot.version + 1,
        subject_id=snapshot.subject_id,
        binding_id=snapshot.binding_id,
        binding_version=snapshot.binding_version,
        policy_version=snapshot.policy_version,
        created_at=now,
        grants=tuple(
            grant for grant in remaining_grants if isinstance(grant, ConsentEvidence)
        ),
        relationships=snapshot.relationships,
        binding=snapshot.binding,
        canonical_hash="",
    )
    await connection.execute(
        "INSERT INTO consent_evidence (consent_id,version,actor_id,subject_id,"
        "binding_id,binding_version,status,evidence_json) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)",
        revoked.consent_id,
        revoked.version,
        revoked.actor_id,
        revoked.subject_id,
        revoked.binding_id,
        revoked.binding_version,
        revoked.status,
        json.dumps(revoked.to_canonical_dict()),
    )
    await connection.execute(
        "INSERT INTO consent_snapshot (snapshot_id,version,actor_id,subject_id,"
        "binding_id,binding_version,snapshot_json) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb)",
        next_snapshot.snapshot_id,
        next_snapshot.version,
        resolved_request_actor,
        next_snapshot.subject_id,
        next_snapshot.binding_id,
        next_snapshot.binding_version,
        json.dumps(next_snapshot.to_canonical_dict()),
    )
    await connection.fetchrow(
        "SELECT * FROM consent_advance_snapshot_head($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        resolved_request_actor,
        next_snapshot.subject_id,
        next_snapshot.binding_id,
        next_snapshot.binding_version,
        snapshot.version,
        snapshot.canonical_hash,
        next_snapshot.snapshot_id,
        next_snapshot.version,
        next_snapshot.canonical_hash,
    )
    await connection.fetchrow(
        "SELECT * FROM consent_advance_authority_head("
        "$1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)",
        resolved_request_actor,
        revoked.actor_id,
        revoked.subject_id,
        revoked.binding_id,
        revoked.binding_version,
        revoked.capability,
        revoked.purpose,
        evidence.version,
        evidence.canonical_hash,
        revoked.consent_id,
        revoked.version,
        revoked.canonical_hash,
    )
    return revoked, next_snapshot


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL consent contract",
)
@pytest.mark.asyncio
async def test_postgres_consent_schema_rls_and_authority() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_consent_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    preexisting_roles = {
        role: bool(await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role))
        for role in ROLES
    }
    store: PostgresConsentStore | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        schema = (Path(__file__).resolve().parents[1] / "postgres_schema.sql").read_text()

        setup = await asyncpg.connect(dsn)
        try:
            await setup.execute(schema)
            tables = (
                "consent_authorization",
                "consent_offer",
                "consent_evidence",
                "consent_snapshot",
                "binding_consent_snapshot",
                "consent_outbox",
                "consent_audit",
                "consent_idempotency",
                "consent_offer_head",
                "consent_evidence_head",
                "consent_snapshot_head",
            )
            for table in tables:
                forced = await setup.fetchval(
                    "SELECT relforcerowsecurity FROM pg_class WHERE relname = $1 AND relkind = 'r'",
                    table,
                )
                assert forced is True, f"{table} must FORCE row level security"
            permissive = await setup.fetch(
                "SELECT tablename, policyname FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename LIKE 'consent_%' "
                "AND (qual = 'true' OR with_check = 'true')"
            )
            assert permissive == []

            # Deployment-owned mappings: app/worker cannot self-provision these.
            for actor_id in ("person_guardian", "person_minor", "system"):
                for role in ("memoria_consent", "memoria_consent_maintenance"):
                    await setup.execute(
                        "SELECT consent_authorize($1, $2, $3, $4)",
                        role,
                        actor_id,
                        "person_minor",
                        "bd_1",
                    )
            await setup.execute(
                "SELECT consent_authorize($1, $2, $3, $4)",
                "memoria_policy_projector",
                "actor-B",
                "person_minor",
                "bd_1",
            )
        finally:
            await setup.close()

        # Domain behavior and idempotent replay still use one PG transaction.
        store = PostgresConsentStore(dsn, session_role="memoria_consent")
        await store.initialize()
        authority = AsyncConsentAuthority(store, resolver=LiveResolver())
        now = utc("2026-08-09T10:00:00+00:00")
        binding = BindingEvidence(
            binding_id="bd_1",
            version=1,
            device_id="dev_1",
            status="active",
            declared_mode="parent_for_child",
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=365),
            canonical_hash="",
        )
        relationship = RelationshipEvidence(
            relationship_id="rel_1",
            snapshot_id="rs_1",
            revision=1,
            relation_type="guardian_of",
            status="active",
            source_person_id="person_guardian",
            target_person_id="person_minor",
            binding_id="bd_1",
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=365),
            canonical_hash="",
        )
        subject = SubjectProof(
            subject_id="person_minor",
            subject_category="minor",
            age_evidence_status="verified",
        )
        offer = ConsentOffer(
            offer_id="of_pg",
            capability="chat",
            subject_id="person_minor",
            actor_id="person_guardian",
            resource_owner_id="person_minor",
            purpose="user_request",
            params=ConsentParams(max_session_seconds=3600),
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=365),
            created_at=now,
            policy_version="policy-cn-minor-v5",
        )
        offer = await authority.create_offer(
            offer_id=offer.offer_id,
            capability=offer.capability,
            subject_id=offer.subject_id,
            actor_id=offer.actor_id,
            resource_owner_id=offer.resource_owner_id,
            purpose=offer.purpose,
            params=offer.params,
            valid_from=offer.valid_from,
            valid_until=offer.valid_until,
            created_at=offer.created_at,
            policy_version=offer.policy_version,
        )
        tampered = replace(
            offer,
            actor_id="person_attacker",
            subject_id="person_attacker",
            resource_owner_id="person_attacker",
            capability="payment",
            canonical_hash="",
        )
        grant = await authority.grant(
            tampered,
            expected_version=1,
            actor_kind="guardian",
            subject=subject,
            binding=binding,
            relationships=(relationship,),
            idempotency_key="pg-grant-1",
            now=now,
        )
        replay = await authority.grant(
            offer.offer_id,
            expected_version=1,
            actor_kind="guardian",
            subject=subject,
            binding=binding,
            relationships=(relationship,),
            idempotency_key="pg-grant-1",
            now=now + timedelta(minutes=1),
        )
        assert replay.outbox_event.event_id == grant.outbox_event.event_id
        assert replay.evidence == grant.evidence
        assert replay.snapshot == grant.snapshot
        assert grant.evidence.actor_id == "person_guardian"
        assert grant.evidence.subject_id == "person_minor"
        assert grant.evidence.capability == "chat"

        with pytest.raises(ConsentDeniedError):
            await authority.grant(
                offer.offer_id,
                expected_version=1,
                actor_kind="guardian",
                subject=replace(subject, subject_id="person_other"),
                binding=binding,
                relationships=(relationship,),
                now=now,
            )
        with pytest.raises(ConsentDeniedError):
            await authority.grant(
                offer.offer_id,
                expected_version=1,
                actor_kind="guardian",
                subject=subject,
                binding=replace(binding, binding_id="bd_other", canonical_hash=""),
                relationships=(relationship,),
                now=now,
            )

        revoked = await authority.revoke(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship,),
            now=now + timedelta(minutes=2),
        )
        assert revoked.evidence.status == "revoked"
        assert revoked.evidence.version == 2

        version_two = await authority.create_offer(
            offer_id=offer.offer_id,
            version=2,
            supersedes_offer_id=offer.offer_id,
            capability=offer.capability,
            subject_id=offer.subject_id,
            actor_id=offer.actor_id,
            resource_owner_id=offer.resource_owner_id,
            purpose="runtime_profile_issue",
            params=offer.params,
            valid_from=offer.valid_from,
            valid_until=offer.valid_until,
            created_at=now + timedelta(minutes=3),
            policy_version=offer.policy_version,
        )
        assert version_two.version == 2
        with pytest.raises(ConsentConflictError, match="offer version"):
            await authority.grant(
                offer.offer_id,
                expected_version=1,
                actor_kind="guardian",
                subject=subject,
                binding=binding,
                relationships=(relationship,),
                now=now + timedelta(minutes=3),
            )

        expired_offer = await authority.create_offer(
            offer_id="of_pg_expired",
            capability="chat",
            subject_id=offer.subject_id,
            actor_id=offer.actor_id,
            resource_owner_id=offer.resource_owner_id,
            purpose="user_request",
            params=offer.params,
            valid_from=now - timedelta(days=2),
            valid_until=now - timedelta(days=1),
            created_at=now - timedelta(days=2),
            policy_version=offer.policy_version,
        )
        with pytest.raises(ConsentDeniedError) as expired_error:
            await authority.grant(
                expired_offer.offer_id,
                expected_version=1,
                actor_kind="guardian",
                subject=subject,
                binding=binding,
                relationships=(relationship,),
                now=now,
            )
        assert expired_error.value.reason == "offer_expired"

        concurrent_offer = await authority.create_offer(
            offer_id="of_pg_concurrent",
            capability="tutor",
            subject_id=offer.subject_id,
            actor_id=offer.actor_id,
            resource_owner_id=offer.resource_owner_id,
            purpose="user_request",
            params=offer.params,
            valid_from=offer.valid_from,
            valid_until=offer.valid_until,
            created_at=now + timedelta(minutes=4),
            policy_version=offer.policy_version,
        )

        async def concurrent_grant(key: str) -> object:
            return await authority.grant(
                concurrent_offer.offer_id,
                expected_version=1,
                actor_kind="guardian",
                subject=subject,
                binding=binding,
                relationships=(relationship,),
                idempotency_key=key,
                now=now + timedelta(minutes=5),
            )

        concurrent_results = await asyncio.gather(
            concurrent_grant("pg-concurrent-1"),
            concurrent_grant("pg-concurrent-2"),
        )
        concurrent_ids = {
            result.evidence.consent_id
            for result in concurrent_results  # type: ignore[attr-defined]
        }
        assert len(concurrent_ids) == 2
        read = await store.transaction()
        active = await read.active_chains("person_minor", "bd_1", 1)
        await read.commit()
        assert len(active) == 1
        assert active[0].consent_id in concurrent_ids

        head_read = await store.transaction()
        active_snapshot = await head_read.snapshot_by_id(active[0].snapshot_id)
        await head_read.commit()
        assert active_snapshot is not None
        expected_fence = ExpectedConsentFence(
            request_actor_id=active[0].actor_id,
            evidence_actor_id=active[0].actor_id,
            consent_id=active[0].consent_id,
            revision=active[0].version,
            canonical_hash=active[0].canonical_hash,
            current_snapshot_id=active_snapshot.snapshot_id,
            current_snapshot_revision=active_snapshot.version,
            current_snapshot_hash=active_snapshot.canonical_hash,
            subject_id=active[0].subject_id,
            binding_id=active[0].binding_id,
            binding_version=active[0].binding_version,
            capability=active[0].capability,
            purpose=active[0].purpose,  # type: ignore[arg-type]
        )

        # Projector receives locked facts only inside its caller-owned tx; it
        # has no direct head-table DML and stale hashes fail closed.
        projector = await _session_as(dsn, "memoria_policy_projector")
        projector_tx = projector.transaction()
        await projector_tx.start()
        try:
            async def no_effect(connection: asyncpg.Connection) -> str:
                assert connection is projector
                return "validated"

            with pytest.raises(ConsentFenceMismatchError, match="authority head missing"):
                await TransactionBoundConsentAuthorizer().execute_with_authority(
                    projector,
                    expected=(expected_fence,),
                    now=now + timedelta(minutes=6),
                    operation=no_effect,
                )
            await projector.execute(
                "SELECT set_config('app.consent_actor', 'actor-A', false)"
            )
            with pytest.raises(ConsentFenceMismatchError, match="authority head missing"):
                await TransactionBoundConsentAuthorizer().execute_with_authority(
                    projector,
                    expected=(
                        replace(
                            expected_fence,
                            request_actor_id="actor-A",
                            evidence_actor_id="actor-B",
                        ),
                    ),
                    now=now + timedelta(minutes=6),
                    operation=no_effect,
                )
            provision = await asyncpg.connect(dsn)
            try:
                await provision.execute(
                    "SELECT consent_authorize($1, $2, $3, $4)",
                    "memoria_policy_projector",
                    active[0].actor_id,
                    active[0].subject_id,
                    active[0].binding_id,
                )
            finally:
                await provision.close()
            assert (
                await TransactionBoundConsentAuthorizer().execute_with_authority(
                    projector,
                    expected=(expected_fence,),
                    now=now + timedelta(minutes=6),
                    operation=no_effect,
                )
                == "validated"
            )
            provision = await asyncpg.connect(dsn)
            try:
                await provision.execute(
                    "SELECT consent_authorize($1, $2, $3, $4)",
                    "memoria_policy_projector",
                    active[0].subject_id,
                    active[0].subject_id,
                    active[0].binding_id,
                )
            finally:
                await provision.close()
            assert (
                await TransactionBoundConsentAuthorizer().execute_with_authority(
                    projector,
                    expected=(
                        replace(
                            expected_fence,
                            request_actor_id=active[0].subject_id,
                        ),
                    ),
                    now=now + timedelta(minutes=6),
                    operation=no_effect,
                )
                == "validated"
            )
            with pytest.raises(ConsentFenceMismatchError, match="canonical_hash"):
                await TransactionBoundConsentAuthorizer().execute_with_authority(
                    projector,
                    expected=(replace(expected_fence, canonical_hash="f" * 64),),
                    now=now + timedelta(minutes=6),
                    operation=no_effect,
                )
            await projector_tx.commit()
        finally:
            if projector.is_in_transaction():
                await projector_tx.rollback()
            await projector.close()
        projector_permissions = await _session_as(dsn, "memoria_policy_projector")
        try:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await projector_permissions.fetchval(
                    "SELECT count(*) FROM consent_evidence_head"
                )
        finally:
            await projector_permissions.close()

        # Strict JSONB decode rejects unknown canonical keys.
        setup = await asyncpg.connect(dsn)
        try:
            raw_json = await setup.fetchval(
                "SELECT snapshot_json::text FROM consent_snapshot WHERE snapshot_id = $1",
                revoked.snapshot.snapshot_id,
            )
            raw = json.loads(raw_json)
            assert raw["relationships"][0]["relation_type"] == "guardian_of"
            assert raw["relationships"][0]["source_person_id"] == "person_guardian"
            assert raw["relationships"][0]["target_person_id"] == "person_minor"
            assert "guardian_person_id" not in raw["relationships"][0]
            assert "subject_person_id" not in raw["relationships"][0]
            raw["bogus_key"] = "x"
            with pytest.raises(ValueError):
                ConsentSnapshot.from_canonical_dict(raw)

            # Unmapped foreign row: the shared API role must not see it.
            await setup.execute(
                "INSERT INTO consent_evidence ("
                " consent_id, version, actor_id, subject_id, binding_id,"
                " binding_version, status, evidence_json"
                ") VALUES ($1, 1, $2, $3, $4, 1, 'active', $5::jsonb)",
                "foreign-consent",
                "person_stranger",
                "person_other",
                "bd_other",
                json.dumps(grant.evidence.to_canonical_dict()),
            )
        finally:
            await setup.close()

        # API: mapping-filtered rows; arbitrary GUCs do not change visibility.
        api = await _session_as(dsn, "memoria_consent")
        try:
            own_before = await api.fetchval("SELECT count(*) FROM consent_evidence")
            assert own_before >= 2
            assert (
                await api.fetchval(
                    "SELECT count(*) FROM consent_evidence WHERE consent_id = 'foreign-consent'"
                )
                == 0
            )
            await api.execute("SELECT set_config('app.consent_actor', 'person_stranger', false)")
            await api.execute("SELECT set_config('app.consent_subject', 'person_other', false)")
            assert await api.fetchval("SELECT count(*) FROM consent_evidence") == own_before
            assert (
                await api.fetchval(
                    "SELECT count(*) FROM consent_evidence WHERE consent_id = 'foreign-consent'"
                )
                == 0
            )
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.fetchval("SELECT count(*) FROM consent_audit")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.fetchval("SELECT count(*) FROM consent_outbox")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.execute(
                    "INSERT INTO consent_evidence ("
                    " consent_id, version, actor_id, subject_id, binding_id,"
                    " binding_version, status, evidence_json"
                    ") VALUES ('forged', 1, 'person_stranger', 'person_other',"
                    " 'bd_other', 1, 'active', '{}'::jsonb)"
                )
        finally:
            await api.close()

        # Auditor: audit SELECT only.
        auditor = await _session_as(dsn, "memoria_consent_audit")
        try:
            assert await auditor.fetchval("SELECT count(*) FROM consent_audit") >= 2
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await auditor.fetchval("SELECT count(*) FROM consent_evidence")
        finally:
            await auditor.close()

        # Worker: no table privileges; only monotonic claim/complete functions.
        worker = await _session_as(dsn, "memoria_consent_outbox")
        try:
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await worker.fetchval("SELECT count(*) FROM consent_outbox")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await worker.execute("UPDATE consent_outbox SET payload_json = '[]'::jsonb")

            claimed = await worker.fetchrow(
                "SELECT * FROM consent_claim_outbox($1, $2)", "worker-live", 1
            )
            assert claimed is not None and claimed["status"] == "processing"
            payload_before = claimed["payload_json"]
            completed = await worker.fetchrow(
                "SELECT * FROM consent_complete_outbox($1, $2)",
                claimed["event_id"],
                "delivered",
            )
            assert completed is not None and completed["status"] == "delivered"
            assert completed["payload_json"] == payload_before

            with pytest.raises(asyncpg.PostgresError):
                await worker.fetchrow(
                    "SELECT * FROM consent_complete_outbox($1, $2)",
                    claimed["event_id"],
                    "delivered",
                )
            with pytest.raises(asyncpg.PostgresError):
                await worker.fetchrow(
                    "SELECT * FROM consent_complete_outbox($1, $2)",
                    revoked.outbox_event.event_id,
                    "delivered",
                )
            with pytest.raises(asyncpg.PostgresError):
                await worker.fetchrow(
                    "SELECT * FROM consent_complete_outbox($1, $2)",
                    revoked.outbox_event.event_id,
                    "pending",
                )
        finally:
            await worker.close()
    finally:
        if store is not None:
            await store.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        for role in ROLES:
            if not preexisting_roles[role] and await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", role
            ):
                await admin.execute(f"DROP ROLE {role}")
        await admin.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL consent contract",
)
@pytest.mark.asyncio
async def test_callback_and_revoker_linearize_on_the_same_actor_heads() -> None:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_consent_lock_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    preexisting_roles = {
        role: bool(await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role))
        for role in ROLES
    }
    store: PostgresConsentStore | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        setup = await asyncpg.connect(dsn)
        try:
            await setup.execute(
                (Path(__file__).resolve().parents[1] / "postgres_schema.sql").read_text()
            )
            for actor_id in ("actor-A", "actor-B", "subject-shared"):
                await setup.execute(
                    "SELECT consent_authorize($1,$2,$3,$4)",
                    "memoria_consent",
                    actor_id,
                    "subject-shared",
                    "binding-shared",
                )
            await setup.execute(
                "SELECT consent_authorize($1,$2,$3,$4)",
                "memoria_policy_projector",
                "subject-shared",
                "subject-shared",
                "binding-shared",
            )
            await setup.execute(
                "CREATE TABLE consent_test_business_effect ("
                "effect_id TEXT PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL)"
            )
            await setup.execute(
                "GRANT INSERT ON consent_test_business_effect TO memoria_policy_projector"
            )
        finally:
            await setup.close()

        store = PostgresConsentStore(dsn, session_role="memoria_consent")
        await store.initialize()
        authority = AsyncConsentAuthority(store, resolver=LiveResolver())
        now = utc("2026-08-09T12:00:00+00:00")
        binding = BindingEvidence(
            binding_id="binding-shared",
            version=1,
            device_id="device-shared",
            status="active",
            declared_mode="parent_for_child",
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=30),
            canonical_hash="",
        )
        subject = SubjectProof("subject-shared", "minor", "verified")

        async def create_grant(
            offer_id: str,
            capability: str,
            actor_id: str,
            minute: int,
        ) -> object:
            relationship = RelationshipEvidence(
                relationship_id=f"relationship-{actor_id}",
                snapshot_id=f"relationship-snapshot-{actor_id}",
                revision=1,
                relation_type="guardian_of",
                status="active",
                source_person_id=actor_id,
                target_person_id="subject-shared",
                binding_id="binding-shared",
                valid_from=now - timedelta(days=1),
                valid_until=now + timedelta(days=30),
                canonical_hash="",
            )
            offer = await authority.create_offer(
                offer_id=offer_id,
                capability=capability,
                subject_id="subject-shared",
                actor_id=actor_id,
                resource_owner_id="subject-shared",
                purpose="user_request",
                params=ConsentParams(max_session_seconds=600),
                valid_from=now - timedelta(hours=1),
                valid_until=now + timedelta(days=1),
                created_at=now + timedelta(minutes=minute),
                policy_version="policy-v2",
            )
            return await authority.grant(
                offer.offer_id,
                expected_version=offer.version,
                subject=subject,
                binding=binding,
                relationships=(relationship,),
                actor_kind="guardian",
                now=now + timedelta(minutes=minute),
            )

        def fence_for(
            result: object,
            current_snapshot: ConsentSnapshot,
        ) -> ExpectedConsentFence:
            evidence = result.evidence  # type: ignore[attr-defined]
            return ExpectedConsentFence(
                request_actor_id="subject-shared",
                evidence_actor_id=evidence.actor_id,
                consent_id=evidence.consent_id,
                revision=evidence.version,
                canonical_hash=evidence.canonical_hash,
                current_snapshot_id=current_snapshot.snapshot_id,
                current_snapshot_revision=current_snapshot.version,
                current_snapshot_hash=current_snapshot.canonical_hash,
                subject_id=evidence.subject_id,
                binding_id=evidence.binding_id,
                binding_version=evidence.binding_version,
                capability=evidence.capability,
                purpose=evidence.purpose,
            )

        first = await create_grant("offer-lock-first", "chat", "actor-A", 0)
        second = await create_grant("offer-lock-second", "tutor", "actor-B", 1)
        assert first.snapshot.version == 1  # type: ignore[attr-defined]
        assert second.snapshot.version == 2  # type: ignore[attr-defined]
        assert second.snapshot.snapshot_id != first.snapshot.snapshot_id  # type: ignore[attr-defined]
        assert {
            grant.consent_id for grant in second.snapshot.grants  # type: ignore[attr-defined]
        } == {
            first.evidence.consent_id,  # type: ignore[attr-defined]
            second.evidence.consent_id,  # type: ignore[attr-defined]
        }
        first_fence = fence_for(first, second.snapshot)  # type: ignore[attr-defined]
        second_fence = fence_for(second, second.snapshot)  # type: ignore[attr-defined]
        consumer: asyncpg.Connection | None = None
        consumer_tx: asyncpg.Transaction | None = None
        consumer_task: asyncio.Task[str] | None = None
        revoker: asyncpg.Connection | None = None
        revoker_tx: asyncpg.Transaction | None = None
        revoke_task: asyncio.Task[tuple[object, ConsentSnapshot]] | None = None
        callback_entered = asyncio.Event()
        release_callback = asyncio.Event()
        try:
            consumer = await _session_as(dsn, "memoria_policy_projector")
            consumer_tx = consumer.transaction()
            await consumer_tx.start()

            async def first_effect(connection: asyncpg.Connection) -> str:
                assert connection is consumer and connection.is_in_transaction()
                callback_entered.set()
                try:
                    async with asyncio.timeout(10.0):
                        await release_callback.wait()
                except TimeoutError as exc:
                    raise AssertionError("callback release barrier timed out") from exc
                await connection.execute(
                    "INSERT INTO consent_test_business_effect VALUES ($1,$2)",
                    "consumer-first",
                    now,
                )
                return "consumer-first"

            consumer_task = asyncio.create_task(
                TransactionBoundConsentAuthorizer().execute_with_authority(
                    consumer,
                    expected=(first_fence, second_fence),
                    now=now + timedelta(minutes=2),
                    operation=first_effect,
                )
            )
            await wait_for_event_or_task(
                callback_entered, consumer_task, timeout_seconds=10.0
            )

            revoker = await _session_as(dsn, "memoria_consent")
            revoker_pid = int(await revoker.fetchval("SELECT pg_backend_pid()"))
            revoker_tx = revoker.transaction()
            await revoker_tx.start()
            revoke_started = asyncio.Event()
            revoke_task = asyncio.create_task(
                _append_revoke_on_locked_heads(
                    revoker,
                    first.evidence,  # type: ignore[attr-defined]
                    second.snapshot,  # type: ignore[attr-defined]
                    now=now + timedelta(minutes=3),
                    request_actor_id="subject-shared",
                    remaining_grants=(second.evidence,),  # type: ignore[attr-defined]
                    started=revoke_started,
                )
            )
            await wait_for_event_or_task(
                revoke_started, revoke_task, timeout_seconds=10.0
            )
            observer = await asyncpg.connect(dsn)
            try:
                await _wait_for_database_lock(observer, revoker_pid, revoke_task)
            finally:
                await observer.close()
            release_callback.set()
            assert (
                await await_task_bounded(consumer_task, timeout_seconds=10.0)
                == "consumer-first"
            )
            await consumer_tx.commit()
            _revoked_first, snapshot_after_first_revoke = await await_task_bounded(
                revoke_task, timeout_seconds=10.0
            )
            await revoker_tx.commit()
        finally:
            release_callback.set()
            await cancel_and_monitor(consumer_task, revoke_task)
            if consumer is not None:
                if consumer.is_in_transaction() and consumer_tx is not None:
                    await consumer_tx.rollback()
                await consumer.close()
            if revoker is not None:
                if revoker.is_in_transaction() and revoker_tx is not None:
                    await revoker_tx.rollback()
                await revoker.close()

        verifier = await asyncpg.connect(dsn)
        assert (
            await verifier.fetchval(
                "SELECT count(*) FROM consent_test_business_effect "
                "WHERE effect_id='consumer-first'"
            )
            == 1
        )
        await verifier.close()

        second_fence = fence_for(second, snapshot_after_first_revoke)
        revoke_first = await _session_as(dsn, "memoria_consent")
        revoke_first_tx = revoke_first.transaction()
        await revoke_first_tx.start()
        await _append_revoke_on_locked_heads(
            revoke_first,
            second.evidence,  # type: ignore[attr-defined]
            snapshot_after_first_revoke,
            now=now + timedelta(minutes=4),
            request_actor_id="subject-shared",
        )
        await revoke_first_tx.commit()
        await revoke_first.close()

        stale_consumer = await _session_as(dsn, "memoria_policy_projector")
        stale_tx = stale_consumer.transaction()
        await stale_tx.start()
        stale_callback_called = False

        async def stale_effect(connection: asyncpg.Connection) -> str:
            nonlocal stale_callback_called
            stale_callback_called = True
            await connection.execute(
                "INSERT INTO consent_test_business_effect VALUES ($1,$2)",
                "revoke-first",
                now,
            )
            return "revoke-first"

        with pytest.raises(ConsentFenceMismatchError):
            await TransactionBoundConsentAuthorizer().execute_with_authority(
                stale_consumer,
                expected=(second_fence,),
                now=now + timedelta(minutes=5),
                operation=stale_effect,
            )
        assert stale_callback_called is False
        await stale_tx.rollback()
        await stale_consumer.close()

        verifier = await asyncpg.connect(dsn)
        try:
            assert (
                await verifier.fetchval(
                    "SELECT count(*) FROM consent_test_business_effect "
                    "WHERE effect_id='revoke-first'"
                )
                == 0
            )
        finally:
            await verifier.close()
    finally:
        if store is not None:
            await store.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        for role in ROLES:
            if not preexisting_roles[role] and await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", role
            ):
                await admin.execute(f"DROP ROLE {role}")
        await admin.close()
