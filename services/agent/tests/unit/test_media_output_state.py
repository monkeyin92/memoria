"""A fence that already has audio on the device keeps its output counters.

2026-10-01: a live-lookup acknowledgement started speaking while the commit that created its fence
was still finishing. The commit then zeroed the response counters, so the next output of the same
fence (the local reply after the lookup ended without a result) restarted at sequence 0; the downlink
expects the next number and rejected it as ``sequence_gap``. The answer never played and the device
stayed in SPEAKING for minutes.
"""

from __future__ import annotations

from pathlib import Path

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.media_session_state import OutputState
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent

FENCE = GenerationFence("session", 2, 2, 0)
OTHER = GenerationFence("session", 3, 3, 0)


def _dirty(output: OutputState) -> None:
    output.output_sequence = 13
    output.output_text_offset = 6
    output.assistant_text = "我查一下"
    output.provider_complete = True
    output.output_complete_emitted = True
    output.tts_started_ns = 1
    output.first_audio_observed = True


def test_a_fresh_fence_starts_every_counter_at_zero() -> None:
    output = OutputState()
    _dirty(output)  # left over from the previous response
    assert output.begin_turn_output(FENCE) is True
    assert output.playback.current_fence == FENCE
    assert (output.output_sequence, output.output_text_offset, output.assistant_text) == (0, 0, "")
    assert output.provider_complete is False and output.output_complete_emitted is False
    assert output.tts_started_ns is None and output.first_audio_observed is False


def test_a_fence_with_registered_audio_keeps_its_counters() -> None:
    output = OutputState()
    output.playback.start(FENCE)
    assert output.playback.register_audio(FENCE, 0, 0, 6720)  # the acknowledgement's first frame
    _dirty(output)
    assert output.audio_sent_for(FENCE) is True
    assert output.begin_turn_output(FENCE) is False
    assert output.output_sequence == 13  # the next frame must be 13, not 0
    assert (output.output_text_offset, output.assistant_text) == (6, "我查一下")
    assert output.provider_complete is True and output.first_audio_observed is True
    assert output.playback.current_fence == FENCE


def test_a_delivery_that_sent_its_first_frame_counts_even_without_a_registered_range() -> None:
    output = OutputState()
    output.reply_delivery.record(FENCE, ReplyDeliveryEvent.FIRST_FRAME_SENT, reason="downlink_frame_accepted")
    _dirty(output)
    assert output.audio_sent_for(FENCE) is True
    assert output.begin_turn_output(FENCE) is False
    assert output.output_sequence == 13


def test_audio_of_another_fence_does_not_protect_this_one() -> None:
    output = OutputState()
    output.playback.start(OTHER)
    assert output.playback.register_audio(OTHER, 0, 0, 160)
    _dirty(output)
    assert output.audio_sent_for(FENCE) is False
    assert output.begin_turn_output(FENCE) is True
    assert output.output_sequence == 0


def test_the_turn_commit_goes_through_the_helper_and_does_not_zero_the_counters_itself() -> None:
    """The reset used to be six inline assignments in the commit; pin the one place that is safe."""

    source = (
        Path(__file__).resolve().parents[2] / "src" / "voice_core" / "media_session_commit.py"
    ).read_text(encoding="utf-8")
    assert "context.output.begin_turn_output(fence)" in source
    assert "context.output.output_sequence = 0" not in source


def test_the_default_turn_budget_covers_the_followup_window_and_the_commit() -> None:
    """2026-10-01: the first question after a reply waits the reopen window and the follow-up grace before it
    commits, and the commit then prepares for 1-1.5 s; the old 2.5 s budget closed the conversation under the
    question being answered (turn_prepare_timeout) in 4 of 7 tries in a quiet room."""

    from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
    from services.agent.src.voice_core.media_session_input import _REOPEN_EVIDENCE_WINDOW_S
    from services.agent.src.voice_core.media_session_playback_stop import (
        _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S,
    )

    budget = MediaVoiceCoreRegistry.__dataclass_fields__["turn_endpoint_absolute_timeout_s"].default
    preparation = 1.5
    assert budget >= _REOPEN_EVIDENCE_WINDOW_S + _PLAYBACK_FOLLOWUP_ENDPOINT_GRACE_S + preparation
