"""Device-side PCM segment collection for formal speaker enrollment."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from services.agent.src.orchestration.speaker_verify import speech_ms_from_pcm
from services.agent.src.prompts import BRIDGE_PHRASES, SPEAKER_ENROLLMENT_SAMPLE_PROMPTS
from services.agent.src.speaker_authority_client import (
    SpeakerAuthorityClient,
    SpeakerEnrollmentSample,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FormalSpeakerEnrollment:
    """Collect bounded endpointed samples without creating an identity locally.

    The collector only frames PCM.  Quality, anti-spoof checks, template
    creation, and activation remain owned by the control-plane authority.
    """

    target_samples: int = 4
    min_speech_ms: int = 400
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


async def run_formal_speaker_enrollment(
    *,
    runtime: Any,
    speak: Callable[[str], Awaitable[None]],
    authority: SpeakerAuthorityClient,
    intent_id: str,
    sample_count: int = 4,
    sample_timeout_s: float = 15.0,
) -> dict[str, Any]:
    """Collect device endpoint samples and submit one formal enrollment.

    The device supplies PCM through the existing Agent audio path. The control
    plane resolves account identity from ``session_id`` and owns templates.
    """

    if not 3 <= sample_count <= 4:
        raise ValueError("formal speaker enrollment currently supports 3 or 4 samples")
    if sample_count > len(SPEAKER_ENROLLMENT_SAMPLE_PROMPTS):
        raise ValueError("formal speaker enrollment is missing a prompt")
    samples: asyncio.Queue[tuple[bytes, int]] = asyncio.Queue(maxsize=sample_count)
    collected: list[tuple[bytes, int]] = []

    async def _receive_sample(pcm: bytes, sample_rate: int) -> None:
        if samples.full():
            return
        await samples.put((pcm, sample_rate))

    runtime.set_formal_speaker_enrollment_sample_sink(_receive_sample)
    runtime.begin_formal_speaker_enrollment(target_samples=sample_count)
    try:
        for index in range(sample_count):
            await speak(SPEAKER_ENROLLMENT_SAMPLE_PROMPTS[index])
            sample: tuple[bytes, int] | None = None
            for attempt in range(2):
                try:
                    sample = await asyncio.wait_for(samples.get(), timeout=sample_timeout_s)
                    break
                except TimeoutError:
                    if attempt == 0:
                        await speak(BRIDGE_PHRASES[2])
            if sample is None:
                runtime.publish_formal_speaker_enrollment_result(
                    accepted=False,
                    reason="sample_timeout",
                )
                return {"status": "failed", "reason": "sample_timeout"}
            collected.append(sample)
        payload = await authority.enroll(
            session_id=runtime.session_id,
            intent_id=intent_id,
            samples=[
                SpeakerEnrollmentSample(pcm=pcm, sample_rate=sample_rate)
                for pcm, sample_rate in collected
            ],
        )
        runtime.publish_formal_speaker_enrollment_result(
            accepted=True,
            reason="submitted",
            profile_id=str(payload.get("profile_id")),
            status=str(payload.get("status")),
        )
        return payload
    except Exception:
        logger.warning(
            "formal speaker enrollment failed session_id=%s",
            runtime.session_id,
            exc_info=True,
        )
        runtime.publish_formal_speaker_enrollment_result(
            accepted=False,
            reason="authority_error",
        )
        return {"status": "failed", "reason": "authority_error"}
    finally:
        runtime.end_formal_speaker_enrollment(reason="completed")


__all__ = ["FormalSpeakerEnrollment", "run_formal_speaker_enrollment"]
