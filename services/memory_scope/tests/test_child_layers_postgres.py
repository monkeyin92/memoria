"""Real-PostgreSQL contract for dialogue_summary / child_archive layers.

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set. Proves physical delete,
30-day remaining-count check, unbind-style purge of both layers, and that
verbatim-looking archive facts are rejected before insert.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.guardian.child_memory import (
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    DialogueSummary,
)
from services.memory_scope.child_layers import ChildLayerStore
from services.memory_scope.subject_erasure import MAINTENANCE_ROLE
from services.memory_scope.tests import test_postgres_store
from services.memory_scope.tests.test_postgres_store import (
    _dsn_with,
    _initialize_store,
)

pg_env = test_postgres_store.pg_env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
        reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
    ),
]

CHILD = "child-layer-subject"
PARENT = "parent-layer-owner"


async def _layers(
    pg_env: tuple[str, str, str, str, str],
) -> tuple[ChildLayerStore, object, str]:
    api_dsn, _worker_dsn, _action_dsn, admin_db_dsn, password = pg_env
    bootstrap = await _initialize_store(api_dsn, admin_db_dsn, password)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(
            f"ALTER ROLE {MAINTENANCE_ROLE} WITH LOGIN PASSWORD '{password}'"
        )
    finally:
        await admin.close()
    database = admin_db_dsn.rsplit("/", 1)[1]
    maintenance = _dsn_with(
        admin_db_dsn, database=database, user=MAINTENANCE_ROLE, password=password
    )
    store = ChildLayerStore(api_dsn=api_dsn, maintenance_dsn=maintenance)
    await store.initialize()
    return store, bootstrap, admin_db_dsn


async def _close_layers(
    store: ChildLayerStore, bootstrap: object, admin_db_dsn: str
) -> None:
    await store.close()
    await bootstrap.close()  # type: ignore[union-attr]
    database = admin_db_dsn.rsplit("/", 1)[1]
    cluster_dsn = _dsn_with(admin_db_dsn, database="postgres")
    admin = await asyncpg.connect(cluster_dsn)
    try:
        await admin.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            database,
        )
    finally:
        await admin.close()


async def test_parent_can_write_list_delete_summary_and_requery_is_empty(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    try:
        summary = DialogueSummary(
            subject_id=CHILD,
            text="孩子聊了小狗和画画",
            source_event_id="evt-1",
            session_id="sess-1",
        )
        written = await store.write_summary(
            summary,
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-memory-1",
        )
        listed = await store.list(
            subject_id=CHILD,
            kind=KIND_DIALOGUE_SUMMARY,
            viewer_subject_id=PARENT,
        )
        assert [item.record_id for item in listed] == [written.record_id]
        assert listed[0].text == "孩子聊了小狗和画画"
        assert '"' not in listed[0].text

        erased = await store.delete_record(
            subject_id=CHILD, record_id=written.record_id
        )
        assert int(erased["memory_records"]) == 1
        again = await store.list(
            subject_id=CHILD,
            kind=KIND_DIALOGUE_SUMMARY,
            viewer_subject_id=PARENT,
        )
        assert again == []
        missing = await store.get(
            subject_id=CHILD,
            record_id=written.record_id,
            viewer_subject_id=PARENT,
        )
        assert missing is None
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


async def test_expired_summary_physical_delete_remaining_count_zero(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    try:
        past = datetime.now(UTC) - timedelta(days=31)
        written = await store.write_summary(
            DialogueSummary(
                subject_id=CHILD,
                text="孩子聊了天气",
                source_event_id="evt-expired",
                session_id="sess-old",
            ),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-memory-2",
            created_at=past,
        )
        assert written.retention_expires_at is not None
        assert written.retention_expires_at <= datetime.now(UTC)

        result = await store.expire_due_summaries(subject_id=CHILD)
        assert int(result["erased"]["memory_records"]) == 1
        assert int(result["remaining"]["memory_records"]) == 0
        listed = await store.list(
            subject_id=CHILD,
            kind=KIND_DIALOGUE_SUMMARY,
            viewer_subject_id=PARENT,
        )
        assert listed == []
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


async def test_child_archive_whitelist_and_unbind_purges_both_layers(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    try:
        with pytest.raises(ValueError):
            ArchiveFact(
                subject_id=CHILD,
                field="preference",
                text="孩子在学校被欺负了",
            )
        await store.write_summary(
            DialogueSummary(
                subject_id=CHILD,
                text="孩子聊了周末计划",
                source_event_id="evt-fresh",
                session_id="sess-fresh",
            ),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-memory-3",
        )
        await store.write_archive_fact(
            ArchiveFact(subject_id=CHILD, field="pet_name", text="宠物叫豆豆"),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-archive-1",
        )
        assert await store.list(
            subject_id=CHILD,
            kind=KIND_DIALOGUE_SUMMARY,
            viewer_subject_id=PARENT,
        )
        assert await store.list(
            subject_id=CHILD,
            kind=KIND_CHILD_ARCHIVE,
            viewer_subject_id=PARENT,
        )

        purged = await store.purge_both_layers(subject_id=CHILD)
        assert int(purged["dialogue_summary"]["memory_records"]) >= 1
        assert int(purged["child_archive"]["memory_records"]) >= 1
        assert (
            await store.list(
                subject_id=CHILD,
                kind=KIND_DIALOGUE_SUMMARY,
                viewer_subject_id=PARENT,
            )
            == []
        )
        assert (
            await store.list(
                subject_id=CHILD,
                kind=KIND_CHILD_ARCHIVE,
                viewer_subject_id=PARENT,
            )
            == []
        )
        # verified_empty coverage: full subject remaining includes both kinds
        # (zero after purge). Layer remaining already asserted above.
        left_summary = await store.remaining_kind(
            subject_id=CHILD, kind=KIND_DIALOGUE_SUMMARY
        )
        left_archive = await store.remaining_kind(
            subject_id=CHILD, kind=KIND_CHILD_ARCHIVE
        )
        assert int(left_summary["memory_records"]) == 0
        assert int(left_archive["memory_records"]) == 0
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


async def _ensure_vector_table(admin_db_dsn: str) -> None:
    """The archive vector table, which this service's schema does not create."""

    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await admin.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_vector_documents (
                item_id UUID NOT NULL,
                account_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimensions INTEGER NOT NULL,
                embedding vector NOT NULL,
                source_event_id TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (item_id, embedding_model, embedding_dimensions)
            )
            """
        )
    finally:
        await admin.close()


async def _vector_total(admin_db_dsn: str) -> int:
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        return int(await admin.fetchval("SELECT count(*) FROM memory_vector_documents"))
    finally:
        await admin.close()


async def _lock_records(admin: asyncpg.Connection, record_ids: set[str]) -> None:
    """Set deletion_locked through the schema's guarded lineage-update escape."""

    for record_id in record_ids:
        await admin.execute(
            "SELECT set_config('app.memory.record_lineage_update', $1, false)", record_id
        )
        await admin.execute(
            "UPDATE memory_records SET deletion_locked = true,"
            " record_version = record_version + 1 WHERE record_id = $1",
            record_id,
        )
        await admin.execute("SELECT set_config('app.memory.record_lineage_update', '', false)")


async def test_child_layer_writes_create_no_vectors(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    try:
        await _ensure_vector_table(admin_db_dsn)
        before = await _vector_total(admin_db_dsn)
        summary = await store.write_summary(
            DialogueSummary(
                subject_id=CHILD,
                text="孩子聊了小狗",
                source_event_id="evt-no-vector-summary",
                session_id="sess-no-vector",
            ),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-memory-no-vector",
        )
        assert await _vector_total(admin_db_dsn) == before
        first = await store.write_archive_fact(
            ArchiveFact(subject_id=CHILD, field="pet_name", text="宠物叫豆豆"),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-archive-no-vector",
        )
        assert await _vector_total(admin_db_dsn) == before
        second = await store.upsert_archive_fact(
            ArchiveFact(subject_id=CHILD, field="preference", text="喜欢蓝色"),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-archive-no-vector",
        )
        assert second.record_id != first.record_id
        assert await _vector_total(admin_db_dsn) == before
        touched = await store.upsert_archive_fact(
            ArchiveFact(subject_id=CHILD, field="preference", text="喜欢蓝色"),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-archive-no-vector",
        )
        assert touched.record_id == second.record_id
        assert await _vector_total(admin_db_dsn) == before

        erased = await store.delete_record(subject_id=CHILD, record_id=summary.record_id)
        assert int(erased["memory_records"]) == 1
        assert await _vector_total(admin_db_dsn) == before
        await store.erase_kind(subject_id=CHILD, kind=KIND_CHILD_ARCHIVE)
        assert await _vector_total(admin_db_dsn) == before
        await store.purge_both_layers(subject_id=CHILD)
        assert await _vector_total(admin_db_dsn) == before
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)


async def test_deletion_locked_does_not_block_parent_delete_or_purge(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        summary = await store.write_summary(
            DialogueSummary(
                subject_id=CHILD,
                text="孩子聊了天气",
                source_event_id="evt-locked-summary",
                session_id="sess-locked",
            ),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-locked-summary",
        )
        fact = await store.write_archive_fact(
            ArchiveFact(subject_id=CHILD, field="pet_name", text="宠物叫豆豆"),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-locked-archive",
        )
        await _lock_records(admin, {summary.record_id, fact.record_id})
        locked = await admin.fetch(
            "SELECT record_id, deletion_locked FROM memory_records WHERE subject_id = $1"
            " ORDER BY record_id",
            CHILD,
        )
        assert {str(row["record_id"]) for row in locked} == {
            summary.record_id,
            fact.record_id,
        }
        assert all(row["deletion_locked"] is True for row in locked)

        erased = await store.delete_record(subject_id=CHILD, record_id=summary.record_id)
        assert int(erased["memory_records"]) == 1
        erased = await store.erase_kind(subject_id=CHILD, kind=KIND_CHILD_ARCHIVE)
        assert int(erased["memory_records"]) == 1
        assert await store.get(
            subject_id=CHILD, record_id=summary.record_id, viewer_subject_id=PARENT
        ) is None

        again = await store.write_summary(
            DialogueSummary(
                subject_id=CHILD,
                text="孩子聊了周末计划",
                source_event_id="evt-locked-summary-2",
                session_id="sess-locked-2",
            ),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-locked-summary-2",
        )
        await store.write_archive_fact(
            ArchiveFact(subject_id=CHILD, field="appellation", text="小名豆豆"),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-locked-archive-2",
        )
        remaining_ids = {
            str(row["record_id"])
            for row in await admin.fetch(
                "SELECT record_id FROM memory_records WHERE subject_id = $1", CHILD
            )
        }
        await _lock_records(admin, remaining_ids)
        purged = await store.purge_both_layers(subject_id=CHILD)
        assert int(purged["dialogue_summary"]["memory_records"]) >= 1
        assert int(purged["child_archive"]["memory_records"]) >= 1
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM memory_records WHERE subject_id = $1", CHILD
            )
            == 0
        )
        assert again.record_id not in {
            str(row["record_id"]) for row in await admin.fetch("SELECT record_id FROM memory_records")
        }
    finally:
        await admin.close()
        await _close_layers(store, bootstrap, admin_db_dsn)
