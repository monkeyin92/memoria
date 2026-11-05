from __future__ import annotations

from services.common.speech_end import speech_end_monotonic


def test_speech_end_is_backdated_to_the_last_voiced_sample() -> None:
    assert (
        speech_end_monotonic(received_at=10.0, event_sample=80_000, voiced_end_sample=72_000) == 9.5
    )


def test_speech_end_without_an_earlier_voiced_sample_is_the_receipt() -> None:
    for voiced_end_sample in (None, 80_000, 96_000):
        assert (
            speech_end_monotonic(
                received_at=10.0, event_sample=80_000, voiced_end_sample=voiced_end_sample
            )
            == 10.0
        )
