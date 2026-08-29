"""Authoritative device-conversation standby lifecycle."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer

logger = logging.getLogger(__name__)
_CONVERSATION_STATE_CLOSED = media_pb2.DESCRIPTOR.enum_types_by_name[
    "ConversationState"
].values_by_name["CONVERSATION_STATE_CLOSED"].number


class MediaSessionStandbyMixin:
    """End device conversations without creating a parallel listening state."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        owner_silence_timeout_s: float
        max_user_speech_duration_s: float
        _sessions: dict[str, _MediaVoiceSession]

        async def _finalize_session(self, session_id: str) -> None: ...

        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: object,
        ) -> tuple[int, int]: ...

    def _owner_silence_enabled(self, context: _MediaVoiceSession) -> bool:
        return self.owner_silence_timeout_s > 0 and context.identity.client_type == "device"

    def _max_user_speech_enabled(self, context: _MediaVoiceSession) -> bool:
        return (
            self.max_user_speech_duration_s > 0
            and context.identity.client_type == "device"
        )

    def _cancel_max_user_speech_watchdog(self, context: _MediaVoiceSession) -> None:
        """Cancel the one-utterance watchdog and clear its deadline."""

        task = context.max_user_speech_task
        context.max_user_speech_task = None
        context.max_user_speech_deadline = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _arm_max_user_speech_watchdog(self, context: _MediaVoiceSession) -> None:
        """Bound one accepted VAD turn independently of owner-silence timing."""

        if (
            not self._max_user_speech_enabled(context)
            or context.closed
            or context.standby_requested
            or context.turn_start_sample is None
            or self._sessions.get(context.identity.session_id) is not context
        ):
            return
        task = context.max_user_speech_task
        if task is not None and not task.done():
            # The deadline is intentionally absolute for this turn. Repeated
            # VAD observations must not extend a stuck stream indefinitely.
            return
        if task is not None:
            context.max_user_speech_task = None
        delay = float(self.max_user_speech_duration_s)
        loop = asyncio.get_running_loop()
        context.max_user_speech_deadline = loop.time() + delay
        context.max_user_speech_task = asyncio.create_task(
            self._max_user_speech_watch(context, delay),
            name=f"media-max-user-speech-{context.identity.session_id}",
        )

    async def _max_user_speech_watch(
        self,
        context: _MediaVoiceSession,
        delay: float,
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if (
            context.max_user_speech_task is not asyncio.current_task()
            or context.closed
            or context.standby_requested
            or context.turn_start_sample is None
            or self._sessions.get(context.identity.session_id) is not context
        ):
            return
        context.max_user_speech_task = None
        context.max_user_speech_deadline = None
        logger.warning(
            "media user speech watchdog expired session=%s duration_s=%.3f",
            context.identity.session_id,
            delay,
        )
        await self._request_device_standby(
            context,
            reason="max_user_speech_duration_timeout",
        )

    def _cancel_owner_silence_timer(
        self,
        context: _MediaVoiceSession,
        *,
        preserve_remaining: bool,
    ) -> None:
        task = context.owner_silence_task
        deadline = context.owner_silence_deadline
        if preserve_remaining and deadline is not None:
            context.owner_silence_remaining_s = max(
                0.0,
                deadline - asyncio.get_running_loop().time(),
            )
        elif not preserve_remaining:
            context.owner_silence_remaining_s = None
        context.owner_silence_deadline = None
        context.owner_silence_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _arm_owner_silence_timer(
        self,
        context: _MediaVoiceSession,
        *,
        reset: bool,
    ) -> None:
        if (
            not self._owner_silence_enabled(context)
            or context.closed
            or context.standby_requested
            or self._sessions.get(context.identity.session_id) is not context
        ):
            return
        self._cancel_owner_silence_timer(
            context,
            preserve_remaining=not reset,
        )
        delay = (
            self.owner_silence_timeout_s
            if reset or context.owner_silence_remaining_s is None
            else context.owner_silence_remaining_s
        )
        context.owner_silence_remaining_s = delay
        context.owner_silence_deadline = asyncio.get_running_loop().time() + delay
        context.owner_silence_task = asyncio.create_task(
            self._owner_silence_watch(context, delay),
            name=f"media-owner-silence-{context.identity.session_id}",
        )

    def _pause_owner_silence_timer(self, context: _MediaVoiceSession) -> None:
        if not self._owner_silence_enabled(context):
            return
        self._cancel_owner_silence_timer(context, preserve_remaining=True)

    def _sync_owner_silence_phase(
        self,
        context: _MediaVoiceSession,
        phase: str,
    ) -> None:
        """Count silence only while the Python authority says it is listening."""

        if phase == "listening":
            # A new post-reply listening window gives the owner the full
            # configured interval. Ambient VAD never reaches this reset seam.
            self._arm_owner_silence_timer(context, reset=True)
            context.owner_silence_grace_used = False
        else:
            # Silence is measured only while the authority is listening.  In
            # particular, an open VAD turn must not consume the owner's
            # post-reply window while its endpoint is still pending.
            self._pause_owner_silence_timer(context)

    def _finish_owner_silence_turn(
        self,
        context: _MediaVoiceSession,
        *,
        accepted: bool,
    ) -> None:
        """Resume a paused window after endpointing without trusting bare VAD."""

        self._cancel_max_user_speech_watchdog(context)
        if not self._owner_silence_enabled(context) or context.standby_requested:
            return
        owner_verified = (
            context.runtime.current_speaker_class == "owner"
            and context.runtime.current_speaker_authority_verified
        )
        if owner_verified:
            context.owner_silence_remaining_s = self.owner_silence_timeout_s
            context.owner_silence_grace_used = False
        if accepted:
            # The accepted turn moves to thinking/output. The assistant-state
            # projection will open a fresh timer only after playback returns
            # the floor to the owner.
            self._pause_owner_silence_timer(context)
            return
        if context.runtime.assistant_speaking or context.runtime.interaction_phase.value in {
            "thinking_silent",
            "speaking",
            "backchannel",
            "interrupted",
        }:
            self._pause_owner_silence_timer(context)
            return
        self._arm_owner_silence_timer(context, reset=owner_verified)

    def _resume_owner_silence_after_reconnect(self, context: _MediaVoiceSession) -> None:
        if context.runtime.interaction_phase.value in {"connecting", "listening"}:
            self._arm_owner_silence_timer(context, reset=False)

    async def _owner_silence_watch(
        self,
        context: _MediaVoiceSession,
        delay: float,
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if (
            context.owner_silence_task is not asyncio.current_task()
            or context.closed
            or context.standby_requested
            or self._sessions.get(context.identity.session_id) is not context
        ):
            return
        if context.turn_start_sample is not None and not context.owner_silence_grace_used:
            # Speaker authority exists only after endpointing. Give one bounded
            # grace window to a started utterance; ambient or stuck VAD cannot
            # extend the owner-only deadline repeatedly.
            context.owner_silence_grace_used = True
            grace_s = min(3.0, max(0.1, self.owner_silence_timeout_s))
            context.owner_silence_remaining_s = grace_s
            context.owner_silence_deadline = asyncio.get_running_loop().time() + grace_s
            context.owner_silence_task = asyncio.create_task(
                self._owner_silence_watch(context, grace_s),
                name=f"media-owner-silence-grace-{context.identity.session_id}",
            )
            return
        context.owner_silence_task = None
        context.owner_silence_deadline = None
        context.owner_silence_remaining_s = None
        await self._request_device_standby(context, reason="owner_silence_timeout")

    async def _request_device_standby(
        self,
        context: _MediaVoiceSession,
        *,
        reason: str,
    ) -> bool:
        if not reason or len(reason) > 128:
            raise ValueError("conversation close reason must be 1-128 characters")
        async with context.standby_lock:
            if (
                context.closed
                or context.standby_requested
                or self._sessions.get(context.identity.session_id) is not context
            ):
                return False
            context.standby_requested = True
            context.standby_reason = reason
            self._cancel_owner_silence_timer(context, preserve_remaining=False)
            self._cancel_max_user_speech_watchdog(context)
            fence = context.runtime.fence
            task_epoch, context_version = self._event_versions(context, fence)
            emitted = await self.bridge.emit_conversation_state(
                context.identity.session_id,
                _CONVERSATION_STATE_CLOSED,
                fence=fence,
                reason=reason,
                task_epoch=task_epoch,
                context_version=context_version,
            )
            self.metrics.inc_media_metric(
                "voice_conversation_standby_total",
                labels={"reason": reason},
            )
            if not emitted:
                logger.warning(
                    "conversation close could not reach Media Edge session=%s reason=%s",
                    context.identity.session_id,
                    reason,
                )
        # The typed CLOSED event is already queued. Resource teardown can now
        # revoke reply tasks and the bridge session without losing the event
        # held by the transport's independent priority queue.
        await self._finalize_session(context.identity.session_id)
        return emitted


__all__ = ["MediaSessionStandbyMixin"]
