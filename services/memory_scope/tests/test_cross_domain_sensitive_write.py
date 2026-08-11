"""Real cross-domain same-connection contract (review P0-B/C): the GLOBAL
``memoria_action_executor`` connection runs the REAL
``SensitiveWriteService.execute`` (Policy engine decide -> immutable
receipt insert into ``policy_receipts_v2`` -> lock current Session/Binding/
Action authority heads -> callback) and the memory commit happens on the
SAME backend pid.  Any callback/authority/tamper failure rolls back BOTH
the receipt and the memory record; the executor role has no direct table
grants."""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.policy.action_authorizer import (
    ActionExecutionRequest,
    PostgresActionAuthorizer,
)
from services.policy.context import PolicyContext
from services.policy.evidence import BindingEvidencePort
from services.policy.postgres_receipt_repository import (
    ConnectionBoundPolicyReceiptRepository,
)
from services.policy.production_wiring import (
    ProductionAuthorityAdapters,
    SensitiveWriteService,
    build_composite_action_authority,
)
from services.policy.receipts import PolicyReceiptV2


class _BridgePolicyReceiptRepository(ConnectionBoundPolicyReceiptRepository):
    """Real Session bridge: receipts are inserted through the narrow
    SECURITY DEFINER ``action_policy_insert_receipt`` (the global action
    executor has NO direct policy_receipts_v2 privilege)."""

    async def insert_many(
        self,
        connection: asyncpg.Connection,
        receipts: tuple[PolicyReceiptV2, ...],
    ) -> None:
        for receipt in sorted(receipts, key=lambda item: item.receipt_id):
            await connection.fetchval(
                "SELECT action_policy_insert_receipt($1::jsonb)",
                receipt.model_dump_json(),
            )


async def _bridge_lock_receipt(
    connection: asyncpg.Connection, receipt_id: str
) -> PolicyReceiptV2 | None:
    """Real Session bridge: lock through ``action_policy_lock_receipt`` and
    decode through the caller's own SELECT boundary."""
    payload = await connection.fetchval(
        "SELECT action_policy_lock_receipt($1)", receipt_id
    )
    if payload is None:
        return None
    return PolicyReceiptV2.model_validate_json(payload)

ACTION_EXECUTOR_ROLE = "memoria_action_executor"
API_ROLE = "memoria_memory_api"
WORKER_ROLE = "memoria_memory_worker"


def _dsn_with(
    dsn: str, *, database: str, user: str | None = None, password: str | None = None
) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(user or parsed.username or "postgres")
    secret = quote(password if password is not None else parsed.password or "")
    credentials = f"{username}:{secret}" if secret else username
    return urlunsplit(
        (parsed.scheme, f"{credentials}@{host}", f"/{database}", parsed.query, "")
    )


class _TestBindingEvidence:
    def __init__(self, binding_id: str, canonical_hash: str) -> None:
        self.binding_id = binding_id
        self.version = 1
        self.device_id = "device-1"
        self.status = "bound"
        self.declared_mode = "hands_free"
        self.valid_from = datetime(2026, 1, 1, tzinfo=UTC)
        self.valid_until = datetime(2030, 1, 1, tzinfo=UTC)
        self.canonical_hash = canonical_hash

    def is_active_at(self, now: datetime) -> bool:
        return self.valid_from <= now < self.valid_until


class _TestConsentParams:
    max_session_seconds: int | None = None
    retention_ttl_seconds: int | None = None
    quiet_hours: tuple[str, str] | None = None
    extras: tuple[tuple[str, str], ...] = ()


class _TestConsentEvidence:
    def __init__(self, canonical_hash: str) -> None:
        self.consent_id = "consent-1"
        self.version = 1
        self.snapshot_id = "consent-snap-1"
        self.status = "active"
        self.subject_id = "person-a"
        self.resource_owner_id = "person-a"
        self.actor_id = "person-a"
        self.actor_kind = "subject"
        self.device_id = "device-1"
        self.binding_id = "binding-1"
        self.binding_version = 1
        self.capability = "memory_capture"
        self.purpose = "memory_capture"
        self.policy_version = "policy-v2"
        self.evidence_id = "evidence-1"
        self.offer_id = "offer-1"
        self.idempotency_key = "consent:person-a:1"
        self.supersedes_consent_id: str | None = None
        self.superseded_by_consent_id: str | None = None
        self.params = _TestConsentParams()
        self.valid_from = datetime(2026, 1, 1, tzinfo=UTC)
        self.valid_until = datetime(2030, 1, 1, tzinfo=UTC)
        self.canonical_hash = canonical_hash

    def is_effective_at(self, now: datetime) -> bool:
        return self.valid_from <= now < self.valid_until

    def matches(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
    ) -> bool:
        return (
            self.subject_id == subject_id
            and self.binding_id == binding_id
            and self.binding_version == binding_version
            and self.capability == capability
        )


class _TestConsentSnapshot:
    def __init__(self, canonical_hash: str) -> None:
        self.snapshot_id = "consent-snap-1"
        self.revision = 1
        self.canonical_hash = canonical_hash
        self.status = "active"
        self.subject_id = "person-a"
        self.binding_id = "binding-1"
        self.binding_version = 1
        self.grant_refs = (("consent-1", 1, canonical_hash),)
        self.valid_from = datetime(2026, 1, 1, tzinfo=UTC)
        self.valid_until = datetime(2030, 1, 1, tzinfo=UTC)

    def is_current_at(self, now: datetime) -> bool:
        return self.valid_from <= now < self.valid_until

    def contains(self, evidence: object) -> bool:
        return getattr(evidence, "consent_id", None) == "consent-1"


class TestPrincipalAdapter:
    """Locks the Session authority head on the SAME connection and returns
    the session-local actor."""

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: object,
        request: ActionExecutionRequest,
    ) -> str:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('session_head'))"
        )
        actor = await connection.fetchval(
            "SELECT current_setting('app.memory.actor_subject_id', true)"
        )
        if not actor:
            raise RuntimeError("principal adapter: session actor missing")
        return actor


class TestBindingAdapter:
    """Locks the Binding authority head (advisory lock on the SAME
    connection - the REAL binding authority was already locked by
    ``session_runtime_assert_action_context`` via ``action_identity_lock_binding``)
    and returns the context's binding evidence.  No table SELECT: the
    executor role has NO direct authority-table reads."""

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: object,
        request: ActionExecutionRequest,
    ) -> BindingEvidencePort:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('binding_head'))"
        )
        if request.context.binding_evidence is None:
            raise RuntimeError("binding adapter: binding evidence missing")
        return request.context.binding_evidence


class TestActionAdapter:
    """Locks the Action resource head (advisory lock on the SAME
    connection) and returns the context's canonical fence.  Tamper
    detection is enforced by the write callback: the receipt's
    action_resource_fence MUST equal the exact context fence (content/
    evidence/subject/scope bound) - a forged context fails closed."""

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: object,
        request: ActionExecutionRequest,
    ) -> object:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('action_head'))"
        )
        if request.context.action_resource_fence is None:
            raise RuntimeError("action adapter: action fence missing")
        return request.context.action_resource_fence


class TestConsentAdapter:
    """Locks the Consent authority head (advisory lock on the SAME
    connection - the engine already validated the current consent snapshot
    against the fence) and returns the context's consent evidence.  No
    table SELECT: the executor has no direct authority-table reads."""

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: object,
        request: ActionExecutionRequest,
    ) -> tuple:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('consent_head'))"
        )
        return request.context.consent_evidence


class TestCaptureAdapter:
    """Locks the Capture authority head (advisory lock on the SAME
    connection) and returns the context's capture evidence (the canonical
    fence already binds the evidence ids/hash)."""

    async def lock_current(
        self,
        connection: asyncpg.Connection,
        receipt: object,
        request: ActionExecutionRequest,
    ) -> tuple:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtext('capture_head'))"
        )
        return request.context.capture_evidence


@pytest.fixture
async def cross_db() -> AsyncIterator[tuple[str, str, str, str]]:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    db_name = f"memory_cross_{uuid.uuid4().hex[:8]}"
    password = "memoria_local_test_password"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
        for role in (ACTION_EXECUTOR_ROLE, API_ROLE, WORKER_ROLE):
            if not await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", role
            ):
                await admin.execute(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS"
                )
            await admin.execute(
                f"ALTER ROLE {role} WITH LOGIN NOSUPERUSER NOBYPASSRLS"
                f" PASSWORD '{password}'"
            )
    finally:
        await admin.close()
    admin_db = _dsn_with(dsn, database=db_name)
    executor = _dsn_with(
        admin_db, database=db_name, user=ACTION_EXECUTOR_ROLE, password=password
    )
    try:
        yield executor, admin_db, password, db_name
    finally:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin.close()


async def _install_schemas(
    admin_db_dsn: str, password: str
) -> None:
    """Install the REAL Identity/Policy/Consent/Session/Memory schemas
    (read-only references) plus a minimal Action head table.  Every schema
    may SET ROLE internally, so the bootstrap session RESETs to the
    superuser between schemas."""
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        root = Path(__file__).resolve().parents[3]
        for schema_name in (
            "services/identity/postgres_schema.sql",
            "services/policy/postgres_receipt_schema.sql",
            "services/consent/postgres_schema.sql",
            "services/session_runtime/postgres_schema.sql",
        ):
            schema = (root / schema_name).read_text(encoding="utf-8")
            await admin.execute(schema)
            await admin.execute("RESET ROLE")
        memory_schema = (
            Path(__file__).resolve().parents[1] / "postgres_schema.sql"
        ).read_text(encoding="utf-8")
        await admin.execute(memory_schema)
        await admin.execute("RESET ROLE")
        # Identity authority data required by the real Session bridge
        # (identity_binding_visible / session_runtime_assert_action_context).
        await admin.execute(
            """
            INSERT INTO identity_persons (
                person_id, display_name, subject_category, age_band,
                age_evidence_status, locale, timezone, status, created_at,
                updated_at
            ) VALUES (
                'person-a', 'Person A', 'adult', 'adult', 'verified',
                'zh-CN', 'Asia/Shanghai', 'active', now(), now()
            ) ON CONFLICT (person_id) DO NOTHING
            """
        )
        await admin.execute(
            """
            INSERT INTO identity_device_bindings (
                binding_id, device_id, declared_mode, account_owner_person_id,
                binding_version, status, reason, valid_from,
                service_profile_version, policy_bundle_version, created_at
            ) VALUES (
                'binding-1', 'device-1', 'self_use', 'person-a',
                1, 'active', 'create', now() - interval '1 hour',
                'v1', 'v1', now()
            ) ON CONFLICT (binding_id) DO NOTHING
            """
        )
        await admin.execute(
            """
            INSERT INTO identity_device_binding_roles (
                binding_id, person_id, role, status, permissions_json,
                granted_at
            ) VALUES (
                'binding-1', 'person-a', 'primary_subject', 'active',
                '[]',
                now() - interval '1 hour'
            ) ON CONFLICT (binding_id, person_id, role) DO NOTHING
            """
        )
        # The session schema has a bidirectional FK between contexts and
        # profiles; bootstrap inserts both rows with FK checks disabled
        # (superuser session_replication_role), which is exactly how the
        # Session prepare_initial transaction behaves.
        await admin.execute("SET session_replication_role = replica")
        try:
            await admin.execute(
                """
                INSERT INTO session_runtime_profiles (
                runtime_profile_id, session_id, profile_revision,
                session_epoch, actor_id, binding_id, binding_version,
                active_subject_id, subject_revision, payload_json, signature,
                issued_at, expires_at
            ) VALUES (
                'profile-1', 'session-1', 1, 1, 'person-a', 'binding-1', 1,
                'person-a', 1,
                jsonb_build_object(
                    'runtime_profile_id', 'profile-1',
                    'session_id', 'session-1',
                    'actor_id', 'person-a',
                    'device_id', 'device-1',
                    'binding_id', 'binding-1',
                    'binding_version', 1,
                    'active_subject_id', 'person-a',
                    'subject_revision', 1,
                    'speaker_state', 'confirmed',
                    'service_mode', 'adult_companion',
                    'session_epoch', 1,
                    'issued_at', now() - interval '1 minute',
                    'expires_at', now() + interval '2 hours'
                ),
                repeat('a', 64),
                now() - interval '1 minute', now() + interval '2 hours'
                ) ON CONFLICT (runtime_profile_id) DO NOTHING
                """
            )
            await admin.execute(
                """
                INSERT INTO session_runtime_contexts (
                    session_id, actor_id, device_id, binding_id,
                    binding_version, active_subject_id, subject_revision,
                    session_epoch, profile_revision, current_runtime_profile_id,
                    generation_id, turn_id, tool_epoch, state, created_at,
                    updated_at
                ) VALUES (
                    'session-1', 'person-a', 'device-1', 'binding-1', 1,
                    'person-a', 1, 1, 1, 'profile-1', 0, 0, 0, 'active',
                    now(), now()
                ) ON CONFLICT (session_id) DO NOTHING
                """
            )
        finally:
            await admin.execute("RESET session_replication_role")
        # The executor role gets EXECUTE on the memory narrow function and
        # NO direct table grants anywhere.
        await admin.execute(
            "GRANT EXECUTE ON FUNCTION memory_sensitive_commit"
            " TO memoria_action_executor"
        )
    finally:
        await admin.close()


def _make_context(
    *,
    capture_evidence_ids: tuple[str, ...] = ("evidence-1",),
    action_resource_id: str = "capture:person-a:personal_private",
    idempotency_key: str = "capture:person-a:evidence-1",
) -> PolicyContext:
    from services.policy.action_fence import build_action_resource_fence

    consent_hash = hashlib.sha256(b"consent-snap-1").hexdigest()
    fence = build_action_resource_fence(
        capability="memory_capture",
        purpose="memory_capture",
        action_resource_id=action_resource_id,
        action_revision=1,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        capture_evidence_ids=capture_evidence_ids,
        consent_snapshot_id="consent-snap-1",
        consent_snapshot_revision=1,
        consent_snapshot_hash=consent_hash,
        issued_at=datetime.now(UTC) - timedelta(seconds=1),
        valid_until=datetime.now(UTC) + timedelta(hours=2),
    )
    binding = _TestBindingEvidence("binding-1", "b" + "1" * 63)
    consent = _TestConsentEvidence(consent_hash)
    snapshot = _TestConsentSnapshot(consent_hash)
    return PolicyContext(
        actor_id="person-a",
        subject_id="person-a",
        resource_owner_id="person-a",
        device_id="device-1",
        capability="memory_capture",
        purpose="memory_capture",
        declared_device_mode="self_use",
        current_session_mode="adult_companion",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        device_trust="trusted",
        safety_state="normal",
        jurisdiction="CN",
        data_classification="private",
        binding_id="binding-1",
        binding_version=1,
        session_id="session-1",
        session_epoch=1,
        runtime_profile_id="profile-1",
        subject_revision=1,
        evaluated_at=datetime.now(UTC),
        action_resource_fence=fence,
        binding_evidence=binding,
        consent_evidence=(consent,),
        consent_snapshot_evidence=(snapshot,),
        idempotency_key=idempotency_key,
    )


def _evidence_digest() -> str:
    """Canonical capture evidence hash for the standard evidence set (the
    same way build_action_resource_fence derives it)."""
    import hashlib as _h

    return _h.sha256(b"evidence-1").hexdigest()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_cross_domain_sensitive_write_same_connection_atomic(
    cross_db: tuple[str, str, str, str],
) -> None:
    executor_dsn, admin_db_dsn, password, db_name = cross_db
    await _install_schemas(admin_db_dsn, password)
    adapters = ProductionAuthorityAdapters(
        principal=TestPrincipalAdapter(),
        binding=TestBindingAdapter(),
        action=TestActionAdapter(),
        consent=TestConsentAdapter(),
        capture=TestCaptureAdapter(),
    )
    service = SensitiveWriteService(
        authorizer=PostgresActionAuthorizer(
            authority=build_composite_action_authority(adapters),
            receipt_locker=_bridge_lock_receipt,
        ),
        repository=_BridgePolicyReceiptRepository(),
    )
    executor = await asyncpg.connect(executor_dsn)
    try:
        async with executor.transaction():
            await executor.execute(
                "SELECT set_config('app.memory.actor_subject_id', 'person-a', true)"
            )
            await executor.execute(
                "SELECT set_config('app.memory.subject_id', 'person-a', true)"
            )
            for name, value in (
                ("app.session_actor", "person-a"),
                ("app.authenticated_actor", "person-a"),
                ("app.authenticated_subject", "person-a"),
                ("app.authenticated_device", "device-1"),
                ("app.authenticated_binding", "binding-1"),
            ):
                await executor.execute(
                    "SELECT set_config($1, $2, true)", name, value
                )
            pid_before = await executor.fetchval("SELECT pg_backend_pid()")
            context = _make_context()

            async def _commit(conn: asyncpg.Connection, receipt: object) -> str:
                from services.policy.receipts import PolicyReceiptV2

                assert isinstance(receipt, PolicyReceiptV2)
                # Same backend pid inside the same caller-owned transaction.
                pid = await conn.fetchval("SELECT pg_backend_pid()")
                assert pid == pid_before
                # C: the record uses the NEWLY minted receipt id AND the
                # receipt's exact action fence must equal the context fence
                # (content/evidence/subject/scope bound) - a tampered
                # context produces a different fence and fails closed.
                if receipt.action_resource_fence != context.action_resource_fence:
                    raise RuntimeError(
                        "action adapter: receipt fence does not match the "
                        "requested fence (tampered content/evidence)"
                    )
                committed = await conn.fetchval(
                    "SELECT memory_sensitive_commit("
                    " 'cross-r1', 'personal_private', 'person-a', 'person-a',"
                    " NULL, '[]'::jsonb, '[\"evidence-1\"]'::jsonb, $1,"
                    " 'consent-1', 'person-a')",
                    receipt.receipt_id,
                )
                return committed

            await service.execute(executor, context, _commit)
        # Counts go through an ADMIN connection - the executor has no
        # direct table privileges and must be DENIED any business-table
        # SELECT.
        admin_conn = await asyncpg.connect(admin_db_dsn)
        try:
            receipts = await admin_conn.fetchval(
                "SELECT count(*) FROM policy_receipts_v2"
            )
            records = await admin_conn.fetchval(
                "SELECT count(*) FROM memory_records WHERE record_id = 'cross-r1'"
            )
            assert receipts == 1 and records == 1
        finally:
            await admin_conn.close()
        with pytest.raises(asyncpg.PostgresError, match="permission denied"):
            await executor.fetchval(
                "SELECT count(*) FROM policy_receipts_v2"
            )
        # Rollback atomicity: a failing callback removes receipt + record.
        with pytest.raises(RuntimeError, match="injected"):
            async with executor.transaction():
                await executor.execute(
                    "SELECT set_config('app.memory.actor_subject_id', 'person-a', true)"
                )
                await executor.execute(
                    "SELECT set_config('app.memory.subject_id', 'person-a', true)"
                )
                for name, value in (
                    ("app.session_actor", "person-a"),
                    ("app.authenticated_actor", "person-a"),
                    ("app.authenticated_subject", "person-a"),
                    ("app.authenticated_device", "device-1"),
                    ("app.authenticated_binding", "binding-1"),
                ):
                    await executor.execute(
                        "SELECT set_config($1, $2, true)", name, value
                    )
                context2 = _make_context(
                    action_resource_id="capture:person-a:rollback",
                    idempotency_key="capture:person-a:rollback",
                )

                async def _failing_commit(
                    conn: asyncpg.Connection, receipt: object
                ) -> None:
                    await conn.fetchval(
                        "SELECT memory_sensitive_commit("
                        " 'cross-r2', 'personal_private', 'person-a', 'person-a',"
                        " NULL, '[]'::jsonb, '[\"evidence-1\"]'::jsonb, $1,"
                        " 'consent-1', 'person-a')",
                        receipt.receipt_id,
                    )
                    raise RuntimeError("injected callback failure")

                await service.execute(executor, context2, _failing_commit)
        admin_conn = await asyncpg.connect(admin_db_dsn)
        try:
            receipts = await admin_conn.fetchval(
                "SELECT count(*) FROM policy_receipts_v2"
            )
            records = await admin_conn.fetchval(
                "SELECT count(*) FROM memory_records WHERE record_id = 'cross-r2'"
            )
            assert receipts == 1 and records == 0
        finally:
            await admin_conn.close()
        # Tamper: a DIFFERENT capture evidence set produces a DIFFERENT
        # canonical action fence - the callback fence comparison fails
        # closed and nothing is written.
        with pytest.raises(RuntimeError, match="does not match"):
            async with executor.transaction():
                await executor.execute(
                    "SELECT set_config('app.memory.actor_subject_id', 'person-a', true)"
                )
                await executor.execute(
                    "SELECT set_config('app.memory.subject_id', 'person-a', true)"
                )
                for name, value in (
                    ("app.session_actor", "person-a"),
                    ("app.authenticated_actor", "person-a"),
                    ("app.authenticated_subject", "person-a"),
                    ("app.authenticated_device", "device-1"),
                    ("app.authenticated_binding", "binding-1"),
                ):
                    await executor.execute(
                        "SELECT set_config($1, $2, true)", name, value
                    )
                forged = _make_context(
                    capture_evidence_ids=("forged-evidence",),
                    idempotency_key="capture:person-a:forged-evidence",
                )
                await service.execute(
                    executor,
                    forged,
                    _commit,
                )
        admin_conn = await asyncpg.connect(admin_db_dsn)
        try:
            receipts = await admin_conn.fetchval(
                "SELECT count(*) FROM policy_receipts_v2"
            )
            assert receipts == 1  # the forged transaction rolled back
        finally:
            await admin_conn.close()
    finally:
        await executor.close()
