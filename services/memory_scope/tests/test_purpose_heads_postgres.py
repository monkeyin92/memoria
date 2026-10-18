"""Real-PostgreSQL contract for ``memory_purpose_heads`` (B01).

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import asyncpg
import pytest
from services.memory_scope import postgres_store
from services.memory_scope.tests import test_postgres_store
from services.memory_scope.tests.test_postgres_store import _initialize_store

pg_env = test_postgres_store.pg_env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
        reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
    ),
]

_INSERT = (
    "INSERT INTO memory_purpose_heads(purpose_scope_id, subject_id, resource_owner_id,"
    " purpose, authority_scope_type, authority_scope_id, valid_until)"
    " VALUES ($1, 'child-1', 'parent-1', $2, $3, $4, $5)"
)


async def test_schema_reapplies_and_enforces_head_rules(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, _w, _a, admin_db_dsn, password = pg_env
    for _ in range(2):  # initialisation is idempotent
        store = await _initialize_store(api_dsn, admin_db_dsn, password)
        await store.close()
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        head = uuid.uuid4()
        await admin.execute(_INSERT, head, "memory_use", "binding", "b1", None)
        # a custody head must carry a finite term
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(
                _INSERT, uuid.uuid4(), "archive_custody", "custody_chain", "c1", None
            )
        await admin.execute(
            _INSERT,
            uuid.uuid4(),
            "archive_custody",
            "custody_chain",
            "c1",
            await admin.fetchval("SELECT now() + interval '30 days'"),
        )
        # one head per natural key
        with pytest.raises(asyncpg.UniqueViolationError):
            await admin.execute(_INSERT, uuid.uuid4(), "memory_use", "binding", "b1", None)
        # revisions never move backwards
        await admin.execute(
            "UPDATE memory_purpose_heads SET restriction_revision = 3 WHERE purpose_scope_id = $1",
            head,
        )
        with pytest.raises(asyncpg.RaiseError):
            await admin.execute(
                "UPDATE memory_purpose_heads SET restriction_revision = 2"
                " WHERE purpose_scope_id = $1",
                head,
            )
        # a stop is only lifted by a newer restriction revision
        await admin.execute(
            "UPDATE memory_purpose_heads SET state='stopped', stopped_at=now()"
            " WHERE purpose_scope_id = $1",
            head,
        )
        with pytest.raises(asyncpg.RaiseError):
            await admin.execute(
                "UPDATE memory_purpose_heads SET state='active', stopped_at=NULL"
                " WHERE purpose_scope_id = $1",
                head,
            )
    finally:
        await admin.close()


async def test_force_rls_hides_heads_without_context(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, _w, _a, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(_INSERT, uuid.uuid4(), "memory_use", "binding", "b9", None)
        flags = await admin.fetchrow(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class"
            " WHERE relname = 'memory_purpose_heads'"
        )
        assert flags is not None
        assert flags["relrowsecurity"] and flags["relforcerowsecurity"]
        app = await asyncpg.connect(api_dsn)
        try:
            assert await app.fetchval("SELECT count(*) FROM memory_purpose_heads") == 0
            async with app.transaction():
                await app.execute(
                    "SELECT set_config('app.memory.actor_subject_id', 'parent-1', true)"
                )
                assert await app.fetchval("SELECT count(*) FROM memory_purpose_heads") == 1
            async with app.transaction():
                await app.execute(
                    "SELECT set_config('app.memory.actor_subject_id', 'stranger', true)"
                )
                assert await app.fetchval("SELECT count(*) FROM memory_purpose_heads") == 0
        finally:
            await app.close()
    finally:
        await admin.close()
        await store.close()


async def test_custody_grant_rules(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, _w, _a, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        head = uuid.uuid4()
        until = await admin.fetchval("SELECT now() + interval '30 days'")
        await admin.execute(_INSERT, head, "archive_custody", "custody_chain", "c1", until)
        sql = (
            "INSERT INTO memory_custody_grants(grant_id, subject_id, resource_owner_id,"
            " grantor_id, purpose_scope_id, custody_chain_id, consent_id, evidence_event_id,"
            " policy_version, policy_hash, effective_at, expires_at, predecessor_grant_id,"
            " status) VALUES ($1,'child-1','parent-1','parent-1',$2,'c1','k1',$3,'v1','h1',"
            " now(), $4, $5, 'active')"
        )
        first, second = uuid.uuid4(), uuid.uuid4()
        await admin.execute(sql, first, head, "ev1", until, None)
        with pytest.raises(asyncpg.CheckViolationError):  # expiry must follow effect
            await admin.execute(
                sql,
                uuid.uuid4(),
                head,
                "ev2",
                await admin.fetchval("SELECT now() - interval '1 day'"),
                None,
            )
        await admin.execute(sql, second, head, "ev3", until, first)
        with pytest.raises(asyncpg.UniqueViolationError):  # one successor only
            await admin.execute(sql, uuid.uuid4(), head, "ev4", until, first)
        with pytest.raises(asyncpg.UniqueViolationError):  # evidence not reusable
            await admin.execute(sql, uuid.uuid4(), head, "ev1", until, None)
        app = await asyncpg.connect(api_dsn)
        try:
            assert await app.fetchval("SELECT count(*) FROM memory_custody_grants") == 0
            async with app.transaction():
                await app.execute(
                    "SELECT set_config('app.memory.actor_subject_id', 'parent-1', true)"
                )
                assert await app.fetchval("SELECT count(*) FROM memory_custody_grants") == 2
        finally:
            await app.close()
    finally:
        await admin.close()
        await store.close()


async def test_custody_items_reference_records_without_copying(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, _w, _a, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        head, grant = uuid.uuid4(), uuid.uuid4()
        until = await admin.fetchval("SELECT now() + interval '30 days'")
        await admin.execute(_INSERT, head, "archive_custody", "custody_chain", "c1", until)
        await admin.execute(
            "INSERT INTO memory_custody_grants(grant_id, subject_id, resource_owner_id,"
            " grantor_id, purpose_scope_id, custody_chain_id, consent_id, evidence_event_id,"
            " policy_version, policy_hash, effective_at, expires_at, status)"
            " VALUES ($1,'child-1','parent-1','parent-1',$2,'c1','k1','ev1','v1','h1',"
            " now(), $3, 'active')",
            grant,
            head,
            until,
        )
        item = (
            "INSERT INTO memory_custody_items(grant_id, record_id, source_purpose_scope_id,"
            " source_revision, source_scope_generation, record_version, eligibility_code)"
            " VALUES ($1, $2, $3, 1, 1, 1, $4)"
        )
        # a record that does not exist cannot be indexed
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await admin.execute(item, grant, "missing", head, "child_archive_whitelist")
        # only the whitelist eligibility code exists
        with pytest.raises(asyncpg.CheckViolationError):
            await admin.execute(item, grant, "missing", head, "claims")
        app = await asyncpg.connect(api_dsn)
        try:
            assert await app.fetchval("SELECT count(*) FROM memory_custody_items") == 0
        finally:
            await app.close()
    finally:
        await admin.close()
        await store.close()


async def test_lifecycle_operation_idempotency_and_guards(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, _w, _a, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        insert = (
            "INSERT INTO memory_lifecycle_operations(operation_id, actor_id, subject_id,"
            " resource_owner_id, action, idempotency_key, request_hash)"
            " VALUES ($1, 'parent-1', 'child-1', 'parent-1', $2, $3, 'h1')"
        )
        op = uuid.uuid4()
        await admin.execute(insert, op, "withdraw_memory", "key-00001")
        with pytest.raises(asyncpg.UniqueViolationError):  # same key, same actor+action
            await admin.execute(insert, uuid.uuid4(), "withdraw_memory", "key-00001")
        await admin.execute(insert, uuid.uuid4(), "withdraw_custody", "key-00001")
        with pytest.raises(asyncpg.CheckViolationError):  # key too short
            await admin.execute(insert, uuid.uuid4(), "withdraw_memory", "short")
        with pytest.raises(asyncpg.RaiseError):  # request hash is immutable
            await admin.execute(
                "UPDATE memory_lifecycle_operations SET request_hash='x' WHERE operation_id=$1",
                op,
            )
        with pytest.raises(asyncpg.CheckViolationError):  # completed needs completed_at
            await admin.execute(
                "UPDATE memory_lifecycle_operations SET status='completed' WHERE operation_id=$1",
                op,
            )
        await admin.execute(
            "UPDATE memory_lifecycle_operations SET status='completed', completed_at=now()"
            " WHERE operation_id=$1",
            op,
        )
        with pytest.raises(asyncpg.RaiseError):  # no reopening
            await admin.execute(
                "UPDATE memory_lifecycle_operations SET status='running', completed_at=NULL"
                " WHERE operation_id=$1",
                op,
            )
        app = await asyncpg.connect(api_dsn)
        try:
            assert await app.fetchval("SELECT count(*) FROM memory_lifecycle_operations") == 0
        finally:
            await app.close()
    finally:
        await admin.close()
        await store.close()


async def test_record_version_links_and_guarded_update(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, _w, _a, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(
            "INSERT INTO memory_records(record_id, scope, subject_id, resource_owner_id,"
            " created_at, payload, consent_snapshot_id, policy_receipt_id,"
            " created_by_actor_id) VALUES ('r1','personal_private','child-1','parent-1',"
            " now(), '{\"kind\":\"child_archive\"}', 'snap', 'rcpt', 'parent-1')"
        )
        row = await admin.fetchrow(
            "SELECT record_version, deletion_locked, purpose_links FROM memory_records"
            " WHERE record_id='r1'"
        )
        assert row is not None
        assert (row["record_version"], row["deletion_locked"], row["purpose_links"]) == (
            1,
            False,
            "[]",
        )
        link = (
            f'[{{"purpose_scope_id":"{uuid.uuid4()}","source_revision":1,'
            '"scope_generation":1,"basis_ref":"consent:k1"}]'
        )
        update = (
            "UPDATE memory_records SET record_version = record_version + 1,"
            " purpose_links = $1::jsonb, deletion_locked = $2 WHERE record_id='r1'"
        )
        # still append-only without the guarded path
        with pytest.raises(asyncpg.RaiseError):
            await admin.execute(update, link, False)
        async with admin.transaction():
            await admin.execute("SELECT set_config('app.memory.record_lineage_update','r1',true)")
            await admin.execute(update, link, True)
        assert (
            await admin.fetchval("SELECT record_version FROM memory_records WHERE record_id='r1'")
            == 2
        )
        # the guarded path may not skip versions or rewrite other columns
        async with admin.transaction():
            await admin.execute("SELECT set_config('app.memory.record_lineage_update','r1',true)")
            with pytest.raises(asyncpg.RaiseError):
                await admin.execute(
                    "UPDATE memory_records SET record_version = 9 WHERE record_id='r1'"
                )
        async with admin.transaction():
            await admin.execute("SELECT set_config('app.memory.record_lineage_update','r1',true)")
            with pytest.raises(asyncpg.RaiseError):
                await admin.execute(
                    "UPDATE memory_records SET record_version = record_version + 1,"
                    " subject_id = 'other' WHERE record_id='r1'"
                )
        # malformed lineage is rejected by the table itself
        async with admin.transaction():
            await admin.execute("SELECT set_config('app.memory.record_lineage_update','r1',true)")
            with pytest.raises(asyncpg.CheckViolationError):
                await admin.execute(update, '[{"purpose_scope_id":"x"}]', False)
    finally:
        await admin.close()
        await store.close()


async def test_upgrade_from_the_previous_schema_keeps_existing_rows(
    pg_env: tuple[str, str, str, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api_dsn, _w, _a, admin_db_dsn, password = pg_env
    old = subprocess.run(
        ["git", "show", "c0cd19f6:services/memory_scope/postgres_schema.sql"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = tmp_path / "old_schema.sql"
    old_path.write_text(old, encoding="utf-8")
    with monkeypatch.context() as patch:
        patch.setattr(postgres_store, "_SCHEMA_PATH", old_path)
        before = await _initialize_store(api_dsn, admin_db_dsn, password)
        await before.close()
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(
            "INSERT INTO memory_records(record_id, scope, subject_id, resource_owner_id,"
            " created_at, payload, consent_snapshot_id, policy_receipt_id,"
            " created_by_actor_id) VALUES ('legacy','personal_private','child-1','parent-1',"
            " now(), '{}', 'snap', 'rcpt', 'parent-1')"
        )
        assert not await admin.fetchval(
            "SELECT 1 FROM information_schema.tables WHERE table_name='memory_purpose_heads'"
        )
    finally:
        await admin.close()
    after = await _initialize_store(api_dsn, admin_db_dsn, password)  # new schema over old
    await after.close()
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        row = await admin.fetchrow(
            "SELECT record_version, deletion_locked, purpose_links FROM memory_records"
            " WHERE record_id='legacy'"
        )
        assert row is not None
        assert (row["record_version"], row["deletion_locked"], row["purpose_links"]) == (
            1,
            False,
            "[]",
        )
        for table in (
            "memory_purpose_heads",
            "memory_custody_grants",
            "memory_custody_items",
            "memory_lifecycle_operations",
        ):
            assert await admin.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE relname = $1", table
            )
    finally:
        await admin.close()
