"""Authoritative device-conversation standby lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
    from services.agent.src.voice_core.media_audio_ingress import MediaAudioIngress

logger = logging.getLogger(__name__)
_CONVERSATION_STATE_CLOSED = (
    media_pb2.DESCRIPTOR.enum_types_by_name["ConversationState"]
    .values_by_name["CONVERSATION_STATE_CLOSED"]
    .number
)


class MediaSessionStandbyMixin:
    """End device conversations without creating a parallel listening state."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        owner_silence_timeout_s: float
        listening_capture_window_s: float
        max_user_speech_duration_s: float
        _audio_ingress: MediaAudioIngress
        _sessions: dict[str, _MediaVoiceSession]

        async def finalize_session(self, session_id: str) -> None: ...

        def _stream_epoch_is_current(
            self, context: _MediaVoiceSession, stream_epoch: int
        ) -> bool: ...

        aigc_notice_config: AigcNoticeConfig

        async def _play_aigc_closing_marker(self, context: _MediaVoiceSession) -> bool: ...

        def _finish_aigc_round(self, context: _MediaVoiceSession, *, close_reason: str) -> None: ...

        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: object,
        ) -> tuple[int, int]: ...

    def _owner_silence_enabled(self, context: _MediaVoiceSession) -> bool:
        return self.owner_silence_timeout_s > 0 and context.identity.client_type == "device"

    def _max_user_speech_enabled(self, context: _MediaVoiceSession) -> bool:
        return self.max_user_speech_duration_s > 0 and context.identity.client_type == "device"

    def _cancel_max_user_speech_watchdog(self, context: _MediaVoiceSession) -> None:
        """Cancel the one-utterance watchdog and clear its deadline."""

        context.pending.active_vad_stream_epoch = None
        context.pending.active_vad_start_sample = None
        task = context.pending.max_user_speech_task
        context.pending.max_user_speech_task = None
        context.pending.max_user_speech_deadline = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _arm_max_user_speech_watchdog(self, context: _MediaVoiceSession) -> None:
        """Bound one accepted VAD turn independently of owner-silence timing."""

        if (
            not self._max_user_speech_enabled(context)
            or context.closed
            or context.standby_requested
            or context.pending.turn_start_sample is None
            or self._sessions.get(context.identity.session_id) is not context
        ):
            return
        task = context.pending.max_user_speech_task
        if task is not None and not task.done():
            # The deadline is intentionally absolute for this turn. Repeated
            # VAD observations must not extend a stuck stream indefinitely.
            return
        if task is not None:
            context.pending.max_user_speech_task = None
        delay = float(self.max_user_speech_duration_s)
        loop = asyncio.get_running_loop()
        context.pending.max_user_speech_deadline = loop.time() + delay
        context.pending.max_user_speech_task = asyncio.create_task(
            self._max_user_speech_watch(context, delay),
            name=f"media-max-user-speech-{context.identity.session_id}",
        )

    def _admit_owner_silence_vad(self, context: _MediaVoiceSession, start_sample: int) -> None:
        """Transfer accepted speech from silence timing to the absolute watchdog."""

        context.pending.active_vad_stream_epoch = context.stream_epoch
        context.pending.active_vad_start_sample = max(
            context.pending.active_vad_start_sample or 0, start_sample
        )
        self._arm_max_user_speech_watchdog(context)
        watchdog_armed = context.pending.max_user_speech_task is not None
        if context.owner_silence_grace_deadline is not None:
            if not watchdog_armed:
                # Explicitly disabling the speech watchdog retains the old
                # one-shot grace. Do not remove the only remaining bound, or
                # invalidate a close already waiting for standby_lock.
                return
            self._cancel_owner_silence_timer(context, preserve_remaining=False)
            context.owner_silence_grace_deadline = None
        else:
            self._pause_owner_silence_timer(context)
        # This is admission, not verified owner activity: a bare VAD edge must
        # never refresh the silence budget or clear grace_used, or ambient noise
        # would keep the conversation open forever. Only a live watchdog can
        # replace a close already waiting for the lock, even for a non-grace
        # timeout.
        if watchdog_armed:
            self._note_owner_silence_activity(context)

    def _note_owner_silence_activity(self, context: _MediaVoiceSession) -> None:
        """Invalidate a pending silence close for evidence that arrived first.

        The acceptance decision is made from a snapshot taken when the silence
        budget ran out, and the close then waits for ``standby_lock``.  Any
        owner-activity evidence accepted before that lock is taken supersedes
        the snapshot; bumping one revision is the single decision point for
        "this close is stale", instead of every caller re-deriving the rule.
        """

        context.owner_silence_activity_revision += 1

    def _note_owner_speech_text(self, context: _MediaVoiceSession) -> None:
        """An accepted transcript is admitted owner speech, not a bare VAD edge.

        It may invalidate a silence close that snapped an earlier snapshot and
        is still waiting for ``standby_lock`` — but only while another bound
        (the absolute speech watchdog or a pending grace) still holds, so it can
        never leave the session with no bound at all.  It deliberately does not
        refresh the silence budget: text without verified speaker authority must
        not extend the conversation, or the device would keep listening to a
        room that merely produces transcripts.
        """

        if (
            context.pending.max_user_speech_task is None
            and context.owner_silence_grace_deadline is None
        ):
            return
        self._note_owner_silence_activity(context)

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
            context.pending.max_user_speech_task is not asyncio.current_task()
            or context.closed
            or context.standby_requested
            or context.pending.turn_start_sample is None
            or self._sessions.get(context.identity.session_id) is not context
        ):
            return
        context.pending.max_user_speech_task = None
        context.pending.max_user_speech_deadline = None
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
        """Stop the timer and record what the owner's measured budget now is.

        ``owner_silence_remaining_s`` has exactly three meanings, and the
        difference decides whether the next arm hands out a whole interval:

        - ``None``: nothing was measured yet, so the next arm starts the full
          interval.
        - ``0.0``: the measured budget is spent, so the next arm is immediate.
        - ``> 0.0``: paused with that much left.

        Ending a measured budget must therefore record ``0.0`` rather than
        ``None``.  ``None`` means "unmeasured", and using it for a spent window
        silently grants a session that already used its window a brand new one.
        """

        task = context.owner_silence_task
        deadline = context.owner_silence_deadline
        if preserve_remaining and deadline is not None:
            context.owner_silence_remaining_s = max(
                0.0,
                deadline - asyncio.get_running_loop().time(),
            )
        elif not preserve_remaining and deadline is not None:
            context.owner_silence_remaining_s = 0.0
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
        if context.owner_silence_grace_deadline is not None:
            # Processing grace is an absolute close deadline, not a listening
            # budget. Phase churn cannot cancel it or mint another interval.
            return
        if context.pending.active_vad_stream_epoch == context.stream_epoch:
            # A delayed listening projection must not re-arm a spent budget
            # while an accepted utterance is still open.
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

    def _listening_capture_armed(self, context: _MediaVoiceSession) -> bool:
        return (
            self.listening_capture_window_s > 0
            and context.identity.client_type == "device"
            and not context.closed
            and not context.standby_requested
        )

    def _open_listening_capture(self, context: _MediaVoiceSession) -> None:
        if context.listening_capture_hold or not self._listening_capture_armed(context):
            return
        if context.listening_capture_deadline is None:
            context.listening_capture_deadline = time.monotonic() + self.listening_capture_window_s

    def _restart_listening_capture(self, context: _MediaVoiceSession) -> None:
        if context.listening_capture_hold or not self._listening_capture_armed(context):
            return
        context.listening_capture_resume_sample = None
        context.listening_capture_deadline = time.monotonic() + self.listening_capture_window_s

    def _reopen_listening_capture_after_empty_retire(
        self,
        context: _MediaVoiceSession,
        *,
        cause: str,
    ) -> None:
        if not self._listening_capture_armed(context):
            return
        held = context.listening_capture_hold
        context.listening_capture_hold = False
        context.listening_capture_resume_sample = None
        context.listening_capture_deadline = time.monotonic() + self.listening_capture_window_s
        if held and context.runtime.interaction_phase is InteractionPhase.CONNECTING:
            context.runtime.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=cause,
            )

    def _latch_listening_capture(self, context: _MediaVoiceSession) -> None:
        if context.listening_capture_hold or not self._listening_capture_armed(context):
            return
        context.listening_capture_hold = True
        context.listening_capture_deadline = None
        self._audio_ingress.discard_queued(context)
        if context.runtime.interaction_phase in (
            InteractionPhase.LISTENING,
            InteractionPhase.USER_SPEAKING,
        ):
            context.runtime.set_interaction_phase(
                InteractionPhase.CONNECTING,
                cause="listening_capture_hold",
                publish=True,
            )
        context.runtime.publish_assistant_state("listening_closed")

    def hold_listening_capture_until_tap(self, context: _MediaVoiceSession) -> None:
        """Hold the microphone on a reclaimed transport until the child taps.

        A reconnect is not a tap, and the firmware only turns a tap into
        `listening.resume` while it believes capture is closed. The hold itself
        survives on the server, but the newly attached transport has to be told
        again, so this publishes `listening_closed` even when the capture was
        already held before the drop.
        """

        if not self._listening_capture_armed(context):
            return
        already_held = context.listening_capture_hold
        self._latch_listening_capture(context)
        if already_held:
            context.runtime.publish_assistant_state("listening_closed")

    def resume_listening_from_screen_tap(
        self,
        context: _MediaVoiceSession,
        *,
        wake_sample: int,
    ) -> None:
        if context.closed or context.standby_requested or wake_sample < 0:
            return
        context.listening_capture_hold = False
        context.listening_capture_resume_sample = wake_sample
        self._audio_ingress.discard_queued(context)
        if self._listening_capture_armed(context):
            context.listening_capture_deadline = time.monotonic() + self.listening_capture_window_s
        if context.runtime.interaction_phase is not InteractionPhase.LISTENING:
            context.runtime.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause="screen_tap",
                publish=True,
            )
        else:
            context.runtime.publish_assistant_state("listening")

    def _expire_listening_capture(self, context: _MediaVoiceSession) -> None:
        deadline = context.listening_capture_deadline
        if context.listening_capture_hold or deadline is None or time.monotonic() <= deadline:
            return
        phase = context.runtime.interaction_phase
        if phase not in (InteractionPhase.LISTENING, InteractionPhase.USER_SPEAKING):
            return
        self._latch_listening_capture(context)

    def _pause_owner_silence_timer(self, context: _MediaVoiceSession) -> None:
        if not self._owner_silence_enabled(context):
            return
        if context.owner_silence_grace_deadline is not None:
            return
        self._cancel_owner_silence_timer(context, preserve_remaining=True)

    def _sync_owner_silence_phase(
        self,
        context: _MediaVoiceSession,
        phase: str,
    ) -> None:
        """Count silence only while the Python authority says it is listening."""

        if context.listening_capture_hold:
            if context.runtime.interaction_phase is InteractionPhase.LISTENING:
                context.runtime.set_interaction_phase(
                    InteractionPhase.CONNECTING,
                    cause="listening_capture_hold",
                    publish=False,
                )
            return
        if phase == "listening":
            # Resume the owner's remaining budget; do not mint a fresh window
            # here. Assistant-only speech (a missed-hearing nudge) also lands
            # on this seam, and a reset would let the assistant's own voice
            # extend the owner-silence deadline indefinitely. A verified owner
            # turn refreshes the full interval in _finish_owner_silence_turn,
            # which is the only place owner activity is actually established.
            self._arm_owner_silence_timer(context, reset=False)
            self._open_listening_capture(context)
            self._expire_listening_capture(context)
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
        refresh_owner_budget: bool = True,
    ) -> None:
        """Resume a paused window after endpointing without trusting bare VAD."""

        self._cancel_max_user_speech_watchdog(context)
        context.pending.admitted_input_stream_epoch = None
        if not self._owner_silence_enabled(context) or context.standby_requested:
            return
        if context.owner_silence_grace_deadline is not None:
            self._cancel_owner_silence_timer(context, preserve_remaining=False)
            context.owner_silence_grace_deadline = None
            # Grace finishes admitted work; it is not new owner activity.
            context.owner_silence_remaining_s = 0.0
        owner_verified = (
            refresh_owner_budget
            and context.runtime.current_speaker_class == "owner"
            and context.runtime.current_speaker_authority_verified
        )
        # 2026-09-20 product contract (F1, user): an accepted user turn refills
        # the whole follow-up window even when speaker authority cannot verify
        # the owner -- the device has no enrollment yet, and the timeout is
        # about "did anyone speak to us", not about attributing content.
        # Refreshing only for a verified owner drained a single window across a
        # conversation: the reply finished with ~4s left and the session went
        # to standby before the next question.  Assistant-initiated speech (a
        # missed-hearing nudge) never reaches this branch, so the assistant
        # still cannot extend its own window.
        if owner_verified or (accepted and refresh_owner_budget):
            context.owner_silence_remaining_s = self.owner_silence_timeout_s
            context.owner_silence_grace_used = False
        if accepted:
            self._restart_listening_capture(context)
            self._pause_owner_silence_timer(context)
            return
        if context.runtime.floor.assistant_holds_floor:
            self._pause_owner_silence_timer(context)
            return
        self._arm_owner_silence_timer(context, reset=owner_verified)

    def _resume_owner_silence_after_reconnect(self, context: _MediaVoiceSession) -> None:
        if context.runtime.floor.listening_or_connecting:
            self._arm_owner_silence_timer(context, reset=False)

    def _aigc_closing_marker_pending(self, context: _MediaVoiceSession) -> bool:
        if context.aigc_closing_marker_state != "idle":
            return False
        return self.aigc_notice_config.position_enabled("closing")

    async def _close_after_aigc_closing_marker(
        self,
        context: _MediaVoiceSession,
        *,
        reason: str,
        expected_activity_revision: int,
    ) -> bool:
        """Close a timed-out round through its closing marker, once.

        A reply already owning the output keeps its slot and the close proceeds
        immediately; a marker that lost its slot closes at once as well.
        """

        if context.closed or context.standby_requested or context.standby_lock.locked():
            return False
        played = False
        if self._aigc_closing_marker_pending(context):
            played = await self._play_aigc_closing_marker(context)
        if not played:
            return False
        self._finish_aigc_round(context, close_reason=reason)
        if expected_activity_revision != context.owner_silence_activity_revision:
            return True
        await self._request_device_standby(
            context,
            reason=reason,
            expected_activity_revision=expected_activity_revision,
        )
        return True

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
        if context.pending.active_vad_stream_epoch == context.stream_epoch:
            if context.pending.max_user_speech_task is not None:
                self._cancel_owner_silence_timer(context, preserve_remaining=False)
                context.owner_silence_grace_deadline = None
                return
            if context.owner_silence_grace_deadline is None:
                self._pause_owner_silence_timer(context)
                return
        input_pending = (
            context.pending.turn_start_sample is not None
            or context.pending.admitted_input_stream_epoch == context.stream_epoch
            or context.turn_commit_task is not None
        )
        if input_pending and not context.owner_silence_grace_used:
            # Speaker authority exists only after endpointing. Give one bounded
            # grace window to a started utterance; ambient or stuck VAD cannot
            # extend the owner-only deadline repeatedly.
            context.owner_silence_grace_used = True
            grace_s = min(3.0, max(0.1, self.owner_silence_timeout_s))
            context.owner_silence_remaining_s = 0.0
            context.owner_silence_grace_deadline = asyncio.get_running_loop().time() + grace_s
            context.owner_silence_deadline = context.owner_silence_grace_deadline
            context.owner_silence_task = asyncio.create_task(
                self._owner_silence_watch(context, grace_s),
                name=f"media-owner-silence-grace-{context.identity.session_id}",
            )
            return
        context.owner_silence_task = None
        context.owner_silence_deadline = None
        context.owner_silence_remaining_s = 0.0
        if await self._close_after_aigc_closing_marker(
            context,
            reason="owner_silence_timeout",
            expected_activity_revision=context.owner_silence_activity_revision,
        ):
            return
        await self._request_device_standby(
            context,
            reason="owner_silence_timeout",
            expected_activity_revision=context.owner_silence_activity_revision,
        )

    async def _request_device_standby(
        self,
        context: _MediaVoiceSession,
        *,
        reason: str,
        expected_activity_revision: int | None = None,
        expected_endpoint: tuple[int, int] | None = None,
        play_line: bool = False,
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
            if expected_endpoint is not None:
                stream_epoch, endpoint_sample = expected_endpoint
                if (
                    not self._stream_epoch_is_current(context, stream_epoch)
                    or context.pending.turn_endpoint_sample != endpoint_sample
                    or not any(
                        task is not None and not task.done()
                        for task in (
                            context.turn_commit_task,
                            context.pending.turn_commit_retry_task,
                        )
                    )
                ):
                    # Tail expiry may have waited behind another close while
                    # a successful commit, new VAD, or reconnect retired it.
                    return False
            if (
                reason == "owner_silence_timeout"
                and expected_activity_revision is not None
                and expected_activity_revision != context.owner_silence_activity_revision
            ):
                # A newly accepted VAD already paused/retracted this timer.
                # Explicit close and the absolute speech watchdog have no
                # revision and can never be vetoed by acoustic observations.
                logger.info(
                    "media owner silence close superseded session=%s "
                    "expected_activity_revision=%s current_activity_revision=%s",
                    context.identity.session_id,
                    expected_activity_revision,
                    context.owner_silence_activity_revision,
                )
                return False
            context.standby_requested = True
            context.standby_reason = reason
            # Admission and this terminal flag are set without yielding on the
            # session event loop. Close cancels the tracked preparation before
            # emitting CLOSED; a shielded endpoint cannot commit behind it.
            commit_task = context.turn_commit_task
            if (
                commit_task is not None
                and commit_task is not asyncio.current_task()
                and not commit_task.done()
                and not commit_task.cancelling()
            ):
                commit_task.cancel()
            context.owner_silence_grace_deadline = None
            context.pending.admitted_input_stream_epoch = None
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
                play_line=play_line,
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
        await self.finalize_session(context.identity.session_id)
        return emitted


__all__ = ["MediaSessionStandbyMixin"]
