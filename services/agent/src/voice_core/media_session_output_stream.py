"""PCM streaming and playback acknowledgement for Media Voice."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.interruption_guard import (
    is_short_assistant_farewell_reply,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession, PCMFrame
from services.agent.src.voice_core.media_protocol import PlaybackEventType, PlaybackProgress
from services.agent.src.voice_core.media_session_types import (
    MediaReplyChunk,
    MediaTextSpan,
    OutputDispatchResult,
    OutputDispatchStatus,
    same_turn_followup_output_pending,
)
from services.agent.src.voice_core.media_session_types import (
    OutputOwnerLease as _OutputOwnerLease,
)
from services.agent.src.voice_core.media_session_types import (
    OutputWork as _OutputWork,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan
from services.agent.src.voice_core.reply_delivery import ReplyDeliveryEvent

if TYPE_CHECKING:
    from services.agent.src.orchestration.conversation_projection import TurnPhase
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

media_pb2: Any = _media_pb2
_DOWNLINK_PCM_SAMPLE_RATE = 24_000


def _playback_terminal(event_type: PlaybackEventType) -> bool | None:
    if event_type is PlaybackEventType.WATERMARK:
        return None
    return event_type in {PlaybackEventType.ENDED, PlaybackEventType.ERROR}


def _next_pcm_send_slot(
    *,
    now: float,
    next_send_at: float,
    frame_samples: int,
) -> tuple[float, float]:
    """Return bounded real-time pacing for one provider PCM frame."""

    if frame_samples <= 0:
        raise ValueError("PCM frame must contain at least one sample")
    send_at = max(now, next_send_at)
    return (
        max(0.0, next_send_at - now),
        send_at + frame_samples / _DOWNLINK_PCM_SAMPLE_RATE,
    )


class MediaOutputStreamMixin:
    """Stream selected PCM and publish only the acknowledged text prefix."""

    if TYPE_CHECKING:
        bridge: MediaBridgeGrpcServer
        metrics: MetricsRegistry
        reconnect_grace_s: float
        _sessions: dict[str, _MediaVoiceSession]

        def _event_versions(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> tuple[int, int]: ...

        def _output_owner_is_current(
            self,
            context: _MediaVoiceSession,
            lease: _OutputOwnerLease,
        ) -> bool: ...

        @staticmethod
        def _release_output_owner(
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str,
        ) -> bool: ...

        async def _cancel_reply_task(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str = "cancelled",
            cancel_timeout_s: float = 5.0,
        ) -> None: ...

        async def _start_selected_output(self, context: _MediaVoiceSession) -> bool: ...

        @staticmethod
        async def _advance_failed_output_generation(
            context: _MediaVoiceSession,
            fence: GenerationFence,
            *,
            reason: str,
        ) -> GenerationFence | None: ...

        @staticmethod
        def _device_playback_flush_required(
            context: _MediaVoiceSession,
            fence: GenerationFence,
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

        def _record_reply_delivery_event(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
            event: ReplyDeliveryEvent,
            reason: str = "",
        ) -> None: ...

        def _observe_turn_phase(
            self,
            context: _MediaVoiceSession,
            previous_phase: TurnPhase,
        ) -> None: ...

        def flush_pending_missed_hearing_nudge(
            self, context: _MediaVoiceSession
        ) -> None: ...

        def clear_device_wake_ack_fence(
            self,
            context: _MediaVoiceSession,
            fence: GenerationFence,
        ) -> None: ...

    async def _abort_unheard_stream(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        reason: str,
        emitted_audio: bool,
    ) -> None:
        owner = context.output_owner
        owner_intent_active = False
        if owner is not None and owner.fence.matches(fence):
            coordinator = context.runtime.orchestrator.delegation
            owner_intent_active = coordinator.output_intent_is_active(
                owner.intent,
                current_fence=context.runtime.fence,
                current_context_version=coordinator.current_context_version(
                    fence.session_id
                ),
                floor_allows_output=context.runtime.output_floor_allows_assistant,
            )
        await self._cancel_reply_task(context, fence, reason=reason)
        if emitted_audio and context.runtime.fence.matches(fence):
            # A higher-priority owner has its own preemption path.  If the
            # current owner instead disappeared because its intent expired (or
            # because the transport failed), close the audible generation here
            # so the device cannot remain in SPEAKING without a terminal fence.
            if reason != "superseded" or not owner_intent_active:
                flush_required = self._device_playback_flush_required(context, fence)
                cancelled = await self._advance_failed_output_generation(
                    context,
                    fence,
                    reason=reason,
                )
                context.playback.discard(fence)
                context.assistant_text = ""
                context.output_sequence = 0
                context.output_text_offset = 0
                context.provider_complete = False
                context.output_complete_emitted = False
                for intent_id, pending in tuple(context.output_work.items()):
                    if pending.fence.matches(fence):
                        context.output_work.pop(intent_id, None)
                context.runtime.orchestrator.delegation.reset_output_intent_state(
                    fence.session_id
                )
                if cancelled is None:
                    await context.runtime.on_assistant_reply_aborted(
                        fence,
                        cause=reason,
                    )
                else:
                    await self._emit_cancel_generation(
                        context,
                        cancelled,
                        heard_fence=fence,
                        source_event_id=f"output_{reason}",
                        payload={"reason": reason},
                        playback_flush_required=flush_required,
                    )
                return
        if emitted_audio or reason == "playback_rejected":
            return
        if reason != "stale_generation" and context.runtime.barge_in_enabled:
            return
        await context.runtime.restore_listen_after_unheard_output(fence, cause=reason)

    async def on_playback_progress(
        self,
        session: MediaBridgeSession,
        progress: PlaybackProgress,
    ) -> None:
        accepts_input = getattr(session, "accepts_input", None)
        if callable(accepts_input) and not accepts_input():
            return
        context = self._sessions.get(session.identity.session_id)
        if context is None or context.closed or getattr(context, "standby_requested", False):
            return
        fence = GenerationFence(
            session_id=context.identity.session_id,
            turn_id=progress.turn_id,
            generation_id=progress.generation_id,
            tool_epoch=progress.tool_epoch,
            session_epoch=progress.session_epoch,
        )
        previously_rendered = context.playback.rendered_sample_end(fence)
        stale_ack_count = context.playback.stale_ack_count
        acknowledged = context.playback.acknowledge(
            fence,
            progress.rendered_sample_end,
            received_sequence=progress.received_sequence,
            approximate=progress.approximate,
            heard_eligible=not (session.identity.client_type == "device" and progress.approximate),
            terminal=_playback_terminal(progress.event_type),
        )
        if context.playback.stale_ack_count != stale_ack_count:
            return
        rendered = context.playback.rendered_sample_end(fence)
        if rendered > previously_rendered:
            self.metrics.add_conversation_participation_ms(
                "assistant",
                (rendered - previously_rendered) * 1_000 / _DOWNLINK_PCM_SAMPLE_RATE,
            )
        self._observe_projection_playback_evidence(context, fence, progress.event_type)
        # Publish the cumulative acknowledged prefix under one turn/revision;
        # publishing only the newly acknowledged span would make clients
        # replace a complete answer with its last phrase.
        heard = context.playback.actual_heard_text(fence)
        delivery = context.reply_delivery.get(fence)
        if (
            context.playback.received_sequence(fence) >= 0
            and (delivery is None or not delivery.first_frame_sent)
        ):
            # Keep direct playback-ledger fixtures and legacy callers honest:
            # an accepted playback range is the same transport boundary as a
            # first frame sent event when the stream path was not involved.
            self._record_reply_delivery_event(
                context,
                fence,
                ReplyDeliveryEvent.FIRST_FRAME_SENT,
                "playback_ledger_backfill",
            )
        if context.playback.is_fully_acknowledged(fence):
            self._record_reply_delivery_event(
                context,
                fence,
                ReplyDeliveryEvent.ACTUAL_HEARD,
                "exact_playback_ack",
            )
        if progress.event_type is PlaybackEventType.ERROR:
            if acknowledged and heard and context.runtime.fence.matches(fence):
                context.runtime.publish_transcript(
                    speaker="assistant",
                    text=heard,
                    final=True,
                    heard=True,
                    text_delivered=True,
                    fence=fence,
                )
            await self._fail_playback_output(context, fence, reason="device_playback_error")
            return
        if (
            progress.event_type is PlaybackEventType.ENDED
            and context.provider_complete
            and not context.playback.is_transport_watermarked(fence)
        ):
            await self._fail_playback_output(
                context,
                fence,
                reason="playback_terminal_incomplete",
            )
            return
        if (
            context.provider_complete
            and context.playback.is_playback_complete(fence)
            and context.runtime.fence.matches(fence)
        ):
            await self._finish_completed_output(context, fence)
        # An empty acknowledged tuple only means no new publishable text span;
        # it must not skip the playback-completion check above. Transcript
        # publication itself still requires a newly acknowledged span so a
        # duplicate ACK cannot re-emit the same text.
        if acknowledged and heard and context.runtime.fence.matches(fence):
            # Commit actual-heard history before publishing the final event;
            # consumers must never observe a "heard" transcript while the
            # authoritative runtime is still SPEAKING.
            context.runtime.publish_transcript(
                speaker="assistant",
                text=heard,
                final=True,
                heard=True,
                text_delivered=True,
                fence=fence,
            )

    def _observe_projection_playback_evidence(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        event_type: PlaybackEventType,
    ) -> None:
        """Mirror accepted playback ACKs into the shadow TurnPhase."""

        if event_type not in (
            PlaybackEventType.STARTED,
            PlaybackEventType.WATERMARK,
            PlaybackEventType.PROGRESS,
            PlaybackEventType.ENDED,
            PlaybackEventType.ERROR,
        ):
            return
        previous_phase = context.projection.phase
        active = event_type in {
            PlaybackEventType.STARTED,
            PlaybackEventType.WATERMARK,
            PlaybackEventType.PROGRESS,
        }
        # apply_playback_evidence owns stale-fence rejection and the uplink
        # capture watermark.  This seam only mirrors already-admitted ACKs.
        context.projection.apply_playback_evidence(
            playback_active=active,
            fence=fence,
        )
        self._observe_turn_phase(context, previous_phase)

    async def _stream_output(
        self,
        context: _MediaVoiceSession,
        session_id: str,
        fence: GenerationFence,
        lease: _OutputOwnerLease,
        chunks: AsyncIterator[MediaReplyChunk],
        *,
        measure_tts_first_frame: bool,
    ) -> OutputDispatchResult:
        """Send one selected source through the shared owner and PCM ledger."""

        emitted_audio = False
        loop = asyncio.get_running_loop()
        next_pcm_send_at = loop.time()
        # This clock starts when the selected provider/output iterator is first
        # consumed.  It deliberately ends at the Edge-accepted PCM boundary;
        # device DAC/Actual Heard remain separate playback evidence.
        context.tts_started_ns = time.monotonic_ns() if measure_tts_first_frame else None
        try:
            async for chunk in chunks:
                if not self._output_owner_is_current(context, lease):
                    self.metrics.inc_media_stale_generation()
                    await self._abort_unheard_stream(
                        context,
                        fence,
                        reason="superseded",
                        emitted_audio=emitted_audio,
                    )
                    return OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "superseded",
                        emitted_audio,
                    )
                # Provider TTS can produce much faster than wall clock. Pace at
                # the generation owner before the gRPC/Edge jitter buffers so
                # Direct Edge's intentional 80-200 ms queue never turns a
                # normal reply burst into discontinuity drops. Cancelling this
                # reply task interrupts the sleep immediately.
                delay_s, next_pcm_send_at = _next_pcm_send_slot(
                    now=loop.time(),
                    next_send_at=next_pcm_send_at,
                    frame_samples=len(chunk.pcm_s16le) // 2,
                )
                if delay_s > 0:
                    await asyncio.sleep(delay_s)
                    if not self._output_owner_is_current(context, lease):
                        self.metrics.inc_media_stale_generation()
                        await self._abort_unheard_stream(
                            context,
                            fence,
                            reason="superseded",
                            emitted_audio=emitted_audio,
                        )
                        return OutputDispatchResult(
                            fence,
                            OutputDispatchStatus.ABORTED,
                            "superseded",
                            emitted_audio,
                        )
                announcement = (
                    chunk.text if chunk.assistant_text_delta is None else chunk.assistant_text_delta
                )
                if announcement:
                    context.assistant_text += announcement
                # PCM-only intents still own the audible lifecycle even when
                # they have no transcript metadata.  This is especially
                # important after a completed segment is promoted to a fresh
                # generation: without entering SPEAKING on its first frame the
                # terminal ACK would leave the runtime stranded in THINKING.
                if announcement or not emitted_audio:
                    # Keep the runtime's heard-text tracker aligned with the
                    # complete provider text, while the ledger still decides
                    # whether that text was actually rendered.
                    speaking_started = await context.runtime.on_assistant_speaking(
                        context.assistant_text,
                        expected_fence=fence,
                        precondition=lambda: self._output_owner_is_current(context, lease),
                    )
                    if not speaking_started or not self._output_owner_is_current(context, lease):
                        self.metrics.inc_media_stale_generation()
                        await self._abort_unheard_stream(
                            context,
                            fence,
                            reason="superseded",
                            emitted_audio=emitted_audio,
                        )
                        return OutputDispatchResult(
                            fence,
                            OutputDispatchStatus.ABORTED,
                            "superseded",
                            emitted_audio,
                        )
                    # ``assistant_text_delta`` is incremental at the provider
                    # boundary, but transcript consumers replace one fenced
                    # turn by revision. Publish the cumulative text so a
                    # second phrase cannot make the UI/history seam regress
                    # to only that phrase. This remains non-final until the
                    # playback ledger supplies an actual-heard watermark.
                    if announcement:
                        context.runtime.publish_transcript(
                            speaker="assistant",
                            text=context.assistant_text,
                            final=False,
                            text_delivered=True,
                            fence=fence,
                        )
                gated = context.runtime.gate_tts_audio(fence, chunk.pcm_s16le)
                if gated is None:
                    self.metrics.inc_media_stale_generation()
                    await self._abort_unheard_stream(
                        context,
                        fence,
                        reason="stale_generation",
                        emitted_audio=emitted_audio,
                    )
                    return OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "stale_generation",
                        emitted_audio,
                    )
                task_epoch, context_version = self._event_versions(context, fence)
                frame = PCMFrame(
                    identity=context.identity,
                    turn_id=fence.turn_id,
                    generation_id=fence.generation_id,
                    tool_epoch=fence.tool_epoch,
                    session_epoch=fence.session_epoch,
                    sequence=context.output_sequence,
                    source_start_sample=chunk.source_start_sample,
                    frame_samples=len(gated) // 2,
                    pcm_s16le=gated,
                    first=chunk.first,
                    final=chunk.final,
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
                if not await self.bridge.emit_pcm_when_connected(
                    session_id,
                    frame,
                    timeout_s=self.reconnect_grace_s,
                ):
                    self.metrics.inc_media_stale_generation()
                    await self._abort_unheard_stream(
                        context,
                        fence,
                        reason="transport_rejected",
                        emitted_audio=emitted_audio,
                    )
                    return OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "transport_rejected",
                        emitted_audio,
                    )
                if not context.playback.register_audio(
                    fence,
                    frame.sequence,
                    frame.source_start_sample,
                    frame.frame_samples,
                ):
                    self.metrics.inc_media_stale_generation()
                    await self._cancel_reply_task(context, fence, reason="playback_rejected")
                    return OutputDispatchResult(
                        fence,
                        OutputDispatchStatus.ABORTED,
                        "playback_rejected",
                        emitted_audio,
                    )
                delivery_before = context.reply_delivery.get(fence)
                self._record_reply_delivery_event(
                    context,
                    fence,
                    ReplyDeliveryEvent.FIRST_FRAME_SENT,
                    "downlink_frame_accepted",
                )
                if (
                    (delivery_before is None or not delivery_before.first_frame_sent)
                    and context.tts_started_ns is not None
                ):
                    self.metrics.observe_voice_latency(
                        "tts_first_frame",
                        (time.monotonic_ns() - context.tts_started_ns) / 1_000_000_000,
                    )
                    context.tts_started_ns = None
                emitted_audio = True
                if not context.first_audio_observed and context.turn_started_ns is not None:
                    self.metrics.observe_voice_latency(
                        "first_audio",
                        (time.monotonic_ns() - context.turn_started_ns) / 1_000_000_000,
                    )
                    context.first_audio_observed = True
                context.output_sequence += 1
                if chunk.text:
                    text_start = context.output_text_offset
                    context.output_text_offset += len(chunk.text)
                    context.playback.add_span(
                        PlaybackSpan(
                            fence=fence,
                            text_start=text_start,
                            text_end=context.output_text_offset,
                            audio_start_sample=(
                                chunk.text_audio_start_sample
                                if chunk.text_audio_start_sample is not None
                                else chunk.source_start_sample
                            ),
                            audio_end_sample=(
                                chunk.text_audio_end_sample
                                if chunk.text_audio_end_sample is not None
                                else chunk.source_start_sample + len(gated) // 2
                            ),
                            text=chunk.text,
                        )
                    )
                for span in chunk.text_spans:
                    text_start = context.output_text_offset
                    context.output_text_offset += len(span.text)
                    context.playback.add_span(
                        PlaybackSpan(
                            fence=fence,
                            text_start=text_start,
                            text_end=context.output_text_offset,
                            audio_start_sample=span.audio_start_sample,
                            audio_end_sample=span.audio_end_sample,
                            text=span.text,
                        )
                    )
        except asyncio.CancelledError:
            self._release_output_owner(context, fence, reason="cancelled")
            raise
        except Exception:
            self.metrics.inc_media_session_failed()
            await self._cancel_reply_task(context, fence, reason="provider_failed")
            raise
        if emitted_audio and context.runtime.fence.matches(fence):
            # Provider completion is not playback completion. Keep the runtime
            # speaking until the client watermark covers all emitted audio.
            context.provider_complete = True
            self._record_reply_delivery_event(
                context,
                fence,
                ReplyDeliveryEvent.PROVIDER_COMPLETED,
                "provider_stream_complete",
            )
            if not context.output_complete_emitted:
                task_epoch, context_version = self._event_versions(context, fence)
                context.output_complete_emitted = await self.bridge.emit_generation(
                    fence.session_id,
                    fence,
                    action=media_pb2.GENERATION_ACTION_COMPLETE,
                    reason="provider_reply_complete",
                    task_epoch=task_epoch,
                    context_version=context_version,
                )
            # A very fast client may acknowledge the last frame before the
            # provider iterator yields completion.
            if context.playback.is_playback_complete(fence):
                await self._finish_completed_output(context, fence)
            elif context.playback.terminal_received(fence):
                await self._fail_playback_output(
                    context,
                    fence,
                    reason="playback_terminal_incomplete",
                )
        else:
            self._release_output_owner(context, fence, reason="provider_completed_without_audio")
            if not await self._start_selected_output(context):
                await context.runtime.on_assistant_reply_aborted(
                    fence,
                    cause="provider_completed_without_audio",
                )
        return OutputDispatchResult(
            fence,
            OutputDispatchStatus.COMPLETED,
            "provider_stream_complete" if emitted_audio else "provider_completed_without_audio",
            emitted_audio,
        )

    async def _output_chunks(
        self,
        context: _MediaVoiceSession,
        work: _OutputWork,
        source_start_sample: int,
    ) -> AsyncIterator[MediaReplyChunk]:
        if work.conversation_text is not None:
            async for chunk in context.provider.generate_reply(
                context.identity,
                work.conversation_text,
                work.fence,
            ):
                if source_start_sample:
                    yield replace(
                        chunk,
                        source_start_sample=chunk.source_start_sample + source_start_sample,
                        text_audio_start_sample=(
                            None
                            if chunk.text_audio_start_sample is None
                            else chunk.text_audio_start_sample + source_start_sample
                        ),
                        text_audio_end_sample=(
                            None
                            if chunk.text_audio_end_sample is None
                            else chunk.text_audio_end_sample + source_start_sample
                        ),
                        text_spans=tuple(
                            MediaTextSpan(
                                span.text,
                                span.audio_start_sample + source_start_sample,
                                span.audio_end_sample + source_start_sample,
                            )
                            for span in chunk.text_spans
                        ),
                    )
                else:
                    yield chunk
            return
        renderer = getattr(context.provider, "generate_output", None)
        if callable(renderer):
            produced = renderer(
                context.identity,
                work.intent,
                work.fence,
                work_id=work.intent_id,
                source_start_sample=source_start_sample,
            )
            if inspect.isawaitable(produced):
                produced = await produced
            async for chunk in produced:
                yield chunk
            return
        if getattr(work.intent, "WhichOneof", lambda _name: None)("source") != "pcm_s16le":
            raise RuntimeError("media provider cannot render an output text source")
        pcm = bytes(getattr(work.intent, "pcm_s16le", b""))
        if not pcm or len(pcm) % 2:
            raise ValueError("output PCM source must be non-empty 16-bit audio")
        frame_samples = int(getattr(context.provider, "output_frame_samples", 480))
        if frame_samples <= 0:
            raise RuntimeError("media provider has an invalid output frame size")
        frame_bytes = frame_samples * 2
        sample = source_start_sample
        for offset in range(0, len(pcm), frame_bytes):
            frame = pcm[offset : offset + frame_bytes]
            final = offset + frame_bytes >= len(pcm)
            if len(frame) < frame_bytes:
                frame += b"\x00" * (frame_bytes - len(frame))
            yield MediaReplyChunk(
                pcm_s16le=frame,
                source_start_sample=sample,
                first=offset == 0,
                final=final,
            )
            sample += frame_samples

    async def _maybe_standby_after_farewell_complete(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        if (
            context.closed
            or context.standby_requested
            or context.identity.client_type != "device"
            or not context.runtime.fence.matches(fence)
        ):
            return
        assistant_text = context.assistant_text.strip() or context.playback.actual_heard_text(
            fence
        ).strip()
        if not is_short_assistant_farewell_reply(assistant_text):
            return
        last_user = next(
            (
                turn.content.strip()
                for turn in reversed(context.runtime.orchestrator.context.turns)
                if turn.role == "user" and turn.content.strip()
            ),
            "",
        )
        if not context.runtime.conversation_close_needed(last_user):
            return
        request_standby = getattr(self, "_request_device_standby", None)
        if callable(request_standby):
            await request_standby(context, reason="conversation_farewell_complete")

    async def _finish_completed_output(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
    ) -> None:
        if (
            not context.provider_complete
            or not context.playback.is_playback_complete(fence)
            or not context.runtime.fence.matches(fence)
        ):
            return
        owner = context.output_owner
        if owner is not None and not owner.fence.matches(fence):
            return
        context.provider_complete = False
        self._record_reply_delivery_event(
            context,
            fence,
            ReplyDeliveryEvent.PLAYBACK_ENDED,
            "playback_completed",
        )
        if owner is not None:
            self._release_output_owner(context, fence, reason="playback_completed")
        if not context.output_complete_emitted:
            task_epoch, context_version = self._event_versions(context, fence)
            context.output_complete_emitted = await self.bridge.emit_generation(
                fence.session_id,
                fence,
                action=media_pb2.GENERATION_ACTION_COMPLETE,
                reason="provider_reply_complete",
                task_epoch=task_epoch,
                context_version=context_version,
            )
        await context.runtime.on_media_playback_done(
            fence,
            context.playback.actual_heard_text(fence),
            tools_active=same_turn_followup_output_pending(
                context.delegation_output_claims,
                context.output_work,
                fence,
            ),
        )
        await self._maybe_standby_after_farewell_complete(context, fence)
        self.clear_device_wake_ack_fence(context, fence)
        if not context.standby_requested:
            self.flush_pending_missed_hearing_nudge(context)
        context.playback.discard(fence)
        context.output_sequence = 0
        context.output_text_offset = 0
        # A playback terminal permanently closes this generation on the
        # hardware and Edge ledgers.  Finish the runtime lifecycle before
        # selecting a queued acknowledgement/deep/tool result so the existing
        # auxiliary-output path rebinds it to a successor generation.  Starting
        # queued work above this boundary made its behavior depend on a small
        # race between playback.ended and delegation completion, and could send
        # new PCM into a fence the device had already terminally closed.
        await self._start_selected_output(context)

    async def _fail_playback_output(
        self,
        context: _MediaVoiceSession,
        fence: GenerationFence,
        *,
        reason: str,
    ) -> None:
        if not context.runtime.fence.matches(fence):
            return
        self._record_reply_delivery_event(
            context,
            fence,
            ReplyDeliveryEvent.ERROR,
            reason,
        )
        await self._cancel_reply_task(context, fence, reason=reason)
        cancelled = await self._advance_failed_output_generation(
            context,
            fence,
            reason=reason,
        )
        context.playback.discard(fence)
        context.assistant_text = ""
        context.output_sequence = 0
        context.output_text_offset = 0
        context.provider_complete = False
        context.output_complete_emitted = False
        if cancelled is None:
            await context.runtime.on_assistant_reply_aborted(fence, cause=reason)
            return
        task_epoch, context_version = self._event_versions(context, cancelled)
        await self.bridge.emit_generation(
            cancelled.session_id,
            cancelled,
            action=media_pb2.GENERATION_ACTION_CANCEL,
            reason=reason,
            task_epoch=task_epoch,
            context_version=context_version,
        )
