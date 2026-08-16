"""Bounded Media Bridge audio ingestion behind the session-registry seam."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import AudioFrame
from services.agent.src.voice_core.speech_timeline import ASRResult, asr_result_to_segment

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MediaAudioIngressState:
    """Per-session queue state owned exclusively by ``MediaAudioIngress``."""

    queue: asyncio.Queue[tuple[AudioFrame, float]]
    pump_task: asyncio.Task[None] | None = None
    discontinuity_pending: bool = False
    provider_failed: bool = False
    recovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finalize_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_finalized_audio_watermark: int = -1
    loss_concealed_ranges: deque[tuple[int, int]] = field(default_factory=deque)

    @classmethod
    def create(cls, max_frames: int) -> MediaAudioIngressState:
        return cls(queue=asyncio.Queue(maxsize=max_frames))


class MediaAudioIngressHost(Protocol):
    """Private registry callbacks needed to commit provider ASR facts."""

    bridge: MediaBridgeGrpcServer
    metrics: MetricsRegistry

    def _stream_epoch_is_current(
        self,
        context: _MediaVoiceSession,
        stream_epoch: int,
    ) -> bool: ...

    async def _discard_projection(
        self,
        context: _MediaVoiceSession,
        reason: str,
    ) -> None: ...

    async def _accept_asr_result_decision(
        self,
        session_id: str,
        result: ASRResult,
    ) -> ASRAcceptDecision: ...

    def _clear_pending_turn_state(self, context: _MediaVoiceSession) -> None: ...


class MediaAudioIngress:
    """Serialize bounded PCM ingress without blocking the bridge reader."""

    def __init__(self, host: MediaAudioIngressHost) -> None:
        self._host = host

    async def cancel(self, context: _MediaVoiceSession) -> None:
        state = context.ingress
        task = state.pump_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.pump_task = None
        self._drain(state.queue)
        self._host.metrics.set_media_metric("media_pcm_queue_depth", 0.0)

    async def accept(self, context: _MediaVoiceSession, frame: AudioFrame) -> None:
        # ``finalize_speech_segment`` rotates the provider task under this same
        # lock.  A replacement transport can also reset the provider while the
        # previous gRPC callback is still unwinding, so admission must wait for
        # the task/epoch boundary before recording or queueing the next frame.
        async with context.ingress.finalize_lock:
            await self._accept_serialized(context, frame)

    async def _accept_serialized(
        self,
        context: _MediaVoiceSession,
        frame: AudioFrame,
    ) -> None:
        callback_stream_epoch = context.stream_epoch
        if (
            frame.identity.session_id != context.identity.session_id
            or frame.identity.stream_epoch != callback_stream_epoch
            or not self._host._stream_epoch_is_current(context, callback_stream_epoch)
        ):
            return
        state = context.ingress
        if state.provider_failed:
            async with state.recovery_lock:
                if state.provider_failed:
                    recover = getattr(context.provider, "recover_after_failure", None)
                    if not callable(recover):
                        logger.error(
                            "media audio provider recovery unavailable session=%s stream_epoch=%s",
                            context.identity.session_id,
                            context.stream_epoch,
                        )
                        return
                    try:
                        result = recover(context.identity)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.exception(
                            "media audio provider recovery failed session=%s stream_epoch=%s",
                            context.identity.session_id,
                            context.stream_epoch,
                        )
                        return
                    state.provider_failed = False
                    state.discontinuity_pending = True
        if not context.asr.record_audio(
            start_sample=frame.capture_start_sample,
            frame_samples=frame.frame_samples,
        ):
            # The Media Edge owns the transport gate; this protects the
            # provider replay window from duplicated bridge callbacks.
            return
        if frame.discontinuity or state.queue.full():
            if state.queue.full():
                self._host.metrics.inc_media_metric("media_pcm_overflow_total")
                self._drain(state.queue)
            self._host.metrics.inc_media_metric("media_discontinuity_total")
            state.discontinuity_pending = True
            frame = replace(frame, discontinuity=True)
        state.queue.put_nowait((frame, time.monotonic()))
        self._host.metrics.set_media_metric("media_pcm_queue_depth", float(state.queue.qsize()))
        task = state.pump_task
        if task is None or task.done():
            state.pump_task = asyncio.create_task(
                self._pump(context),
                name=f"media-audio-pump-{context.identity.session_id}-{context.stream_epoch}",
            )
        # Preserve the former low-load scheduling behaviour without waiting
        # for a slow external ASR provider.
        await asyncio.sleep(0)

    def reset_for_reconnect(self, context: _MediaVoiceSession) -> None:
        context.ingress.loss_concealed_ranges.clear()
        context.ingress.discontinuity_pending = False
        context.ingress.provider_failed = False
        context.ingress.last_finalized_audio_watermark = -1

    async def _reset_discontinuity(
        self,
        context: _MediaVoiceSession,
        frame: AudioFrame,
    ) -> None:
        # A provider turn preparation may be awaiting external work after its
        # Projection preflight. Do not let a later transport discontinuity
        # consume that provisional turn before the preparation can publish it.
        async with context.turn_commit_lock:
            endpoint_task = context.turn_endpoint_task
            if endpoint_task is not None and not endpoint_task.done():
                endpoint_task.cancel()
            if context.turn_endpoint_timeout_handle is not None:
                context.turn_endpoint_timeout_handle.cancel()
                context.turn_endpoint_timeout_handle = None
            context.asr.mark_committed(frame.capture_start_sample)
            await self._host._discard_projection(context, "audio_discontinuity")
            self._host._clear_pending_turn_state(context)
            context.ingress.loss_concealed_ranges.clear()
            reset = getattr(context.provider, "reset_after_discontinuity", None)
            if callable(reset):
                result = reset(
                    context.identity,
                    capture_start_sample=frame.capture_start_sample,
                )
                if inspect.isawaitable(result):
                    await result
            context.ingress.discontinuity_pending = False

    async def finalize_speech_segment(self, context: _MediaVoiceSession) -> bool:
        """Rotate one provider task without letting a provider fault kill the bridge."""

        async with context.ingress.finalize_lock:
            callback_stream_epoch = context.stream_epoch
            await self._wait_until_idle(context)
            if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
                return False
            audio_watermark = context.asr.last_sent_sample
            if audio_watermark <= context.ingress.last_finalized_audio_watermark:
                return True
            finalize = getattr(context.provider, "finalize_speech_segment", None)
            if not callable(finalize):
                return True
            try:
                results = finalize(context.identity)
                if inspect.isawaitable(results):
                    results = await results
            except asyncio.CancelledError:
                raise
            except Exception:
                self._host.metrics.inc_media_session_failed()
                context.ingress.provider_failed = True
                context.ingress.discontinuity_pending = True
                self._drain(context.ingress.queue)
                # Match endpoint-tail and reconnect cleanup: a finalize fault
                # cannot delete the provisional turn while its commit is in
                # the prepare/publish critical section.
                async with context.turn_commit_lock:
                    await self._host._discard_projection(context, "asr_finalize_failed")
                    self._host._clear_pending_turn_state(context)
                logger.exception(
                    "media ASR finalize failed session=%s stream_epoch=%s watermark=%s",
                    context.identity.session_id,
                    context.stream_epoch,
                    audio_watermark,
                )
                return False
            if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
                return False
            # The returned finals belong to the task that just ended. Accept them
            # before announcing the freshly started task epoch; doing it in the
            # opposite order would correctly classify the tail as stale.
            await self._accept_provider_results(
                context,
                results,
                callback_stream_epoch=callback_stream_epoch,
                observe_task_before_results=False,
            )
            context.ingress.last_finalized_audio_watermark = audio_watermark
            return True

    async def _wait_until_idle(self, context: _MediaVoiceSession) -> None:
        state = context.ingress
        while True:
            task = state.pump_task
            if task is None:
                if state.queue.empty():
                    return
                await asyncio.sleep(0)
                continue
            if task is asyncio.current_task():
                raise RuntimeError("media audio ingress cannot wait on its own pump")
            await asyncio.shield(task)

    async def _observe_provider_task(self, context: _MediaVoiceSession) -> None:
        provider_task_epoch = getattr(context.provider, "current_asr_task_epoch", 0)
        if not provider_task_epoch:
            return
        if isinstance(provider_task_epoch, bool) or not isinstance(provider_task_epoch, int):
            raise RuntimeError("media provider returned an invalid ASR task epoch")
        previous_task_epoch = context.asr.latest_authoritative_task_epoch
        if not context.asr.observe_task(provider_task_epoch):
            raise RuntimeError("media provider ASR task epoch moved backwards")
        if provider_task_epoch > previous_task_epoch:
            await self._host.bridge.emit_speech_task_started(
                context.identity.session_id,
                provider_task_epoch,
                context.runtime.speech_timeline,
            )

    async def _accept_provider_results(
        self,
        context: _MediaVoiceSession,
        results: Iterable[ASRResult],
        *,
        callback_stream_epoch: int,
        observe_task_before_results: bool = True,
    ) -> None:
        if observe_task_before_results:
            await self._observe_provider_task(context)
        for result in results:
            if not isinstance(result, ASRResult):
                raise RuntimeError("media provider returned an invalid ASR result")
            if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
                return
            if (
                any(
                    result.capture_start_sample < end and start < result.capture_end_sample
                    for start, end in context.ingress.loss_concealed_ranges
                )
                and not result.loss_concealed
            ):
                confidence = result.confidence
                if confidence is not None:
                    confidence = max(0.0, confidence * 0.75)
                result = replace(result, confidence=confidence, loss_concealed=True)
            decision = await self._host._accept_asr_result_decision(
                context.identity.session_id,
                result,
            )
            if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
                return
            shadow_result = decision.accepted or result
            await self._host.bridge.emit_speech_segment_decision(
                context.identity.session_id,
                asr_result_to_segment(shadow_result, session_id=context.identity.session_id),
                authoritative_accepted=decision.accepted is not None,
                authoritative_reason=decision.reason.value,
                timeline=context.runtime.speech_timeline,
                latest_task_epoch=context.asr.latest_authoritative_task_epoch,
            )
        if not observe_task_before_results:
            await self._observe_provider_task(context)

    async def _process(
        self,
        context: _MediaVoiceSession,
        frame: AudioFrame,
        *,
        enqueued_at: float,
    ) -> None:
        callback_stream_epoch = frame.identity.stream_epoch
        if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
            return
        if frame.discontinuity or context.ingress.discontinuity_pending:
            await self._reset_discontinuity(context, frame)
        results = await context.provider.ingest_audio(context.identity, frame)
        if frame.loss_concealed:
            ranges = context.ingress.loss_concealed_ranges
            ranges.append(
                (frame.capture_start_sample, frame.capture_start_sample + frame.frame_samples)
            )
            while len(ranges) > 64:
                ranges.popleft()
            self._host.metrics.inc_media_metric("media_loss_concealed_frames_total")
        self._host.metrics.set_media_metric(
            "asr_send_lag_ms",
            max(0.0, (time.monotonic() - enqueued_at) * 1_000),
        )
        if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
            return
        context.runtime.feed_speaker_pcm(frame.payload)
        await self._accept_provider_results(
            context,
            results,
            callback_stream_epoch=callback_stream_epoch,
        )

    async def _pump(self, context: _MediaVoiceSession) -> None:
        state = context.ingress
        try:
            while not context.closed:
                try:
                    frame, enqueued_at = state.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                self._host.metrics.set_media_metric(
                    "media_pcm_queue_depth",
                    float(state.queue.qsize()),
                )
                try:
                    await self._process(context, frame, enqueued_at=enqueued_at)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._host.metrics.inc_media_session_failed()
                    state.discontinuity_pending = True
                    state.provider_failed = True
                    self._drain(state.queue)
                    logger.exception(
                        "media audio pump failed session=%s stream_epoch=%s",
                        context.identity.session_id,
                        context.stream_epoch,
                    )
                    return
        finally:
            if state.pump_task is asyncio.current_task():
                state.pump_task = None
            self._host.metrics.set_media_metric(
                "media_pcm_queue_depth",
                float(state.queue.qsize()),
            )

    @staticmethod
    def _drain(queue: asyncio.Queue[tuple[AudioFrame, float]]) -> None:
        while True:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
                continue
            return
