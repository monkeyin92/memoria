"""Crisis push ledger and worker on the PostgreSQL guardian store.

Runs on ``guardian_postgres_store`` (production roles, RLS applies); the
crisis evidence archive stays a local SQLite ``LifeArchive``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.archive.life_archive import LifeArchive
from services.guardian.crisis import CrisisNotificationService, GuardianNotification
from services.guardian.domain import GuardianAccessDeniedError
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.push import (
    CRISIS_PUSH_TIP,
    CRISIS_PUSH_TITLE,
    CrisisPushContent,
    CrisisPushWorker,
    PushSendResult,
    crisis_push_retry_delay_s,
)

TEMPLATE = "crisis-template-01"
OPENID = "guardian-openid-a"


def _now() -> datetime:
    # Notifications are stamped with the wall clock when they are queued.
    return datetime.now(UTC).replace(microsecond=0)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class FakeSender:
    def __init__(self, *results: PushSendResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str, CrisisPushContent]] = []

    async def send_crisis_alert(
        self,
        *,
        openid: str,
        template_id: str,
        content: CrisisPushContent,
    ) -> PushSendResult:
        self.calls.append((openid, template_id, content))
        await asyncio.sleep(0)
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0] if self.results else PushSendResult("delivered")


async def _names(guardian_user_id: str, minor_user_id: str) -> str | None:
    assert guardian_user_id == "guardian-a"
    return {"minor-a": "小明"}.get(minor_user_id)


async def _store_with_crisis(
    store: PostgresGuardianStore,
    tmp_path: Path,
    *,
    occurred_at: datetime,
) -> tuple[PostgresGuardianStore, str]:
    digest = hashlib.sha256(b"binding-code").hexdigest()
    link = await store.create_link(
        guardian_user_id="guardian-a",
        minor_user_id="minor-a",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=occurred_at + timedelta(minutes=15),
        now=occurred_at,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="minor-a",
        binding_code_hash=digest,
        now=occurred_at,
    )
    receipt = await CrisisNotificationService(
        store, LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    ).record_minor_crisis(
        minor_user_id="minor-a",
        session_id="voice-session-a",
        turn_id=3,
        generation_id=1,
        tool_epoch=1,
        script_version="crisis-transfer-draft-v1",
        occurred_at=occurred_at,
    )
    assert receipt.notification_count == 1
    return store, receipt.crisis_event_id


def _worker(
    store: PostgresGuardianStore,
    sender: FakeSender,
    clock: Clock,
    **kwargs: object,
) -> CrisisPushWorker:
    return CrisisPushWorker(
        store,
        sender,
        template_id=TEMPLATE,
        display_name=_names,
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )


async def _only_notification(store: PostgresGuardianStore) -> GuardianNotification:
    notifications = await store.guardian_notifications(guardian_user_id="guardian-a")
    assert len(notifications) == 1
    return notifications[0]


async def _accept(store: PostgresGuardianStore, now: datetime, times: int = 1) -> None:
    for _ in range(times):
        await store.record_push_subscription(
            guardian_user_id="guardian-a",
            template_id=TEMPLATE,
            result="accept",
            openid=OPENID,
            now=now,
        )


async def _remaining(store: PostgresGuardianStore) -> int:
    subscription = await store.push_subscription(
        guardian_user_id="guardian-a",
        template_id=TEMPLATE,
    )
    return subscription.remaining if subscription is not None else 0


@pytest.mark.asyncio
async def test_ledger_counts_acceptances_and_bans_void_the_balance(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    now = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)

    await _accept(store, now, times=2)
    assert await _remaining(store) == 2
    rejected = await store.record_push_subscription(
        guardian_user_id="guardian-a",
        template_id=TEMPLATE,
        result="reject",
        openid=None,
        now=now,
    )
    assert (rejected.remaining, rejected.last_result) == (2, "reject")
    await _accept(store, now, times=40)
    assert await _remaining(store) == 20  # bounded ledger
    banned = await store.record_push_subscription(
        guardian_user_id="guardian-a",
        template_id=TEMPLATE,
        result="ban",
        openid=None,
        now=now,
    )
    assert (banned.remaining, banned.last_result) == (0, "ban")
    with pytest.raises(ValueError):
        await store.record_push_subscription(
            guardian_user_id="guardian-a",
            template_id=TEMPLATE,
            result="accept",
            openid=None,
            now=now,
        )
    assert await store.push_subscription(
        guardian_user_id="guardian-b",
        template_id=TEMPLATE,
    ) is None


@pytest.mark.asyncio
async def test_worker_delivers_fixed_content_and_consumes_one_acceptance(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = _now()
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    await _accept(store, occurred, times=2)
    sender = FakeSender(PushSendResult("delivered"))
    clock = Clock(occurred + timedelta(seconds=5))

    assert await _worker(store, sender, clock).run_once() == 1

    notification = await _only_notification(store)
    assert notification.status == "delivered"
    assert notification.delivered_at is not None
    assert await _remaining(store) == 1
    assert len(sender.calls) == 1
    openid, template_id, content = sender.calls[0]
    assert (openid, template_id) == (OPENID, TEMPLATE)
    assert content == CrisisPushContent(
        title=CRISIS_PUSH_TITLE,
        child_display_name="小明",
        occurred_at=occurred,
        tip=CRISIS_PUSH_TIP,
    )
    # The push payload type has no field that could carry conversation text.
    assert {field.name for field in dataclasses.fields(CrisisPushContent)} == {
        "title",
        "child_display_name",
        "occurred_at",
        "tip",
    }
    # Delivered rows are never claimed again.
    assert await _worker(store, sender, clock).run_once() == 0
    assert len(sender.calls) == 1


@pytest.mark.asyncio
async def test_guardian_without_acceptance_is_marked_no_subscription(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = _now()
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    sender = FakeSender()

    await _worker(store, sender, Clock(occurred)).run_once()

    notification = await _only_notification(store)
    assert notification.status == "no_subscription"
    assert notification.last_error_code == "no_subscription"
    assert sender.calls == []


@pytest.mark.asyncio
async def test_wechat_refusal_marks_no_subscription_and_exhausts_the_ledger(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = _now()
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    await _accept(store, occurred, times=3)
    sender = FakeSender(PushSendResult("no_subscription", "wechat_43101"))

    await _worker(store, sender, Clock(occurred)).run_once()

    notification = await _only_notification(store)
    assert notification.status == "no_subscription"
    assert notification.last_error_code == "wechat_43101"
    assert await _remaining(store) == 0


@pytest.mark.asyncio
async def test_transient_failures_back_off_refund_and_stop_at_the_attempt_cap(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = _now()
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    await _accept(store, occurred)
    sender = FakeSender(PushSendResult("retry", "wechat_-1"))
    clock = Clock(occurred)
    worker = _worker(store, sender, clock, max_attempts=3)

    assert await worker.run_once() == 1
    notification = await _only_notification(store)
    assert notification.status == "pending"
    assert notification.attempts == 1
    assert await _remaining(store) == 1  # refunded: WeChat did not deliver
    assert await worker.run_once() == 0  # still backing off
    clock.advance(crisis_push_retry_delay_s(1) - 1)
    assert await worker.run_once() == 0
    clock.advance(1)
    assert await worker.run_once() == 1
    clock.advance(crisis_push_retry_delay_s(2))
    assert await worker.run_once() == 1

    notification = await _only_notification(store)
    assert notification.status == "failed"
    assert notification.attempts == 3
    assert notification.last_error_code == "wechat_-1"
    assert len(sender.calls) == 3
    assert await _remaining(store) == 1
    clock.advance(3600)
    assert await worker.run_once() == 0
    assert crisis_push_retry_delay_s(1) == 30
    assert crisis_push_retry_delay_s(50) == 900


@pytest.mark.asyncio
async def test_permanent_failure_refunds_and_sender_exceptions_retry(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = _now()
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    await _accept(store, occurred)

    class RaisingSender(FakeSender):
        async def send_crisis_alert(self, **kwargs: object) -> PushSendResult:
            raise RuntimeError("network stack exploded")

    clock = Clock(occurred)
    await _worker(store, RaisingSender(), clock).run_once()
    notification = await _only_notification(store)
    assert notification.status == "pending"
    assert notification.last_error_code == "sender_error"
    assert await _remaining(store) == 1

    clock.advance(crisis_push_retry_delay_s(1))
    await _worker(store, FakeSender(PushSendResult("failed", "wechat_40037")), clock).run_once()
    notification = await _only_notification(store)
    assert notification.status == "failed"
    assert notification.last_error_code == "wechat_40037"
    assert await _remaining(store) == 1


@pytest.mark.asyncio
async def test_concurrent_workers_send_each_notification_once(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = _now()
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    await _accept(store, occurred, times=5)
    sender = FakeSender(PushSendResult("delivered"))
    clock = Clock(occurred)
    workers = [_worker(store, sender, clock) for _ in range(4)]

    claimed = await asyncio.gather(*(worker.run_once() for worker in workers))

    assert sum(claimed) == 1
    assert len(sender.calls) == 1
    assert await _remaining(store) == 4


@pytest.mark.asyncio
async def test_reclaim_after_a_crashed_worker_reuses_its_reservation(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = _now()
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    await _accept(store, occurred, times=2)
    clock = Clock(occurred)
    claimed = await store.claim_crisis_pushes(
        worker_id="crashed-worker",
        now=clock(),
        limit=10,
        lease_s=60,
        max_attempts=5,
        max_age_s=86_400,
    )
    assert len(claimed) == 1
    assert await store.reserve_crisis_push_subscription(
        notification_id=claimed[0].notification_id,
        worker_id="crashed-worker",
        template_id=TEMPLATE,
        now=clock(),
    ) == OPENID
    assert await _remaining(store) == 1
    # A second worker cannot reserve or settle a claim it does not hold.
    with pytest.raises(GuardianAccessDeniedError):
        await store.reserve_crisis_push_subscription(
            notification_id=claimed[0].notification_id,
            worker_id="other-worker",
            template_id=TEMPLATE,
            now=clock(),
        )
    assert await store.complete_crisis_push(
        notification_id=claimed[0].notification_id,
        worker_id="other-worker",
        outcome="delivered",
        error_code=None,
        retry_delay_s=None,
        exhaust_subscription=False,
        now=clock(),
    ) is False

    sender = FakeSender(PushSendResult("delivered"))
    live = _worker(store, sender, clock)
    assert await live.run_once() == 0  # lease still held by the crashed worker
    clock.advance(61)
    assert await live.run_once() == 1

    notification = await _only_notification(store)
    assert notification.status == "delivered"
    assert notification.attempts == 2
    assert await _remaining(store) == 1  # the crashed reservation was reused
    assert await store.complete_crisis_push(
        notification_id=claimed[0].notification_id,
        worker_id="crashed-worker",
        outcome="failed",
        error_code="late",
        retry_delay_s=None,
        exhaust_subscription=False,
        now=clock(),
    ) is False


@pytest.mark.asyncio
async def test_alerts_older_than_the_push_window_stay_on_the_page_only(
    guardian_postgres_store: PostgresGuardianStore,
    tmp_path: Path,
) -> None:
    occurred = datetime.now(UTC)
    store, _ = await _store_with_crisis(
        guardian_postgres_store, tmp_path, occurred_at=occurred
    )
    await _accept(store, occurred)
    sender = FakeSender()

    assert await _worker(store, sender, Clock(occurred + timedelta(days=2))).run_once() == 0

    notification = await _only_notification(store)
    assert notification.status == "pending"
    assert sender.calls == []


@pytest.mark.asyncio
async def test_ledger_is_exported_without_openid_and_deleted_with_the_account(
    guardian_postgres_store: PostgresGuardianStore,
) -> None:
    store = guardian_postgres_store
    accepted_at = datetime(2026, 9, 25, tzinfo=UTC)
    await _accept(store, accepted_at)

    exported = await store.export_for_account(account_id="guardian-a")
    # Driver values (datetime); governance makes the export portable.
    assert exported["guardian_push_subscriptions"] == [
        {
            "guardian_user_id": "guardian-a",
            "template_id": TEMPLATE,
            "remaining": 1,
            "last_result": "accept",
            "created_at": accepted_at,
            "updated_at": accepted_at,
            "openid_on_file": True,
        }
    ]
    assert OPENID not in repr(exported)
    assert (await store.remaining_account_rows(account_id="guardian-a")) == {
        "guardian_push_subscriptions": 1
    }
    deleted = await store.delete_for_account(account_id="guardian-a")
    assert deleted["guardian_push_subscriptions"] == 1
    assert await store.remaining_account_rows(account_id="guardian-a") == {}
