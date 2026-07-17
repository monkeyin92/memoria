"""Unit tests for session-scoped target-speaker verification."""

from __future__ import annotations

import math
import struct

import numpy as np
from services.agent.src.orchestration.speaker_verify import (
    SpeakerVerifier,
    cosine_similarity,
    embed_pcm,
    speech_ms_from_pcm,
)


def _signal_pcm(
    *,
    kind: str,
    seconds: float,
    sample_rate: int = 16000,
    seed: int = 0,
) -> bytes:
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float32) / sample_rate
    rng = np.random.default_rng(seed)
    if kind == "owner":
        # Low-band voiced-like sum of harmonics.
        x = (
            0.22 * np.sin(2 * math.pi * 140 * t)
            + 0.12 * np.sin(2 * math.pi * 280 * t)
            + 0.08 * np.sin(2 * math.pi * 420 * t)
        )
        x += 0.03 * rng.standard_normal(n).astype(np.float32)
    elif kind == "bystander":
        # High-band noise + different formant-like tone.
        x = 0.18 * rng.standard_normal(n).astype(np.float32)
        x += 0.2 * np.sin(2 * math.pi * 1700 * t)
        x += 0.1 * np.sin(2 * math.pi * 2400 * t)
    else:
        raise ValueError(kind)
    samples = np.clip(x * 32767, -32767, 32767).astype(np.int16)
    return samples.tobytes()


def test_same_speaker_scores_high_and_bystander_scores_lower() -> None:
    owner = _signal_pcm(kind="owner", seconds=4.0, seed=1)
    same = _signal_pcm(kind="owner", seconds=1.2, seed=2)
    other = _signal_pcm(kind="bystander", seconds=1.2, seed=3)

    owner_emb = embed_pcm(owner)
    same_emb = embed_pcm(same, min_speech_ms=400)
    other_emb = embed_pcm(other, min_speech_ms=400)
    assert owner_emb is not None
    assert same_emb is not None
    assert other_emb is not None

    same_score = cosine_similarity(owner_emb, same_emb)
    other_score = cosine_similarity(owner_emb, other_emb)
    assert same_score > 0.85
    assert other_score < same_score - 0.05


def test_verifier_enrolls_then_rejects_mismatch() -> None:
    verifier = SpeakerVerifier(
        enabled=True,
        enroll_speech_ms=1200,
        enroll_timeout_ms=5000,
        accept_threshold=0.70,
        min_verify_speech_ms=400,
    )
    verifier.begin_enrollment()
    owner = _signal_pcm(kind="owner", seconds=2.0, seed=11)
    # Feed in chunks to mimic realtime PCM.
    step = 3200  # 100ms @16k mono int16
    for i in range(0, len(owner), step):
        verifier.feed_pcm(owner[i : i + step])
        result = verifier.try_finalize_enrollment()
        if result is not None:
            break
    assert verifier.is_enrolled

    verifier.mark_utterance_start()
    other = _signal_pcm(kind="bystander", seconds=1.0, seed=12)
    verifier.feed_pcm(other)
    verifier.mark_utterance_end()
    score = verifier.score_latest_utterance()
    assert score.accepted is False
    assert score.reason in {"mismatch", "embed_failed", "too_short"}

    verifier.mark_utterance_start()
    same = _signal_pcm(kind="owner", seconds=1.0, seed=13)
    verifier.feed_pcm(same)
    verifier.mark_utterance_end()
    match = verifier.score_latest_utterance()
    assert match.accepted is True
    assert match.score >= 0.70


def test_quiet_pcm_has_low_speech_ms() -> None:
    quiet = struct.pack("<" + "h" * 16000, *([0] * 16000))
    assert speech_ms_from_pcm(quiet) < 200
    assert embed_pcm(quiet) is None


def test_zero_pcm_enroll_times_out_with_wall_clock_or_force() -> None:
    """Regression: no feed_pcm used to leave PENDING forever (chat blocked)."""
    verifier = SpeakerVerifier(
        enabled=True,
        enroll_speech_ms=3500,
        enroll_timeout_ms=2000,
    )
    verifier.begin_enrollment()
    assert verifier.try_finalize_enrollment() is None
    # Wall elapsed alone must fail-open even with zero PCM.
    timed = verifier.try_finalize_enrollment(wall_elapsed_ms=2500)
    assert timed is not None
    assert timed.reason == "enroll_timeout"
    assert verifier.state.value == "open"

    verifier2 = SpeakerVerifier(enabled=True, enroll_speech_ms=3500, enroll_timeout_ms=60_000)
    verifier2.begin_enrollment()
    forced = verifier2.try_finalize_enrollment(force=True)
    assert forced is not None
    assert forced.reason == "enroll_timeout"
    assert verifier2.state.value == "open"
