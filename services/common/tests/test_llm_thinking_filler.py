"""Timing and once-per-turn rules for the LLM thinking filler gate."""

from __future__ import annotations

from services.common.llm_thinking_filler import (
    LLM_THINKING_DELAY_S,
    LLM_THINKING_TEXT,
    LlmThinkingFiller,
)


def test_filler_plays_only_after_delay() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=1, now=100.0)
    assert gate.should_play(turn_id=1, now=100.0 + LLM_THINKING_DELAY_S - 0.01) is False
    assert gate.should_play(turn_id=1, now=100.0 + LLM_THINKING_DELAY_S) is True


def test_filler_plays_at_most_once_per_turn() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=2, now=0.0)
    assert gate.should_play(turn_id=2, now=LLM_THINKING_DELAY_S) is True
    gate.mark_played(turn_id=2)
    assert gate.should_play(turn_id=2, now=LLM_THINKING_DELAY_S + 10.0) is False


def test_filler_never_after_reply_text_spoken() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=3, now=0.0)
    gate.note_reply_spoken(turn_id=3)
    assert gate.should_play(turn_id=3, now=LLM_THINKING_DELAY_S + 1.0) is False


def test_new_turn_can_play_again() -> None:
    gate = LlmThinkingFiller()
    gate.arm(turn_id=4, now=0.0)
    gate.mark_played(turn_id=4)
    gate.arm(turn_id=5, now=10.0)
    assert gate.should_play(turn_id=5, now=10.0 + LLM_THINKING_DELAY_S) is True


def test_filler_text_is_the_fixed_line() -> None:
    assert LLM_THINKING_TEXT == "嗯，我想一想。"
