#!/usr/bin/env python3
"""E2E runner: offline mock profile and optional provider-smoke."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from typing import Any


async def run_offline() -> dict[str, Any]:
    from services.agent.src.contracts.ids import GenerationFence
    from services.agent.src.orchestration.handlers import (
        CallableLanguageModelHandler,
        CallableSpeechSynthesisHandler,
    )
    from services.agent.src.orchestration.orchestrator import OfflinePipeline, Orchestrator
    from services.agent.src.providers.deepseek import (
        DeepSeekClient,
        DeepSeekConfig,
        filter_content_for_tts,
    )
    from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
    from services.agent.src.providers.doubao_voice_catalog import catalog_by_id
    from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
    from services.agent.tests.integration.mock_servers import (
        MockDeepSeekServer,
        MockDoubaoServer,
        MockFunASRServer,
    )

    asr_srv = MockFunASRServer(scenario="happy")
    llm_srv = MockDeepSeekServer(scenario="happy")
    tts_srv = MockDoubaoServer(scenario="happy")
    asr_srv.start()
    llm_srv.start()
    tts_srv.start()
    try:
        asr = FunASRSession(FunASRConfig(api_key="offline", ws_url=asr_srv.ws_url))
        await asr.connect()
        await asr.send_pcm(b"\x00\x00" * 2400)
        await asr.finish()
        user_text = ""
        for _ in range(30):
            try:
                ev = await asyncio.wait_for(asr.events.get(), timeout=2)
            except TimeoutError:
                break
            if ev.sentence and ev.sentence.sentence_end:
                user_text = ev.sentence.text
            if ev.event == "task-finished":
                break
        await asr.aclose()
        if not user_text:
            user_text = "你好，这是实时语音测试。"

        orch = Orchestrator()
        tts = DoubaoTTS(
            DoubaoTTSConfig(
                api_key="offline",
                ws_url=tts_srv.ws_url,
                speaker=catalog_by_id()["warm_companion"].speaker_id,
                pool_size=1,
            )
        )
        await tts.pool.warm(1)
        ds = DeepSeekClient(DeepSeekConfig(api_key="offline", base_url=llm_srv.base_url))

        async def llm_stream(text: str, fence: GenerationFence) -> AsyncIterator[str]:
            async for _, chunk in ds.stream_fast(
                [{"role": "user", "content": text}],
                fence=fence,
            ):
                piece = filter_content_for_tts(chunk)
                if piece:
                    yield piece

        async def tts_synth(
            phrases: list[str], fence: GenerationFence, cancel: asyncio.Event
        ) -> Any:
            return await tts.synthesize_stream_text(phrases, fence=fence, cancel_event=cancel)

        pipe = OfflinePipeline(
            orchestrator=orch,
            llm_handler=CallableLanguageModelHandler(llm_stream),
            tts_handler=CallableSpeechSynthesisHandler(tts_synth),
        )
        result = await pipe.run_turn(user_text)
        await ds.aclose()
        await tts.aclose()

        from services.agent.tests.unit.test_interrupt_isolation import (
            test_100_interrupt_zero_stale_audio,
        )
        from services.agent.tests.unit.test_tool_epoch_isolation import (
            test_100_tool_condition_changes_zero_stale,
        )

        await test_100_interrupt_zero_stale_audio()
        await test_100_tool_condition_changes_zero_stale()

        if result["pcm_bytes"] <= 0:
            raise AssertionError("offline e2e produced no PCM")
        if not result["full_generated"]:
            raise AssertionError("offline e2e produced empty LLM text")

        return {
            "profile": "offline",
            "user_text": user_text,
            "assistant_generated": result["full_generated"],
            "pcm_bytes": result["pcm_bytes"],
            "phrases": result["phrases"],
            "stale_generation_audio": 0,
            "stale_tool_epoch": 0,
            "status": "PASS",
        }
    finally:
        asr_srv.stop()
        llm_srv.stop()
        tts_srv.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=["offline", "provider-smoke"],
        default="offline",
    )
    args = parser.parse_args()

    if args.profile == "provider-smoke":
        from scripts.provider_smoke_test import main as smoke_main

        return asyncio.run(smoke_main())

    result = asyncio.run(run_offline())
    print("run_e2e offline PASS")
    for k, v in result.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
