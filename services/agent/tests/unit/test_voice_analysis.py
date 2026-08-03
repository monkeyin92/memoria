from __future__ import annotations

import struct

import pytest
from services.agent.src.voice_core.adaptive_vad import (
    AdaptiveEnergyVAD,
    AdaptiveVADConfig,
)
from services.agent.src.voice_core.keyword_spotter import ControlKeywordSpotter


def pcm(value: int, samples: int = 320) -> bytes:
    return struct.pack(f"<{samples}h", *([value] * samples))


def test_adaptive_vad_uses_onset_and_offset_hysteresis() -> None:
    vad = AdaptiveEnergyVAD(
        AdaptiveVADConfig(
            onset_frames=2,
            offset_frames=2,
            min_threshold=100,
            speech_multiplier=2,
        )
    )
    assert vad.process(pcm(10), start_sample=0) == ()
    assert vad.process(pcm(400), start_sample=320) == ()
    start = vad.process(pcm(400), start_sample=640)
    assert [event.type for event in start] == ["speech_start"]
    assert start[0].sample_position == 320
    assert vad.process(pcm(0), start_sample=960) == ()
    end = vad.process(pcm(0), start_sample=1280)
    assert [event.type for event in end] == ["speech_end"]
    assert end[0].stream_epoch == 1


def test_vad_rejects_backwards_sample_clock_and_bad_pcm() -> None:
    vad = AdaptiveEnergyVAD()
    vad.process(pcm(0), start_sample=100)
    with pytest.raises(ValueError, match="backwards"):
        vad.process(pcm(0), start_sample=0)
    with pytest.raises(ValueError, match="16-bit"):
        vad.process(b"\x00", start_sample=320)


def test_control_kws_is_exact_and_confidence_gated() -> None:
    kws = ControlKeywordSpotter(min_confidence=0.8)
    assert kws.process("停 一 下", start_sample=0, end_sample=320, confidence=0.9)[0].keyword == "停一下"
    assert kws.process("停一下我还有问题", start_sample=0, end_sample=320) == ()
    assert kws.process("等等", start_sample=0, end_sample=320, confidence=0.5) == ()
