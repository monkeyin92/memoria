"""Inbound audio/VAD/KWS coordination for one Media Voice session."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.interaction_plane import (
    InteractionEvent,
    InteractionSnapshot,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.interruption import (
    InterruptionPolicy,
    evidence_from_speech_segment,
)
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)
from services.agent.src.voice_core.speech_timeline import SegmentKind, SpeechSegment

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
    from services.agent.src.voice_core.media_audio_ingress import MediaAudioIngress

media_pb2: Any = _media_pb2


class MediaSessionInputMixin:
    """Translate transport input into the existing turn/interaction fences."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        interruption_policy: InterruptionPolicy
        _audio_ingress: MediaAudioIngress

        async def _get_or_create(self, identity: SessionIdentity) -> _MediaVoiceSession: ...

        async def _apply_projection_segment(
            self, context: _MediaVoiceSession, segment: SpeechSegment
        ) -> None: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...

        async def _retire_prepare_retry_before_new_vad(
            self, context: _MediaVoiceSession
        ) -> None: ...

        async def _record_interrupted_timed_spans(
            self, context: _MediaVoiceSession, fence: GenerationFence
        ) -> None: ...

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
            cancel_timeout_s: float = 5.0,
        ) -> None: ...

        def _event_versions(
            self, context: _MediaVoiceSession, fence: GenerationFence
        ) -> tuple[int, int]: ...

    async def on_audio_frame(
        self,
        session: MediaBridgeSession,
        frame: AudioFrame,
    ) -> None:
        context = await self._get_or_create(session.identity)
        await self._audio_ingress.accept(context, frame)

    async def on_speech_segment(
        self,
        session: MediaBridgeSession,
        segment: SpeechSegment,
        detected_monotonic_ms: int = 0,
    ) -> None:
        context = await self._get_or_create(session.identity)
        if not context.runtime.ingest_media_speech_segment(segment):
            return

        if segment.kind is SegmentKind.VAD and not segment.final:
            # Only an accepted range-stamped VAD may supersede a retrying turn.
            # A replayed/stale start is observational noise and must not discard
            # the still-authoritative provisional projection.
            await self._retire_prepare_retry_before_new_vad(context)
        if segment.kind is SegmentKind.VAD:
            if context.runtime.assistant_speaking:
                # VAD is an acoustic observation, never an interrupt by
                # itself.  Project the available evidence into the shared
                # policy and only execute its reversible duck/continue flags;
                # ASR/Router still owns semantic cancellation.
                aec_verified = all(
                    value is not None
                    for value in (
                        segment.near_end_rms,
                        segment.far_end_rms,
                        segment.residual_echo_score,
                    )
                )
                interruption = self.interruption_policy.evaluate(
                    evidence_from_speech_segment(
                        segment,
                        active_generation_id=max(
                            1,
                            (context.playback.current_fence or context.runtime.fence).generation_id,
                        ),
                        aec_mode=("verified" if aec_verified else "unverified"),
                        aec_verified=aec_verified,
                    ),
                    speaker_profile=(
                        "child"
                        if context.runtime.mode_policy.runtime_profile is not None
                        and context.runtime.mode_policy.runtime_profile.profile.subject_category
                        == "minor"
                        else "adult"
                    ),
                )
            else:
                interruption = None
            await self._apply_projection_segment(context, segment)
            if segment.final:
                if not await self._audio_ingress.finalize_speech_segment(
                    context,
                    vad_start_sample=context.turn_start_sample,
                    vad_event_sample=segment.capture_start_sample,
                    voiced_end_sample=segment.voiced_end_sample,
                    finalize_reason="vad_end",
                ):
                    return
                voiced_end_sample = (
                    segment.voiced_end_sample
                    if segment.voiced_end_sample is not None
                    else segment.capture_start_sample
                )
                previous_endpoint = context.turn_endpoint_sample
                # Late/replayed VAD finals may arrive out of callback order.
                # Never move a pending endpoint backwards, or an older tail
                # event could truncate the logical turn before ASR coverage.
                context.turn_endpoint_sample = max(
                    context.turn_endpoint_sample or 0,
                    voiced_end_sample,
                )
                if context.turn_endpoint_sample != previous_endpoint:
                    if context.turn_endpoint_timeout_handle is not None:
                        context.turn_endpoint_timeout_handle.cancel()
                        context.turn_endpoint_timeout_handle = None
                    context.turn_endpoint_grace_deadline = None
                    context.turn_endpoint_tail_deadline = None
                context.turn_retire_sample = max(
                    context.turn_retire_sample or 0,
                    segment.capture_start_sample,
                )
                if context.turn_start_sample is None:
                    context.turn_start_sample = min(
                        segment.capture_start_sample,
                        context.turn_end_sample
                        if context.turn_end_sample is not None
                        else segment.capture_start_sample,
                    )
                self._schedule_turn_commit(context)
            else:
                pending_endpoint = context.turn_endpoint_sample
                if (
                    pending_endpoint is not None
                    and segment.capture_start_sample >= pending_endpoint
                ):
                    pause_s = (segment.capture_start_sample - pending_endpoint) / 16_000
                    previous_pause = context.observed_within_turn_pause_s
                    context.observed_within_turn_pause_s = (
                        pause_s if previous_pause is None else previous_pause * 0.7 + pause_s * 0.3
                    )
                if context.turn_start_sample is None:
                    # One accepted range-stamped start opens the Runtime's
                    # speaker fence. Resumed VAD segments keep the same fence;
                    # sample ranges still decide the eventual turn boundary.
                    context.runtime.on_user_voice_started()
                interaction = context.runtime.decide_interaction(
                    InteractionSnapshot(
                        event=InteractionEvent.VAD_START,
                        assistant_speaking=context.runtime.assistant_speaking,
                        has_speech_energy=True,
                    )
                )
                if interruption is not None:
                    interaction = replace(
                        interaction,
                        reason=interruption.reason,
                        duck_output=interruption.duck_output,
                        cancel_generation=False,
                        continue_output=interruption.continue_output,
                        backchannel=interruption.backchannel,
                    )
                if interaction.duck_output:
                    context.runtime.publish_assistant_audio("duck", gain=0.0)
                context.runtime.apply_interaction_decision(interaction)
                task = context.turn_endpoint_task
                if task is not None and not task.done():
                    task.cancel()
                if context.turn_endpoint_timeout_handle is not None:
                    context.turn_endpoint_timeout_handle.cancel()
                    context.turn_endpoint_timeout_handle = None
                context.turn_endpoint_sample = None
                context.turn_retire_sample = None
                context.turn_endpoint_grace_deadline = None
                context.turn_endpoint_tail_deadline = None
                context.turn_start_sample = min(
                    segment.capture_start_sample,
                    context.turn_start_sample
                    if context.turn_start_sample is not None
                    else segment.capture_start_sample,
                )
        # Sample ranges remain authoritative for media turn boundaries.
        if segment.kind is SegmentKind.KWS and segment.final:
            interruption = self.interruption_policy.evaluate(
                evidence_from_speech_segment(
                    segment,
                    active_generation_id=(
                        context.playback.current_fence or context.runtime.fence
                    ).generation_id,
                    device_monotonic_ms=detected_monotonic_ms or None,
                ),
                asr_text=segment.text,
                local_hard_stop=segment.hard_stop,
            )
            if interruption.cancel_generation:
                # Never subtract an Edge wall-clock timestamp from Core's
                # monotonic clock. The only trustworthy local measurement is
                # this handler's own work; end-to-end timing is fail-closed
                # until distributed tracing is available.
                _ = detected_monotonic_ms
                stop_started_ns = time.monotonic_ns()
                previous_fence = context.playback.current_fence or context.runtime.fence
                cancelled = (
                    session.fence
                    if not session.fence.matches(previous_fence)
                    else session.generation.cancel(previous_fence)
                )
                if cancelled is not None:
                    if not previous_fence.matches(
                        cancelled
                    ) and context.runtime.orchestrator.state.name in (
                        "SPEAKING",
                        "INTERRUPTION_PENDING",
                    ):
                        await self._record_interrupted_timed_spans(context, previous_fence)
                        heard = context.playback.actual_heard_text(previous_fence)
                        interrupted_fence = await context.runtime.on_real_interrupt(
                            cause="media_keyword_interrupt",
                            create_user_turn=False,
                            synchronized_transcript=heard,
                            force_generation_bump=True,
                        )
                        if not interrupted_fence.matches(cancelled):
                            raise ValueError(
                                "Voice Core keyword stop generation diverged from Media Edge"
                            )
                        await context.runtime.on_media_playback_interrupted(
                            interrupted_from=previous_fence,
                            synchronized_transcript=heard,
                        )
                    accepted = await context.runtime.accept_media_generation(
                        cancelled,
                        cause="media_keyword_interrupt",
                    )
                    if accepted:
                        context.playback.start(cancelled)
                        context.provider_complete = False
                        context.output_complete_emitted = False
                        await self._cancel_reply_task(context, previous_fence)
                        task_epoch, context_version = self._event_versions(context, cancelled)
                        await self.bridge.emit_realtime_effect(
                            context.identity.session_id,
                            media_pb2.REALTIME_EFFECT_KIND_CANCEL_GENERATION,
                            cancelled,
                            source_event_id="keyword_interrupt",
                            payload={"reason": "keyword_interrupt"},
                            task_epoch=task_epoch,
                            context_version=context_version,
                        )
                        self.metrics.observe_voice_latency(
                            "interrupt_core_stop",
                            (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
                        )
            fence = context.runtime.fence
            task_epoch, context_version = self._event_versions(context, fence)
            await self.bridge.emit_event(
                context.identity.session_id,
                "keyword.hit",
                {
                    "keyword": segment.text,
                    "confidence": segment.confidence,
                    "hard_stop": segment.hard_stop,
                    "start_sample": segment.capture_start_sample,
                    "end_sample": segment.capture_end_sample,
                },
                turn_id=fence.turn_id,
                generation_id=fence.generation_id,
                tool_epoch=fence.tool_epoch,
                task_epoch=task_epoch,
                context_version=context_version,
            )
