"""Real-PostgreSQL contract for the daily child layer retention pass.

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from services.guardian.child_memory import (
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    ChildMemoryKind,
    DialogueSummary,
)
from services.guardian.subject_activity import SubjectActivityStore
from services.memory_scope.child_layer_retention import (
    ChildLayerRetentionWorker,
    ExpirySummary,
)
from services.memory_scope.child_layers import ChildLayerStore
from services.memory_scope.tests import test_postgres_store
from services.memory_scope.tests.test_child_layers_postgres import (
    _close_layers,
    _layers,
)

pg_env = test_postgres_store.pg_env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
        reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
    ),
]

PARENT = "parent-retention-owner"
DUE = "child-retention-due"
IDLE = "child-retention-idle"
ACTIVE = "child-retention-active"


def _activity_table_sql() -> str:
    schema = (Path(__file__).resolve().parents[2] / "guardian" / "postgres_schema.sql").read_text(
        encoding="utf-8"
    )
    start = schema.index("CREATE TABLE IF NOT EXISTS guardian_subject_activity")
    return schema[start : schema.index(");", start) + 2]


async def _seed(store: ChildLayerStore) -> None:
    for created_at, text in (
        (datetime.now(UTC) - timedelta(days=31), "孩子聊了天气"),
        (datetime.now(UTC), "孩子聊了周末计划"),
    ):
        await store.write_summary(
            DialogueSummary(
                subject_id=DUE,
                text=text,
                source_event_id=f"evt-{text}",
                session_id="sess-retention",
            ),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-retention-summary",
            created_at=created_at,
        )
    for subject_id in (IDLE, ACTIVE):
        await store.write_archive_fact(
            ArchiveFact(subject_id=subject_id, field="pet_name", text="宠物叫豆豆"),
            resource_owner_id=PARENT,
            actor_id=PARENT,
            consent_snapshot_id="consent-retention-archive",
        )


async def _texts(store: ChildLayerStore, subject_id: str, kind: ChildMemoryKind) -> list[str]:
    records = await store.list(
        subject_id=subject_id,
        kind=kind,
        viewer_subject_id=PARENT,
    )
    return [record.text for record in records]


async def test_retention_pass_erases_due_rows_once_and_then_nothing(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    activity = SubjectActivityStore(admin_db_dsn)
    try:
        admin = await asyncpg.connect(admin_db_dsn)
        try:
            await admin.execute(_activity_table_sql())
        finally:
            await admin.close()
        now = datetime.now(UTC)
        await activity.touch(
            subject_person_id=IDLE, source="dialogue", now=now - timedelta(days=200)
        )
        await activity.touch(subject_person_id=ACTIVE, source="dialogue", now=now)
        await _seed(store)
        worker = ChildLayerRetentionWorker(store, activity)

        first = await worker.run_once(now=now)
        second = await worker.run_once(now=now)

        assert first == [
            ExpirySummary(KIND_DIALOGUE_SUMMARY, 1, 0, 1, 1),
            ExpirySummary(KIND_CHILD_ARCHIVE, 1, 0, 1, 1),
        ]
        assert second == [
            ExpirySummary(KIND_DIALOGUE_SUMMARY, 0, 0, 0, 0),
            ExpirySummary(KIND_CHILD_ARCHIVE, 0, 0, 0, 0),
        ]
        assert await _texts(store, DUE, KIND_DIALOGUE_SUMMARY) == ["孩子聊了周末计划"]
        assert await _texts(store, IDLE, KIND_CHILD_ARCHIVE) == []
        assert await _texts(store, ACTIVE, KIND_CHILD_ARCHIVE) == ["宠物叫豆豆"]
    finally:
        await activity.close()
        await _close_layers(store, bootstrap, admin_db_dsn)


async def test_missing_activity_table_keeps_archives_and_still_expires_summaries(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    activity = SubjectActivityStore(admin_db_dsn)
    try:
        await _seed(store)
        worker = ChildLayerRetentionWorker(store, activity)

        summaries = await worker.run_once()

        assert summaries == [ExpirySummary(KIND_DIALOGUE_SUMMARY, 1, 0, 1, 1)]
        assert await _texts(store, DUE, KIND_DIALOGUE_SUMMARY) == ["孩子聊了周末计划"]
        assert await _texts(store, IDLE, KIND_CHILD_ARCHIVE) == ["宠物叫豆豆"]
        assert await _texts(store, ACTIVE, KIND_CHILD_ARCHIVE) == ["宠物叫豆豆"]
    finally:
        await activity.close()
        await _close_layers(store, bootstrap, admin_db_dsn)


async def test_retention_expiry_audits_only_the_call_that_deletes(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    try:
        await _seed(store)
        admin = await asyncpg.connect(admin_db_dsn)
        try:
            erased = await store.expire_due_summaries(subject_id=DUE)
            rows = await admin.fetch(
                "SELECT * FROM memory_audit_events"
                " WHERE action = 'child_layer_erase'"
            )
            assert int(erased["erased"]["memory_records"]) == 1
            assert len(rows) == 1, rows
            row = rows[0]
            payload = (
                json.loads(row["payload"])
                if isinstance(row["payload"], str)
                else dict(row["payload"])
            )
            assert row["subject_id"] is None
            assert row["record_id"] is None
            assert row["proposal_id"] is None
            assert row["actor_subject_id"] == "system:child_layer_erase"
            assert payload["kind"] == KIND_DIALOGUE_SUMMARY
            assert payload["memory_records"] == 1
            assert payload["memory_status_events"] == 1
            assert payload["only_expired"] is True
            assert payload["reason"] == "retention_expiry"
            assert row["created_at"] is not None
            serialized = json.dumps(dict(row), default=str, ensure_ascii=False)
            assert "previous_value" not in serialized
            assert DUE not in serialized

            again = await store.expire_due_summaries(subject_id=DUE)
            count = await admin.fetchval(
                "SELECT count(*) FROM memory_audit_events"
                " WHERE action = 'child_layer_erase'"
            )
            assert int(again["erased"]["memory_records"]) == 0
            assert count == 1
        finally:
            await admin.close()
    finally:
        await _close_layers(store, bootstrap, admin_db_dsn)
