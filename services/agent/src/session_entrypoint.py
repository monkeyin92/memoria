"""AgentSession entrypoint and per-session wiring for the duplex voice agent."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any, Literal, cast

from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.agent_voice_profile import _apply_cached_voice_profile
from services.agent.src.config import load_turn_timing
from services.agent.src.device_vad import (
    DEVICE_ENDPOINTING_MAX_DELAY_S,
    DEVICE_ENDPOINTING_MIN_DELAY_S,
    DEVICE_POST_PLAYBACK_HOLDOFF_S,
    DEVICE_TURN_TRANSCRIPT_TIMEOUT_S,
    DeviceVadProjector,
    commit_device_user_turn_after_asr,
    device_turn_commit_busy,
)
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.fixed_speech import FixedSpeechPlayer
from services.agent.src.orchestration.formal_speaker_enrollment import (
    run_formal_speaker_enrollment,
)
from services.agent.src.orchestration.utterance_router import InterruptSemanticVerdict
from services.agent.src.providers.interrupt_semantic_classifier import (
    InterruptSemanticClassifier,
    InterruptSemanticClassifierConfig,
)
from services.agent.src.response_planner_client import ResponsePlannerClient
from services.agent.src.runtime_speaker import KeywordSpotterBinding
from services.agent.src.tutor_session import production_system_prompt
from services.agent.src.voice_profile_client import VoiceProfileClient
from services.common.companions import companion_definition
from services.common.miniprogram_gateway_ticket import (
    DEVICE_AGENT_DISPATCH_METADATA,
    MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    MINIPROGRAM_AEC_FAILED,
    MINIPROGRAM_AEC_FAILED_ACK,
    MINIPROGRAM_AEC_HEALTH_ACK_TOPIC,
    MINIPROGRAM_AEC_HEALTH_TOPIC,
    MINIPROGRAM_AGENT_DISPATCH_METADATA,
)

try:
    from livekit import rtc
    from livekit.agents import AgentSession, llm, room_io
    from livekit.plugins import openai, silero

    _HAS_LIVEKIT = True
except ImportError:  # pragma: no cover
    _HAS_LIVEKIT = False
    rtc = None  # type: ignore[assignment]
    AgentSession = None  # type: ignore[assignment,misc]
    llm = None  # type: ignore[assignment]
    room_io = None  # type: ignore[assignment]
    openai = None  # type: ignore[assignment]
    silero = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

TELEMETRY_TOPIC = "voice-agent.telemetry"
CASCADE_OPUS_MAX_BITRATE = 64_000


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


def apply_miniprogram_session_audio_policy(
    session_kwargs: dict[str, Any],
    dispatch_metadata: object,
) -> bool:
    """Disable LiveKit's warmup only when this job has gateway AEC."""
    if not is_miniprogram_aec_session(dispatch_metadata):
        return False
    session_kwargs["aec_warmup_duration"] = None
    return True


def is_miniprogram_aec_session(dispatch_metadata: object) -> bool:
    return dispatch_metadata == MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA


def is_miniprogram_session(dispatch_metadata: object) -> bool:
    return isinstance(dispatch_metadata, str) and dispatch_metadata in {
        MINIPROGRAM_AGENT_DISPATCH_METADATA,
        MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    }


def is_device_session(dispatch_metadata: object) -> bool:
    return dispatch_metadata == DEVICE_AGENT_DISPATCH_METADATA


def build_keyword_spotter_pcm_observer(
    runtime: DuplexRuntime,
    spotter: Any,
) -> Callable[[bytes], None]:
    """Decode one fenced VAD epoch and accept only its complete result."""

    active_binding: KeywordSpotterBinding | None = None
    closed_binding: KeywordSpotterBinding | None = None

    def _observe(pcm: bytes) -> None:
        nonlocal active_binding, closed_binding
        binding = runtime.keyword_spotter_binding()
        if binding == closed_binding:
            return
        if binding != active_binding:
            spotter.reset()
            active_binding = binding
            closed_binding = None
        if binding is None:
            return
        spotter.feed_pcm(pcm)

    def _finalize(binding: KeywordSpotterBinding | None) -> None:
        nonlocal active_binding, closed_binding
        if binding is None or binding != active_binding:
            spotter.reset()
            active_binding = None
            closed_binding = None
            return
        keyword = spotter.finish_utterance()
        active_binding = None
        closed_binding = binding
        if keyword:
            runtime.observe_keyword_spotter_hit(keyword, binding=binding)

    runtime.set_keyword_spotter_finalizer(_finalize)
    return _observe


def should_enable_legacy_speaker_verifier(settings: Any, *, offline: bool) -> bool:
    """Keep the old per-session enrollment separate from formal authority."""
    enabled = bool(getattr(settings, "speaker_verify_enabled", False))
    authority_enabled = bool(getattr(settings, "speaker_authority_enabled", False))
    if enabled and authority_enabled:
        logger.warning(
            "legacy speaker enrollment disabled because formal speaker authority is enabled"
        )
    return enabled and not authority_enabled and not offline


def prewarm(proc: Any) -> None:
    if not _HAS_LIVEKIT:
        return
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.05,
        min_silence_duration=0.30,
        prefix_padding_duration=0.30,
        force_cpu=True,
    )


def build_turn_handling_options(
    profile: str,
    *,
    interruptions_enabled: bool = True,
    device_vad: bool = False,
) -> Any:
    """Build TurnHandlingOptions; raises on API mismatch (no silent swallow)."""
    from livekit.agents import TurnHandlingOptions, inference

    config = build_turn_handling_config(
        profile,
        interruptions_enabled=interruptions_enabled,
        device_vad=device_vad,
    )
    turn_detection = config["turn_detection"]
    if isinstance(turn_detection, str):
        turn_detection_mode: Any = turn_detection
    else:
        turn_detector_version = cast(Literal["v1", "v1-mini"], turn_detection["version"])
        turn_detection_mode = inference.TurnDetector(version=turn_detector_version)
    return TurnHandlingOptions(
        turn_detection=turn_detection_mode,
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
    interruptions_enabled: bool = True,
    device_vad: bool = False,
) -> dict[str, Any]:
    _ = offline
    session_kwargs: dict[str, Any] = {
        "vad": vad,
        "llm": llm,
        "stt": stt,
        "tts": tts,
    }
    try:
        session_kwargs["turn_handling"] = build_turn_handling_options(
            profile,
            interruptions_enabled=interruptions_enabled,
            device_vad=device_vad,
        )
    except Exception as exc:
        logger.error(
            "TurnHandlingOptions construction failed; using config fallback: %s",
            exc,
            exc_info=True,
        )
        session_kwargs["turn_handling_config"] = build_turn_handling_config(
            profile,
            interruptions_enabled=interruptions_enabled,
            device_vad=device_vad,
        )
        if os.getenv("ENVIRONMENT", "development") == "production":
            raise
    return session_kwargs


async def entrypoint(ctx: Any) -> None:
    """Production LiveKit entry. Wires FunASR/Doubao TTS/LLM + DuplexRuntime."""
    if not _HAS_LIVEKIT:
        raise RuntimeError("livekit-agents not installed")

    dispatch_metadata = getattr(getattr(ctx, "job", None), "metadata", "")
    await ctx.connect()

    from services.agent.src.config import AgentSettings
    from services.agent.src.providers.handlers import build_voice_provider_handlers
    from services.agent.src.providers.qwen_emotion_asr import (
        QwenEmotionConfig,
        QwenEmotionSidecar,
    )
    from services.agent.src.providers.vosk_kws import (
        VoskKeywordSpotter,
        VoskKeywordSpotterConfig,
    )

    runtime_settings = AgentSettings()
    provider_handlers = await build_voice_provider_handlers(
        settings=runtime_settings,
        llm_factory=openai.LLM,
    )
    stt_plugin = provider_handlers.asr
    llm_plugin = provider_handlers.language_model
    tts_plugin = provider_handlers.speech_synthesis
    realtime_search_resolver = provider_handlers.realtime_search_resolver
    realtime_search_model = provider_handlers.realtime_search_model

    profile = os.getenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    offline = os.getenv("OFFLINE_MOCK", "false").lower() == "true"
    miniprogram_session = is_miniprogram_session(dispatch_metadata)
    device_session = is_device_session(dispatch_metadata)
    controlled_half_duplex_session = miniprogram_session or device_session
    miniprogram_aec_session = is_miniprogram_aec_session(dispatch_metadata)

    room_name = str(ctx.room.name)
    runtime_session_id = (
        room_name.removeprefix("voice-") if room_name.startswith("voice-") else room_name
    )
    from services.agent.src.orchestration.speaker_verify import SpeakerVerifier

    speaker_verifier = SpeakerVerifier(
        enabled=should_enable_legacy_speaker_verifier(runtime_settings, offline=offline),
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
        input_guard_enabled=profile == "cn_self_hosted" or miniprogram_aec_session,
        trusted_aec_playback_control=miniprogram_aec_session,
        barge_in_enabled=not controlled_half_duplex_session,
        capture_release_holdoff_s=(
            DEVICE_POST_PLAYBACK_HOLDOFF_S if device_session else 0.0
        ),
        listener_cues_enabled=cues_on,
        use_paralinguistic_tags=False,
        speaker_verifier=speaker_verifier,
    )
    interrupt_semantic_classifier: InterruptSemanticClassifier | None = None
    if (
        miniprogram_aec_session
        and runtime.barge_in_enabled
        and runtime_settings.interrupt_semantic_enabled
        and not offline
    ):
        interrupt_semantic_classifier = InterruptSemanticClassifier(
            InterruptSemanticClassifierConfig(
                api_key=runtime_settings.dashscope_api_key,
                base_url=runtime_settings.dashscope_compatible_base_url,
                model=runtime_settings.interrupt_semantic_model,
                timeout_s=runtime_settings.interrupt_semantic_timeout_s,
            )
        )

        async def _resolve_interrupt_semantic(
            final_text: str,
            sticky_text: str,
            assistant_text: str,
        ) -> InterruptSemanticVerdict:
            assert interrupt_semantic_classifier is not None
            return await interrupt_semantic_classifier.classify(
                final_text=final_text,
                sticky_text=sticky_text,
                assistant_text=assistant_text,
            )

        runtime.set_interrupt_semantic_resolver(_resolve_interrupt_semantic)
    from services.agent.src.conversation_close_wiring import (
        install_conversation_close_semantic_resolver,
    )
    from services.agent.src.live_lookup_wiring import install_live_lookup_semantic_resolver

    live_lookup_classifier = None
    close_intent_classifier = None
    if not offline:
        live_lookup_classifier = install_live_lookup_semantic_resolver(
            runtime,
            runtime_settings,
        )
        close_intent_classifier = install_conversation_close_semantic_resolver(
            runtime,
            runtime_settings,
        )
    from services.agent.src.policy_runtime_wiring import (
        install_runtime_policy_clients,
    )

    mode_policy_client, action_policy_client, effect_commit_client = (
        await install_runtime_policy_clients(
            runtime=runtime,
            settings=runtime_settings,
            session_id=runtime_session_id,
            offline=offline,
        )
    )
    if runtime_settings.speaker_authority_enabled and not offline:
        from services.agent.src.speaker_authority_client import (
            SpeakerAuthorityClient,
            SpeakerAuthorityClientConfig,
        )

        speaker_authority = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint=runtime_settings.speaker_authority_url,
                internal_token=runtime_settings.speaker_internal_token.get_secret_value(),
                timeout_s=runtime_settings.speaker_authority_timeout_s,
            )
        )

        async def _classify_speaker(pcm: bytes, sample_rate: int) -> Any:
            try:
                return await speaker_authority.classify(
                    session_id=runtime_session_id,
                    pcm=pcm,
                    sample_rate=sample_rate,
                )
            finally:
                runtime.set_reject_non_owner_voice(speaker_authority.reject_non_owner_voice)

        runtime.set_speaker_classifier(
            _classify_speaker,
            sample_rate=runtime_settings.funasr_sample_rate,
            timeout_s=runtime_settings.speaker_authority_timeout_s,
        )
        runtime.set_target_speaker_focus(True)
    archive_sink = None
    archive_token = runtime_settings.internal_token("archive_write")
    archive_spool_key = runtime_settings.archive_spool_key.get_secret_value()
    if runtime_settings.archive_sink_enabled and archive_token and archive_spool_key:
        from services.agent.src.archive_sink import ArchiveSink, ArchiveSinkConfig

        archive_sink = ArchiveSink(
            ArchiveSinkConfig(
                endpoint=runtime_settings.archive_session_events_url,
                internal_token=archive_token,
                spool_path=Path(runtime_settings.archive_spool_path),
                spool_key=archive_spool_key,
                spool_max_bytes=runtime_settings.archive_spool_max_bytes,
            )
        )

        async def _publish_evidence(event: dict[str, Any]) -> None:
            delivered = await archive_sink.publish(event)
            if not delivered:
                logger.warning(
                    "archive event queued in encrypted spool event_id=%s", event["event_id"]
                )

        async def _publish_owner_turn(
            event: dict[str, Any],
            pcm: bytes,
            sample_rate: int,
        ) -> None:
            delivered = await archive_sink.publish_owner_turn(
                event,
                pcm=pcm,
                sample_rate=sample_rate,
            )
            if not delivered:
                logger.warning(
                    "owner archive turn queued in shared encrypted spool event_id=%s",
                    event["event_id"],
                )

        async def _replay_archive_spool() -> None:
            try:
                replayed = await archive_sink.replay()
                if replayed:
                    logger.info("archive spool replayed events=%s", replayed)
            except Exception:
                logger.error("archive spool replay failed", exc_info=True)

        runtime.set_evidence_publisher(_publish_evidence)
        runtime.set_owner_turn_publisher(_publish_owner_turn)
        runtime._spawn(_replay_archive_spool(), name="archive-spool-replay")
    elif runtime_settings.archive_sink_enabled:
        logger.warning("archive sink is disabled because token or spool key is not configured")
    response_planner_client = None
    response_plan_token = runtime_settings.internal_token("response_plan")
    if response_plan_token and not offline:
        from services.agent.src.response_planner_client import ResponsePlannerClientConfig

        response_planner_client = ResponsePlannerClient(
            ResponsePlannerClientConfig(
                endpoint=runtime_settings.response_plan_url,
                internal_token=response_plan_token,
                timeout_s=runtime_settings.response_plan_timeout_s,
            )
        )
    elif not offline:
        logger.warning("response plan is disabled because token is unavailable")
    voice_profile_client = None
    voice_token = runtime_settings.internal_token("voice_resolution")
    if (
        runtime_settings.voice_profile_enabled
        and voice_token
        and not offline
    ):
        from services.agent.src.voice_profile_client import VoiceProfileClientConfig

        voice_profile_client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint=runtime_settings.voice_profile_url,
                internal_token=voice_token,
                timeout_s=runtime_settings.voice_profile_timeout_s,
            )
        )
        runtime.set_voice_profile_refresher(
            lambda: voice_profile_client.refresh(session_id=runtime_session_id)
        )
        await voice_profile_client.refresh(session_id=runtime_session_id)
        _apply_cached_voice_profile(
            tts_plugin=tts_plugin,
            client=voice_profile_client,
            session_id=runtime_session_id,
            mode=runtime.mode_policy.mode,
            policy=runtime.mode_policy,
        )
    elif runtime_settings.voice_profile_enabled and not offline:
        logger.warning("voice profile is disabled because token is unavailable")
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
    if hasattr(stt_plugin, "set_trace_callback"):
        stt_plugin.set_trace_callback(
            lambda name, status, detail: runtime.mark_audio_event(
                name,
                status=status,
                detail=detail,
            )
        )
    runtime.set_keyword_spotter_finalizer(getattr(stt_plugin, "flush_speech_segment", lambda _: None))
    runtime.mark_audio_event("agent_runtime_created")
    emotion_sidecar: QwenEmotionSidecar | None = None
    pcm_observers: list[Any] = [runtime.feed_speaker_pcm]
    if runtime_settings.qwen_emotion_enabled and hasattr(stt_plugin, "set_pcm_observer"):
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
    keyword_spotter: VoskKeywordSpotter | None = None
    if (
        miniprogram_aec_session
        and runtime.barge_in_enabled
        and runtime_settings.miniprogram_kws_enabled
        and hasattr(stt_plugin, "set_pcm_observer")
    ):
        keyword_spotter = VoskKeywordSpotter.try_create(
            VoskKeywordSpotterConfig.from_settings(runtime_settings)
        )
        if keyword_spotter is not None:
            pcm_observers.append(build_keyword_spotter_pcm_observer(runtime, keyword_spotter))
            runtime.mark_audio_event("keyword_spotter_ready")
        else:
            runtime.mark_audio_event("keyword_spotter_ready", status="error")
    if hasattr(stt_plugin, "set_pcm_observer"):
        def _fanout_pcm(pcm: bytes) -> None:
            for observer in pcm_observers:
                with contextlib.suppress(Exception):
                    observer(pcm)

        stt_plugin.set_pcm_observer(_fanout_pcm)
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
        interruptions_enabled=not controlled_half_duplex_session,
        device_vad=device_session,
    )
    session_kwargs.pop("turn_handling_config", None)
    if apply_miniprogram_session_audio_policy(session_kwargs, dispatch_metadata):
        logger.info(
            "mini_program_session_audio_policy aec_warmup_duration=disabled session_id=%s",
            runtime_session_id,
        )

    session = AgentSession(**session_kwargs)
    runtime.set_user_turn_clearer(session.clear_user_turn)

    async def _publish_ui_event(event: dict[str, Any]) -> None:
        await ctx.room.local_participant.publish_data(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            reliable=True,
            topic="voice-agent.ui",
        )

    runtime.set_event_publisher(_publish_ui_event)
    runtime.attach_session_events(session)

    original_interrupt = session.interrupt
    runtime.set_playback_stop_seam(lambda: original_interrupt())
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

    async def _target_speaker_interrupt() -> None:
        async def _stop_livekit() -> str | None:
            await original_interrupt()
            return None

        await runtime.on_real_interrupt(
            cause="target_speaker_confirmed",
            stop_playback=_stop_livekit,
        )

    runtime.set_target_speaker_interrupt(_target_speaker_interrupt)
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
        ack_fence = runtime.fence
        await asyncio.sleep(0.12)
        if not ack_fence.matches(runtime.fence):
            runtime.mark_audio_event(
                "control_ack_played",
                status="ignored",
                detail={"reason": "stale_fence"},
            )
            return
        sample_rate = int(runtime_settings.doubao_tts_sample_rate or 24000)
        played = False
        if hasattr(tts_plugin, "synthesize_stream_text"):
            try:
                if hasattr(tts_plugin, "apply_speech_plan"):
                    tts_plugin.apply_speech_plan(
                        emotion="neutral",
                        rate=1.0,
                        fence=ack_fence,
                    )
                if hasattr(tts_plugin, "bind_fence"):
                    tts_plugin.bind_fence(ack_fence)
                result = await tts_plugin.synthesize_stream_text(
                    [phrase],
                    fence=ack_fence,
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
                            "ack_len": len(phrase),
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
            tts_plugin.apply_speech_plan(
                emotion="neutral",
                rate=1.0,
                fence=ack_fence,
            )
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
            detail={"ack_len": len(phrase), "path": "session_say_fallback"},
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
        """Nearby noise stopped playout; acknowledge without starting a new generation."""
        await _say_control_ack("我继续。")

    runtime.set_false_interrupt_recover(_false_interrupt_recover)

    device_turn_commit_task: asyncio.Task[Any] | None = None

    def _set_device_uplink(enabled: bool) -> None:
        setter = getattr(stt_plugin, "set_pcm_enabled", None)
        if callable(setter):
            setter(enabled)
        logger.info(
            "device uplink_pcm enabled=%s session_id=%s",
            enabled,
            runtime_session_id,
        )

    def _on_device_start() -> None:
        nonlocal device_turn_commit_task
        task = device_turn_commit_task
        if task is not None and device_turn_commit_busy(task):
            task.cancel()
            device_turn_commit_task = None
            logger.info(
                "device VAD start cancelled commit_in_flight session_id=%s",
                runtime_session_id,
            )
        _set_device_uplink(True)

    def _commit_device_turn(*, delay_s: float) -> None:
        nonlocal device_turn_commit_task
        vad = device_vad
        if vad is None:
            return
        _set_device_uplink(False)
        if device_turn_commit_busy(device_turn_commit_task):
            logger.info(
                "device VAD end skipped commit_in_flight session_id=%s",
                runtime_session_id,
            )
            return
        now = monotonic()
        # Anchor the ASR wait to the start of the utterance. A commit deferred
        # past the playback holdoff would otherwise run after FunASR already
        # returned its final and mistake real speech for an empty transcript.
        since = vad.speech_started_at or now
        timeout = DEVICE_TURN_TRANSCRIPT_TIMEOUT_S + max(0.0, now - since)

        async def _commit() -> None:
            if delay_s > 0.0:
                await asyncio.sleep(delay_s)
                if vad.speech_started_during_playback:
                    # Whole utterance sat inside the playback tail window and
                    # began while the speaker was live: echo, not a user turn.
                    logger.info(
                        "user_turn_ignored reason=echo_during_playback "
                        "session_id=%s",
                        runtime_session_id,
                    )
                    return
            await commit_device_user_turn_after_asr(
                session,
                session_id=runtime_session_id,
                stt=stt_plugin,
                since=since,
                timeout=timeout,
            )

        device_turn_commit_task = runtime._spawn(
            _commit(), name="device-vad-turn-commit"
        )

    def _on_device_endpoint() -> None:
        _commit_device_turn(delay_s=0.0)

    def _on_device_endpoint_deferred(delay_s: float) -> None:
        _commit_device_turn(delay_s=max(0.0, delay_s))

    device_vad = (
        DeviceVadProjector(
            session,
            runtime_session_id,
            on_start=_on_device_start,
            on_endpoint=_on_device_endpoint,
            on_endpoint_deferred=_on_device_endpoint_deferred,
        )
        if device_session
        else None
    )
    if device_vad is not None:
        _set_device_uplink(False)
        clearer = getattr(stt_plugin, "clear_pcm_drain", None)
        if callable(clearer):
            clearer()

        def _on_device_phase(phase: Any, previous: Any) -> None:
            phase_name = getattr(phase, "value", str(phase))
            previous_name = getattr(previous, "value", str(previous))
            if phase_name == "speaking":
                device_vad.set_playback_active(True)
                _set_device_uplink(False)
                flush = getattr(stt_plugin, "flush_speech_segment", None)
                if callable(flush):
                    flush()
                return
            # Only arm the tail window when playback actually ends. Timing it
            # from the start of playback cannot cover a long reply and has
            # already expired before the speaker goes quiet.
            if previous_name == "speaking":
                device_vad.set_playback_active(False)
                device_vad.begin_playback_holdoff()

        runtime.set_phase_listener(_on_device_phase)

    def _on_control_packet(packet: Any) -> None:
        if device_vad is not None and device_vad.accept(packet):
            return
        topic = getattr(packet, "topic", None)
        if topic == MINIPROGRAM_AEC_HEALTH_TOPIC:
            if not miniprogram_aec_session or getattr(packet, "participant", None) is None:
                return
            try:
                event = json.loads(bytes(packet.data).decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                return
            failure_id = event.get("failure_id") if isinstance(event, dict) else None
            if (
                not isinstance(event, dict)
                or event.get("type") != MINIPROGRAM_AEC_FAILED
                or event.get("session_id") != runtime.session_id
                or not isinstance(failure_id, str)
                or not 1 <= len(failure_id) <= 64
            ):
                return
            runtime.revoke_trusted_aec_playback_control(cause="gateway_apm_failed")

            async def _ack_aec_failure() -> None:
                await ctx.room.local_participant.publish_data(
                    json.dumps(
                        {
                            "type": MINIPROGRAM_AEC_FAILED_ACK,
                            "session_id": runtime.session_id,
                            "failure_id": failure_id,
                        },
                        separators=(",", ":"),
                    ),
                    reliable=True,
                    topic=MINIPROGRAM_AEC_HEALTH_ACK_TOPIC,
                )

            runtime._spawn(_ack_aec_failure(), name="miniprogram-aec-failed-ack")
            return
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
        if (
            event_type not in {"stop_response", "rtc_recovered"}
            or event.get("session_id") != runtime.session_id
        ):
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
    # Persona/ServiceMode come from the frozen signed RuntimeProfile (runtime_profile_gate.py).
    agent_instructions = production_system_prompt(runtime)
    if miniprogram_session:
        agent_instructions += (
            "\n\n当前客户端是受控半双工小程序。普通回答只说一到三句、最多一百二十个"
            "中英文数字字符，优先先给完整结论；只有用户明确要求故事、朗读、详细方案或继续时才展开。"
        )
    elif device_session:
        agent_instructions += (
            "\n\n当前客户端是无端侧 AEC 的受控半双工硬件机器人。普通回答只说一到三句、最多"
            "一百二十个中英文数字字符，优先先给完整结论；只有用户明确要求故事、朗读、详细方案"
            "或继续时才展开。"
        )
    fast_model_warmer = getattr(llm_plugin, "prewarm", None)
    agent = DuplexVoiceAgent(
        instructions=agent_instructions,
        runtime=runtime,
        voice_profile_client=voice_profile_client,
        response_planner_client=response_planner_client,
        realtime_search_resolver=realtime_search_resolver,
        realtime_search_model=realtime_search_model,
        fast_model_warmer=fast_model_warmer if callable(fast_model_warmer) else None,
        llm_provider=runtime_settings.llm_provider,
        llm_model=runtime_settings.llm_fast_model,
        tts_provider="volcengine_doubao",
        tts_model=runtime_settings.doubao_tts_resource_id,
    )

    async def _handle_text_input(
        active_session: Any,
        event: Any,
    ) -> None:
        await agent.handle_text_input(active_session, event)

    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=room_io.RoomOptions(
            text_input=room_io.TextInputOptions(
                text_input_cb=_handle_text_input,
            ),
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
        shutdown_errors: list[Exception] = []

        async def _close_component(label: str, operation: Any) -> None:
            try:
                await operation
            except Exception as exc:
                logger.error("shutdown component failed component=%s", label, exc_info=True)
                shutdown_errors.append(exc)

        if hasattr(stt_plugin, "set_pcm_observer"):
            stt_plugin.set_pcm_observer(None)
        if hasattr(stt_plugin, "set_trace_callback"):
            stt_plugin.set_trace_callback(None)
        if emotion_sidecar is not None:
            await _close_component("emotion_sidecar", emotion_sidecar.aclose())
        await _close_component("runtime", runtime.close())
        if interrupt_semantic_classifier is not None:
            await _close_component(
                "interrupt_semantic_classifier",
                interrupt_semantic_classifier.aclose(),
            )
        if live_lookup_classifier is not None:
            await _close_component(
                "live_lookup_classifier",
                live_lookup_classifier.aclose(),
            )
        if close_intent_classifier is not None:
            await _close_component(
                "close_intent_classifier",
                close_intent_classifier.aclose(),
            )
        if realtime_search_resolver is not None:
            await _close_component(
                "realtime_search_resolver",
                realtime_search_resolver.aclose(),
            )
        await _close_component("tts", tts_plugin.aclose())
        if response_planner_client is not None:
            await _close_component("response_planner_client", response_planner_client.aclose())
        if voice_profile_client is not None:
            await _close_component("voice_profile_client", voice_profile_client.close())
        if mode_policy_client is not None:
            await _close_component("mode_policy_client", mode_policy_client.aclose())
        if action_policy_client is not None:
            await _close_component("action_policy_client", action_policy_client.aclose())
        if effect_commit_client is not None:
            await _close_component("effect_commit_client", effect_commit_client.aclose())
        if archive_sink is not None:
            await _close_component("archive_sink", archive_sink.close())
        if shutdown_errors:
            raise RuntimeError("one or more Agent shutdown components failed") from shutdown_errors[
                0
            ]

    ctx.add_shutdown_callback(_shutdown_runtime)

    ready_publish = runtime.publish_assistant_state("ready")
    if ready_publish is None:
        raise RuntimeError("Agent UI publisher was not configured")
    await ready_publish

    fixed_speech = FixedSpeechPlayer(session=session, runtime=runtime, tts=tts_plugin)

    companion = companion_definition(runtime.mode_policy.companion_style_id)
    welcome_text = companion.welcome_text if companion is not None else "嗨，想聊什么就直接说吧。"
    welcome_emotion = companion.default_voice_emotion if companion is not None else "neutral"
    welcome_rate = companion.default_voice_rate if companion is not None else 1.0
    welcome_instruction = companion.voice_instruction if companion is not None else ""

    # Formal biometric setup is device-only.  The control API resolves the
    # account from this active voice session; the Mini Program only displays
    # the resulting status and never supplies PCM.
    if device_session and runtime_settings.speaker_authority_enabled and not offline:
        try:
            from services.agent.src.speaker_authority_client import (
                SpeakerAuthorityClient,
                SpeakerAuthorityClientConfig,
            )

            formal_authority = SpeakerAuthorityClient(
                SpeakerAuthorityClientConfig(
                    endpoint=runtime_settings.speaker_authority_url,
                    internal_token=runtime_settings.speaker_internal_token.get_secret_value(),
                    timeout_s=runtime_settings.speaker_authority_timeout_s,
                )
            )
            enrollment_status = await formal_authority.enrollment_status(
                session_id=runtime.session_id,
            )
            enrollment_state = str(
                (enrollment_status.get("enrollment") or {}).get("state") or "blocked"
            )
            if enrollment_state in {"requested", "required"}:
                intent_id = str((enrollment_status.get("enrollment") or {}).get("intent_id") or "")
                if intent_id:

                    async def _say_enrollment_prompt(text: str) -> None:
                        await fixed_speech.say(
                            text,
                            interruptible=False,
                            restore_state="speaker_enroll",
                        )

                    await run_formal_speaker_enrollment(
                        runtime=runtime,
                        speak=_say_enrollment_prompt,
                        authority=formal_authority,
                        intent_id=intent_id,
                    )
                else:
                    logger.info(
                        "formal speaker enrollment skipped missing intent "
                        "session_id=%s state=%s",
                        runtime.session_id,
                        enrollment_state,
                    )
        except Exception:
            logger.warning(
                "formal speaker enrollment setup unavailable session_id=%s",
                runtime.session_id,
                exc_info=True,
            )

    if runtime.speaker_verifier.enabled:
        # Fixed single-stream prompt (not generate_reply) so TTS does not
        # split into multiple phrases that sound like a second voice / speed-up.
        enroll_publish = runtime.publish_assistant_state("speaker_enroll")
        if enroll_publish is None:
            raise RuntimeError("Agent UI publisher was not configured")
        await enroll_publish
        runtime.mark_audio_event("speaker_enroll_prompt_started")
        await fixed_speech.say(
            "请用正常音量连续说大约四秒，可以说：我是主人，请记住我的声音。",
            interruptible=False,
            restore_state="speaker_enroll",
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
            # Wall-clock end: leave legacy spectral guard unavailable, never owner.
            # (elapsed_ms only advances when feed_pcm runs).
            runtime.poll_speaker_enrollment(force=True)
        # Hard safety: never leave PENDING or all chat turns stay blocked.
        if runtime.speaker_verifier.state.value == "pending":
            runtime.poll_speaker_enrollment(force=True)
        runtime.mark_audio_event("welcome_generation_started")
        if runtime.speaker_verifier.state.value == "enrolled":
            await fixed_speech.say(
                f"好的，已经记住你的声音了。{welcome_text}",
                interruptible=False,
                emotion=welcome_emotion,
                rate=welcome_rate,
                instruction=welcome_instruction,
            )
        else:
            # Fail-open: do not announce "跳过声纹" — it felt like a random extra
            # sentence after the model had already answered enroll speech.
            await fixed_speech.say(
                f"好的。{welcome_text}",
                interruptible=False,
                emotion=welcome_emotion,
                rate=welcome_rate,
                instruction=welcome_instruction,
            )
    else:
        runtime.mark_audio_event("welcome_generation_started")
        await fixed_speech.say(
            welcome_text,
            interruptible=True,
            emotion=welcome_emotion,
            rate=welcome_rate,
            instruction=welcome_instruction,
        )


def build_turn_handling_config(
    profile: str = "livekit_cloud",
    *,
    interruptions_enabled: bool = True,
    device_vad: bool = False,
) -> dict[str, Any]:
    """Pure config dict for tests without LiveKit types."""
    self_hosted = profile == "cn_self_hosted"
    endpointing_min_delay, endpointing_max_delay, false_interruption_timeout = load_turn_timing(
        profile
    )
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
    preemptive_enabled = os.getenv("PREEMPTIVE_GENERATION", preemptive_default).lower() == "true"
    preemptive_tts = os.getenv("PREEMPTIVE_TTS", "false").lower() == "true"
    # Device sessions receive authoritative VAD boundaries from the hardware
    # gateway. FunASR END_OF_SPEECH is not a reliable commit signal: empty
    # finals never raise the STT speaking flag, so stt-mode EOU never fires.
    # Manual mode plus wait-for-ASR then commit_device_user_turn is the only path.
    turn_detection: Any = "manual" if device_vad else {"version": turn_version}
    return {
        "turn_detection": turn_detection,
        "endpointing": {
            "mode": "dynamic",
            # Production 20260730 observed a provider transcript 2.05s after
            # turn commit. Keep the prior conservative window so natural pauses
            # do not start playback on the first incomplete final.
            # Device VAD already waited for the FunASR final; do not add the
            # self-hosted 1.5s EOU sleep on top of that.
            "min_delay": (
                DEVICE_ENDPOINTING_MIN_DELAY_S if device_vad else endpointing_min_delay
            ),
            "max_delay": (
                DEVICE_ENDPOINTING_MAX_DELAY_S if device_vad else endpointing_max_delay
            ),
            "alpha": float(os.getenv("ENDPOINTING_ALPHA", "0.85")),
        },
        "interruption": {
            "enabled": interruptions_enabled,
            "mode": interruption_mode,
            # The semantic/echo guard restores false interruptions, so the VAD
            # threshold only needs to catch a short「停」without waiting 550ms.
            "min_duration": float(
                os.getenv("INTERRUPTION_MIN_DURATION_S", "0.35" if self_hosted else "0.25")
            ),
            "min_words": 0,
            "discard_audio_if_uninterruptible": True,
            "false_interruption_timeout": false_interruption_timeout,
            "resume_false_interruption": True,
            "backchannel_boundary": (0.50, 1.80),
        },
        "preemptive_generation": {
            "enabled": preemptive_enabled,
            "preemptive_tts": preemptive_tts and preemptive_enabled,
            "max_speech_duration": float(os.getenv("PREEMPTIVE_MAX_SPEECH_DURATION_S", "10.0")),
            "max_retries": int(os.getenv("PREEMPTIVE_MAX_RETRIES", "2")),
        },
        # Product flag: fence-gated stream first phrase while LLM continues (not LiveKit preemptive).
        "stream_speak_while_think": True,
    }


def create_runtime_for_tests(tts: Any | None = None) -> DuplexRuntime:
    """Test helper: construct wired runtime without LiveKit room."""
    return DuplexRuntime.create(tts=tts)
