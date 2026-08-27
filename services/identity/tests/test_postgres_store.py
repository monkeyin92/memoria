"""Live PostgreSQL contract for the identity store: schema bootstrap,
FORCE-RLS with transaction-local actor context, cross-person/family
negatives, outbox worker minimal role, JSONB constraints and the two-party
ownership transfer flow."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.identity.authority import (
    DeterministicConsentSnapshotResolver,
    TransferVerificationError,
)
from services.identity.domain import (
    AgeEvidenceError,
    IdentityConflictError,
    IdentityNotFoundError,
)
from services.identity.postgres_store import PostgresIdentityStore
from services.identity.service import IdentityService
from services.identity.testing_authorities import TestTransferAuthority

_DSN = os.getenv("MEMORIA_TEST_POSTGRES_DSN", "")
_ROLE_PASSWORD = "memoria_identity_test_password"
_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "postgres_schema.sql"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not _DSN,
        reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL identity contract",
    ),
]


def _with_credentials(
    dsn: str, *, username: str, password: str, database: str | None = None
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    credentials = f"{quote(username)}:{quote(password)}"
    path = f"/{database}" if database else parsed.path
    return urlunsplit(
        (parsed.scheme, f"{credentials}@{host}", path, parsed.query, "")
    )


async def _ensure_role(admin: asyncpg.Connection, name: str) -> None:
    exists = await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", name)
    if not exists:
        await admin.execute(
            f"CREATE ROLE {name} LOGIN NOSUPERUSER NOBYPASSRLS "
            f"PASSWORD '{_ROLE_PASSWORD}'"
        )
    else:
        await admin.execute(
            f"ALTER ROLE {name} LOGIN NOSUPERUSER NOBYPASSRLS "
            f"PASSWORD '{_ROLE_PASSWORD}'"
        )


async def _bootstrap(database: str) -> dict[str, str]:
    """Create the database, roles and schema; return role-scoped DSNs."""
    admin = await asyncpg.connect(_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        for role in (
            "memoria_identity",
            "memoria_identity_registration",
            "memoria_identity_outbox",
            "memoria_identity_migration",
        ):
            await _ensure_role(admin, role)
    finally:
        await admin.close()
    admin_user = urlsplit(_DSN).username or "postgres"
    admin_password = urlsplit(_DSN).password or ""
    admin_dsn = _with_credentials(
        _DSN, username=admin_user, password=admin_password, database=database
    )
    schema_admin = await asyncpg.connect(admin_dsn)
    try:
        await schema_admin.execute(_SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        await schema_admin.close()
    return {
        "admin": admin_dsn,
        "api": _with_credentials(
            _DSN, username="memoria_identity", password=_ROLE_PASSWORD, database=database
        ),
        "registration": _with_credentials(
            _DSN,
            username="memoria_identity_registration",
            password=_ROLE_PASSWORD,
            database=database,
        ),
        "outbox": _with_credentials(
            _DSN,
            username="memoria_identity_outbox",
            password=_ROLE_PASSWORD,
            database=database,
        ),
        "migration": _with_credentials(
            _DSN,
            username="memoria_identity_migration",
            password=_ROLE_PASSWORD,
            database=database,
        ),
    }


async def _service(
    dsns: dict[str, str],
) -> tuple[PostgresIdentityStore, IdentityService, TestTransferAuthority]:
    store = PostgresIdentityStore(
        dsns["api"], registration_dsn=dsns["registration"]
    )
    await store.initialize(expected_role="memoria_identity")
    authority = TestTransferAuthority(b"pg-test-secret-that-is-at-least-32-bytes")
    service = IdentityService(
        store,
        transfer_verifier=authority,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )
    return store, service, authority


async def _guardian(service: IdentityService, *, guardian: str, ward: str, now: datetime) -> str:
    proposed = await service.propose_relationship(
        source_person_id=guardian,
        target_person_id=ward,
        relation_type="guardian_of",
        established_evidence_id="evidence-guardian",
        actor_person_id=guardian,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id, person_id=guardian, now=now
    )
    return (
        await service.confirm_relationship(
            relationship_id=proposed.relationship_id, person_id=ward, now=now
        )
    ).relationship_id


async def _drop_database(database: str) -> None:
    admin = await asyncpg.connect(_DSN)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
    finally:
        await admin.close()


async def test_schema_roles_rls_and_version_chain() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        try:
            admin = await asyncpg.connect(dsns["admin"])
            try:
                attrs = await admin.fetch(
                    """
                    SELECT rolname, rolcanlogin, rolsuper, rolbypassrls
                    FROM pg_roles
                    WHERE rolname IN (
                        'memoria_identity',
                        'memoria_identity_registration',
                        'memoria_identity_outbox',
                        'memoria_identity_migration'
                    )
                    ORDER BY rolname
                    """
                )
                assert len(attrs) == 4
                for row in attrs:
                    assert bool(row["rolcanlogin"])
                    assert not bool(row["rolsuper"])
                    assert not bool(row["rolbypassrls"])
                open_policies = await admin.fetch(
                    """
                    SELECT tablename, policyname, qual, with_check
                    FROM pg_policies
                    WHERE schemaname = 'public'
                      AND tablename LIKE 'identity\\_%'
                    """
                )
                assert open_policies, "expected identity RLS policies"
                for row in open_policies:
                    assert str(row["qual"] or "").strip().lower() != "true"
                    assert str(row["with_check"] or "").strip().lower() != "true"
                rls = await admin.fetch(
                    """
                    SELECT c.relname AS tablename,
                           c.relrowsecurity,
                           c.relforcerowsecurity
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relname LIKE 'identity\\_%'
                      AND c.relkind = 'r'
                    """
                )
                assert {str(row["tablename"]) for row in rls} == {
                    "identity_persons",
                    "identity_relationships",
                    "identity_device_bindings",
                    "identity_device_binding_roles",
                    "identity_audit_events",
                    "identity_outbox",
                    "identity_transfer_intents",
                    "identity_idempotency_records",
                }
                for row in rls:
                    assert bool(row["relrowsecurity"])
                    assert bool(row["relforcerowsecurity"])
                # Dedicated DDL owner owns the objects; runtime roles cannot
                # create schema objects.
                owner_has_create = await admin.fetchval(
                    """
                    SELECT has_schema_privilege(
                        'memoria_identity_owner', 'public', 'CREATE'
                    )
                    """
                )
                assert owner_has_create is True
                for runtime_role in (
                    "memoria_identity",
                    "memoria_identity_registration",
                    "memoria_identity_outbox",
                    "memoria_identity_migration",
                ):
                    runtime_create = await admin.fetchval(
                        """
                        SELECT has_schema_privilege($1, 'public', 'CREATE')
                        """,
                        runtime_role,
                    )
                    assert runtime_create is False
                table_owners = await admin.fetch(
                    """
                    SELECT c.relname, pg_get_userbyid(c.relowner) AS owner
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relname LIKE 'identity\\_%'
                      AND c.relkind = 'r'
                    """
                )
                assert {str(row["owner"]) for row in table_owners} == {
                    "memoria_identity_owner"
                }
                definer_ports = await admin.fetch(
                    """
                    SELECT p.proname,
                           pg_get_userbyid(p.proowner) AS owner,
                           p.prosecdef,
                           p.proconfig
                    FROM pg_proc p
                    JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname = 'public'
                      AND p.proname IN (
                        'identity_person_visible',
                        'identity_relationship_visible',
                        'identity_binding_visible',
                        'identity_audit_visible',
                        'identity_idempotency_visible',
                        'identity_register_person',
                        'identity_write_idempotency',
                        'identity_patch_idempotency_result',
                        'identity_self_update_profile',
                        'identity_declare_age_evidence',
                        'identity_verify_age_evidence',
                        'identity_next_binding_version'
                      )
                    """
                )
                assert len(definer_ports) == 12
                for row in definer_ports:
                    assert str(row["owner"]) == "memoria_identity_owner"
                    assert bool(row["prosecdef"])
                    config = row["proconfig"] or []
                    assert "row_security=on" in config
                # The API role must not hold direct write privileges on the
                # authoritative tables (a GUC is not a credential), and the
                # registration port is EXECUTE-only for the dedicated
                # registration role.
                for table, privilege in (
                    ("identity_persons", "INSERT"),
                    ("identity_audit_events", "INSERT"),
                    ("identity_audit_events", "UPDATE"),
                    ("identity_outbox", "INSERT"),
                    ("identity_outbox", "UPDATE"),
                    ("identity_idempotency_records", "INSERT"),
                    ("identity_idempotency_records", "UPDATE"),
                ):
                    granted = await admin.fetchval(
                        """
                        SELECT has_table_privilege(
                            'memoria_identity', $1::regclass, $2
                        )
                        """,
                        table,
                        privilege,
                    )
                    assert granted is False, f"{table} {privilege} must be revoked"
                api_register_exec = await admin.fetchval(
                    """
                    SELECT has_function_privilege(
                        'memoria_identity',
                        to_regprocedure(
                            'identity_register_person(text,text,text,text,text,'
                            'text,text,text,timestamptz,timestamptz,text,jsonb,'
                            'text)'
                        ),
                        'EXECUTE'
                    )
                    """
                )
                assert api_register_exec is False
                registration_register_exec = await admin.fetchval(
                    """
                    SELECT has_function_privilege(
                        'memoria_identity_registration',
                        to_regprocedure(
                            'identity_register_person(text,text,text,text,text,'
                            'text,text,text,timestamptz,timestamptz,text,jsonb,'
                            'text)'
                        ),
                        'EXECUTE'
                    )
                    """
                )
                assert registration_register_exec is True
                # The standalone unrestricted registration snapshot port must
                # not exist: replay only happens through the compare-or-insert
                # register port.
                snapshot_gone = await admin.fetchval(
                    """
                    SELECT to_regprocedure(
                        'identity_person_registration_snapshot(text)'
                    ) IS NULL
                    """
                )
                assert snapshot_gone is True
                for port, signature in (
                    (
                        "identity_write_audit",
                        "identity_write_audit(text,text,text,text,text,text,"
                        "text,text,jsonb,timestamptz)",
                    ),
                    (
                        "identity_enqueue_outbox",
                        "identity_enqueue_outbox(text,text,text,jsonb,"
                        "timestamptz)",
                    ),
                    (
                        "identity_update_person",
                        "identity_update_person(text,text,text,text,text,text,"
                        "text,text,timestamptz,text)",
                    ),
                ):
                    gone = await admin.fetchval(
                        """
                        SELECT to_regprocedure($1) IS NULL
                        """,
                        signature,
                    )
                    assert gone is True, f"generic port {port} must be removed"
                for port, signature in (
                    (
                        "identity_next_binding_version",
                        "identity_next_binding_version(text)",
                    ),
                    (
                        "identity_write_idempotency",
                        "identity_write_idempotency(text,text,text,text,jsonb,"
                        "timestamptz)",
                    ),
                    (
                        "identity_patch_idempotency_result",
                        "identity_patch_idempotency_result(text,text,jsonb)",
                    ),
                    (
                        "identity_self_update_profile",
                        "identity_self_update_profile(text,text,text,text,"
                        "timestamptz,text)",
                    ),
                    (
                        "identity_declare_age_evidence",
                        "identity_declare_age_evidence(text,text,timestamptz,"
                        "text)",
                    ),
                ):
                    api_exec = await admin.fetchval(
                        """
                        SELECT has_function_privilege(
                            'memoria_identity', to_regprocedure($1), 'EXECUTE'
                        )
                        """,
                        signature,
                    )
                    assert api_exec is True, f"API role must EXECUTE {port}"
                    reg_exec = await admin.fetchval(
                        """
                        SELECT has_function_privilege(
                            'memoria_identity_registration',
                            to_regprocedure($1),
                            'EXECUTE'
                        )
                        """,
                        signature,
                    )
                    assert reg_exec is False, (
                        f"registration role must not EXECUTE {port}"
                    )
            finally:
                await admin.close()

            now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
            parent = (
                await service.register_person(
                    display_name="家长",
                    timezone="Asia/Shanghai",
                    subject_category="adult",
                    age_band="adult",
                    age_evidence_status="verified",
                    age_evidence_id="evidence-pg-adult",
                    now=now,
                )
            ).person_id
            child = (
                await service.register_person(
                    display_name="孩子",
                    timezone="Asia/Shanghai",
                    subject_category="minor",
                    age_band="under_14",
                    age_evidence_status="unverified",
                    now=now,
                )
            ).person_id
            await _guardian(service, guardian=parent, ward=child, now=now)
            v1 = await service.create_binding(
                device_id="dev-pg-a",
                declared_mode="parent_for_child",
                account_owner_person_id=parent,
                primary_subject_ids=(child,),
                roles=((parent, "guardian"),),
                service_profile_version="student-cn-v3",
                policy_bundle_version="policy-cn-minor-v5",
                now=now,
            )
            assert v1.binding_version == 1
            v2 = await service.supersede_binding(
                device_id="dev-pg-a",
                declared_mode="parent_for_child",
                primary_subject_ids=(child,),
                roles=((parent, "guardian"),),
                service_profile_version="student-cn-v4",
                policy_bundle_version="policy-cn-minor-v6",
                actor_person_id=parent,
                now=now + timedelta(minutes=1),
            )
            assert v2.binding_version == 2
            assert v2.supersedes_binding_id == v1.binding_id

            # Cross-person read with the explicit actor is fine.
            assert (
                await service.get_person(child, actor_person_id=parent) is not None
            )

            # Cross-family negative: a foreign actor never sees the other
            # family's binding or its members.
            other_parent = (
                await service.register_person(
                    display_name="另一家",
                    timezone="Asia/Shanghai",
                    subject_category="adult",
                    age_band="adult",
                    age_evidence_status="verified",
                    age_evidence_id="evidence-pg-adult",
                    now=now,
                )
            ).person_id
            manifest_b = await service.get_active_manifest(
                "dev-pg-a", now=now, actor_person_id=other_parent
            )
            assert manifest_b is None
            versions_b = await service.list_binding_versions(
                "dev-pg-a", actor_person_id=other_parent
            )
            assert versions_b == ()
            hidden = await store.get_person(parent, actor_person_id=other_parent)
            assert hidden is None
        finally:
            await store.close()
    finally:
        await _drop_database(database)


@pytest.mark.asyncio
async def test_new_owner_binding_version_allocation_sees_revoked_history() -> None:
    """Version allocation must not be narrowed by the new owner's RLS view."""
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
        first_owner = (
            await service.register_person(
                display_name="原所有者",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-first-owner",
                now=now,
            )
        ).person_id
        second_owner = (
            await service.register_person(
                display_name="新所有者",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-second-owner",
                now=now,
            )
        ).person_id
        first = await service.create_binding(
            device_id="dev-pg-rebind",
            declared_mode="self_use",
            account_owner_person_id=first_owner,
            primary_subject_ids=(first_owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        await service.revoke_binding(
            device_id="dev-pg-rebind",
            actor_person_id=first_owner,
            now=now + timedelta(minutes=1),
        )

        rebound = await service.create_binding(
            device_id="dev-pg-rebind",
            declared_mode="self_use",
            account_owner_person_id=second_owner,
            primary_subject_ids=(second_owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now + timedelta(minutes=2),
        )
        assert first.binding_version == 1
        assert rebound.binding_version == 2
        admin = await asyncpg.connect(dsns["admin"])
        try:
            versions = await admin.fetch(
                """
                SELECT binding_version, status, account_owner_person_id
                FROM identity_device_bindings
                WHERE device_id = $1
                ORDER BY binding_version
                """,
                "dev-pg-rebind",
            )
            assert [(row["binding_version"], row["status"]) for row in versions] == [
                (1, "revoked"),
                (2, "active"),
            ]
            assert versions[1]["account_owner_person_id"] == second_owner
        finally:
            await admin.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_api_role_rls_rejects_cross_actor_binding_read_and_write() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = None
    try:
        store, service, _pg_authority = await _service(dsns)
        now = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="绑定所有者",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-rls-owner",
                now=now,
            )
        ).person_id
        foreign_actor = (
            await service.register_person(
                display_name="其他用户",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-rls-foreign",
                now=now,
            )
        ).person_id
        binding = await service.create_binding(
            device_id=f"rls-device-{uuid.uuid4().hex[:8]}",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            roles=(),
            service_profile_version="profile-rls-v1",
            policy_bundle_version="policy-rls-v1",
            now=now,
        )

        api = await asyncpg.connect(dsns["api"])
        try:
            await api.fetchval(
                "SELECT set_config('app.identity_actor', $1, false)",
                foreign_actor,
            )
            hidden = await api.fetchrow(
                """
                SELECT binding_id
                FROM identity_device_bindings
                WHERE binding_id = $1
                """,
                binding.binding_id,
            )
            assert hidden is None

            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.execute(
                    """
                    INSERT INTO identity_device_bindings (
                        binding_id, device_id, declared_mode,
                        account_owner_person_id, binding_version, status, reason,
                        valid_from, service_profile_version,
                        policy_bundle_version, created_at
                    ) VALUES (
                        $1, $2, 'self_use', $3, 1, 'active', 'create',
                        $4, 'profile-forged', 'policy-forged', $4
                    )
                    """,
                    f"forged-binding-{uuid.uuid4().hex[:8]}",
                    f"forged-device-{uuid.uuid4().hex[:8]}",
                    owner,
                    now,
                )
        finally:
            await api.close()
    finally:
        if store is not None:
            await store.close()
        await _drop_database(database)


async def test_initialize_rejects_schema_without_force_rls() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store = PostgresIdentityStore(
        dsns["api"], registration_dsn=dsns["registration"]
    )
    try:
        admin = await asyncpg.connect(dsns["admin"])
        try:
            await admin.execute(
                "ALTER TABLE identity_persons NO FORCE ROW LEVEL SECURITY"
            )
        finally:
            await admin.close()
        with pytest.raises(RuntimeError, match="force row-level security"):
            await store.initialize(expected_role="memoria_identity")
    finally:
        await store.close()
        await _drop_database(database)


async def test_transfer_intent_two_party_and_atomic_events() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="本人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        target = (
            await service.register_person(
                display_name="新主人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        v1 = await service.create_binding(
            device_id="dev-pg-tx",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        policy_id, step_up = _pg_authority.mint(
            actor_person_id=owner,
            subject_person_id=target,
            device_id="dev-pg-tx",
            binding_id=v1.binding_id,
            binding_version=v1.binding_version,
            expires_at=now + timedelta(days=8),
        )
        intent = await service.create_transfer_intent(
            device_id="dev-pg-tx",
            to_account_owner_person_id=target,
            step_up_evidence_id=step_up,
            policy_receipt_id=policy_id,
            idempotency_key="idem-pg-0001",
            actor_person_id=owner,
            now=now + timedelta(minutes=1),
        )
        # Idempotent replay with the same key returns the same intent.
        replayed = await service.create_transfer_intent(
            device_id="dev-pg-tx",
            to_account_owner_person_id=target,
            step_up_evidence_id=step_up,
            policy_receipt_id=policy_id,
            idempotency_key="idem-pg-0001",
            actor_person_id=owner,
            now=now + timedelta(minutes=2),
        )
        assert replayed.transfer_id == intent.transfer_id
        # Forged tickets never create an intent.
        with pytest.raises(TransferVerificationError):
            await service.create_transfer_intent(
                device_id="dev-pg-tx",
                to_account_owner_person_id=target,
                step_up_evidence_id="step-up-1",
                policy_receipt_id="forged-receipt-id",
                idempotency_key="idem-pg-0002",
                actor_person_id=owner,
                now=now + timedelta(minutes=3),
            )
        accepted = await service.accept_transfer_intent(
            transfer_id=intent.transfer_id,
            actor_person_id=target,
            primary_subject_ids=(target,),
            declared_mode="self_use",
            now=now + timedelta(minutes=4),
            idempotency_key=str(uuid.uuid4()),
        )
        assert accepted.binding_version == 2
        assert accepted.account_owner_id == target
        assert all(role.person_id != owner for role in accepted.roles)

        admin = await asyncpg.connect(dsns["admin"])
        try:
            audit_actions = {
                str(row["action"])
                for row in await admin.fetch(
                    "SELECT action FROM identity_audit_events"
                )
            }
            assert "transfer.create" in audit_actions
            assert "transfer.accept" in audit_actions
            assert "binding.transfer" in audit_actions
            outbox_topics = {
                str(row["topic"])
                for row in await admin.fetch(
                    "SELECT topic FROM identity_outbox"
                )
            }
            assert "identity.transfer.created" in outbox_topics
            assert "identity.transfer.accepted" in outbox_topics
            assert "identity.binding.transferred" in outbox_topics
            # Audit and outbox share the business event identity.
            pairs = await admin.fetch(
                """
                SELECT a.event_id, o.event_id AS outbox_event_id
                FROM identity_audit_events a
                JOIN identity_outbox o ON o.topic = 'identity.transfer.created'
                WHERE a.action = 'transfer.create'
                """
            )
            assert pairs and str(pairs[0]["event_id"]) == str(
                pairs[0]["outbox_event_id"]
            )
        finally:
            await admin.close()
    finally:
        await store.close()
        await _drop_database(database)


async def test_outbox_worker_role_is_minimal() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="本人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        await service.create_binding(
            device_id="dev-pg-worker",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        worker = await asyncpg.connect(dsns["outbox"])
        try:
            rows = await worker.fetch("SELECT topic FROM identity_outbox")
            assert {str(row["topic"]) for row in rows} == {
                "identity.person.registered",
                "identity.binding.created"
            }
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await worker.fetch("SELECT person_id FROM identity_persons")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await worker.fetch(
                    "SELECT binding_id FROM identity_device_bindings"
                )
        finally:
            await worker.close()
    finally:
        await store.close()
        await _drop_database(database)


async def test_jsonb_constraints_reject_non_array_permissions() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        owner = (
            await service.register_person(
                display_name="本人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        manifest = await service.create_binding(
            device_id="dev-pg-jsonb",
            declared_mode="self_use",
            account_owner_person_id=owner,
            primary_subject_ids=(owner,),
            service_profile_version="self-v1",
            policy_bundle_version="policy-self-v1",
            now=now,
        )
        admin = await asyncpg.connect(dsns["admin"])
        try:
            # Existing role grants are immutable: any permissions rewrite is
            # refused by the immutable trigger before the JSONB CHECK runs.
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await admin.execute(
                    """
                    INSERT INTO identity_device_binding_roles (
                        binding_id, person_id, role, status,
                        permissions_json, granted_at
                    ) VALUES ($1, $2, 'member', 'active',
                              '{"not": "an array"}'::jsonb, now())
                    """,
                    manifest.binding_id,
                    owner,
                )
            with pytest.raises(asyncpg.exceptions.RaiseError):
                await admin.execute(
                    """
                    UPDATE identity_device_binding_roles
                    SET permissions_json = '["device.status.view"]'::jsonb
                    WHERE binding_id = $1 AND role = 'account_owner'
                    """,
                    manifest.binding_id,
                )
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await admin.execute(
                    """
                    INSERT INTO identity_audit_events (
                        event_id, action, payload_json, created_at
                    ) VALUES ('audit-bad-json', 'x', '[1,2]'::jsonb, now())
                    """
                )
        finally:
            await admin.close()
    finally:
        await store.close()
        await _drop_database(database)


async def test_registration_authority_role_gated_and_guc_not_a_credential() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        # 1) A raw SQL client holding the API DSN cannot forge the
        # registration/internal GUC to create persons or write audit, outbox
        # or idempotency rows: the API role simply has no INSERT privilege.
        api = await asyncpg.connect(dsns["api"])
        try:
            for scope in ("registration", "internal"):
                await api.execute(f"SET app.identity_scope = '{scope}'")
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_persons (
                            person_id, display_name, subject_category, age_band,
                            age_evidence_status, locale, timezone, status,
                            created_at, updated_at
                        ) VALUES (
                            $1, '伪造注册', 'unknown', 'unknown', 'unverified',
                            'zh-CN', 'Asia/Shanghai', 'active', now(), now()
                        )
                        """,
                        f"forged-{scope}",
                    )
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_audit_events (
                            event_id, action, payload_json, created_at
                        ) VALUES ($1, 'person.register', '{}'::jsonb, now())
                        """,
                        f"forged-audit-{scope}",
                    )
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_outbox (
                            outbox_id, event_id, topic, payload_json, status,
                            attempts, locked_until, last_error_code, created_at,
                            delivered_at, updated_at
                        ) VALUES (
                            $1, $2, 'identity.binding.created', '{}'::jsonb,
                            'pending', 0, NULL, NULL, now(), NULL, now()
                        )
                        """,
                        f"forged-outbox-{scope}",
                        f"forged-evt-{scope}",
                    )
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(
                        """
                        INSERT INTO identity_idempotency_records (
                            scope_key, idempotency_key, operation, content_hash,
                            result_payload_json, created_at
                        ) VALUES (
                            $1, $2, 'transfer.create', $3, '{}'::jsonb, now()
                        )
                        """,
                        f"forged-{scope}:transfer:x",
                        "forged-key",
                        "a" * 64,
                    )
            # Forging the actor GUC does not unlock person creation either.
            await api.execute("SET app.identity_scope = 'registration'")
            await api.execute("SET app.identity_actor = 'somebody-else'")
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await api.execute(
                    """
                    INSERT INTO identity_persons (
                        person_id, display_name, subject_category, age_band,
                        age_evidence_status, locale, timezone, status,
                        created_at, updated_at
                    ) VALUES (
                        $1, '伪造actor', 'unknown', 'unknown', 'unverified',
                        'zh-CN', 'Asia/Shanghai', 'active', now(), now()
                    )
                    """,
                    "forged-actor",
                )
        finally:
            await api.close()

        # 2) The dedicated registration path succeeds and persists person +
        # audit + outbox in one transaction.
        owner = (
            await service.register_person(
                display_name="本人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-pg-adult",
                now=now,
            )
        ).person_id
        admin = await asyncpg.connect(dsns["admin"])
        try:
            person = await admin.fetchrow(
                "SELECT person_id FROM identity_persons WHERE person_id = $1",
                owner,
            )
            assert person is not None
            audit = await admin.fetchrow(
                """
                SELECT event_id, action FROM identity_audit_events
                WHERE person_id = $1
                """,
                owner,
            )
            assert audit is not None
            assert str(audit["action"]) == "person.register"
            outbox = await admin.fetchrow(
                """
                SELECT outbox_id FROM identity_outbox
                WHERE topic = 'identity.person.registered'
                  AND payload_json->>'person_id' = $1
                """,
                owner,
            )
            assert outbox is not None
        finally:
            await admin.close()

        # 3) The registration role holds no table privileges: it can neither
        # read relationships/bindings/transfers nor update other persons.
        reg = await asyncpg.connect(dsns["registration"])
        try:
            for statement in (
                "SELECT person_id FROM identity_persons",
                "SELECT relationship_id FROM identity_relationships",
                "SELECT binding_id FROM identity_device_bindings",
                "SELECT transfer_id FROM identity_transfer_intents",
                "SELECT event_id FROM identity_audit_events",
            ):
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await reg.fetch(statement)
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await reg.execute(
                    """
                    UPDATE identity_persons
                    SET display_name = '篡改'
                    WHERE person_id = $1
                    """,
                    owner,
                )
        finally:
            await reg.close()

        # 4) Cross-actor age DECLARATION changes (guardian -> child) succeed
        # through the action-level port: a guardian maintains the child's
        # minor band, never adult/verified status.
        child = (
            await service.register_person(
                display_name="孩子",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        await _guardian(service, guardian=owner, ward=child, now=now)
        grown = await service.declare_age_evidence(
            person_id=child,
            age_band="14_17",
            actor_person_id=owner,
            now=now + timedelta(days=1),
        )
        assert grown.subject_category == "minor"
        assert grown.age_band == "14_17"
    finally:
        await store.close()
        await _drop_database(database)


async def test_register_compare_or_insert_replay_drift_and_concurrency() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        # 1) Exact replay returns the persisted DB row (never the unpersisted
        #    request snapshot) and writes no duplicate trail rows.
        first = await service.register_person(
            display_name="幂等",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            now=now,
        )
        replayed = await service.register_person(
            display_name="幂等",
            timezone="Asia/Shanghai",
            subject_category="unknown",
            age_band="unknown",
            age_evidence_status="unverified",
            person_id=first.person_id,
            now=now + timedelta(minutes=5),
        )
        admin = await asyncpg.connect(dsns["admin"])
        try:
            row = await admin.fetchrow(
                "SELECT * FROM identity_persons WHERE person_id = $1",
                first.person_id,
            )
            assert row is not None
            assert replayed.display_name == str(row["display_name"])
            assert replayed.subject_category == str(row["subject_category"])
            assert replayed.age_band == str(row["age_band"])
            assert replayed.locale == str(row["locale"])
            assert replayed.timezone == str(row["timezone"])
            person_count = await admin.fetchval(
                "SELECT count(*) FROM identity_persons WHERE person_id = $1",
                first.person_id,
            )
            assert person_count == 1
            audit_count = await admin.fetchval(
                "SELECT count(*) FROM identity_audit_events "
                "WHERE person_id = $1",
                first.person_id,
            )
            assert audit_count == 1
            outbox_count = await admin.fetchval(
                """
                SELECT count(*) FROM identity_outbox
                WHERE payload_json->>'person_id' = $1
                """,
                first.person_id,
            )
            assert outbox_count == 1
        finally:
            await admin.close()

        # 2) Content drift is an explicit conflict.
        with pytest.raises(IdentityConflictError):
            await service.register_person(
                display_name="改名",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=first.person_id,
                now=now,
            )

        # 3) The registration role cannot read another person without proving
        #    the full canonical content: wrong/partial fields raise and the
        #    error reveals no row content.
        canonical_payload = json.dumps(
            {
                "person_id": first.person_id,
                "display_name": "改名",
                "subject_category": "unknown",
                "age_band": "unknown",
                "age_evidence_status": "unverified",
                "locale": "zh-CN",
                "timezone": "Asia/Shanghai",
                "status": "active",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
            ensure_ascii=False,
        )
        reg = await asyncpg.connect(dsns["registration"])
        try:
            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await reg.fetchval(
                    """
                    SELECT identity_register_person(
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13
                    )
                    """,
                    first.person_id,
                    "改名",
                    "unknown",
                    "unknown",
                    "unverified",
                    "zh-CN",
                    "Asia/Shanghai",
                    "active",
                    now,
                    now,
                    None,
                    canonical_payload,
                    None,
                )
            message = str(excinfo.value)
            assert "different authoritative fields" in message
            assert "幂等" not in message
            with pytest.raises(asyncpg.PostgresError):
                await reg.fetchval(
                    """
                    SELECT identity_register_person(
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NULL,
                        NULL
                    )
                    """,
                    first.person_id,
                    "幂等",
                    "unknown",
                    "unknown",
                    "unverified",
                    "zh-CN",
                    "Asia/Shanghai",
                    "active",
                    now,
                    now,
                    None,
                )
        finally:
            await reg.close()

        # 4) Concurrency: exact replays both return the persisted row; a
        #    drift race yields exactly one winner and one conflict.
        same_id = f"conc-same-{uuid.uuid4().hex[:8]}"
        winner_a, winner_b = await asyncio.gather(
            service.register_person(
                display_name="并发",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=same_id,
                now=now,
            ),
            service.register_person(
                display_name="并发",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=same_id,
                now=now,
            ),
        )
        assert winner_a.person_id == winner_b.person_id == same_id
        assert winner_a.display_name == winner_b.display_name == "并发"
        admin = await asyncpg.connect(dsns["admin"])
        try:
            row = await admin.fetchrow(
                "SELECT * FROM identity_persons WHERE person_id = $1",
                same_id,
            )
            assert row is not None
            assert winner_a.display_name == str(row["display_name"])
            assert winner_b.display_name == str(row["display_name"])
        finally:
            await admin.close()
        drift_id = f"conc-drift-{uuid.uuid4().hex[:8]}"
        drift_results = await asyncio.gather(
            service.register_person(
                display_name="甲",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=drift_id,
                now=now,
            ),
            service.register_person(
                display_name="乙",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                person_id=drift_id,
                now=now,
            ),
            return_exceptions=True,
        )
        assert sorted(type(result).__name__ for result in drift_results) == [
            "IdentityConflictError",
            "PersonSubject",
        ]
    finally:
        await store.close()
        await _drop_database(database)


async def test_age_authority_raw_update_blocked_and_verification_action_level() -> None:
    database = f"memoria_identity_{uuid.uuid4().hex[:10]}"
    dsns = await _bootstrap(database)
    store, service, _pg_authority = await _service(dsns)
    try:
        now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        # 1) The API role cannot rewrite age/status/profile fields with raw
        #    UPDATE even after forging the actor GUC (UPDATE grant revoked).
        victim = (
            await service.register_person(
                display_name="受害者",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        api = await asyncpg.connect(dsns["api"])
        try:
            await api.execute(
                "SELECT set_config('app.identity_actor', $1, false)", victim
            )
            for statement in (
                """
                UPDATE identity_persons
                SET subject_category = 'adult',
                    age_band = 'adult',
                    age_evidence_status = 'verified'
                WHERE person_id = $1
                """,
                """
                UPDATE identity_persons
                SET status = 'disabled'
                WHERE person_id = $1
                """,
                """
                UPDATE identity_persons
                SET display_name = '改名'
                WHERE person_id = $1
                """,
            ):
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await api.execute(statement, victim)
            # The action-level self port cannot claim adult either.
            with pytest.raises(asyncpg.PostgresError):
                await api.execute(
                    """
                    SELECT identity_declare_age_evidence($1, 'adult', $2, $1)
                    """,
                    victim,
                    now + timedelta(days=1),
                )
            # The verification port is not executable by the API role.
            with pytest.raises(asyncpg.PostgresError):
                await api.execute(
                    """
                    SELECT identity_verify_age_evidence(
                        $1, 'adult', 'verified', 'evidence-x', $1, $2
                    )
                    """,
                    victim,
                    now + timedelta(days=1),
                )
        finally:
            await api.close()
        row = await store.get_person(
            victim, actor_person_id=victim
        )
        assert row is not None
        assert row.subject_category == "unknown"
        assert row.age_evidence_status == "unverified"
        assert row.status == "active"
        assert row.display_name == "受害者"

        # 2) Authoritative verification: verifier must be a verified adult
        #    with visibility (active guardian relationship); evidence id is
        #    mandatory and the audit/outbox trail lands in the same
        #    transaction.
        verifier = (
            await service.register_person(
                display_name="核验人",
                timezone="Asia/Shanghai",
                subject_category="adult",
                age_band="adult",
                age_evidence_status="verified",
                age_evidence_id="evidence-verifier",
                now=now,
            )
        ).person_id
        target = (
            await service.register_person(
                display_name="待核验",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        # No relationship yet: the verifier cannot see the target (fail
        # closed on PostgreSQL).
        with pytest.raises(IdentityNotFoundError):
            await service.verify_age_evidence(
                person_id=target,
                evidence_id="evidence-verify-1",
                verifier_person_id=verifier,
                now=now + timedelta(days=1),
            )
        await _guardian(service, guardian=verifier, ward=target, now=now)
        verified = await service.verify_age_evidence(
            person_id=target,
            evidence_id="evidence-verify-1",
            verifier_person_id=verifier,
            now=now + timedelta(days=1),
        )
        assert verified.subject_category == "adult"
        assert verified.age_band == "adult"
        assert verified.age_evidence_status == "verified"
        admin = await asyncpg.connect(dsns["admin"])
        try:
            audit = await admin.fetchrow(
                """
                SELECT action, payload_json FROM identity_audit_events
                WHERE action = 'person.age_verification'
                  AND person_id = $1
                """,
                target,
            )
            assert audit is not None
            audit_payload = json.loads(str(audit["payload_json"]))
            assert audit_payload["evidence_id"] == "evidence-verify-1"
            assert audit_payload["verifier_person_id"] == verifier
            outbox = await admin.fetchrow(
                """
                SELECT topic FROM identity_outbox
                WHERE topic = 'identity.person.age_verified'
                  AND payload_json->>'person_id' = $1
                """,
                target,
            )
            assert outbox is not None
        finally:
            await admin.close()

        # 3) A non-verified guardian cannot verify anyone as an adult.
        plain = (
            await service.register_person(
                display_name="普通监护人",
                timezone="Asia/Shanghai",
                subject_category="unknown",
                age_band="unknown",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        ward = (
            await service.register_person(
                display_name="被监护",
                timezone="Asia/Shanghai",
                subject_category="minor",
                age_band="under_14",
                age_evidence_status="unverified",
                now=now,
            )
        ).person_id
        await _guardian(service, guardian=plain, ward=ward, now=now)
        with pytest.raises(AgeEvidenceError):
            await service.verify_age_evidence(
                person_id=ward,
                evidence_id="evidence-guardian-forge",
                verifier_person_id=plain,
                now=now + timedelta(days=1),
            )
        # Self-verification is rejected even for a verified adult.
        with pytest.raises(AgeEvidenceError):
            await service.verify_age_evidence(
                person_id=verifier,
                evidence_id="evidence-self",
                verifier_person_id=verifier,
                now=now + timedelta(days=1),
            )
    finally:
        await store.close()
        await _drop_database(database)
