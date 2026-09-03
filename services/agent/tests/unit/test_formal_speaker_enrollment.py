from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from services.agent.src.orchestration.formal_speaker_enrollment import (
    FormalSpeakerEnrollment,
    run_formal_speaker_enrollment,
)
from services.agent.src.prompts import SPEAKER_ENROLLMENT_SAMPLE_PROMPTS


def _pcm(seconds: float, sample_rate: int = 16_000) -> bytes:
    samples = np.full(int(seconds * sample_rate), 2_000, dtype=np.int16)
    return samples.tobytes()


def test_formal_enrollment_collects_four_bounded_endpoint_samples() -> None:
    collector = FormalSpeakerEnrollment(sample_rate=16_000)
    collector.begin()

    accepted = [collector.add_endpoint(_pcm(1.5)) for _ in range(4)]

    assert all(item for item in accepted)
    assert collector.complete is True
    assert collector.active is False
    assert collector.sample_count == 4
    assert all(len(item) <= 16_000 * 2 * 6 for item in collector.samples())


def test_formal_enrollment_rejects_short_and_odd_pcm() -> None:
    collector = FormalSpeakerEnrollment(sample_rate=16_000)
    collector.begin()

    assert collector.add_endpoint(_pcm(0.2)) is None
    assert collector.add_endpoint(b"\x00") is None
    assert collector.sample_count == 0


class _FakeEnrollmentRuntime:
    def __init__(self) -> None:
        self.session_id = "session-enroll"
        self.sink: Any = None
        self.results: list[dict[str, Any]] = []
        self._collector = FormalSpeakerEnrollment()

    def set_formal_speaker_enrollment_sample_sink(self, sink: Any) -> None:
        self.sink = sink

    def begin_formal_speaker_enrollment(self, *, target_samples: int = 4) -> None:
        self._collector = FormalSpeakerEnrollment(target_samples=target_samples)
        self._collector.begin()

    def end_formal_speaker_enrollment(self, *, reason: str = "completed") -> None:
        self._collector.cancel()
        self.sink = None

    def publish_formal_speaker_enrollment_result(self, **payload: Any) -> None:
        self.results.append(payload)


class _FakeEnrollmentAuthority:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enroll(self, **payload: Any) -> dict[str, str]:
        self.calls.append(payload)
        return {"profile_id": "profile-shadow", "status": "shadow"}


@pytest.mark.asyncio
async def test_run_formal_enrollment_speaks_four_prompts_and_submits() -> None:
    runtime = _FakeEnrollmentRuntime()
    authority = _FakeEnrollmentAuthority()
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)
        assert runtime.sink is not None
        await runtime.sink(_pcm(1.5), 16_000)

    payload = await run_formal_speaker_enrollment(
        runtime=runtime,
        speak=speak,
        authority=authority,  # type: ignore[arg-type]
        intent_id="intent-1",
    )

    assert spoken == list(SPEAKER_ENROLLMENT_SAMPLE_PROMPTS)
    assert payload == {"profile_id": "profile-shadow", "status": "shadow"}
    assert authority.calls[0]["session_id"] == "session-enroll"
    assert authority.calls[0]["intent_id"] == "intent-1"
    assert len(authority.calls[0]["samples"]) == 4
    assert runtime.results[-1]["accepted"] is True
    assert runtime.sink is None


@pytest.mark.asyncio
async def test_run_formal_enrollment_times_out_without_sample() -> None:
    runtime = _FakeEnrollmentRuntime()
    authority = _FakeEnrollmentAuthority()

    async def speak(_text: str) -> None:
        return None

    payload = await run_formal_speaker_enrollment(
        runtime=runtime,
        speak=speak,
        authority=authority,  # type: ignore[arg-type]
        intent_id="intent-1",
        sample_timeout_s=0.01,
    )

    assert payload == {"status": "failed", "reason": "sample_timeout"}
    assert authority.calls == []
    assert runtime.results[-1] == {"accepted": False, "reason": "sample_timeout"}
    assert runtime.sink is None
