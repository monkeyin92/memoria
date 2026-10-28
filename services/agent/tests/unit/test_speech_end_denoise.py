from __future__ import annotations

import math

import pytest
from services.agent.src.voice_core.speech_end_denoise import (
    SpeechEndDenoiseLatch,
    SpeechEndDenoiseScore,
    _ScoreIdle,
    add_shift_mean,
    freeze_utterance,
    mark_utterance,
)


def test_shifts_before_mark_are_outside_the_frozen_mean() -> None:
    window = _ScoreIdle()
    window = add_shift_mean(window, 1.0)
    window = add_shift_mean(window, 1.0)
    window = mark_utterance(window)
    window = add_shift_mean(window, 0.2)
    window = add_shift_mean(window, 0.6)
    _frozen, score = freeze_utterance(window)
    assert score is not None
    assert score.summary == pytest.approx((0.2 + 0.6) / 2)


def test_mean_of_shifts_after_mark_is_the_arithmetic_mean() -> None:
    shifts = (0.1, 0.2, 0.4, 0.5)
    window = mark_utterance(_ScoreIdle())
    for shift in shifts:
        window = add_shift_mean(window, shift)
    _frozen, score = freeze_utterance(window)
    assert score is not None
    assert score.summary == pytest.approx(sum(shifts) / len(shifts))


def test_shift_after_freeze_and_second_freeze_return_the_first_score() -> None:
    window = mark_utterance(_ScoreIdle())
    window = add_shift_mean(window, 0.2)
    window = add_shift_mean(window, 0.4)
    frozen, first = freeze_utterance(window)
    assert first is not None
    grown = add_shift_mean(frozen, 0.9)
    _again, second = freeze_utterance(grown)
    assert second is first
    assert second.summary == pytest.approx(0.3)


def test_mark_after_freeze_with_no_new_finite_shift_freezes_none() -> None:
    window = mark_utterance(_ScoreIdle())
    window = add_shift_mean(window, 0.4)
    _frozen, first = freeze_utterance(window)
    assert first is not None
    reopened = mark_utterance(_frozen)
    reopened = add_shift_mean(reopened, math.nan)
    _refrozen, score = freeze_utterance(reopened)
    assert score is None


def test_non_finite_bin_mean_does_not_change_the_window() -> None:
    window = mark_utterance(_ScoreIdle())
    window = add_shift_mean(window, 0.3)
    assert add_shift_mean(window, math.nan) is window
    assert add_shift_mean(window, math.inf) is window
    assert add_shift_mean(window, -math.inf) is window


def test_idle_freeze_returns_none() -> None:
    idle = _ScoreIdle()
    window, score = freeze_utterance(idle)
    assert window is idle
    assert score is None


def test_latch_applies_to_stored_sample_including_zero() -> None:
    score = SpeechEndDenoiseScore(0.25)
    latch = SpeechEndDenoiseLatch(0, score)
    assert latch.applies_to(0) is True
    assert latch.applies_to(None) is False
    assert latch.applies_to(1) is False


def test_score_rejects_nan() -> None:
    with pytest.raises(ValueError):
        SpeechEndDenoiseScore(math.nan)
