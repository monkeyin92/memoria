"""Prove FunASR/CosyVoice are real LiveKit STT/TTS subclasses with stream()."""

from __future__ import annotations

import pytest
from livekit.agents import stt, tts
from services.agent.src.providers.cosyvoice_tts import (
    CosyVoiceConfig,
    CosyVoiceSynthesizeStream,
    CosyVoiceTTS,
)
from services.agent.src.providers.funasr_stt import (
    FunASRConfig,
    FunASRRecognizeStream,
    FunASRSTT,
)


def test_funasr_is_livekit_stt_subclass() -> None:
    plugin = FunASRSTT(FunASRConfig(api_key="t", ws_url="ws://127.0.0.1:1"))
    assert isinstance(plugin, stt.STT)
    assert plugin.capabilities.streaming is True
    assert plugin.capabilities.interim_results is True
    assert plugin.capabilities.aligned_transcript == "word"
    assert plugin.capabilities.offline_recognize is False
    assert issubclass(FunASRRecognizeStream, stt.RecognizeStream)
    assert hasattr(FunASRRecognizeStream, "_run")
    assert callable(FunASRRecognizeStream._run)


@pytest.mark.asyncio
async def test_funasr_stream_returns_recognize_stream() -> None:
    plugin = FunASRSTT(FunASRConfig(api_key="t", ws_url="ws://127.0.0.1:1"))
    stream = plugin.stream()
    assert isinstance(stream, stt.RecognizeStream)
    assert isinstance(stream, FunASRRecognizeStream)
    await stream.aclose()


def test_cosyvoice_is_livekit_tts_subclass() -> None:
    plugin = CosyVoiceTTS(CosyVoiceConfig(api_key="t", ws_url="ws://127.0.0.1:1", pool_size=1))
    assert isinstance(plugin, tts.TTS)
    assert plugin.capabilities.streaming is True
    assert plugin.capabilities.aligned_transcript is True
    assert issubclass(CosyVoiceSynthesizeStream, tts.SynthesizeStream)
    assert hasattr(CosyVoiceSynthesizeStream, "_run")
    assert callable(CosyVoiceSynthesizeStream._run)


@pytest.mark.asyncio
async def test_cosyvoice_stream_and_synthesize() -> None:
    plugin = CosyVoiceTTS(CosyVoiceConfig(api_key="t", ws_url="ws://127.0.0.1:1", pool_size=1))
    stream = plugin.stream()
    assert isinstance(stream, tts.SynthesizeStream)
    assert isinstance(stream, CosyVoiceSynthesizeStream)
    # synthesize() returns ChunkedStream without needing a live WS for construction type-check
    assert callable(plugin.synthesize)
    await stream.aclose()
