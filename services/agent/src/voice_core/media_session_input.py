"""Inbound audio/VAD/KWS coordination for one Media Voice session."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.conversation_projection import TurnPhase
from services.agent.src.orchestration.interaction_plane import (
    InteractionEvent,
    InteractionSnapshot,
)
from services.agent.src.orchestration.interruption_guard import PlaybackInputDecision
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.interruption import (
    InterruptionPolicy,
    evidence_from_speech_segment,
)
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession
from services.agent.src.voice_core.media_protocol import (
    AudioFrame,
    SessionIdentity,
    should_pause_asr_for_playback,
)
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)
from services.agent.src.voice_core.speech_timeline import SegmentKind, SpeechSegment

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
    from services.agent.src.voice_core.media_audio_ingress import MediaAudioIngress

media_pb2: Any = _media_pb2
logger = logging.getLogger(__name__)

_EMPTY_VAD_RMS = 1e-4
# How long a turn that already has text may stay reopened by vad.start
# without the new speech producing any text of its own. Field 2026-09-28:
# background sound kept reopening an answered weather question for 17 s.
_REOPEN_EVIDENCE_WINDOW_S = 2.0


def _is_spurious_connect_vad(segment: SpeechSegment, context: _MediaVoiceSession) -> bool:
    """True for connect-time VAD that must not steal the wake-ack idle latch.

    Wake-word tail and the local listening cue can open a sample-0 VAD with
    leftover uplink RMS.  Until the allowlisted greeting has a first frame,
    that epoch must not take the floor.  Real speech after sample 0 still
    skips the greeting so we do not talk over the user.
    """

    if segment.kind is not SegmentKind.VAD:
        return False
    rms = segment.near_end_rms
    empty = rms is not None and rms <= _EMPTY_VAD_RMS
    pending_connect = context.device_wake_ack_pending and segment.capture_start_sample == 0
    if not segment.final:
        return empty or pending_connect
    if context.turn_start_sample is not None:
        return False
    return empty or pending_connect


def _vad_precedes_pending_turn(segment: SpeechSegment, context: _MediaVoiceSession) -> bool:
    floor = context.pending_turn_onset_floor
    if segment.kind is not SegmentKind.VAD or floor is None:
        return False
    sample = segment.capture_start_sample
    if segment.final and segment.voiced_end_sample is not None:
        sample = segment.voiced_end_sample
    return sample < floor


class MediaSessionInputMixin:
    """Translate transport input into the existing turn/interaction fences."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        interruption_policy: InterruptionPolicy
        _audio_ingress: MediaAudioIngress
        _sessions: dict[str, _MediaVoiceSession]

        async def open_session(self, identity: SessionIdentity) -> _MediaVoiceSession: ...

        async def _apply_projection_segment(
            self, context: _MediaVoiceSession, segment: SpeechSegment
        ) -> None: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...

        def _arm_evidence_less_floor_hold(self, context: _MediaVoiceSession) -> None: ...

        def _clear_pending_turn_state(self, context: _MediaVoiceSession) -> None: ...

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

        async def _emit_cancel_generation(
            self,
            context: _MediaVoiceSession,
            cancelled: GenerationFence,
            *,
            heard_fence: GenerationFence,
            source_event_id: str,
            payload: dict[str, Any],
            playback_flush_required: bool | None = None,
        ) -> bool: ...

        def _sync_owner_silence_phase(
            self, context: _MediaVoiceSession, phase: str
        ) -> None: ...

        def _arm_max_user_speech_watchdog(self, context: _MediaVoiceSession) -> None: ...

        def _cancel_max_user_speech_watchdog(self, context: _MediaVoiceSession) -> None: ...

        def _admit_owner_silence_vad(
            self, context: _MediaVoiceSession, start_sample: int
        ) -> None: ...

        @staticmethod
        def _reply_in_flight(context: _MediaVoiceSession) -> bool: ...

        @staticmethod
        def _pending_turn_has_text_evidence(context: _MediaVoiceSession) -> bool: ...

        @staticmethod
        def _asr_covers_endpoint(
            context: _MediaVoiceSession, result_end_sample: int | None, endpoint_sample: int
        ) -> bool: ...

    def _admit_vad_start(self, context: _MediaVoiceSession, segment: SpeechSegment) -> bool:
        """Open accepted speech synchronously, before projection can yield."""

        if _vad_precedes_pending_turn(segment, context):
            return False
        for kind, endpoint in (
            ("clock-fact", context.clock_fact_endpoint_pinned),
            ("conversation-close", context.conversation_close_endpoint_pinned),
            ("live-query", context.live_query_endpoint_pinned),
        ):
            if endpoint is not None:
                logger.info(
                    "media vad_start ignored after %s pin session=%s "
                    "pinned_endpoint=%s vad_start=%s",
                    kind, context.identity.session_id, endpoint, segment.capture_start_sample,
                )
                return False
        if context.live_query_forced_authoritative and context.live_query_forced_text:
            logger.info(
                "media vad_start ignored after live-query forced recovery "
                "session=%s endpoint=%s vad_start=%s text_len=%s",
                context.identity.session_id, context.turn_endpoint_sample,
                segment.capture_start_sample, len(context.live_query_forced_text),
            )
            return False
        pending_endpoint = context.turn_endpoint_sample
        if pending_endpoint is not None and segment.capture_start_sample >= pending_endpoint:
            pause_s = (segment.capture_start_sample - pending_endpoint) / 16_000
            previous_pause = context.observed_within_turn_pause_s
            context.observed_within_turn_pause_s = (
                pause_s if previous_pause is None else previous_pause * 0.7 + pause_s * 0.3
            )
        if context.turn_start_sample is None:
            # One accepted start opens the speaker fence. Resumed VAD keeps
            # that fence, including turns whose range was first created by ASR.
            voice_decision = context.runtime.on_user_voice_started()
            if voice_decision is PlaybackInputDecision.IGNORE:
                return False
            context.turn_input_fence = context.runtime.fence
        if pending_endpoint is not None and segment.capture_start_sample >= pending_endpoint:
            self._arm_reopen_evidence_window(context, pending_endpoint)
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
        if context.evidence_less_hold_since is not None:
            # Resumed VAD resets the tail timeout above, but may only nudge,
            # never lift, the cap on output waiting behind an empty turn.
            self._arm_evidence_less_floor_hold(context)
        context.turn_start_sample = min(
            segment.capture_start_sample,
            context.turn_start_sample
            if context.turn_start_sample is not None
            else segment.capture_start_sample,
        )
        self._admit_owner_silence_vad(context, segment.capture_start_sample)
        logger.info(
            "media vad start admitted session=%s stream_epoch=%s sample=%s "
            "silence_remaining_s=%s grace_active=%s watchdog_armed=%s vad_revision=%s",
            context.identity.session_id, context.stream_epoch, segment.capture_start_sample,
            context.owner_silence_remaining_s, context.owner_silence_grace_deadline is not None,
            context.max_user_speech_task is not None, context.owner_silence_activity_revision,
        )
        return True

    def _arm_reopen_evidence_window(
        self, context: _MediaVoiceSession, endpoint_sample: int
    ) -> None:
        """Bound how long speech without any text may hold an answered turn open.

        A vad.start after a text-covered endpoint reopens the turn, and every
        later one does again, so steady background sound defers the reply
        indefinitely. The window starts at the first such reopen and is never
        extended by later ones: if the new speech has produced no text of its
        own when it closes, the turn commits at the text-covered endpoint.
        """

        if context.reopen_evidence_handle is not None:
            return
        if (
            context.identity.client_type != "device"
            or not self._pending_turn_has_text_evidence(context)
            or not self._asr_covers_endpoint(context, context.turn_end_sample, endpoint_sample)
            or self._reply_in_flight(context)
        ):
            return
        context.reopen_evidence_endpoint = endpoint_sample
        context.reopen_evidence_turn_end = context.turn_end_sample
        context.reopen_evidence_turn_start = context.turn_start_sample
        context.reopen_evidence_handle = asyncio.get_running_loop().call_later(
            _REOPEN_EVIDENCE_WINDOW_S,
            self._expire_reopen_evidence_window,
            context.identity.session_id,
            context.stream_epoch,
            endpoint_sample,
        )

    def _expire_reopen_evidence_window(
        self, session_id: str, stream_epoch: int, endpoint_sample: int
    ) -> None:
        context = self._sessions.get(session_id)
        if context is None or context.reopen_evidence_endpoint != endpoint_sample:
            return
        turn_end_at_reopen = context.reopen_evidence_turn_end
        turn_start_at_reopen = context.reopen_evidence_turn_start
        context.reopen_evidence_handle = None
        context.reopen_evidence_endpoint = None
        context.reopen_evidence_turn_end = None
        context.reopen_evidence_turn_start = None
        partial = context.pending_partial
        new_text = (context.turn_end_sample or 0) > (turn_end_at_reopen or 0) or (
            partial is not None
            and partial.text.strip()
            and partial.capture_end_sample > endpoint_sample
        )
        if (
            context.closed
            or context.standby_requested
            or context.stream_epoch != stream_epoch
            # The same logical turn: a turn that committed meanwhile and a
            # newer one must never be committed at this older endpoint.
            or context.turn_start_sample is None
            or context.turn_start_sample != turn_start_at_reopen
            or new_text
            or self._reply_in_flight(context)
        ):
            return
        # Back to the text-covered endpoint: a VAD end inside the noise would
        # never be covered by ASR, so committing there would stall again.
        if context.turn_endpoint_timeout_handle is not None:
            context.turn_endpoint_timeout_handle.cancel()
            context.turn_endpoint_timeout_handle = None
        context.turn_endpoint_sample = endpoint_sample
        context.turn_retire_sample = max(context.turn_retire_sample or 0, endpoint_sample)
        context.turn_endpoint_grace_deadline = time.monotonic()
        context.turn_endpoint_tail_deadline = None
        self._cancel_max_user_speech_watchdog(context)
        logger.info(
            "media reopened turn committing without new text session=%s "
            "endpoint=%s window_s=%.1f",
            session_id,
            endpoint_sample,
            _REOPEN_EVIDENCE_WINDOW_S,
        )
        self._schedule_turn_commit(context)

    async def on_audio_frame(
        self,
        session: MediaBridgeSession,
        frame: AudioFrame,
    ) -> None:
        # The transport can finish a CLOSED projection before an in-flight
        # callback unwinds.  Reject at this seam before open_session so a
        # late frame cannot resurrect a registry/runtime context.
        if not session.accepts_input():
            return
        current = self._sessions.get(session.identity.session_id)
        if current is not None and (current.closed or current.standby_requested):
            return
        context = await self.open_session(session.identity)
        if (
            context.closed
            or context.standby_requested
            or not session.accepts_input()
        ):
            return
        await self._audio_ingress.accept(context, frame)

    async def on_speech_segment(
        self,
        session: MediaBridgeSession,
        segment: SpeechSegment,
        detected_monotonic_ms: int = 0,
    ) -> None:
        if not session.accepts_input():
            return
        current = self._sessions.get(session.identity.session_id)
        if current is not None and (current.closed or current.standby_requested):
            return
        context = await self.open_session(session.identity)
        if (
            context.closed
            or context.standby_requested
            or not session.accepts_input()
        ):
            return
        if _is_spurious_connect_vad(segment, context):
            logger.info(
                "media empty vad ignored session=%s sample=%s rms=%s final=%s "
                "pending_wake_ack=%s",
                context.identity.session_id,
                segment.capture_start_sample,
                segment.near_end_rms,
                segment.final,
                context.device_wake_ack_pending,
            )
            return
        if _vad_precedes_pending_turn(segment, context):
            logger.info(
                "media vad ignored before pending turn boundary session=%s "
                "stream_epoch=%s sample=%s floor=%s final=%s",
                context.identity.session_id, segment.stream_epoch,
                segment.capture_start_sample, context.pending_turn_onset_floor, segment.final,
            )
            return
        if not context.runtime.ingest_media_speech_segment(segment):
            return

        if segment.kind is SegmentKind.VAD and not segment.final:
            # Only an accepted range-stamped VAD may supersede a retrying turn.
            # A replayed/stale start is observational noise and must not discard
            # the still-authoritative provisional projection.
            await self._retire_prepare_retry_before_new_vad(context)
        if (
            context.closed
            or context.standby_requested
            or not session.accepts_input()
            or context.stream_epoch != segment.stream_epoch
            or self._sessions.get(session.identity.session_id) is not context
            or _vad_precedes_pending_turn(segment, context)
        ):
            return
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
                # A VAD edge that will open a new turn has no classification
                # of its own yet: the runtime still holds the previous
                # utterance's decision until ``on_user_voice_started`` resets
                # it below. Never project that stale authority onto this
                # candidate.
                turn_open = context.turn_start_sample is not None
                interruption = self.interruption_policy.evaluate(
                    evidence_from_speech_segment(
                        segment,
                        active_generation_id=max(
                            1,
                            (context.playback.current_fence or context.runtime.fence).generation_id,
                        ),
                        aec_mode=("verified" if aec_verified else "unverified"),
                        aec_verified=aec_verified,
                        speaker_class=(
                            context.runtime.current_speaker_class if turn_open else "uncertain"
                        ),
                        owner_authority_verified=(
                            turn_open
                            and context.runtime.current_speaker_class == "owner"
                            and context.runtime.current_speaker_authority_verified
                        ),
                        speaker_reason_code=(
                            context.runtime.current_speaker_reason_code
                            if turn_open
                            else "classification_pending"
                        ),
                    ),
                    speaker_profile=(
                        "child"
                        if context.runtime.mode_policy.runtime_profile is not None
                        and context.runtime.mode_policy.runtime_profile.profile.subject_category
                        == "minor"
                        else "adult"
                    ),
                    device_conversation=context.identity.client_type == "device",
                )
            else:
                interruption = None
            # Keep interruption evidence and voice admission in one synchronous
            # block: neither stale owner authority nor an expired timer can
            # cross the projection publication await below.
            vad_start_admitted = segment.final or self._admit_vad_start(context, segment)
            await self._apply_projection_segment(context, segment)
            if (
                context.closed
                or context.standby_requested
                or not session.accepts_input()
                or context.stream_epoch != segment.stream_epoch
                or self._sessions.get(session.identity.session_id) is not context
                or _vad_precedes_pending_turn(segment, context)
            ):
                return
            if segment.final:
                if (
                    context.active_vad_stream_epoch == segment.stream_epoch
                    and context.active_vad_start_sample is not None
                    and segment.capture_start_sample < context.active_vad_start_sample
                ):
                    # An older endpoint must not stop the watchdog for speech
                    # admitted while this event was awaiting projection.
                    return
                if (
                    context.identity.client_type == "device"
                    and context.runtime.playback_overlap_input_blocked()
                ):
                    # Wake/playback echo must not arm ASR tail timeouts.  The
                    # board emits VAD edges once; dropping vad.end here would
                    # desynchronise the uplink for the rest of the session.
                    logger.info(
                        "media vad_end ignored during playback session=%s "
                        "endpoint_sample=%s",
                        context.identity.session_id,
                        segment.voiced_end_sample or segment.capture_start_sample,
                    )
                    self._clear_pending_turn_state(context)
                    return
                if context.runtime.formal_speaker_enrollment_active:
                    self._cancel_max_user_speech_watchdog(context)
                    # Drain the PCM pump before snapshotting. Skipping this
                    # left enrollment with a few milliseconds of audio even
                    # when the user spoke a full sentence.
                    await self._audio_ingress._wait_until_idle(context)
                    context.runtime.on_user_voice_stopped()
                    await context.provider.pause_asr_for_playback(context.identity)
                    context.ingress.last_finalized_audio_watermark = max(
                        context.ingress.last_finalized_audio_watermark,
                        context.asr.last_sent_sample,
                    )
                    self._clear_pending_turn_state(context)
                    return
                # Keep the accepted utterance's absolute deadline across ASR
                # finalization: silence is paused and endpoint-tail timing
                # has not taken ownership yet. A hung provider must stay bounded.
                if not await self._audio_ingress.finalize_speech_segment(
                    context,
                    vad_start_sample=context.turn_start_sample,
                    vad_event_sample=segment.capture_start_sample,
                    voiced_end_sample=segment.voiced_end_sample,
                    finalize_reason="vad_end",
                ):
                    return
                if (
                    context.closed
                    or context.standby_requested
                    or not session.accepts_input()
                    or context.stream_epoch != segment.stream_epoch
                    or self._sessions.get(session.identity.session_id) is not context
                    or _vad_precedes_pending_turn(segment, context)
                    or (
                        context.active_vad_start_sample is not None
                        and segment.capture_start_sample < context.active_vad_start_sample
                    )
                ):
                    # Provider finalization is another yield point: a newer
                    # utterance (or terminal/reconnected session) must not be
                    # endpointed by the old VAD when its ASR call returns.
                    return
                voiced_end_sample = (
                    segment.voiced_end_sample
                    if segment.voiced_end_sample is not None
                    else segment.capture_start_sample
                )
                if context.clock_fact_endpoint_pinned is not None:
                    logger.info(
                        "media vad_end ignored after clock-fact pin session=%s "
                        "pinned_endpoint=%s voiced_end=%s",
                        context.identity.session_id,
                        context.clock_fact_endpoint_pinned,
                        voiced_end_sample,
                    )
                    return
                if context.conversation_close_endpoint_pinned is not None:
                    logger.info(
                        "media vad_end ignored after conversation-close pin session=%s "
                        "pinned_endpoint=%s voiced_end=%s",
                        context.identity.session_id,
                        context.conversation_close_endpoint_pinned,
                        voiced_end_sample,
                    )
                    return
                if context.live_query_endpoint_pinned is not None:
                    logger.info(
                        "media vad_end ignored after live-query pin session=%s "
                        "pinned_endpoint=%s voiced_end=%s",
                        context.identity.session_id,
                        context.live_query_endpoint_pinned,
                        voiced_end_sample,
                    )
                    return
                if (
                    context.live_query_forced_authoritative
                    and context.live_query_forced_text
                ):
                    logger.info(
                        "media vad_end ignored after live-query forced recovery "
                        "session=%s endpoint=%s voiced_end=%s text_len=%s",
                        context.identity.session_id,
                        context.turn_endpoint_sample,
                        voiced_end_sample,
                        len(context.live_query_forced_text),
                    )
                    return
                # This synchronous handoff follows the post-await fences and
                # pin checks. Only the current endpoint may release the speech
                # watchdog; _schedule_turn_commit below arms the existing tail.
                self._cancel_max_user_speech_watchdog(context)
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
                if (
                    not vad_start_admitted
                    or context.active_vad_stream_epoch != segment.stream_epoch
                ):
                    return
                interaction = context.runtime.decide_interaction(
                    InteractionSnapshot(
                        event=InteractionEvent.VAD_START,
                        assistant_speaking=context.runtime.assistant_speaking,
                        has_speech_energy=True,
                        turn_phase=context.projection.phase,
                    )
                )
                if interruption is not None:
                    if (
                        interruption.cancel_generation
                        and context.projection.phase is TurnPhase.ACOUSTIC_ONLY
                    ):
                        self.metrics.inc_media_metric("voice_acoustic_only_cancel_blocked_total")
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
                device_conversation=context.identity.client_type == "device",
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
                    if (
                        not previous_fence.matches(cancelled)
                        and context.runtime.floor.interruptible
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
                        if should_pause_asr_for_playback(context.identity):
                            await context.provider.pause_asr_for_playback(context.identity)
                        await self._cancel_reply_task(context, previous_fence)
                        await self._emit_cancel_generation(
                            context,
                            cancelled,
                            heard_fence=previous_fence,
                            source_event_id="keyword_interrupt",
                            payload={"reason": "keyword_interrupt"},
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
