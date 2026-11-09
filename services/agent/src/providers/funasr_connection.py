"""FunASR websocket connection lifecycle: transport I/O and recovery.

``FunASRConnectionMixin`` is composed into ``FunASRSession``; the session owns
the provider websocket trace, the audio evidence, the task failure bookkeeping
shared with ``FunASRTaskFailureMixin`` and the send, rotation and rescue flows,
while the state this mixin reads is declared here only for type checking.  The
module exists so the transport half of the adapter can be read on its own:
connect/retry, the receive loop, control messages, reconnect with PCM replay,
readiness and close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

import websockets
from websockets.asyncio.client import ClientConnection

from services.agent.src.providers.funasr_failures import _is_empty_audio_error
from services.agent.src.providers.funasr_protocol import (
    FunASRServerEvent,
    build_continue_task_context,
    build_finish_task,
    build_run_task,
    parse_server_message,
)
from services.agent.src.providers.provider_errors import APIConnectionError

if TYPE_CHECKING:
    from services.agent.src.observability.metrics import MetricsRegistry
    from services.agent.src.providers.funasr_failures import (
        FunASRTaskEventContext,
        FunASRTaskFailure,
    )
    from services.agent.src.providers.funasr_stt import FunASRConfig
    from services.agent.src.providers.reliability import CircuitBreaker

logger = logging.getLogger(__name__)


class FunASRConnectionMixin:
    """Own the FunASR websocket connection lifecycle and reconnect replay."""

    if TYPE_CHECKING:
        # Shared state owned by FunASRSession.__init__.
        config: FunASRConfig
        metrics: MetricsRegistry | None
        task_id: str | None
        events: asyncio.Queue[FunASRServerEvent]
        _ws: ClientConnection | None
        _started: asyncio.Event
        _closed: bool
        _failed: bool
        _recv_task: asyncio.Task[None] | None
        _ready: asyncio.Event
        _reconnect_lock: asyncio.Lock
        _breaker: CircuitBreaker
        _finishing: bool
        _terminal_finishing: bool
        _rotation_pending: bool
        _task_started_event: asyncio.Event
        _task_finished_received: asyncio.Event
        _task_finished_consumed: asyncio.Event
        _idle_terminal_event: asyncio.Event
        _finished_task_id: str | None
        _task_failed_event: asyncio.Event
        _replaceable_provider_failure: bool
        _last_task_failure: FunASRTaskFailure | None
        _pcm_ring: deque[bytes]
        _pcm_ring_ranges: deque[tuple[int, int]]
        _pcm_ring_bytes: int
        _max_ring_bytes: int
        _rescue_deadline: float | None
        _segment_nonempty_final_seen: bool
        _task_epoch: int
        _task_sample_origin: int
        _task_audio_end_sample: int | None
        _task_audio_send_count: int
        _last_sent_sample: int
        _last_provider_acked_sample: int
        _last_emitted_final_sample: int
        _context: tuple[dict[str, object], ...]
        _metrics_ws_active: bool
        _ws_trace_enabled: bool
        _trace_suppressed_total: int

        # Core session members consumed by this mixin.
        def _trace_ws(self, message: str) -> None: ...

        def _log_realtime_final_text(self, sentence: Any, task_id: str) -> None: ...

        def _record_task_audio_send(
            self,
            *,
            pcm: bytes,
            start_sample: int,
            end_sample: int,
        ) -> None: ...

        def _reset_segment_evidence(self) -> None: ...

        def _reset_task_audio_evidence(self) -> None: ...

        def replay_start_sample(self) -> int: ...

        @property
        def task_pcm_peak_abs(self) -> int | None: ...

        @property
        def task_pcm_rms(self) -> float | None: ...

        # FunASRTaskFailureMixin members consumed by this mixin.
        def _failure_matches_current_task(self) -> bool: ...

        def _failure_exception(self, prefix: str) -> APIConnectionError: ...

        def _record_task_failure(
            self,
            *,
            task_id: str | None = None,
            task_epoch: int | None = None,
            segment_epoch: int | None = None,
            error_code: Any = None,
            error_message: Any = None,
        ) -> FunASRTaskFailure: ...

        def _remember_current_task_event_context(self) -> None: ...

        def task_event_context(
            self, task_id: str
        ) -> FunASRTaskEventContext | None: ...

    def _mark_ws_connected(self) -> None:
        if self.metrics is not None and not self._metrics_ws_active:
            self.metrics.add_provider_ws_active("asr", 1)
            self._metrics_ws_active = True

    def _mark_ws_disconnected(self) -> None:
        if self.metrics is not None and self._metrics_ws_active:
            self.metrics.add_provider_ws_active("asr", -1)
            self._metrics_ws_active = False

    def _clear_pcm_ring(self) -> None:
        self._pcm_ring.clear()
        self._pcm_ring_ranges.clear()
        self._pcm_ring_bytes = 0

    def _build_run_task(self) -> dict[str, Any]:
        return build_run_task(
            model=self.config.model,
            sample_rate=self.config.sample_rate,
            language_hints=[self.config.language],
            semantic_punctuation_enabled=self.config.semantic_punctuation,
            max_sentence_silence_ms=self.config.max_sentence_silence_ms,
            heartbeat=self.config.heartbeat,
            context=list(self._context),
            vocabulary_id=self.config.vocabulary_id,
            speech_noise_threshold=self.config.speech_noise_threshold,
            vad_model=self.config.vad_model,
        )

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("FunASR session is closed")
        self._breaker.before_request()
        try:
            ws, task_id, startup_events = await self._open_with_retry()
        except Exception:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        self._ws = ws
        self.task_id = task_id
        self._task_epoch += 1
        self._task_sample_origin = 0
        self._reset_task_audio_evidence()
        self._reset_segment_evidence()
        self._rescue_deadline = None
        self._failed = False
        self._replaceable_provider_failure = False
        self._finishing = False
        self._terminal_finishing = False
        self._rotation_pending = False
        self._task_started_event.set()
        self._task_finished_received.clear()
        self._task_finished_consumed.clear()
        self._idle_terminal_event.clear()
        self._finished_task_id = None
        self._task_failed_event.clear()
        self._started.set()
        self._ready.set()
        self._remember_current_task_event_context()
        self._mark_ws_connected()
        for ev in startup_events:
            await self.events.put(ev)
        self._recv_task = asyncio.create_task(self._recv_loop(), name="funasr-recv-loop")

    async def _open_once(
        self,
    ) -> tuple[ClientConnection, str, list[FunASRServerEvent]]:
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        ws: ClientConnection | None = None
        startup_events: list[FunASRServerEvent] = []
        try:
            ws = await websockets.connect(
                self.config.ws_url,
                additional_headers=headers,
                open_timeout=self.config.connect_timeout_s,
            )
            run = self._build_run_task()
            task_id = str(run["header"]["task_id"])
            await ws.send(json.dumps(run, ensure_ascii=False))
            self._trace_ws(f"tx run-task task_id={task_id} origin=connect")
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=self.config.connect_timeout_s)
                if isinstance(message, bytes):
                    continue
                ev = parse_server_message(message)
                startup_events.append(ev)
                if ev.event == "task-failed":
                    self._record_task_failure(
                        task_id=ev.task_id or task_id,
                        task_epoch=max(1, self._task_epoch + 1),
                        error_code=ev.error_code,
                        error_message=ev.error_message,
                    )
                    raise self._failure_exception("FunASR task failed during connect")
                if ev.event == "task-started":
                    return ws, task_id, startup_events
        except BaseException:
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            raise

    async def _open_with_retry(
        self,
    ) -> tuple[ClientConnection, str, list[FunASRServerEvent]]:
        last_error: BaseException | None = None
        for delay in (0.0, 0.2, 0.5, 1.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self._open_once()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                last_error = exc
        assert last_error is not None
        raise APIConnectionError("FunASR connect failed") from last_error

    async def _recv_loop(self) -> None:
        while not self._closed:
            ws = self._ws
            if ws is None:
                return
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        continue
                    ev = parse_server_message(message)
                    self._trace_ws(
                        f"rx event={ev.event} task_id={ev.task_id or 'unknown'}"
                        + (
                            f" sentence_id={ev.sentence.sentence_id}"
                            f" sentence_end={ev.sentence.sentence_end}"
                            f" heartbeat={ev.sentence.heartbeat}"
                            f" text_len={len(ev.sentence.text)}"
                            f" begin_ms={ev.sentence.begin_ms} end_ms={ev.sentence.end_ms}"
                            if ev.sentence is not None
                            else ""
                        )
                        + (f" error_code={ev.error_code}" if ev.error_code else "")
                    )
                    task_context = self.task_event_context(ev.task_id)
                    is_current_task = bool(ev.task_id) and ev.task_id == self.task_id
                    if ev.task_id and task_context is None:
                        logger.info(
                            "unknown FunASR task event ignored event=%s task_id=%s "
                            "current_task_id=%s",
                            ev.event,
                            ev.task_id,
                            self.task_id,
                        )
                        continue
                    if ev.event == "task-failed" and ev.task_id and not is_current_task:
                        logger.info(
                            "stale FunASR task failure ignored task_id=%s current_task_id=%s",
                            ev.task_id,
                            self.task_id,
                        )
                        continue
                    if ev.event in ("task-started", "task-finished") and ev.task_id and not is_current_task:
                        logger.info(
                            "stale FunASR lifecycle event ignored event=%s task_id=%s "
                            "current_task_id=%s",
                            ev.event,
                            ev.task_id,
                            self.task_id,
                        )
                        continue
                    if ev.event == "result-generated" and ev.task_id and not is_current_task:
                        if (
                            task_context is None
                            or not task_context.boundary_observed
                            or ev.sentence is None
                            or not ev.sentence.sentence_end
                        ):
                            logger.info(
                                "stale FunASR result ignored task_id=%s current_task_id=%s",
                                ev.task_id,
                                self.task_id,
                            )
                            continue
                    if (
                        ev.event == "result-generated"
                        and task_context is not None
                        and task_context.boundary_observed
                        and (ev.sentence is None or not ev.sentence.sentence_end)
                    ):
                        logger.info(
                            "non-final FunASR tail ignored after task boundary task_id=%s",
                            ev.task_id,
                        )
                        continue
                    if self._failed and ev.event != "task-failed":
                        logger.info(
                            "late FunASR event ignored after task failure event=%s task_id=%s",
                            ev.event,
                            ev.task_id,
                        )
                        continue
                    if ev.event == "task-failed":
                        empty_audio = _is_empty_audio_error(ev.error_code)
                        self._failed = True
                        self._replaceable_provider_failure = True
                        if not empty_audio:
                            self._breaker.record_failure()
                        self._record_task_failure(
                            task_id=ev.task_id,
                            error_code=ev.error_code,
                            error_message=ev.error_message,
                        )
                        self._task_started_event.set()
                        self._task_failed_event.set()
                        self._mark_ws_disconnected()
                        self._ready.clear()
                        if empty_audio:
                            logger.info(
                                "FunASR empty-audio task will be replaced on next PCM "
                                "task_id=%s task_epoch=%s sends=%s peak=%s rms=%s",
                                ev.task_id,
                                self._task_epoch,
                                self._task_audio_send_count,
                                self.task_pcm_peak_abs,
                                self.task_pcm_rms,
                            )
                    elif (
                        ev.event == "task-started"
                        and ev.task_id == self.task_id
                        and not self._failed
                        and not self._task_failed_event.is_set()
                    ):
                        self._failed = False
                        self._ready.set()
                        self._task_started_event.set()
                        self._remember_current_task_event_context()
                    elif ev.event == "result-generated" and ev.sentence is not None:
                        self._log_realtime_final_text(ev.sentence, ev.task_id)
                        if (
                            ev.sentence.sentence_end
                            and ev.sentence.text
                            and not ev.sentence.heartbeat
                            and not ev.sentence.rescue_synthesized
                        ):
                            # Realtime finals were previously metric-only, so a
                            # log-only view of a session made the realtime path
                            # look completely silent even while it was
                            # delivering transcripts.
                            logger.info(
                                "FunASR realtime final task_id=%s sentence_id=%s "
                                "text_len=%s begin_ms=%s end_ms=%s",
                                ev.task_id,
                                ev.sentence.sentence_id,
                                len(ev.sentence.text),
                                ev.sentence.begin_ms,
                                ev.sentence.end_ms,
                            )
                        result_context = task_context or self.task_event_context(ev.task_id)
                        sample_origin = (
                            result_context.sample_origin
                            if result_context is not None
                            else self._task_sample_origin
                        )
                        task_audio_end = (
                            result_context.audio_end_sample
                            if result_context is not None
                            else self._task_audio_end_sample
                        )
                        end_ms = ev.sentence.end_ms or ev.sentence.begin_ms
                        ack = max(
                            0,
                            sample_origin
                            + round(end_ms * self.config.sample_rate / 1000),
                        )
                        ack_ceiling = (
                            task_audio_end
                            if task_audio_end is not None
                            else self._last_sent_sample
                        )
                        self._last_provider_acked_sample = max(
                            self._last_provider_acked_sample,
                            min(ack, ack_ceiling),
                        )
                        if ev.sentence.sentence_end:
                            self._last_emitted_final_sample = max(
                                self._last_emitted_final_sample,
                                min(ack, ack_ceiling),
                            )
                            if ev.sentence.text.strip():
                                # Nonempty FunASR text suppresses SenseVoice for this segment.
                                self._segment_nonempty_final_seen = True
                    elif ev.event == "task-finished" and ev.task_id == self.task_id:
                        self._finished_task_id = ev.task_id
                        self._ready.clear()
                        self._task_finished_received.set()
                        self._remember_current_task_event_context()
                        logger.info(
                            "FunASR task boundary observed task_id=%s rotation_pending=%s",
                            ev.task_id,
                            self._rotation_pending,
                        )
                    await self.events.put(ev)
                    if ev.event == "task-failed":
                        with contextlib.suppress(Exception):
                            await ws.close()
                        self._failed = True
                        return
                if self._closed:
                    return
                raise APIConnectionError("FunASR WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._failed or self._task_failed_event.is_set():
                    return
                if not await self._recover(ws):
                    self._failed = True
                    self._task_failed_event.set()
                    if not self._failure_matches_current_task():
                        self._record_task_failure(error_message="FunASR reconnect failed")
                    failure = self._last_task_failure
                    await self.events.put(
                        FunASRServerEvent(
                            event="task-failed",
                            task_id=self.task_id or "",
                            error_code=failure.error_code if failure else None,
                            error_message=(
                                failure.error_message
                                if failure
                                else "FunASR reconnect failed"
                            ),
                        )
                    )
                    logger.info(
                        "FunASR receive loop ended after failed recovery task_id=%s",
                        self.task_id or "",
                    )
                    return

    async def _recover(self, failed_ws: ClientConnection) -> bool:
        async with self._reconnect_lock:
            if self._closed or self._failed or self._task_failed_event.is_set():
                return False
            if self._ws is not failed_ws and self._ready.is_set():
                return True
            self._ready.clear()
            self._mark_ws_disconnected()
            with contextlib.suppress(Exception):
                await failed_ws.close()
            started_at = asyncio.get_running_loop().time()
            try:
                replay_start = self.replay_start_sample()
                self._breaker.before_request()
                if self.metrics is not None:
                    self.metrics.inc_provider_ws_reconnect("asr")
                ws, task_id, startup_events = await self._open_with_retry()
            except Exception:
                self._breaker.record_failure()
                self._failed = True
                self._ready.clear()
                self._task_failed_event.set()
                self._clear_pcm_ring()
                if self._ws is failed_ws:
                    self._ws = None
                self._mark_ws_disconnected()
                return False
            try:
                self._ws = ws
                self.task_id = task_id
                self._task_epoch += 1
                self._task_sample_origin = replay_start
                self._reset_task_audio_evidence()
                self._failed = False
                self._replaceable_provider_failure = False
                self._task_started_event.set()
                self._task_finished_received.clear()
                self._task_finished_consumed.clear()
                self._finished_task_id = None
                self._task_failed_event.clear()
                self._remember_current_task_event_context()
                self._mark_ws_connected()
                for ev in startup_events:
                    await self.events.put(ev)
                if asyncio.get_running_loop().time() - started_at <= 2.0:
                    replay = self._replay_pcm()
                    if replay:
                        await ws.send(replay)
                        self._record_task_audio_send(
                            pcm=replay,
                            start_sample=replay_start,
                            end_sample=replay_start + len(replay) // 2,
                        )
                if self._finishing:
                    await ws.send(json.dumps(build_finish_task(task_id), ensure_ascii=False))
            except Exception:
                self._failed = True
                self._task_failed_event.set()
                self._ready.clear()
                self._clear_pcm_ring()
                self._mark_ws_disconnected()
                with contextlib.suppress(Exception):
                    await ws.close()
                if self._ws is ws:
                    self._ws = None
                self._breaker.record_failure()
                return False
            self._breaker.record_success()
            self._ready.set()
            return True

    def _push_ring(self, pcm: bytes, *, start_sample: int | None = None) -> None:
        start = self._last_sent_sample if start_sample is None else start_sample
        end = start + len(pcm) // 2
        self._pcm_ring.append(pcm)
        self._pcm_ring_ranges.append((start, end))
        self._pcm_ring_bytes += len(pcm)
        while self._pcm_ring_bytes > self._max_ring_bytes and self._pcm_ring:
            old = self._pcm_ring.popleft()
            self._pcm_ring_ranges.popleft()
            self._pcm_ring_bytes -= len(old)

    def _replay_pcm(self) -> bytes:
        """Slice the bounded ring at the reconnect watermark."""

        start_sample = self.replay_start_sample()
        chunks: list[bytes] = []
        for pcm, (frame_start, frame_end) in zip(
            self._pcm_ring,
            self._pcm_ring_ranges,
            strict=True,
        ):
            if frame_end <= start_sample:
                continue
            offset_samples = max(0, start_sample - frame_start)
            chunks.append(pcm[offset_samples * 2 :])
        return b"".join(chunks)

    async def update_context(self, context: tuple[dict[str, object], ...]) -> None:
        self._context = context
        if self._ws is None or self.task_id is None:
            return
        if self._rotation_pending or self._finished_task_id == self.task_id:
            # The next run-task snapshots ``self._context``.  Do not send a
            # continue-task to an already-finished provider task.
            return
        await self._wait_until_ready("update context")
        msg = build_continue_task_context(self.task_id, list(context))
        await self._ws.send(json.dumps(msg, ensure_ascii=False))

    async def _wait_until_ready(self, operation: str) -> None:
        try:
            await asyncio.wait_for(
                self._ready.wait(),
                timeout=self.config.connect_timeout_s,
            )
        except TimeoutError as exc:
            raise APIConnectionError(
                f"FunASR was not ready to {operation} after {self.config.connect_timeout_s:.3f}s"
            ) from exc
        if self._failed or self._task_failed_event.is_set():
            raise self._failure_exception(f"FunASR cannot {operation}")
        if self._ws is None:
            raise APIConnectionError(f"FunASR cannot {operation}: session unavailable")

    async def aclose(self) -> None:
        self._closed = True
        self._rescue_deadline = None
        self._reset_segment_evidence()
        self._clear_pcm_ring()
        if self._ws_trace_enabled and self._trace_suppressed_total:
            logger.info(
                "funasr_ws_trace closing suppressed_total=%s task_id=%s",
                self._trace_suppressed_total,
                self.task_id or "unknown",
            )
        self._mark_ws_disconnected()
        self._ready.clear()
        # Wake any waiter blocked on a task boundary; it must fail fast
        # instead of burning the full result timeout on a closed session.
        self._task_failed_event.set()
        self._idle_terminal_event.set()
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
            self._recv_task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def reconnect_with_replay(self) -> None:
        ws = self._ws
        if ws is None or not await self._recover(ws):
            raise APIConnectionError("FunASR reconnect failed")
