"""Bound a device reply whose server-side SPEAKING state never ends."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession
from services.agent.src.voice_core.media_session_state import (
    MediaVoiceSessionState as _MediaVoiceSession,
)

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer

logger = logging.getLogger(__name__)
media_pb2: Any = _media_pb2

#: Seconds without one transport-accepted downlink PCM frame after which a
#: reply the runtime still believes is speaking is treated as stuck, cancelled
#: and stood by.
#:
#: Evidence from the 2026-10-20 field runs (findings.md,
#: claude-rca-stuck-speaking.md): the longest healthy reply measured ~12 s of
#: speech (``reply_s``), and story playback streams 24 kHz PCM in 20 ms frames,
#: so a healthy downlink gap stays far below 1 s.  15 s leaves 3 s above the
#: longest measured reply and about 750 frame intervals of slack.
#:
#: The neighbouring timeouts do not cover this state:
#: ``output_generation_timeout_s`` (45 s) bounds a provider iterator that
#: stalls before or between chunks and is rescheduled on every chunk, so it
#: never fires once the provider has finished; ``owner_silence_timeout_s``
#: (15 s) only runs in LISTENING.  A generation that reached the device and
#: then produced no frame and no terminal therefore had no bound at all: the
#: device stayed in SPEAKING for 4-13 minutes while FunASR kept consuming the
#: open microphone (``media DTLN stats`` advanced the whole time).
SPEAKING_STALL_TIMEOUT_S = 15.0

# Poll at a fraction of the timeout so an injected test value still fires
# promptly, but never busy-spin the event loop.
_MIN_POLL_INTERVAL_S = 0.01
_MAX_POLL_INTERVAL_S = 1.0


class MediaSpeakingWatchdogMixin:
    """Close a device session whose audible reply stopped progressing.

    ``assistant_speaking`` is the server's belief, not evidence that the device
    left SPEAKING.  The firmware only leaves SPEAKING on a generation terminal,
    and the server only starts its silence timer in LISTENING, so a cancelled
    or stalled reply that never sends a terminal leaves both sides waiting
    forever.  This watchdog turns that unbounded wait into a bounded close that
    also revokes the uplink.

    It is armed by a frame the transport accepted, never by provider
    production: only a frame on the wire puts the device into SPEAKING, so
    only such a frame can leave it there.
    """

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        speaking_stall_timeout_s: float
        _sessions: dict[str, _MediaVoiceSession]

        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> tuple[int, int]: ...

        async def _request_device_standby(
            self,
            context: _MediaVoiceSession,
            *,
            reason: str,
        ) -> bool: ...

    @property
    def _speaking_stall_poll_s(self) -> float:
        return min(
            _MAX_POLL_INTERVAL_S,
            max(_MIN_POLL_INTERVAL_S, self.speaking_stall_timeout_s / 4.0),
        )

    def _fence_terminal_sent(self, context: _MediaVoiceSession, fence: GenerationFence) -> bool:
        """True once this fence already got its authoritative end signal."""

        session = self.bridge.bridge.get(context.identity.session_id)
        if session is not None and session.terminal_covers(fence):
            return True
        if context.output.output_complete_emitted and context.runtime.fence.matches(fence):
            return True
        return context.output.playback.terminal_received(fence)

    def _device_playback_finished(self, context: _MediaVoiceSession, fence: GenerationFence) -> bool:
        """True only once the device itself reported this generation's end.

        A generation terminal on the wire is the server's instruction, not the
        device's exit from SPEAKING: the firmware flushes its queue and acks
        with ``playback.ended``.  Field 2026-10-21 (session 74cdaf88):
        ``generation.completed`` went out at 11:32:58 and the device never
        acked; it stayed in SPEAKING for 4+ minutes because this check treated
        the sent terminal as the end and the watchdog stood down.
        """

        return context.output.playback.terminal_received(fence)

    def _speaking_output_active(self, context: _MediaVoiceSession) -> bool:
        """Return whether the server still owes the device an audible ending."""

        if context.closed or context.standby_requested:
            return False
        if context.runtime.assistant_speaking:
            return True
        # The flag clears a moment before the terminal is queued.  An owner
        # whose audio already reached the device still owes that generation.
        owner = context.output.output_owner
        if owner is None or not context.output.audio_sent_for(owner.fence):
            return False
        return not self._fence_terminal_sent(context, owner.fence)

    def _stalled_generation_fence(
        self,
        session: MediaBridgeSession | None,
        armed: GenerationFence,
    ) -> GenerationFence:
        """Return the generation the device is actually speaking.

        A terminal for any other fence is discarded by the firmware as stale,
        so the fence the transport last announced wins over a fence that a
        newer runtime generation already replaced without audio.
        """

        if session is not None and session.generation_active:
            return session.fence
        return armed

    def _note_downlink_audio(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        """Record one frame the transport accepted for ``fence``."""

        if context.identity.client_type != "device":
            return
        context.output.last_downlink_audio_at = time.monotonic()
        context.output.speaking_stall_fence = fence
        self._arm_speaking_stall_watchdog(context)

    def _arm_speaking_stall_watchdog(self, context: _MediaVoiceSession) -> None:
        """Start this session's watchdog once a generation is on the wire."""

        if (
            context.identity.client_type != "device"
            or context.closed
            or context.standby_requested
            or self.speaking_stall_timeout_s <= 0
            or context.output.speaking_stall_fence is None
        ):
            return
        task = context.output.speaking_stall_task
        if task is not None and not task.done():
            return
        context.output.speaking_stall_task = asyncio.create_task(
            self._speaking_stall_watch(context),
            name=f"media-speaking-stall-{context.identity.session_id}",
        )

    def _cancel_speaking_stall_watchdog(self, context: _MediaVoiceSession) -> None:
        task = context.output.speaking_stall_task
        context.output.speaking_stall_task = None
        context.output.speaking_stall_fence = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _speaking_stall_watch(self, context: _MediaVoiceSession) -> None:
        task = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(self._speaking_stall_poll_s)
                if (
                    context.closed
                    or context.standby_requested
                    or self._sessions.get(context.identity.session_id) is not context
                    or context.output.speaking_stall_task is not task
                ):
                    return
                fence = context.output.speaking_stall_fence
                if fence is None:
                    return
                if not self._speaking_output_active(context):
                    return
                # A terminal the server sent is not a terminal the device
                # applied; only its playback receipt ends this wait.
                if self._device_playback_finished(context, fence):
                    return
                if not self.bridge.has_live_connection(context.identity.session_id):
                    # A reconnect suspends pacing inside its bounded grace;
                    # that is transport recovery, not a stuck reply. Restart
                    # the idle measurement once the downlink is back.
                    context.output.last_downlink_audio_at = time.monotonic()
                    continue
                last_audio_at = context.output.last_downlink_audio_at
                if last_audio_at is None:
                    context.output.last_downlink_audio_at = time.monotonic()
                    continue
                idle_s = time.monotonic() - last_audio_at
                if idle_s < self.speaking_stall_timeout_s:
                    continue
                session = self.bridge.bridge.get(context.identity.session_id)
                await self._close_stalled_speaking(
                    context,
                    self._stalled_generation_fence(session, fence),
                    idle_s=idle_s,
                )
                return
        finally:
            if context.output.speaking_stall_task is task:
                context.output.speaking_stall_task = None

    async def _close_stalled_speaking(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        idle_s: float,
    ) -> None:
        session_id = context.identity.session_id
        # Revoke both uplink gates before any await: from here no audio frame
        # reaches payload parsing, DTLN or the ASR provider, however long the
        # close path takes.  The transport gate rejects a late frame first; the
        # ingress flag also stops frames already queued in the pump.
        context.ingress.input_revoked = True
        self.bridge.bridge.revoke_uplink(session_id, stream_epoch=context.stream_epoch)
        logger.warning(
            "media speaking watchdog expired session=%s reason=stalled_speaking "
            "idle_s=%.3f timeout_s=%.3f",
            session_id,
            idle_s,
            self.speaking_stall_timeout_s,
        )
        self.metrics.inc_media_metric("voice_speaking_stall_total")
        session = self.bridge.bridge.get(session_id)
        if session is None or not session.terminal_covers(fence):
            # Terminate the generation the device is actually speaking before
            # the close: CONVERSATION_STATE_CLOSED installs the transport
            # tombstone and would reject this control afterwards.
            task_epoch, context_version = self._event_versions(context, fence)
            await self.bridge.emit_generation(
                fence.session_id,
                fence,
                action=media_pb2.GENERATION_ACTION_CANCEL,
                reason="speaking_stalled",
                task_epoch=task_epoch,
                context_version=context_version,
            )
        # CLOSED is the standby that makes the device close its microphone.
        await self._request_device_standby(context, reason="speaking_stalled")


__all__ = ["MediaSpeakingWatchdogMixin", "SPEAKING_STALL_TIMEOUT_S"]
