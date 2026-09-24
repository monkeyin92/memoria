"""Subscribe-message delivery for guardian crisis notifications.

WeChat one-time subscribe messages grant exactly one send per user
acceptance.  The ledger records each acceptance per guardian and template;
the worker reserves one acceptance per notification, sends a FIXED alert and
settles the reservation.  The alert never carries conversation content: the
only per-event values are the child's display name (already shown on the
guardian page) and the time of the event.

When a guardian has no remaining acceptance the notification is marked
``no_subscription`` and stays visible on the guardian page, which remains the
authoritative surface for every alert.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast

logger = logging.getLogger(__name__)

PushSubscriptionResult = Literal["accept", "reject", "ban"]
CrisisPushOutcome = Literal["delivered", "no_subscription", "failed", "retry"]

# WeChat does not cap stored one-time acceptances, but an unbounded balance is
# only ever the result of a replayed client; keep the ledger small and honest.
MAX_PUSH_SUBSCRIPTION_BALANCE = 20
# An alert older than this is not pushed (for example, alerts queued before
# delivery was switched on); it stays visible on the guardian page.
CRISIS_PUSH_MAX_AGE = timedelta(hours=24)

CRISIS_PUSH_TITLE = "安全提醒"
CRISIS_PUSH_TIP = "请打开小程序查看并尽快联系孩子"
CRISIS_PUSH_DEFAULT_CHILD_NAME = "孩子"


@dataclass(frozen=True, slots=True)
class PushSubscription:
    guardian_user_id: str
    template_id: str
    remaining: int
    last_result: PushSubscriptionResult
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PendingCrisisPush:
    notification_id: str
    crisis_event_id: str
    guardian_user_id: str
    minor_user_id: str
    occurred_at: datetime
    attempts: int


@dataclass(frozen=True, slots=True)
class CrisisPushContent:
    """The complete payload of one crisis push.

    There is deliberately no field that could hold transcript, severity or
    any other conversation-derived value.
    """

    title: str
    child_display_name: str
    occurred_at: datetime
    tip: str


@dataclass(frozen=True, slots=True)
class PushSendResult:
    outcome: CrisisPushOutcome
    error_code: str | None = None


class PushSubscriptionStorePort(Protocol):
    async def record_push_subscription(
        self,
        *,
        guardian_user_id: str,
        template_id: str,
        result: PushSubscriptionResult,
        openid: str | None,
        now: datetime,
    ) -> PushSubscription: ...

    async def push_subscription(
        self,
        *,
        guardian_user_id: str,
        template_id: str,
    ) -> PushSubscription | None: ...


class CrisisPushStorePort(Protocol):
    async def claim_crisis_pushes(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        lease_s: int,
        max_attempts: int,
        max_age_s: int,
    ) -> tuple[PendingCrisisPush, ...]: ...

    async def reserve_crisis_push_subscription(
        self,
        *,
        notification_id: str,
        worker_id: str,
        template_id: str,
        now: datetime,
    ) -> str | None: ...

    async def complete_crisis_push(
        self,
        *,
        notification_id: str,
        worker_id: str,
        outcome: CrisisPushOutcome,
        error_code: str | None,
        retry_delay_s: int | None,
        exhaust_subscription: bool,
        now: datetime,
    ) -> bool: ...


class CrisisPushSender(Protocol):
    async def send_crisis_alert(
        self,
        *,
        openid: str,
        template_id: str,
        content: CrisisPushContent,
    ) -> PushSendResult: ...


# (guardian_user_id, minor_user_id) -> display name already shown to that guardian.
DisplayNameResolver = Callable[[str, str], Awaitable[str | None]]


def validate_push_subscription_result(result: str) -> PushSubscriptionResult:
    if result not in {"accept", "reject", "ban"}:
        raise ValueError("push subscription result is invalid")
    return cast(PushSubscriptionResult, result)


def push_identifier(value: str, *, field: str) -> str:
    clean = str(value).strip()
    if not 1 <= len(clean) <= 128:
        raise ValueError(f"{field} is invalid")
    return clean


def push_template_id(value: str) -> str:
    clean = str(value).strip()
    if not 1 <= len(clean) <= 128 or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in clean
    ):
        raise ValueError("push template id is invalid")
    return clean


def push_openid(value: str) -> str:
    clean = str(value).strip()
    if not 1 <= len(clean) <= 128:
        raise ValueError("push openid is invalid")
    return clean


def validate_crisis_push_claim(
    *,
    worker_id: str,
    limit: int,
    lease_s: int,
    max_attempts: int,
    max_age_s: int,
) -> None:
    push_identifier(worker_id, field="worker_id")
    if not 1 <= limit <= 100:
        raise ValueError("crisis push claim limit must be between 1 and 100")
    if not 5 <= lease_s <= 600:
        raise ValueError("crisis push lease must be between 5 and 600 seconds")
    if not 1 <= max_attempts <= 10:
        raise ValueError("crisis push attempts must be between 1 and 10")
    if not 60 <= max_age_s <= 604_800:
        raise ValueError("crisis push max age must be between 60s and 7 days")


def validate_crisis_push_outcome(
    outcome: str,
    *,
    error_code: str | None,
    retry_delay_s: int | None,
) -> None:
    if outcome not in {"delivered", "no_subscription", "failed", "retry"}:
        raise ValueError("crisis push outcome is invalid")
    if error_code is not None and not 1 <= len(error_code) <= 96:
        raise ValueError("crisis push error code is invalid")
    if outcome == "retry" and (retry_delay_s is None or not 1 <= retry_delay_s <= 3600):
        raise ValueError("crisis push retry requires a bounded delay")


def crisis_push_content(
    *,
    occurred_at: datetime,
    child_display_name: str | None,
) -> CrisisPushContent:
    name = (child_display_name or "").strip()
    if not name or name == "朋友":
        name = CRISIS_PUSH_DEFAULT_CHILD_NAME
    return CrisisPushContent(
        title=CRISIS_PUSH_TITLE,
        child_display_name=name,
        occurred_at=occurred_at.astimezone(UTC),
        tip=CRISIS_PUSH_TIP,
    )


def crisis_push_retry_delay_s(attempts: int, *, base_s: int = 30, cap_s: int = 900) -> int:
    """Bounded exponential backoff: 30s, 60s, 120s ... capped at 15 minutes."""

    exponent = max(0, attempts - 1)
    return int(min(cap_s, base_s * (2 ** min(exponent, 16))))


class CrisisPushWorker:
    """Claims pending crisis notifications and pushes a fixed alert."""

    def __init__(
        self,
        store: CrisisPushStorePort,
        sender: CrisisPushSender,
        *,
        template_id: str,
        display_name: DisplayNameResolver,
        interval_s: float = 10.0,
        batch_size: int = 20,
        lease_s: int = 60,
        max_attempts: int = 5,
        max_age: timedelta = CRISIS_PUSH_MAX_AGE,
        worker_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not template_id.strip():
            raise ValueError("crisis push worker requires a template id")
        if interval_s <= 0:
            raise ValueError("crisis push interval must be positive")
        if not 1 <= max_attempts <= 10:
            raise ValueError("crisis push attempts must be between 1 and 10")
        self._store = store
        self._sender = sender
        self._template_id = template_id.strip()
        self._display_name = display_name
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._lease_s = lease_s
        self._max_attempts = max_attempts
        self._max_age_s = int(max_age.total_seconds())
        self._worker_id = worker_id or f"crisis-push-{uuid.uuid4().hex[:12]}"
        self._clock = clock or (lambda: datetime.now(UTC))
        self._task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="guardian-crisis-push")

    async def stop(self) -> None:
        self._closed.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._closed.is_set():
            try:
                await self.run_once()
            except Exception:
                logger.exception("guardian crisis push batch failed")
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue

    async def run_once(self) -> int:
        """Deliver one claimed batch; returns the number of claimed notifications."""

        claimed = await self._store.claim_crisis_pushes(
            worker_id=self._worker_id,
            now=self._clock(),
            limit=self._batch_size,
            lease_s=self._lease_s,
            max_attempts=self._max_attempts,
            max_age_s=self._max_age_s,
        )
        for push in claimed:
            try:
                await self._deliver(push)
            except Exception:
                # The lease expires and a later batch retries within the cap.
                logger.exception(
                    "guardian crisis push delivery failed notification_id=%s",
                    push.notification_id,
                )
        return len(claimed)

    async def _deliver(self, push: PendingCrisisPush) -> None:
        openid = await self._store.reserve_crisis_push_subscription(
            notification_id=push.notification_id,
            worker_id=self._worker_id,
            template_id=self._template_id,
            now=self._clock(),
        )
        if openid is None:
            await self._complete(
                push,
                PushSendResult("no_subscription", "no_subscription"),
                exhaust_subscription=False,
            )
            return
        try:
            name = await self._display_name(push.guardian_user_id, push.minor_user_id)
        except Exception:
            name = None
        content = crisis_push_content(occurred_at=push.occurred_at, child_display_name=name)
        try:
            result = await self._sender.send_crisis_alert(
                openid=openid,
                template_id=self._template_id,
                content=content,
            )
        except Exception:
            logger.exception(
                "guardian crisis push sender raised notification_id=%s",
                push.notification_id,
            )
            result = PushSendResult("retry", "sender_error")
        # WeChat refused the send (e.g. errcode 43101): the ledger balance is
        # not real, so it is exhausted rather than refunded.
        await self._complete(
            push,
            result,
            exhaust_subscription=result.outcome == "no_subscription",
        )

    async def _complete(
        self,
        push: PendingCrisisPush,
        result: PushSendResult,
        *,
        exhaust_subscription: bool,
    ) -> None:
        outcome = result.outcome
        retry_delay_s: int | None = None
        if outcome == "retry":
            if push.attempts >= self._max_attempts:
                outcome = "failed"
            else:
                retry_delay_s = crisis_push_retry_delay_s(push.attempts)
        settled = await self._store.complete_crisis_push(
            notification_id=push.notification_id,
            worker_id=self._worker_id,
            outcome=outcome,
            error_code=result.error_code,
            retry_delay_s=retry_delay_s,
            exhaust_subscription=exhaust_subscription,
            now=self._clock(),
        )
        logger.info(
            "guardian crisis push notification_id=%s outcome=%s error_code=%s "
            "attempt=%s settled=%s",
            push.notification_id,
            outcome,
            result.error_code or "-",
            push.attempts,
            settled,
        )


__all__ = [
    "CRISIS_PUSH_DEFAULT_CHILD_NAME",
    "CRISIS_PUSH_MAX_AGE",
    "CRISIS_PUSH_TIP",
    "CRISIS_PUSH_TITLE",
    "MAX_PUSH_SUBSCRIPTION_BALANCE",
    "CrisisPushContent",
    "CrisisPushOutcome",
    "CrisisPushSender",
    "CrisisPushStorePort",
    "CrisisPushWorker",
    "DisplayNameResolver",
    "PendingCrisisPush",
    "PushSendResult",
    "PushSubscription",
    "PushSubscriptionResult",
    "PushSubscriptionStorePort",
    "crisis_push_content",
    "crisis_push_retry_delay_s",
    "push_identifier",
    "push_openid",
    "push_template_id",
    "validate_crisis_push_claim",
    "validate_crisis_push_outcome",
    "validate_push_subscription_result",
]
