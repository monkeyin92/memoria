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
from services.agent.src.orchestration.utterance_router import InterruptSemanticVerdict
from services.agent.src.providers.cosyvoice_protocol import pcm_duration_ms
from services.agent.src.providers.cosyvoice_tts import CosyVoiceConfig, CosyVoiceTTS
from services.agent.src.providers.deepseek import (
    DeepSeekClient,
    DeepSeekConfig,
    filter_content_for_tts,
)
from services.agent.src.providers.funasr_protocol import timestamps_monotonic
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.src.providers.handlers import build_realtime_search_resolver
from services.agent.src.providers.interrupt_semantic_classifier import (
    InterruptSemanticClassifier,
    InterruptSemanticClassifierConfig,
)
from services.agent.src.providers.qwen_voice_catalog import QWEN_VOICE_CATALOG
from services.common.voice_identity import TTS_MODEL, TTS_PROVIDER

DEFAULT_DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"


def _dashscope_ws_url() -> str:
    return os.getenv("DASHSCOPE_WS_URL", DEFAULT_DASHSCOPE_WS_URL)


def _validate_tts_result(
    pcm: bytes,
    words: tuple[TimedWord, ...],
    *,
    sample_rate: int,
    num_channels: int = 1,
    alignment_status: str = "ok",
) -> None:
    if sample_rate != 24_000 or num_channels != 1:
        raise AssertionError(
            "Qwen-Audio TTS PCM contract must be 24000 Hz mono, "
            f"got {sample_rate} Hz/{num_channels} channels"
        )
    if not pcm:
        raise AssertionError("Qwen-Audio TTS returned no PCM")
    if len(pcm) % 2:
        raise AssertionError("Qwen-Audio TTS PCM is not frame-aligned signed 16-bit little-endian")
    if not words:
        raise AssertionError("Qwen-Audio TTS returned no word timestamps")
    if alignment_status not in {"ok", "scaled"}:
        raise AssertionError(f"Qwen-Audio TTS word timestamp alignment is {alignment_status}")
    previous = -1
    for word in words:
        begin_ms = word.begin_ms
        end_ms = word.end_ms
        if begin_ms < previous or end_ms < begin_ms:
            raise AssertionError("Qwen-Audio TTS word timestamps are not monotonic")
        previous = end_ms
    duration_ms = pcm_duration_ms(pcm, sample_rate=sample_rate)
    if abs(previous - duration_ms) > 300:
        raise AssertionError(
            "Qwen-Audio TTS alignment differs from PCM duration by "
            f"{abs(previous - duration_ms)} ms"
        )


async def smoke_qwen_tts() -> list[tuple[bytes, tuple[str, ...], tuple[str, ...]]]:
    cfg = CosyVoiceConfig.from_env({**os.environ, "COSYVOICE_POOL_SIZE": "1"})
    tts = CosyVoiceTTS(cfg)
    try:
        await tts.pool.warm(1)
        result = await tts.synthesize_stream_text(
            ["你好，", "这是实时语音测试。"],
            fence=GenerationFence("provider-smoke-tts", 1, 1, 0),
        )
        _validate_tts_result(
            result.pcm,
            result.words,
            sample_rate=cfg.sample_rate,
            num_channels=1,
            alignment_status=result.alignment_status,
        )
        samples = [(result.pcm, ("实时语音测试",), ())]
        # Every persona voice must exist on the model: a vendor rename would
        # otherwise surface only as a silent fallback in a live session.
        voice_alignment: list[str] = []
        for index, voice in enumerate(QWEN_VOICE_CATALOG, 2):
            fence = GenerationFence(f"provider-smoke-tts-{voice.profile_id}", index, index, 0)
            tts.apply_voice_profile(
                model=TTS_MODEL,
                resource_id=TTS_MODEL,
                voice=voice.speaker_id,
                profile_id=voice.profile_id,
                provider=TTS_PROVIDER,
                voice_kind="designed",
            )
            tts.bind_fence(fence)
            tts.apply_speech_plan(emotion="sad", rate=0.95, fence=fence)
            styled = await tts.synthesize_stream_text(["我在这里，慢慢说就好。"], fence=fence)
            _validate_tts_result(
                styled.pcm,
                styled.words,
                sample_rate=cfg.sample_rate,
                num_channels=1,
                alignment_status=styled.alignment_status,
            )
            voice_alignment.append(f"{voice.profile_id}:{styled.alignment_status}")
            samples.append((styled.pcm, ("在这里", "慢慢说"), ()))

        task_started = asyncio.Event()
        tts.set_trace_callback(
            lambda name, _status, _detail: (
                task_started.set() if name == "cosyvoice_task_started" else None
            )
        )
        cancel = asyncio.Event()
        cancel_fence = GenerationFence("provider-smoke-tts-cancel", 8, 8, 0)
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
            await asyncio.wait_for(task_started.wait(), timeout=cfg.connect_timeout_s + 1)
            if not tts.pool.active_by_fence:
                raise AssertionError("TTS cancel probe found no active task")
            cancel.set()
            canceled = await asyncio.wait_for(cancel_task, timeout=cfg.connect_timeout_s + 1)
        finally:
            if not cancel_task.done():
                cancel.set()
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task
        if not canceled.discarded or tts.pool.active_by_fence:
            raise AssertionError("TTS cancel probe did not discard the active connection")
        if (
            tts.pool.metrics.get("tts_connections_discarded_total", {"reason": "cancel"})
            != cancel_count + 1
        ):
            raise AssertionError("TTS cancel probe did not record the cancel discard")
    finally:
        await tts.aclose()
    print(
        f"Qwen-Audio TTS smoke: PASS (model={cfg.model} voice={cfg.voice} "
        "pcm_s16le/24000Hz/mono + timestamps + cancel/eviction"
        f" + persona_voices={','.join(voice_alignment)})"
    )
    resampled = []
    for pcm, expected_markers, forbidden_markers in samples:
        pcm_16k = audioop.ratecv(pcm, 2, 1, cfg.sample_rate, 16000, None)[0]
        if len(pcm_16k) % 2:
            raise AssertionError("TTS resampled PCM is not frame-aligned signed 16-bit")
        resampled.append((pcm_16k, expected_markers, forbidden_markers))
    return resampled


async def smoke_funasr(
    pcm_16k: bytes,
    *,
    expected_markers: tuple[str, ...],
    forbidden_markers: tuple[str, ...] = (),
) -> None:
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
    if final is None or not all(marker in final.text for marker in expected_markers):
        raise AssertionError(f"FunASR final mismatch: {getattr(final, 'text', '')!r}")
    if any(marker in final.text for marker in forbidden_markers):
        raise AssertionError(f"FunASR unexpectedly transcribed reference context: {final.text!r}")
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
        thinking_mode=(
            "deepseek" if settings.llm_provider == "deepseek" else "dashscope"
        ),
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
    label = "DeepSeek" if settings.llm_provider in {"bailian_deepseek", "deepseek"} else "Qwen"
    print(f"{label} smoke: PASS (streaming content, thinking disabled)")
    return label


async def smoke_realtime_search(settings: AgentSettings) -> None:
    """Exercise the isolated Qwen source used for fenced DEEP_RESULT work."""

    resolver = build_realtime_search_resolver(settings=settings)
    if resolver is None:
        raise AssertionError("Qwen realtime search resolver is unavailable")
    try:
        result = await resolver.resolve(query="请联网查询今天的日期，只用一句中文回答。")
    finally:
        await resolver.aclose()
    if not result:
        raise AssertionError("Qwen realtime search returned no public result")
    print("Qwen realtime-search smoke: PASS (forced public search, non-empty result)")


async def smoke_interrupt_semantic() -> None:
    settings = AgentSettings()
    if not settings.interrupt_semantic_enabled:
        raise AssertionError("interrupt semantic classifier is disabled")
    classifier = InterruptSemanticClassifier(
        InterruptSemanticClassifierConfig(
            api_key=settings.dashscope_api_key,
            base_url=settings.dashscope_compatible_base_url,
            model=settings.interrupt_semantic_model,
            timeout_s=settings.interrupt_semantic_timeout_s,
        )
    )
    cases = (
        (
            "份停听一下能是据提供的数据和指示来协助。",
            "停一下",
            "根据提供的数据和指示来协助。",
            InterruptSemanticVerdict.CONTROL_ONLY,
        ),
        (
            "等一下，我想问下周三有什么安排？",
            "等一下",
            "我正在介绍今天的数据。",
            InterruptSemanticVerdict.HAS_USER_CONTENT,
        ),
        (
            "不是，你说的“根据提供的数据”是什么意思？",
            "不是",
            "根据提供的数据和指示来协助。",
            InterruptSemanticVerdict.HAS_USER_CONTENT,
        ),
        (
            "停一下，你叫什么名字？",
            "停一下",
            "我正在介绍自己。",
            InterruptSemanticVerdict.HAS_USER_CONTENT,
        ),
        (
            "停一下根据提供的数据和指示来协助。",
            "停一下",
            "根据提供的数据和指示来协助。",
            InterruptSemanticVerdict.CONTROL_ONLY,
        ),
    )
    try:
        for final_text, sticky_text, assistant_text, expected in cases:
            actual = await classifier.classify(
                final_text=final_text,
                sticky_text=sticky_text,
                assistant_text=assistant_text,
            )
            if actual is not expected:
                raise AssertionError(
                    f"interrupt semantic mismatch expected={expected} actual={actual}"
                )
    finally:
        await classifier.aclose()
    print(
        "Interrupt semantic smoke: PASS "
        f"(model={settings.interrupt_semantic_model}; {len(cases)} cases)"
    )


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
        await smoke_realtime_search(settings)
        samples = await smoke_qwen_tts()
        for pcm_16k, expected_markers, forbidden_markers in samples:
            await smoke_funasr(
                pcm_16k,
                expected_markers=expected_markers,
                forbidden_markers=forbidden_markers,
            )
        llm_label = await smoke_llm()
        await smoke_interrupt_semantic()
    except Exception as exc:
        print(f"provider_smoke_test FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(
        "provider_smoke_test PASS: FunASR, QwenRealtimeSearch, "
        f"{llm_label}, QwenAudioTTS, InterruptSemantic"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
