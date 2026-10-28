from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SpeechEndDenoiseScore:
    summary: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.summary):
            raise ValueError("speech-end denoise summary must be finite")


@dataclass(frozen=True, slots=True)
class SpeechEndDenoiseLatch:
    endpoint_sample: int
    score: SpeechEndDenoiseScore

    def applies_to(self, endpoint_sample: int | None) -> bool:
        return endpoint_sample is not None and endpoint_sample == self.endpoint_sample


@dataclass(frozen=True, slots=True)
class _ScoreIdle:
    total_sum: float = 0.0
    total_count: int = 0


@dataclass(frozen=True, slots=True)
class _ScoreOpen:
    total_sum: float
    total_count: int
    baseline_sum: float
    baseline_count: int


@dataclass(frozen=True, slots=True)
class _ScoreFrozen:
    total_sum: float
    total_count: int
    score: SpeechEndDenoiseScore | None


ScoreWindow = _ScoreIdle | _ScoreOpen | _ScoreFrozen


def add_shift_mean(window: ScoreWindow, bin_mean: float) -> ScoreWindow:
    if not math.isfinite(bin_mean):
        return window
    total_sum = window.total_sum + bin_mean
    total_count = window.total_count + 1
    if isinstance(window, _ScoreOpen):
        return _ScoreOpen(
            total_sum,
            total_count,
            window.baseline_sum,
            window.baseline_count,
        )
    if isinstance(window, _ScoreFrozen):
        return _ScoreFrozen(total_sum, total_count, window.score)
    return _ScoreIdle(total_sum, total_count)


def mark_utterance(window: ScoreWindow) -> _ScoreOpen:
    return _ScoreOpen(
        window.total_sum,
        window.total_count,
        window.total_sum,
        window.total_count,
    )


def freeze_utterance(
    window: ScoreWindow,
) -> tuple[ScoreWindow, SpeechEndDenoiseScore | None]:
    if isinstance(window, _ScoreIdle):
        return window, None
    if isinstance(window, _ScoreFrozen):
        return window, window.score
    counted = window.total_count - window.baseline_count
    if counted == 0:
        score = None
    else:
        mean = (window.total_sum - window.baseline_sum) / counted
        score = SpeechEndDenoiseScore(mean) if math.isfinite(mean) else None
    frozen = _ScoreFrozen(window.total_sum, window.total_count, score)
    return frozen, score


__all__ = [
    "SpeechEndDenoiseLatch",
    "SpeechEndDenoiseScore",
    "add_shift_mean",
    "freeze_utterance",
    "mark_utterance",
]
