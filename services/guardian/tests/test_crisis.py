"""Minor crisis notification on the PostgreSQL guardian store.

The crisis evidence goes to a ``PostgresLifeArchive`` on the same clone, as the
archive role ``memoria_app``.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.archive.domain import IdempotencyConflictError
from services.archive.postgres_archive import PostgresLifeArchive
from services.guardian.crisis import (
    CrisisNotificationReceipt,
    CrisisNotificationService,
    CrisisNotificationUnavailableError,
    minor_crisis_event_id,
)
from services.guardian.domain import GuardianConflictError
from services.guardian.parent_alert import PARENT_ALERT_COPY, interrupted_parent_detail
from services.guardian.postgres_store import PostgresGuardianStore
from testing.guardian_seed import declare_binding_guardian
from testing.postgres_harness import TestDatabase


@pytest.fixture
async def archive(postgres_database: TestDatabase) -> AsyncIterator[PostgresLifeArchive]:
    life_archive = PostgresLifeArchive(postgres_database.role_dsn("memoria_app"))
    try:
        yield life_archive
    finally:
        await life_archive.close()


@pytest.mark.asyncio
async def test_minor_crisis_is_idempotent_and_contains_no_transcript_or_severity(
    postgres_database: TestDatabase,
    guardian_postgres_store: PostgresGuardianStore,
    archive: PostgresLifeArchive,
) -> None:
    store = guardian_postgres_store
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
        reply_template_id="crisis-support-reply",
        occurred_at=now,
    )
    replay = await service.record_minor_crisis(
        minor_user_id="minor-a",
        session_id="voice-session-a",
        turn_id=4,
        generation_id=2,
        tool_epoch=1,
        script_version="crisis-transfer-draft-v1",
        reply_template_id="crisis-support-reply",
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
        "reply_template_id": "crisis-support-reply",
        "notification_required": True,
        "attempt_push": True,
        "possibly_story_retell": False,
        "tool_epoch": 1,
        "contains_transcript": False,
        "contains_severity": False,
        "declared_guardian_count": 0,
    }
    assert first.reply_template_id == "crisis-support-reply"
    assert first.script_version == "crisis-transfer-draft-v1"
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        row = await admin.fetchrow(
            """
            SELECT reply_template_id, script_version
            FROM guardian_crisis_events
            WHERE crisis_event_id = $1::uuid
            """,
            first.crisis_event_id,
        )
    finally:
        await admin.close()
    assert row is not None
    assert str(row["reply_template_id"]) == "crisis-support-reply"
    assert str(row["script_version"]) == "crisis-transfer-draft-v1"


@pytest.mark.asyncio
async def test_relay_tier_crisis_records_event_evidence_and_a_pin_only_notification(
    guardian_postgres_store: PostgresGuardianStore,
    archive: PostgresLifeArchive,
) -> None:
    store = guardian_postgres_store
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

    async def record(*, attempt_push: bool) -> CrisisNotificationReceipt:
        return await service.record_minor_crisis(
            minor_user_id="minor-a",
            session_id="voice-session-relay",
            turn_id=6,
            generation_id=1,
            tool_epoch=1,
            script_version="support-other-v1",
            reply_template_id="support-other-v1",
            occurred_at=now,
            attempt_push=attempt_push,
        )

    first = await record(attempt_push=False)
    replay = await record(attempt_push=False)
    with pytest.raises(IdempotencyConflictError):
        await record(attempt_push=True)
    with pytest.raises(GuardianConflictError):
        await store.enqueue_crisis_event(
            crisis_event_id=first.crisis_event_id,
            evidence_event_id=first.evidence_event_id,
            minor_user_id="minor-a",
            occurred_at=now,
            script_version="support-other-v1",
            reply_template_id="support-other-v1",
            attempt_push=True,
        )

    assert first == replay
    assert first.notification_count == 1
    notifications = await store.guardian_notifications(guardian_user_id="guardian-a")
    assert [(item.crisis_event_id, item.status) for item in notifications] == [
        (first.crisis_event_id, "pin_only")
    ]
    event = await archive.event(account_id="minor-a", event_id=first.evidence_event_id)
    assert event is not None
    assert event.payload["notification_required"] is True
    assert event.payload["attempt_push"] is False


@pytest.mark.asyncio
async def test_declared_guardian_receives_the_crisis_without_an_active_link(
    postgres_database: TestDatabase,
    guardian_postgres_store: PostgresGuardianStore,
    archive: PostgresLifeArchive,
) -> None:
    """A declared guardian is a distinct, working notification basis.

    The subject has no account, so no guardian link can be confirmed.  The
    caller resolves the declaring guardian from the Identity authority and the
    enqueue must reach exactly that guardian — never an arbitrary id, and
    never by fabricating an active ``wechat_identity`` link.
    """

    store = guardian_postgres_store
    service = CrisisNotificationService(store, archive)
    now = datetime(2026, 9, 16, 4, 0, tzinfo=UTC)
    # PostgreSQL re-validates a declared guardian against Identity (P0-04).
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        await declare_binding_guardian(
            admin, guardian_id="guardian-declared", subject_id="minor-declared", at=now
        )
    finally:
        await admin.close()

    receipt = await service.record_minor_crisis(
        minor_user_id="minor-declared",
        session_id="voice-session-declared",
        turn_id=1,
        generation_id=1,
        tool_epoch=1,
        script_version="crisis-transfer-draft-v1",
        reply_template_id="crisis-support-reply",
        occurred_at=now,
        declared_guardian_ids=("guardian-declared",),
    )

    assert receipt.notification_count == 1
    assert await store.active_link(
        guardian_user_id="guardian-declared",
        minor_user_id="minor-declared",
    ) is None
    notifications = await store.guardian_notifications(
        guardian_user_id="guardian-declared"
    )
    assert [notification.minor_user_id for notification in notifications] == [
        "minor-declared"
    ]
    assert (
        await store.guardian_notifications(guardian_user_id="guardian-other") == ()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_push", (True, False))
async def test_minor_crisis_without_active_guardian_is_not_silently_accepted(
    guardian_postgres_store: PostgresGuardianStore,
    archive: PostgresLifeArchive,
    attempt_push: bool,
) -> None:
    store = guardian_postgres_store
    service = CrisisNotificationService(store, archive)

    with pytest.raises(CrisisNotificationUnavailableError):
        await service.record_minor_crisis(
            minor_user_id="minor-without-guardian",
            session_id="voice-session-a",
            turn_id=1,
            generation_id=1,
            tool_epoch=0,
            script_version="crisis-transfer-draft-v1",
            reply_template_id="crisis-support-reply",
            attempt_push=attempt_push,
        )


@pytest.mark.asyncio
async def test_script_interrupted_mark_keeps_the_tier_copy_until_the_flag_is_set(
    postgres_database: TestDatabase,
    guardian_postgres_store: PostgresGuardianStore,
    archive: PostgresLifeArchive,
) -> None:
    store = guardian_postgres_store
    now = datetime.now(UTC)
    digest = hashlib.sha256(b"binding-code-interrupt").hexdigest()
    link = await store.create_link(
        guardian_user_id="guardian-interrupt",
        minor_user_id="minor-interrupt",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=now + timedelta(minutes=15),
        now=now,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="minor-interrupt",
        binding_code_hash=digest,
        now=now,
    )
    service = CrisisNotificationService(store, archive)
    receipt = await service.record_minor_crisis(
        minor_user_id="minor-interrupt",
        session_id="voice-session-interrupt",
        turn_id=3,
        generation_id=3,
        tool_epoch=0,
        script_version="victim-support-v1",
        reply_template_id="victim-support-v1",
        occurred_at=now,
    )
    event_id = minor_crisis_event_id(
        minor_user_id="minor-interrupt",
        session_id="voice-session-interrupt",
        turn_id=3,
        generation_id=3,
        tool_epoch=0,
    )
    assert receipt.crisis_event_id == event_id
    before = await store.guardian_notifications(guardian_user_id="guardian-interrupt")
    assert len(before) == 1
    assert before[0].script_interrupted is False
    assert before[0].status == "pending"
    assert (
        interrupted_parent_detail(
            before[0].reply_template_id,
            script_interrupted=before[0].script_interrupted,
        )
        is None
    )
    assert (
        PARENT_ALERT_COPY[before[0].reply_template_id].parent_action
        == "尽快当面陪孩子聊聊，先让孩子安心，不反复追问细节。如有危险请拨打110"
    )

    assert await store.mark_script_interrupted(crisis_event_id=event_id) == "victim-support-v1"
    assert await store.mark_script_interrupted(crisis_event_id=event_id) == "victim-support-v1"
    assert (
        await store.mark_script_interrupted(
            crisis_event_id="00000000-0000-0000-0000-000000000000"
        )
        is None
    )
    after = await store.guardian_notifications(guardian_user_id="guardian-interrupt")
    assert len(after) == 1
    assert after[0].status == "pending"
    assert after[0].script_interrupted is True
    assert (
        interrupted_parent_detail(
            after[0].reply_template_id,
            script_interrupted=after[0].script_interrupted,
        )
        == "这次的提醒话术孩子没有听完，如有危险请拨打110"
    )
    replay = await service.record_minor_crisis(
        minor_user_id="minor-interrupt",
        session_id="voice-session-interrupt",
        turn_id=3,
        generation_id=3,
        tool_epoch=0,
        script_version="victim-support-v1",
        reply_template_id="victim-support-v1",
        occurred_at=now,
    )
    assert replay.notification_count == 1
    kept = await store.guardian_notifications(guardian_user_id="guardian-interrupt")
    assert kept[0].script_interrupted is True
    api = await asyncpg.connect(postgres_database.role_dsn("memoria_guardian"))
    try:
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await api.execute("UPDATE guardian_crisis_events SET script_interrupted = FALSE")
    finally:
        await api.close()
