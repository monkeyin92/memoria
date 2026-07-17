"""LiveKit AgentSession entrypoint with Duplex Orchestrator wiring (ch.11, ch.8–18)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator, AsyncIterable
from typing import TYPE_CHECKING, Any, Literal, cast

from services.agent.src.contracts.events import TimedWord
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.prompts import VOICE_SYSTEM_PROMPT

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Voice replies stay shorter than chat, but 96/3 cut creative answers mid-stream.
MAX_VOICE_REPLY_SENTENCES = 8
MAX_VOICE_REPLY_CHARS = 320
# Longer budget when user asks for writing / plans / multi-step content.
MAX_VOICE_REPLY_CHARS_LONGFORM = 560
MAX_VOICE_REPLY_SENTENCES_LONGFORM = 12
_LONGFORM_HINTS = (
    "创作",
    "写一",
    "写个",
    "写段",
    "故事",
    "小说",
    "文案",
    "诗",
    "歌词",
    "详细",
    "完整",
    "长一点",
    "继续写",
    "大纲",
    "方案",
    "计划",
    "步骤",
)
_SENTENCE_ENDINGS = frozenset("。！？；!?")
TELEMETRY_TOPIC = "voice-agent.telemetry"
CASCADE_OPUS_MAX_BITRATE = 64_000

try:
    from livekit import agents, rtc
    from livekit.agents import Agent, AgentSession, StopResponse, llm, room_io
    from livekit.agents.types import TimedString
    from livekit.plugins import openai, silero

    _HAS_LIVEKIT = True
except ImportError:  # pragma: no cover
    _HAS_LIVEKIT = False
    agents = None  # type: ignore[assignment]
    rtc = None  # type: ignore[assignment]
    llm = None  # type: ignore[assignment]
    TimedString = str  # type: ignore[misc, assignment]


def build_cascade_audio_output_options() -> Any:
    """Keep 24 kHz TTS PCM while avoiding low-bitrate Opus metallic artifacts."""
    if not _HAS_LIVEKIT:
        raise RuntimeError("LiveKit is unavailable")
    publish_options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    publish_options.audio_encoding.max_bitrate = CASCADE_OPUS_MAX_BITRATE
    return room_io.AudioOutputOptions(
        sample_rate=24000,
        num_channels=1,
        track_publish_options=publish_options,
    )


def _chunk_text(chunk: Any) -> str:
    """Extract plain text from LLM stream items for fence gating."""
    if isinstance(chunk, str):
        return chunk
    delta = getattr(chunk, "delta", None)
    if delta is not None:
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
    return ""


def _message_text(message: Any) -> str:
    content = getattr(message, "text_content", None)
    if callable(content):
        content = content()
    if isinstance(content, str):
        return content
    if isinstance(message, str):
        return message
    raw = getattr(message, "content", "")
    if isinstance(raw, list):
        return "\n".join(part for part in raw if isinstance(part, str))
    return str(raw or "")


def _heard_only_chat_context(chat_ctx: Any, heard_assistant: list[str]) -> Any:
    """Replace LiveKit's generated assistant history with actually-heard text."""
    safe = chat_ctx.copy()
    assistant_items = [
        item
        for item in list(safe.items)
        if str(getattr(item, "role", "")) == "assistant"
    ]
    if len(assistant_items) != len(heard_assistant):
        logger.info(
            "heard_history_alignment generated_count=%s heard_count=%s strategy=latest",
            len(assistant_items),
            len(heard_assistant),
        )
    unmatched = max(0, len(assistant_items) - len(heard_assistant))
    for item in assistant_items[:unmatched]:
        safe.remove(item)
    for item, text in zip(
        reversed(assistant_items[unmatched:]),
        reversed(heard_assistant),
        strict=False,
    ):
        item.content = [text]
    return safe


class DuplexVoiceAgent(Agent if _HAS_LIVEKIT else object):  # type: ignore[misc]
    """Agent that gates LLM/TTS through GenerationFence and tracks active tasks."""

    def __init__(self, *, instructions: str, runtime: DuplexRuntime) -> None:
        if _HAS_LIVEKIT:
            super().__init__(instructions=instructions)
        self._runtime = runtime
        self._llm_text_buf = ""

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        text = _message_text(new_message) if new_message is not None else ""
        if text.strip():
            has_speech_metrics = hasattr(new_message, "metrics")
            metrics = getattr(new_message, "metrics", {}) or {}
            speech_anchored = (
                any(
                    metrics.get(name) is not None
                    for name in ("started_speaking_at", "stopped_speaking_at")
                )
                if has_speech_metrics
                else None
            )
            logger.info(
                "user_turn_endpoint_timing started_speaking_at=%s "
                "stopped_speaking_at=%s transcription_delay=%s "
                "end_of_turn_delay=%s",
                metrics.get("started_speaking_at"),
                metrics.get("stopped_speaking_at"),
                metrics.get("transcription_delay"),
                metrics.get("end_of_turn_delay"),
            )
            accepted, reason = self._runtime.accept_user_turn(
                text.strip(),
                speech_anchored=speech_anchored,
            )
            if not accepted:
                logger.info(
                    "%s reason=%s",
                    (
                        "post_playback_input_ignored"
                        if reason
                        in {
                            "backchannel",
                            "assistant_echo",
                            "non_target_language",
                            "speaker_mismatch",
                            "interrupt_command_only",
                        }
                        else "user_turn_ignored"
                    ),
                    reason,
                )
                raise StopResponse()
            self._runtime.mark_audio_event("last_user_audio")
            fence = await self._runtime.on_turn_committed(text.strip())
            self._runtime.publish_transcript(
                speaker="user",
                text=text.strip(),
                final=True,
                fence=fence,
            )
            self._llm_text_buf = ""
            logger.info(
                "turn_committed turn_id=%s generation_id=%s tool_epoch=%s text=%s",
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
                text.strip()[:80],
            )
        parent = getattr(Agent, "on_user_turn_completed", None) if _HAS_LIVEKIT else None
        if parent is not None:
            try:
                await parent(self, turn_ctx, new_message)
            except TypeError:
                return

    def llm_node(
        self,
        chat_ctx: Any,
        tools: list[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        return self._llm_node_impl(chat_ctx, tools, model_settings)

    async def _llm_node_impl(
        self,
        chat_ctx: Any,
        tools: list[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        """Gate every LLM token/chunk through GenerationFence; register cancel task."""
        if not _HAS_LIVEKIT:
            return
            yield  # pragma: no cover  # make this an async generator

        fence = self._runtime.fence
        if self._runtime.tts is not None:
            self._runtime.tts.bind_fence(fence)

        task = asyncio.current_task()
        self._runtime.orchestrator.set_active_llm_task(task)
        self._llm_text_buf = ""
        heard_assistant = [
            turn.content
            for turn in self._runtime.orchestrator.context.turns
            if turn.role == "assistant"
        ]
        safe_chat_ctx = _heard_only_chat_context(chat_ctx, heard_assistant)
        if self._runtime.speech_plan.llm_instruction:
            safe_chat_ctx.add_message(
                role="system",
                content=self._runtime.speech_plan.llm_instruction,
            )
        segmenter = self._runtime.orchestrator.segmenter
        if segmenter is None:
            raise RuntimeError("PhraseSegmenter is not configured")
        segmenter.reset(fence)
        reply_chars = 0
        reply_sentences = 0
        reply_budget_exhausted = False
        first_content_marked = False
        first_phrase_marked = False
        last_user = ""
        for turn in reversed(self._runtime.orchestrator.context.turns):
            if turn.role == "user" and turn.content:
                last_user = turn.content
                break
        longform = any(hint in last_user for hint in _LONGFORM_HINTS) or (
            self._runtime.speech_plan.delivery_mode in {"deliberative", "supportive"}
        )
        max_chars = MAX_VOICE_REPLY_CHARS_LONGFORM if longform else MAX_VOICE_REPLY_CHARS
        max_sentences = (
            MAX_VOICE_REPLY_SENTENCES_LONGFORM if longform else MAX_VOICE_REPLY_SENTENCES
        )

        def _accept_segment(text: str) -> str | None:
            nonlocal reply_chars, reply_sentences, reply_budget_exhausted
            remaining_chars = max_chars - reply_chars
            remaining_sentences = max_sentences - reply_sentences
            if remaining_chars <= 0 or remaining_sentences <= 0:
                reply_budget_exhausted = True
                return None
            accepted_chars = 0
            accepted_sentences = 0
            accepted: list[str] = []
            for char in text:
                if char.isalnum():
                    if accepted_chars >= remaining_chars:
                        break
                    accepted_chars += 1
                accepted.append(char)
                if char in _SENTENCE_ENDINGS:
                    accepted_sentences += 1
                    if accepted_sentences >= remaining_sentences:
                        break
            fitted = "".join(accepted).strip()
            if not fitted:
                reply_budget_exhausted = True
                return None
            if fitted != text and fitted[-1] not in _SENTENCE_ENDINGS:
                fitted += "。"
            chars = sum(1 for ch in fitted if ch.isalnum())
            reply_chars += chars
            reply_sentences += sum(ch in _SENTENCE_ENDINGS for ch in fitted)
            if (
                fitted != text
                or reply_chars >= max_chars
                or reply_sentences >= max_sentences
            ):
                reply_budget_exhausted = True
            return fitted

        stream: Any = None
        try:
            self._runtime.mark_audio_event("llm_request_started")
            stream = Agent.default.llm_node(self, safe_chat_ctx, tools, model_settings)
            # default may return async gen or coroutine of async gen
            if asyncio.iscoroutine(stream):
                stream = await stream
            assert stream is not None
            async for chunk in stream:
                if task is not None and task.cancelled():
                    break
                if self._runtime.orchestrator.tts_cancel_event().is_set():
                    break
                text = _chunk_text(chunk)
                if text:
                    if not first_content_marked:
                        self._runtime.mark_audio_event("llm_first_content_token")
                        first_content_marked = True
                    gated = self._runtime.gate_llm_token(fence, text)
                    if gated is None:
                        # Stale generation — stop yielding into TTS pipeline.
                        logger.info(
                            "stale llm token dropped generation_id=%s",
                            fence.generation_id,
                        )
                        break
                    self._llm_text_buf += gated
                    for segment in segmenter.push_token(gated):
                        accepted_segment = _accept_segment(segment.text)
                        if accepted_segment is None:
                            break
                        if not first_phrase_marked:
                            # P1-6: first audible phrase while LLM stream still open.
                            self._runtime.mark_audio_event(
                                "first_phrase_ready",
                                detail={
                                    "stream_speak_while_think": True,
                                    "generation_id": fence.generation_id,
                                },
                            )
                            first_phrase_marked = True
                        yield accepted_segment
                    if reply_budget_exhausted:
                        logger.info(
                            "voice_reply_budget_reached chars=%s sentences=%s",
                            reply_chars,
                            reply_sentences,
                        )
                        break
                    if not isinstance(chunk, str):
                        delta = getattr(chunk, "delta", None)
                        if delta is not None and hasattr(chunk, "model_copy"):
                            chunk_without_text: Any = chunk
                            yield chunk_without_text.model_copy(
                                update={"delta": delta.model_copy(update={"content": None})}
                            )
                    continue
                yield chunk
            if fence.matches(self._runtime.fence) and not reply_budget_exhausted:
                for segment in segmenter.flush(end_of_stream=True):
                    accepted_segment = _accept_segment(segment.text)
                    if accepted_segment is None:
                        break
                    if not first_phrase_marked:
                        self._runtime.mark_audio_event(
                            "first_phrase_ready",
                            detail={
                                "stream_speak_while_think": True,
                                "generation_id": fence.generation_id,
                            },
                        )
                        first_phrase_marked = True
                    yield accepted_segment
        finally:
            if reply_budget_exhausted and stream is not None:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await close()
            self._runtime.orchestrator.clear_active_llm_task(task)

    def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        return self._tts_node_impl(text, model_settings)

    async def _tts_node_impl(
        self,
        text: AsyncIterable[str],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        """Gate every audio frame through GenerationFence; register cancel task."""
        if not _HAS_LIVEKIT:
            return
            yield  # pragma: no cover

        from services.agent.src.orchestration.prosody import prepare_tts_text

        fence = self._runtime.fence
        if self._runtime.tts is not None:
            self._runtime.tts.bind_fence(fence)

        task = asyncio.current_task()
        self._runtime.orchestrator.set_active_tts_task(task)
        spoken_parts: list[str] = []
        first_audio_marked = False

        async def _track_text() -> AsyncGenerator[str, None]:
            first_segment = True
            async for part in text:
                rewritten = prepare_tts_text(
                    part,
                    self._runtime.speech_plan,
                    is_first_segment=first_segment,
                    use_markup_tags=self._runtime.use_paralinguistic_tags,
                )
                first_segment = False
                if not rewritten:
                    continue
                spoken_parts.append(rewritten)
                self._runtime.update_pending_assistant_text("".join(spoken_parts))
                yield rewritten

        try:
            self._runtime.mark_audio_event("tts_task_started")
            stream = Agent.default.tts_node(self, _track_text(), model_settings)
            if asyncio.iscoroutine(stream):
                stream = await stream
            assert stream is not None
            async for frame in stream:
                if task is not None and task.cancelled():
                    break
                if self._runtime.orchestrator.tts_cancel_event().is_set():
                    break
                pcm = bytes(getattr(frame, "data", b"") or b"")
                if pcm:
                    if not first_audio_marked:
                        self._runtime.mark_audio_event(
                            "tts_first_audio_received",
                            detail={"pcm_bytes": len(pcm)},
                        )
                        first_audio_marked = True
                    gated = self._runtime.gate_tts_audio(fence, pcm)
                    if gated is None:
                        logger.info(
                            "stale tts audio dropped generation_id=%s",
                            fence.generation_id,
                        )
                        break
                yield frame
        finally:
            self._runtime.orchestrator.clear_active_tts_task(task)

    def transcription_node(
        self,
        text: AsyncIterable[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        return self._transcription_node_impl(text, model_settings)

    async def _transcription_node_impl(
        self,
        text: AsyncIterable[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        """Capture timed transcript words into HeardTextTracker."""
        if not _HAS_LIVEKIT:
            return
            yield  # pragma: no cover

        words: list[TimedWord] = []
        async for delta in Agent.default.transcription_node(self, text, model_settings):
            if isinstance(delta, TimedString) or (
                hasattr(delta, "start_time") and hasattr(delta, "end_time")
            ):
                try:
                    start = float(getattr(delta, "start_time", 0.0) or 0.0)
                    end = float(getattr(delta, "end_time", start) or start)
                    words.append(
                        TimedWord(
                            text=str(delta),
                            begin_ms=int(start * 1000),
                            end_ms=int(end * 1000),
                        )
                    )
                except Exception:
                    pass
            yield delta
        if words:
            self._runtime.orchestrator.heard_tracker.add_words(words)


def prewarm(proc: Any) -> None:
    if not _HAS_LIVEKIT:
        return
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.05,
        min_silence_duration=0.30,
        prefix_padding_duration=0.30,
        force_cpu=True,
    )


def build_turn_handling_options(profile: str) -> Any:
    """Build TurnHandlingOptions; raises on API mismatch (no silent swallow)."""
    from livekit.agents import TurnHandlingOptions, inference

    config = build_turn_handling_config(profile)
    turn_detector_version = cast(
        Literal["v1", "v1-mini"], config["turn_detection"]["version"]
    )
    return TurnHandlingOptions(
        turn_detection=inference.TurnDetector(version=turn_detector_version),
        endpointing=config["endpointing"],
        interruption=config["interruption"],
        preemptive_generation=config["preemptive_generation"],
    )


def build_session_kwargs(
    *,
    vad: Any,
    stt: Any,
    llm: Any,
    tts: Any,
    profile: str,
    offline: bool,
) -> dict[str, Any]:
    _ = offline
    session_kwargs: dict[str, Any] = {
        "vad": vad,
        "llm": llm,
        "stt": stt,
        "tts": tts,
    }
    try:
        session_kwargs["turn_handling"] = build_turn_handling_options(profile)
    except Exception as exc:
        logger.error(
            "TurnHandlingOptions construction failed; using config fallback: %s",
            exc,
            exc_info=True,
        )
        session_kwargs["turn_handling_config"] = build_turn_handling_config(profile)
        if os.getenv("ENVIRONMENT", "development") == "production":
            raise
    return session_kwargs


async def entrypoint(ctx: Any) -> None:
    """Production LiveKit entry. Wires FunASR/CosyVoice/LLM + DuplexRuntime."""
    if not _HAS_LIVEKIT:
        raise RuntimeError("livekit-agents not installed")

    await ctx.connect()

    from services.agent.src.config import AgentSettings
    from services.agent.src.providers.cosyvoice_tts import CosyVoiceTTS
    from services.agent.src.providers.deepseek import DeepSeekClient, DeepSeekConfig
    from services.agent.src.providers.funasr_stt import FunASRSTT
    from services.agent.src.providers.qwen_emotion_asr import (
        QwenEmotionConfig,
        QwenEmotionSidecar,
    )

    stt_plugin = FunASRSTT.from_env()
    tts_plugin = CosyVoiceTTS.from_env()
    try:
        await tts_plugin.pool.warm()
    except Exception as exc:
        logger.warning("CosyVoice pool warm failed (will open on demand): %s", exc)

    import httpx

    runtime_settings = AgentSettings()

    llm_plugin = openai.LLM(
        model=runtime_settings.llm_fast_model,
        api_key=runtime_settings.llm_api_key,
        base_url=runtime_settings.llm_base_url,
        temperature=float(os.getenv("DEEPSEEK_FAST_TEMPERATURE", "0.45")),
        tool_choice="auto",
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body={
            "thinking": {"type": "disabled"},
            "max_tokens": int(os.getenv("DEEPSEEK_FAST_MAX_TOKENS", "240")),
        },
    )

    profile = os.getenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    offline = os.getenv("OFFLINE_MOCK", "false").lower() == "true"

    room_name = str(ctx.room.name)
    runtime_session_id = (
        room_name.removeprefix("voice-") if room_name.startswith("voice-") else room_name
    )
    from services.agent.src.orchestration.speaker_verify import SpeakerVerifier

    speaker_verifier = SpeakerVerifier(
        enabled=runtime_settings.speaker_verify_enabled and not offline,
        enroll_speech_ms=runtime_settings.speaker_enroll_speech_ms,
        enroll_timeout_ms=runtime_settings.speaker_enroll_timeout_ms,
        accept_threshold=runtime_settings.speaker_accept_threshold,
        min_verify_speech_ms=runtime_settings.speaker_min_verify_speech_ms,
    )
    cue_playback = runtime_settings.listener_cue_playback
    cues_on = runtime_settings.listener_cues_enabled
    if cues_on and cue_playback == "background" and not runtime_settings.listener_cue_aec_validated:
        cue_playback = "main_track"
    runtime = DuplexRuntime.create(
        session_id=runtime_session_id,
        tts=tts_plugin,
        input_guard_enabled=profile == "cn_self_hosted",
        listener_cues_enabled=cues_on,
        use_paralinguistic_tags=runtime_settings.cosyvoice_paralinguistic_tags,
        speaker_verifier=speaker_verifier,
    )
    runtime.cue_scheduler.min_speech_ms = runtime_settings.listener_cue_min_speech_ms
    runtime.cue_scheduler.pause_ms = runtime_settings.listener_cue_pause_ms
    runtime.cue_scheduler.cooldown_ms = runtime_settings.listener_cue_cooldown_ms
    runtime.cue_scheduler.max_per_turn = runtime_settings.listener_cue_max_per_turn
    # Main-track cues do not need dual-track AEC; background mode still gates on it.
    runtime.set_listener_cue_aec_healthy(
        cue_playback == "main_track" or runtime_settings.listener_cue_aec_validated
    )
    if hasattr(tts_plugin, "set_trace_callback"):
        tts_plugin.set_trace_callback(
            lambda name, status, detail: runtime.mark_audio_event(
                name,
                status=status,
                detail=detail,
            )
        )
    runtime.mark_audio_event("agent_runtime_created")
    emotion_sidecar: QwenEmotionSidecar | None = None
    pcm_observers: list[Any] = [runtime.feed_speaker_pcm]
    if runtime_settings.qwen_emotion_enabled and hasattr(
        stt_plugin, "set_pcm_observer"
    ):
        emotion_sidecar = QwenEmotionSidecar(
            QwenEmotionConfig.from_env(),
            on_observation=lambda result: runtime.observe_acoustic_emotion(
                result.label,
                text=result.text,
                turn_id=result.turn_id,
            ),
        )
        runtime.set_emotion_turn_observer(emotion_sidecar.start_turn)
        if emotion_sidecar.start():
            pcm_observers.append(emotion_sidecar.feed_pcm)
            runtime.mark_audio_event("emotion_sidecar_ready")
        else:
            emotion_sidecar = None
            runtime.mark_audio_event("emotion_sidecar_ready", status="error")
    if hasattr(stt_plugin, "set_pcm_observer"):

        def _fanout_pcm(pcm: bytes) -> None:
            for observer in pcm_observers:
                with contextlib.suppress(Exception):
                    observer(pcm)

        stt_plugin.set_pcm_observer(_fanout_pcm)
    deep_client = DeepSeekClient(
        DeepSeekConfig(
            api_key=runtime_settings.llm_api_key,
            base_url=runtime_settings.llm_base_url,
            fast_model=runtime_settings.llm_fast_model,
            deep_model=runtime_settings.llm_deep_model,
            deep_total_timeout_s=float(os.getenv("DEEPSEEK_DEEP_TOTAL_TIMEOUT_S", "90")),
        )
    )
    runtime.configure_deep_path(deep_client)
    await runtime.orchestrator.ready()
    ctx.proc.userdata["duplex_runtime"] = runtime
    ctx.proc.userdata["generation_fence"] = runtime.fence
    ctx.proc.userdata["orchestrator"] = runtime.orchestrator

    session_kwargs = build_session_kwargs(
        vad=ctx.proc.userdata.get("vad"),
        stt=stt_plugin,
        llm=llm_plugin,
        tts=tts_plugin,
        profile=profile,
        offline=offline,
    )
    session_kwargs.pop("turn_handling_config", None)

    session = AgentSession(**session_kwargs)

    async def _publish_ui_event(event: dict[str, Any]) -> None:
        await ctx.room.local_participant.publish_data(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            reliable=True,
            topic="voice-agent.ui",
        )

    runtime.set_event_publisher(_publish_ui_event)
    runtime.attach_session_events(session)

    original_interrupt = session.interrupt

    def _interrupt_wrapped(*args: Any, **kwargs: Any) -> Any:
        async def _stop_livekit() -> str | None:
            await original_interrupt(*args, **kwargs)
            return None

        return asyncio.create_task(
            runtime.on_real_interrupt(
                cause="session.interrupt",
                stop_playback=_stop_livekit,
            ),
            name="duplex-session-interrupt",
        )

    session.interrupt = _interrupt_wrapped  # type: ignore[method-assign]
    runtime.set_result_speaker(
        lambda text: session.say(text, allow_interruptions=True, add_to_chat_ctx=True)
    )

    async def _play_pcm_via_room_track(pcm: bytes, *, sample_rate: int) -> None:
        """Push PCM through a temporary LocalAudioTrack (main session.say is mute post-barge-in).

        Only used while assistant playout is already stopped — unpublish when done.
        """
        from livekit import rtc

        if not pcm or len(pcm) < 4:
            return
        source = rtc.AudioSource(sample_rate, 1)
        track = rtc.LocalAudioTrack.create_audio_track("memoria-ack", source)
        pub = await ctx.room.local_participant.publish_track(track)
        try:
            frame_ms = 20
            samples = sample_rate * frame_ms // 1000
            bytes_per = samples * 2
            # Pad to whole frames
            if len(pcm) % 2:
                pcm = pcm[:-1]
            pad = (-len(pcm)) % bytes_per
            if pad:
                pcm = pcm + b"\x00" * pad
            for offset in range(0, len(pcm), bytes_per):
                chunk = pcm[offset : offset + bytes_per]
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=sample_rate,
                    num_channels=1,
                    samples_per_channel=samples,
                )
                await source.capture_frame(frame)
                await asyncio.sleep(frame_ms / 1000)
            await asyncio.sleep(0.05)
        finally:
            with contextlib.suppress(Exception):
                await ctx.room.local_participant.unpublish_track(pub.sid)

    async def _say_control_ack(text: str) -> None:
        """Short fixed ack after interrupt — prefer RTC PCM path (session.say is silent).

        Prod 20260717-195800: session.say after barge-in got first_pcm but never
        playback_started; wait_for_playout returned in ~200ms with no audible audio.
        """
        phrase = (text or "").strip() or "嗯，你说。"
        await asyncio.sleep(0.12)
        sample_rate = int(runtime_settings.cosyvoice_sample_rate or 24000)
        played = False
        if hasattr(tts_plugin, "synthesize_stream_text"):
            try:
                if hasattr(tts_plugin, "apply_speech_plan"):
                    tts_plugin.apply_speech_plan(emotion="neutral", rate=1.0)
                if hasattr(tts_plugin, "bind_fence"):
                    tts_plugin.bind_fence(runtime.fence)
                result = await tts_plugin.synthesize_stream_text(
                    [phrase],
                    fence=runtime.fence,
                )
                if result.pcm and not result.discarded:
                    await _play_pcm_via_room_track(
                        result.pcm,
                        sample_rate=sample_rate,
                    )
                    played = True
                    runtime.mark_audio_event(
                        "control_ack_played",
                        detail={
                            "text": phrase[:20],
                            "path": "room_track_pcm",
                            "pcm_bytes": len(result.pcm),
                        },
                    )
            except Exception:
                logger.warning("control ack PCM path failed", exc_info=True)
        if played:
            return
        # Last resort: session.say (often silent after barge-in).
        if hasattr(tts_plugin, "apply_speech_plan"):
            tts_plugin.apply_speech_plan(emotion="neutral", rate=1.0)
        handle = session.say(
            phrase,
            allow_interruptions=False,
            add_to_chat_ctx=False,
        )
        wait = getattr(handle, "wait_for_playout", None)
        if callable(wait):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(wait(), timeout=2.5)
        await asyncio.sleep(0.45)
        runtime.mark_audio_event(
            "control_ack_played",
            detail={"text": phrase[:20], "path": "session_say_fallback"},
        )

    async def _interrupt_yield_say(phrase: str) -> None:
        """Semantic interrupt ack:「嗯，你说。」vs「好的。」"""
        text = (phrase or "").strip() or "嗯，你说。"
        if text in {"嗯，你说", "嗯你说"}:
            text = "嗯，你说。"
        elif text in {"好的", "好"}:
            text = "好的。"
        await _say_control_ack(text)

    runtime.set_interrupt_yield(_interrupt_yield_say)

    async def _false_interrupt_recover() -> None:
        """Nearby noise stopped LiveKit playout; speaker gate rejected — continue."""
        await _say_control_ack("我继续。")
        last_user = ""
        for turn in reversed(runtime.orchestrator.context.turns):
            if turn.role == "user" and turn.content:
                last_user = turn.content.strip()
                break
        if last_user:
            with contextlib.suppress(Exception):
                await session.generate_reply(
                    instructions=(
                        f"用户刚才的问题是：{last_user}。"
                        "刚才回答被旁边杂声打断了。不要提打断或杂声，"
                        "用一两句完整自然的中文把答案说完。"
                    ),
                )

    runtime.set_false_interrupt_recover(_false_interrupt_recover)

    def _on_control_packet(packet: Any) -> None:
        topic = getattr(packet, "topic", None)
        if topic == TELEMETRY_TOPIC:
            if getattr(packet, "participant", None) is None:
                return
            try:
                telemetry = json.loads(bytes(packet.data).decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                return
            if isinstance(telemetry, dict):
                runtime.observe_client_audio_trace(telemetry)
            return
        if topic != "voice-agent.control":
            return
        # Only the LiveKit server sends packets without a participant identity.
        if getattr(packet, "participant", None) is not None:
            return
        try:
            event = json.loads(bytes(packet.data).decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return
        event_type = event.get("type")
        if event_type not in {"stop_response", "rtc_recovered"} or event.get(
            "session_id"
        ) != runtime.session_id:
            return

        async def _apply_control() -> None:
            async def _stop_livekit() -> str | None:
                with contextlib.suppress(RuntimeError):
                    await original_interrupt(force=True)
                return None

            await runtime.on_real_interrupt(
                cause=(
                    "rtc_recovered"
                    if event_type == "rtc_recovered"
                    else str(event.get("reason") or "user_button")
                ),
                stop_playback=_stop_livekit,
                create_user_turn=False,
                force_generation_bump=event_type == "rtc_recovered",
            )
            if event_type == "rtc_recovered":
                runtime.publish_assistant_state("listening")

        runtime._spawn(_apply_control(), name=f"duplex-{event_type.replace('_', '-')}")

    ctx.room.on("data_received", _on_control_packet)

    agent = DuplexVoiceAgent(instructions=VOICE_SYSTEM_PROMPT, runtime=runtime)

    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                sample_rate=24000,
                num_channels=1,
                frame_size_ms=50,
                auto_gain_control=True,
                pre_connect_audio=True,
            ),
            audio_output=build_cascade_audio_output_options(),
            text_output=room_io.TextOutputOptions(
                sync_transcription=True,
                json_format=True,
            ),
        ),
    )
    if session.output.audio is None:
        raise RuntimeError("LiveKit audio output was not attached")
    runtime.attach_playback_events(session.output.audio)
    runtime.mark_audio_event("audio_output_attached")

    async def _shutdown_runtime() -> None:
        ctx.room.off("data_received", _on_control_packet)
        if emotion_sidecar is not None:
            stt_plugin.set_pcm_observer(None)
            await emotion_sidecar.aclose()
        await runtime.close()

    ctx.add_shutdown_callback(_shutdown_runtime)

    ready_publish = runtime.publish_assistant_state("ready")
    if ready_publish is None:
        raise RuntimeError("Agent UI publisher was not configured")
    await ready_publish

    async def _say_fixed(text: str, *, interruptible: bool = False) -> None:
        """One CosyVoice stream of fixed text — avoids multi-phrase LLM TTS glitches."""
        if hasattr(tts_plugin, "apply_speech_plan"):
            tts_plugin.apply_speech_plan(emotion="neutral", rate=1.0)
        handle = session.say(
            text,
            allow_interruptions=interruptible,
            add_to_chat_ctx=False,
        )
        wait = getattr(handle, "wait_for_playout", None)
        if callable(wait):
            await wait()
        else:
            for _ in range(150):
                if not runtime._was_speaking:
                    break
                await asyncio.sleep(0.1)
        # Fixed say may not clear speaking via conversation_item path.
        runtime._was_speaking = False
        await asyncio.sleep(0.2)

    if runtime.speaker_verifier.enabled:
        # Fixed single-stream prompt (not generate_reply) so CosyVoice does not
        # split into multiple phrases that sound like a second voice / speed-up.
        runtime.publish_assistant_state("speaker_enroll")
        runtime.mark_audio_event("speaker_enroll_prompt_started")
        await _say_fixed(
            "请用正常音量连续说大约四秒，可以说：我是主人，请记住我的声音。",
            interruptible=False,
        )
        # Only start PCM enrollment after the prompt has fully finished playing.
        runtime.begin_speaker_enrollment()
        enroll_deadline = asyncio.get_running_loop().time() + (
            runtime_settings.speaker_enroll_timeout_ms / 1000.0
        )
        while asyncio.get_running_loop().time() < enroll_deadline:
            result = runtime.poll_speaker_enrollment()
            if result is not None:
                break
            # Faster poll once speech is flowing so welcome starts sooner.
            progress = runtime.speaker_verifier.enrollment_progress()
            speech_ms = int(progress.get("speech_ms") or 0)
            await asyncio.sleep(0.1 if speech_ms > 400 else 0.2)
        else:
            # Wall-clock end: force fail-open even if zero PCM was observed
            # (elapsed_ms only advances when feed_pcm runs).
            runtime.poll_speaker_enrollment(force=True)
        # Hard safety: never leave PENDING or all chat turns stay blocked.
        if runtime.speaker_verifier.state.value == "pending":
            runtime.poll_speaker_enrollment(force=True)
        runtime.publish_assistant_state("listening")
        runtime.mark_audio_event("welcome_generation_started")
        if runtime.speaker_verifier.state.value == "enrolled":
            await _say_fixed(
                "好的，已经记住你的声音了。想聊什么都可以直接说。",
                interruptible=False,
            )
        else:
            # Fail-open: do not announce "跳过声纹" — it felt like a random extra
            # sentence after the model had already answered enroll speech.
            await _say_fixed(
                "好的，想聊什么都可以直接说。",
                interruptible=False,
            )
    else:
        runtime.mark_audio_event("welcome_generation_started")
        await session.generate_reply(
            instructions="用一句自然中文打招呼，并邀请用户直接说需求。",
        )

def build_turn_handling_config(profile: str = "livekit_cloud") -> dict[str, Any]:
    """Pure config dict for tests without LiveKit types."""
    self_hosted = profile == "cn_self_hosted"
    turn_version = "v1-mini" if self_hosted else "v1"
    env_version = os.getenv("LIVEKIT_TURN_DETECTOR_VERSION")
    if not self_hosted and env_version in ("v1", "v1-mini"):
        turn_version = env_version
    # P1-5: Adaptive Interruption needs LiveKit Cloud agent-gateway. Self-hosted
    # fails with "failed to connect to LiveKit Adaptive Interruption" and falls
    # back to VAD after noisy retries — default VAD on cn_self_hosted.
    adaptive_default = "false" if self_hosted else "true"
    interruption_mode = (
        "adaptive"
        if os.getenv("LIVEKIT_ADAPTIVE_INTERRUPTION", adaptive_default).lower() == "true"
        else "vad"
    )
    # P1-6: LiveKit preemptive starts LLM before EOU — keep default off on self-hosted
    # (historically caused stuck thinking). Streaming phrase→TTS is the safe path.
    preemptive_default = "false" if self_hosted else "false"
    preemptive_enabled = (
        os.getenv("PREEMPTIVE_GENERATION", preemptive_default).lower() == "true"
    )
    preemptive_tts = os.getenv("PREEMPTIVE_TTS", "false").lower() == "true"
    return {
        "turn_detection": {"version": turn_version},
        "endpointing": {
            "mode": "dynamic",
            # Prod sample (20260717): end_of_turn_delay stuck ~2.0s (= max_delay).
            # Floor 0.90 keeps false-EOU risk low while shaving ~0.4–1.1s off the
            # user-stop → first-audio gap vs the previous 1.30/2.00 defaults.
            "min_delay": float(
                os.getenv("ENDPOINTING_MIN_DELAY_S", "0.90" if self_hosted else "0.30")
            ),
            "max_delay": float(
                os.getenv("ENDPOINTING_MAX_DELAY_S", "1.50" if self_hosted else "2.00")
            ),
            "alpha": float(os.getenv("ENDPOINTING_ALPHA", "0.85")),
        },
        "interruption": {
            "enabled": True,
            "mode": interruption_mode,
            # Slightly longer on self-hosted: short noise/echo was cancelling
            # mid-reply creative TTS (user hears "突然不说了").
            "min_duration": float(
                os.getenv("INTERRUPTION_MIN_DURATION_S", "0.55" if self_hosted else "0.25")
            ),
            "min_words": 0,
            "discard_audio_if_uninterruptible": True,
            "false_interruption_timeout": float(
                os.getenv(
                    "FALSE_INTERRUPTION_TIMEOUT_S", "1.50" if self_hosted else "1.20"
                )
            ),
            "resume_false_interruption": True,
            "backchannel_boundary": (0.50, 1.80),
        },
        "preemptive_generation": {
            "enabled": preemptive_enabled,
            "preemptive_tts": preemptive_tts and preemptive_enabled,
            "max_speech_duration": float(
                os.getenv("PREEMPTIVE_MAX_SPEECH_DURATION_S", "10.0")
            ),
            "max_retries": int(os.getenv("PREEMPTIVE_MAX_RETRIES", "2")),
        },
        # Product flag: fence-gated stream first phrase while LLM continues (not LiveKit preemptive).
        "stream_speak_while_think": True,
    }


def create_runtime_for_tests(tts: Any | None = None) -> DuplexRuntime:
    """Test helper: construct wired runtime without LiveKit room."""
    return DuplexRuntime.create(tts=tts)
