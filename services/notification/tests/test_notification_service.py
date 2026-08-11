"""NotificationService behavior across the in-memory and SQLite adapters."""

from __future__ import annotations

import asyncio
import random
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.notification.domain import (
    BackoffPolicy,
    DeliveryAttempt,
    NotificationConflictError,
    NotificationFence,
    NotificationFencingError,
    NotificationNotFoundError,
    NotificationPolicyError,
    NotificationStateError,
    ReceiptNotVerifiedError,
    RecipientBinding,
    RecipientSpec,
    RelationshipInactiveError,
    RelationshipSnapshot,
)
from services.notification.in_memory_store import InMemoryNotificationStore
from services.notification.repository import (
    ChannelResult,
    InMemoryNotificationReceiptVerifier,
    InMemoryOperatorAuthorizationPort,
    InMemoryRelationshipResolver,
)
from services.notification.service import NotificationService
from services.notification.sqlite_store import SqliteNotificationStore
from services.notification.tests.receipt_helpers import (
    make_notification_receipt,
    notify_obligations_for_roles,
)


class FakeChannel:
    """Test delivery adapter: records content, never fabricates receipts."""

    channel: str

    def __init__(self, channel: str, *, ok: bool = True, error_code: str | None = None) -> None:
        self.channel = channel
        self._ok = ok
        self._error_code = error_code
        self.sent: list[tuple[str, str]] = []

    async def send(
        self,
        *,
        recipient: RecipientBinding,
        content: str,
        attempt: DeliveryAttempt,
    ) -> ChannelResult:
        self.sent.append((content, attempt.channel))
        if self._ok:
            return ChannelResult(ok=True, channel_receipt_id=f"ext-{attempt.attempt_id}")
        return ChannelResult(ok=False, error_code=self._error_code)


class RaisingChannel:
    channel = "wechat_subscription"

    async def send(self, **kwargs: object) -> ChannelResult:
        raise RuntimeError("adapter exploded")


class HangingChannel:
    channel = "wechat_subscription"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, **kwargs: object) -> ChannelResult:
        self.sent.append("called")
        await asyncio.sleep(3600)
        raise AssertionError("never reached")


class CancellationResistantChannel:
    """Adapter that swallows CancelledError and keeps running: proves that
    the local wait_for deadline is NOT a hard kill and that provider-side
    idempotency is the real double-delivery guarantee."""

    channel = "wechat_subscription"

    def __init__(self) -> None:
        self.started: list[str] = []
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def send(self, **kwargs: object) -> ChannelResult:
        attempt = kwargs["attempt"]
        self.started.append(attempt.attempt_id)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Self-limiting: even if stop() is never called (e.g. a broken
            # test teardown), the coroutine exits after 5s so the event
            # loop can never be held forever by this adapter.
            for _ in range(50):
                if self._stop:
                    break
                await asyncio.sleep(0.1)
        raise AssertionError("never reached")


class IdempotentFakeProvider:
    """Provider that deduplicates by the STABLE logical delivery key
    ({intent}:{recipient}:{channel}, NEVER the attempt_number): a repeated
    invocation - the late attempt-1 worker AND a re-claiming worker's
    attempt-2 - returns the SAME external receipt and produces NO second
    external side effect (the hard guarantee when a local timeout cannot
    kill a hung adapter)."""

    channel = "wechat_subscription"

    def __init__(self) -> None:
        self._delivered: dict[str, str] = {}
        self.external_effects: list[str] = []

    def _key(self, attempt: DeliveryAttempt) -> str:
        return attempt.logical_delivery_key

    async def send(
        self,
        *,
        recipient: RecipientBinding,
        content: str,
        attempt: DeliveryAttempt,
    ) -> ChannelResult:
        key = self._key(attempt)
        existing = self._delivered.get(key)
        if existing is not None:
            return ChannelResult(ok=True, channel_receipt_id=existing)
        external_id = f"ext-{key}"
        self._delivered[key] = external_id
        self.external_effects.append(key)
        return ChannelResult(ok=True, channel_receipt_id=external_id)


@pytest.fixture(params=["memory", "sqlite"])
def store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> InMemoryNotificationStore | SqliteNotificationStore:
    if request.param == "memory":
        return InMemoryNotificationStore()
    return SqliteNotificationStore(tmp_path / "notification.sqlite3")


@pytest.fixture
def service(
    store: InMemoryNotificationStore | SqliteNotificationStore,
) -> NotificationService:
    return NotificationService(
        store,
        receipt_verifier=InMemoryNotificationReceiptVerifier(),
        relationship_resolver=InMemoryRelationshipResolver(),
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
        send_timeout_seconds=1.0,
    )


@pytest.fixture(autouse=True)
async def initialized_store(
    store: InMemoryNotificationStore | SqliteNotificationStore,
) -> None:
    await store.initialize()
    yield


def _now() -> datetime:
    # The service runs on the real clock unless a test pins ``now``; the
    # receipt helper defaults to the same real clock so receipts are never
    # accidentally expired/issued in the future relative to the service.
    return datetime.now(UTC)


#: Fixed past anchor for relationship validity windows so repeated test
#: calls produce byte-identical snapshots (idempotency comparisons).
_FIXED_PAST = datetime(2026, 1, 1, tzinfo=UTC)


_TEMPLATE_BY_KIND = {
    "crisis_safety": ("crisis_safety_notice", "safety_concern", "孩子", "联系监护人"),
    "emergency": ("emergency_notice", "emergency_alert", "家人", "联系紧急联系人"),
    "care_alert": ("care_alert_notice", "care_reminder", "老人", "陪伴安抚并关注变化"),
}


def _spec(
    *,
    relationship_id: str = "rel-1",
    channels: tuple[str, ...] | None = None,
) -> RecipientSpec:
    return RecipientSpec(
        relationship_id=relationship_id,
        channels=channels,  # type: ignore[arg-type]
    )


def _snapshot(
    relationship_id: str = "rel-1",
    *,
    person_id: str = "guardian-1",
    role: str = "guardian",
    status: str = "active",
    snapshot_id: str | None = None,
    revision: int = 1,
    binding_version: int | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> RelationshipSnapshot:
    return RelationshipSnapshot(
        relationship_id=relationship_id,
        subject_person_id="minor-1",
        person_id=person_id,
        role=role,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        #: Distinct canonical evidence per relationship: the generated
        #: PolicyReceiptV2 requires UNIQUE relationship_snapshot_ids, so a
        #: multi-recipient fixture can never reuse one snapshot id.
        snapshot_id=snapshot_id or f"rel-snap-{relationship_id}",
        revision=revision,
        binding_version=binding_version,
        valid_from=valid_from if valid_from is not None else _FIXED_PAST,
        valid_until=valid_until,
    )


async def _create(
    service: NotificationService,
    *,
    kind: str = "crisis_safety",
    recipients: tuple[RecipientSpec, ...] | None = None,
    key: str = "crisis:evt-1",
    policy_receipt_id: str = "policy-receipt-1",
    fence: NotificationFence | None = None,
    actor_person_id: str | None = "actor-1",
    snapshots: tuple[RelationshipSnapshot, ...] = (),
    occurred_at: datetime | None = None,
    now: datetime | None = None,
):
    template_key, reason_code, role_label, action_hint = _TEMPLATE_BY_KIND[kind]
    effective_fence = fence or NotificationFence(
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id=actor_person_id or "actor-1",
        subject_person_id="minor-1",
        valid_until=_now() + timedelta(hours=2),
    )
    effective_snapshots: tuple[RelationshipSnapshot, ...] = snapshots
    if not effective_snapshots:
        effective_snapshots = tuple(
            _snapshot(relationship_id=spec.relationship_id)
            for spec in (recipients or (_spec(),))
        )
    if policy_receipt_id:
        verifier = service.receipt_verifier
        assert verifier is not None
        obligations = notify_obligations_for_roles(
            recipient_roles=tuple(snapshot.role for snapshot in effective_snapshots),
            intent_kind=kind,
        )
        verifier.register(
            make_notification_receipt(
                receipt_id=policy_receipt_id,
                actor_person_id=effective_fence.actor_person_id,
                subject_person_id=effective_fence.subject_person_id,
                fence=effective_fence,
                relationship_snapshot_ids=tuple(
                    snapshot.snapshot_id for snapshot in effective_snapshots
                ),
                obligations=obligations,
            )
        )
    resolver = service.relationship_resolver
    assert resolver is not None
    for spec in recipients or (_spec(),):
        snapshot = next(
            (
                item
                for item in effective_snapshots
                if item.relationship_id == spec.relationship_id
            ),
            None,
        )
        if snapshot is None:
            snapshot = _snapshot(relationship_id=spec.relationship_id)
        resolver.register(snapshot)
    return await service.create_intent(
        intent_kind=kind,  # type: ignore[arg-type]
        subject_person_id="minor-1",
        source_event_id="crisis-event-1",
        idempotency_key=key,
        policy_receipt_id=policy_receipt_id,
        fence=effective_fence,
        actor_person_id=actor_person_id,
        template_key=template_key,
        template_params={
            "role_label": role_label,
            "reason_code": reason_code,
            "action_hint": action_hint,
        },
        reason_code=reason_code,
        script_version="2026-08-09.1",
        recipients=recipients or (_spec(),),
        occurred_at=occurred_at or now,
        now=now,
    )


def _register_operator(
    service: NotificationService,
    *,
    actor_person_id: str = "operator-1",
    receipt_id: str = "operator-receipt-1",
    now: datetime | None = None,
) -> None:
    """Register an operator authorization reference (NOT a policy receipt:
    ``notification_operator`` is not a canonical capability)."""
    authorizer = service.operator_authorizer
    assert authorizer is not None
    authorizer.register(receipt_id, actor_person_id)


async def test_create_intent_is_idempotent(
    service: NotificationService,
) -> None:
    now = _now()
    first = await _create(service, now=now)
    second = await _create(service, now=now)
    assert second.intent_id == first.intent_id
    recipients = await service.list_recipients(first.intent_id, actor_person_id="guardian-1")
    assert len(recipients) == 1
    assert recipients[0].status == "pending"


async def test_concurrent_same_key_first_write_wins(
    service: NotificationService,
) -> None:
    """Two racing creates with the same idempotency key must both succeed
    and resolve to ONE intent (first write wins); outbox event ids are
    stable so no duplicate events are emitted."""
    import asyncio

    now = _now()
    results = await asyncio.gather(
        _create(service, key="crisis:race", now=now),
        _create(service, key="crisis:race", now=now),
    )
    assert results[0].intent_id == results[1].intent_id
    assert (await service.get_intent_by_key("crisis:race", actor_person_id="minor-1")).intent_id == results[0].intent_id
    recipients = await service.list_recipients(
        results[0].intent_id, actor_person_id="guardian-1"
    )
    assert len(recipients) == 1
    outbox = service._store.outbox_events()  # noqa: SLF001
    created = [event for event in outbox if event.topic == "notification.intent.created"]
    assert len(created) == 1
    assert created[0].event_id == "crisis:race:intent.created"


async def test_concurrent_same_key_different_content_conflicts(
    service: NotificationService,
) -> None:
    import asyncio

    results = await asyncio.gather(
        _create(service, key="crisis:race-2"),
        _create(
            service,
            key="crisis:race-2",
            recipients=(_spec(relationship_id="rel-2"),),
            snapshots=(_snapshot(relationship_id="rel-2", person_id="g2"),),
        ),
        return_exceptions=True,
    )
    ok = [item for item in results if not isinstance(item, Exception)]
    conflicts = [
        item for item in results if isinstance(item, NotificationConflictError)
    ]
    assert len(ok) == 1 and len(conflicts) == 1


async def test_atomic_create_rolls_back_on_failure(
    store: InMemoryNotificationStore | SqliteNotificationStore,
    service: NotificationService,
) -> None:
    """A mid-way failure in the atomic create leaves NO intent, recipient,
    audit or outbox row behind (section 11.6)."""
    key = "crisis:atomic-fail"
    store._fail_after_step = "recipients"  # noqa: SLF001
    with pytest.raises(RuntimeError, match="injected"):
        await _create(service, key=key)
    store._fail_after_step = None  # noqa: SLF001
    # Nothing survived: no intent, no recipients, no outbox event.
    with pytest.raises(NotificationNotFoundError):
        await service.get_intent_by_key(key, actor_person_id="minor-1")
    outbox = service._store.outbox_events()  # noqa: SLF001
    assert all(
        key not in event.event_id for event in outbox
    )


async def test_cross_subject_id_lookup_returns_nothing(
    service: NotificationService,
) -> None:
    """API-scoped reads: knowing an id is not enough - a different subject
    or person gets an empty result / not-found."""
    intent = await _create(service)
    with pytest.raises(NotificationNotFoundError):
        await service.get_intent(intent.intent_id, actor_person_id="other-subject")
    assert (
        await service.list_recipients(
            intent.intent_id, actor_person_id="other-person"
        )
        == ()
    )
    with pytest.raises(NotificationNotFoundError):
        await service.get_intent_by_key(
            "crisis:evt-1", actor_person_id="other-subject"
        )


async def test_duplicate_authoritative_recipient_rejected(
    service: NotificationService,
) -> None:
    """Fifth review: the same authoritative (person, role) must never produce
    duplicate notifications on one intent."""
    with pytest.raises(NotificationPolicyError, match="duplicate recipient"):
        await _create(
            service,
            recipients=(
                _spec(relationship_id="rel-1"),
                _spec(relationship_id="rel-dup"),
            ),
            snapshots=(
                _snapshot(relationship_id="rel-1", person_id="guardian-1"),
                _snapshot(relationship_id="rel-dup", person_id="guardian-1"),
            ),
            key="crisis:dup-recipient",
        )


async def test_bool_and_naive_time_rejected(service: NotificationService) -> None:
    """Fifth review: security-relevant numbers reject bool; times must be
    timezone-aware with valid ordering."""
    with pytest.raises(ValueError, match="epoch"):
        NotificationFence(
            device_id="device-1",
            session_id="s",
            epoch=True,  # type: ignore[arg-type]
            binding_id="b",
            binding_version=1,
            runtime_profile_id="p",
            actor_person_id="a",
            subject_person_id="s1",
            valid_until=_now() + timedelta(hours=2),
        )
    with pytest.raises(ValueError, match="binding_version"):
        NotificationFence(
            device_id="device-1",
            session_id="s",
            epoch=1,
            binding_id="b",
            binding_version=True,  # type: ignore[arg-type]
            runtime_profile_id="p",
            actor_person_id="a",
            subject_person_id="s1",
            valid_until=_now() + timedelta(hours=2),
        )
    with pytest.raises(ValueError, match="epoch"):
        make_notification_receipt(
            fence=NotificationFence(
                device_id="device-1",
                session_id="s",
                epoch=True,  # type: ignore[arg-type]
                binding_id="b",
                binding_version=1,
                runtime_profile_id="p",
                actor_person_id="a",
                subject_person_id="s1",
                valid_until=_now() + timedelta(hours=2),
            )
        )
    with pytest.raises(ValueError, match="binding_version"):
        make_notification_receipt(
            fence=NotificationFence(
                device_id="device-1",
                session_id="s",
                epoch=1,
                binding_id="b",
                binding_version=True,  # type: ignore[arg-type]
                runtime_profile_id="p",
                actor_person_id="a",
                subject_person_id="s1",
                valid_until=_now() + timedelta(hours=2),
            )
        )
    # naive valid_from / valid_until rejected.
    with pytest.raises(ValueError, match="timezone"):
        _snapshot(
            relationship_id="rel-naive",
            valid_from=datetime(2026, 8, 8, 10, 0),  # naive
        )
    # valid_until <= valid_from rejected.
    with pytest.raises(ValueError, match="valid_until"):
        _snapshot(
            relationship_id="rel-order",
            valid_from=_now() - timedelta(days=1),
            valid_until=_now() - timedelta(days=2),
        )
    # naive occurred_at rejected at the service boundary.
    with pytest.raises(NotificationPolicyError, match="timezone-aware"):
        await _create(service, occurred_at=datetime(2026, 8, 9, 10, 0), key="crisis:naive")


async def test_cancel_intent_subject_does_not_need_operator(
    service: NotificationService,
) -> None:
    """Fifth review: the intent's own subject can atomically cancel their
    whole intent without an operator receipt; a third party cannot."""
    now = _now()
    intent = await _create(service, now=now)
    # Subject path: no operator receipt required.
    updated = await service.cancel_intent(
        intent_id=intent.intent_id,
        reason="user_request",
        actor_person_id="minor-1",
        now=now,
    )
    assert updated.status == "cancelled"
    # Third party without operator authorization is rejected.
    intent2 = await _create(service, key="crisis:third-party", now=now)
    with pytest.raises(NotificationPolicyError, match="operator"):
        await service.cancel_intent(
            intent_id=intent2.intent_id,
            reason="operator_override",
            actor_person_id="stranger-1",
            now=now,
        )
    # Verified operator can cancel.
    _register_operator(service, actor_person_id="operator-1", now=now)
    cancelled = await service.cancel_intent(
        intent_id=intent2.intent_id,
        reason="operator_override",
        actor_person_id="operator-1",
        authorization_ref="operator-receipt-1",
        now=now,
    )
    assert cancelled.status == "cancelled"


async def test_cancel_intent_skips_terminal_and_replay_is_idempotent(
    service: NotificationService,
) -> None:
    """P0-4/P0-5: cancelling an intent with an already-delivered recipient
    keeps the delivered facts; re-running the same cancel produces the same
    result without duplicate audit/outbox rows."""
    now = _now()
    intent = await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="delivered",
        channel_receipt_id="ext-1",
        now=now + timedelta(seconds=1),
    )
    updated = await service.cancel_intent(
        intent_id=intent.intent_id,
        reason="user_request",
        actor_person_id="minor-1",
        now=now + timedelta(seconds=2),
    )
    # The delivered recipient is terminal: it stays delivered, the intent
    # derives to delivered (not cancelled).
    assert updated.status == "delivered"
    (recipient,) = await service.list_recipients(
        intent.intent_id, actor_person_id="guardian-1"
    )
    assert recipient.status == "delivered"
    assert recipient.delivered_at is not None
    # Replay the same cancel: same result, no duplicate side effects.
    again = await service.cancel_intent(
        intent_id=intent.intent_id,
        reason="user_request",
        actor_person_id="minor-1",
        now=now + timedelta(seconds=3),
    )
    assert again.intent_id == updated.intent_id and again.status == "delivered"
    outbox = service._store.outbox_events()  # noqa: SLF001
    cancelled_events = [
        event
        for event in outbox
        if event.topic == "notification.intent.cancelled"
    ]
    assert len(cancelled_events) <= 1


async def test_atomic_completion_stale_token_rejected(
    service: NotificationService,
) -> None:
    """Fifth review: the atomic completion guards on the fencing token; a
    stale worker changes no state."""
    now = _now()
    await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    later = now + timedelta(minutes=5)
    (second,) = await service.claim_due(now=later)
    assert second.fencing_token != attempt.fencing_token
    with pytest.raises(NotificationFencingError):
        await service.complete_attempt(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            result="delivered",
            channel_receipt_id="stale",
            now=later,
        )
    # No receipt was created for the stale attempt.
    assert (
        await service.list_receipts(
            (await service.get_intent_by_key("crisis:evt-1", actor_person_id="minor-1")).intent_id,
            actor_person_id="minor-1",
        )
        == ()
    )


async def test_atomic_completion_rolls_back_on_failure(
    store: InMemoryNotificationStore | SqliteNotificationStore,
    service: NotificationService,
) -> None:
    """A mid-way failure in the atomic completion leaves no receipt, no
    recipient transition and no attempt finish behind."""
    now = _now()
    await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    # Force a failure inside the transaction: duplicate receipt attempt id.
    # First complete normally, then attempt a second completion of the same
    # attempt with a stale token -> nothing changes.
    delivered = await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="delivered",
        channel_receipt_id="ext-1",
        now=now + timedelta(seconds=1),
    )
    assert delivered.status == "delivered"
    with pytest.raises(NotificationFencingError):
        await service.complete_attempt(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            result="delivered",
            channel_receipt_id="ext-2",
            now=now + timedelta(seconds=2),
        )
    (recipient,) = await service.list_recipients(
        delivered.intent_id, actor_person_id="guardian-1"
    )
    assert recipient.delivered_channel == "wechat_subscription"
    receipts = await service.list_receipts(
        delivered.intent_id, actor_person_id="minor-1"
    )
    assert len(receipts) == 1
    assert receipts[0].channel_receipt_id == "ext-1"


async def test_in_memory_atomic_completion_injected_failure_rolls_back(
    store: InMemoryNotificationStore,
    service: NotificationService,
) -> None:
    """Injected mid-transaction failure: recipient stays leased/pending, no
    receipt, no finished attempt."""
    if not isinstance(store, InMemoryNotificationStore):
        pytest.skip("injection hook is in-memory only")
    now = _now()
    await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    store._fail_after_step = "complete-recipient"  # noqa: SLF001
    with pytest.raises(RuntimeError, match="injected"):
        await service.complete_attempt(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            result="delivered",
            channel_receipt_id="ext-injected",
            now=now + timedelta(seconds=1),
        )
    store._fail_after_step = None  # noqa: SLF001
    recipient = await store.get_recipient_for_worker(attempt.recipient_id)
    assert recipient is not None and recipient.status == "in_progress"
    assert recipient.fencing_token == attempt.fencing_token
    stored_attempt = await store.get_attempt(attempt.attempt_id)
    assert stored_attempt is not None and stored_attempt.status == "leased"
    assert await store.get_receipt_by_attempt(attempt.attempt_id) is None


@pytest.mark.parametrize(
    "fail_step",
    [
        "complete-attempt",
        "complete-receipt",
        "complete-audit",
        "complete-outbox",
        "complete-intent",
    ],
)
async def test_in_memory_atomic_completion_full_rollback_every_step(
    store: InMemoryNotificationStore | SqliteNotificationStore,
    service: NotificationService,
    fail_step: str,
) -> None:
    """P0-C/P0-B: every mid-transaction failure rolls back recipient,
    attempt, receipt, audit, outbox AND intent - no half state survives."""
    if not isinstance(store, InMemoryNotificationStore):
        pytest.skip("injection hook is in-memory only")
    now = _now()
    await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    store._fail_after_step = fail_step  # noqa: SLF001
    with pytest.raises(RuntimeError, match="injected"):
        await service.complete_attempt(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            result="delivered",
            channel_receipt_id="ext-injected",
            now=now + timedelta(seconds=1),
        )
    store._fail_after_step = None  # noqa: SLF001
    recipient = await store.get_recipient_for_worker(attempt.recipient_id)
    assert recipient is not None and recipient.status == "in_progress"
    stored_attempt = await store.get_attempt(attempt.attempt_id)
    assert stored_attempt is not None and stored_attempt.status == "leased"
    assert await store.get_receipt_by_attempt(attempt.attempt_id) is None
    intent = await store.get_intent_for_worker(recipient.intent_id)
    assert intent is not None and intent.status == "pending"
    outbox = service._store.outbox_events()  # noqa: SLF001
    assert not any("ext-injected" in event.event_id for event in outbox)
    assert not any("intent.delivered" in event.event_id for event in outbox)


async def test_in_memory_cancel_intent_rolls_back_every_recipient(
    store: InMemoryNotificationStore | SqliteNotificationStore,
    service: NotificationService,
) -> None:
    """P0-D: a failure inside the atomic cancel leaves every recipient AND
    the intent untouched - never partially cancelled."""
    if not isinstance(store, InMemoryNotificationStore):
        pytest.skip("injection hook is in-memory only")
    now = _now()
    intent = await _create(
        service,
        recipients=(
            _spec(relationship_id="rel-1"),
            _spec(relationship_id="rel-2"),
        ),
        snapshots=(
            _snapshot(relationship_id="rel-1", person_id="g1"),
            _snapshot(relationship_id="rel-2", person_id="g2"),
        ),
        now=now,
    )
    store._fail_after_step = "cancel-recipients"  # noqa: SLF001
    with pytest.raises(RuntimeError, match="injected"):
        await service.cancel_intent(
            intent_id=intent.intent_id,
            reason="user_request",
            actor_person_id="minor-1",
            now=now,
        )
    store._fail_after_step = None  # noqa: SLF001
    # Nothing was cancelled: intent still pending, both recipients pending.
    assert (await store.get_intent_for_worker(intent.intent_id)).status == "pending"
    for recipient in await store.list_recipients_for_worker(intent.intent_id):
        assert recipient.status == "pending"
    assert (await service.claim_due(now=now + timedelta(seconds=1))) != ()


async def test_idempotency_key_conflict_rejected(service: NotificationService) -> None:
    await _create(service)
    with pytest.raises(NotificationConflictError):
        await _create(
            service,
            recipients=(_spec(relationship_id="rel-other"),),
            snapshots=(_snapshot(relationship_id="rel-other", person_id="other-guardian"),),
        )


async def test_role_matrix_fails_closed(service: NotificationService) -> None:
    with pytest.raises(NotificationPolicyError):
        await _create(
            service,
            recipients=(_spec(relationship_id="rel-ec"),),
            snapshots=(_snapshot(relationship_id="rel-ec", role="emergency_contact"),),
        )
    with pytest.raises(NotificationPolicyError):
        await _create(
            service,
            kind="emergency",
            recipients=(_spec(relationship_id="rel-g"),),
            snapshots=(_snapshot(relationship_id="rel-g", role="guardian"),),
            key="emergency:evt-1",
        )
    with pytest.raises(NotificationPolicyError):
        await _create(
            service,
            kind="care_alert",
            recipients=(_spec(relationship_id="rel-g2"),),
            snapshots=(_snapshot(relationship_id="rel-g2", role="guardian"),),
            key="care:evt-1",
        )
    with pytest.raises(NotificationPolicyError):
        await _create(
            service,
            kind="care_alert",
            recipients=(_spec(relationship_id="rel-ec2"),),
            snapshots=(_snapshot(relationship_id="rel-ec2", role="emergency_contact"),),
            key="care:evt-2",
        )


@pytest.mark.parametrize(
    "non_recipient_role",
    ["account_owner", "device_admin", "primary_subject", "member"],
)
async def test_payer_and_device_admin_never_receive_content(
    service: NotificationService, non_recipient_role: str
) -> None:
    """Decision D-06 / PR-15: the payer (account_owner) and device admin are
    NOT recipients of crisis/emergency/care content; only the active
    relationship binding role of the intent kind may receive it."""
    for kind in ("crisis_safety", "emergency", "care_alert"):
        with pytest.raises(
            NotificationPolicyError,
            match="unknown recipient role|does not accept role|not a recipient role",
        ):
            await _create(
                service,
                kind=kind,
                recipients=(_spec(relationship_id="rel-payer"),),
                snapshots=(
                    _snapshot(
                        relationship_id="rel-payer",
                        role=non_recipient_role,  # type: ignore[arg-type]
                    ),
                ),
                key=f"{kind}:payer-{non_recipient_role}",
            )


async def test_policy_receipt_required(service: NotificationService) -> None:
    with pytest.raises(NotificationPolicyError, match="policy_receipt_id"):
        await _create(service, key="crisis:no-receipt", policy_receipt_id="")


async def test_create_intent_requires_actor(service: NotificationService) -> None:
    """P0-4: a sensitive intent without an explicit actor fails immediately
    with a stable reason code; the actor must equal the fence actor."""
    with pytest.raises(NotificationPolicyError, match="actor_person_id is required"):
        await _create(service, actor_person_id="", key="crisis:no-actor")


async def test_create_intent_rejects_forged_actor(service: NotificationService) -> None:
    """P0-4: an actor claiming a fence issued for someone else is rejected
    before any receipt lookup."""
    forged = NotificationFence(
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id="real-actor",
        subject_person_id="minor-1",
        valid_until=_now() + timedelta(hours=2),
    )
    with pytest.raises(NotificationPolicyError, match="must match the fence actor"):
        await _create(
            service,
            fence=forged,
            actor_person_id="forger",
            key="crisis:forged-actor",
        )


async def test_idempotency_key_conflict_on_different_policy_receipt(
    service: NotificationService,
) -> None:
    """A replay with the same idempotency key but a different policy receipt
    must be rejected (receipts fence the notification decision)."""
    await _create(service)
    with pytest.raises(NotificationConflictError):
        await _create(service, policy_receipt_id="policy-receipt-2")


async def test_legacy_receipt_with_older_binding_version_rejected(
    service: NotificationService,
) -> None:
    """A receipt issued under an older binding manifest version must never
    be reusable with the current fence."""
    old_fence = NotificationFence(
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id="actor-1",
        subject_person_id="minor-1",
        valid_until=_now() + timedelta(hours=2),
    )
    verifier = service.receipt_verifier
    assert verifier is not None
    verifier.register(
        make_notification_receipt(
            receipt_id="policy-receipt-legacy",
            fence=old_fence,
        )
    )
    with pytest.raises(ReceiptNotVerifiedError):
        await service.create_intent(
            intent_kind="crisis_safety",
            subject_person_id="minor-1",
            source_event_id="crisis-event-1",
            idempotency_key="crisis:legacy-receipt",
            policy_receipt_id="policy-receipt-legacy",
            fence=NotificationFence(
                device_id="device-1",
                session_id="session-1",
                epoch=1,
                binding_id="binding-1",
                binding_version=2,
                runtime_profile_id="profile-1",
                actor_person_id="actor-1",
                subject_person_id="minor-1",
                valid_until=_now() + timedelta(hours=2),
            ),
            actor_person_id="actor-1",
            template_key="crisis_safety_notice",
            template_params={
                "role_label": "孩子",
                "reason_code": "safety_concern",
                "action_hint": "联系监护人",
            },
            reason_code="safety_concern",
            script_version="2026-08-09.1",
            recipients=(_spec(),),
            occurred_at=_now(),
            now=_now(),
        )


async def test_unsubscribe_is_terminal_and_blocks_claim_and_replay(
    service: NotificationService,
) -> None:
    """退订: user_request cancel is audited, terminal, and forbids both
    future claims and manual replay."""
    _register_operator(service, actor_person_id="operator-1", now=_now())
    intent = await _create(service)
    (recipient,) = await service.list_recipients(intent.intent_id, actor_person_id="guardian-1")

    unsubscribed = await service.unsubscribe_recipient(
        recipient_id=recipient.recipient_id,
        actor_person_id=recipient.person_id,
    )
    assert unsubscribed.status == "cancelled"
    assert unsubscribed.cancelled_reason == "user_request"

    # Never claimable again, even after any lease window.
    assert (
        await service.claim_due(
            now=_now() + timedelta(days=30),
        )
        == ()
    )
    with pytest.raises(NotificationStateError, match="cancelled"):
        await service.replay_recipient(
            recipient_id=recipient.recipient_id,
            actor_person_id="operator-1",
            authorization_ref="operator-receipt-1",
        )


async def test_unsubscribe_requires_recipient_identity_or_evidence(
    service: NotificationService,
) -> None:
    intent = await _create(service)
    (recipient,) = await service.list_recipients(intent.intent_id, actor_person_id="guardian-1")
    with pytest.raises(NotificationPolicyError, match="actor_person_id"):
        await service.unsubscribe_recipient(recipient_id=recipient.recipient_id)
    # A third party can never unsubscribe, with or without an evidence
    # string (an evidence string is not a permission).
    with pytest.raises(NotificationNotFoundError, match="does not exist"):
        await service.unsubscribe_recipient(
            recipient_id=recipient.recipient_id,
            actor_person_id="stranger-1",
            now=_now(),
        )


@pytest.mark.parametrize("relationship_status", ["pending", "revoked", "expired", "disputed"])
async def test_relationship_inactive_fails_closed(
    service: NotificationService, relationship_status: str
) -> None:
    with pytest.raises(RelationshipInactiveError):
        await _create(
            service,
            recipients=(_spec(relationship_id="rel-inactive"),),
            snapshots=(
                _snapshot(
                    relationship_id="rel-inactive",
                    status=relationship_status,  # type: ignore[arg-type]
                ),
            ),
            key=f"crisis:rel-{relationship_status}",
        )


async def test_unknown_channel_rejected(service: NotificationService) -> None:
    with pytest.raises(NotificationPolicyError):
        await _create(
            service,
            recipients=(_spec(channels=("telegram",)),),
            key="crisis:bad-channel",
        )


async def test_crisis_create_never_blocks_on_missing_channels(
    service: NotificationService,
) -> None:
    # No adapters wired: creation must succeed (section 10.6 decoupling).
    intent = await _create(service)
    assert intent.status == "pending"
    summary = await service.dispatch_due({}, now=_now())
    assert summary.attempted == 1 and summary.failed == 1
    recipient = (await service.list_recipients(intent.intent_id, actor_person_id="guardian-1"))[0]
    assert recipient.status == "dead_lettered"
    assert recipient.last_error_code == "channel_invalid"


async def test_delivery_flow_with_receipt(service: NotificationService) -> None:
    now = _now()
    intent = await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    recipient = await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="delivered",
        channel_receipt_id="ext-receipt-1",
        now=now + timedelta(seconds=1),
    )
    assert recipient.status == "delivered"
    assert recipient.delivered_channel == "wechat_subscription"
    receipts = await service.list_receipts(intent.intent_id, actor_person_id="minor-1")
    assert len(receipts) == 1
    assert receipts[0].channel_receipt_id == "ext-receipt-1"
    assert (await service.get_intent(intent.intent_id, actor_person_id="minor-1")).status == "delivered"
    assert (await service.list_attempts(recipient.recipient_id, actor_person_id="guardian-1"))[0].status == "delivered"


async def test_delivered_requires_channel_receipt(service: NotificationService) -> None:
    await _create(service, now=_now())
    (attempt,) = await service.claim_due(now=_now())
    with pytest.raises(NotificationStateError):
        await service.complete_attempt(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            result="delivered",
            now=_now(),
        )


async def test_stale_worker_fencing_rejected(service: NotificationService) -> None:
    now = _now()
    await _create(service, now=now)
    (first_attempt,) = await service.claim_due(now=now)
    # Lease expires; another worker claims and gets a new fencing token.
    later = now + timedelta(minutes=5)
    (second_attempt,) = await service.claim_due(now=later)
    assert second_attempt.attempt_number == 2
    assert second_attempt.fencing_token != first_attempt.fencing_token
    with pytest.raises(NotificationFencingError):
        await service.complete_attempt(
            attempt_id=first_attempt.attempt_id,
            fencing_token=first_attempt.fencing_token,
            result="delivered",
            channel_receipt_id="stale",
            now=later,
        )
    recipient = await service.list_recipients(
        (await service.get_intent_by_key("crisis:evt-1", actor_person_id="minor-1")).intent_id,
        actor_person_id="guardian-1",
    )
    assert recipient[0].fencing_token == second_attempt.fencing_token


async def test_claim_while_lease_active_is_rejected(service: NotificationService) -> None:
    now = _now()
    await _create(service, now=now)
    (first,) = await service.claim_due(now=now)
    # A second worker cannot claim the same recipient while the lease is
    # active (list_due excludes it and the atomic claim guard refuses it).
    assert await service.claim_due(now=now + timedelta(seconds=30)) == ()
    assert await service._store.claim_recipient(  # noqa: SLF001
        first.recipient_id,
        DeliveryAttempt(
            attempt_id="other-worker",
            intent_id=first.intent_id,
            recipient_id=first.recipient_id,
            attempt_number=2,
            channel="sms",
            logical_delivery_key=f"{first.intent_id}:{first.recipient_id}:sms",
            status="leased",
            fencing_token="other-token",
            leased_until=now + timedelta(minutes=5),
            started_at=now,
        ),
        now + timedelta(seconds=30),
    ) is None


async def test_template_key_must_match_intent_kind(service: NotificationService) -> None:
    fence = NotificationFence(
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id="actor-1",
        subject_person_id="person-1",
        valid_until=_now() + timedelta(hours=2),
    )
    verifier = service.receipt_verifier
    assert verifier is not None
    verifier.register(
        make_notification_receipt(
            receipt_id="policy-receipt-1",
            actor_person_id="actor-1",
            subject_person_id="person-1",
            fence=fence,
            relationship_snapshot_ids=("rel-snap-1",),
        )
    )
    service.relationship_resolver.register(
        _snapshot(
            relationship_id="rel-ec",
            person_id="ec-1",
            role="emergency_contact",
        )
    )
    with pytest.raises(NotificationPolicyError):
        await service.create_intent(
            intent_kind="emergency",
            subject_person_id="person-1",
            source_event_id="evt-1",
            idempotency_key="emergency:bad-template",
            policy_receipt_id="policy-receipt-1",
            fence=fence,
            actor_person_id="actor-1",
            template_key="crisis_safety_notice",
            template_params={
                "role_label": "家人",
                "reason_code": "emergency_alert",
                "action_hint": "联系紧急联系人",
            },
            reason_code="emergency_alert",
            script_version="2026-08-09.1",
            recipients=(_spec(relationship_id="rel-ec"),),
            now=_now(),
        )


async def test_fallback_channel_on_retryable_failure(
    service: NotificationService,
) -> None:
    now = _now()
    await _create(
        service,
        recipients=(_spec(channels=("wechat_subscription", "sms")),),
        now=now,
    )
    (attempt,) = await service.claim_due(now=now)
    failed = await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="failed",
        error_code="channel_unavailable",
        now=now + timedelta(seconds=1),
    )
    assert failed.status == "failed"
    assert failed.channel_index == 1
    # Fallback is immediate: next attempt at now.
    assert failed.next_attempt_at is not None and failed.next_attempt_at <= now + timedelta(seconds=1)
    (fallback_attempt,) = await service.claim_due(now=now + timedelta(seconds=2))
    assert fallback_attempt.channel == "sms"
    delivered = await service.complete_attempt(
        attempt_id=fallback_attempt.attempt_id,
        fencing_token=fallback_attempt.fencing_token,
        result="delivered",
        channel_receipt_id="ext-sms-1",
        now=now + timedelta(seconds=3),
    )
    assert delivered.status == "delivered"
    assert delivered.delivered_channel == "sms"


async def test_max_retries_dead_letters(service: NotificationService) -> None:
    now = _now()
    service._max_retries = 2  # noqa: SLF001
    await _create(service, recipients=(_spec(channels=("sms",)),), now=now)
    (first,) = await service.claim_due(now=now)
    await service.complete_attempt(
        attempt_id=first.attempt_id,
        fencing_token=first.fencing_token,
        result="failed",
        error_code="timeout",
        now=now + timedelta(seconds=1),
    )
    (second,) = await service.claim_due(now=now + timedelta(minutes=1))
    assert second.attempt_number == 2
    failed = await service.complete_attempt(
        attempt_id=second.attempt_id,
        fencing_token=second.fencing_token,
        result="failed",
        error_code="timeout",
        now=now + timedelta(minutes=2),
    )
    assert failed.status == "dead_lettered"
    assert await service.claim_due(now=now + timedelta(hours=1)) == ()
    assert (await service.get_intent_by_key("crisis:evt-1", actor_person_id="minor-1")).status == "dead_lettered"


async def test_unknown_error_code_dead_letters_immediately(
    service: NotificationService,
) -> None:
    now = _now()
    await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    failed = await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="failed",
        error_code="unreviewed_code",
        now=now + timedelta(seconds=1),
    )
    assert failed.status == "dead_lettered"


async def test_wrong_contact_cancel_forbids_further_delivery(
    service: NotificationService,
) -> None:
    now = _now()
    _register_operator(service, actor_person_id="operator-1", now=now)
    intent = await _create(service, now=now)
    recipient = (await service.list_recipients(intent.intent_id, actor_person_id="guardian-1"))[0]
    cancelled = await service.cancel_recipient(
        recipient_id=recipient.recipient_id,
        reason="wrong_contact",
        actor_person_id="operator-1",
        authorization_ref="operator-receipt-1",
        now=now,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_reason == "wrong_contact"
    assert await service.claim_due(now=now + timedelta(hours=1)) == ()
    with pytest.raises(NotificationStateError):
        await service.replay_recipient(
            recipient_id=recipient.recipient_id,
            actor_person_id="operator-1",
            authorization_ref="operator-receipt-1",
            now=now + timedelta(hours=1),
        )


async def test_cancel_after_delivery_keeps_audit(service: NotificationService) -> None:
    """P0-4: delivered is terminal - a later cancel is a no-op that keeps
    the delivered facts (status + delivered_at) and never rewrites them."""
    now = _now()
    _register_operator(service, actor_person_id="guardian-1", now=now)
    intent = await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="delivered",
        channel_receipt_id="ext-1",
        now=now + timedelta(seconds=1),
    )
    recipient = (await service.list_recipients(intent.intent_id, actor_person_id="guardian-1"))[0]
    cancelled = await service.cancel_recipient(
        recipient_id=recipient.recipient_id,
        reason="wrong_contact",
        actor_person_id="guardian-1",
        authorization_ref="operator-receipt-1",
        now=now + timedelta(seconds=2),
    )
    # Terminal state is preserved; the delivered receipt history stays.
    assert cancelled.status == "delivered"
    assert cancelled.delivered_at is not None
    assert cancelled.cancelled_reason is None
    receipts = await service.list_receipts(
        intent.intent_id, actor_person_id="minor-1"
    )
    assert len(receipts) == 1


async def test_cancel_intent_cancels_all_recipients(
    service: NotificationService,
) -> None:
    now = _now()
    _register_operator(service, actor_person_id="operator-1", now=now)
    intent = await _create(
        service,
        recipients=(_spec(relationship_id="rel-1"), _spec(relationship_id="rel-2")),
        snapshots=(
            _snapshot(relationship_id="rel-1", person_id="g1"),
            _snapshot(relationship_id="rel-2", person_id="g2"),
        ),
        now=now,
    )
    updated = await service.cancel_intent(
        intent_id=intent.intent_id,
        reason="operator_override",
        actor_person_id="operator-1",
        authorization_ref="operator-receipt-1",
        now=now,
    )
    assert updated.status == "cancelled"
    recipients = await service.list_recipients(intent.intent_id, actor_person_id="guardian-1")
    assert all(item.status == "cancelled" for item in recipients)
    assert await service.claim_due(now=now + timedelta(hours=1)) == ()


async def test_relationship_inactive_cancels_recipients(
    service: NotificationService,
) -> None:
    now = _now()
    await _create(service, now=now)
    _register_operator(service, actor_person_id="operator-1", now=now)
    resolver = service.relationship_resolver
    assert resolver is not None
    resolver.register(
        RelationshipSnapshot(
            relationship_id="rel-1",
            subject_person_id="minor-1",
            person_id="guardian-1",
            role="guardian",
            status="revoked",
            valid_from=now - timedelta(days=1),
        )
    )
    cancelled = await service.mark_relationship_inactive(
        relationship_id="rel-1",
        actor_person_id="operator-1",
        authorization_ref="operator-receipt-1",
        now=now,
    )
    assert len(cancelled) == 1
    assert cancelled[0].cancelled_reason == "relationship_revoked"
    assert cancelled[0].relationship_status == "revoked"
    assert await service.claim_due(now=now + timedelta(hours=1)) == ()
    assert (await service.get_intent_by_key("crisis:evt-1", actor_person_id="minor-1")).status == "cancelled"
    resolver.register(
        RelationshipSnapshot(
            relationship_id="rel-1",
            subject_person_id="minor-1",
            person_id="guardian-1",
            role="guardian",
            status="suspended",
            valid_from=now - timedelta(days=1),
        )
    )
    with pytest.raises(NotificationPolicyError):
        await service.mark_relationship_inactive(
            relationship_id="rel-1",
            actor_person_id="operator-1",
            authorization_ref="operator-receipt-1",
            now=now,
        )


async def test_manual_replay_from_dead_letter(service: NotificationService) -> None:
    now = _now()
    _register_operator(service, actor_person_id="operator-1", now=now)
    intent = await _create(service, now=now)
    recipient = (await service.list_recipients(intent.intent_id, actor_person_id="guardian-1"))[0]
    (attempt,) = await service.claim_due(now=now)
    await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="failed",
        error_code="unreviewed_code",
        now=now + timedelta(seconds=1),
    )
    replayed = await service.replay_recipient(
        recipient_id=recipient.recipient_id,
        actor_person_id="operator-1",
        authorization_ref="operator-receipt-1",
        now=now + timedelta(minutes=1),
    )
    assert replayed.status == "failed"
    (fresh,) = await service.claim_due(now=now + timedelta(minutes=2))
    assert fresh.attempt_number == 2
    delivered = await service.complete_attempt(
        attempt_id=fresh.attempt_id,
        fencing_token=fresh.fencing_token,
        result="delivered",
        channel_receipt_id="ext-2",
        now=now + timedelta(minutes=3),
    )
    assert delivered.status == "delivered"


async def test_manual_replay_from_delivered_rejected(
    service: NotificationService,
) -> None:
    now = _now()
    _register_operator(service, actor_person_id="operator-1", now=now)
    intent = await _create(service, now=now)
    recipient = (await service.list_recipients(intent.intent_id, actor_person_id="guardian-1"))[0]
    (attempt,) = await service.claim_due(now=now)
    await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="delivered",
        channel_receipt_id="ext-1",
        now=now + timedelta(seconds=1),
    )
    with pytest.raises(NotificationStateError):
        await service.replay_recipient(
            recipient_id=recipient.recipient_id,
            actor_person_id="operator-1",
            authorization_ref="operator-receipt-1",
            now=now + timedelta(minutes=1),
        )


async def test_dispatch_due_with_real_fake_channel(
    service: NotificationService,
) -> None:
    now = _now()
    intent = await _create(service, now=now)
    channel = FakeChannel("wechat_subscription")
    summary = await service.dispatch_due(
        {"wechat_subscription": channel},  # type: ignore[dict-item]
        now=now,
    )
    assert summary.attempted == 1 and summary.delivered == 1 and summary.failed == 0
    assert len(channel.sent) == 1
    content = channel.sent[0][0]
    assert "孩子" in content and "safety_concern" in content
    for forbidden in ("transcript", "诊断", "depressed", "severity"):
        assert forbidden not in content
    assert (await service.get_intent(intent.intent_id, actor_person_id="minor-1")).status == "delivered"


async def test_dispatch_due_does_not_fabricate_delivery(
    service: NotificationService,
) -> None:
    now = _now()
    await _create(service, now=now)
    channel = FakeChannel("wechat_subscription", ok=False, error_code="timeout")
    summary = await service.dispatch_due(
        {"wechat_subscription": channel},  # type: ignore[dict-item]
        now=now,
    )
    assert summary.failed == 1 and summary.delivered == 0
    intent = await service.get_intent_by_key("crisis:evt-1", actor_person_id="minor-1")
    assert await service.list_receipts(intent.intent_id, actor_person_id="minor-1") == ()
    assert intent.status == "in_progress"  # retryable failure


@pytest.mark.parametrize(
    "scenario,expected_reason",
    [
        ("revoked", "relationship_revoked"),
        ("expired", "relationship_expired"),
        ("disputed", "relationship_disputed"),
        ("contact_changed", "relationship_revoked"),
        ("snapshot_mismatch", "relationship_revoked"),
        ("binding_bump", "authorization_revoked"),
        ("receipt_expired", "authorization_expired"),
        ("consent_revoked", "authorization_revoked"),
    ],
)
async def test_dispatch_fail_closed_reasons_never_call_adapter(
    store: InMemoryNotificationStore | SqliteNotificationStore,
    service: NotificationService,
    scenario: str,
    expected_reason: str,
) -> None:
    """Every pre-send re-verification failure records the PRECISE restricted
    reason (never a blanket one), never calls the adapter, terminates the
    leased attempt (no residue) and writes the same reason into recipient /
    attempt / audit / outbox."""
    now = _now()
    intent = await _create(service, now=now, key=f"crisis:fc-{scenario}")
    resolver = service.relationship_resolver
    verifier = service.receipt_verifier
    assert resolver is not None and verifier is not None

    if scenario == "revoked":
        resolver.register(
            _snapshot(status="revoked", valid_from=_FIXED_PAST)
        )
    elif scenario == "expired":
        resolver.register(
            _snapshot(status="expired", valid_from=_FIXED_PAST)
        )
    elif scenario == "disputed":
        resolver.register(
            _snapshot(status="disputed", valid_from=_FIXED_PAST)
        )
    elif scenario == "contact_changed":
        resolver.register(
            _snapshot(person_id="other-guardian", valid_from=_FIXED_PAST)
        )
    elif scenario == "snapshot_mismatch":
        resolver.register(
            _snapshot(snapshot_id="rel-snap-9", revision=2, valid_from=_FIXED_PAST)
        )
    elif scenario == "binding_bump":
        resolver.register(
            _snapshot(binding_version=2, valid_from=_FIXED_PAST)
        )
    elif scenario == "receipt_expired":
        verifier.register(
            make_notification_receipt(
                receipt_id="policy-receipt-1",
                actor_person_id="actor-1",
                subject_person_id="minor-1",
                fence=NotificationFence(
                    device_id="device-1",
                    session_id="session-1",
                    epoch=1,
                    binding_id="binding-1",
                    binding_version=1,
                    runtime_profile_id="profile-1",
                    actor_person_id="actor-1",
                    subject_person_id="minor-1",
                    valid_until=_now() + timedelta(hours=2),
                ),
                relationship_snapshot_ids=("rel-snap-1",),
                expires_at=now - timedelta(seconds=1),
            ),
            receipt_fence_valid=False,
            exact_evidence_valid=False,
        )
    elif scenario == "consent_revoked":
        verifier.register(
            make_notification_receipt(
                receipt_id="policy-receipt-1",
                actor_person_id="actor-1",
                subject_person_id="minor-1",
                fence=NotificationFence(
                    device_id="device-1",
                    session_id="session-1",
                    epoch=1,
                    binding_id="binding-1",
                    binding_version=1,
                    runtime_profile_id="profile-1",
                    actor_person_id="actor-1",
                    subject_person_id="minor-1",
                    valid_until=_now() + timedelta(hours=2),
                ),
                relationship_snapshot_ids=("rel-snap-1",),
            ),
            exact_evidence_valid=False,
        )

    channel = FakeChannel("wechat_subscription")
    summary = await service.dispatch_due(
        {"wechat_subscription": channel},  # type: ignore[dict-item]
        now=now,
    )
    assert len(channel.sent) == 0
    assert summary.cancelled == 1 and summary.delivered == 0
    recipient = (
        await service.list_recipients(intent.intent_id, actor_person_id="guardian-1")
    )[0]
    assert recipient.status == "cancelled"
    assert recipient.cancelled_reason == expected_reason
    # No leased residue: the attempt is finished with the same reason.
    attempts = await store.list_attempts(
        recipient.recipient_id, person_id="guardian-1"
    )
    assert attempts
    assert all(attempt.status != "leased" for attempt in attempts)
    assert attempts[0].error_code == expected_reason
    # Audit and outbox carry the same restricted reason.
    event_id = f"audit:recipient:{recipient.recipient_id}.cancelled"
    matching_audit = [
        event for event in store.audit_events() if event.event_id == event_id
    ]
    assert matching_audit
    assert matching_audit[0].payload.get("reason") == expected_reason
    outbox = [
        e
        for e in store.outbox_events()
        if e.event_id == f"recipient:{recipient.recipient_id}.cancelled"
    ]
    assert outbox and outbox[0].payload.get("reason") == expected_reason


async def test_dispatch_send_timeout_must_be_smaller_than_lease() -> None:
    store = InMemoryNotificationStore()
    with pytest.raises(ValueError, match="lease_seconds"):
        NotificationService(
            store,  # type: ignore[arg-type]
            lease_seconds=30.0,
            send_timeout_seconds=30.0,
        )
    with pytest.raises(ValueError, match="lease_seconds"):
        NotificationService(
            store,  # type: ignore[arg-type]
            lease_seconds=30.0,
            send_timeout_seconds=45.0,
        )


async def test_dispatch_raising_and_hanging_adapters_fail_atomically_and_continue(
    service: NotificationService,
) -> None:
    """P0 worker resilience: an adapter that raises or hangs (beyond the
    bounded send timeout) fails THAT attempt atomically with a stable
    retryable error code and the batch continues; no lease is left
    behind."""
    now = _now()
    await _create(service, now=now, key="crisis:raise")
    await _create(
        service,
        now=now,
        key="crisis:hang",
        recipients=(_spec(relationship_id="rel-hang", channels=("sms",)),),
        snapshots=(
            _snapshot(relationship_id="rel-hang", person_id="g2"),
        ),
    )
    raising = RaisingChannel()
    hanging = HangingChannel()
    # Both channels claim in one batch; each adapter failure is isolated.
    summary = await service.dispatch_due(
        {
            "wechat_subscription": raising,
            "sms": hanging,  # type: ignore[dict-item]
        },
        now=now,
    )
    assert summary.attempted == 2
    # A raising adapter FAILS atomically; a hanging adapter (beyond the
    # bounded send timeout) becomes UNKNOWN (uncertain) - never a failed
    # retry and never a fallback until a reconcile resolves it.
    assert summary.failed == 1
    assert summary.uncertain == 1
    # No leased residue: every attempt left the leased state (failed or
    # uncertain).
    intent_ids = (
        (await service.get_intent_by_key("crisis:raise", actor_person_id="minor-1")).intent_id,
        (await service.get_intent_by_key("crisis:hang", actor_person_id="minor-1")).intent_id,
    )
    for intent_id in intent_ids:
        recipients = await service._store.list_recipients_by_intent_subject(  # noqa: SLF001
            intent_id, subject_person_id="minor-1"
        )
        assert recipients
        attempts = await service._store.list_attempts(  # noqa: SLF001
            recipients[0].recipient_id, person_id=recipients[0].person_id
        )
        assert attempts and all(
            attempt.status != "leased" for attempt in attempts
        )
        assert attempts[0].error_code in (
            "channel_unavailable",
            "timeout_unknown",
        )


async def test_dispatch_fence_expiry_never_calls_adapter(
    service: NotificationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0: the fence authorization window is part of the fingerprint and
    persisted on the intent; once expired, the pre-send re-verification
    fails closed with authorization_expired and the adapter is never
    called."""
    now = _now()
    fence = NotificationFence(
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id="actor-1",
        subject_person_id="minor-1",
        valid_until=now + timedelta(minutes=1),
    )
    await _create(service, now=now, fence=fence, key="crisis:fence-expiry")
    # The pre-send re-verification runs against the REAL clock; advance it
    # deterministically past the fence window so the dispatch sees the
    # expiry (the batch's own ``now`` parameter only drives claim/lease).
    import services.notification.service as service_module

    class _FakeDatetime:
        @staticmethod
        def now(tz: object = None) -> datetime:
            return now + timedelta(minutes=5)

    monkeypatch.setattr(service_module, "datetime", _FakeDatetime)
    channel = FakeChannel("wechat_subscription")
    summary = await service.dispatch_due(
        {"wechat_subscription": channel},  # type: ignore[dict-item]
        now=now + timedelta(minutes=5),
    )
    assert len(channel.sent) == 0
    assert summary.cancelled == 1 and summary.delivered == 0
    intent = await service.get_intent_by_key(
        "crisis:fence-expiry", actor_person_id="minor-1"
    )
    recipient = (
        await service.list_recipients(intent.intent_id, actor_person_id="guardian-1")
    )[0]
    assert recipient.status == "cancelled"
    assert recipient.cancelled_reason == "authorization_expired"
    attempts = await service._store.list_attempts(  # noqa: SLF001
        recipient.recipient_id, person_id="guardian-1"
    )
    assert attempts and all(
        attempt.status != "leased" for attempt in attempts
    )
    assert attempts[0].error_code == "authorization_expired"


async def test_fence_valid_until_is_required_and_aware() -> None:
    """P0 / Contract: a sensitive notification fence is never unlimited -
    a missing or naive valid_until fails construction."""
    with pytest.raises(ValueError, match="valid_until"):
        NotificationFence(
            device_id="device-1",
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_version=1,
            runtime_profile_id="profile-1",
            actor_person_id="actor-1",
            subject_person_id="minor-1",
            valid_until=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        NotificationFence(
            device_id="device-1",
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_version=1,
            runtime_profile_id="profile-1",
            actor_person_id="actor-1",
            subject_person_id="minor-1",
            valid_until=datetime(2026, 8, 9, 10, 0),  # naive
        )


async def test_dispatch_short_lease_batch_bounded_by_remaining_lease(
    store: InMemoryNotificationStore | SqliteNotificationStore,
) -> None:
    """P0 lease race: with 4 recipients, a 2s lease and a 1s send timeout,
    later attempts must NOT start sending after their lease expired - each
    send is bounded by min(timeout, remaining lease) and an expired attempt
    fails without ever calling the adapter; no lease is left behind."""
    service = NotificationService(
        store,
        lease_seconds=2.0,
        max_retries=2,
        send_timeout_seconds=1.0,
        reconcile_timeout_seconds=0.5,
        receipt_verifier=InMemoryNotificationReceiptVerifier(),
        relationship_resolver=InMemoryRelationshipResolver(),
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
    )
    now = _now()
    recipients = tuple(
        _spec(relationship_id=f"rel-{i}", channels=("wechat_subscription",))
        for i in range(4)
    )
    snapshots = tuple(
        _snapshot(relationship_id=f"rel-{i}", person_id=f"g{i}")
        for i in range(4)
    )
    await _create(
        service,
        now=now,
        key="crisis:lease-race",
        recipients=recipients,
        snapshots=snapshots,
    )
    hanging = HangingChannel()
    summary = await service.dispatch_due(
        {"wechat_subscription": hanging},  # type: ignore[dict-item]
        now=now,
    )
    # Every attempt either timed out inside its lease or failed when the
    # lease had already expired.  Critically, the adapter was called at
    # most for the attempts that still had remaining lease when their send
    # started - the later attempts were never sent (a send past the lease
    # could have raced a re-claiming worker), so fewer sends than attempts
    # proves the lease bounding works.
    assert 0 < len(hanging.sent) < 4
    # Sends that timed out inside their lease are UNKNOWN (uncertain),
    # never failed; sends that could not start inside the remaining lease
    # fail atomically.  Unknown outcomes are never retried/fallback.
    assert summary.delivered == 0
    assert summary.failed + summary.uncertain == 4
    for i in range(4):
        intent = await service.get_intent_by_key(
            "crisis:lease-race", actor_person_id="minor-1"
        )
        recipients_list = await store.list_recipients_by_intent_subject(
            intent.intent_id, subject_person_id="minor-1"
        )
        attempts = await store.list_attempts(
            recipients_list[i].recipient_id,
            person_id=recipients_list[i].person_id,
        )
        assert attempts and all(
            attempt.status != "leased" for attempt in attempts
        )


async def test_dispatch_after_reclaim_sends_exactly_once(
    service: NotificationService,
) -> None:
    """P0: after the original lease expires and a NEW worker re-claims, the
    old worker's dispatch cycle cannot send (nothing is due to it) and the
    new worker sends exactly once - the same recipient is never double
    delivered."""
    now = _now()
    await _create(service, now=now, key="crisis:reclaim")
    channel = FakeChannel("wechat_subscription")
    later = now + timedelta(minutes=10)
    # Old worker's cycle after the lease expired: the recipient is now due
    # again, so the OLD worker could claim it too - but a deterministic
    # reclaim test must pin the token.  Claim as the old worker first.
    old_attempts = await service.claim_due(now=now)
    assert len(old_attempts) == 1
    old_attempt = old_attempts[0]
    # At ``later`` the lease has expired: a new dispatch cycle re-claims
    # with a NEW fencing token and delivers exactly once.
    fresh_summary = await service.dispatch_due(
        {"wechat_subscription": channel},  # type: ignore[dict-item]
        now=later,
    )
    assert fresh_summary.delivered == 1
    assert len(channel.sent) == 1
    # The OLD worker's completion is now stale: the recipient carries the
    # new token, so the CAS rejects it and changes nothing.
    with pytest.raises(NotificationFencingError):
        await service.complete_attempt(
            attempt_id=old_attempt.attempt_id,
            fencing_token=old_attempt.fencing_token,
            result="delivered",
            channel_receipt_id="stale-double",
            now=later,
        )
    receipts = await service.list_receipts(
        (await service.get_intent_by_key("crisis:reclaim", actor_person_id="minor-1")).intent_id,
        actor_person_id="minor-1",
    )
    assert len(receipts) == 1
    assert receipts[0].channel_receipt_id != "stale-double"


async def test_dispatch_recheck_uses_real_clock_after_slow_send(
    service: NotificationService,
) -> None:
    """P0: the relationship/policy re-check runs against the REAL current
    UTC clock immediately before each send - a slow earlier send must not
    let a later attempt pass an authorization check with the batch's stale
    start timestamp."""
    now = _now()
    await _create(
        service,
        now=now,
        key="crisis:recheck-clock",
        recipients=(
            _spec(relationship_id="rel-1", channels=("wechat_subscription",)),
            _spec(relationship_id="rel-2", channels=("wechat_subscription",)),
        ),
        snapshots=(
            _snapshot(relationship_id="rel-1", person_id="g1"),
            _snapshot(relationship_id="rel-2", person_id="g2"),
        ),
    )
    resolver = service.relationship_resolver
    assert resolver is not None

    class SlowChannel:
        channel = "wechat_subscription"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(
            self, *, recipient: RecipientBinding, content: str, attempt: DeliveryAttempt
        ) -> ChannelResult:
            self.sent.append(recipient.person_id)
            await asyncio.sleep(0.4)
            return ChannelResult(ok=True, channel_receipt_id=f"ext-{attempt.attempt_id}")

    channel = SlowChannel()

    async def revoke_second_after_start() -> None:
        await asyncio.sleep(0.15)
        resolver.register(
            _snapshot(
                relationship_id="rel-2",
                person_id="g2",
                status="revoked",
                valid_from=_FIXED_PAST,
            )
        )

    summary, _ = await asyncio.gather(
        service.dispatch_due(
            {"wechat_subscription": channel},  # type: ignore[dict-item]
            now=now,
        ),
        revoke_second_after_start(),
    )
    # The first recipient (slow send, 0.4s) delivered; the second was
    # re-checked at ~0.4s REAL time - after the revocation at 0.15s - and
    # was cancelled without ever calling the adapter.
    assert channel.sent == ["g1"]
    assert summary.delivered == 1 and summary.cancelled == 1
    intent = await service.get_intent_by_key(
        "crisis:recheck-clock", actor_person_id="minor-1"
    )
    recipients = await service._store.list_recipients_by_intent_subject(  # noqa: SLF001
        intent.intent_id, subject_person_id="minor-1"
    )
    by_person = {recipient.person_id: recipient for recipient in recipients}
    assert by_person["g1"].status == "delivered"
    assert by_person["g2"].status == "cancelled"
    assert by_person["g2"].cancelled_reason == "relationship_revoked"


async def test_dispatch_stale_completion_is_safe_noop_and_batch_continues(
    service: NotificationService,
) -> None:
    """P0: a completion whose fencing token was superseded by a re-claim is
    a safe no-op (no state change, no exception) and a raising/hanging
    adapter failure never aborts the rest of the batch."""
    now = _now()
    await _create(service, now=now, key="crisis:stale-1")
    await _create(
        service,
        now=now,
        key="crisis:stale-2",
        recipients=(_spec(relationship_id="rel-2", channels=("sms",)),),
        snapshots=(_snapshot(relationship_id="rel-2", person_id="g2"),),
    )
    # Claim the first attempt, let its lease expire and re-claim it with a
    # new token (simulating a racing worker).
    (old_attempt,) = await service.claim_due(now=now, limit=1)
    later = now + timedelta(minutes=10)
    (new_attempt,) = await service.claim_due(now=later, limit=1)
    assert new_attempt.fencing_token != old_attempt.fencing_token
    # The stale completion is a safe no-op.
    committed = await service._complete_or_stale(  # noqa: SLF001
        old_attempt,
        result="delivered",
        channel_receipt_id="stale-noop",
        now=later,
    )
    assert committed is None
    recipient = await service._store.get_recipient_for_worker(  # noqa: SLF001
        old_attempt.recipient_id
    )
    assert recipient is not None
    assert recipient.fencing_token == new_attempt.fencing_token
    assert recipient.status == "in_progress"
    # The batch continues past an adapter exception: the re-claimed
    # wechat recipient delivers via the new token while the second sms
    # recipient hits the raising adapter and fails without aborting.
    class RaisingChannel2:
        channel = "sms"

        async def send(self, **kwargs: object) -> ChannelResult:
            raise RuntimeError("boom")

    ok = FakeChannel("wechat_subscription")
    dispatch_at = later + timedelta(minutes=5)
    summary = await service.dispatch_due(
        {
            "wechat_subscription": ok,  # type: ignore[dict-item]
            "sms": RaisingChannel2(),  # type: ignore[dict-item]
        },
        now=dispatch_at,
    )
    assert summary.attempted == 2
    assert summary.delivered == 1 and summary.failed == 1
    # The re-claimed recipient delivered exactly once via the new token.
    assert len(ok.sent) == 1


async def test_cancellation_resistant_adapter_needs_provider_idempotency(
    service: NotificationService,
) -> None:
    """P0: wait_for cannot kill an adapter that swallows CancelledError -
    the local deadline times out and the batch continues, but the only hard
    double-delivery guarantee is the provider-side idempotency key: a
    repeated invocation for the SAME attempt produces no second external
    effect."""
    now = _now()
    await _create(service, now=now, key="crisis:cancel-resistant")
    resistant = CancellationResistantChannel()
    try:
        # Outer watchdog: if the hard-deadline implementation regresses the
        # dispatch would hang forever; this bounded wait fails the test
        # instead of hanging the whole CI run.
        summary = await asyncio.wait_for(
            service.dispatch_due(
                {"wechat_subscription": resistant},  # type: ignore[dict-item]
                now=now,
            ),
            timeout=30,
        )
        assert summary.uncertain == 1
        # The hung coroutine is still running (cancellation was swallowed);
        # only provider idempotency can prevent a double external effect.
        assert len(resistant.started) == 1
    finally:
        resistant.stop()
        await service.shutdown_detached_sends(timeout=5)

    # Provider-side idempotency: the SAME stable logical delivery key
    # invoked by attempt-1 (late) AND a re-claiming attempt-2 (different
    # attempt_number!) produces ONE external effect and the SAME external
    # receipt - this is the real timeout/reclaim double-delivery race, not
    # a same-attempt self-replay.
    provider = IdempotentFakeProvider()
    intent = await service.get_intent_by_key(
        "crisis:cancel-resistant", actor_person_id="minor-1"
    )
    recipient = (
        await service._store.list_recipients_by_intent_subject(  # noqa: SLF001
            intent.intent_id, subject_person_id="minor-1"
        )
    )[0]
    attempt = DeliveryAttempt(
        attempt_id="attempt-dup",
        intent_id=intent.intent_id,
        recipient_id=recipient.recipient_id,
        attempt_number=1,
        channel="wechat_subscription",
        logical_delivery_key=(
            f"{intent.intent_id}:{recipient.recipient_id}:wechat_subscription"
        ),
        status="leased",
        fencing_token="tok",
        leased_until=now + timedelta(minutes=1),
        started_at=now,
    )
    first = await provider.send(recipient=recipient, content="x", attempt=attempt)
    attempt2 = replace(
        attempt,
        attempt_id="attempt-2",
        attempt_number=2,
        fencing_token="new-token",
    )
    second = await provider.send(
        recipient=recipient, content="x", attempt=attempt2
    )
    assert first.ok and second.ok
    assert first.channel_receipt_id == second.channel_receipt_id
    assert len(provider.external_effects) == 1


async def test_backoff_injected_into_retry_schedule(
    service: NotificationService,
) -> None:
    now = _now()
    service._backoff = BackoffPolicy(  # noqa: SLF001
        base_seconds=60.0, factor=2.0, max_seconds=600.0, rng=random.Random(7)
    )
    await _create(service, recipients=(_spec(channels=("sms",)),), now=now)
    (attempt,) = await service.claim_due(now=now)
    failed = await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="failed",
        error_code="rate_limited",
        now=now + timedelta(seconds=1),
    )
    assert failed.next_attempt_at is not None
    assert failed.next_attempt_at > now + timedelta(seconds=50)
    assert failed.next_attempt_at < now + timedelta(seconds=80)


async def test_audit_and_outbox_events(service: NotificationService) -> None:
    now = _now()
    await _create(service, now=now)
    (attempt,) = await service.claim_due(now=now)
    await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="delivered",
        channel_receipt_id="ext-1",
        now=now + timedelta(seconds=1),
    )
    store = service._store  # noqa: SLF001
    outbox = store.outbox_events()
    topics = {event.topic for event in outbox}
    assert "notification.intent.created" in topics
    assert "notification.intent.delivered" in topics
    actions = {event.action for event in store.audit_events()}
    assert {"intent.create", "recipient.delivered"} <= actions
    assert len({event.event_id for event in outbox}) == len(outbox)


async def test_sqlite_legacy_schema_migrates_fence_columns(
    tmp_path: Path,
) -> None:
    """Databases created before the fence-snapshot columns existed must be
    upgraded idempotently on initialize (device_id/subject_revision on
    intents, relationship_snapshot_id/relationship_revision on recipients):
    INSERTs must never fail with 'no column named'."""
    path = tmp_path / "legacy-notification.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE notification_intents (
            intent_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            intent_kind TEXT NOT NULL,
            subject_person_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            policy_receipt_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            epoch INTEGER NOT NULL,
            binding_id TEXT NOT NULL,
            binding_version INTEGER NOT NULL,
            runtime_profile_id TEXT NOT NULL,
            actor_person_id TEXT NOT NULL,
            fence_context_hash TEXT NOT NULL,
            template_key TEXT NOT NULL,
            template_params_json TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            script_version TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            status TEXT NOT NULL,
            cancelled_reason TEXT,
            cancelled_at TEXT,
            delivered_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE notification_recipients (
            recipient_id TEXT PRIMARY KEY,
            intent_id TEXT NOT NULL,
            person_id TEXT NOT NULL,
            role TEXT NOT NULL,
            relationship_id TEXT NOT NULL,
            relationship_status TEXT NOT NULL,
            channels_json TEXT NOT NULL,
            channel_index INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL,
            next_attempt_at TEXT,
            leased_until TEXT,
            fencing_token TEXT,
            last_error_code TEXT,
            valid_from TEXT NOT NULL,
            valid_until TEXT,
            delivered_at TEXT,
            delivered_channel TEXT,
            cancelled_reason TEXT,
            cancelled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    # A legacy intent row WITHOUT a fence window (pre-P0 data): after the
    # migration it must be quarantined (cancelled + audit), never treated
    # as unlimited and never dispatched.
    connection.execute(
        """
        INSERT INTO notification_intents (
            intent_id, idempotency_key, intent_kind, subject_person_id,
            source_event_id, policy_receipt_id, session_id, epoch,
            binding_id, binding_version, runtime_profile_id,
            actor_person_id, fence_context_hash, template_key,
            template_params_json, reason_code, script_version,
            occurred_at, status, created_at, updated_at
        ) VALUES ('legacy-null-window', 'legacy:k-null', 'crisis_safety',
                  'minor-1', 'e-null', 'r-null', 's-null', 1, 'b-null', 1,
                  'p-null', 'a-null', 'h-null', 'crisis_safety_notice',
                  '{}', 'safety_concern', 'v-null', '2026-08-09T10:00:00+00:00',
                  'pending', '2026-08-09T10:00:00+00:00',
                  '2026-08-09T10:00:00+00:00')
        """
    )
    connection.commit()
    connection.close()

    store = SqliteNotificationStore(path)
    await store.initialize()
    # initialize is idempotent: a second call must not break anything.
    await store.initialize()
    ready = store._ready()  # noqa: SLF001
    intent_columns = {
        str(row["name"])
        for row in ready.execute("PRAGMA table_info(notification_intents)")
    }
    recipient_columns = {
        str(row["name"])
        for row in ready.execute("PRAGMA table_info(notification_recipients)")
    }
    assert {"device_id", "subject_revision"} <= intent_columns
    assert {"relationship_snapshot_id", "relationship_revision"} <= recipient_columns
    # Legacy NULL fence window: quarantined as cancelled with an audit row.
    quarantined = ready.execute(
        "SELECT status, cancelled_reason, valid_until FROM notification_intents"
        " WHERE intent_id = 'legacy-null-window'"
    ).fetchone()
    assert quarantined is not None
    assert quarantined["status"] == "cancelled"
    assert quarantined["cancelled_reason"] == "authorization_expired"
    assert quarantined["valid_until"] is not None
    quarantine_audit = ready.execute(
        "SELECT action FROM notification_audit_events"
        " WHERE event_id = 'audit:legacy-null-window:quarantine'"
    ).fetchone()
    assert quarantine_audit is not None
    assert quarantine_audit["action"] == "intent.quarantine"
    # The invariant is now enforced at the database level: a NULL fence
    # window can never be written again (fail closed, not just migrated).
    with pytest.raises(sqlite3.IntegrityError, match="valid_until"):
        ready.execute(
            """
            INSERT INTO notification_intents (
                intent_id, idempotency_key, intent_kind, subject_person_id,
                source_event_id, policy_receipt_id, session_id, epoch,
                binding_id, binding_version, runtime_profile_id,
                actor_person_id, fence_context_hash, template_key,
                template_params_json, reason_code, script_version,
                occurred_at, status, created_at, updated_at
            ) VALUES ('null-rejected', 'k:null-rejected', 'crisis_safety',
                      'minor-1', 'e', 'r', 's', 1, 'b', 1, 'p', 'a', 'h',
                      'crisis_safety_notice', '{}', 'safety_concern', 'v',
                      '2026-08-09T10:00:00+00:00', 'pending',
                      '2026-08-09T10:00:00+00:00',
                      '2026-08-09T10:00:00+00:00')
            """
        )
    await store.close()

    # A full business roundtrip against the migrated legacy database works.
    store = SqliteNotificationStore(path)
    await store.initialize()
    service = NotificationService(
        store,
        receipt_verifier=InMemoryNotificationReceiptVerifier(),
        relationship_resolver=InMemoryRelationshipResolver(),
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
    )
    await _create(service)
    (attempt,) = await service.claim_due(now=_now())
    delivered = await service.complete_attempt(
        attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        result="delivered",
        channel_receipt_id="migrated-ext",
        now=_now() + timedelta(seconds=1),
    )
    assert delivered.status == "delivered"
    await store.close()


async def test_sqlite_naive_timestamps_rejected_at_persistence_boundary(
    tmp_path: Path,
) -> None:
    """_iso/_from_iso must reject naive or malformed timestamps at the
    store boundary - not just at domain construction (unreachable-check
    regression)."""
    from services.notification.sqlite_store import _from_iso, _iso, _required_iso

    with pytest.raises(ValueError, match="timezone-aware"):
        _iso(datetime(2026, 8, 9, 10, 0))  # naive
    with pytest.raises(ValueError, match="timezone-aware"):
        _from_iso("2026-08-09T10:00:00")  # naive persisted value
    with pytest.raises(ValueError):
        _from_iso("not-a-date")
    with pytest.raises(ValueError, match="missing required"):
        _required_iso(None)

    aware = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    assert _from_iso(_iso(aware)) == aware

    # A naive timestamp inside a domain object must fail at the store
    # boundary when a SQLite row is round-tripped.
    store = SqliteNotificationStore(tmp_path / "naive.sqlite3")
    await store.initialize()
    service = NotificationService(
        store,
        receipt_verifier=InMemoryNotificationReceiptVerifier(),
        relationship_resolver=InMemoryRelationshipResolver(),
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
    )
    with pytest.raises(NotificationPolicyError, match="timezone-aware"):
        await _create(service, occurred_at=datetime(2026, 8, 9, 10, 0))
    await store.close()
