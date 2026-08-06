"""Connection-loss and cancellation callbacks for Media Voice sessions."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession
from services.agent.src.voice_core.media_protocol import MediaEnvelope, SessionIdentity
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)


class MediaSessionConnectionMixin:
    """Apply transport lifecycle facts to the one Registry-owned state record."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        reconnect_grace_s: float
        _sessions: dict[str, _MediaVoiceSession]
        _cleanup_tasks: dict[str, asyncio.Task[None]]

        async def _get_or_create(
            self, identity: SessionIdentity
        ) -> _MediaVoiceSession: ...

        async def _cancel_audio_pump(self, context: _MediaVoiceSession) -> None: ...

        async def _record_interrupted_timed_spans(
            self, context: _MediaVoiceSession, fence: GenerationFence
        ) -> None: ...

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
        ) -> None: ...

    async def on_client_event(
        self,
        session: MediaBridgeSession,
        event: MediaEnvelope,
        detected_monotonic_ms: int = 0,
    ) -> None:
        context = await self._get_or_create(session.identity)
        if event.type != "client.stop_assistant":
            return
        # The edge timestamp is wall-clock time on another host. It is useful
        # trace metadata, but never a subtraction operand in this process.
        # This metric therefore measures only Core-side stop handling; the
        # end-to-end SLO stays unavailable until trace/clock synchronization is
        # deployed and remains fail-closed in the rollout gate.
        _ = detected_monotonic_ms
        stop_started_ns = time.monotonic_ns()
        # MediaBridgeSession has already advanced its authoritative generation
        # before this callback runs. The Voice Core consumes that exact fence.
        previous_fence = context.playback.current_fence or context.runtime.fence
        if not previous_fence.matches(
            session.fence
        ) and context.runtime.orchestrator.state.name in ("SPEAKING", "INTERRUPTION_PENDING"):
            await self._record_interrupted_timed_spans(context, previous_fence)
            heard = context.playback.actual_heard_text(previous_fence)
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
        context.playback.start(session.fence)
        context.provider_complete = False
        context.output_complete_emitted = False
        if not previous_fence.matches(session.fence):
            await self._cancel_reply_task(context, previous_fence)
        self.metrics.observe_voice_latency(
            "interrupt_core_stop",
            (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
        )

    async def on_downlink_overflow(self, session: MediaBridgeSession) -> None:
        """Cancel the authoritative runtime when transport delivery is lost."""

        context = self._sessions.get(session.identity.session_id)
        if context is None or context.closed:
            return
        previous_fence = context.playback.current_fence or context.runtime.fence
        cancelled = session.fence
        if previous_fence.matches(cancelled):
            return
        if context.runtime.orchestrator.state.name in ("SPEAKING", "INTERRUPTION_PENDING"):
            await self._record_interrupted_timed_spans(context, previous_fence)
            heard = context.playback.actual_heard_text(previous_fence)
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
        context.playback.start(cancelled)
        context.provider_complete = False
        context.output_complete_emitted = False
        await self._cancel_reply_task(context, previous_fence)


    async def on_session_closed(self, session: MediaBridgeSession) -> None:
        session_id = session.identity.session_id
        if session.state == "closed":
            await self._finalize_session(session_id)
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
            await self._finalize_session(session_id)
        except asyncio.CancelledError:
            return
        finally:
            if self._cleanup_tasks.get(session_id) is asyncio.current_task():
                self._cleanup_tasks.pop(session_id, None)

    async def _finalize_session(self, session_id: str) -> None:
        cleanup = self._cleanup_tasks.pop(session_id, None)
        current_task = asyncio.current_task()
        if cleanup is not None and cleanup is not current_task and not cleanup.done():
            cleanup.cancel()
        context = self._sessions.get(session_id)
        if context is None:
            return
        await context.turn_commit_lock.acquire()
        try:
            if self._sessions.get(session_id) is not context or context.closed:
                return
            self._sessions.pop(session_id, None)
            context_stream_epoch = context.stream_epoch
            context.closed = True
            context.projection.discard_provisional(None, "session_closed")
        finally:
            context.turn_commit_lock.release()
        self.metrics.set_media_active_sessions(len(self._sessions))
        output_fence = (
            context.output_owner.fence
            if context.output_owner is not None
            else context.playback.current_fence or context.runtime.fence
        )
        await self._cancel_reply_task(context, output_fence)
        context.runtime.orchestrator.delegation.set_output_intent_observer(None)
        if context.turn_endpoint_task is not None and not context.turn_endpoint_task.done():
            context.turn_endpoint_task.cancel()
        if context.turn_endpoint_timeout_handle is not None:
            context.turn_endpoint_timeout_handle.cancel()
            context.turn_endpoint_timeout_handle = None
        await self._cancel_audio_pump(context)
        await context.runtime.close()
        await context.provider.close(context.identity)
        # The registry owns the transport session created by the bridge. Once
        # reconnect grace expires there is no replacement epoch left to claim
        # it, so remove it as well; otherwise stale identities accumulate in
        # ``MediaBridgeServer.sessions`` and can block a future open.
        self.bridge.bridge.close_if_epoch(session_id, context_stream_epoch)
