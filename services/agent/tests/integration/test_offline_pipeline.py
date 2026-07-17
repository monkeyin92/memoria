"""Offline ASR -> LLM -> TTS path using mocks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.orchestrator import OfflinePipeline, Orchestrator
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.deepseek import (
    DeepSeekClient,
    DeepSeekConfig,
    filter_content_for_tts,
)
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.tests.integration.mock_servers import (
    MockCosyVoiceServer,
    MockDeepSeekServer,
    MockFunASRServer,
)


@pytest.mark.asyncio
async def test_offline_asr_llm_tts() -> None:
    asr_srv = MockFunASRServer(scenario="happy")
    llm_srv = MockDeepSeekServer(scenario="happy")
    tts_srv = MockCosyVoiceServer(scenario="happy")
    asr_srv.start()
    llm_srv.start()
    tts_srv.start()
    try:
        # ASR session path
        asr = FunASRSession(FunASRConfig(api_key="t", ws_url=asr_srv.ws_url))
        await asr.connect()
        await asr.send_pcm(b"\x00\x00" * 2000)
        await asr.finish()
        user_text = "测试"
        for _ in range(20):
            try:
                ev = await asyncio.wait_for(asr.events.get(), timeout=2)
            except TimeoutError:
                break
            if ev.sentence and ev.sentence.sentence_end:
                user_text = ev.sentence.text
                break
        await asr.aclose()

        orch = Orchestrator()
        tts = CosyVoiceTTS(CosyVoiceConfig(api_key="t", ws_url=tts_srv.ws_url, pool_size=1))
        await tts.pool.warm(1)
        ds = DeepSeekClient(DeepSeekConfig(api_key="t", base_url=llm_srv.base_url))

        async def llm_stream(text: str, fence: GenerationFence) -> AsyncIterator[str]:
            async for _f, chunk in ds.stream_fast(
                [{"role": "user", "content": text}], fence=fence
            ):
                piece = filter_content_for_tts(chunk)
                if piece:
                    yield piece

        async def tts_synth(
            phrases: list[str], fence: GenerationFence, cancel: asyncio.Event
        ) -> object:
            return await tts.synthesize_stream_text(phrases, fence=fence, cancel_event=cancel)

        pipe = OfflinePipeline(orchestrator=orch, llm_stream=llm_stream, tts_synth=tts_synth)
        result = await pipe.run_turn(user_text)
        await ds.aclose()
        await tts.aclose()

        assert result["pcm_bytes"] > 0
        assert result["full_generated"]
        assert result["assistant_text"] or result["full_generated"]
    finally:
        asr_srv.stop()
        llm_srv.stop()
        tts_srv.stop()
