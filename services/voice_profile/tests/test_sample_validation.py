"""The voice-sample gate: what is measured, and what gets rejected."""

from __future__ import annotations

import time

import numpy as np
from services.voice_profile.sample_validation import (
    MAX_CLIPPED_RATIO,
    MIN_SPEECH_MS,
    validate_voice_sample,
)
from services.voice_profile.testing_audio import (
    DEFAULT_SAMPLE_RATE,
    silent,
    speech_like,
    voice_sample_mp3,
    voice_sample_wav,
    wav_bytes,
)


def test_a_normal_recording_passes_with_measured_metrics() -> None:
    result = validate_voice_sample(voice_sample_wav(15_000))
    assert result.passed is True
    assert result.reasons == ()
    metrics = result.metrics
    assert metrics is not None
    assert metrics.duration_ms == 15_000
    assert metrics.sample_rate == DEFAULT_SAMPLE_RATE
    assert metrics.channels == 1
    assert -40.0 < metrics.rms_dbfs < -10.0
    assert metrics.peak_dbfs < 0.0
    assert metrics.clipped_ratio == 0.0
    assert metrics.speech_ms >= MIN_SPEECH_MS


def test_a_compressed_sample_decodes_through_the_same_path() -> None:
    result = validate_voice_sample(voice_sample_mp3(15_000))
    assert result.passed is True
    assert result.metrics is not None
    # Lossy encoding shifts the level a little but not the substance.
    assert 12_000 <= result.metrics.duration_ms <= 15_200
    assert result.metrics.speech_ms >= MIN_SPEECH_MS


def test_silence_is_rejected_as_silence() -> None:
    result = validate_voice_sample(wav_bytes(silent()))
    assert result.passed is False
    assert result.reasons == ("audio_silent",)


def test_a_recording_that_is_mostly_silence_is_rejected_for_lack_of_speech() -> None:
    mostly_quiet = np.concatenate(
        [speech_like(duration_ms=3_000), silent(duration_ms=12_000)]
    )
    result = validate_voice_sample(wav_bytes(mostly_quiet))
    assert result.passed is False
    assert result.reasons == ("speech_too_short",)
    assert result.metrics is not None
    assert result.metrics.silence_ratio > 0.5


def test_too_short_and_too_long_are_rejected() -> None:
    short = validate_voice_sample(voice_sample_wav(4_000))
    assert short.passed is False
    assert "audio_too_short" in short.reasons

    long = validate_voice_sample(voice_sample_wav(90_000))
    assert long.passed is False
    assert long.reasons == ("audio_too_long",)


def test_clipped_audio_is_rejected() -> None:
    damaged = np.clip(speech_like(amplitude=2.0), -1.0, 1.0)
    result = validate_voice_sample(wav_bytes(damaged))
    assert result.passed is False
    assert result.metrics is not None
    assert result.metrics.clipped_ratio > MAX_CLIPPED_RATIO
    assert result.reasons == ("audio_clipped",)


def test_a_narrowband_sample_is_rejected() -> None:
    result = validate_voice_sample(wav_bytes(speech_like(), sample_rate=8_000))
    assert result.passed is False
    assert result.reasons == ("audio_low_sample_rate",)


def test_undecodable_bytes_are_a_verdict_not_an_exception() -> None:
    result = validate_voice_sample(b"RIFF" + b"\x01\x02" * 16_000)
    assert result.passed is False
    assert result.reasons == ("audio_decode_failed",)
    assert result.metrics is None

    assert validate_voice_sample(b"").reasons == ("audio_decode_failed",)


def test_the_measured_length_wins_over_what_the_client_claims() -> None:
    """A client cannot pass the duration gate by lying about duration_ms.

    The enrollment request carries a client-computed ``duration_ms``; this
    module never sees it, so a 2 s payload claiming 12 s is still short.
    """
    result = validate_voice_sample(voice_sample_wav(2_000))
    assert result.passed is False
    assert result.metrics is not None
    assert result.metrics.duration_ms < 3_000


def test_measuring_a_full_length_sample_stays_far_inside_the_promise() -> None:
    """The sample check must never be what makes the user wait a minute."""
    sample = voice_sample_wav(60_000)
    started = time.perf_counter()
    result = validate_voice_sample(sample)
    elapsed = time.perf_counter() - started
    assert result.passed is True
    assert elapsed < 2.0, f"sample validation took {elapsed:.2f}s"
