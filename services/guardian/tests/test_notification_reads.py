from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.guardian.domain import GuardianNotFoundError
from services.guardian.postgres_store import PostgresGuardianStore
from testing.postgres_harness import TestDatabase

NOW = datetime(2026, 10, 16, 11, 42, tzinfo=UTC)
CRISES = (
    ("7d1f9c1e-0000-4000-8000-0000000000a1", "crisis-self-v1"),
    ("7d1f9c1e-0000-4000-8000-0000000000a2", "victim-support-v1"),
    ("7d1f9c1e-0000-4000-8000-0000000000a3", "crisis-support-reply"),
)


async def _link(store: PostgresGuardianStore) -> str:
    digest = hashlib.sha256(b"binding-code").hexdigest()
    link = await store.create_link(
        guardian_user_id="guardian-a",
        minor_user_id="minor-a",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=NOW + timedelta(minutes=15),
        now=NOW,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="minor-a",
        binding_code_hash=digest,
        now=NOW,
    )
    return link.link_id


async def _crisis(store: PostgresGuardianStore, index: int) -> str:
    crisis_event_id, template = CRISES[index]
    await store.enqueue_crisis_event(
        crisis_event_id=crisis_event_id,
        evidence_event_id=f"guardian-crisis:{crisis_event_id}",
        minor_user_id="minor-a",
        occurred_at=NOW + timedelta(minutes=index),
        script_version=template,
        reply_template_id=template,
    )
    notifications = await store.guardian_notifications(guardian_user_id="guardian-a")
    return next(
        item.notification_id
        for item in notifications
        if item.crisis_event_id == crisis_event_id
    )


async def _mark(store: PostgresGuardianStore, notification_id: str, at: datetime) -> datetime:
    return await store.mark_guardian_notification_read(
        guardian_user_id="guardian-a",
        notification_id=notification_id,
        now=at,
    )


@pytest.mark.asyncio
async def test_repeat_acknowledgement_keeps_the_first_read_time(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await _link(store)
    notification_id = await _crisis(store, 0)
    first = NOW + timedelta(minutes=5)

    assert await _mark(store, notification_id, first) == first
    assert await _mark(store, notification_id, first + timedelta(hours=1)) == first

    alert = await store.guardian_notification(
        guardian_user_id="guardian-a", notification_id=notification_id
    )
    assert alert.read_at == first
    assert await store.unread_guardian_notifications(guardian_user_id="guardian-a") is None


@pytest.mark.asyncio
async def test_unread_shows_the_newest_and_counts_the_rest_exactly(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await _link(store)
    assert await store.unread_guardian_notifications(guardian_user_id="guardian-a") is None
    ids = [await _crisis(store, index) for index in range(3)]

    unread = await store.unread_guardian_notifications(guardian_user_id="guardian-a")
    assert unread is not None
    assert (unread.newest.notification_id, unread.others_count) == (ids[2], 2)

    await _mark(store, ids[2], NOW + timedelta(minutes=10))
    unread = await store.unread_guardian_notifications(guardian_user_id="guardian-a")
    assert unread is not None
    assert (unread.newest.notification_id, unread.others_count) == (ids[1], 1)

    await _mark(store, ids[0], NOW + timedelta(minutes=11))
    await _mark(store, ids[1], NOW + timedelta(minutes=12))
    assert await store.unread_guardian_notifications(guardian_user_id="guardian-a") is None

    history = await store.guardian_notifications(guardian_user_id="guardian-a")
    assert [item.notification_id for item in history] == ids[::-1]
    assert all(item.read_at is not None for item in history)


@pytest.mark.asyncio
async def test_template_id_is_parsed_once_with_a_safe_fallback(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await _link(store)
    ids = [await _crisis(store, index) for index in range(3)]

    templates = [
        (
            await store.guardian_notification(
                guardian_user_id="guardian-a", notification_id=notification_id
            )
        ).reply_template_id
        for notification_id in ids
    ]
    assert templates == ["crisis-self-v1", "victim-support-v1", None]


@pytest.mark.asyncio
async def test_another_guardian_can_neither_read_nor_acknowledge(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await _link(store)
    notification_id = await _crisis(store, 0)

    with pytest.raises(GuardianNotFoundError):
        await store.guardian_notification(
            guardian_user_id="guardian-b", notification_id=notification_id
        )
    with pytest.raises(GuardianNotFoundError):
        await store.mark_guardian_notification_read(
            guardian_user_id="guardian-b", notification_id=notification_id, now=NOW
        )
    assert await store.unread_guardian_notifications(guardian_user_id="guardian-b") is None
    unread = await store.unread_guardian_notifications(guardian_user_id="guardian-a")
    assert unread is not None and unread.newest.read_at is None


@pytest.mark.asyncio
async def test_revoked_link_is_not_found_even_with_a_receipt(
    postgres_database: TestDatabase,
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    link_id = await _link(store)
    read_id = await _crisis(store, 0)
    unread_id = await _crisis(store, 1)
    await _mark(store, read_id, NOW + timedelta(minutes=5))

    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        await admin.execute(
            "UPDATE guardian_links SET status = 'revoked', revoked_at = $2 WHERE link_id = $1",
            link_id,
            NOW + timedelta(minutes=6),
        )
    finally:
        await admin.close()

    for notification_id in (read_id, unread_id):
        with pytest.raises(GuardianNotFoundError):
            await store.guardian_notification(
                guardian_user_id="guardian-a", notification_id=notification_id
            )
        with pytest.raises(GuardianNotFoundError):
            await _mark(store, notification_id, NOW + timedelta(minutes=7))
    assert await store.unread_guardian_notifications(guardian_user_id="guardian-a") is None
    assert await store.guardian_notifications(guardian_user_id="guardian-a") == ()


@pytest.mark.asyncio
async def test_receipts_export_with_the_parent_and_leave_with_the_account(
    postgres_database: TestDatabase,
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    await _link(store)
    notification_id = await _crisis(store, 0)
    read_at = NOW + timedelta(minutes=5)
    await _mark(store, notification_id, read_at)

    parent_export = await store.export_for_account(account_id="guardian-a")
    assert parent_export["guardian_notification_reads"] == [
        {
            "notification_id": uuid.UUID(notification_id),
            "guardian_user_id": "guardian-a",
            "read_at": read_at,
        }
    ]
    child_export = await store.export_for_account(account_id="minor-a")
    assert child_export["guardian_notification_reads"] == []

    await store.delete_for_account(account_id="guardian-a")
    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        assert await admin.fetchval("SELECT count(*) FROM guardian_notification_reads") == 0
    finally:
        await admin.close()


async def _counts(database: TestDatabase) -> tuple[int, int]:
    admin = await asyncpg.connect(database.owner_dsn())
    try:
        outbox = await admin.fetchval("SELECT count(*) FROM guardian_notification_outbox")
        reads = await admin.fetchval("SELECT count(*) FROM guardian_notification_reads")
    finally:
        await admin.close()
    return int(outbox), int(reads)


@pytest.mark.asyncio
async def test_subject_and_account_deletion_remove_read_receipts(
    postgres_database: TestDatabase,
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    """Outbox deletion cascades receipts even though the delete guard is on both tables."""

    store = guardian_postgres_store
    await _link(store)
    first = await _crisis(store, 0)
    await _mark(store, first, NOW + timedelta(minutes=5))
    assert await _counts(postgres_database) == (1, 1)

    admin = await asyncpg.connect(postgres_database.owner_dsn())
    try:
        with pytest.raises(asyncpg.PostgresError, match="account deletion pipeline"):
            await admin.execute(
                "DELETE FROM guardian_notification_reads WHERE notification_id = $1",
                uuid.UUID(first),
            )
    finally:
        await admin.close()
    assert await _counts(postgres_database) == (1, 1)

    await store.delete_subject_rows(account_id="guardian-a", subject_id="minor-a")
    assert await _counts(postgres_database) == (0, 0)

    second = await _crisis(store, 1)
    await _mark(store, second, NOW + timedelta(minutes=6))
    assert await _counts(postgres_database) == (1, 1)

    await store.delete_for_account(account_id="guardian-a")
    assert await _counts(postgres_database) == (0, 0)
