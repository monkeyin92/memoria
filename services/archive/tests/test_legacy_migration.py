"""The read-only legacy SQLite message import into the PostgreSQL archive.

The legacy source is a SQLite file by nature; the target is PostgreSQL only.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from services.archive.domain import ContextQuery
from services.archive.migration import migrate_legacy_sqlite_to_postgres
from services.archive.postgres_archive import PostgresLifeArchive


def _legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                emotion TEXT,
                local_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE daily_summaries (
                user_id TEXT NOT NULL,
                summary_date TEXT NOT NULL,
                content_json TEXT NOT NULL,
                source TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                generated_at TEXT NOT NULL
            );
            INSERT INTO messages VALUES
                (1, 'account-001', 'user', '我在杭州读过书。', 'calm', '2026-07-19',
                 '2026-07-19T08:00:00Z'),
                (2, 'account-001', 'assistant', '这是一段旧回复。', NULL, '2026-07-19',
                 '2026-07-19T08:00:01Z');
            INSERT INTO daily_summaries VALUES
                ('account-001', '2026-07-19', '{}', 'fallback', 2,
                 '2026-07-19T09:00:00Z');
            """
        )


@pytest.mark.asyncio
async def test_legacy_migration_is_read_only_deterministic_and_idempotent(
    tmp_path: Path, archive: PostgresLifeArchive
) -> None:
    source = tmp_path / "legacy.sqlite3"
    _legacy_database(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    dry_run = await migrate_legacy_sqlite_to_postgres(source, archive, dry_run=True)
    first = await migrate_legacy_sqlite_to_postgres(source, archive)
    second = await migrate_legacy_sqlite_to_postgres(source, archive)

    assert dry_run.eligible_messages == 2
    assert dry_run.inserted == 0
    assert first.inserted == 2
    assert first.duplicates == 0
    assert second.inserted == 0
    assert second.duplicates == 2
    assert first.event_set_sha256 == second.event_set_sha256 == dry_run.event_set_sha256
    assert first.legacy_projection_count == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert len(context.evidence) == 1
    assert all(item.event_type == "legacy.message_imported" for item in context.evidence)
    assistant = next(item for item in context.evidence if item.speaker_class == "assistant")
    assert assistant.payload["actual_heard"] == "unverified_legacy"
    assert all(item.speaker_class != "uncertain" for item in context.evidence)


@pytest.mark.asyncio
async def test_failed_legacy_migration_rolls_back_the_whole_batch(
    tmp_path: Path,
    archive: PostgresLifeArchive,
    owner_sql: Callable[..., list[tuple[Any, ...]]],
) -> None:
    source = tmp_path / "legacy.sqlite3"
    _legacy_database(source)
    owner_sql(
        """
        CREATE FUNCTION reject_legacy_assistant() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'simulated migration failure';
        END
        $$
        """
    )
    owner_sql(
        """
        CREATE TRIGGER reject_legacy_assistant
        BEFORE INSERT ON archive_evidence_events
        FOR EACH ROW WHEN (NEW.speaker_class = 'assistant')
        EXECUTE FUNCTION reject_legacy_assistant()
        """
    )

    with pytest.raises(asyncpg.RaiseError, match="simulated migration failure"):
        await migrate_legacy_sqlite_to_postgres(source, archive)

    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert context.evidence == ()
    assert owner_sql(
        "SELECT count(*) FROM archive_evidence_events WHERE account_id = 'account-001'"
    ) == [(0,)]
