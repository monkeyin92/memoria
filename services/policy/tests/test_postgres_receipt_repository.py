"""PostgreSQL append-only policy receipt repository (live contract test).

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set (same convention as the
identity / guardian / notification PostgreSQL contract tests). A skip is NOT a
pass: the live roundtrip must be executed against a real PostgreSQL before the
repository is considered verified.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from packages.contracts.generated.python import multi_subject_contracts as generated
from services.policy.action_fence import build_action_resource_fence
from services.policy.decision_service import DecisionService
from services.policy.engine import PolicyEngine
from services.policy.postgres_receipt_repository import (
    POLICY_RECEIPTS_V2_SCHEMA_SQL,
    PostgresPolicyReceiptRepository,
    _int_list,
    _obligations_from_json,
    _obligations_to_json,
    _str_list,
    lock_policy_receipt,
)
from services.policy.production_wiring import (
    PostgresPolicySettings,
    build_postgres_decision_lifecycle,
)
from services.policy.receipts import PolicyReceiptConflictError, PolicyReceiptV2
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_consent_snapshot_ref,
    make_context,
)

APP_ROLE = "memoria_policy"
AUDIT_ROLE = "memoria_policy_audit"
RUNTIME_ROLES = (
    "memoria_policy",
    "memoria_policy_api",
    "memoria_policy_projector",
    "memoria_policy_worker",
    "memoria_policy_audit",
    "memoria_policy_maintenance",
)


class _ConcurrentEmptyReadRepository:
    """Force two PostgreSQL tasks to observe the same initial absence."""

    def __init__(
        self,
        delegate: PostgresPolicyReceiptRepository,
        *,
        first_created_at: datetime | None = None,
    ) -> None:
        self._delegate = delegate
        self._initial_gets = 0
        self._both_read_empty = asyncio.Event()
        self._first_created_at = first_created_at
        self._first_inserted = asyncio.Event()

    async def get(self, receipt_id: str) -> PolicyReceiptV2 | None:
        current = await self._delegate.get(receipt_id)
        if current is not None or self._initial_gets >= 2:
            return current
        self._initial_gets += 1
        if self._initial_gets == 2:
            self._both_read_empty.set()
        await asyncio.wait_for(self._both_read_empty.wait(), timeout=5)
        return None

    async def get_scoped(
        self,
        receipt_id: str,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> PolicyReceiptV2 | None:
        current = await self._delegate.get_scoped(
            receipt_id,
            actor_id=actor_id,
            subject_id=subject_id,
        )
        if current is not None or self._initial_gets >= 2:
            return current
        self._initial_gets += 1
        if self._initial_gets == 2:
            self._both_read_empty.set()
        await asyncio.wait_for(self._both_read_empty.wait(), timeout=5)
        return None

    async def insert(self, receipt: PolicyReceiptV2) -> None:
        if (
            self._first_created_at is not None
            and receipt.created_at != self._first_created_at
        ):
            await asyncio.wait_for(self._first_inserted.wait(), timeout=5)
        try:
            await self._delegate.insert(receipt)
        finally:
            if receipt.created_at == self._first_created_at:
                self._first_inserted.set()

    async def insert_scoped(
        self,
        receipt: PolicyReceiptV2,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> None:
        if (
            self._first_created_at is not None
            and receipt.created_at != self._first_created_at
        ):
            await asyncio.wait_for(self._first_inserted.wait(), timeout=5)
        try:
            await self._delegate.insert_scoped(
                receipt,
                actor_id=actor_id,
                subject_id=subject_id,
            )
        finally:
            if receipt.created_at == self._first_created_at:
                self._first_inserted.set()


@pytest.mark.parametrize("bad", [[True], [0], [-1], [1.5], ["1"]])
def test_repository_revision_decode_rejects_invalid_integer_array(
    bad: list[object],
) -> None:
    with pytest.raises(ValueError, match="revisions"):
        _int_list(bad, "revisions", minimum=1)


@pytest.mark.parametrize("bad", [[""], [" "], ["x" * 129], [1], [None]])
def test_repository_id_array_decode_rejects_invalid_strings(
    bad: list[object],
) -> None:
    with pytest.raises(ValueError, match="snapshot_ids"):
        _str_list(bad, "snapshot_ids", maximum=128)


@pytest.mark.parametrize(
    "bad",
    [
        [{"code": "NO_MODEL_TRAINING"}],
        [{"code": "NO_MODEL_TRAINING", "params": {}, "unknown": True}],
        [{"code": "EXECUTE_PROMPT", "params": {}}],
        [
            {
                "code": "RETENTION_TTL",
                "params": {"retention_ttl_seconds": True},
            }
        ],
        [{"code": "RETENTION_TTL", "params": {"prompt": "run"}}],
    ],
)
def test_repository_obligation_decode_rejects_dirty_json(
    bad: list[dict[str, object]],
) -> None:
    with pytest.raises(ValueError):
        _obligations_from_json(bad)


def test_repository_obligation_json_roundtrip_is_strict_and_lossless() -> None:
    obligations = (
        generated.PolicyObligationSpec(
            code=generated.PolicyObligation.POLICY_OBLIGATION_RETENTION_TTL,
            params=generated.ObligationParams(
                retention_ttl_seconds=86400,
                max_session_seconds=None,
                quiet_hours=("21:30", "06:30"),
                extras=(("scope", "private"),),
            ),
        ),
    )

    assert _obligations_from_json(_obligations_to_json(obligations)) == obligations


@pytest.mark.asyncio
async def test_postgres_repository_rejects_unscoped_legacy_calls() -> None:
    context = make_context(evaluated_at=datetime(2026, 8, 9, 8, 0, tzinfo=UTC))
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-unscoped")
    receipt = engine.receipt_for(context, engine.decide(context))
    repository = PostgresPolicyReceiptRepository(
        dsn="postgresql://memoria_policy_api:secret@localhost/memoria"
    )

    with pytest.raises(ValueError, match="authenticated actor and subject"):
        await repository.insert(receipt)
    with pytest.raises(ValueError, match="authenticated actor and subject"):
        await repository.get(receipt.receipt_id)


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


@pytest.fixture
async def pg_env() -> object:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    db_name = f"memoria_policy_{uuid.uuid4().hex[:10]}"
    app_password = uuid.uuid4().hex
    audit_password = uuid.uuid4().hex
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f"CREATE DATABASE {db_name}")
    finally:
        await admin.close()
    admin_db = _dsn_with(dsn, database=db_name)
    admin = await asyncpg.connect(admin_db)
    try:
        await admin.execute(POLICY_RECEIPTS_V2_SCHEMA_SQL)
        await admin.execute(f"ALTER ROLE {APP_ROLE} PASSWORD '{app_password}'")
        await admin.execute(f"ALTER ROLE {AUDIT_ROLE} PASSWORD '{audit_password}'")
    finally:
        await admin.close()
    app_dsn = _dsn_with(
        admin_db,
        database=db_name,
        user=APP_ROLE,
        password=app_password,
    )
    audit_dsn = _dsn_with(
        admin_db,
        database=db_name,
        user=AUDIT_ROLE,
        password=audit_password,
    )
    try:
        yield app_dsn, audit_dsn, admin_db
    finally:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name}")
        finally:
            await admin.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_receipt_repository_roundtrip(pg_env: object) -> None:
    app_dsn, _audit_dsn, _admin_db = pg_env  # type: ignore[misc]

    repository = PostgresPolicyReceiptRepository(dsn=app_dsn)
    now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    action_fence = build_action_resource_fence(
        capability="voice_clone_use",
        purpose="user_request",
        action_resource_id="voice-clone-1",
        action_revision=1,
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        issued_at=now,
        valid_until=now + timedelta(minutes=5),
    )
    receipt = generated.PolicyReceiptV2.model_validate(
        {
            "receipt_id": "receipt-pg-1",
            "actor_id": "person-adult",
            "subject_id": "person-adult",
            "resource_owner_id": "person-adult",
            "device_id": "device-1",
            "capability": "voice_clone_use",
            "purpose": "user_request",
            "effect": "allow_with_obligations",
            "reason_code": "adult_subject_authorized",
            "obligations": [
                {
                    "code": "REQUIRE_STEP_UP_AUTH",
                    "params": {
                        "max_session_seconds": 1800,
                        "retention_ttl_seconds": None,
                        "quiet_hours": ["21:30", "06:30"],
                        "extras": [],
                    },
                },
                {
                    "code": "WRITE_POLICY_RECEIPT",
                    "params": {
                        "max_session_seconds": None,
                        "retention_ttl_seconds": None,
                        "quiet_hours": None,
                        "extras": [],
                    },
                },
            ],
            "policy_version": "multi-subject-v2",
            "context_hash": "d" * 64,
            "action_resource_fence": action_fence.model_dump(mode="json"),
            "action_fence_hash": action_fence.canonical_hash,
            "consent_snapshot_ids": ["snap-1"],
            "consent_snapshot_revisions": [1],
            "relationship_snapshot_ids": [],
            "relationship_snapshot_revisions": [],
            "binding_id": "binding-1",
            "binding_version": 1,
            "binding_canonical_hash": "c" * 64,
            "session_id": "session-1",
            "session_epoch": 1,
            "runtime_profile_id": "profile-1",
            "subject_revision": 0,
            "device_trust": "trusted",
            "data_classification": "private",
            "safety_state": "normal",
            "jurisdiction": "CN",
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(minutes=5)).isoformat().replace(
                "+00:00", "Z"
            ),
            "exact_fence": True,
        }
    )

    await repository.insert_scoped(
        receipt,
        actor_id="person-adult",
        subject_id="person-adult",
    )
    loaded = await repository.get_scoped(
        "receipt-pg-1",
        actor_id="person-adult",
        subject_id="person-adult",
    )

    assert loaded is not None
    assert isinstance(loaded, generated.PolicyReceiptV2)
    assert loaded == receipt
    assert [o.code for o in loaded.obligations] == [
        "REQUIRE_STEP_UP_AUTH",
        "WRITE_POLICY_RECEIPT",
    ]
    assert loaded.obligations[0].params.max_session_seconds == 1800
    assert loaded.obligations[0].params.quiet_hours == ("21:30", "06:30")
    assert loaded.consent_snapshot_ids == ("snap-1",)
    assert loaded.consent_snapshot_revisions == (1,)


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_receipt_repository_binding_only_exact_replay_and_conflict(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, _admin_db = pg_env  # type: ignore[misc]

    repository = PostgresPolicyReceiptRepository(dsn=app_dsn)
    now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    context = make_context(
        binding_evidence=make_binding(now=now),
        evaluated_at=now,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-pg-2")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    await repository.insert_scoped(
        receipt,
        actor_id=context.actor_id,
        subject_id=context.subject_id,
    )
    await repository.insert_scoped(
        receipt,
        actor_id=context.actor_id,
        subject_id=context.subject_id,
    )  # exact replay is idempotent
    assert await repository.get_scoped(
        receipt.receipt_id,
        actor_id=context.actor_id,
        subject_id=context.subject_id,
    ) == receipt

    with pytest.raises(PolicyReceiptConflictError, match="immutable"):
        changed_payload = receipt.model_dump(mode="json")
        changed_payload["reason_code"] = "changed"
        await repository.insert_scoped(
            generated.PolicyReceiptV2.model_validate(changed_payload)
            ,
            actor_id=context.actor_id,
            subject_id=context.subject_id,
        )


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_decision_service_retry_revocation_and_runtime_conflict(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, admin_db = pg_env  # type: ignore[misc]
    repository = PostgresPolicyReceiptRepository(dsn=app_dsn)
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)

    replay_consent = make_consent(
        consent_id="consent-pg-replay",
        snapshot_id="snapshot-pg-replay",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=now,
    )
    replay_context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        idempotency_key="pg-replay-1",
        consent_evidence=(replay_consent,),
        binding_evidence=make_binding(now=now),
        evaluated_at=now,
    )
    first = await service.decide_and_persist(replay_context)
    replay = await service.decide_and_persist(
        replace(replay_context, evaluated_at=now + timedelta(seconds=1))
    )
    assert replay == first

    revoke_consent = make_consent(
        consent_id="consent-pg-revoke",
        snapshot_id="snapshot-pg-revoke",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=now,
    )
    revoke_context = replace(
        replay_context,
        idempotency_key="pg-revoke-1",
        consent_evidence=(revoke_consent,),
        consent_snapshot_evidence=(
            make_consent_snapshot_ref(
                snapshot_id="current-pg-revoke-1",
                revision=1,
                canonical_hash="1" * 64,
                consents=(revoke_consent,),
                now=now,
            ),
        ),
    )
    allowed = await service.decide_and_persist(revoke_context)
    revoked_consent = replace(revoke_consent, status="revoked")
    revoked_context = replace(
        revoke_context,
        evaluated_at=now + timedelta(seconds=1),
        consent_evidence=(revoked_consent,),
        consent_snapshot_evidence=(
            make_consent_snapshot_ref(
                snapshot_id="current-pg-revoke-2",
                revision=2,
                canonical_hash="2" * 64,
                consents=(revoked_consent,),
                now=now + timedelta(seconds=1),
            ),
        ),
    )
    denied = await service.decide_and_persist(revoked_context)
    replayed_deny = await service.decide_and_persist(
        replace(revoked_context, evaluated_at=now + timedelta(seconds=2))
    )
    assert allowed.effect == "allow_with_obligations"
    assert denied.effect == "deny"
    assert replayed_deny == denied
    assert denied.receipt_id != allowed.receipt_id
    assert "-v1-" in denied.receipt_id
    revoked_v2_consent = replace(
        revoke_consent, status="revoked", canonical_hash="d" * 64
    )
    revoked_v2_context = replace(
        revoked_context,
        evaluated_at=now + timedelta(seconds=3),
        consent_evidence=(revoked_v2_consent,),
        consent_snapshot_evidence=(
            make_consent_snapshot_ref(
                snapshot_id="current-pg-revoke-3",
                revision=3,
                canonical_hash="3" * 64,
                consents=(revoked_v2_consent,),
                now=now + timedelta(seconds=3),
            ),
        ),
    )
    denied_v2 = await service.decide_and_persist(revoked_v2_context)
    replayed_v2 = await service.decide_and_persist(
        replace(revoked_v2_context, evaluated_at=now + timedelta(seconds=4))
    )
    assert "-v2-" in denied_v2.receipt_id
    assert replayed_v2 == denied_v2

    conflict_consent = make_consent(
        consent_id="consent-pg-conflict",
        snapshot_id="snapshot-pg-conflict",
        capability="voice_clone_use",
        purpose="voice_clone",
        now=now,
    )
    conflict_context = replace(
        replay_context,
        idempotency_key="pg-conflict-1",
        consent_evidence=(conflict_consent,),
        consent_snapshot_evidence=(
            make_consent_snapshot_ref(
                snapshot_id="current-pg-conflict-1",
                revision=1,
                canonical_hash="4" * 64,
                consents=(conflict_consent,),
                now=now,
            ),
        ),
    )
    await service.decide_and_persist(conflict_context)
    with pytest.raises(PolicyReceiptConflictError, match="immutable"):
        await service.decide_and_persist(
            replace(
                conflict_context,
                evaluated_at=now + timedelta(seconds=1),
                device_trust="untrusted",
            )
        )

    admin = await asyncpg.connect(admin_db)
    try:
        assert await admin.fetchval("SELECT count(*) FROM policy_receipts_v2") == 5
    finally:
        await admin.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_concurrent_same_key_replays_single_committed_row(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, admin_db = pg_env  # type: ignore[misc]
    now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    repository = _ConcurrentEmptyReadRepository(
        PostgresPolicyReceiptRepository(dsn=app_dsn),
        first_created_at=now,
    )
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        capability="chat",
        idempotency_key="pg-concurrent-replay-1",
        evaluated_at=now,
    )

    first, second = await asyncio.gather(
        service.decide_and_persist(context),
        service.decide_and_persist(
            replace(context, evaluated_at=now + timedelta(seconds=1))
        ),
    )

    assert first == second
    admin = await asyncpg.connect(admin_db)
    try:
        assert await admin.fetchval("SELECT count(*) FROM policy_receipts_v2") == 1
    finally:
        await admin.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_concurrent_authoritative_drift_has_one_fail_closed(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, admin_db = pg_env  # type: ignore[misc]
    repository = _ConcurrentEmptyReadRepository(
        PostgresPolicyReceiptRepository(dsn=app_dsn)
    )
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    context = make_context(
        capability="chat",
        data_classification="public",
        idempotency_key="pg-concurrent-content-conflict-1",
        evaluated_at=now,
    )

    results = await asyncio.gather(
        service.decide_and_persist(context),
        service.decide_and_persist(
            replace(
                context,
                evaluated_at=now + timedelta(seconds=1),
                data_classification="private",
            )
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, PolicyReceiptConflictError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    admin = await asyncpg.connect(admin_db)
    try:
        assert await admin.fetchval("SELECT count(*) FROM policy_receipts_v2") == 1
    finally:
        await admin.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_expired_replay_fails_closed_without_new_row(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, admin_db = pg_env  # type: ignore[misc]
    service = DecisionService(
        engine=PolicyEngine(),
        repository=PostgresPolicyReceiptRepository(dsn=app_dsn),
    )
    now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    context = make_context(
        capability="chat",
        idempotency_key="pg-expired-replay-1",
        evaluated_at=now,
    )
    first = await service.decide_and_persist(context)
    assert first.expires_at is not None

    with pytest.raises(PolicyReceiptConflictError, match="expired"):
        await service.decide_and_persist(
            replace(
                context,
                evaluated_at=first.expires_at + timedelta(seconds=1),
            )
        )

    admin = await asyncpg.connect(admin_db)
    try:
        assert await admin.fetchval("SELECT count(*) FROM policy_receipts_v2") == 1
    finally:
        await admin.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_roles_enforce_append_only_permission_matrix(
    pg_env: object,
) -> None:
    app_dsn, audit_dsn, admin_db = pg_env  # type: ignore[misc]
    admin = await asyncpg.connect(admin_db)
    try:
        for role in RUNTIME_ROLES:
            attributes = await admin.fetchrow(
                "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles "
                "WHERE rolname = $1",
                role,
            )
            assert attributes is not None
            assert attributes["rolsuper"] is False
            assert attributes["rolbypassrls"] is False
            assert attributes["rolcanlogin"] is True
        assert await admin.fetchval(
            "SELECT relforcerowsecurity FROM pg_class WHERE relname = $1",
            "policy_receipts_v2",
        ) is True
        expected = {
            "memoria_policy": {"SELECT", "INSERT"},
            "memoria_policy_api": {"SELECT", "INSERT"},
            "memoria_policy_projector": {"SELECT", "INSERT"},
            "memoria_policy_worker": {"SELECT"},
            "memoria_policy_audit": {"SELECT"},
            "memoria_policy_maintenance": {"SELECT", "INSERT"},
        }
        for role, privileges in expected.items():
            for privilege in ("SELECT", "INSERT"):
                assert await admin.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    role,
                    "policy_receipts_v2",
                    privilege,
                ) is (privilege in privileges)
        for privilege in ("UPDATE", "DELETE", "TRUNCATE"):
            for role in RUNTIME_ROLES:
                assert await admin.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)",
                    role,
                    "policy_receipts_v2",
                    privilege,
                ) is False
    finally:
        await admin.close()

    app = await asyncpg.connect(app_dsn)
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute(
                "UPDATE policy_receipts_v2 SET reason_code = 'changed'"
            )
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("DELETE FROM policy_receipts_v2")
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await app.execute("TRUNCATE policy_receipts_v2")
    finally:
        await app.close()

    audit = await asyncpg.connect(audit_dsn)
    try:
        assert await audit.fetchval("SELECT count(*) FROM policy_receipts_v2") == 0
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await audit.execute(
                "INSERT INTO policy_receipts_v2 (receipt_id) VALUES ('forbidden')"
            )
    finally:
        await audit.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_api_receipts_are_actor_subject_scoped_and_global_audit_is_explicit(
    pg_env: object,
) -> None:
    app_dsn, audit_dsn, _admin_db = pg_env  # type: ignore[misc]
    now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)

    context_a = make_context(
        actor_id="actor-a",
        subject_id="subject-a",
        resource_owner_id="subject-a",
        idempotency_key=None,
        evaluated_at=now,
    )
    context_b = make_context(
        actor_id="actor-b",
        subject_id="subject-b",
        resource_owner_id="subject-b",
        idempotency_key=None,
        evaluated_at=now,
    )
    receipt_a = PolicyEngine(
        receipt_id_factory=lambda: "receipt-scope-a"
    ).receipt_for(context_a, PolicyEngine().decide(context_a))
    receipt_b = PolicyEngine(
        receipt_id_factory=lambda: "receipt-scope-b"
    ).receipt_for(context_b, PolicyEngine().decide(context_b))
    repository = PostgresPolicyReceiptRepository(dsn=app_dsn)

    await repository.insert_scoped(
        receipt_a,
        actor_id="actor-a",
        subject_id="subject-a",
    )
    await repository.insert_scoped(
        receipt_b,
        actor_id="actor-b",
        subject_id="subject-b",
    )

    assert await repository.get_scoped(
        receipt_a.receipt_id,
        actor_id="actor-a",
        subject_id="subject-a",
    ) == receipt_a
    assert await repository.get_scoped(
        receipt_b.receipt_id,
        actor_id="actor-a",
        subject_id="subject-a",
    ) is None

    audit = await asyncpg.connect(audit_dsn)
    try:
        assert await audit.fetchval(
            "SELECT count(*) FROM policy_receipts_v2"
        ) == 2
    finally:
        await audit.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_api_persists_unknown_subject_deny_with_actor_scope(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, _admin_db = pg_env  # type: ignore[misc]
    now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
    context = make_context(
        actor_id="actor-unknown-safe",
        subject_id=None,
        resource_owner_id=None,
        capability="tutor",
        current_session_mode="unknown_safe",
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        speaker_confidence=None,
        idempotency_key=None,
        evaluated_at=now,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-null-subject")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)
    repository = PostgresPolicyReceiptRepository(dsn=app_dsn)

    assert decision.effect == "deny"
    assert receipt.subject_id is None
    await repository.insert_scoped(
        receipt,
        actor_id="actor-unknown-safe",
        subject_id=None,
    )

    assert await repository.get_scoped(
        receipt.receipt_id,
        actor_id="actor-unknown-safe",
        subject_id=None,
    ) == receipt
    assert await repository.get_scoped(
        receipt.receipt_id,
        actor_id="actor-other",
        subject_id=None,
    ) is None


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_api_rejects_forged_receipt_actor_subject_and_wrong_context_lock(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, _admin_db = pg_env  # type: ignore[misc]
    now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
    context_a = make_context(
        actor_id="actor-a",
        subject_id="subject-a",
        resource_owner_id="subject-a",
        idempotency_key=None,
        evaluated_at=now,
    )
    context_b = make_context(
        actor_id="actor-b",
        subject_id="subject-b",
        resource_owner_id="subject-b",
        idempotency_key=None,
        evaluated_at=now,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-forged")
    receipt_a = engine.receipt_for(context_a, engine.decide(context_a))
    receipt_b = PolicyEngine(
        receipt_id_factory=lambda: "receipt-lock-b"
    ).receipt_for(context_b, PolicyEngine().decide(context_b))
    repository = PostgresPolicyReceiptRepository(dsn=app_dsn)
    await repository.insert_scoped(
        receipt_b,
        actor_id="actor-b",
        subject_id="subject-b",
    )

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await repository.insert_scoped(
            receipt_a,
            actor_id="actor-b",
            subject_id="subject-b",
        )

    connection = await asyncpg.connect(app_dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.authenticated_actor', $1, true)",
                "actor-a",
            )
            await connection.execute(
                "SELECT set_config('app.authenticated_subject', $1, true)",
                "subject-a",
            )
            with pytest.raises(asyncpg.PostgresError) as error:
                await lock_policy_receipt(connection, receipt_b.receipt_id)
            assert error.value.sqlstate == "SR403"
    finally:
        await connection.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_jsonb_constraints_reject_unknown_keys_and_bad_types(
    pg_env: object,
) -> None:
    app_dsn, _audit_dsn, admin_db = pg_env  # type: ignore[misc]
    now = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        consent_evidence=(
            make_consent(
                capability="voice_clone_use", purpose="voice_clone", now=now
            ),
        ),
        binding_evidence=make_binding(now=now),
        evaluated_at=now,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-jsonb-source")
    receipt = engine.receipt_for(context, engine.decide(context))
    await PostgresPolicyReceiptRepository(dsn=app_dsn).insert_scoped(
        receipt,
        actor_id=context.actor_id,
        subject_id=context.subject_id,
    )

    admin = await asyncpg.connect(admin_db)
    try:
        mutations = (
            (
                "receipt-jsonb-obligation",
                "jsonb_build_object('obligations', "
                "jsonb_build_array((r.obligations -> 0) || '{\"unknown\":true}'::jsonb))",
            ),
            (
                "receipt-jsonb-action",
                "jsonb_build_object('action_resource_fence', "
                "r.action_resource_fence || '{\"unknown\":true}'::jsonb)",
            ),
            (
                "receipt-jsonb-revision",
                "jsonb_build_object('consent_snapshot_revisions', '[true]'::jsonb)",
            ),
        )
        for receipt_id, mutation in mutations:
            with pytest.raises(asyncpg.CheckViolationError):
                await admin.execute(
                    "INSERT INTO policy_receipts_v2 "
                    "SELECT (jsonb_populate_record(NULL::policy_receipts_v2, "
                    "to_jsonb(r) || jsonb_build_object('receipt_id', $1::text) || "
                    f"{mutation})).* "
                    "FROM policy_receipts_v2 AS r "
                    "WHERE r.receipt_id = 'receipt-jsonb-source'",
                    receipt_id,
                )
    finally:
        await admin.close()


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL policy receipt contract",
)
async def test_postgres_decision_lifecycle_uses_api_role_and_persists_before_return(
    pg_env: object,
) -> None:
    _app_dsn, _audit_dsn, admin_db = pg_env  # type: ignore[misc]
    password = "memoria_policy_api_test"
    admin = await asyncpg.connect(admin_db)
    try:
        await admin.execute(
            f"ALTER ROLE memoria_policy_api PASSWORD '{password}'"
        )
    finally:
        await admin.close()
    api_dsn = _dsn_with(
        admin_db,
        database=urlsplit(admin_db).path.removeprefix("/"),
        user="memoria_policy_api",
        password=password,
    )
    lifecycle = build_postgres_decision_lifecycle(
        PostgresPolicySettings(dsn=api_dsn)
    )

    decision = await lifecycle.decide_and_persist(
        make_context(
            evaluated_at=datetime(2026, 8, 10, 16, 0, tzinfo=UTC),
            idempotency_key="postgres-lifecycle-1",
        )
    )

    assert isinstance(decision, generated.PolicyDecision)
    admin = await asyncpg.connect(admin_db)
    try:
        assert await admin.fetchval(
            "SELECT count(*) FROM policy_receipts_v2 WHERE receipt_id = $1",
            decision.receipt_id,
        ) == 1
    finally:
        await admin.close()
