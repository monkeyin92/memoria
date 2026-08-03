"""Reference Linux audio front-end with a sample-clocked NLMS AEC.

This is a deterministic development implementation, useful on a Linux SBC
and in replay tests.  It does not pretend that a generic microphone can solve
room acoustics by itself: production hardware must feed the actual speaker
PCM into ``ingest_playback_reference`` and still be calibrated in situ.
"""

from __future__ import annotations

import math
import struct
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AudioDeviceConfig:
    sample_rate: int = 16_000
    channels: int = 1
    frame_ms: int = 20
    aec_taps: int = 128
    aec_step: float = 0.2
    aec_leakage: float = 1e-5

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.channels != 1 or self.frame_ms <= 0:
            raise ValueError("the reference pipeline currently supports mono PCM")
        if self.aec_taps <= 0 or self.aec_taps > 2048:
            raise ValueError("aec_taps must be between 1 and 2048")
        if not 0.0 < self.aec_step <= 2.0:
            raise ValueError("aec_step must be in (0, 2]")
        if not 0.0 <= self.aec_leakage < 1.0:
            raise ValueError("aec_leakage must be in [0, 1)")

    @property
    def frame_samples(self) -> int:
        return self.sample_rate * self.frame_ms // 1000


class NLMSAcousticEchoCanceller:
    """Normalized least-mean-squares adaptive echo subtraction."""

    def __init__(self, *, taps: int = 128, step: float = 0.2, leakage: float = 1e-5) -> None:
        if taps <= 0 or not 0.0 < step <= 2.0 or not 0.0 <= leakage < 1.0:
            raise ValueError("invalid NLMS configuration")
        self.taps = taps
        self.step = step
        self.leakage = leakage
        self._weights = [0.0] * taps
        self._reference = deque([0.0] * taps, maxlen=taps)

    def reset(self) -> None:
        self._weights = [0.0] * self.taps
        self._reference = deque([0.0] * self.taps, maxlen=self.taps)

    def process(self, microphone: Sequence[float], reference: Sequence[float]) -> tuple[float, ...]:
        if len(microphone) != len(reference):
            raise ValueError("microphone and playback reference must have equal length")
        output: list[float] = []
        for desired, reference_sample in zip(microphone, reference, strict=True):
            if not math.isfinite(desired) or not math.isfinite(reference_sample):
                raise ValueError("AEC samples must be finite")
            self._reference.appendleft(float(reference_sample))
            vector = tuple(self._reference)
            estimated = sum(weight * sample for weight, sample in zip(self._weights, vector, strict=True))
            error = float(desired) - estimated
            energy = 1e-6 + sum(sample * sample for sample in vector)
            gain = self.step * error / energy
            self._weights = [
                (1.0 - self.leakage) * weight + gain * sample
                for weight, sample in zip(self._weights, vector, strict=True)
            ]
            output.append(error)
        return tuple(output)


@dataclass(frozen=True, slots=True)
class DevicePcmFrame:
    stream_epoch: int
    sequence: int
    capture_start_sample: int
    samples: tuple[int, ...]
    aec_applied: bool = True

    def __post_init__(self) -> None:
        if self.stream_epoch < 1 or self.sequence < 0 or self.capture_start_sample < 0:
            raise ValueError("device frame metadata is invalid")
        if not self.samples:
            raise ValueError("device frame must contain samples")
        if any(sample < -32768 or sample > 32767 for sample in self.samples):
            raise ValueError("PCM sample is outside int16 range")

    @property
    def capture_end_sample(self) -> int:
        return self.capture_start_sample + len(self.samples)

    def to_pcm_s16le(self) -> bytes:
        return struct.pack(f"<{len(self.samples)}h", *self.samples)


@dataclass(slots=True)
class LinuxAudioPipeline:
    """Sample-clocked capture/playback reference pairing for a Linux device."""

    config: AudioDeviceConfig = field(default_factory=AudioDeviceConfig)
    stream_epoch: int = 1
    _capture_sample: int = 0
    _sequence: int = 0
    _playback: deque[tuple[int, tuple[float, ...]]] = field(default_factory=deque)
    _aec: NLMSAcousticEchoCanceller = field(init=False)

    def __post_init__(self) -> None:
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        self._aec = NLMSAcousticEchoCanceller(
            taps=self.config.aec_taps,
            step=self.config.aec_step,
            leakage=self.config.aec_leakage,
        )

    def reset_stream(self, stream_epoch: int) -> None:
        if stream_epoch <= self.stream_epoch:
            raise ValueError("stream_epoch must advance")
        self.stream_epoch = stream_epoch
        self._capture_sample = 0
        self._sequence = 0
        self._playback.clear()
        self._aec.reset()

    def ingest_playback_reference(self, start_sample: int, samples: Sequence[int | float]) -> None:
        if start_sample < 0:
            raise ValueError("playback start sample must be non-negative")
        values = tuple(float(sample) for sample in samples)
        if not values or any(not math.isfinite(sample) for sample in values):
            raise ValueError("playback reference must contain finite samples")
        self._playback.append((start_sample, values))
        while len(self._playback) > 64:
            self._playback.popleft()

    def capture(self, samples: Sequence[int | float]) -> DevicePcmFrame:
        microphone = tuple(float(sample) for sample in samples)
        if not microphone:
            raise ValueError("capture frame must contain samples")
        if any(not math.isfinite(sample) or abs(sample) > 32768 for sample in microphone):
            raise ValueError("capture sample is invalid")
        start = self._capture_sample
        reference = self._reference_for(start, len(microphone))
        cleaned = self._aec.process(microphone, reference)
        output = tuple(max(-32768, min(32767, int(round(sample)))) for sample in cleaned)
        frame = DevicePcmFrame(
            stream_epoch=self.stream_epoch,
            sequence=self._sequence,
            capture_start_sample=start,
            samples=output,
        )
        self._capture_sample += len(microphone)
        self._sequence += 1
        return frame

    def _reference_for(self, start_sample: int, length: int) -> tuple[float, ...]:
        end_sample = start_sample + length
        for ref_start, values in reversed(self._playback):
            ref_end = ref_start + len(values)
            if ref_start <= start_sample and ref_end >= end_sample:
                offset = start_sample - ref_start
                return values[offset : offset + length]
        return (0.0,) * length


__all__ = [
    "AudioDeviceConfig",
    "DevicePcmFrame",
    "LinuxAudioPipeline",
    "NLMSAcousticEchoCanceller",
]
