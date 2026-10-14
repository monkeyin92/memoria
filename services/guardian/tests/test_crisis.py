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
)
from services.guardian.domain import GuardianConflictError
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
