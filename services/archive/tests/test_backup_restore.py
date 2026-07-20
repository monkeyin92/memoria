from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.backup import create_sqlite_backup, restore_sqlite_backup
from services.archive.domain import ContextQuery, EvidenceEvent
from services.archive.life_archive import LifeArchive


@pytest.mark.asyncio
async def test_backup_restores_an_empty_archive_with_verified_counts(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backups" / "archive.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    archive = LifeArchive.sqlite(source)
    await archive.record(
        EvidenceEvent(
            event_id="backup-event-001",
            account_id="account-001",
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 11, 0, tzinfo=UTC),
            speaker_class="owner",
            source="backup-test",
            payload={"text": "需要恢复的证据。"},
        )
    )

    manifest = create_sqlite_backup(source, backup)
    report = restore_sqlite_backup(backup, manifest.manifest_path, restored)

    assert report.integrity_check == "ok"
    assert report.event_count == manifest.event_count == 1
    assert report.outbox_count == manifest.outbox_count == 1
    recovered = LifeArchive.sqlite(restored)
    context = await recovered.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert [item.event_id for item in context.evidence] == ["backup-event-001"]
