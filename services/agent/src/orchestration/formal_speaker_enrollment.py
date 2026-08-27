"""Device-side PCM segment collection for formal speaker enrollment."""

from __future__ import annotations

from dataclasses import dataclass, field

from services.agent.src.orchestration.speaker_verify import speech_ms_from_pcm


@dataclass(slots=True)
class FormalSpeakerEnrollment:
    """Collect bounded endpointed samples without creating an identity locally.

    The collector only frames PCM.  Quality, anti-spoof checks, template
    creation, and activation remain owned by the control-plane authority.
    """

    target_samples: int = 4
    min_speech_ms: int = 1200
    max_sample_ms: int = 6000
    sample_rate: int = 16000
    active: bool = False
    _samples: list[bytes] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not 3 <= self.target_samples <= 10:
            raise ValueError("formal enrollment target must be between 3 and 10 samples")
        if self.min_speech_ms <= 0 or self.max_sample_ms < self.min_speech_ms:
            raise ValueError("formal enrollment speech bounds are invalid")
        if self.sample_rate < 8000:
            raise ValueError("formal enrollment sample rate is unsupported")

    def begin(self) -> None:
        self._samples.clear()
        self.active = True

    def cancel(self) -> None:
        self._samples.clear()
        self.active = False

    @property
    def complete(self) -> bool:
        return len(self._samples) >= self.target_samples

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def add_endpoint(self, pcm: bytes) -> bytes | None:
        """Accept one VAD endpoint only when it has enough voiced speech."""

        if not self.active or self.complete or not pcm or len(pcm) % 2:
            return None
        max_bytes = self.sample_rate * 2 * self.max_sample_ms // 1000
        bounded = pcm[:max_bytes]
        if speech_ms_from_pcm(bounded, sample_rate=self.sample_rate) < self.min_speech_ms:
            return None
        sample = bytes(bounded)
        self._samples.append(sample)
        if self.complete:
            self.active = False
        return sample

    def samples(self) -> tuple[bytes, ...]:
        return tuple(self._samples)


__all__ = ["FormalSpeakerEnrollment"]
