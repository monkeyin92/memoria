"""FunASR Realtime STT adapter — LiveKit stt.STT subclass (ch.12)."""

from __future__ import annotations

import asyncio
import audioop
import collections
import contextlib
import json
import logging
import math
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

import websockets
from livekit import rtc
from livekit.agents import (
    APIConnectionError,
    APIConnectOptions,
    stt,
)
from livekit.agents.types import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    NotGivenOr,
    TimedString,
)
from websockets.asyncio.client import ClientConnection

from services.agent.src.observability.metrics import GLOBAL_METRICS, MetricsRegistry
from services.agent.src.orchestration.stable_prefix import StablePrefixTracker
from services.agent.src.providers.funasr_protocol import (
    FunASRSentence,
    FunASRServerEvent,
    build_continue_task_context,
    build_finish_task,
    build_run_task,
    conversation_item_to_funasr_context,
    parse_server_message,
    result_trace_metrics,
    sentence_to_asr_result,
)
from services.agent.src.providers.reliability import CircuitBreaker
from services.agent.src.voice_core.speech_timeline import ASRResult

logger = logging.getLogger(__name__)


@dataclass
class FunASRConfig:
    api_key: str
    ws_url: str
    model: str = "fun-asr-realtime"
    sample_rate: int = 16000
    language: str = "zh"
    chunk_ms: int = 80
    max_sentence_silence_ms: int = 550
    semantic_punctuation: bool = False
    heartbeat: bool = True
    reconnect_audio_ms: int = 400
    connect_timeout_s: float = 5.0
    result_timeout_s: float = 8.0
    conversation_context_enabled: bool = False
    vocabulary_id: str | None = None
    speech_noise_threshold: float | None = None

    def __post_init__(self) -> None:
        threshold = self.speech_noise_threshold
        if threshold is not None and (not math.isfinite(threshold) or not -1.0 <= threshold <= 1.0):
            raise ValueError("FunASR speech noise threshold must be between -1 and 1")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> FunASRConfig:
        import os

        e = env or dict(os.environ)
        vocabulary_id = e.get("FUNASR_VOCABULARY_ID", "").strip() or None
        threshold_raw = e.get("FUNASR_SPEECH_NOISE_THRESHOLD", "").strip()
        return cls(
            api_key=e.get("DASHSCOPE_API_KEY", ""),
            ws_url=e.get("FUNASR_MOCK_WS_URL") or e.get("DASHSCOPE_WS_URL", ""),
            model=e.get("FUNASR_MODEL", "fun-asr-realtime"),
            sample_rate=int(e.get("FUNASR_SAMPLE_RATE", "16000")),
            language=e.get("FUNASR_LANGUAGE", "zh"),
            chunk_ms=int(e.get("FUNASR_CHUNK_MS", "80")),
            max_sentence_silence_ms=int(e.get("FUNASR_MAX_SENTENCE_SILENCE_MS", "550")),
            semantic_punctuation=e.get("FUNASR_SEMANTIC_PUNCTUATION", "false").lower() == "true",
            heartbeat=e.get("FUNASR_HEARTBEAT", "true").lower() == "true",
            reconnect_audio_ms=int(e.get("FUNASR_RECONNECT_AUDIO_MS", "400")),
            connect_timeout_s=float(e.get("FUNASR_CONNECT_TIMEOUT_S", "5")),
            result_timeout_s=float(e.get("FUNASR_RESULT_TIMEOUT_S", "8")),
            conversation_context_enabled=(
                e.get("FUNASR_CONTEXT_ENABLED", "false").lower() == "true"
            ),
            vocabulary_id=vocabulary_id,
            speech_noise_threshold=(float(threshold_raw) if threshold_raw else None),
        )


class FunASRSession:
    """Standalone streaming FunASR session (mock or real WS). Used by offline path & stream."""

    def __init__(
        self,
        config: FunASRConfig,
        *,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self.config = config
        self.metrics = metrics
        self.task_id: str | None = None
        self._ws: ClientConnection | None = None
        self._started = asyncio.Event()
        self._closed = False
        self._failed = False
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self._recv_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._reconnect_lock = asyncio.Lock()
        self._task_rotation_lock = asyncio.Lock()
        self._breaker = CircuitBreaker()
        self._finishing = False
        self._terminal_finishing = False
        self._rotation_pending = False
        self._task_started_event = asyncio.Event()
        self._task_finished_received = asyncio.Event()
        self._task_finished_consumed = asyncio.Event()
        self._finished_task_id: str | None = None
        self._pcm_ring: collections.deque[bytes] = collections.deque()
        self._pcm_ring_ranges: collections.deque[tuple[int, int]] = collections.deque()
        self._pcm_ring_bytes = 0
        self._max_ring_bytes = int(config.sample_rate * 2 * config.reconnect_audio_ms / 1000)
        self._context: tuple[dict[str, object], ...] = ()
        self._task_epoch = 0
        self._segment_epoch = 1
        self._task_sample_origin = 0
        self._last_sent_sample = 0
        self._last_provider_acked_sample = 0
        self._last_committed_sample = 0
        self._last_emitted_final_sample = 0
        self._metrics_ws_active = False

    def _mark_ws_connected(self) -> None:
        if self.metrics is not None and not self._metrics_ws_active:
            self.metrics.add_provider_ws_active("asr", 1)
            self._metrics_ws_active = True

    def _mark_ws_disconnected(self) -> None:
        if self.metrics is not None and self._metrics_ws_active:
            self.metrics.add_provider_ws_active("asr", -1)
            self._metrics_ws_active = False

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def task_epoch(self) -> int:
        return self._task_epoch

    @property
    def segment_epoch(self) -> int:
        return self._segment_epoch

    @property
    def finishing(self) -> bool:
        return self._finishing

    @property
    def rotation_pending(self) -> bool:
        return self._rotation_pending

    @property
    def task_sample_origin(self) -> int:
        return self._task_sample_origin

    @property
    def last_sent_sample(self) -> int:
        return self._last_sent_sample

    @property
    def last_provider_acked_sample(self) -> int:
        return self._last_provider_acked_sample

    @property
    def last_committed_sample(self) -> int:
        return self._last_committed_sample

    @property
    def last_emitted_final_sample(self) -> int:
        return self._last_emitted_final_sample

    def mark_committed_sample(self, sample: int) -> None:
        if sample < 0:
            raise ValueError("committed sample must be non-negative")
        self._last_committed_sample = max(self._last_committed_sample, sample)

    def replay_start_sample(self) -> int:
        """Return the earliest sample worth replaying after reconnect."""

        window = max(1, self.config.sample_rate * self.config.reconnect_audio_ms // 1000)
        return max(
            self._task_sample_origin,
            self._last_provider_acked_sample,
            self._last_committed_sample,
            self._last_sent_sample - window,
        )

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
        self._failed = False
        self._finishing = False
        self._terminal_finishing = False
        self._rotation_pending = False
        self._task_started_event.set()
        self._task_finished_received.clear()
        self._task_finished_consumed.clear()
        self._finished_task_id = None
        self._started.set()
        self._ready.set()
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
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=self.config.connect_timeout_s)
                if isinstance(message, bytes):
                    continue
                ev = parse_server_message(message)
                startup_events.append(ev)
                if ev.event == "task-failed":
                    raise APIConnectionError(ev.error_message or "funasr task-failed")
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
        raise APIConnectionError(f"FunASR connect failed: {last_error}") from last_error

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
                    if ev.task_id and ev.task_id == self._finished_task_id:
                        logger.info(
                            "late FunASR event ignored after task boundary event=%s task_id=%s",
                            ev.event,
                            ev.task_id,
                        )
                        continue
                    if ev.task_id and self.task_id and ev.task_id != self.task_id:
                        logger.info(
                            "stale FunASR task event ignored event=%s task_id=%s current_task_id=%s",
                            ev.event,
                            ev.task_id,
                            self.task_id,
                        )
                        continue
                    if ev.event == "task-failed":
                        self._failed = True
                        self._breaker.record_failure()
                    await self.events.put(ev)
                    if ev.event == "task-started" and ev.task_id == self.task_id:
                        self._failed = False
                        self._ready.set()
                        self._task_started_event.set()
                    if ev.event == "result-generated" and ev.sentence is not None:
                        end_ms = ev.sentence.end_ms or ev.sentence.begin_ms
                        ack = max(
                            0,
                            self._task_sample_origin
                            + round(end_ms * self.config.sample_rate / 1000),
                        )
                        self._last_provider_acked_sample = max(
                            self._last_provider_acked_sample,
                            min(ack, self._last_sent_sample),
                        )
                        if ev.sentence.sentence_end:
                            self._last_emitted_final_sample = max(
                                self._last_emitted_final_sample,
                                min(ack, self._last_sent_sample),
                            )
                    if ev.event == "task-finished" and ev.task_id == self.task_id:
                        self._finished_task_id = ev.task_id
                        self._ready.clear()
                        self._task_finished_received.set()
                        if self._rotation_pending:
                            continue
                        return
                    if ev.event == "task-failed":
                        self._task_started_event.set()
                        self._mark_ws_disconnected()
                        self._ready.clear()
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
                if not await self._recover(ws):
                    self._failed = True
                    await self.events.put(
                        FunASRServerEvent(
                            event="task-failed",
                            task_id=self.task_id or "",
                            error_message="FunASR reconnect failed",
                        )
                    )
                    return

    async def _recover(self, failed_ws: ClientConnection) -> bool:
        async with self._reconnect_lock:
            if self._closed:
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
                return False
            try:
                self._ws = ws
                self.task_id = task_id
                self._task_epoch += 1
                self._task_sample_origin = replay_start
                self._failed = False
                self._task_started_event.set()
                self._task_finished_received.clear()
                self._task_finished_consumed.clear()
                self._finished_task_id = None
                self._mark_ws_connected()
                for ev in startup_events:
                    await self.events.put(ev)
                if asyncio.get_running_loop().time() - started_at <= 2.0:
                    replay = self._replay_pcm()
                    if replay:
                        await ws.send(replay)
                if self._finishing:
                    await ws.send(json.dumps(build_finish_task(task_id), ensure_ascii=False))
            except Exception:
                self._failed = True
                self._mark_ws_disconnected()
                with contextlib.suppress(Exception):
                    await ws.close()
                self._breaker.record_failure()
                return False
            self._breaker.record_success()
            self._ready.set()
            return True

    def _push_ring(self, pcm: bytes) -> None:
        start = self._last_sent_sample
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

    async def send_pcm(
        self,
        pcm: bytes,
        *,
        replayed: bool = False,
        capture_start_sample: int | None = None,
    ) -> None:
        if self._closed or self._ws is None:
            raise RuntimeError("FunASR session not connected")
        if not pcm or len(pcm) % 2:
            raise ValueError("FunASR PCM must be non-empty 16-bit samples")
        if not replayed:
            start = self._last_sent_sample if capture_start_sample is None else capture_start_sample
            if start < self._last_sent_sample:
                raise ValueError("capture sample position moved backwards")
            if start > self._last_sent_sample:
                # A capture discontinuity must not make replay bytes appear to
                # belong to the missing interval.
                self._pcm_ring.clear()
                self._pcm_ring_ranges.clear()
                self._pcm_ring_bytes = 0
            self._last_sent_sample = start
            self._push_ring(pcm)
            self._last_sent_sample += len(pcm) // 2
        await self._wait_until_ready("send PCM")
        ws = self._ws
        if ws is None:
            raise RuntimeError("FunASR session not connected")
        started = monotonic()
        try:
            await ws.send(pcm)
        except Exception:
            if not await self._recover(ws):
                raise APIConnectionError("FunASR reconnect failed") from None
        if self.metrics is not None:
            self.metrics.set_media_metric(
                "asr_send_lag_ms",
                (monotonic() - started) * 1000.0,
            )

    async def update_context(self, context: tuple[dict[str, object], ...]) -> None:
        self._context = context
        if self._ws is None or self.task_id is None:
            return
        await self._wait_until_ready("update context")
        msg = build_continue_task_context(self.task_id, list(context))
        await self._ws.send(json.dumps(msg, ensure_ascii=False))

    async def finish(self, *, terminal: bool = True) -> None:
        if self._finishing or self._ws is None or self.task_id is None:
            return
        self._finishing = True
        self._terminal_finishing = terminal
        self._task_finished_received.clear()
        self._task_finished_consumed.clear()
        self._finished_task_id = None
        await self._wait_until_ready("finish task")
        ws = self._ws
        assert ws is not None
        try:
            await ws.send(json.dumps(build_finish_task(self.task_id), ensure_ascii=False))
        except Exception:
            if not await self._recover(ws):
                raise APIConnectionError("FunASR reconnect failed while finishing") from None

    async def wait_for_task_finished(self, *, require_consumed: bool = False) -> None:
        """Wait for one provider task boundary; heartbeat packets cannot extend it."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.result_timeout_s
        try:
            await asyncio.wait_for(
                self._task_finished_received.wait(),
                timeout=max(0.001, deadline - loop.time()),
            )
            if require_consumed:
                await asyncio.wait_for(
                    self._task_finished_consumed.wait(),
                    timeout=max(0.001, deadline - loop.time()),
                )
        except TimeoutError as exc:
            raise APIConnectionError(
                f"FunASR task boundary timed out after {self.config.result_timeout_s:.3f}s"
            ) from exc

    def acknowledge_task_finished(self, task_id: str) -> None:
        if task_id and task_id == self.task_id:
            self._task_finished_consumed.set()

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        """Finish one VAD segment and reuse the same WebSocket for the next task."""

        async with self._task_rotation_lock:
            if self._closed or self._ws is None or self.task_id is None:
                raise APIConnectionError("FunASR task rotation requires an active session")
            if self._finishing:
                raise APIConnectionError("FunASR task rotation already in progress")
            self._rotation_pending = True
            try:
                await self.finish(terminal=False)
                await self.wait_for_task_finished(require_consumed=require_consumed)
                await self._start_reused_task()
            except BaseException:
                self._rotation_pending = False
                raise

    async def _start_reused_task(self) -> None:
        ws = self._ws
        if ws is None:
            raise APIConnectionError("FunASR WebSocket is unavailable for task reuse")

        run = self._build_run_task()
        task_id = str(run["header"]["task_id"])
        next_origin = self._last_sent_sample
        self._task_started_event.clear()
        self._task_finished_received.clear()
        self._task_finished_consumed.clear()
        self.task_id = task_id
        self._task_epoch += 1
        self._segment_epoch += 1
        self._task_sample_origin = next_origin
        self._finishing = False
        self._terminal_finishing = False
        self._ready.clear()
        self._pcm_ring.clear()
        self._pcm_ring_ranges.clear()
        self._pcm_ring_bytes = 0
        try:
            await ws.send(json.dumps(run, ensure_ascii=False))
            await asyncio.wait_for(
                self._task_started_event.wait(),
                timeout=self.config.connect_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise APIConnectionError(f"FunASR reused task failed to start: {exc}") from exc
        if self._failed or not self._ready.is_set():
            raise APIConnectionError("FunASR reused task was rejected")
        self._rotation_pending = False

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
        if self._failed or self._ws is None:
            raise APIConnectionError(f"FunASR cannot {operation}: session unavailable")

    async def aclose(self) -> None:
        self._closed = True
        self._mark_ws_disconnected()
        self._ready.clear()
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


def resample_pcm_16le(pcm: bytes, *, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return pcm
    return audioop.ratecv(pcm, 2, 1, src_rate, dst_rate, None)[0]


def _speech_data_from_sentence(
    *,
    text: str,
    language: str,
    begin_ms: int,
    end_ms: int | None,
    words: tuple[Any, ...],
    confidence: float | None = None,
) -> stt.SpeechData:
    timed: list[TimedString] = []
    for w in words:
        w_text = getattr(w, "text", "") + (getattr(w, "punctuation", "") or "")
        timed.append(
            TimedString(
                w_text,
                start_time=getattr(w, "begin_ms", 0) / 1000.0,
                end_time=getattr(w, "end_ms", 0) / 1000.0,
            )
        )
    from livekit.agents.language import LanguageCode

    return stt.SpeechData(
        language=LanguageCode(language),
        text=text,
        start_time=begin_ms / 1000.0,
        end_time=(end_ms or begin_ms) / 1000.0,
        confidence=confidence or 0.0,
        words=timed or None,
    )


class FunASRRecognizeStream(stt.RecognizeStream):
    """LiveKit RecognizeStream that drives FunASRSession over WebSocket."""

    def __init__(
        self,
        *,
        stt_instance: FunASRSTT,
        config: FunASRConfig,
        conn_options: APIConnectOptions,
        sample_rate: int = 16000,
        initial_context: tuple[dict[str, object], ...] = (),
    ) -> None:
        super().__init__(
            stt=stt_instance,
            conn_options=conn_options,
            sample_rate=sample_rate,
        )
        self._config = config
        self._stt_instance = stt_instance
        self._pending_context = tuple(initial_context)
        self._context_updates: asyncio.Queue[tuple[dict[str, object], ...]] = asyncio.Queue(1)
        self._session: FunASRSession | None = None
        self._prefix_tracker = StablePrefixTracker()
        self._speaking = False
        # Sentence IDs are stable across a FunASR reconnect.  Keying by
        # provider task would let a replayed final cross the task boundary.
        self._final_sentence_ids: set[tuple[int, str]] = set()
        self._sentence_revisions: dict[tuple[int, str], int] = {}
        self._provider_task_id = ""
        self._provider_task_epoch = 0
        self._asr_results: deque[ASRResult] = deque(maxlen=64)
        self._stream_epoch = 1
        self._last_emitted_final_sample = 0

    @property
    def asr_results(self) -> tuple[ASRResult, ...]:
        """Recent range-stamped results for a Voice Core/timeline adapter."""

        return tuple(self._asr_results)

    def mark_committed_sample(self, sample: int) -> None:
        if self._session is not None:
            self._session.mark_committed_sample(sample)

    def set_stream_epoch(self, stream_epoch: int) -> None:
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        self._stream_epoch = stream_epoch

    def update_context(self, context: tuple[dict[str, object], ...]) -> None:
        self._pending_context = tuple(context)
        if self._context_updates.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._context_updates.get_nowait()
        self._context_updates.put_nowait(self._pending_context)

    async def _run(self) -> None:
        session = FunASRSession(
            self._config,
            metrics=getattr(self._stt_instance, "metrics", None),
        )
        session._context = self._pending_context
        self._session = session
        try:
            await session.connect()
        except Exception as exc:
            raise APIConnectionError(f"FunASR connect failed: {exc}") from exc

        send_task = asyncio.create_task(self._send_audio(session), name="funasr-send")
        recv_task = asyncio.create_task(self._recv_events(session), name="funasr-recv")
        ctx_task = asyncio.create_task(self._context_loop(session), name="funasr-ctx")
        try:
            # Must not cancel recv when send completes first — finals/task-finished
            # arrive after finish-task is sent. Run both to completion.
            # A provider-boundary timeout must cancel the peer task promptly;
            # return_exceptions=True would wait forever on a heartbeat-only
            # receive loop after the send side has already failed.
            await asyncio.gather(send_task, recv_task)
        finally:
            for t in (send_task, recv_task, ctx_task):
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
            if session.failed:
                await session.aclose()
            else:
                with contextlib.suppress(Exception):
                    await session.finish()
                await session.aclose()
            self._session = None

    async def _context_loop(self, session: FunASRSession) -> None:
        while True:
            ctx = await self._context_updates.get()
            with contextlib.suppress(Exception):
                await session.update_context(ctx)

    async def _send_audio(self, session: FunASRSession) -> None:
        samples_per_chunk = max(1, self._config.sample_rate * self._config.chunk_ms // 1000)
        byte_stream = bytearray()
        capture_sample = 0
        async for data in self._input_ch:
            if isinstance(data, rtc.AudioFrame):
                pcm = bytes(data.data)
                if data.sample_rate != self._config.sample_rate:
                    pcm = resample_pcm_16le(
                        pcm, src_rate=data.sample_rate, dst_rate=self._config.sample_rate
                    )
                byte_stream.extend(pcm)
                chunk_bytes = samples_per_chunk * 2
                while len(byte_stream) >= chunk_bytes:
                    chunk = bytes(byte_stream[:chunk_bytes])
                    del byte_stream[:chunk_bytes]
                    self._stt_instance.observe_pcm(chunk)
                    await session.send_pcm(chunk, capture_start_sample=capture_sample)
                    capture_sample += len(chunk) // 2
            elif isinstance(data, self._FlushSentinel):
                if byte_stream:
                    remaining = bytes(byte_stream)
                    self._stt_instance.observe_pcm(remaining)
                    await session.send_pcm(remaining, capture_start_sample=capture_sample)
                    capture_sample += len(remaining) // 2
                    byte_stream.clear()
                # end_input() flushes and closes the channel; that is the
                # terminal task boundary handled below. A live flush comes
                # from the authoritative VAD endpoint and rotates to a fresh
                # provider task on the same WebSocket.
                if not self._input_ch.closed:
                    await session.rotate_task()
        if byte_stream:
            remaining = bytes(byte_stream)
            self._stt_instance.observe_pcm(remaining)
            await session.send_pcm(remaining, capture_start_sample=capture_sample)
        await session.finish()
        await session.wait_for_task_finished()

    async def _recv_events(self, session: FunASRSession) -> None:
        idle_timeouts = 0
        max_idle = 3
        while True:
            try:
                ev = await asyncio.wait_for(
                    session.events.get(),
                    timeout=self._config.result_timeout_s,
                )
                idle_timeouts = 0
            except TimeoutError:
                if not session.finishing:
                    # A long-lived listening session is expected to be quiet.
                    # Only an explicit task boundary has a result deadline.
                    continue
                idle_timeouts += 1
                if idle_timeouts >= max_idle:
                    # Provider went silent after finish; end stream cleanly.
                    if self._speaking:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                        )
                        self._speaking = False
                    return
                continue
            if ev.event == "task-failed":
                raise APIConnectionError(ev.error_message or "funasr task-failed")
            if ev.event == "task-finished":
                session.acknowledge_task_finished(ev.task_id)
                if self._speaking:
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                    )
                    self._speaking = False
                if session.rotation_pending:
                    continue
                if not session.finishing:
                    raise APIConnectionError("FunASR task finished without a client boundary")
                return
            if ev.event != "result-generated" or ev.sentence is None:
                continue
            sent = ev.sentence
            if sent.heartbeat and not sent.text:
                continue
            if ev.task_id and ev.task_id != self._provider_task_id:
                self._provider_task_id = ev.task_id
                self._provider_task_epoch += 1
            task_epoch = max(1, session.task_epoch, self._provider_task_epoch)
            sentence_id = str(sent.sentence_id)
            sentence_key = (session.segment_epoch, sentence_id)
            revision = self._sentence_revisions.get(sentence_key, 0) + 1
            self._sentence_revisions[sentence_key] = revision
            asr_result = sentence_to_asr_result(
                sent,
                task_epoch=task_epoch,
                sample_rate=self._config.sample_rate,
                revision=revision,
                stream_epoch=self._stream_epoch,
                sample_offset=session.task_sample_origin,
            )
            if asr_result.capture_end_sample <= session.last_committed_sample:
                logger.info(
                    "late FunASR result ignored task_epoch=%s sentence_id=%s",
                    task_epoch,
                    sent.sentence_id,
                )
                continue
            if (
                sent.sentence_end
                and asr_result.capture_end_sample <= self._last_emitted_final_sample
            ):
                logger.info(
                    "duplicate FunASR final behind sample watermark ignored sentence_id=%s",
                    sent.sentence_id,
                )
                continue
            if (
                sent.sentence_end
                and sent.sentence_id > 0
                and sentence_key in self._final_sentence_ids
            ):
                logger.info(
                    "duplicate FunASR final ignored segment_epoch=%s sentence_id=%s",
                    session.segment_epoch,
                    sent.sentence_id,
                )
                continue
            self._asr_results.append(asr_result)
            if not self._speaking and sent.text:
                self._speaking = True
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.START_OF_SPEECH)
                )
            sd = _speech_data_from_sentence(
                text=sent.text,
                language=self._config.language,
                begin_ms=sent.begin_ms,
                end_ms=sent.end_ms,
                words=sent.words,
            )
            request_id = session.task_id or ""
            if sent.sentence_end:
                if sent.sentence_id > 0:
                    self._final_sentence_ids.add(sentence_key)
                self._last_emitted_final_sample = max(
                    self._last_emitted_final_sample,
                    asr_result.capture_end_sample,
                )
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        request_id=request_id,
                        alternatives=[sd],
                    )
                )
                self._stt_instance.trace_result(
                    sent,
                    task_epoch=max(1, self._provider_task_epoch),
                )
                self._prefix_tracker.on_final(sent.sentence_id)
            else:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.INTERIM_TRANSCRIPT,
                        request_id=request_id,
                        alternatives=[sd],
                    )
                )
                preflight = self._prefix_tracker.observe(sent.sentence_id, sent.text)
                if preflight:
                    pre_sd = _speech_data_from_sentence(
                        text=preflight,
                        language=self._config.language,
                        begin_ms=sent.begin_ms,
                        end_ms=sent.end_ms,
                        words=(),
                    )
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(
                            type=stt.SpeechEventType.PREFLIGHT_TRANSCRIPT,
                            request_id=request_id,
                            alternatives=[pre_sd],
                        )
                    )


class FunASRSTT(stt.STT[Any]):
    """LiveKit STT plugin for Alibaba FunASR Realtime."""

    def __init__(
        self,
        config: FunASRConfig,
        *,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=True,
                interim_results=True,
                diarization=False,
                aligned_transcript="word",
                offline_recognize=False,
                keyterms=False,
                chat_context=config.conversation_context_enabled,
            )
        )
        self._config = config
        self.metrics = metrics
        self._context_items: deque[dict[str, object]] = deque(maxlen=10)
        self._streams: weakref.WeakSet[FunASRRecognizeStream] = weakref.WeakSet()
        self._pcm_observer: Callable[[bytes], None] | None = None
        self._trace_callback: Callable[[str, str, dict[str, int]], None] | None = None

    @classmethod
    def from_env(cls) -> FunASRSTT:
        return cls(FunASRConfig.from_env(), metrics=GLOBAL_METRICS)

    @property
    def provider(self) -> str:
        return "alibaba_model_studio"

    @property
    def model(self) -> str:
        return self._config.model

    async def _recognize_impl(
        self,
        buffer: Any,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        # Streaming-only; implement abstract method for base class contract.
        _ = buffer, language, conn_options
        raise NotImplementedError("FunASRSTT is streaming-only; call stream()")

    def _push_conversation_item(self, ev: Any) -> None:
        if not self._config.conversation_context_enabled:
            return
        item: dict[str, Any]
        if isinstance(ev, dict):
            item = ev
        else:
            role = getattr(getattr(ev, "item", None), "role", None) or getattr(ev, "role", "")
            text = getattr(getattr(ev, "item", None), "text_content", None) or getattr(
                ev, "text", ""
            )
            if callable(text):
                text = text()
            item = {"role": str(role or ""), "text": str(text or "")}
        mapped = conversation_item_to_funasr_context(item)
        if mapped is not None:
            self._context_items.append(mapped)
        for stream in tuple(self._streams):
            stream.update_context(tuple(self._context_items))

    def push_conversation_item(self, item: dict[str, Any]) -> None:
        """Public helper used by unit tests / offline path."""
        self._push_conversation_item(item)

    def set_pcm_observer(self, observer: Callable[[bytes], None] | None) -> None:
        self._pcm_observer = observer

    def set_trace_callback(
        self,
        callback: Callable[[str, str, dict[str, int]], None] | None,
    ) -> None:
        self._trace_callback = callback

    def flush_speech_segment(self, _binding: object = None) -> None:
        """Queue one VAD-authoritative task boundary on every active stream."""

        flushed = 0
        for stream in tuple(self._streams):
            try:
                stream.flush()
                flushed += 1
            except RuntimeError:
                # A stream can disappear between the WeakSet snapshot and the
                # synchronous flush. The next stream starts with a clean task.
                continue
        callback = self._trace_callback
        if callback is not None:
            callback(
                "funasr_segment_flush",
                "ok" if flushed else "ignored",
                {"active_streams": flushed},
            )

    def trace_result(self, sentence: FunASRSentence, *, task_epoch: int) -> None:
        callback = self._trace_callback
        if callback is None:
            return
        callback(
            "funasr_final",
            "ok",
            result_trace_metrics(sentence, task_epoch=task_epoch),
        )

    def observe_pcm(self, pcm: bytes) -> None:
        if self._pcm_observer is None:
            return
        try:
            self._pcm_observer(pcm)
        except Exception:
            logger.warning("non-blocking PCM observer failed", exc_info=True)

    def stream(
        self,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> FunASRRecognizeStream:
        _ = language
        stream = FunASRRecognizeStream(
            stt_instance=self,
            config=self._config,
            conn_options=conn_options,
            sample_rate=self._config.sample_rate,
            initial_context=tuple(self._context_items),
        )
        self._streams.add(stream)
        return stream

    def create_session(self) -> FunASRSession:
        session = FunASRSession(self._config, metrics=self.metrics)
        session._context = tuple(self._context_items)
        return session

    async def aclose(self) -> None:
        await asyncio.gather(
            *(stream.aclose() for stream in tuple(self._streams)),
            return_exceptions=True,
        )
