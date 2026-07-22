#!/usr/bin/env python3
"""Live provider acceptance checks; skip only when provider credentials are absent."""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import os
import sys

from services.agent.src.config import AgentSettings
from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.deepseek import (
    DeepSeekClient,
    DeepSeekConfig,
    filter_content_for_tts,
)
from services.agent.src.providers.doubao_protocol import pcm_duration_ms
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.providers.funasr_protocol import timestamps_monotonic
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession

DEFAULT_DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def _dashscope_ws_url() -> str:
    return os.getenv("DASHSCOPE_WS_URL", DEFAULT_DASHSCOPE_WS_URL)


def _validate_doubao_result(
    pcm: bytes,
    words: tuple[TimedWord, ...],
    *,
    sample_rate: int,
    num_channels: int = 1,
    alignment_status: str = "ok",
) -> None:
    if sample_rate != 24_000 or num_channels != 1:
        raise AssertionError(
            "Doubao PCM contract must be 24000 Hz mono, "
            f"got {sample_rate} Hz/{num_channels} channels"
        )
    if not pcm:
        raise AssertionError("Doubao returned no PCM")
    if len(pcm) % 2:
        raise AssertionError("Doubao PCM is not frame-aligned signed 16-bit little-endian")
    if not words:
        raise AssertionError("Doubao returned no word timestamps")
    if alignment_status not in {"ok", "scaled"}:
        raise AssertionError(f"Doubao word timestamp alignment is {alignment_status}")
    previous = -1
    for word in words:
        begin_ms = word.begin_ms
        end_ms = word.end_ms
        if begin_ms < previous or end_ms < begin_ms:
            raise AssertionError("Doubao word timestamps are not monotonic")
        previous = end_ms
    duration_ms = pcm_duration_ms(pcm, sample_rate=sample_rate)
    if abs(previous - duration_ms) > 300:
        raise AssertionError(
            f"Doubao alignment differs from PCM duration by {abs(previous - duration_ms)} ms"
        )


async def smoke_doubao() -> bytes:
    cfg = DoubaoTTSConfig.from_env({**os.environ, "DOUBAO_TTS_POOL_SIZE": "1"})
    tts = DoubaoTTS(cfg)
    try:
        await tts.pool.warm(1)
        result = await tts.synthesize_stream_text(
            ["你好，", "这是豆包实时语音测试。"],
            fence=GenerationFence("provider-smoke-doubao", 1, 1, 0),
        )
        _validate_doubao_result(
            result.pcm,
            result.words,
            sample_rate=cfg.sample_rate,
            num_channels=1,
            alignment_status=result.alignment_status,
        )

        session_started = asyncio.Event()
        tts.set_trace_callback(
            lambda name, _status, _detail: (
                session_started.set() if name == "doubao_session_started" else None
            )
        )
        cancel = asyncio.Event()
        cancel_fence = GenerationFence("provider-smoke-doubao-cancel", 2, 2, 0)
        cancel_count = tts.pool.metrics.get(
            "tts_connections_discarded_total",
            {"reason": "cancel"},
        )
        cancel_task = asyncio.create_task(
            tts.synthesize_stream_text(
                ["这是一段用于验证实时取消的较长语音。" * 8],
                fence=cancel_fence,
                cancel_event=cancel,
            )
        )
        try:
            await asyncio.wait_for(
                session_started.wait(),
                timeout=cfg.connect_timeout_s + 1,
            )
            active = next(iter(tts.pool.active_by_fence.values()), None)
            if active is None or not active.session_id:
                raise AssertionError("Doubao cancel probe found no active session")
            cancel.set()
            canceled = await asyncio.wait_for(
                cancel_task,
                timeout=cfg.connect_timeout_s + 1,
            )
        finally:
            if not cancel_task.done():
                cancel.set()
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
        if not canceled.discarded:
            raise AssertionError("Doubao cancel probe did not discard the active session")
        if not active.cancel_sent:
            raise AssertionError("Doubao cancel probe did not write CancelSession")
        if not active.closed or any(
            connection is active for connection in tts.pool.active_by_fence.values()
        ):
            raise AssertionError("Doubao cancel probe did not evict the active connection")
        if (
            tts.pool.metrics.get(
                "tts_connections_discarded_total",
                {"reason": "cancel"},
            )
            != cancel_count + 1
        ):
            raise AssertionError("Doubao cancel probe did not send CancelSession")
    finally:
        await tts.aclose()
    print(
        f"Doubao smoke: PASS (model={cfg.resource_id} voice={cfg.speaker} "
        "pcm_s16le/24000Hz/mono + timestamps + CancelSession/eviction)"
    )
    pcm_16k = audioop.ratecv(result.pcm, 2, 1, cfg.sample_rate, 16000, None)[0]
    if len(pcm_16k) % 2:
        raise AssertionError("Doubao resampled PCM is not frame-aligned signed 16-bit")
    return pcm_16k


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
    has_doubao_auth = bool(os.getenv("DOUBAO_TTS_API_KEY")) or bool(
        os.getenv("DOUBAO_TTS_APP_ID") and os.getenv("DOUBAO_TTS_ACCESS_TOKEN")
    )
    if not has_doubao_auth:
        missing.append("DOUBAO_TTS_AUTH")
    if settings.llm_provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        message = "provider_smoke_test SKIP: missing " + ", ".join(missing)
        print(message)
        return 1 if required else 0
    try:
        pcm_16k = await smoke_doubao()
        await smoke_funasr(pcm_16k)
        llm_label = await smoke_llm()
    except Exception as exc:
        print(f"provider_smoke_test FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(f"provider_smoke_test PASS: FunASR, {llm_label}, Doubao")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
