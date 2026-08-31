from __future__ import annotations

import pytest

from services.agent.src.providers.funasr_empty_accounting import classify_funasr_empty_outcome


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"rms": 50, "min_rms": 100, "pcm_gated": True}, "empty+gating"),
        ({"rms": 50, "min_rms": 100, "pcm_gated": False}, "low_rms"),
        ({"rms": 500, "min_rms": 100, "empty_audio_error": True}, "empty+vendor"),
        ({"rms": 500, "min_rms": 100, "empty_audio_error": False}, "empty+vendor"),
    ],
)
def test_classify_funasr_empty_outcome(kwargs: dict[str, object], expected: str) -> None:
    assert classify_funasr_empty_outcome(**kwargs) == expected
