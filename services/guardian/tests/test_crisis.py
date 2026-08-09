from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.archive.life_archive import LifeArchive
from services.guardian.crisis import (
    CrisisNotificationService,
    CrisisNotificationUnavailableError,
)
from services.guardian.sqlite_store import SqliteGuardianStore


@pytest.mark.asyncio
async def test_minor_crisis_is_idempotent_and_contains_no_transcript_or_severity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guardian.sqlite3"
    archive = LifeArchive.sqlite(path)
    store = SqliteGuardianStore(path)
    store.initialize()
    now = datetime.now(UTC)
    digest = hashlib.sha256(b"binding-code").hexdigest()
    link = await store.create_link(
        guardian_user_id="guardian-a",
        minor_user_id="minor-a",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=now + timedelta(minutes=15),
        now=now,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="minor-a",
        binding_code_hash=digest,
        now=now,
    )
    service = CrisisNotificationService(store, archive)

    first = await service.record_minor_crisis(
        minor_user_id="minor-a",
        session_id="voice-session-a",
        turn_id=4,
        generation_id=2,
        tool_epoch=1,
        script_version="crisis-transfer-draft-v1",
        occurred_at=now,
    )
    replay = await service.record_minor_crisis(
        minor_user_id="minor-a",
        session_id="voice-session-a",
        turn_id=4,
        generation_id=2,
        tool_epoch=1,
        script_version="crisis-transfer-draft-v1",
        occurred_at=now,
    )
    notifications = await store.guardian_notifications(guardian_user_id="guardian-a")
    event = await archive.event(
        account_id="minor-a",
        event_id=first.evidence_event_id,
    )

    assert first == replay
    assert first.notification_count == 1
    assert len(notifications) == 1
    assert notifications[0].status == "pending"
    assert event is not None
    assert dict(event.payload) == {
        "script_version": "crisis-transfer-draft-v1",
        "notification_required": True,
        "tool_epoch": 1,
        "contains_transcript": False,
        "contains_severity": False,
    }


@pytest.mark.asyncio
async def test_minor_crisis_without_active_guardian_is_not_silently_accepted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "guardian.sqlite3"
    archive = LifeArchive.sqlite(path)
    store = SqliteGuardianStore(path)
    store.initialize()
    service = CrisisNotificationService(store, archive)

    with pytest.raises(CrisisNotificationUnavailableError):
        await service.record_minor_crisis(
            minor_user_id="minor-without-guardian",
            session_id="voice-session-a",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            script_version="crisis-transfer-draft-v1",
        )
