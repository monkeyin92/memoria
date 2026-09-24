"""Subject erasure against the FORCE-RLS PostgreSQL memory schema.

Rows are written through the real API/worker roles; erasure runs ONLY as the
``memoria_memory_maintenance`` login through ``memory_subject_erase``.  The
API/worker roles cannot call it, and after it every ordinary UPDATE/DELETE is
still rejected - by ACL for the runtime roles and by the append-only guards
for the owner, even with the erase target set by hand.

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.governance.subject_ports import SubjectMemoryScopePort
from services.memory_scope.postgres_store import PostgresMemoryStore
from services.memory_scope.subject_erasure import (
    MAINTENANCE_ROLE,
    PostgresSubjectMemoryScope,
)
from services.memory_scope.tests import test_postgres_store
from services.memory_scope.tests.test_postgres_store import (
    _dsn_with,
    _initialize_store,
)
from services.memory_scope.tests.test_subject_erasure import (
    CHILD,
    CHILD_EVIDENCE,
    ELDER,
    OWNER,
    VoteKeys,
    _read,
    _record,
    assert_subject_erasure,
    seed_subject_scenario,
)

#: The shared fresh-database + runtime-roles fixture.
pg_env = test_postgres_store.pg_env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
        reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
    ),
]


async def _seed_capture_evidence(admin_db_dsn: str) -> None:
    """Capture evidence is written by the owner-side projector port; seed the
    projection rows directly (superuser) for the child and the elder."""
    now = datetime.now(UTC)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.executemany(
            "INSERT INTO memory_capture_evidence (evidence_id, revision,"
            " canonical_hash, status, subject_id, binding_id, binding_version,"
            " valid_from, valid_until)"
            " VALUES ($1, 1, $2, 'active', $3, 'binding-1', 1, $4, $5)",
            [
                (CHILD_EVIDENCE, "1" * 64, CHILD, now, now + timedelta(hours=1)),
                ("evidence-elder-1", "2" * 64, ELDER, now, now + timedelta(hours=1)),
            ],
        )
    finally:
        await admin.close()


def _admin_vote_keys(admin_db_dsn: str) -> VoteKeys:
    async def keys() -> set[tuple[str, str, str]]:
        admin = await asyncpg.connect(admin_db_dsn)
        try:
            rows = await admin.fetch(
                "SELECT proposal_id, subject_id, decision FROM memory_shared_votes"
            )
        finally:
            await admin.close()
        return {(row[0], row[1], row[2]) for row in rows}

    return keys


async def test_postgres_subject_erasure_is_narrow_and_idempotent(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, worker_dsn, _action_dsn, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    family = PostgresMemoryStore(api_dsn).with_role("api")
    await family.initialize()
    stores = (store, family)
    worker = PostgresMemoryStore(worker_dsn).with_role("worker")
    await worker.initialize()
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(
            f"ALTER ROLE {MAINTENANCE_ROLE} WITH LOGIN PASSWORD '{password}'"
        )
    finally:
        await admin.close()
    database = admin_db_dsn.rsplit("/", 1)[1]
    erasure = PostgresSubjectMemoryScope(
        _dsn_with(admin_db_dsn, database=database, user=MAINTENANCE_ROLE, password=password)
    )
    await erasure.initialize()
    port: SubjectMemoryScopePort = erasure
    assert port is erasure
    try:
        await _seed_capture_evidence(admin_db_dsn)
        seeded = await seed_subject_scenario(store, family)
        # An owner record citing the child's capture evidence is the child's
        # too (subject export rule), though it names only the owner.
        cited = _record("r-owner-cites-child", OWNER, evidence=CHILD_EVIDENCE)
        await store.persist_record(cited, actor_family_space_id=None)
        seeded.erased["r-owner-cites-child"] = (OWNER, None)
        await worker.mark_outbox_processed("out-r-child")

        # Runtime roles hold no EXECUTE on the erase ports.
        for dsn in (api_dsn, worker_dsn):
            connection = await asyncpg.connect(dsn)
            try:
                for function in ("memory_subject_erase", "memory_subject_remaining"):
                    with pytest.raises(asyncpg.PostgresError, match="permission denied"):
                        await connection.fetchval(f"SELECT {function}($1)", CHILD)
            finally:
                await connection.close()

        await assert_subject_erasure(
            stores,
            erasure,
            seeded,
            _admin_vote_keys(admin_db_dsn),
            extra_erased={"memory_records": 3, "memory_capture_evidence": 1},
        )

        admin = await asyncpg.connect(admin_db_dsn)
        try:
            outbox = {
                row["outbox_id"]: row
                for row in await admin.fetch("SELECT * FROM memory_outbox")
            }
            assert outbox["out-r-child"]["status"] == "processed"
            assert outbox["out-r-child"]["payload"] in ("{}", {})
            assert outbox["out-r-owner"]["status"] == "pending"
            evidence = {
                row["evidence_id"]
                for row in await admin.fetch("SELECT evidence_id FROM memory_capture_evidence")
            }
            assert evidence == {"evidence-elder-1"}
            audit = await admin.fetchrow(
                "SELECT subject_id, payload FROM memory_audit_events"
                " WHERE event_id = 'audit-r-child'"
            )
            assert audit is not None and audit["subject_id"] == CHILD
            assert audit["payload"] in ("{}", {})

            # The owner still hits the append-only guards, even with the
            # erase target set by hand for a row that does not name it...
            await admin.execute("SET row_security = off")
            for statement in (
                "DELETE FROM memory_records WHERE record_id = 'r-owner'",
                "UPDATE memory_records SET payload = '{}' WHERE record_id = 'r-owner'",
                "DELETE FROM memory_status_events WHERE record_id = 'r-owner'",
                "DELETE FROM memory_shared_votes WHERE subject_id = 'person-elder'",
            ):
                with pytest.raises(asyncpg.PostgresError, match="append-only"):
                    await admin.execute(statement)
            async with admin.transaction():
                await admin.execute("SET LOCAL ROLE memoria_memory_owner")
                await admin.execute("SET LOCAL row_security = on")
                await admin.execute(
                    "SELECT set_config('app.memory.subject_erase', $1, true)", CHILD
                )
                with pytest.raises(asyncpg.PostgresError, match="append-only"):
                    await admin.execute(
                        "DELETE FROM memory_records WHERE record_id = 'r-owner'"
                    )
        finally:
            await admin.close()

        # ...and the runtime roles are still refused by ACL.
        for dsn in (api_dsn, worker_dsn):
            connection = await asyncpg.connect(dsn)
            try:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.memory.actor_subject_id', $1, true)",
                        OWNER,
                    )
                    await connection.execute(
                        "SELECT set_config('app.memory.subject_erase', $1, true)",
                        OWNER,
                    )
                    with pytest.raises(asyncpg.PostgresError, match="permission denied"):
                        await connection.execute(
                            "DELETE FROM memory_records WHERE record_id = 'r-owner'"
                        )
            finally:
                await connection.close()
        assert await _read(stores, "r-owner", (OWNER, None)) is not None
    finally:
        await erasure.close()
        await store.close()
        await family.close()
        await worker.close()


async def test_postgres_subject_erasure_refuses_runtime_logins(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    api_dsn, _worker_dsn, _action_dsn, admin_db_dsn, password = pg_env
    store = await _initialize_store(api_dsn, admin_db_dsn, password)
    try:
        with pytest.raises(ValueError, match=MAINTENANCE_ROLE):
            PostgresSubjectMemoryScope(api_dsn)
    finally:
        await store.close()
