"""FORCE-RLS PostgreSQL contract for the memory scope store seam (PR-17).

The admin DSN is used ONLY to create the database/roles and to bootstrap the
schema; business operations run as TWO real application roles
(``memoria_memory_api`` and ``memoria_memory_worker``, both LOGIN,
NOSUPERUSER, NOBYPASSRLS, non-table-owner).  The tests assert the runtime
role properties and prove: no context -> zero rows / rejected writes, valid
context -> read/write works, cross-subject / cross-family context -> zero
rows, and the worker-only outbox poll/update path is pinned to the worker
role.

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set (same convention as the
identity PostgreSQL contract test).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.memory_scope.domain import (
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    SharedMemoryProposal,
)
from services.memory_scope.postgres_store import PostgresMemoryStore
from services.memory_scope.tests.test_adapter_contract import (
    scenario_approvals_complete_freeze,
    scenario_freeze_lost_cas_reports_real_state,
    scenario_pending_freeze,
    scenario_pending_not_directly_promotable,
)

API_ROLE = "memoria_memory_api"
WORKER_ROLE = "memoria_memory_worker"
ACTION_EXECUTOR_ROLE = "memoria_action_executor"
OWNER_ROLE = "memoria_memory_owner"
MEMORY_TABLES = (
    "memory_records",
    "memory_status_events",
    "memory_shared_proposals",
    "memory_shared_votes",
    "memory_outbox",
    "memory_audit_events",
)


def _dsn_with(dsn: str, *, database: str, user: str | None = None, password: str | None = None) -> str:
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
async def pg_env() -> AsyncIterator[tuple[str, str, str, str]]:
    """Provision a fresh database + BOTH app roles and yield (api_dsn,
    worker_dsn, admin_db_dsn, role_password); drops the database
    afterwards."""
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    db_name = f"memory_scope_pg_{uuid.uuid4().hex[:8]}"
    # Deterministic shared password (see the notification fixture rationale):
    # the app roles are cluster-global, so concurrent runs must not ALTER
    # ROLE over each other with random passwords.
    password = "memoria_local_test_password"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
        for role in (API_ROLE, WORKER_ROLE, ACTION_EXECUTOR_ROLE):
            if not await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", role
            ):
                await admin.execute(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS"
                )
    finally:
        await admin.close()
    admin_db = _dsn_with(dsn, database=db_name)
    api = _dsn_with(admin_db, database=db_name, user=API_ROLE, password=password)
    worker = _dsn_with(admin_db, database=db_name, user=WORKER_ROLE, password=password)
    action_executor = _dsn_with(
        admin_db,
        database=db_name,
        user=ACTION_EXECUTOR_ROLE,
        password=password,
    )
    try:
        yield api, worker, action_executor, admin_db, password
    finally:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin.close()


async def _initialize_store(app_dsn: str, admin_db_dsn: str, password: str) -> PostgresMemoryStore:
    store = PostgresMemoryStore(app_dsn)
    await store.initialize(
        bootstrap_dsn=admin_db_dsn,
        app_role_password=password,
    )
    return store


async def _assert_app_role_properties(store: PostgresMemoryStore) -> None:
    async with store._require_pool().acquire() as connection:  # noqa: SLF001
        row = await connection.fetchrow(
            "SELECT current_user AS usr,"
            " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su,"
            " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
            " AS bypass"
        )
    assert row is not None
    assert row["usr"] == API_ROLE
    assert row["su"] is False
    assert row["bypass"] is False


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_postgres_record_roundtrip_and_withdrawal(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, worker_dsn, action_executor_dsn, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    worker_store = PostgresMemoryStore(worker_dsn).with_role("worker")
    await worker_store.initialize()
    try:
        await _assert_app_role_properties(store)
        async with worker_store._require_pool().acquire() as connection:  # noqa: SLF001
            row = await connection.fetchrow(
                "SELECT current_user AS usr,"
                " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su,"
                " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
                " AS bypass"
            )
            assert row is not None
            assert row["usr"] == WORKER_ROLE
            assert row["su"] is False
            assert row["bypass"] is False
        record_id = "pg-r1"
        now = datetime.now(UTC)
        await store.persist_record(
            MemoryRecord(
                record_id=record_id,
                scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                subject_id="person-a",
                resource_owner_id="person-a",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                created_by_actor_id="person-a",
                created_at=now,
            ),
            actor_family_space_id=None,
            status_events=(
                MemoryRecordStatusEvent(
                    event_id="pg-e1",
                    record_id=record_id,
                    status="confirmed",
                    reason_code="captured",
                    created_at=now,
                ),
            ),
        )
        fetched = await store.get_record(record_id, actor_subject_id="person-a")
        assert fetched is not None
        assert fetched.scope is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        assert fetched.status == "confirmed"

        # Cross-subject context: RLS hides the row entirely.
        assert await store.get_record(record_id, actor_subject_id="person-b") is None

        # Least privilege: the API role has NO UPDATE grant on records - an
        # in-place update is denied by ACL before any trigger runs.
        async with store._require_pool().acquire() as connection:  # noqa: SLF001
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.memory.actor_subject_id',"
                    " 'person-a', true)"
                )
                await connection.execute(
                    "SELECT set_config('app.memory.subject_id', 'person-a', true)"
                )
                with pytest.raises(asyncpg.PostgresError, match="permission denied"):
                    await connection.execute(
                        "UPDATE memory_records SET scope = 'family_shared'"
                        " WHERE record_id = $1",
                        record_id,
                    )
        # Append-only immutability is enforced at the database level: the
        # schema owner (migration role) with row_security off still hits the
        # BEFORE UPDATE trigger for any in-place update.
        owner = await asyncpg.connect(admin_db_dsn)
        try:
            await owner.execute("SET row_security = off")
            with pytest.raises(asyncpg.PostgresError, match="append-only"):
                await owner.execute(
                    "UPDATE memory_records SET scope = 'family_shared'"
                    " WHERE record_id = $1",
                    record_id,
                )
        finally:
            await owner.close()

        await store.persist_withdrawal(
            "pg-proposal",
            actor_subject_id="person-a",
            record_id=record_id,
            status_events=(
                MemoryRecordStatusEvent(
                    event_id="pg-e2",
                    record_id=record_id,
                    status="revoked",
                    reason_code="withdrawn",
                    created_at=now,
                ),
            ),
        )
        assert (
            await store.list_records_for_subject(
                "person-a",
                scopes=(MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,),
                actor_subject_id="person-a",
            )
            == ()
        )
        revoked = await store.list_records_for_subject(
            "person-a",
            scopes=(MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,),
            actor_subject_id="person-a",
            include_revoked=True,
        )
        assert len(revoked) == 1
        assert revoked[0].status == "revoked"

        # Worker-only outbox path: the API appends, the worker polls and
        # marks processed; the API adapter refuses worker commands.
        await store.append_outbox(
            MemoryOutboxEvent(
                outbox_id="pg-out-1",
                event_id="pg-out-1",
                topic="memory.record.captured",
                payload={"record_id": record_id},
                created_at=now,
            )
        )
        pending = await worker_store.list_pending_outbox(limit=10)
        assert any(event.event_id == "pg-out-1" for event in pending)
        await worker_store.mark_outbox_processed("pg-out-1")
        assert not any(
            event.event_id == "pg-out-1"
            for event in await worker_store.list_pending_outbox(limit=10)
        )
    finally:
        await store.close()
        await worker_store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_postgres_rls_fails_closed_and_isolates_contexts(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    """PR-17: no context -> zero rows / rejected writes; valid context ->
    read/write; cross-subject and cross-family contexts -> zero rows."""
    api_dsn, worker_dsn, action_executor_dsn, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    try:
        await _assert_app_role_properties(store)
        pool = store._require_pool()

        # No app context: reads see zero rows and writes are rejected.
        async with pool.acquire() as connection:
            async with connection.transaction():
                rows = await connection.fetch("SELECT * FROM memory_records")
                assert rows == []
                with pytest.raises(asyncpg.PostgresError, match="row-level security"):
                    await connection.execute(
                        "INSERT INTO memory_records (record_id, scope, subject_id,"
                        " resource_owner_id, policy_receipt_id, consent_snapshot_id,"
                        " created_by_actor_id, created_at)"
                        " VALUES ('rl1', 'personal_private', 'person-a', 'person-a',"
                        " 'r', 'c', 'person-a', now())"
                    )

        # Valid context: the store writes and reads work.
        now = datetime.now(UTC)
        await store.persist_record(
            MemoryRecord(
                record_id="pg-a",
                scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
                subject_id="person-a",
                resource_owner_id="person-a",
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-1",
                consent_snapshot_id="consent-1",
                created_by_actor_id="person-a",
                created_at=now,
            ),
            actor_family_space_id=None,
        )
        await store.persist_record(
            MemoryRecord(
                record_id="pg-fam",
                scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
                subject_id="person-a",
                resource_owner_id="person-a",
                family_space_id="family-1",
                co_subject_ids=("person-b",),
                source_evidence_ids=("evidence-1",),
                policy_receipt_id="receipt-2",
                consent_snapshot_id="consent-1",
                created_by_actor_id="person-a",
                created_at=now,
            ),
            actor_family_space_id="family-1",
        )

        # Cross-subject context reads nothing.
        assert await store.get_record("pg-a", actor_subject_id="person-b") is None

        # Family isolation: person-b is a co-subject but a different family
        # context must see nothing (RLS family predicate, P1).
        assert (
            await store.list_records_in_family(
                "family-1",
                "person-b",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-b",
                actor_family_space_id="family-2",
            )
            == ()
        )
        assert (
            await store.list_records_in_family(
                "family-1",
                "person-b",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-b",
                actor_family_space_id="family-1",
            )
            != ()
        )
        # Fifth review: grant-aware read - guardian context reads only the
        # granted owner + guardian_summary scope.
        guardian_view = await store.get_record(
            "pg-fam",
            actor_subject_id="guardian-1",
            actor_family_space_id="family-1",
            grant_owner_id="person-a",
            grant_scope="guardian_summary",
        )
        # pg-fam is family_shared, not guardian_summary: scope pinning
        # rejects it.
        assert guardian_view is None
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_postgres_proposal_freeze_is_atomic_and_terminal(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    """Main architecture review: freeze_proposal_atomically moves a pending
    proposal to frozen with ONE audit/outbox pair in a single transaction;
    a second call is an idempotent no-op."""
    api_dsn, worker_dsn, action_executor_dsn, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    try:
        now = datetime.now(UTC)
        proposal = SharedMemoryProposal(
            proposal_id="pg-freeze-1",
            family_space_id="family-1",
            proposer_subject_id="person-a",
            co_subject_ids=("person-b",),
            binding_version=1,
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_role="primary_subject",
            runtime_profile_id="profile-1",
            device_id="device-1",
            subject_revision=0,
            fence_context_hash="f" * 64,
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            proposal_policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            proposal_revision=1,
            capture_evidence_hash="c" * 64,
            consent_snapshot_revision=1,
            consent_snapshot_hash="b" * 64,
            membership_snapshot_id="membership:family-1:1",
            membership_snapshot_revision=1,
            membership_snapshot_hash="a" * 64,
            status="pending",
            created_at=now,
        )
        await store.persist_proposal(
            proposal, actor_family_space_id="family-1"
        )
        frozen = await store.freeze_proposal_atomically(
            "pg-freeze-1",
            actor_subject_id="person-a",
            family_space_id="family-1",
            reason="authorization revoked",
            audit=(
                MemoryAuditEvent(
                    event_id="audit:pg-freeze-1:frozen:authorization",
                    action="memory.shared.frozen",
                    actor_subject_id="person-a",
                    subject_id="person-a",
                    record_id=None,
                    proposal_id="pg-freeze-1",
                    payload={"reason": "authorization revoked"},
                    created_at=now,
                ),
            ),
            outbox=(
                MemoryOutboxEvent(
                    outbox_id="pg-freeze-1:frozen:authorization",
                    event_id="pg-freeze-1:frozen:authorization",
                    topic="memory.shared.frozen",
                    payload={"proposal_id": "pg-freeze-1"},
                    created_at=now,
                ),
            ),
            now=now,
        )
        assert frozen is True
        row = await store.get_proposal(
            "pg-freeze-1",
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert row is not None and row.status == "frozen"
        # Idempotent replay: no duplicate side effects.
        assert (
            await store.freeze_proposal_atomically(
                "pg-freeze-1",
                actor_subject_id="person-a",
                family_space_id="family-1",
                reason="again",
                audit=(),
                outbox=(),
                now=now,
            )
            is False
        )
        # No promoted record exists.
        records = await store.list_records_for_subject(
            "person-a",
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id="person-a",
        )
        assert records == ()
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_postgres_adapter_contract_lifecycle(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    """The SAME proposal-lifecycle scenarios as the in-memory/SQLite
    adapter contract run against real PostgreSQL: pending revoke freeze,
    approvals_complete revoke freeze, pending is never directly promotable
    and a lost freeze CAS reports the real terminal state."""
    api_dsn, worker_dsn, action_executor_dsn, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    try:
        await scenario_pending_freeze(store)
        await scenario_approvals_complete_freeze(store)
        await scenario_pending_not_directly_promotable(store)
        await scenario_freeze_lost_cas_reports_real_state(store)
    finally:
        await store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_postgres_sensitive_commit_function_and_rls_isolation(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    """Cross-domain same-connection supplement: the narrow SECURITY
    DEFINER ``memory_sensitive_commit`` is the ONLY write path for the
    sensitive executor - it validates the session-local actor/subject/
    family context (fail closed) and the API role still has NO direct
    table INSERT privilege (RLS/ACL isolation is preserved)."""
    api_dsn, worker_dsn, action_executor_dsn, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    executor_store = PostgresMemoryStore(action_executor_dsn).with_role(
        "action_executor"
    )
    await executor_store.initialize()
    try:
        admin = await asyncpg.connect(admin_db_dsn)
        try:
            owner = await admin.fetchrow(
                "SELECT rolcanlogin, rolsuper, rolbypassrls"
                " FROM pg_roles WHERE rolname = $1",
                OWNER_ROLE,
            )
            assert owner is not None
            assert owner["rolcanlogin"] is False
            assert owner["rolsuper"] is False
            assert owner["rolbypassrls"] is False

            table_owners = await admin.fetch(
                "SELECT c.relname, pg_get_userbyid(c.relowner) AS owner"
                " FROM pg_class AS c"
                " JOIN pg_namespace AS n ON n.oid = c.relnamespace"
                " WHERE n.nspname = 'public' AND c.relname = ANY($1::text[])",
                list(MEMORY_TABLES),
            )
            assert {row["relname"] for row in table_owners} == set(MEMORY_TABLES)
            assert {row["owner"] for row in table_owners} == {OWNER_ROLE}

            function_owners = await admin.fetch(
                "SELECT pg_get_userbyid(p.proowner) AS owner"
                " FROM pg_proc AS p"
                " JOIN pg_namespace AS n ON n.oid = p.pronamespace"
                " WHERE n.nspname = 'public'"
                " AND p.proname = 'memory_sensitive_commit'"
                " AND p.pronargs = 10",
            )
            assert len(function_owners) == 1
            assert function_owners[0]["owner"] == OWNER_ROLE
        finally:
            await admin.close()

        now = datetime.now(UTC)
        record = MemoryRecord(
            record_id="pg-sensitive-1",
            scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            subject_id="person-a",
            resource_owner_id="person-a",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            created_by_actor_id="person-a",
            created_at=now,
        )
        pool = executor_store._require_pool()  # noqa: SLF001
        async with pool.acquire() as connection:
            async with connection.transaction():
                pid_before = await connection.fetchval("SELECT pg_backend_pid()")
                await executor_store.sensitive_commit(
                    record, actor_family_space_id=None, connection=connection
                )
                pid_after = await connection.fetchval("SELECT pg_backend_pid()")
                # Same backend pid inside one caller-owned transaction.
                assert pid_before == pid_after
        fetched = await store.get_record(
            "pg-sensitive-1", actor_subject_id="person-a"
        )
        assert fetched is not None and fetched.record_id == "pg-sensitive-1"
        # Replay with the SAME policy receipt fails closed (one receipt ->
        # at most one record).
        replay = MemoryRecord(
            record_id="pg-sensitive-1-replay",
            scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            subject_id="person-a",
            resource_owner_id="person-a",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            created_by_actor_id="person-a",
            created_at=now,
        )
        async with pool.acquire() as connection:
            async with connection.transaction():
                with pytest.raises(asyncpg.PostgresError):
                    await executor_store.sensitive_commit(
                        replay, actor_family_space_id=None, connection=connection
                    )
        # Rollback atomicity: a failure inside the same transaction removes
        # the record again.
        rollback_record = replace(
            record,
            record_id="pg-sensitive-1-rollback",
            policy_receipt_id="receipt-2",
        )
        async with pool.acquire() as connection:
            async with connection.transaction():
                await executor_store.sensitive_commit(
                    rollback_record,
                    actor_family_space_id=None,
                    connection=connection,
                )
                with pytest.raises(asyncpg.PostgresError):
                    await connection.execute("SELECT 1/0")
        assert (
            await store.get_record(
                "pg-sensitive-1-rollback", actor_subject_id="person-a"
            )
            is None
        )
        # The action-executor role has NO direct table grants.
        async with pool.acquire() as connection:
            grants = await connection.fetch(
                "SELECT privilege_type FROM information_schema.role_table_grants"
                " WHERE grantee = $1 AND table_name = 'memory_records'",
                ACTION_EXECUTOR_ROLE,
            )
            assert grants == []
            async with connection.transaction():
                await executor_store._scope(  # noqa: SLF001
                    connection,
                    actor_subject_id="person-a",
                    subject_id="person-a",
                    record_id="pg-sensitive-direct-insert",
                )
                with pytest.raises(asyncpg.PostgresError, match="permission denied"):
                    await connection.execute(
                        "INSERT INTO memory_records ("
                        " record_id, scope, subject_id, resource_owner_id,"
                        " policy_receipt_id, consent_snapshot_id,"
                        " created_by_actor_id, created_at"
                        ") VALUES ("
                        " 'pg-sensitive-direct-insert', 'personal_private',"
                        " 'person-a', 'person-a', 'receipt-direct',"
                        " 'consent-1', 'person-a', now()"
                        ")"
                    )
        # GUC actor mismatch fails closed inside the function.
        forged = MemoryRecord(
            record_id="pg-sensitive-2",
            scope=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            subject_id="person-a",
            resource_owner_id="person-a",
            source_evidence_ids=("evidence-1",),
            policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            created_by_actor_id="forged-actor",
            created_at=now,
        )
        async with pool.acquire() as connection:
            async with connection.transaction():
                await store._scope(  # noqa: SLF001
                    connection,
                    actor_subject_id="person-a",
                    subject_id="person-a",
                )
                with pytest.raises(asyncpg.PostgresError, match="actor mismatch"):
                    await connection.fetchval(
                        "SELECT memory_sensitive_commit("
                        " $1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10)",
                        forged.record_id,
                        forged.scope.value,
                        forged.subject_id,
                        forged.resource_owner_id,
                        forged.family_space_id,
                        "[]",
                        "[]",
                        forged.policy_receipt_id,
                        forged.consent_snapshot_id,
                        forged.created_by_actor_id,
                    )
    finally:
        await store.close()
        await executor_store.close()
