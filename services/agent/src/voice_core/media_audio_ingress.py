"""Bounded Media Bridge audio ingestion behind the session-registry seam."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.voice_core.asr_stream_supervisor import ASRAcceptDecision
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_pcm_tap import MediaPcmTap, maybe_create_tap
from services.agent.src.voice_core.media_protocol import AudioFrame
from services.agent.src.voice_core.media_session_types import ProviderAudioTaskSnapshot
from services.agent.src.voice_core.speech_timeline import ASRResult, asr_result_to_segment
from src.voice_core.two_stage_denoiser import TwoStageDenoiser, TwoStageDenoisingConfig
from src.voice_core.listening_state_manager import (
    ListeningStateManager,
    ListeningStateConfig,
    ListeningState,
)

if TYPE_CHECKING:
    from services.agent.src.voice_core.media_session_state import (
        MediaVoiceSessionState as _MediaVoiceSession,
    )

logger = logging.getLogger(__name__)

# Overflow and pump-stall diagnostics are rate limited per session so a
# multi-second provider stall produces a bounded number of log lines.
_OVERFLOW_LOG_INTERVAL_S = 5.0
_PUMP_STALL_THRESHOLD_S = 2.0
_PUMP_STALL_LOG_INTERVAL_S = 5.0


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
    pcm_tap: MediaPcmTap | None = None
    pcm_tap_initialized: bool = False
    overflow_dropped_frames: int = 0
    overflow_dropped_samples: int = 0
    last_overflow_log: float = 0.0
    last_pump_stall_log: float = 0.0

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

    def _schedule_turn_commit(self, context: _MediaVoiceSession) -> None: ...


class MediaAudioIngress:
    """Serialize bounded PCM ingress without blocking the bridge reader."""

    def __init__(self, host: MediaAudioIngressHost) -> None:
        self._host = host

        # Initialize two-stage denoising pipeline
        self._denoiser = TwoStageDenoiser(TwoStageDenoisingConfig(
            stage1_enabled=True,    # RNNoise lightweight denoising
            stage2_enabled=True,    # DTLN deep denoising
            skip_stage2_on_silence=True,  # Skip stage 2 on silence for performance
            vad_threshold=0.3,      # Threshold for determining silence
        ))

        # Initialize listening state manager
        self._state_manager = ListeningStateManager(ListeningStateConfig(
            idle_timeout=5.0,       # Auto-exit listening after 5s of no speech
            silence_timeout=1.5,    # Consider speech ended after 1.5s silence
            auto_transition=True,   # Automatically transition states
            allow_interruption=True,  # Allow interruption during speaking
        ))

        # Statistics
        self._denoising_log_interval = 100  # Log stats every N frames

        logger.info(
            "MediaAudioIngress initialized with denoising and state management: "
            "denoiser_stage1=%s denoiser_stage2=%s",
            self._denoiser._stage1._rnnoise_available,
            self._denoiser._stage2._model_available,
        )

    async def cancel(self, context: _MediaVoiceSession) -> None:
        state = context.ingress
        task = state.pump_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        state.pump_task = None
        self._drain(state.queue)
        tap = state.pcm_tap
        state.pcm_tap = None
        state.pcm_tap_initialized = False
        if tap is not None:
            tap.close()
        self._host.metrics.set_media_metric("media_pcm_queue_depth", 0.0)

        # Clean up listening state for this session
        self._state_manager.cleanup_session(context.identity.session_id)

    async def start_listening(self, session_id: str) -> bool:
        """Transition session to LISTENING state (triggered by button/wake word).

        Returns:
            True if transition succeeded, False if already listening
        """
        return await self._state_manager.transition_to_listening(session_id)

    async def stop_listening(self, session_id: str, reason: str = "manual") -> None:
        """Transition session to IDLE state (stop listening)."""
        await self._state_manager.transition_to_idle(session_id, reason)

    def get_listening_state(self, session_id: str) -> ListeningState:
        """Get current listening state for a session."""
        return self._state_manager.get_state(session_id)

    async def initialize_session_listening(self, session_id: str) -> None:
        """Initialize listening state for a new session.

        Temporary: Auto-enters LISTENING state on connection.
        TODO: Replace with wake word or button trigger.
        """
        logger.info(
            "Initializing session listening: session=%s auto_enter_listening=True",
            session_id,
        )
        await self._state_manager.transition_to_listening(session_id)

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
                self._drop_oldest_frame(context)
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
        # A reconnect advances the stream epoch; start a fresh tap file so the
        # capture file name matches the epoch it actually contains.
        tap = context.ingress.pcm_tap
        context.ingress.pcm_tap = None
        context.ingress.pcm_tap_initialized = False
        if tap is not None:
            tap.close()

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

    async def finalize_speech_segment(
        self,
        context: _MediaVoiceSession,
        *,
        vad_start_sample: int | None = None,
        vad_event_sample: int | None = None,
        voiced_end_sample: int | None = None,
        finalize_reason: str = "unspecified",
    ) -> bool:
        """Rotate one provider task without letting a provider fault kill the bridge."""

        async with context.ingress.finalize_lock:
            callback_stream_epoch = context.stream_epoch
            await self._wait_until_idle(context)
            audio_watermark = context.asr.last_sent_sample
            previous_watermark = context.ingress.last_finalized_audio_watermark
            task_epoch_before = self._provider_task_epoch(context)
            provider_audio_before = self._provider_audio_snapshot(context)
            if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
                self._log_asr_boundary(
                    context,
                    result="stale",
                    finalize_reason=finalize_reason,
                    vad_start_sample=vad_start_sample,
                    vad_event_sample=vad_event_sample,
                    voiced_end_sample=voiced_end_sample,
                    audio_watermark=audio_watermark,
                    previous_watermark=previous_watermark,
                    task_epoch_before=task_epoch_before,
                    task_epoch_after=self._provider_task_epoch(context),
                    provider_audio=provider_audio_before,
                )
                return False
            if audio_watermark <= previous_watermark:
                self._log_asr_boundary(
                    context,
                    result="duplicate",
                    finalize_reason=finalize_reason,
                    vad_start_sample=vad_start_sample,
                    vad_event_sample=vad_event_sample,
                    voiced_end_sample=voiced_end_sample,
                    audio_watermark=audio_watermark,
                    previous_watermark=previous_watermark,
                    task_epoch_before=task_epoch_before,
                    task_epoch_after=task_epoch_before,
                    provider_audio=provider_audio_before,
                )
                return True
            finalize = getattr(context.provider, "finalize_speech_segment", None)
            if not callable(finalize):
                self._log_asr_boundary(
                    context,
                    result="no-provider-finalize",
                    finalize_reason=finalize_reason,
                    vad_start_sample=vad_start_sample,
                    vad_event_sample=vad_event_sample,
                    voiced_end_sample=voiced_end_sample,
                    audio_watermark=audio_watermark,
                    previous_watermark=previous_watermark,
                    task_epoch_before=task_epoch_before,
                    task_epoch_after=task_epoch_before,
                    provider_audio=provider_audio_before,
                )
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
                self._log_asr_boundary(
                    context,
                    result="failed",
                    finalize_reason=finalize_reason,
                    vad_start_sample=vad_start_sample,
                    vad_event_sample=vad_event_sample,
                    voiced_end_sample=voiced_end_sample,
                    audio_watermark=audio_watermark,
                    previous_watermark=previous_watermark,
                    task_epoch_before=task_epoch_before,
                    task_epoch_after=self._provider_task_epoch(context),
                    provider_audio=provider_audio_before,
                )
                return False
            if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
                self._log_asr_boundary(
                    context,
                    result="stale",
                    finalize_reason=finalize_reason,
                    vad_start_sample=vad_start_sample,
                    vad_event_sample=vad_event_sample,
                    voiced_end_sample=voiced_end_sample,
                    audio_watermark=audio_watermark,
                    previous_watermark=previous_watermark,
                    task_epoch_before=task_epoch_before,
                    task_epoch_after=self._provider_task_epoch(context),
                    provider_audio=provider_audio_before,
                )
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
            # Final ASR results returned by provider finalization are observed
            # before this watermark is published. Re-arm the endpoint check
            # after publishing the watermark, otherwise a zero/short grace
            # task can fail closed once and the valid final is later discarded
            # by the absolute tail timeout.
            if context.turn_endpoint_sample is not None:
                self._host._schedule_turn_commit(context)
            self._log_asr_boundary(
                context,
                result="success",
                finalize_reason=finalize_reason,
                vad_start_sample=vad_start_sample,
                vad_event_sample=vad_event_sample,
                voiced_end_sample=voiced_end_sample,
                audio_watermark=audio_watermark,
                previous_watermark=previous_watermark,
                task_epoch_before=task_epoch_before,
                task_epoch_after=self._provider_task_epoch(context),
                provider_audio=provider_audio_before,
            )
            return True

    @staticmethod
    def _provider_task_epoch(context: _MediaVoiceSession) -> int:
        value = getattr(context.provider, "current_asr_task_epoch", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _provider_audio_snapshot(
        context: _MediaVoiceSession,
    ) -> ProviderAudioTaskSnapshot | None:
        value = getattr(context.provider, "current_asr_audio_task_snapshot", None)
        return value if isinstance(value, ProviderAudioTaskSnapshot) else None

    @staticmethod
    def _provider_rotation_pending(context: _MediaVoiceSession) -> bool:
        value = getattr(context.provider, "asr_rotation_pending", False)
        return value if isinstance(value, bool) else False

    @staticmethod
    def _log_asr_boundary(
        context: _MediaVoiceSession,
        *,
        result: str,
        finalize_reason: str,
        vad_start_sample: int | None,
        vad_event_sample: int | None,
        voiced_end_sample: int | None,
        audio_watermark: int,
        previous_watermark: int,
        task_epoch_before: int,
        task_epoch_after: int,
        provider_audio: ProviderAudioTaskSnapshot | None,
    ) -> None:
        segment_end = voiced_end_sample if voiced_end_sample is not None else vad_event_sample
        segment_samples = (
            max(0, segment_end - vad_start_sample)
            if vad_start_sample is not None and segment_end is not None
            else None
        )
        payload = {
            "audio_admitted_watermark": audio_watermark,
            "finalize_reason": finalize_reason,
            "previous_finalized_watermark": previous_watermark,
            "provider_pcm_end_sample": (
                provider_audio.audio_end_sample if provider_audio is not None else None
            ),
            "provider_pcm_encoding": (
                provider_audio.encoding if provider_audio is not None else None
            ),
            "provider_pcm_sample_rate_hz": (
                provider_audio.sample_rate_hz if provider_audio is not None else None
            ),
            "provider_pcm_channels": (
                provider_audio.channels if provider_audio is not None else None
            ),
            "provider_pcm_samples": (
                provider_audio.audio_samples if provider_audio is not None else 0
            ),
            "provider_pcm_observed_samples": (
                provider_audio.observed_sample_count if provider_audio is not None else 0
            ),
            "provider_pcm_peak_abs": (
                provider_audio.peak_abs if provider_audio is not None else None
            ),
            "provider_pcm_rms": (
                round(provider_audio.rms, 3)
                if provider_audio is not None and provider_audio.rms is not None
                else None
            ),
            "provider_pcm_all_zero": (
                provider_audio.all_zero if provider_audio is not None else None
            ),
            "provider_pcm_clipping_detected": (
                provider_audio.clipping_detected if provider_audio is not None else None
            ),
            "provider_pcm_send_count": (
                provider_audio.send_count if provider_audio is not None else 0
            ),
            "provider_pcm_start_sample": (
                provider_audio.audio_start_sample if provider_audio is not None else None
            ),
            "provider_task_epoch_after": task_epoch_after,
            "provider_task_epoch_before": task_epoch_before,
            "provider_task_origin_sample": (
                provider_audio.task_sample_origin if provider_audio is not None else None
            ),
            "result": result,
            "next_task_pending": MediaAudioIngress._provider_rotation_pending(context),
            "rotation_observed": task_epoch_after > task_epoch_before > 0,
            "segment_samples": segment_samples,
            "session_id": context.identity.session_id,
            "stream_epoch": context.stream_epoch,
            "vad_event_sample": vad_event_sample,
            "vad_start_sample": vad_start_sample,
            "voiced_end_sample": voiced_end_sample,
        }
        logger.info(
            "media_asr_boundary %s",
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )

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
        results = tuple(results)
        for result in results:
            if not isinstance(result, ASRResult):
                raise RuntimeError("media provider returned an invalid ASR result")
            if not self._host._stream_epoch_is_current(context, callback_stream_epoch):
                logger.warning(
                    "media ASR results dropped after stream epoch change session=%s "
                    "callback_stream_epoch=%s current_stream_epoch=%s dropped=%s",
                    context.identity.session_id,
                    callback_stream_epoch,
                    context.stream_epoch,
                    len(results),
                )
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
                logger.warning(
                    "media ASR result handling aborted after stream epoch change "
                    "session=%s callback_stream_epoch=%s current_stream_epoch=%s",
                    context.identity.session_id,
                    callback_stream_epoch,
                    context.stream_epoch,
                )
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

        session_id = context.identity.session_id

        # Check if we should accept audio based on listening state
        if not self._state_manager.should_accept_audio(session_id):
            current_state = self._state_manager.get_state(session_id)
            logger.debug(
                "Dropping audio frame in state=%s session=%s",
                current_state.value,
                session_id,
            )
            return

        if frame.discontinuity or context.ingress.discontinuity_pending:
            await self._reset_discontinuity(context, frame)

        # Apply two-stage denoising before ASR
        denoised_payload, vad_prob, denoising_stats = self._denoiser.process(frame.payload)

        # Replace frame payload with denoised audio
        frame = replace(frame, payload=denoised_payload)

        # Log denoising statistics periodically
        if denoising_stats["total_frames"] % self._denoising_log_interval == 0:
            logger.info(
                "Denoising stats session=%s: stage2_skip_rate=%.1f%% vad_prob=%.2f "
                "stage1_available=%s stage2_available=%s",
                session_id,
                denoising_stats["stage2_skip_rate"] * 100,
                vad_prob,
                denoising_stats["stage1_available"],
                denoising_stats["stage2_available"],
            )

        self._tap_frame(context, frame)
        results = await context.provider.ingest_audio(context.identity, frame)
        stall_s = time.monotonic() - enqueued_at
        if (
            stall_s > _PUMP_STALL_THRESHOLD_S
            and time.monotonic() - context.ingress.last_pump_stall_log
            > _PUMP_STALL_LOG_INTERVAL_S
        ):
            context.ingress.last_pump_stall_log = time.monotonic()
            logger.warning(
                "media ingress pump stalled session=%s stream_epoch=%s lag_s=%.2f "
                "queue_depth=%s dropped_frames=%s",
                context.identity.session_id,
                context.stream_epoch,
                stall_s,
                context.ingress.queue.qsize(),
                context.ingress.overflow_dropped_frames,
            )
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

    def _drop_oldest_frame(self, context: _MediaVoiceSession) -> None:
        # A slow provider must not silently destroy admitted speech.  Drop only
        # the oldest buffered frame so the newest audio still reaches ASR, and
        # make the loss loud (rate limited) instead of metric-only.
        state = context.ingress
        with contextlib.suppress(asyncio.QueueEmpty):
            dropped, _ = state.queue.get_nowait()
            state.overflow_dropped_frames += 1
            state.overflow_dropped_samples += dropped.frame_samples
        now = time.monotonic()
        if now - state.last_overflow_log > _OVERFLOW_LOG_INTERVAL_S:
            state.last_overflow_log = now
            logger.warning(
                "media ingress queue overflow session=%s stream_epoch=%s "
                "dropped_frames=%s dropped_samples=%s queue_capacity=%s",
                context.identity.session_id,
                context.stream_epoch,
                state.overflow_dropped_frames,
                state.overflow_dropped_samples,
                state.queue.maxsize,
            )

    @staticmethod
    def _tap_frame(context: _MediaVoiceSession, frame: AudioFrame) -> None:
        # Diagnostics-only capture of exactly the PCM admitted toward the ASR
        # provider.  The tap is env-gated and strictly fail-open.
        state = context.ingress
        if not state.pcm_tap_initialized:
            state.pcm_tap_initialized = True
            state.pcm_tap = maybe_create_tap(
                context.identity.session_id,
                context.stream_epoch,
            )
        tap = state.pcm_tap
        if tap is not None:
            tap.write(frame.payload)

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
