#!/usr/bin/env python3
"""Live provider acceptance checks; skip only when provider credentials are absent."""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import os
import sys
from typing import Any

from services.agent.src.config import AgentSettings
from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.llm_types import ChatContext
from services.agent.src.providers.doubao_protocol import pcm_duration_ms
from services.agent.src.providers.doubao_tts import DoubaoTTS, DoubaoTTSConfig
from services.agent.src.providers.doubao_voice_catalog import (
    DOUBAO_TTS_MODEL,
    DOUBAO_VOICE_CATALOG,
)
from services.agent.src.providers.funasr_protocol import FunASRSentence, timestamps_monotonic
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.src.providers.handlers import (
    build_language_model_handler,
    build_realtime_search_resolver,
)
from services.agent.src.providers.openai_chat import OpenAIChatModel
from services.agent.src.providers.tts_factory import (
    build_tts,
    tts_provider_label,
    tts_runtime_descriptor,
)

DEFAULT_DASHSCOPE_WS_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

SMOKE_TEXT = "你好，这是语音测试。"
SMOKE_MARKER = "语音测试"


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


async def smoke_doubao() -> list[tuple[bytes, tuple[str, ...], tuple[str, ...]]]:
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

        samples = [(result.pcm, ("实时语音测试",), ())]
        style_alignment: list[str] = []
        if cfg.style_control_enabled:
            styled_text = "我在这里，慢慢说就好。"
            reference_markers = ("今天有点难过", "想找人聊聊")
            for index, voice in enumerate(DOUBAO_VOICE_CATALOG, 2):
                fence = GenerationFence(
                    f"provider-smoke-doubao-style-{voice.profile_id}",
                    index,
                    index,
                    0,
                )
                tts.apply_voice_profile(
                    model=DOUBAO_TTS_MODEL,
                    resource_id=DOUBAO_TTS_MODEL,
                    voice=voice.speaker_id,
                    profile_id=voice.profile_id,
                    provider="volcengine_doubao",
                    voice_kind="designed",
                )
                tts.bind_fence(fence)
                tts.apply_speech_plan(
                    emotion="sad",
                    rate=0.95,
                    instruction="温柔关切地承接，略带伤感，但不要播报腔。",
                    pitch=-1,
                    reference_contexts=("用户：今天有点难过，想找人聊聊。",),
                    fence=fence,
                )
                styled = await tts.synthesize_stream_text([styled_text], fence=fence)
                _validate_doubao_result(
                    styled.pcm,
                    styled.words,
                    sample_rate=cfg.sample_rate,
                    num_channels=1,
                    alignment_status=styled.alignment_status,
                )
                style_alignment.append(f"{voice.profile_id}:{styled.alignment_status}")
                samples.append((styled.pcm, ("在这里", "慢慢说"), reference_markers))

        session_started = asyncio.Event()
        tts.set_trace_callback(
            lambda name, _status, _detail: (
                session_started.set() if name == "doubao_session_started" else None
            )
        )
        cancel = asyncio.Event()
        cancel_fence = GenerationFence("provider-smoke-doubao-cancel", 8, 8, 0)
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
        "pcm_s16le/24000Hz/mono + timestamps + CancelSession/eviction"
        f" + style_context={'all_5_voices' if cfg.style_control_enabled else 'off'}"
        f" alignment={','.join(style_alignment) if style_alignment else 'baseline'})"
    )
    resampled = []
    for pcm, expected_markers, forbidden_markers in samples:
        pcm_16k = audioop.ratecv(pcm, 2, 1, cfg.sample_rate, 16000, None)[0]
        if len(pcm_16k) % 2:
            raise AssertionError("Doubao resampled PCM is not frame-aligned signed 16-bit")
        resampled.append((pcm_16k, expected_markers, forbidden_markers))
    return resampled


async def _drain_pcm(tts: Any, text: str, *, fence: GenerationFence) -> bytes:
    """Speak one line through a session-shaped TTS object and collect its PCM."""

    bind = getattr(tts, "bind_fence", None)
    if callable(bind):
        bind(fence)
    stream = tts.stream()
    stream.push_text(text)
    stream.end_input()
    pcm = bytearray()
    sample_rate = 0
    async for event in stream:
        if isinstance(event, (bytes, bytearray)):
            pcm += event
            continue
        frame = getattr(event, "frame", None)
        data = getattr(frame, "data", b"") if frame is not None else b""
        if data:
            pcm += data
            sample_rate = sample_rate or int(getattr(frame, "sample_rate", 0))
    if not pcm:
        raise AssertionError("TTS returned no PCM")
    if len(pcm) % 2:
        raise AssertionError("TTS PCM is not frame-aligned signed 16-bit little-endian")
    if sample_rate and sample_rate != 24_000:
        raise AssertionError(f"TTS PCM must be 24000 Hz, got {sample_rate} Hz")
    return bytes(pcm)


async def smoke_stepfun() -> list[tuple[bytes, tuple[str, ...], tuple[str, ...]]]:
    """Speak through the StepFun primary the media session would build.

    The gate follows the configured provider: a Doubao account failure is the
    backup's problem once StepFun is primary, and it must not decide whether a
    release ships. A silent failover to the backup does fail the gate, because
    then the primary this environment claims to use is not working.
    """

    settings = AgentSettings()
    tts = build_tts(settings)
    descriptor = tts_runtime_descriptor(settings)
    try:
        pcm = await _drain_pcm(
            tts,
            SMOKE_TEXT,
            fence=GenerationFence("provider-smoke-stepfun", 1, 1, 0),
        )
        engine = getattr(tts, "last_engine", None)
        spoken_by = str(getattr(engine(), "provider", "")) if callable(engine) else ""
        if spoken_by != "stepfun":
            raise AssertionError(
                f"StepFun is the configured primary but {spoken_by or 'no engine'} spoke"
            )
    finally:
        await tts.aclose()
    print(
        f"StepFun smoke: PASS (model={descriptor.engine} voice={descriptor.voice} "
        f"pcm_s16le/24000Hz/mono {len(pcm)} B)"
    )
    pcm_16k = audioop.ratecv(pcm, 2, 1, 24_000, 16000, None)[0]
    if len(pcm_16k) % 2:
        raise AssertionError("StepFun resampled PCM is not frame-aligned signed 16-bit")
    return [(pcm_16k, (SMOKE_MARKER,), ())]


async def smoke_backup_doubao() -> str:
    """Report the Doubao backup without failing the gate when it is not primary."""

    try:
        await smoke_doubao()
    except Exception as exc:
        print(
            "Doubao backup probe: DEGRADED "
            f"({type(exc).__name__}: {exc}); the configured primary stands alone "
            "until the account is restored"
        )
        return "degraded"
    return "ok"


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
    # A pause longer than max_sentence_silence splits one utterance into
    # several finals (low_magnetic's comma pause is ~555 ms against 550 ms);
    # production joins every final inside the turn range, so the smoke does too.
    finals: list[FunASRSentence] = []
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
                    finals.append(event.sentence)
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
    text = "".join(sentence.text for sentence in finals)
    if not finals or not all(marker in text for marker in expected_markers):
        raise AssertionError(f"FunASR final mismatch: {[sentence.text for sentence in finals]!r}")
    if any(marker in text for marker in forbidden_markers):
        raise AssertionError(f"FunASR unexpectedly transcribed reference context: {text!r}")
    words = tuple(word for sentence in finals for word in sentence.words)
    if any(not sentence.words for sentence in finals) or not timestamps_monotonic(words):
        raise AssertionError("FunASR final word timestamps are empty or non-monotonic")
    if not finished:
        raise AssertionError("FunASR task did not finish normally")
    print(f"FunASR smoke: PASS (interim + {len(finals)} final(s) + monotonic word timestamps)")


async def smoke_llm() -> str:
    settings = AgentSettings()
    model = build_language_model_handler(
        settings=settings,
        llm_factory=OpenAIChatModel,
    )
    chat_ctx = ChatContext.empty()
    chat_ctx.add_message(role="user", content="请只回答“连接正常”。")
    content = ""
    try:
        async with model.chat(chat_ctx=chat_ctx) as stream:
            async for chunk in stream:
                if chunk.delta is not None and chunk.delta.content:
                    content += chunk.delta.content
    finally:
        await model.aclose()
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


async def main() -> int:
    required = os.getenv("MEMORIA_PROVIDER_SMOKE_REQUIRED", "false").lower() == "true"
    if os.getenv("OFFLINE_MOCK", "false").lower() == "true":
        message = "provider_smoke_test SKIP: OFFLINE_MOCK=true"
        print(message)
        return 1 if required else 0
    settings = AgentSettings()
    tts_label = tts_provider_label(settings)
    tts_passes_with_doubao = tts_label == "volcengine_doubao"
    missing = [name for name in ("DASHSCOPE_API_KEY",) if not os.getenv(name)]
    has_doubao_auth = bool(os.getenv("DOUBAO_TTS_API_KEY")) or bool(
        os.getenv("DOUBAO_TTS_APP_ID") and os.getenv("DOUBAO_TTS_ACCESS_TOKEN")
    )
    # The StepFun-primary chain still builds the Doubao backup, so both sets of
    # credentials are required either way; only the account behind them differs.
    if not has_doubao_auth:
        missing.append("DOUBAO_TTS_AUTH")
    if not tts_passes_with_doubao and not os.getenv("STEPFUN_API_KEY"):
        missing.append("STEPFUN_API_KEY")
    if settings.llm_provider == "deepseek" and not os.getenv("DEEPSEEK_API_KEY"):
        missing.append("DEEPSEEK_API_KEY")
    if missing:
        message = "provider_smoke_test SKIP: missing " + ", ".join(missing)
        print(message)
        return 1 if required else 0
    if (
        required
        and tts_passes_with_doubao
        and os.getenv("DOUBAO_TTS_STYLE_CONTROL_ENABLED", "false").lower() != "true"
    ):
        print("provider_smoke_test FAIL: Doubao style control is required but disabled")
        return 1
    passed = ["FunASR"]
    try:
        if settings.live_search_qwen_enabled:
            await smoke_realtime_search(settings)
            passed.append("QwenRealtimeSearch")
        samples = await smoke_doubao() if tts_passes_with_doubao else await smoke_stepfun()
        for pcm_16k, expected_markers, forbidden_markers in samples:
            await smoke_funasr(
                pcm_16k,
                expected_markers=expected_markers,
                forbidden_markers=forbidden_markers,
            )
        llm_label = await smoke_llm()
        passed.append(llm_label)
        if tts_passes_with_doubao:
            passed.append("Doubao")
        else:
            await smoke_backup_doubao()
            passed.append("StepFun")
    except Exception as exc:
        print(f"provider_smoke_test FAIL: {type(exc).__name__}: {exc}")
        return 1
    print("provider_smoke_test PASS: " + ", ".join(passed))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
