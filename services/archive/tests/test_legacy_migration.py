from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from services.archive.domain import ContextQuery
from services.archive.life_archive import LifeArchive
from services.archive.migration import migrate_legacy_sqlite


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
async def test_legacy_migration_is_read_only_deterministic_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "archive.sqlite3"
    _legacy_database(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    dry_run = migrate_legacy_sqlite(source, target, dry_run=True)
    first = migrate_legacy_sqlite(source, target)
    second = migrate_legacy_sqlite(source, target)

    assert dry_run.eligible_messages == 2
    assert dry_run.inserted == 0
    assert first.inserted == 2
    assert first.duplicates == 0
    assert second.inserted == 0
    assert second.duplicates == 2
    assert first.event_set_sha256 == second.event_set_sha256 == dry_run.event_set_sha256
    assert first.legacy_projection_count == 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    archive = LifeArchive.sqlite(target)
    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert len(context.evidence) == 1
    assert all(item.event_type == "legacy.message_imported" for item in context.evidence)
    assistant = next(item for item in context.evidence if item.speaker_class == "assistant")
    assert assistant.payload["actual_heard"] == "unverified_legacy"
    assert all(item.speaker_class != "uncertain" for item in context.evidence)


@pytest.mark.asyncio
async def test_failed_legacy_migration_rolls_back_the_whole_batch(tmp_path: Path) -> None:
    source = tmp_path / "legacy.sqlite3"
    target = tmp_path / "archive.sqlite3"
    _legacy_database(source)
    archive = LifeArchive.sqlite(target)
    archive.initialize()
    with sqlite3.connect(target) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_legacy_assistant
            BEFORE INSERT ON evidence_events
            WHEN NEW.speaker_class = 'assistant'
            BEGIN
                SELECT RAISE(ABORT, 'simulated migration failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="simulated migration failure"):
        migrate_legacy_sqlite(source, target)

    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert context.evidence == ()
