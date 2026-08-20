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
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2 as _media_pb2
from services.agent.src.voice_core.media_bridge_server import MediaBridgeSession, PCMFrame
from services.agent.src.voice_core.media_protocol import PlaybackProgress
from services.agent.src.voice_core.media_session_types import (
    MediaReplyChunk,
    MediaTextSpan,
    OutputDispatchResult,
    OutputDispatchStatus,
)
from services.agent.src.voice_core.media_session_types import (
    OutputOwnerLease as _OutputOwnerLease,
)
from services.agent.src.voice_core.media_session_types import (
    OutputWork as _OutputWork,
)
from services.agent.src.voice_core.playback_ledger import PlaybackSpan

if TYPE_CHECKING:
    from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

media_pb2: Any = _media_pb2


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

    async def on_playback_progress(
        self,
        session: MediaBridgeSession,
        progress: PlaybackProgress,
    ) -> None:
        context = self._sessions.get(session.identity.session_id)
        if context is None or context.closed:
            return
        fence = GenerationFence(
            session_id=context.identity.session_id,
            turn_id=progress.turn_id,
            generation_id=progress.generation_id,
            tool_epoch=progress.tool_epoch,
        )
        acknowledged = context.playback.acknowledge(
            fence,
            progress.rendered_sample_end,
            received_sequence=progress.received_sequence,
            approximate=progress.approximate,
            heard_eligible=not (session.identity.client_type == "device" and progress.approximate),
        )
        # Publish the cumulative acknowledged prefix under one turn/revision;
        # publishing only the newly acknowledged span would make clients
        # replace a complete answer with its last phrase.
        heard = context.playback.actual_heard_text(fence)
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

    async def _stream_output(
        self,
        context: _MediaVoiceSession,
        session_id: str,
        fence: GenerationFence,
        lease: _OutputOwnerLease,
        chunks: AsyncIterator[MediaReplyChunk],
    ) -> OutputDispatchResult:
        """Send one selected source through the shared owner and PCM ledger."""

        emitted_audio = False
        try:
            async for chunk in chunks:
                if not self._output_owner_is_current(context, lease):
                    self.metrics.inc_media_stale_generation()
                    await self._cancel_reply_task(context, fence, reason="superseded")
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
                        await self._cancel_reply_task(context, fence, reason="superseded")
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
                    await self._cancel_reply_task(context, fence, reason="stale_generation")
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
                    await self._cancel_reply_task(context, fence, reason="transport_rejected")
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
        if owner is not None:
            self._release_output_owner(context, fence, reason="playback_completed")
        if await self._start_selected_output(context):
            return
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
        )
