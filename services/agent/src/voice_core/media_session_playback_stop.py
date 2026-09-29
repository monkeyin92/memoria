"""Spoken playback stop (「停」「等等」「好了」) while a Media Voice reply plays.

Field 2026-09-29 (story on a real ESP32, ``audio_mode=interrupt_assist``): the
signed ``allowed_barge_in`` excludes voice, so firmware and Edge suppress every
playback-window vad.start and the pending turn never gets a VAD endpoint.  The
cloud-ASR 「停」 finals therefore buffered forever, and a committed stop command
only flipped the runtime floor to listening while the reply kept streaming.
This mixin pins the endpoint of a lexical stop heard during playback and then
carries out the Router's INTERRUPT_COMMAND the way a keyword stop does.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.conversation_close_router import lexical_playback_control_only
from services.agent.src.voice_core.media_protocol import should_pause_asr_for_playback
from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)

# UtteranceRouter rule 4 (pure stop / completion acknowledgement) reports this
# reason when ``accept_user_turn`` suppresses the control turn.
PLAYBACK_STOP_ROUTE_REASON = "interrupt_command_only"
_PLAYBACK_STOP_CAUSE = "media_voice_stop"
# The assistant text announced most recently is what the uplink can still be
# echoing: the phrase being played plus the one queued behind it (phrases are
# at most ~28 characters) and the ASR finalization lag.  A stop phrase found
# there is treated as the reply's own voice, never as the owner's command.
_PLAYBACK_STOP_ECHO_TAIL_CHARS = 64


def _compact(text: str) -> str:
    return "".join(character for character in text if character.isalnum())


class MediaPlaybackStopMixin:
    """Endpoint and execute a spoken stop command during assistant playback."""

    if TYPE_CHECKING:
        metrics: MetricsRegistry

        @staticmethod
        def _reply_in_flight(context: _MediaVoiceSession) -> bool: ...

        def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...

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

        @staticmethod
        def _device_playback_flush_required(
            context: _MediaVoiceSession, fence: GenerationFence
        ) -> bool: ...

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

    @staticmethod
    def _playback_stop_is_echo(context: _MediaVoiceSession, text: str) -> bool:
        """True when a stop phrase repeats what the reply itself just said.

        A single character such as 「停」 is below the generic assistant-echo
        guard, so a story line like 「小兔子大喊：停！」 would otherwise stop
        its own reply through an open microphone.
        """

        heard = _compact(text)
        if not heard or not lexical_playback_control_only(text):
            return False
        spoken = _compact(context.output.assistant_text)[-_PLAYBACK_STOP_ECHO_TAIL_CHARS:]
        return heard in spoken

    def _maybe_pin_playback_stop(
        self,
        context: _MediaVoiceSession,
        result: ASRResult,
        *,
        source: str,
    ) -> None:
        """Endpoint a device stop phrase heard while a reply holds the floor.

        Only the stop phrase's own interval is committed: earlier playback-
        window candidates (echo, or held speech without owner authority) would
        otherwise merge into 「……停」, route as chat and stay held.  The
        fence mirrors the playback-boundary split in ``MediaTurnEndpointMixin``.
        """

        if (
            context.identity.client_type != "device"
            or context.closed
            or context.standby_requested
            or context.pending.turn_endpoint_sample is not None
        ):
            return
        text = result.text.strip()
        if not text or not lexical_playback_control_only(text):
            return
        if not self._reply_in_flight(context):
            return
        floor = context.pending.pending_turn_onset_floor
        if floor is not None and result.capture_start_sample < floor:
            return
        if self._playback_stop_is_echo(context, text):
            logger.info(
                "media playback stop ignored as reply echo session=%s text_len=%s source=%s",
                context.identity.session_id,
                len(text),
                source,
            )
            return
        start = result.capture_start_sample
        if start > 0:
            timeline = context.runtime.speech_timeline
            timeline.evict_segment_ids({
                segment.segment_id
                for segment in timeline.segments_in_range(
                    stream_epoch=result.stream_epoch,
                    start_sample=0,
                    end_sample=start,
                )
            })
        pending = context.pending
        pending.clock_fact_partial_text = None
        pending.clock_fact_partial_stable_since = None
        pending.live_query_partial_text = None
        pending.live_query_partial_stable_since = None
        pending.conversation_close_partial_text = None
        pending.conversation_close_partial_stable_since = None
        pending.cancel_close_semantic()
        pending.pending_turn_playback_overlap = False
        pending.pending_turn_onset_floor = start
        pending.pending_partial = None if result.is_final else result
        endpoint = result.capture_end_sample
        pending.turn_start_sample = start
        pending.turn_end_sample = endpoint
        pending.turn_endpoint_sample = endpoint
        pending.turn_retire_sample = endpoint
        pending.turn_endpoint_grace_deadline = time.monotonic()
        logger.info(
            "media early playback-stop endpoint session=%s start=%s endpoint=%s "
            "text_len=%s source=%s",
            context.identity.session_id,
            start,
            endpoint,
            len(text),
            source,
        )
        self._schedule_turn_commit(context)

    async def _stop_reply_for_voice_command(self, context: _MediaVoiceSession) -> bool:
        """Carry out a Router-accepted spoken stop like a keyword stop.

        ``accept_user_turn`` only suppresses the control turn; the reply task,
        the generation and the device playback must be stopped here.  The
        session stays open: this is never the END_SESSION path.
        """

        if (
            context.closed
            or context.standby_requested
            or not context.runtime.floor.interruptible
        ):
            return False
        previous_fence = context.output.playback.current_fence or context.runtime.fence
        if not previous_fence.matches(context.runtime.fence):
            # Another control already replaced this generation.
            return False
        stop_started_ns = time.monotonic_ns()
        flush_required = self._device_playback_flush_required(context, previous_fence)
        await self._record_interrupted_timed_spans(context, previous_fence)
        heard = context.output.playback.actual_heard_text(previous_fence)
        cancelled = await context.runtime.on_real_interrupt(
            cause=_PLAYBACK_STOP_CAUSE,
            create_user_turn=False,
            synchronized_transcript=heard,
            force_generation_bump=True,
        )
        if cancelled.matches(previous_fence):
            return False
        await context.runtime.on_media_playback_interrupted(
            interrupted_from=previous_fence,
            synchronized_transcript=heard,
        )
        context.output.playback.start(cancelled)
        context.output.provider_complete = False
        context.output.output_complete_emitted = False
        if should_pause_asr_for_playback(context.identity):
            await context.provider.pause_asr_for_playback(context.identity)
        await self._cancel_reply_task(context, previous_fence, reason=_PLAYBACK_STOP_CAUSE)
        await self._emit_cancel_generation(
            context,
            cancelled,
            heard_fence=previous_fence,
            source_event_id="voice_stop_command",
            payload={"reason": "voice_stop_command"},
            playback_flush_required=flush_required,
        )
        self.metrics.observe_voice_latency(
            "interrupt_core_stop",
            (time.monotonic_ns() - stop_started_ns) / 1_000_000_000,
        )
        logger.info(
            "media spoken stop interrupted reply session=%s turn=%s generation=%s "
            "replacement_generation=%s flush=%s",
            context.identity.session_id,
            previous_fence.turn_id,
            previous_fence.generation_id,
            cancelled.generation_id,
            flush_required,
        )
        return True


__all__ = ["PLAYBACK_STOP_ROUTE_REASON", "MediaPlaybackStopMixin"]
