"""Measure a submitted voice sample before any provider work happens.

The consumer enrollment path used to record a passing evaluation and a passing
objective quality measurement without measuring anything (see
``_ready_profile_for_device`` in ``services/control_api/app/routes/voice.py``).
Those two gates are human A/B preference and TTS-output probes, so a
mini-program user can never honestly satisfy them.

This module is the honest replacement for the sample itself: it decodes the
submitted audio with PyAV (already a project dependency, the same one
``services/device_media_gateway/opus.py`` decodes with) and reports what the
recording actually contains, so a silent, clipped, too short or non-audio file
is rejected inside the same request instead of becoming an unusable voice on
the device.

Everything here is pure and local: no provider call, no storage write, no
network. A 10-60 second sample measures in well under a second.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Literal, cast

import av
import numpy as np
from av.container.input import InputContainer

SampleRejection = Literal[
    "audio_decode_failed",
    "audio_too_short",
    "audio_too_long",
    "audio_low_sample_rate",
    "audio_silent",
    "speech_too_short",
    "audio_clipped",
]

#: Measured duration the product accepts. The upper bound matches the
#: enrollment contract; the lower bound matches the recorded-sample minimum.
MIN_SAMPLE_DURATION_MS = 10_000
MAX_SAMPLE_DURATION_MS = 60_000
#: Containers carry a little padding past the advertised length.
MAX_SAMPLE_DURATION_SLACK_MS = 2_000
MIN_SAMPLE_RATE_HZ = 16_000
#: Below this overall level the recording is silence, not a quiet voice.
MIN_RMS_DBFS = -45.0
#: A 20 ms frame below this level counts as silence.
SILENCE_FRAME_DBFS = -45.0
FRAME_MS = 20
#: Speech is what the provider clones; a mostly-silent file clones nothing.
MIN_SPEECH_MS = 8_000
#: A handful of clipped samples is normal in a phone recording; a large
#: share of them means the input gain was wrong and the clone is unusable.
MAX_CLIPPED_RATIO = 0.05
#: ``|x|`` at or above this counts as a clipped sample.
CLIP_MAGNITUDE = 0.985
#: Decoding stops here: a decompression bomb must not become a memory bomb.
MAX_DECODED_MS = 300_000
_FLOOR_DBFS = -120.0


@dataclass(frozen=True, slots=True)
class VoiceSampleMetrics:
    """What the submitted audio actually contains, all values measured."""

    duration_ms: int
    sample_rate: int
    channels: int
    rms_dbfs: float
    peak_dbfs: float
    clipped_ratio: float
    silence_ratio: float
    speech_ms: int
    dc_offset: float

    def to_dict(self) -> dict[str, object]:
        return {
            "duration_ms": self.duration_ms,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "rms_dbfs": round(self.rms_dbfs, 2),
            "peak_dbfs": round(self.peak_dbfs, 2),
            "clipped_ratio": round(self.clipped_ratio, 6),
            "silence_ratio": round(self.silence_ratio, 4),
            "speech_ms": self.speech_ms,
            "dc_offset": round(self.dc_offset, 6),
        }


@dataclass(frozen=True, slots=True)
class VoiceSampleValidation:
    """The measurement plus the verdict derived from it.

    ``admitted`` is the delivery decision: only a consumer enrollment asks the
    submitted recording to be the admission evidence, so a lab enrollment
    measures the same audio without being admitted by it.
    """

    passed: bool
    reasons: tuple[SampleRejection, ...]
    metrics: VoiceSampleMetrics | None
    admitted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "admitted": self.admitted,
            "reasons": list(self.reasons),
            "metrics": self.metrics.to_dict() if self.metrics is not None else None,
        }


def _dbfs(magnitude: float) -> float:
    if magnitude <= 0.0:
        return _FLOOR_DBFS
    return max(_FLOOR_DBFS, 20.0 * math.log10(magnitude))


def _decode_to_mono(audio: bytes) -> tuple[np.ndarray, int, int]:
    """Decode any supported container to mono float32 at its source rate.

    Returns ``(samples, sample_rate, source_channels)``. Raises ``ValueError``
    for anything PyAV cannot open or that carries no audio stream.
    """
    chunks: list[np.ndarray] = []
    resampler = av.AudioResampler(format="fltp", layout="mono")
    sample_rate = 0
    channels = 0
    decoded_samples = 0
    limit = 0
    try:
        container = av.open(io.BytesIO(audio))
    except (av.error.FFmpegError, OSError, EOFError) as exc:
        raise ValueError("the sample could not be decoded") from exc
    input_container = cast(InputContainer, container)
    try:
        with input_container:
            stream = next(
                (
                    item
                    for item in input_container.streams
                    if isinstance(item, av.AudioStream)
                ),
                None,
            )
            if stream is None:
                raise ValueError("the sample contains no audio stream")
            sample_rate = int(stream.rate or 0)
            layout = stream.layout
            channels = len(layout.channels) if layout is not None else 1
            if sample_rate <= 0:
                raise ValueError("the sample declares no sample rate")
            limit = sample_rate * MAX_DECODED_MS // 1000
            for decoded in input_container.decode(stream):
                if not isinstance(decoded, av.AudioFrame):
                    continue
                if decoded.sample_rate:
                    sample_rate = int(decoded.sample_rate)
                for resampled in resampler.resample(decoded):
                    block = np.asarray(resampled.to_ndarray(), dtype=np.float32)
                    chunks.append(block.reshape(-1))
                    decoded_samples += block.shape[-1]
                if decoded_samples >= limit:
                    break
            for resampled in resampler.resample(None):
                block = np.asarray(resampled.to_ndarray(), dtype=np.float32)
                chunks.append(block.reshape(-1))
    except (av.error.FFmpegError, OSError, EOFError) as exc:
        raise ValueError("the sample could not be decoded") from exc
    if not chunks:
        raise ValueError("the sample decoded to no audio")
    samples = np.concatenate(chunks)
    if samples.size == 0:
        raise ValueError("the sample decoded to no audio")
    return samples, sample_rate, channels


def _measure(samples: np.ndarray, sample_rate: int, channels: int) -> VoiceSampleMetrics:
    magnitude = np.abs(samples)
    frame_length = max(1, sample_rate * FRAME_MS // 1000)
    usable = samples.size - (samples.size % frame_length)
    if usable:
        frames = samples[:usable].reshape(-1, frame_length)
        frame_rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
        loud = frame_rms > 10.0 ** (SILENCE_FRAME_DBFS / 20.0)
        silence_ratio = float(1.0 - loud.mean())
        speech_ms = int(loud.sum()) * FRAME_MS
    else:
        silence_ratio = 1.0
        speech_ms = 0
    return VoiceSampleMetrics(
        duration_ms=int(round(samples.size / sample_rate * 1000)),
        sample_rate=sample_rate,
        channels=channels,
        rms_dbfs=_dbfs(float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))),
        peak_dbfs=_dbfs(float(magnitude.max())),
        clipped_ratio=float(np.count_nonzero(magnitude >= CLIP_MAGNITUDE) / samples.size),
        silence_ratio=silence_ratio,
        speech_ms=speech_ms,
        dc_offset=float(np.mean(samples, dtype=np.float64)),
    )


def _reasons(metrics: VoiceSampleMetrics) -> tuple[SampleRejection, ...]:
    """Every measured reason this sample cannot be cloned, in a stable order."""
    found: list[SampleRejection] = []
    if metrics.duration_ms < MIN_SAMPLE_DURATION_MS:
        found.append("audio_too_short")
    if metrics.duration_ms > MAX_SAMPLE_DURATION_MS + MAX_SAMPLE_DURATION_SLACK_MS:
        found.append("audio_too_long")
    if metrics.sample_rate < MIN_SAMPLE_RATE_HZ:
        found.append("audio_low_sample_rate")
    if metrics.rms_dbfs < MIN_RMS_DBFS:
        found.append("audio_silent")
    elif metrics.speech_ms < MIN_SPEECH_MS:
        found.append("speech_too_short")
    if metrics.clipped_ratio > MAX_CLIPPED_RATIO:
        found.append("audio_clipped")
    return tuple(found)


def validate_voice_sample(
    audio: bytes, *, admit_on_pass: bool = False
) -> VoiceSampleValidation:
    """Decode, measure and judge one submitted voice sample.

    An undecodable file is a rejection, never an exception: the caller turns
    the reasons into user-facing copy.

    ``admit_on_pass`` marks the consumer path, where this measurement is the
    delivery evidence. A lab enrollment leaves it off: it still gets the
    measurement, but admission stays with its own A/B and quality gates.
    """
    try:
        samples, sample_rate, channels = _decode_to_mono(audio)
    except ValueError:
        return VoiceSampleValidation(
            passed=False,
            reasons=("audio_decode_failed",),
            metrics=None,
            admitted=False,
        )
    metrics = _measure(samples, sample_rate, channels)
    reasons = _reasons(metrics)
    passed = not reasons
    return VoiceSampleValidation(
        passed=passed,
        reasons=reasons,
        metrics=metrics,
        admitted=passed and admit_on_pass,
    )
