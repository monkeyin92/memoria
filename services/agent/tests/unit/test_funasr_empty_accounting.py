from __future__ import annotations

import pytest
from services.agent.src.providers.funasr_empty_accounting import classify_funasr_empty_outcome


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"rms": 50, "min_rms": 100, "pcm_gated": True}, "empty+gating"),
        ({"rms": 50, "min_rms": 100, "pcm_gated": False}, "low_rms"),
        # An explicit EmptyAudio rejection and a silent-but-accepted task are
        # different findings and must not share a bucket.
        ({"rms": 500, "min_rms": 100, "empty_audio_error": True}, "empty+vendor_error"),
        ({"rms": 500, "min_rms": 100, "empty_audio_error": False}, "empty+vendor_silent"),
        # Gating and low RMS still outrank provider attribution.
        (
            {"rms": 500, "min_rms": 100, "empty_audio_error": True, "pcm_gated": True},
            "empty+gating",
        ),
        ({"rms": 50, "min_rms": 100, "empty_audio_error": True}, "low_rms"),
    ],
)
def test_classify_funasr_empty_outcome(kwargs: dict[str, object], expected: str) -> None:
    assert classify_funasr_empty_outcome(**kwargs) == expected
