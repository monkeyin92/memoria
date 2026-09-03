"""Idempotent delivery state for one fenced assistant reply.

``OutputDispatchResult`` describes the in-process scheduling task.  This
ledger is the media contract on top of it: one complete generation fence has
one stable delivery key, a bounded event history, and at most one terminal
outcome.  It deliberately stores no transcript or audio payload.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from services.agent.src.contracts.ids import GenerationFence


class ReplyDeliveryEvent(StrEnum):
    """Observable milestones and terminal events for one reply delivery."""

    FIRST_FRAME_SENT = "first_frame_sent"
    PROVIDER_COMPLETED = "provider_completed"
    ACTUAL_HEARD = "actual_heard"
    PLAYBACK_ENDED = "playback_ended"
    PREEMPTED = "preempted"
    TRANSPORT_REJECTED = "transport_rejected"
    ERROR = "error"
    SKIPPED = "skipped"
    NO_AUDIO = "no_audio"


_TERMINAL_EVENTS = frozenset(
    {
        ReplyDeliveryEvent.PLAYBACK_ENDED,
        ReplyDeliveryEvent.PREEMPTED,
        ReplyDeliveryEvent.TRANSPORT_REJECTED,
        ReplyDeliveryEvent.ERROR,
        ReplyDeliveryEvent.SKIPPED,
        ReplyDeliveryEvent.NO_AUDIO,
    }
)


@dataclass(frozen=True, slots=True)
class ReplyDeliveryKey:
    """Stable identity for one reply, including the complete authority fence."""

    fence: GenerationFence

    @classmethod
    def from_fence(cls, fence: GenerationFence) -> ReplyDeliveryKey:
        return cls(fence=fence)

    @property
    def delivery_id(self) -> str:
        """Human-readable, non-secret correlation id with no user content."""

        return (
            f"{self.fence.session_id}/epoch-{self.fence.session_epoch}"
            f"/turn-{self.fence.turn_id}/generation-{self.fence.generation_id}"
            f"/tool-{self.fence.tool_epoch}"
        )


@dataclass(frozen=True, slots=True)
class ReplyDelivery:
    """Immutable snapshot of the delivery state for one fenced reply."""

    key: ReplyDeliveryKey
    events: tuple[ReplyDeliveryEvent, ...] = ()
    first_frame_sent: bool = False
    provider_completed: bool = False
    actual_heard: bool = False
    playback_ended: bool = False
    terminal_event: ReplyDeliveryEvent | None = None
    terminal_reason: str | None = None

    @property
    def delivery_id(self) -> str:
        return self.key.delivery_id

    @property
    def terminal(self) -> bool:
        return self.terminal_event is not None


@dataclass(slots=True)
class ReplyDeliveryLedger:
    """Keep one idempotent, fence-keyed delivery record per active session."""

    max_deliveries: int = 256
    _records: OrderedDict[ReplyDeliveryKey, ReplyDelivery] = field(
        default_factory=OrderedDict,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.max_deliveries <= 0:
            raise ValueError("max_deliveries must be positive")

    def ensure(self, fence: GenerationFence) -> ReplyDelivery:
        """Create or return the stable record for ``fence``."""

        key = ReplyDeliveryKey.from_fence(fence)
        current = self._records.get(key)
        if current is not None:
            return current
        current = ReplyDelivery(key=key)
        self._records[key] = current
        self._evict()
        return current

    def get(self, fence: GenerationFence) -> ReplyDelivery | None:
        return self._records.get(ReplyDeliveryKey.from_fence(fence))

    def snapshots(self) -> tuple[ReplyDelivery, ...]:
        """Return records in insertion order for diagnostics and tests."""

        return tuple(self._records.values())

    def record(
        self,
        fence: GenerationFence,
        event: ReplyDeliveryEvent,
        *,
        reason: str = "",
    ) -> tuple[ReplyDelivery, bool]:
        """Record one event, returning ``(snapshot, changed)``.

        Repeating the same milestone or terminal event is a no-op.  Once a
        terminal event exists, late events are ignored so a stale callback
        cannot rewrite the authoritative outcome — except an unheard
        ``PREEMPTED`` may be replaced by ``FIRST_FRAME_SENT`` when a later
        successful attempt reuses the same fence (live-lookup deep TTS).
        """

        current = self.ensure(fence)
        if event in current.events:
            return current, False
        # Unheard scheduling cancels (e.g. live-lookup preempt before any
        # downlink frame) must not poison a later successful deep TTS on the
        # same fence. Heard preempts stay sticky.
        if (
            current.terminal
            and current.terminal_event is ReplyDeliveryEvent.PREEMPTED
            and not current.first_frame_sent
            and event is ReplyDeliveryEvent.FIRST_FRAME_SENT
        ):
            current = ReplyDelivery(key=current.key)
            self._records[current.key] = current
        elif current.terminal:
            return current, False
        if event is ReplyDeliveryEvent.ACTUAL_HEARD and not current.first_frame_sent:
            raise ValueError("actual_heard requires first_frame_sent")
        if event is ReplyDeliveryEvent.PLAYBACK_ENDED and not current.first_frame_sent:
            raise ValueError("playback_ended requires first_frame_sent")
        if reason and len(reason) > 64:
            raise ValueError("reply delivery reason must be at most 64 characters")

        events = (*current.events, event)
        updated = replace(
            current,
            events=events,
            first_frame_sent=current.first_frame_sent
            or event is ReplyDeliveryEvent.FIRST_FRAME_SENT,
            provider_completed=current.provider_completed
            or event is ReplyDeliveryEvent.PROVIDER_COMPLETED,
            actual_heard=current.actual_heard or event is ReplyDeliveryEvent.ACTUAL_HEARD,
            playback_ended=current.playback_ended
            or event is ReplyDeliveryEvent.PLAYBACK_ENDED,
            terminal_event=event if event in _TERMINAL_EVENTS else current.terminal_event,
            terminal_reason=(reason or event.value) if event in _TERMINAL_EVENTS else current.terminal_reason,
        )
        self._records[current.key] = updated
        return updated, True

    def _evict(self) -> None:
        while len(self._records) > self.max_deliveries:
            key, record = next(iter(self._records.items()))
            if not record.terminal:
                # Never evict an in-flight delivery.  The next completed
                # delivery will make room without losing its trace.
                self._records.move_to_end(key)
                if all(not item.terminal for item in self._records.values()):
                    return
                continue
            self._records.popitem(last=False)


def reply_delivery_projection_payload(
    snapshot: ReplyDelivery,
    event: ReplyDeliveryEvent,
    *,
    reason: str = "",
    occurred_at: datetime | None = None,
) -> dict[str, Any]:
    """Build the cross-process, text-free projection for one changed event."""

    fence = snapshot.key.fence
    event_id = hashlib.sha256(
        f"{snapshot.delivery_id}\0{event.value}".encode()
    ).hexdigest()
    observed = occurred_at or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("reply delivery occurred_at must be timezone-aware")
    return {
        "schema_version": "reply-delivery-v1",
        "event_id": event_id,
        "delivery_id": snapshot.delivery_id,
        "session_id": fence.session_id,
        "session_epoch": fence.session_epoch,
        "turn_id": fence.turn_id,
        "generation_id": fence.generation_id,
        "tool_epoch": fence.tool_epoch,
        "event_type": event.value,
        "terminal_event": (
            snapshot.terminal_event.value if snapshot.terminal_event is not None else None
        ),
        "terminal_reason": snapshot.terminal_reason,
        "first_frame_sent": snapshot.first_frame_sent,
        "provider_completed": snapshot.provider_completed,
        "actual_heard": snapshot.actual_heard,
        "playback_ended": snapshot.playback_ended,
        "reason": reason or None,
        "occurred_at": observed.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


__all__ = [
    "ReplyDelivery",
    "ReplyDeliveryEvent",
    "ReplyDeliveryKey",
    "ReplyDeliveryLedger",
    "reply_delivery_projection_payload",
]
