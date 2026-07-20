#!/usr/bin/env python3
"""Live provider acceptance checks; skip only when provider credentials are absent."""

from __future__ import annotations

import asyncio
import audioop
import os
import sys

from services.agent.src.config import AgentSettings
from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.cosyvoice_protocol import pcm_duration_ms
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.deepseek import (
    DeepSeekClient,
    DeepSeekConfig,
    filter_content_for_tts,
)
from services.agent.src.providers.funasr_protocol import timestamps_monotonic
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession

DEFAULT_DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def _dashscope_ws_url() -> str:
    return os.getenv("DASHSCOPE_WS_URL", DEFAULT_DASHSCOPE_WS_URL)


def _validate_cosyvoice_result(
    pcm: bytes, words: tuple[TimedWord, ...], sample_rate: int
) -> None:
    if not pcm:
        raise AssertionError("CosyVoice returned no PCM")
    if not words:
        raise AssertionError("CosyVoice returned no word timestamps")
    previous = -1
    for word in words:
        begin_ms = word.begin_ms
        end_ms = word.end_ms
        if begin_ms < previous or end_ms < begin_ms:
            raise AssertionError("CosyVoice word timestamps are not monotonic")
        previous = end_ms
    duration_ms = pcm_duration_ms(pcm, sample_rate=sample_rate)
    if abs(previous - duration_ms) > 300:
        raise AssertionError(
            f"CosyVoice alignment differs from PCM duration by {abs(previous - duration_ms)} ms"
        )


async def smoke_cosyvoice() -> bytes:
    # Resolve designed profiles (v3.5) or system longanyang (v3-flash).
    env = {
        **os.environ,
        "DASHSCOPE_API_KEY": os.environ["DASHSCOPE_API_KEY"],
        "DASHSCOPE_WS_URL": _dashscope_ws_url(),
        "COSYVOICE_POOL_SIZE": "1",
    }
    cfg = CosyVoiceConfig.from_env(env)
    if "v3.5" in cfg.model:
        if cfg.voice == "longanyang" or not cfg.voice:
            raise AssertionError(
                "v3.5 smoke requires a designed voice_id "
                "(run scripts/design_cosyvoice_voices.py)"
            )
    elif cfg.model != "cosyvoice-v3-flash" or cfg.voice != "longanyang":
        raise AssertionError(
            "v3 smoke requires cosyvoice-v3-flash + longanyang, "
            f"got model={cfg.model} voice={cfg.voice}"
        )
    tts = CosyVoiceTTS(cfg)
    try:
        await tts.pool.warm(1)
        tts.apply_speech_plan(emotion="neutral", rate=0.98)
        result = await tts.synthesize_stream_text(
            ["你好，这是语音合成测试。"],
            fence=GenerationFence("provider-smoke-cosyvoice", 1, 1, 0),
        )
        _validate_cosyvoice_result(result.pcm, result.words, cfg.sample_rate)
        asr_audio = await tts.synthesize_stream_text(
            ["你好，这是实时语音测试。"],
            fence=GenerationFence("provider-smoke-funasr-fixture", 1, 1, 0),
        )
        _validate_cosyvoice_result(asr_audio.pcm, asr_audio.words, cfg.sample_rate)
    finally:
        await tts.aclose()
    mode = "freeform" if cfg.uses_freeform_instruct else "fixed"
    print(
        f"CosyVoice smoke: PASS (model={cfg.model} voice={cfg.voice} "
        f"instruct={mode} + timestamps)"
    )
    return audioop.ratecv(asr_audio.pcm, 2, 1, cfg.sample_rate, 16000, None)[0]


async def smoke_funasr(pcm_16k: bytes) -> None:
    cfg = FunASRConfig(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        ws_url=_dashscope_ws_url(),
        model=os.getenv("FUNASR_MODEL", "fun-asr-realtime"),
        sample_rate=16000,
    )
    session = FunASRSession(cfg)
    interim = False
    final = None
    finished = False
    try:
        await session.connect()
        chunk_bytes = cfg.sample_rate * 2 * cfg.chunk_ms // 1000
        for offset in range(0, len(pcm_16k), chunk_bytes):
            await session.send_pcm(pcm_16k[offset : offset + chunk_bytes])
            await asyncio.sleep(cfg.chunk_ms / 1000)
        await session.finish()
        while True:
            event = await asyncio.wait_for(session.events.get(), timeout=cfg.result_timeout_s)
            if event.event == "result-generated" and event.sentence is not None:
                if event.sentence.sentence_end:
                    final = event.sentence
                elif event.sentence.text:
                    interim = True
            elif event.event == "task-failed":
                raise AssertionError(event.error_message or "FunASR task-failed")
            elif event.event == "task-finished":
                finished = True
                break
    finally:
        await session.aclose()
    if not interim:
        raise AssertionError("FunASR returned no interim transcript")
    if final is None or "实时语音测试" not in final.text:
        raise AssertionError(f"FunASR final mismatch: {getattr(final, 'text', '')!r}")
    if not final.words or not timestamps_monotonic(final.words):
        raise AssertionError("FunASR final word timestamps are empty or non-monotonic")
    if not finished:
        raise AssertionError("FunASR task did not finish normally")
    print("FunASR smoke: PASS (interim + final + monotonic word timestamps)")


async def smoke_llm() -> str:
    settings = AgentSettings()
    cfg = DeepSeekConfig(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        fast_model=settings.llm_fast_model,
    )
    client = DeepSeekClient(cfg)
    content = ""
    try:
        async for _, chunk in client.stream_fast(
            [{"role": "user", "content": "请只回答“连接正常”。"}],
            fence=GenerationFence("provider-smoke-deepseek", 1, 1, 0),
        ):
            content += filter_content_for_tts(chunk)
    finally:
        await client.aclose()
    if "连接正常" not in content:
        raise AssertionError(f"LLM content missing expected phrase: {content!r}")
    label = "DeepSeek" if settings.llm_provider == "deepseek" else "Qwen"
    print(f"{label} smoke: PASS (streaming content, thinking disabled)")
    return label


async def main() -> int:
    required = os.getenv("MEMORIA_PROVIDER_SMOKE_REQUIRED", "false").lower() == "true"
    if os.getenv("OFFLINE_MOCK", "false").lower() == "true":
        message = "provider_smoke_test SKIP: OFFLINE_MOCK=true"
        print(message)
        return 1 if required else 0
    settings = AgentSettings()
    missing = [name for name in ("DASHSCOPE_API_KEY",) if not os.getenv(name)]
    if settings.llm_provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        message = "provider_smoke_test SKIP: missing " + ", ".join(missing)
        print(message)
        return 1 if required else 0
    try:
        pcm_16k = await smoke_cosyvoice()
        await smoke_funasr(pcm_16k)
        llm_label = await smoke_llm()
    except Exception as exc:
        print(f"provider_smoke_test FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(f"provider_smoke_test PASS: FunASR, {llm_label}, CosyVoice")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
