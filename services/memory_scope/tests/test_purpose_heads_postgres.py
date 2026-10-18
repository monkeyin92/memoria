"""Real-PostgreSQL contract for ``memory_purpose_heads`` (B01).

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
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
