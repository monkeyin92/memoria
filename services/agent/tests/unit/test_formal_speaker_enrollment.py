from __future__ import annotations

import numpy as np
from services.agent.src.orchestration.formal_speaker_enrollment import FormalSpeakerEnrollment


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
