from __future__ import annotations

from services.agent.src.contracts.events import TimedWord
from services.agent.src.providers.cosyvoice_protocol import (
    pcm_duration_ms,
    scale_word_timestamps,
)


def test_pcm_duration() -> None:
    # 24000 Hz * 0.1s * 2 bytes = 4800 bytes
    pcm = b"\x00" * 4800
    assert pcm_duration_ms(pcm, sample_rate=24000) == 100


def test_scale_ok() -> None:
    words = (TimedWord(text="你", begin_ms=0, end_ms=100), TimedWord(text="好", begin_ms=100, end_ms=200))
    scaled, status = scale_word_timestamps(words, pcm_duration_ms_value=200, offset_ms=50)
    assert status == "ok"
    assert scaled[0].begin_ms == 50
    assert scaled[-1].end_ms == 250


def test_trailing_silence_keeps_word_timestamps_unscaled() -> None:
    # Measured on qwen-audio-3.1-tts-flash (longwan_v3.1): last word ends at
    # 2320 ms, the sentence PCM runs to 2770 ms.
    words = (
        TimedWord(text="我", begin_ms=240, end_ms=480),
        TimedWord(text="好。", begin_ms=2080, end_ms=2320),
    )
    scaled, status = scale_word_timestamps(words, pcm_duration_ms_value=2770, offset_ms=1000)
    assert status == "ok"
    assert [(w.begin_ms, w.end_ms) for w in scaled] == [(1240, 1480), (3080, 3320)]


def test_overrunning_timestamps_are_stretched_onto_pcm() -> None:
    words = (TimedWord(text="你", begin_ms=0, end_ms=200), TimedWord(text="好", begin_ms=200, end_ms=400))
    scaled, status = scale_word_timestamps(words, pcm_duration_ms_value=200, offset_ms=0)
    assert status == "scaled"
    assert scaled[-1].end_ms == 200


def test_degraded_large_error_still_scales_to_pcm() -> None:
    words = (TimedWord(text="你", begin_ms=0, end_ms=100),)
    scaled, status = scale_word_timestamps(words, pcm_duration_ms_value=1000)
    assert status == "degraded"
    assert scaled[-1].end_ms == 1000


def test_words_ending_far_before_audio_stay_degraded() -> None:
    words = (TimedWord(text="你", begin_ms=0, end_ms=500),)
    _scaled, status = scale_word_timestamps(words, pcm_duration_ms_value=1400)
    assert status == "degraded"
