"""ASR audio ingress and provider-task lifecycle mixin for the voice provider adapter.

``MediaAsrIngressMixin`` owns how PCM frames reach the ASR session and how the
session's tasks are finalized, fenced and replaced around stream epochs, input
gaps, failures and playback.  The adapter dataclass owns the state and the
shared ASR helpers, declared here for type checking only.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from typing import TYPE_CHECKING

from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.speech_timeline import ASRResult

if TYPE_CHECKING:
    from services.agent.src.providers.asr_session import StreamingASRSession
    from services.agent.src.providers.funasr_protocol import FunASRServerEvent
    from services.agent.src.voice_core.provider_adapter import (
        ExistingVoiceProviderConfig,
    )

# A capture hold stops PCM while the device is idle. The provider task is
# usually closed by then; rebuild it before the first frame after a gap this
# long instead of sending that frame into a dead task.
_RESUME_GAP_S = 3.0


class MediaAsrIngressMixin:
    """Feed PCM to the ASR session and manage its task lifecycle."""

    if TYPE_CHECKING:
        config: ExistingVoiceProviderConfig
        _asr: StreamingASRSession | None
        _stream_epoch: int
        _audio_lock: asyncio.Lock
        _audio_since_finalize: bool
        _last_ingest_sample_end: int | None
        _asr_resume_sample_floor: int | None
        _pending_vad_start: tuple[int, int] | None
        _applied_vad_start: tuple[int, int] | None

        async def _ensure_asr(
            self, stream_epoch: int, identity: SessionIdentity | None = None
        ) -> StreamingASRSession: ...

        async def _reset_asr_session(self, stream_epoch: int) -> None: ...

        async def _rotate_asr_task(self, identity: SessionIdentity, *, reason: str) -> None: ...

        def _remember_asr_task(self, asr: StreamingASRSession, stream_epoch: int) -> None: ...

        def _apply_vad_start(self, asr: StreamingASRSession) -> None: ...

        def _clear_asr_event_context(self) -> None: ...

        def _map_asr_event(
            self,
            asr: StreamingASRSession,
            event: FunASRServerEvent,
            stream_epoch: int,
        ) -> ASRResult | None: ...

        def _log_asr_text_debug_decision(
            self,
            identity: SessionIdentity,
            result: ASRResult,
            *,
            source: str,
        ) -> None: ...

    async def reset_for_stream_epoch(self, identity: SessionIdentity) -> None:
        async with self._audio_lock:
            await self._reset_for_stream_epoch(identity)

    async def _reset_for_stream_epoch(self, identity: SessionIdentity) -> None:
        """Fence a transport epoch before its first PCM frame is accepted."""

        stream_epoch = identity.stream_epoch
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if stream_epoch < self._stream_epoch:
            raise ValueError("ASR stream epoch moved backwards")
        if stream_epoch == self._stream_epoch:
            return
        await self._reset_asr_session(stream_epoch)

    async def recover_after_failure(self, identity: SessionIdentity) -> None:
        async with self._audio_lock:
            await self._recover_after_failure(identity)

    async def _recover_after_failure(self, identity: SessionIdentity) -> None:
        """Replace a failed provider task before the next frame is processed."""

        if identity.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self._stream_epoch and identity.stream_epoch < self._stream_epoch:
            raise ValueError("ASR stream epoch moved backwards")
        await self._reset_asr_session(identity.stream_epoch)

    async def ingest_audio(
        self,
        identity: SessionIdentity,
        frame: AudioFrame,
    ) -> tuple[ASRResult, ...]:
        async with self._audio_lock:
            return await self._ingest_audio(identity, frame)

    async def _ingest_audio(
        self,
        identity: SessionIdentity,
        frame: AudioFrame,
    ) -> tuple[ASRResult, ...]:
        """Send one absolute-range PCM frame and drain bounded ASR results."""

        if frame.identity != identity:
            raise ValueError("provider frame identity does not match session")
        if len(frame.payload) % 2:
            raise ValueError("provider input must be 16-bit PCM")
        admitted_frame = self._frame_after_playback_boundary(frame)
        if admitted_frame is None:
            return ()
        frame = admitted_frame
        asr = await self._ensure_asr(identity.stream_epoch, identity)
        self._apply_vad_start(asr)
        if (
            self._last_ingest_sample_end is not None
            and frame.capture_start_sample - self._last_ingest_sample_end
            > _RESUME_GAP_S * self.config.sample_rate
        ):
            reset_for_gap = getattr(asr, "reset_for_gap", None)
            if callable(reset_for_gap):
                await reset_for_gap()
            else:
                await self._rotate_asr_task(identity, reason="upstream_gap")
            self._applied_vad_start = None
            self._apply_vad_start(asr)
        self._remember_asr_task(asr, identity.stream_epoch)
        await asr.send_pcm(
            frame.payload,
            capture_start_sample=frame.capture_start_sample,
        )
        self._audio_since_finalize = True
        self._last_ingest_sample_end = frame.capture_start_sample + frame.frame_samples
        # ``send_pcm`` may transparently reconnect and advance the provider
        # task. Remember both sides so queued late events retain their own
        # task epoch and absolute sample origin.
        self._remember_asr_task(asr, identity.stream_epoch)
        results: list[ASRResult] = []
        for _ in range(self.config.max_events_per_audio_frame):
            try:
                event = asr.events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if event.event == "task-failed":
                raise RuntimeError(f"FunASR task failed error_code={event.error_code or 'unknown'}")
            if event.event != "result-generated" or event.sentence is None:
                continue
            result = self._map_asr_event(asr, event, identity.stream_epoch)
            if result is not None:
                results.append(result)
        return tuple(results)

    async def finalize_speech_segment(
        self,
        identity: SessionIdentity,
    ) -> tuple[ASRResult, ...]:
        async with self._audio_lock:
            return await self._finalize_speech_segment(identity)

    async def _finalize_speech_segment(
        self,
        identity: SessionIdentity,
    ) -> tuple[ASRResult, ...]:
        """Close one VAD task and arm the next task for its first PCM frame."""

        if self._asr is None or not self._audio_since_finalize:
            return ()
        asr = await self._ensure_asr(identity.stream_epoch, identity)
        previous_task_id = str(getattr(asr, "task_id", "") or "")
        if not previous_task_id:
            raise RuntimeError("FunASR segment boundary has no active task id")
        self._remember_asr_task(asr, identity.stream_epoch)
        await asr.rotate_task(require_consumed=False)
        self._remember_asr_task(asr, identity.stream_epoch)

        results: list[ASRResult] = []
        boundary_seen = False
        final_seen = False
        tail_deadline: float | None = None
        tail_grace_s = float(getattr(getattr(asr, "config", None), "post_finish_tail_grace_s", 0.0))
        # Audio ingestion continuously drains provider events. The larger
        # history bound is a fail-closed ceiling for the small tail that can
        # remain between the final PCM frame and task-finished.
        for _ in range(self.config.max_asr_result_history):
            if asr.events.empty():
                if not boundary_seen or final_seen:
                    break
                loop = asyncio.get_running_loop()
                if tail_deadline is None:
                    tail_deadline = loop.time() + tail_grace_s
                remaining = tail_deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    event = await asyncio.wait_for(asr.events.get(), timeout=remaining)
                except TimeoutError:
                    break
            else:
                event = asr.events.get_nowait()
            if event.event == "task-failed":
                raise RuntimeError(f"FunASR task failed error_code={event.error_code or 'unknown'}")
            if event.event == "result-generated" and event.sentence is not None:
                result = self._map_asr_event(asr, event, identity.stream_epoch)
                if result is not None:
                    self._log_asr_text_debug_decision(identity, result, source="segment_final")
                    results.append(result)
                    if event.task_id == previous_task_id and result.is_final:
                        final_seen = True
            if event.event == "task-finished" and event.task_id == previous_task_id:
                boundary_seen = True
                tail_deadline = asyncio.get_running_loop().time() + tail_grace_s
        if not boundary_seen:
            raise RuntimeError("FunASR task boundary event was not available to the media adapter")
        self._audio_since_finalize = False
        self._last_ingest_sample_end = None
        self._pending_vad_start = None
        self._applied_vad_start = None
        return tuple(results)

    async def reset_after_discontinuity(
        self,
        identity: SessionIdentity,
        *,
        capture_start_sample: int,
    ) -> None:
        async with self._audio_lock:
            await self._reset_after_discontinuity(
                identity,
                capture_start_sample=capture_start_sample,
            )

    async def _reset_after_discontinuity(
        self,
        identity: SessionIdentity,
        *,
        capture_start_sample: int,
    ) -> None:
        """Fence a dropped input interval before accepting newer audio."""

        if capture_start_sample < 0:
            raise ValueError("discontinuity sample must be non-negative")
        had_active_asr = self._asr is not None
        asr = await self._ensure_asr(identity.stream_epoch)
        asr.mark_committed_sample(capture_start_sample)
        # Failure recovery has already discarded the poisoned provider task. A
        # fresh session adopts the next frame's absolute sample origin on first
        # send and must not immediately reconnect a second time.
        if had_active_asr:
            await asr.reconnect_with_replay()
        self._audio_since_finalize = False
        self._last_ingest_sample_end = None
        self._pending_vad_start = None
        self._applied_vad_start = None
        self._clear_asr_event_context()
        self._remember_asr_task(asr, identity.stream_epoch)

    def _frame_after_playback_boundary(self, frame: AudioFrame) -> AudioFrame | None:
        """Drop the part of a capture frame that predates the playback fence."""

        floor = self._asr_resume_sample_floor
        if floor is None:
            return frame
        frame_end = frame.capture_start_sample + frame.frame_samples
        if frame_end <= floor:
            return None
        if frame.capture_start_sample < floor:
            offset_samples = floor - frame.capture_start_sample
            frame = replace(
                frame,
                capture_start_sample=floor,
                frame_samples=frame_end - floor,
                payload=frame.payload[offset_samples * 2 :],
            )
        self._asr_resume_sample_floor = None
        return frame

    async def reset_asr_after_playback(
        self,
        identity: SessionIdentity,
        *,
        playback_end_sample: int,
        playback_floor_sample: int,
    ) -> None:
        async with self._audio_lock:
            await self._reset_asr_after_playback(
                identity,
                playback_end_sample=playback_end_sample,
                playback_floor_sample=playback_floor_sample,
            )

    async def _reset_asr_after_playback(
        self,
        identity: SessionIdentity,
        *,
        playback_end_sample: int,
        playback_floor_sample: int,
    ) -> None:
        """Rotate normally completed playback and fence its echo tail.

        The caller reaches this seam only after provider completion and full
        playback acknowledgement. A spoken stop or content barge-in preempts
        the reply before this seam, so rotating here cannot discard one that
        already interrupted the reply. A sentence that started before the
        playback end without being recognized as an interrupt can lose its
        beginning with the old task; that is an accepted limitation of this
        round.
        """

        if playback_end_sample < 0 or playback_floor_sample < 0:
            raise ValueError("playback samples must be non-negative")
        if playback_floor_sample < playback_end_sample:
            raise ValueError("playback floor sample must not precede playback end")
        resume_sample_floor = max(
            playback_floor_sample,
            self._asr_resume_sample_floor or 0,
        )
        self._asr_resume_sample_floor = resume_sample_floor
        self._audio_since_finalize = False
        self._last_ingest_sample_end = None
        self._pending_vad_start = None
        self._applied_vad_start = None
        asr = self._asr
        if asr is None:
            self._clear_asr_event_context()
            return
        reset_for_gap = getattr(asr, "reset_for_gap", None)
        if callable(reset_for_gap):
            result = reset_for_gap()
            if inspect.isawaitable(result):
                await result
            self._clear_asr_event_context()
            return
        await self._reset_asr_session(identity.stream_epoch)
        self._asr_resume_sample_floor = resume_sample_floor

    async def pause_asr_for_playback(self, identity: SessionIdentity) -> None:
        async with self._audio_lock:
            await self._pause_asr_for_playback(identity)

    async def _pause_asr_for_playback(self, identity: SessionIdentity) -> None:
        """Close the current ASR task when playback starts to avoid idle timeout.

        Half-duplex playback stops capture, so FunASR would idle-timeout after
        23 seconds. Rotating the task here prevents that; a new task starts
        when capture resumes. interrupt_assist callers must not invoke this.
        """

        self._pending_vad_start = None
        self._applied_vad_start = None
        await self._rotate_asr_task(identity, reason="playback_start")
