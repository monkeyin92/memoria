from __future__ import annotations

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.reply_delivery import (
    ReplyDeliveryEvent,
    ReplyDeliveryLedger,
    reply_delivery_projection_payload,
)


def test_delivery_key_includes_complete_generation_fence() -> None:
    ledger = ReplyDeliveryLedger()
    first = GenerationFence("session", 4, 8, 2, session_epoch=1)
    second = first.with_session_epoch(2)

    first_record = ledger.ensure(first)
    second_record = ledger.ensure(second)

    assert first_record.delivery_id == "session/epoch-1/turn-4/generation-8/tool-2"
    assert second_record.delivery_id != first_record.delivery_id
    assert len(ledger.snapshots()) == 2


def test_delivery_events_are_idempotent_and_terminal_is_sticky() -> None:
    ledger = ReplyDeliveryLedger()
    fence = GenerationFence("session", 4, 8, 2, session_epoch=1)

    for event, reason in (
        (ReplyDeliveryEvent.FIRST_FRAME_SENT, "downlink_frame_accepted"),
        (ReplyDeliveryEvent.PROVIDER_COMPLETED, "provider_stream_complete"),
        (ReplyDeliveryEvent.ACTUAL_HEARD, "exact_playback_ack"),
        (ReplyDeliveryEvent.PLAYBACK_ENDED, "playback_completed"),
    ):
        ledger.record(fence, event, reason=reason)

    duplicate, changed = ledger.record(
        fence,
        ReplyDeliveryEvent.PLAYBACK_ENDED,
        reason="duplicate_callback",
    )
    late, late_changed = ledger.record(
        fence,
        ReplyDeliveryEvent.PREEMPTED,
        reason="stale_interrupt",
    )

    assert changed is False
    assert late_changed is False
    assert duplicate == late
    assert duplicate.events == (
        ReplyDeliveryEvent.FIRST_FRAME_SENT,
        ReplyDeliveryEvent.PROVIDER_COMPLETED,
        ReplyDeliveryEvent.ACTUAL_HEARD,
        ReplyDeliveryEvent.PLAYBACK_ENDED,
    )
    assert duplicate.first_frame_sent is True
    assert duplicate.provider_completed is True
    assert duplicate.actual_heard is True
    assert duplicate.playback_ended is True
    assert duplicate.terminal_event is ReplyDeliveryEvent.PLAYBACK_ENDED
    assert duplicate.terminal_reason == "playback_completed"


def test_terminal_failure_can_be_recorded_before_any_audio() -> None:
    ledger = ReplyDeliveryLedger()
    fence = GenerationFence("session", 1, 1, 0)

    record, changed = ledger.record(
        fence,
        ReplyDeliveryEvent.TRANSPORT_REJECTED,
        reason="transport_rejected",
    )

    assert changed is True
    assert record.first_frame_sent is False
    assert record.terminal_event is ReplyDeliveryEvent.TRANSPORT_REJECTED
    assert record.terminal_reason == "transport_rejected"


def test_actual_heard_requires_first_frame_boundary() -> None:
    ledger = ReplyDeliveryLedger()
    fence = GenerationFence("session", 1, 1, 0)

    with pytest.raises(ValueError, match="first_frame_sent"):
        ledger.record(fence, ReplyDeliveryEvent.ACTUAL_HEARD)


def test_projection_payload_is_fenced_and_text_free() -> None:
    ledger = ReplyDeliveryLedger()
    fence = GenerationFence("session", 7, 11, 2, session_epoch=3)
    snapshot, changed = ledger.record(fence, ReplyDeliveryEvent.FIRST_FRAME_SENT)
    assert changed

    payload = reply_delivery_projection_payload(
        snapshot,
        ReplyDeliveryEvent.FIRST_FRAME_SENT,
        reason="downlink_frame_accepted",
    )

    assert payload["delivery_id"] == snapshot.delivery_id
    assert payload["event_id"]
    assert payload["session_epoch"] == 3
    assert payload["first_frame_sent"] is True
    assert "text" not in payload
    assert "pcm_s16le" not in payload
