"""Classify FunASR empty-transcript outcomes for logs, metrics and receipts."""

from __future__ import annotations

from typing import Literal

FunASREmptyClass = Literal["empty+vendor", "empty+gating", "low_rms"]


def classify_funasr_empty_outcome(
    *,
    rms: int,
    min_rms: int,
    empty_audio_error: bool = False,
    pcm_gated: bool = False,
) -> FunASREmptyClass:
    """Split empty ASR into vendor, uplink gating, or low-RMS buckets."""

    if pcm_gated:
        return "empty+gating"
    if rms < min_rms:
        return "low_rms"
    if empty_audio_error:
        return "empty+vendor"
    return "empty+vendor"
