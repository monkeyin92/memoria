"""AIGC notice hooks for one device conversation round.

A round is point-screen wake through session end: the owner-silence timeout,
an explicit stop, or an anti-addiction refusal/limit. Each round opens with an
optional configured marker and closes with one. Both hooks are silently absent
when their config disables them or names no resource.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from services.agent.src.aigc_generation_log import AigcRoundLogger, AigcRoundRecord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.delegation_coordinator import DelegationCoordinator
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)
from services.agent.src.voice_core.aigc_notice import (
    AIGC_NOTICE_SAMPLE_RATE_HZ,
    AigcNoticeConfig,
    AigcNoticePosition,
    opening_marker_ready_at,
)
from services.agent.src.voice_core.media_protocol import PlaybackEventType, PlaybackProgress
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)
from services.agent.src.voice_core.media_session_types import OutputWork as _OutputWork

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer

logger = logging.getLogger(__name__)

_MARKER_TTL_MS = 10_000
_MARKER_FLOOR_WAIT_S = 1.0
DEFAULT_AIGC_PCM_SAMPLE_RATE_HZ = AIGC_NOTICE_SAMPLE_RATE_HZ
_MARKER_DRAIN_GRACE_S = 0.3
_MARKER_POLL_S = 0.02
_CLOSE_REASON_MAX_CHARS = 128


def close_reason_for(close_reason: str) -> str:
    """Bound the close reason recorded in the AIGC round log."""

    cleaned = close_reason.strip()
    if not cleaned:
        return "conversation_end"
    return cleaned[:_CLOSE_REASON_MAX_CHARS]


@dataclass(frozen=True, slots=True)
class _MarkerRequest:
    intent: Any
    fence: GenerationFence
    duration_s: float


class MediaSessionAigcNoticeMixin:
    """Play the configured AIGC markers around one device conversation round."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        aigc_notice_config: AigcNoticeConfig
        aigc_round_logger: AigcRoundLogger | None
        _sessions: dict[str, _MediaVoiceSession]

        async def _request_device_standby(
            self, context: _MediaVoiceSession, *, reason: str
        ) -> bool: ...

        async def _enqueue_output_work(
            self, context: _MediaVoiceSession, work: _OutputWork
        ) -> bool: ...

        def _media_output_is_busy(self, context: _MediaVoiceSession) -> bool: ...

        @staticmethod
        def _release_output_owner(
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str,
        ) -> bool: ...

        async def _speak_device_wake_ack(self, context: _MediaVoiceSession) -> None: ...

    # Round bookkeeping ---------------------------------------------------

    def _start_aigc_round(
        self, context: _MediaVoiceSession, *, reason: str = "device_wake"
    ) -> AigcRoundRecord | None:
        """Open this round's markers and internal generation-log record once."""

        if context.identity.client_type != "device":
            return None
        if context.aigc_round is not None or context.closed or context.standby_requested:
            return context.aigc_round
        round_logger = self.aigc_round_logger
        if round_logger is None:
            return None
        record = round_logger.start()
        context.aigc_round = record
        logger.info(
            "AIGC round started session=%s content_id=%s reason=%s provider=%s engine=%s",
            context.identity.session_id,
            record.content_id,
            reason,
            record.descriptor.provider,
            record.descriptor.engine,
        )
        return record

    def _finish_aigc_round(self, context: _MediaVoiceSession, *, close_reason: str) -> None:
        record = context.aigc_round
        round_logger = self.aigc_round_logger
        if record is None or round_logger is None:
            return
        context.aigc_round = None
        context.aigc_opening_marker_pending = False
        if record.ended_at is not None:
            return
        try:
            round_logger.finish(record, close_reason=close_reason_for(close_reason))
        except Exception:
            logger.exception("AIGC round log failed session=%s", context.identity.session_id)

    # Opening -------------------------------------------------------------

    def _open_aigc_round(
        self, context: _MediaVoiceSession, *, reason: str = "device_wake"
    ) -> None:
        """Start the round record and mark which hooks the open path must run."""

        if context.identity.client_type != "device":
            return
        self._start_aigc_round(context, reason=reason)
        enabled = self.aigc_notice_config.position_enabled("opening")
        context.aigc_opening_marker_pending = enabled
        if not enabled:
            context.aigc_opening_marker_ready_at = time.monotonic()

    async def _speak_aigc_opening_marker(self, context: _MediaVoiceSession) -> None:
        if not context.aigc_opening_marker_pending or context.closed:
            return
        pcm = self.aigc_notice_config.marker_pcm("opening")
        if pcm is None:
            context.aigc_opening_marker_pending = False
            return
        # Admission is bounded (floor wait plus one queue write); playback itself
        # stays detached, so a stalled downlink cannot hold the wake path.
        await self._play_aigc_opening_marker(context, pcm)
        context.aigc_opening_marker_pending = False

    async def _play_aigc_opening_marker(
        self, context: _MediaVoiceSession, pcm: bytes
    ) -> None:
        try:
            request = await self._admit_aigc_marker(
                context,
                pcm,
                position="opening",
                floor_wait_s=_MARKER_FLOOR_WAIT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "AIGC opening marker failed session=%s", context.identity.session_id
            )
            return
        finally:
            context.aigc_opening_marker_pending = False
        if request is None:
            logger.info(
                "AIGC opening marker skipped session=%s reason=no_slot",
                context.identity.session_id,
            )
            return
        context.aigc_opening_marker_fence = request.fence
        started_at = context.aigc_opening_marker_started_at or time.monotonic()
        context.aigc_opening_marker_ready_at = opening_marker_ready_at(
            started_at=started_at,
            pcm=pcm,
            sample_rate_hz=self._aigc_pcm_sample_rate(context),
            lead_interval_ms=self.aigc_notice_config.lead_interval_ms,
        )
        self.metrics.inc_media_metric("voice_aigc_notice_opening_total")
        logger.info(
            "AIGC opening marker admitted session=%s content_id=%s fence=%s",
            context.identity.session_id,
            context.aigc_round.content_id if context.aigc_round is not None else "",
            request.fence,
        )

    async def _wait_aigc_opening_lead_in(self, context: _MediaVoiceSession) -> None:
        """Hold the wake acknowledgement until the configured lead-in elapsed."""

        ready_at = context.aigc_opening_marker_ready_at
        if ready_at is None:
            return
        remaining = ready_at - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

    # Closing -------------------------------------------------------------

    async def _play_aigc_closing_marker(self, context: _MediaVoiceSession) -> bool:
        """Play the closing marker and keep the session open until it finished.

        The caller's wait is bounded by the same configured length; the cue is
        detached so a stalled downlink cannot hold the round open.
        """

        if context.aigc_closing_marker_state != "idle":
            return False
        pcm = self.aigc_notice_config.marker_pcm("closing")
        if pcm is None:
            return False
        request = await self._admit_aigc_marker(
            context,
            pcm,
            position="closing",
            floor_wait_s=_MARKER_FLOOR_WAIT_S,
        )
        if request is None:
            context.aigc_closing_marker_state = "unavailable"
            logger.warning(
                "AIGC closing marker not played session=%s", context.identity.session_id
            )
            return False
        context.aigc_closing_marker_fence = request.fence
        context.aigc_closing_marker_started_at = time.monotonic()
        context.aigc_closing_marker_state = "playing"
        self.metrics.inc_media_metric("voice_aigc_notice_closing_total")
        logger.info(
            "AIGC closing marker admitted session=%s content_id=%s fence=%s",
            context.identity.session_id,
            context.aigc_round.content_id if context.aigc_round is not None else "",
            request.fence,
        )
        try:
            await asyncio.wait_for(
                self._finish_aigc_closing_marker(context, request),
                timeout=request.duration_s + _MARKER_DRAIN_GRACE_S,
            )
        except TimeoutError:
            logger.warning(
                "AIGC closing marker did not confirm session=%s",
                context.identity.session_id,
            )
        return True

    def _on_aigc_marker_playback(
        self,
        context: _MediaVoiceSession,
        progress: PlaybackProgress,
    ) -> None:
        """The device's terminal playback receipt ends the opening marker.

        ``STARTED`` and progress ACKs must not clear the fence: the switch to
        the normal output path at stream completion would then leave the marker
        owner and the speaking phase behind.
        """

        fence = context.aigc_opening_marker_fence
        if (
            fence is not None
            and progress.generation_id == fence.generation_id
            and progress.event_type is PlaybackEventType.ENDED
        ):
            context.aigc_opening_marker_fence = None

    async def _finish_aigc_opening_marker(
        self, context: _MediaVoiceSession, fence: GenerationFence
    ) -> None:
        """Close the opening cue without treating it as an assistant reply.

        The marker opened the first wire-legal turn and entered SPEAKING on its
        first PCM frame. Release that output and return the floor to LISTENING
        before the wake acknowledgement is admitted; otherwise both the owner
        and the speaking phase outlive the cue and block 「我在呢」.
        """

        opening = context.aigc_opening_marker_fence
        if opening is None or not fence.matches(opening):
            return
        context.aigc_opening_marker_fence = None
        self._release_output_owner(context, fence, reason="aigc_marker_complete")
        if context.closed or context.standby_requested:
            return
        if not context.runtime.fence.matches(fence):
            return
        await context.runtime.on_media_playback_done(fence, "")

    async def _finish_aigc_closing_marker(
        self, context: _MediaVoiceSession, request: _MarkerRequest
    ) -> None:
        await self._wait_aigc_marker_heard(context, request)
        context.aigc_closing_marker_state = "done"
        context.aigc_closing_marker_fence = None
        logger.info(
            "AIGC closing marker finished session=%s duration_s=%.3f",
            context.identity.session_id,
            time.monotonic() - (context.aigc_closing_marker_started_at or time.monotonic()),
        )

    # Shared --------------------------------------------------------------

    async def _aigc_marker_fence(
        self, context: _MediaVoiceSession, *, position: AigcNoticePosition
    ) -> GenerationFence | None:
        """The device requires a wire-legal fence: turn 0 cannot carry audio.

        Before the first user turn the turn is opened (like the wake
        acknowledgement); a closing marker starts a new output generation of
        the current turn, exactly as a queued follow-up source would.
        """

        runtime = context.runtime
        if runtime.fence.turn_id == 0 and position == "opening":
            opened = await runtime.begin_media_auxiliary_output(runtime.fence)
            if opened is not None:
                context.aigc_opening_marker_turn_id = opened.turn_id
        if runtime.fence.turn_id == 0:
            return None
        if position == "closing":
            next_fence = await runtime.begin_media_auxiliary_output(runtime.fence)
            if next_fence is None:
                return None
            context.output.playback.start(next_fence)
            context.output.output_sequence = 0
            context.output.output_text_offset = 0
            context.output.assistant_text = ""
            context.output.provider_complete = False
            context.output.output_complete_emitted = False
            return next_fence
        return runtime.fence

    def _restore_aigc_marker_phase(self, context: _MediaVoiceSession) -> None:
        previous = context.aigc_marker_previous_phase
        context.aigc_marker_previous_phase = None
        if previous is None or context.closed or context.standby_requested:
            return
        runtime = context.runtime
        if runtime.interaction_phase is not InteractionPhase.THINKING_SILENT:
            return
        if runtime.orchestrator.state is ConversationState.LISTENING:
            runtime.set_interaction_phase(previous, cause="aigc_notice_marker_done")

    def _aigc_marker_timeout_s(self, pcm: bytes, sample_rate: int) -> float:
        return min(
            len(pcm) / 2 / sample_rate,
            self.aigc_notice_config.marker_duration_ms / 1_000,
        )

    def _aigc_pcm_sample_rate(self, context: _MediaVoiceSession) -> int:
        """The provider's PCM rate; the parent gate rejects any other rate."""

        rate = int(getattr(context.provider, "output_sample_rate", 0) or 0)
        return rate if rate > 0 else AIGC_NOTICE_SAMPLE_RATE_HZ

    async def _admit_aigc_marker(
        self,
        context: _MediaVoiceSession,
        pcm: bytes,
        *,
        position: AigcNoticePosition,
        floor_wait_s: float,
    ) -> _MarkerRequest | None:
        if context.closed or context.standby_requested:
            return None
        # A reply still owns the output: a marker cannot preempt it (that would
        # flush the child's answer) and waiting would block the close behind it.
        if self._media_output_is_busy(context):
            return None
        deadline = time.monotonic() + max(0.0, floor_wait_s)
        while not context.runtime.output_floor_allows_assistant:
            if context.closed or context.standby_requested:
                return None
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(_MARKER_POLL_S)
        runtime = context.runtime
        if runtime.orchestrator.state is ConversationState.CONNECTING:
            await runtime.orchestrator.ready()
        coordinator = runtime.orchestrator.delegation
        session_id = context.identity.session_id
        fence = await self._aigc_marker_fence(context, position=position)
        if fence is None:
            return None
        # A cue is consumer audio, not a conversation reply: never publish it as
        # assistant text or as a plan-bound media output.
        session = self.bridge.bridge.get(session_id)
        previous_phase = runtime.interaction_phase
        if session is not None:
            session.generation_active = True
        runtime.set_interaction_phase(
            InteractionPhase.THINKING_SILENT, cause="aigc_notice_marker"
        )
        now_ms = int(time.time() * 1_000)
        context_version = coordinator.current_context_version(session_id)
        intent = DelegationCoordinator.aigc_marker(
            fence=fence,
            context_version=context_version,
            expires_at_ms=now_ms + _MARKER_TTL_MS,
            pcm_s16le=pcm,
            now_ms=now_ms,
        )
        # Admission runs against the real floor seen before the silence above;
        # a floor that closed meanwhile lets the selected source wait instead.
        floor_before_marker = runtime.output_floor_allows_assistant
        coordinator.admit_output_intent(
            intent,
            current_fence=fence,
            current_context_version=context_version,
            floor_allows_output=floor_before_marker,
            now_ms=now_ms,
        )
        if not coordinator.output_intent_is_active(
            intent,
            current_fence=fence,
            current_context_version=context_version,
            floor_allows_output=True,
            now_ms=now_ms,
        ):
            return None
        # The source is retained rather than waited on: the marker tells the
        # device the AI notice is on, and a stalled downlink must not hold the
        # round open (the closing path already does this).
        if not await self._enqueue_output_work(context, _OutputWork(intent, fence)):
            self._restore_aigc_marker_phase(context)
            return None
        context.aigc_marker_previous_phase = previous_phase
        sample_rate = self._aigc_pcm_sample_rate(context)
        request = _MarkerRequest(intent, fence, self._aigc_marker_timeout_s(pcm, sample_rate))
        if position == "opening":
            # The lead-in interval runs from admission and follows the whole
            # clip; the wake acknowledgement is held until then.
            context.aigc_opening_marker_started_at = time.monotonic()
        self._restore_aigc_marker_phase(context)
        return request

    async def _wait_aigc_marker_heard(
        self, context: _MediaVoiceSession, request: _MarkerRequest
    ) -> float:
        """Wait until the marker is heard; return its start time, or ``-1`` if it never played.

        A marker is a PCM fast-acknowledgement, so its dispatch result reports
        no emitted audio while it plays. The device playback ledger is the
        evidence that some of the tone reached the speaker.
        """

        intent_id = str(request.intent.intent_id)
        deadline = time.monotonic() + request.duration_s + _MARKER_DRAIN_GRACE_S
        while time.monotonic() < deadline:
            if context.closed or context.standby_requested:
                return -1.0
            # User speech, a reply or a newer source takes the output back: the
            # cue lost its slot and must not be followed by anything.
            if not context.runtime.fence.matches(request.fence):
                return -1.0
            rendered = context.output.playback.rendered_sample_end(request.fence)
            if rendered > 0:
                return time.monotonic() - rendered / self._aigc_pcm_sample_rate(context)
            owner = context.output.output_owner
            queued = context.output.output_work.get(intent_id)
            if queued is None and (owner is None or str(owner.intent.intent_id) != intent_id):
                # It either played out and its dispatch returned, or it was
                # dropped before a frame; only the ledger distinguishes them.
                return -1.0
            await asyncio.sleep(_MARKER_POLL_S)
        return -1.0


__all__ = ["MediaSessionAigcNoticeMixin", "close_reason_for"]
