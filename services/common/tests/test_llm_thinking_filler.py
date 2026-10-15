from __future__ import annotations

from services.common.llm_thinking_filler import LlmThinkingFiller, speech_end_monotonic


def test_filler_is_due_three_seconds_after_speech_end_not_commit() -> None:
    gate = LlmThinkingFiller()
    speech_ended, commit_at = 100.0, 101.5
    gate.arm(turn_id=1, now=speech_ended)
    assert gate.seconds_until_due(turn_id=1, now=commit_at) == 1.5
    assert gate.should_play(turn_id=1, now=102.99) is False
    assert gate.should_play(turn_id=1, now=103.0) is True


def test_speech_end_is_backdated_to_the_last_voiced_sample() -> None:
    assert (
        speech_end_monotonic(received_at=10.0, event_sample=80_000, voiced_end_sample=72_000)
        == 9.5
    )


def test_speech_end_without_an_earlier_voiced_sample_is_the_receipt() -> None:
    for voiced_end_sample in (None, 80_000, 96_000):
        assert (
            speech_end_monotonic(
                received_at=10.0, event_sample=80_000, voiced_end_sample=voiced_end_sample
            )
            == 10.0
        )


def test_filler_plays_at_most_once_per_turn() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=2, now=0.0)
    gate.mark_played(turn_id=2)
    assert gate.should_play(turn_id=2, now=10.0) is False
    assert gate.seconds_until_due(turn_id=2, now=10.0) is None


def test_reply_audio_closes_the_filler_for_the_turn() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=3, now=0.0)
    gate.note_reply_spoken(turn_id=3)
    assert gate.should_play(turn_id=3, now=5.0) is False
    gate.mark_played(turn_id=3)
    assert gate.should_play(turn_id=3, now=5.0) is False


def test_new_turn_rearms_after_the_last_one_played() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=4, now=0.0)
    gate.mark_played(turn_id=4)
    gate.arm(turn_id=5, now=10.0)
    assert gate.should_play(turn_id=5, now=12.9) is False
    assert gate.should_play(turn_id=5, now=13.0) is True


def test_late_signals_for_an_earlier_turn_leave_the_current_turn_armed() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=6, now=0.0)
    gate.arm(turn_id=7, now=1.0)
    gate.note_reply_spoken(turn_id=6)
    gate.mark_played(turn_id=6)
    assert gate.should_play(turn_id=6, now=4.0) is False
    assert gate.should_play(turn_id=7, now=4.0) is True


def test_unarmed_gate_never_plays() -> None:
    gate = LlmThinkingFiller()
    assert gate.should_play(turn_id=1, now=100.0) is False
    assert gate.seconds_until_due(turn_id=1, now=100.0) is None
