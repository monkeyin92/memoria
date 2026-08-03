"""Small adaptive-energy VAD for the media edge hot path.

It is intentionally deterministic and dependency-free.  A neural VAD can be
plugged behind the same shape later, but a fixed RMS threshold must not be the
only implementation used by a noisy family device.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

VadEventType = Literal["speech_start", "speech_end"]


@dataclass(frozen=True, slots=True)
class VADEvent:
    type: VadEventType
    sample_position: int
    probability: float
    rms: float
    noise_floor: float
    stream_epoch: int


@dataclass(frozen=True, slots=True)
class AdaptiveVADConfig:
    sample_rate: int = 16_000
    frame_ms: int = 20
    onset_frames: int = 3
    offset_frames: int = 15
    noise_ewma_alpha: float = 0.02
    speech_multiplier: float = 3.0
    min_threshold: float = 350.0
    max_threshold: float = 5_000.0

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.frame_ms <= 0:
            raise ValueError("sample_rate and frame_ms must be positive")
        if self.onset_frames <= 0 or self.offset_frames <= 0:
            raise ValueError("onset_frames and offset_frames must be positive")
        if not 0.0 < self.noise_ewma_alpha <= 1.0:
            raise ValueError("noise_ewma_alpha must be in (0, 1]")
        if self.speech_multiplier <= 1.0:
            raise ValueError("speech_multiplier must be greater than 1")
        if not 0.0 < self.min_threshold <= self.max_threshold:
            raise ValueError("VAD thresholds are invalid")

    @property
    def frame_samples(self) -> int:
        return max(1, self.sample_rate * self.frame_ms // 1000)


class AdaptiveEnergyVAD:
    """Onset/offset hysteresis over an adaptive noise floor."""

    def __init__(self, config: AdaptiveVADConfig | None = None) -> None:
        self.config = config or AdaptiveVADConfig()
        self._stream_epoch = 1
        self._noise_floor = 0.0
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._last_start_sample = 0

    @property
    def stream_epoch(self) -> int:
        return self._stream_epoch

    @property
    def noise_floor(self) -> float:
        return self._noise_floor

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def reset(self, stream_epoch: int = 1) -> None:
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        self._stream_epoch = stream_epoch
        self._noise_floor = 0.0
        self._in_speech = False
        self._speech_frames = 0
        self._silence_frames = 0
        self._last_start_sample = 0

    def process(
        self,
        samples: bytes | bytearray | memoryview | Sequence[int],
        *,
        start_sample: int,
    ) -> tuple[VADEvent, ...]:
        if start_sample < 0:
            raise ValueError("start_sample must be non-negative")
        values = self._samples(samples)
        if not values:
            return ()
        if start_sample < self._last_start_sample:
            raise ValueError("VAD sample clock moved backwards")
        self._last_start_sample = start_sample
        rms = math.sqrt(sum(value * value for value in values) / len(values))
        if self._noise_floor == 0.0:
            self._noise_floor = rms
        threshold = min(
            self.config.max_threshold,
            max(self.config.min_threshold, self._noise_floor * self.config.speech_multiplier),
        )
        is_voice = rms >= threshold
        if not is_voice and not self._in_speech:
            alpha = self.config.noise_ewma_alpha
            self._noise_floor = (1.0 - alpha) * self._noise_floor + alpha * rms
        probability = max(0.0, min(1.0, rms / max(threshold, 1.0)))
        end_sample = start_sample + len(values)
        events: list[VADEvent] = []
        if is_voice:
            self._speech_frames += 1
            self._silence_frames = 0
            if not self._in_speech and self._speech_frames >= self.config.onset_frames:
                self._in_speech = True
                onset = max(
                    0,
                    end_sample - self.config.onset_frames * self.config.frame_samples,
                )
                events.append(
                    VADEvent(
                        type="speech_start",
                        sample_position=onset,
                        probability=probability,
                        rms=rms,
                        noise_floor=self._noise_floor,
                        stream_epoch=self._stream_epoch,
                    )
                )
        else:
            self._speech_frames = 0
            if self._in_speech:
                self._silence_frames += 1
                if self._silence_frames >= self.config.offset_frames:
                    self._in_speech = False
                    self._silence_frames = 0
                    events.append(
                        VADEvent(
                            type="speech_end",
                            sample_position=end_sample,
                            probability=probability,
                            rms=rms,
                            noise_floor=self._noise_floor,
                            stream_epoch=self._stream_epoch,
                        )
                    )
        return tuple(events)

    @staticmethod
    def _samples(samples: bytes | bytearray | memoryview | Sequence[int]) -> tuple[int, ...]:
        if isinstance(samples, (bytes, bytearray, memoryview)):
            raw = bytes(samples)
            if len(raw) % 2:
                raise ValueError("VAD PCM must be 16-bit little-endian")
            return tuple(struct.unpack(f"<{len(raw) // 2}h", raw))
        return tuple(int(sample) for sample in samples)


VADEngine = AdaptiveEnergyVAD

__all__ = ["AdaptiveEnergyVAD", "AdaptiveVADConfig", "VADEngine", "VADEvent"]
