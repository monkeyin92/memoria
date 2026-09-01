"""Classify FunASR empty-transcript outcomes for logs, metrics and receipts."""

from __future__ import annotations

from typing import Literal

FunASREmptyClass = Literal[
    "empty+vendor_error",
    "empty+vendor_silent",
    "empty+gating",
    "low_rms",
]


def classify_funasr_empty_outcome(
    *,
    rms: int,
    min_rms: int,
    empty_audio_error: bool = False,
    pcm_gated: bool = False,
) -> FunASREmptyClass:
    """Split empty ASR into vendor, uplink gating, or low-RMS buckets.

    ``empty+vendor_error`` means the provider actively rejected the audio (an
    ``EmptyAudio`` task failure); ``empty+vendor_silent`` means it accepted the
    task and simply never returned a transcript.  Keeping these apart matters:
    only the first is provider-attributable, and collapsing both into one
    bucket previously made every non-gated, non-quiet outcome look like vendor
    behaviour regardless of what the provider actually did.
    """

    if pcm_gated:
        return "empty+gating"
    if rms < min_rms:
        return "low_rms"
    if empty_audio_error:
        return "empty+vendor_error"
    return "empty+vendor_silent"
