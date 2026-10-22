"""Connection-loss and cancellation callbacks for Media Voice sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import TYPE_CHECKING

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.session_recap_sink import snapshot_context_turns
from services.agent.src.voice_core.crisis_script_interrupt import (
    active_subject_id,
    publish_crisis_self_interrupt,
    unfinished_safety_announcement,
)
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession
from services.agent.src.voice_core.media_protocol import (
    MediaEnvelope,
    SessionIdentity,
    should_pause_asr_for_playback,
)
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)

if TYPE_CHECKING:
    from services.agent.src.daily_chat import DailyChatClient

logger = logging.getLogger(__name__)


class MediaSessionConnectionMixin:
    """Apply transport lifecycle facts to the one Registry-owned state record."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        reconnect_grace_s: float
        session_recap_publisher: object | None
        daily_chat_client: DailyChatClient | None
        _sessions: dict[str, _MediaVoiceSession]
        _cleanup_tasks: dict[str, asyncio.Task[None]]

        def _finish_aigc_round(self, context: _MediaVoiceSession, *, close_reason: str) -> None: ...

        async def _cancel_aigc_round_closing(self, context: _MediaVoiceSession) -> None: ...

        async def open_session(self, identity: SessionIdentity) -> _MediaVoiceSession: ...

        async def _cancel_audio_pump(self, context: _MediaVoiceSession) -> None: ...

        def _pause_owner_silence_timer(self, context: _MediaVoiceSession) -> None: ...

        def _cancel_owner_silence_timer(
            self,
            context: _MediaVoiceSession,
            *,
            preserve_remaining: bool,
        ) -> None: ...

        def _cancel_max_user_speech_watchdog(self, context: _MediaVoiceSession) -> None: ...

        def _cancel_speaking_stall_watchdog(self, context: _MediaVoiceSession) -> None: ...

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
            finalize_audible: bool = True,
        ) -> None: ...

        def _clear_pending_turn_state(self, context: _MediaVoiceSession) -> None: ...

    async def on_client_event(
        self,
        session: MediaBridgeSession,
        event: MediaEnvelope,
        detected_monotonic_ms: int = 0,
    ) -> None:
        if not session.accepts_input():
            return
        current = self._sessions.get(session.identity.session_id)
        if current is not None and (current.closed or current.standby_requested):
            return
        context = await self.open_session(session.identity)
        if context.closed or context.standby_requested or not session.accepts_input():
            return
        if event.type != "client.stop_assistant":
            return
        # The edge timestamp is wall-clock time on another host. It is useful
        # trace metadata, but never a subtraction operand in this process.
        # This metric therefore measures only Core-side stop handling; the
        # end-to-end SLO stays unavailable until trace/clock synchronization is
        # deployed and remains fail-closed in the rollout gate.
        _ = detected_monotonic_ms
        stop_started_ns = time.monotonic_ns()
        # A stop is a barge-in: a closing marker that has not started is dropped
        # (the next round's opening marker replaces it) and a playing one is released.
        context.aigc_closing_marker_state = (
            "interrupted" if context.aigc_closing_marker_state == "playing" else "skipped"
        )
        context.aigc_closing_marker_fence = None
        # MediaBridgeSession has already advanced its authoritative generation
        # before this callback runs. The Voice Core consumes that exact fence.
        previous_fence = context.output.playback.current_fence or context.runtime.fence
        if not previous_fence.matches(session.fence) and context.runtime.floor.interruptible:
            await self._record_interrupted_timed_spans(context, previous_fence)
            heard = context.output.playback.actual_heard_text(previous_fence)
            interrupted_fence = await context.runtime.on_real_interrupt(
                cause="client_stop_assistant",
                create_user_turn=False,
                synchronized_transcript=heard,
                force_generation_bump=True,
            )
            if not interrupted_fence.matches(session.fence):
                raise ValueError("Voice Core stop generation diverged from Media Edge")
            await context.runtime.on_media_playback_interrupted(
                interrupted_from=previous_fence,
                synchronized_transcript=heard,
            )
        accepted = await context.runtime.accept_media_generation(
            session.fence,
            cause="client_stop_assistant",
        )
        if not accepted:
            raise ValueError("Voice Core rejected authoritative stop generation")
        context.output.playback.start(session.fence)
        context.output.provider_complete = False
        context.output.output_complete_emitted = False
        if should_pause_asr_for_playback(context.identity):
            await context.provider.pause_asr_for_playback(context.identity)
        if not previous_fence.matches(session.fence):
            await self._cancel_reply_task(
                context,
                previous_fence,
                finalize_audible=False,
            )
        self.metrics.observe_voice_latency(
            "interrupt_core_stop",
            (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
        )

    def _release_aigc_closing_marker(self, context: _MediaVoiceSession) -> None:
        """The child took the floor or the transport was replaced mid-marker."""

        fence = context.aigc_closing_marker_fence
        if fence is None or context.aigc_closing_marker_state != "playing":
            return
        if not context.runtime.fence.matches(fence):
            context.aigc_closing_marker_fence = None
            context.aigc_closing_marker_state = "interrupted"
            logger.info(
                "AIGC closing marker interrupted session=%s fence=%s",
                context.identity.session_id,
                fence,
            )

    async def on_downlink_overflow(self, session: MediaBridgeSession) -> None:
        """Cancel the authoritative runtime when transport delivery is lost."""

        context = self._sessions.get(session.identity.session_id)
        if context is None or context.closed:
            return
        previous_fence = context.output.playback.current_fence or context.runtime.fence
        cancelled = session.fence
        if previous_fence.matches(cancelled):
            return
        if context.runtime.floor.interruptible:
            await self._record_interrupted_timed_spans(context, previous_fence)
            heard = context.output.playback.actual_heard_text(previous_fence)
            interrupted_fence = await context.runtime.on_real_interrupt(
                cause="downlink_queue_full",
                create_user_turn=False,
                synchronized_transcript=heard,
                force_generation_bump=True,
            )
            if not interrupted_fence.matches(cancelled):
                raise ValueError("Voice Core overflow generation diverged from Media Edge")
            await context.runtime.on_media_playback_interrupted(
                interrupted_from=previous_fence,
                synchronized_transcript=heard,
            )
        if not await context.runtime.accept_media_generation(
            cancelled,
            cause="downlink_queue_full",
        ):
            raise ValueError("Voice Core rejected overflow cancellation")
        context.output.playback.start(cancelled)
        context.output.provider_complete = False
        context.output.output_complete_emitted = False
        if should_pause_asr_for_playback(context.identity):
            await context.provider.pause_asr_for_playback(context.identity)
        await self._cancel_reply_task(
            context,
            previous_fence,
            finalize_audible=False,
        )

    async def on_session_closed(self, session: MediaBridgeSession) -> None:
        session_id = session.identity.session_id
        context = self._sessions.get(session_id)
        if context is not None:
            self._pause_owner_silence_timer(context)
        if session.state == "closed" or session.terminal_requested:
            await self.finalize_session(session_id)
            return
        # A gRPC stream closing is normally a transport reconnect, not a
        # conversation close. Keep the runtime/provider alive briefly so a
        # higher stream epoch can reclaim the same context without losing the
        # turn, pending reply, or generation fence.
        previous = self._cleanup_tasks.pop(session_id, None)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._expire_disconnected_session(session_id, session.identity.stream_epoch),
            name=f"media-reconnect-grace-{session_id}",
        )
        self._cleanup_tasks[session_id] = task

    async def _expire_disconnected_session(self, session_id: str, stream_epoch: int) -> None:
        try:
            await asyncio.sleep(self.reconnect_grace_s)
            current = self._sessions.get(session_id)
            bridge_session = self.bridge.bridge.get(session_id)
            if current is None or current.closed or current.identity.stream_epoch != stream_epoch:
                return
            # A replacement gRPC stream claims the Edge session before the
            # first application event reaches this registry. Inspect that
            # authoritative epoch as well, otherwise the old grace task could
            # close the newly reconnected transport/context.
            if bridge_session is None or bridge_session.identity.stream_epoch != stream_epoch:
                return
            await self.finalize_session(session_id)
        except asyncio.CancelledError:
            return
        finally:
            if self._cleanup_tasks.get(session_id) is asyncio.current_task():
                self._cleanup_tasks.pop(session_id, None)

    async def _publish_child_session_recap(self, context: _MediaVoiceSession) -> None:
        """Write the session summary/archive draft, then discard the in-memory transcript."""

        publisher = getattr(self, "session_recap_publisher", None)
        profile = context.runtime.mode_policy.runtime_profile
        subject_category = profile.profile.subject_category if profile is not None else None
        subject_id = (
            profile.profile.active_subject_id
            if profile is not None
            else context.identity.subject_id
        )
        if (
            not callable(publisher)
            or subject_category != "minor"
            or not subject_id
            or not context.identity.account_id
        ):
            return
        turns = snapshot_context_turns(context.runtime.orchestrator.context.turns)
        try:
            result = publisher(
                subject_id=subject_id,
                resource_owner_id=context.identity.account_id,
                session_id=context.identity.session_id,
                turns=turns,
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.warning(
                "session recap publish failed session=%s",
                context.identity.session_id,
                exc_info=True,
            )
        finally:
            # Scheme A: drop the ephemeral child transcript after the write attempt.
            context.runtime.orchestrator.context.clear_turns()

    async def _record_daily_chat_usage(self, context: _MediaVoiceSession) -> None:
        accrual, client = context.daily_chat_accrual, self.daily_chat_client
        if accrual is None or client is None:
            return
        elapsed = max(1, math.ceil(time.monotonic() - context.started_at))
        await client.record(accrual, elapsed_seconds=elapsed)

    async def finalize_session(self, session_id: str) -> None:
        cleanup = self._cleanup_tasks.pop(session_id, None)
        current_task = asyncio.current_task()
        if cleanup is not None and cleanup is not current_task and not cleanup.done():
            cleanup.cancel()
        context = self._sessions.get(session_id)
        if context is None:
            return
        # Terminal admission must close before waiting on a provider prepare
        # that holds turn_commit_lock. Endpoint shielding cannot keep it alive.
        context.standby_requested = True
        commit_task = context.turn_commit_task
        if (
            commit_task is not None
            and commit_task is not current_task
            and not commit_task.done()
            and not commit_task.cancelling()
        ):
            commit_task.cancel()
        await context.turn_commit_lock.acquire()
        try:
            if self._sessions.get(session_id) is not context or context.closed:
                return
            self._sessions.pop(session_id, None)
            context_stream_epoch = context.stream_epoch
            # Install the bridge tombstone before releasing the registry lock.
            # Resource teardown below can await provider I/O; late callbacks
            # must be rejected for that entire interval and must not recreate
            # this context after it leaves ``_sessions``.
            self.bridge.bridge.mark_terminal(
                session_id,
                stream_epoch=context_stream_epoch,
            )
            context.closed = True
            retry_task = context.output.output_retry_task
            context.output.output_retry_task = None
            if retry_task is not None and retry_task is not current_task:
                retry_task.cancel()
            enrollment_task = context.speaker_enrollment_task
            context.speaker_enrollment_task = None
            if (
                enrollment_task is not None
                and enrollment_task is not current_task
                and not enrollment_task.done()
            ):
                enrollment_task.cancel()
            self._cancel_owner_silence_timer(context, preserve_remaining=False)
            self._cancel_max_user_speech_watchdog(context)
            self._cancel_speaking_stall_watchdog(context)
            context.projection.discard_provisional(None, "session_closed")
            context.projection.reset_phase(stream_epoch=context.stream_epoch)
            self._clear_pending_turn_state(context)
        finally:
            context.turn_commit_lock.release()
        self.metrics.set_media_active_sessions(len(self._sessions))
        if retry_task is not None and retry_task is not current_task:
            await asyncio.gather(retry_task, return_exceptions=True)
        output_fence = (
            context.output.output_owner.fence
            if context.output.output_owner is not None
            else context.output.playback.current_fence or context.runtime.fence
        )
        # Read before cancellation. Teardown can clear the speaking latch, and
        # the opening cue has no script text yet.
        unfinished_script = unfinished_safety_announcement(context)
        await self._cancel_reply_task(
            context,
            output_fence,
            finalize_audible=False,
        )
        if context.turn_endpoint_task is not None and not context.turn_endpoint_task.done():
            context.turn_endpoint_task.cancel()
        if context.pending.turn_endpoint_timeout_handle is not None:
            context.pending.turn_endpoint_timeout_handle.cancel()
            context.pending.turn_endpoint_timeout_handle = None
        if context.pending.evidence_less_hold_handle is not None:
            context.pending.evidence_less_hold_handle.cancel()
            context.pending.evidence_less_hold_handle = None
        await self._cancel_audio_pump(context)
        await self._record_daily_chat_usage(context)
        await self._publish_child_session_recap(context)
        # Power loss and a network drop that never reconnects both land here.
        # The opening cue counts: the script is committed before its text is
        # appended, and the child has not heard it.
        if unfinished_script is not None:
            publish_crisis_self_interrupt(
                getattr(self, "crisis_script_interrupt_publisher", None),
                announced_text=unfinished_script,
                crisis_reply_heard=context.crisis_reply_heard,
                minor_user_id=active_subject_id(context),
                session_id=context.identity.session_id,
                turn_id=context.runtime.fence.turn_id,
                generation_id=context.runtime.fence.generation_id,
                tool_epoch=context.runtime.fence.tool_epoch,
            )
        # The round ends here for an explicit stop or a client close; a timed-out
        # silence round already recorded itself before its closing marker.
        await self._cancel_aigc_round_closing(context)
        self._finish_aigc_round(context, close_reason=context.standby_reason or "session_closed")
        await context.runtime.close()
        await context.provider.close(context.identity)
        # Idle/terminal teardown completed: publish the ingress buffer facts
        # without any audio content so the e2e (and an operator) can prove the
        # listening-silence frames and all queued/denoised/TAP buffers are gone.
        ingress = context.ingress
        logger.info(
            "media_audio_buffers_cleared %s",
            json.dumps(
                {
                    "session_id": context.identity.session_id,
                    "stream_epoch": context.stream_epoch,
                    "queue_frames": ingress.queue.qsize(),
                    "pcm_tap_open": ingress.pcm_tap is not None,
                    "denoiser_frames": int(ingress.denoiser.get_stats().get("total_frames", 0)),
                    "silence_dropped_frames": ingress.listening_silence_dropped_frames,
                    "silence_dropped_samples": ingress.listening_silence_dropped_samples,
                    "overflow_dropped_frames": ingress.overflow_dropped_frames,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        # The registry owns the transport session created by the bridge. Once
        # reconnect grace expires there is no replacement epoch left to claim
        # it, so remove it as well; otherwise stale identities accumulate in
        # ``MediaBridgeServer.sessions`` and can block a future open.
        self.bridge.bridge.close_if_epoch(session_id, context_stream_epoch)
