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
    sanitize_diagnostic_text,
    sanitize_error_code,
    sanitize_error_message,
    sentence_to_asr_result,
)
from services.agent.src.providers.reliability import CircuitBreaker
from services.agent.src.providers.sensevoice import (
    SenseVoiceRescue,
    SenseVoiceRescueConfig,
)
from services.agent.src.voice_core.speech_timeline import ASRResult

logger = logging.getLogger(__name__)

_WS_TRACE_MAX_PER_WINDOW = 20


def _is_empty_audio_error(error_code: object) -> bool:
    """Return whether FunASR rejected one task because it found no usable audio."""

    return str(error_code or "").strip().casefold() == "emptyaudio"


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
    post_finish_tail_grace_s: float = 0.25
    conversation_context_enabled: bool = False
    vocabulary_id: str | None = None
    speech_noise_threshold: float | None = None
    ws_trace: bool = False
    rescue_config: SenseVoiceRescueConfig | None = None

    def __post_init__(self) -> None:
        threshold = self.speech_noise_threshold
        if threshold is not None and (not math.isfinite(threshold) or not -1.0 <= threshold <= 1.0):
            raise ValueError("FunASR speech noise threshold must be between -1 and 1")
        if (
            not math.isfinite(self.post_finish_tail_grace_s)
            or not 0.0 <= self.post_finish_tail_grace_s <= 2.0
        ):
            raise ValueError("FunASR post-finish tail grace must be between 0 and 2 seconds")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> FunASRConfig:
        import os

        e = env or dict(os.environ)
        vocabulary_id = e.get("FUNASR_VOCABULARY_ID", "").strip() or None
        threshold_raw = e.get("FUNASR_SPEECH_NOISE_THRESHOLD", "").strip()
        rescue_url = e.get("SENSEVOICE_URL", "").strip()
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
            post_finish_tail_grace_s=float(
                e.get("FUNASR_POST_FINISH_TAIL_GRACE_S", "0.25")
            ),
            conversation_context_enabled=(
                e.get("FUNASR_CONTEXT_ENABLED", "false").lower() == "true"
            ),
            vocabulary_id=vocabulary_id,
            speech_noise_threshold=(float(threshold_raw) if threshold_raw else None),
            ws_trace=e.get("FUNASR_WS_TRACE", "false").lower() == "true",
            rescue_config=(
                SenseVoiceRescueConfig.from_env(e) if rescue_url else None
            ),
        )


@dataclass(frozen=True, slots=True)
class FunASRTaskFailure:
    task_id: str
    task_epoch: int
    segment_epoch: int
    error_code: str | None
    error_message: str


@dataclass(frozen=True, slots=True)
class FunASRTaskEventContext:
    """Immutable sample-clock context for one provider task."""

    task_epoch: int
    segment_epoch: int
    sample_origin: int
    audio_end_sample: int | None
    boundary_observed: bool


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
        self._idle_terminal_event = asyncio.Event()
        self._finished_task_id: str | None = None
        self._task_failed_event = asyncio.Event()
        self._replaceable_provider_failure = False
        self._last_task_failure: FunASRTaskFailure | None = None
        self._pcm_ring: collections.deque[bytes] = collections.deque()
        self._pcm_ring_ranges: collections.deque[tuple[int, int]] = collections.deque()
        self._pcm_ring_bytes = 0
        self._max_ring_bytes = int(config.sample_rate * 2 * config.reconnect_audio_ms / 1000)
        # SenseVoice rescue evidence, scoped to one VAD segment (between two
        # rotate_task calls).  Unlike the reconnect ring this buffer keeps the
        # whole segment audio so an empty turn can still be transcribed
        # offline.  Task replacements inside a segment never clear it.
        rescue_config = config.rescue_config
        self._rescue = SenseVoiceRescue(rescue_config) if rescue_config is not None else None
        self._max_segment_samples = (
            int(rescue_config.max_audio_s * config.sample_rate)
            if rescue_config is not None
            else 0
        )
        self._segment_pcm: collections.deque[bytes] = collections.deque()
        self._segment_pcm_ranges: collections.deque[tuple[int, int]] = collections.deque()
        self._segment_pcm_samples = 0
        self._segment_pcm_start_sample: int | None = None
        self._segment_pcm_end_sample: int | None = None
        self._segment_pcm_gaps = 0
        self._segment_nonempty_final_seen = False
        self._rescue_deadline: float | None = None
        self._context: tuple[dict[str, object], ...] = ()
        self._task_epoch = 0
        self._segment_epoch = 1
        self._task_sample_origin = 0
        self._task_audio_start_sample: int | None = None
        self._task_audio_end_sample: int | None = None
        self._task_audio_send_count = 0
        self._task_pcm_sample_count = 0
        self._task_pcm_peak_abs = 0
        self._task_pcm_square_sum = 0.0
        self._last_sent_sample = 0
        self._last_rotation_sample = 0
        self._last_provider_acked_sample = 0
        self._last_committed_sample = 0
        self._last_emitted_final_sample = 0
        self._task_event_contexts: dict[str, FunASRTaskEventContext] = {}
        self._task_event_order: deque[str] = deque()
        self._metrics_ws_active = False
        # Env-gated diagnostics: rate-limited visibility into every control
        # message crossing the provider WebSocket.  Strictly observational.
        self._ws_trace_enabled = bool(config.ws_trace)
        self._trace_window_start = 0.0
        self._trace_window_count = 0
        self._trace_suppressed_total = 0

    def _trace_ws(self, message: str) -> None:
        if not self._ws_trace_enabled:
            return
        now = monotonic()
        if now - self._trace_window_start >= 1.0:
            if self._trace_suppressed_total:
                logger.info(
                    "funasr_ws_trace suppressed_total=%s task_id=%s",
                    self._trace_suppressed_total,
                    self.task_id or "unknown",
                )
                self._trace_suppressed_total = 0
            self._trace_window_start = now
            self._trace_window_count = 0
        if self._trace_window_count >= _WS_TRACE_MAX_PER_WINDOW:
            self._trace_suppressed_total += 1
            return
        self._trace_window_count += 1
        logger.info("funasr_ws_trace %s", message)

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
    def last_task_failure(self) -> FunASRTaskFailure | None:
        return self._last_task_failure

    def _failure_matches_current_task(self) -> bool:
        failure = self._last_task_failure
        if failure is None:
            return False
        return failure.task_epoch == max(1, self._task_epoch) and (
            not self.task_id or failure.task_id == self.task_id
        )

    def _has_replaceable_empty_audio_failure(self) -> bool:
        failure = self._last_task_failure
        return bool(
            self._replaceable_provider_failure
            and failure is not None
            and _is_empty_audio_error(failure.error_code)
            and self._failure_matches_current_task()
        )

    def _record_task_failure(
        self,
        *,
        task_id: str | None = None,
        task_epoch: int | None = None,
        segment_epoch: int | None = None,
        error_code: Any = None,
        error_message: Any = None,
    ) -> FunASRTaskFailure:
        failure = FunASRTaskFailure(
            task_id=sanitize_diagnostic_text(
                str(task_id or self.task_id or "unknown"),
                fallback="unknown",
                limit=128,
            ),
            task_epoch=max(1, task_epoch if task_epoch is not None else self._task_epoch),
            segment_epoch=max(
                1,
                segment_epoch if segment_epoch is not None else self._segment_epoch,
            ),
            error_code=sanitize_error_code(error_code),
            error_message=sanitize_error_message(
                error_message,
                fallback="FunASR task failed",
            ),
        )
        self._last_task_failure = failure
        logger.warning(
            "FunASR task failure task_id=%s task_epoch=%s segment_epoch=%s "
            "error_code=%s error_message=%s",
            failure.task_id,
            failure.task_epoch,
            failure.segment_epoch,
            failure.error_code or "unknown",
            failure.error_message,
        )
        return failure

    def _failure_summary(self, prefix: str) -> str:
        failure = self._last_task_failure
        if failure is None:
            return (
                f"{prefix} task_id={self.task_id or 'unknown'} "
                f"task_epoch={max(1, self._task_epoch)} "
                f"segment_epoch={max(1, self._segment_epoch)} "
                "error_code=unknown error_message=FunASR task failed"
            )
        return (
            f"{prefix} task_id={failure.task_id} "
            f"task_epoch={failure.task_epoch} "
            f"segment_epoch={failure.segment_epoch} "
            f"error_code={failure.error_code or 'unknown'} "
            f"error_message={failure.error_message}"
        )

    def _failure_exception(self, prefix: str) -> APIConnectionError:
        return APIConnectionError(self._failure_summary(prefix))

    def _clear_pcm_ring(self) -> None:
        self._pcm_ring.clear()
        self._pcm_ring_ranges.clear()
        self._pcm_ring_bytes = 0

    def _reset_segment_evidence(self) -> None:
        """Drop the current VAD segment's rescue evidence (segment boundary)."""

        self._segment_pcm.clear()
        self._segment_pcm_ranges.clear()
        self._segment_pcm_samples = 0
        self._segment_pcm_start_sample = None
        self._segment_pcm_end_sample = None
        self._segment_pcm_gaps = 0
        self._segment_nonempty_final_seen = False

    def _remember_segment_pcm(self, pcm: bytes, *, start_sample: int, end_sample: int) -> None:
        if self._segment_pcm_start_sample is None:
            self._segment_pcm_start_sample = start_sample
        elif start_sample > (self._segment_pcm_end_sample or start_sample):
            # A capture discontinuity inside the segment; keep the bytes but
            # remember that the offline transcript may span a gap.
            self._segment_pcm_gaps += 1
        self._segment_pcm.append(pcm)
        self._segment_pcm_ranges.append((start_sample, end_sample))
        self._segment_pcm_samples += len(pcm) // 2
        self._segment_pcm_end_sample = max(self._segment_pcm_end_sample or 0, end_sample)
        while self._segment_pcm and self._segment_pcm_samples > self._max_segment_samples:
            # Keep the most recent tail: an over-long empty segment is rescued
            # with its newest audio, matching what the provider dropped last.
            dropped = self._segment_pcm.popleft()
            _, drop_end = self._segment_pcm_ranges.popleft()
            self._segment_pcm_samples -= len(dropped) // 2
            self._segment_pcm_start_sample = drop_end

    def pending_rescue_deadline(self) -> float | None:
        """Loop-time deadline of an in-flight offline segment rescue, if any."""

        return self._rescue_deadline

    def _remember_current_task_event_context(self) -> None:
        task_id = self.task_id or ""
        if not task_id:
            return
        if task_id not in self._task_event_contexts:
            self._task_event_order.append(task_id)
        self._task_event_contexts[task_id] = FunASRTaskEventContext(
            task_epoch=max(1, self._task_epoch),
            segment_epoch=max(1, self._segment_epoch),
            sample_origin=self._task_sample_origin,
            audio_end_sample=self._task_audio_end_sample,
            boundary_observed=self._finished_task_id == task_id,
        )
        while len(self._task_event_order) > 8:
            expired = self._task_event_order.popleft()
            self._task_event_contexts.pop(expired, None)

    def task_event_context(self, task_id: str) -> FunASRTaskEventContext | None:
        """Return the preserved epoch/origin for a queued provider event."""

        if task_id and task_id == self.task_id:
            self._remember_current_task_event_context()
        return self._task_event_contexts.get(task_id)

    def _task_context_matches(
        self,
        *,
        ws: ClientConnection,
        task_id: str,
        task_epoch: int,
    ) -> bool:
        """Return whether a PCM send still targets the same live task."""

        return (
            not self._closed
            and not self._failed
            and self._ws is ws
            and (self.task_id == task_id or (not task_id and self.task_id is None))
            and self._task_epoch == task_epoch
            and (not task_id or self._finished_task_id != task_id)
            and self._ready.is_set()
        )

    async def _fail_task_fence(
        self,
        *,
        task_id: str | None = None,
        task_epoch: int | None = None,
        error_code: str = "task_fence",
        error_message: str = "FunASR task state changed during PCM send",
        failed_ws: ClientConnection | None = None,
        enqueue_event: bool = True,
    ) -> FunASRTaskFailure:
        """Fail closed after an ambiguous provider/task transition.

        Once a PCM frame may have crossed a provider boundary, the frame is not
        replayed and the session cannot be made ready again by a late event.
        """

        failure = self._record_task_failure(
            task_id=task_id,
            task_epoch=task_epoch,
            error_code=error_code,
            error_message=error_message,
        )
        self._failed = True
        self._replaceable_provider_failure = False
        self._ready.clear()
        self._rotation_pending = False
        self._task_failed_event.set()
        self._task_started_event.set()
        self._clear_pcm_ring()
        ws = failed_ws if failed_ws is not None else self._ws
        if ws is not None and self._ws is ws:
            self._ws = None
            self._mark_ws_disconnected()
            with contextlib.suppress(Exception):
                await ws.close()
        if enqueue_event:
            self.events.put_nowait(
                FunASRServerEvent(
                    event="task-failed",
                    task_id=failure.task_id,
                    error_code=failure.error_code,
                    error_message=failure.error_message,
                )
            )
        return failure

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

    def _task_boundary_observed(self) -> bool:
        return bool(self.task_id) and self._finished_task_id == self.task_id

    @property
    def task_sample_origin(self) -> int:
        return self._task_sample_origin

    @property
    def task_audio_start_sample(self) -> int | None:
        return self._task_audio_start_sample

    @property
    def task_audio_end_sample(self) -> int | None:
        return self._task_audio_end_sample

    @property
    def task_audio_send_count(self) -> int:
        return self._task_audio_send_count

    @property
    def task_pcm_sample_count(self) -> int:
        return self._task_pcm_sample_count

    @property
    def task_pcm_peak_abs(self) -> int | None:
        return self._task_pcm_peak_abs if self._task_pcm_sample_count else None

    @property
    def task_pcm_rms(self) -> float | None:
        if not self._task_pcm_sample_count:
            return None
        return math.sqrt(self._task_pcm_square_sum / self._task_pcm_sample_count)

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

    def _reset_task_audio_evidence(self) -> None:
        self._task_audio_start_sample = None
        self._task_audio_end_sample = None
        self._task_audio_send_count = 0
        self._task_pcm_sample_count = 0
        self._task_pcm_peak_abs = 0
        self._task_pcm_square_sum = 0.0

    def _record_task_audio_send(
        self,
        *,
        pcm: bytes,
        start_sample: int,
        end_sample: int,
    ) -> None:
        if start_sample < self._task_sample_origin or end_sample <= start_sample:
            raise ValueError("FunASR provider audio range is invalid")
        sample_count = len(pcm) // 2
        if not pcm or len(pcm) % 2 or end_sample - start_sample != sample_count:
            raise ValueError("FunASR provider PCM evidence is invalid")
        if self._task_audio_start_sample is None:
            self._task_audio_start_sample = start_sample
        else:
            self._task_audio_start_sample = min(self._task_audio_start_sample, start_sample)
        self._task_audio_end_sample = max(self._task_audio_end_sample or 0, end_sample)
        self._task_audio_send_count += 1
        chunk_peak = audioop.max(pcm, 2)
        chunk_rms = audioop.rms(pcm, 2)
        self._task_pcm_sample_count += sample_count
        self._task_pcm_peak_abs = max(self._task_pcm_peak_abs, chunk_peak)
        self._task_pcm_square_sum += float(chunk_rms * chunk_rms * sample_count)
        self._remember_current_task_event_context()

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
                                # Any usable final in this VAD segment makes the
                                # offline rescue unnecessary for the whole turn.
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

    async def send_pcm(
        self,
        pcm: bytes,
        *,
        replayed: bool = False,
        capture_start_sample: int | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("FunASR session not connected")
        replaceable_provider_failure = (
            (self._failed or self._task_failed_event.is_set())
            and self._replaceable_provider_failure
        )
        if self._ws is None and not replaceable_provider_failure:
            raise RuntimeError("FunASR session not connected")
        if not pcm or len(pcm) % 2:
            raise ValueError("FunASR PCM must be non-empty 16-bit samples")
        async with self._task_rotation_lock:
            # Re-check the absolute capture watermark while holding the same
            # lock as task rotation.  A caller must never send a frame whose
            # range was valid only before a boundary/recovery completed.
            if not replayed:
                start = (
                    self._last_sent_sample
                    if capture_start_sample is None
                    else capture_start_sample
                )
                if start < self._last_sent_sample:
                    raise ValueError("capture sample position moved backwards")
            else:
                start = (
                    capture_start_sample
                    if capture_start_sample is not None
                    else max(
                        self._task_sample_origin,
                        self._last_sent_sample - len(pcm) // 2,
                    )
                )
            end = start + len(pcm) // 2
            await self._replace_failed_provider_task_if_needed(next_origin=start)
            await self._start_task_for_pcm_if_needed(next_origin=start)
            await self._wait_until_ready("send PCM")
            ws = self._ws
            if ws is None:
                raise RuntimeError("FunASR session not connected")
            task_id = self.task_id or ""
            task_epoch = self._task_epoch
            if not self._task_context_matches(
                ws=ws,
                task_id=task_id,
                task_epoch=task_epoch,
            ):
                await self._fail_task_fence(
                    task_id=task_id,
                    task_epoch=task_epoch,
                    error_code="task_boundary_race",
                    error_message="FunASR task changed before PCM send",
                    failed_ws=ws,
                )
                raise self._failure_exception("FunASR task boundary race")
            started = monotonic()
            send_completed = False
            try:
                await ws.send(pcm)
                send_completed = True
                if not self._task_context_matches(
                    ws=ws,
                    task_id=task_id,
                    task_epoch=task_epoch,
                ):
                    await self._fail_task_fence(
                        task_id=task_id,
                        task_epoch=task_epoch,
                        error_code="task_boundary_race",
                        error_message="FunASR task changed during PCM send",
                        failed_ws=ws,
                    )
                    raise self._failure_exception("FunASR task boundary race")
                if not replayed and self._last_sent_sample == 0 and self._task_sample_origin == 0:
                    self._task_sample_origin = start
                self._record_task_audio_send(
                    pcm=pcm,
                    start_sample=start,
                    end_sample=end,
                )
                if not replayed:
                    if start > self._last_sent_sample:
                        # A capture discontinuity must not make replay bytes
                        # appear to belong to the missing interval.  Commit the
                        # clear only after the provider send is known to target
                        # the captured task.
                        self._clear_pcm_ring()
                    self._push_ring(pcm, start_sample=start)
                    self._last_sent_sample = end
                    self._remember_segment_pcm(
                        pcm,
                        start_sample=start,
                        end_sample=end,
                    )
            except Exception as exc:
                if self._failed or self._task_failed_event.is_set():
                    raise
                if send_completed:
                    await self._fail_task_fence(
                        task_id=task_id,
                        task_epoch=task_epoch,
                        error_code="pcm_evidence_invalid",
                        error_message="FunASR PCM evidence could not be committed",
                        failed_ws=ws,
                    )
                    raise self._failure_exception("FunASR PCM evidence failed") from exc
                if not await self._recover(ws):
                    if not self._failure_matches_current_task():
                        self._record_task_failure(error_message="FunASR reconnect failed")
                    self._clear_pcm_ring()
                    raise self._failure_exception("FunASR reconnect failed") from None
        if self.metrics is not None:
            self.metrics.set_media_metric(
                "asr_send_lag_ms",
                (monotonic() - started) * 1000.0,
            )

    async def _replace_failed_provider_task_if_needed(self, *, next_origin: int) -> None:
        """Replace an explicit provider failure before sending the next PCM frame."""

        if not (self._failed or self._task_failed_event.is_set()):
            return
        if not self._replaceable_provider_failure:
            raise self._failure_exception("FunASR cannot send PCM")

        failed_task_id = self.task_id or ""
        empty_audio = self._has_replaceable_empty_audio_failure()
        if empty_audio:
            # The provider has conclusively rejected this task as empty.  Its
            # bounded ring belongs to the discarded segment and must not be
            # replayed into the next user turn.
            self._clear_pcm_ring()
        replay_start = self.replay_start_sample()
        replay = self._replay_pcm()
        failed_ws = self._ws
        recv_task = self._recv_task
        self._recv_task = None
        if recv_task is not None and recv_task is not asyncio.current_task():
            recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await recv_task
        self._mark_ws_disconnected()
        if failed_ws is not None:
            with contextlib.suppress(Exception):
                await failed_ws.close()

        try:
            self._breaker.before_request()
            if self.metrics is not None:
                self.metrics.inc_provider_ws_reconnect("asr")
            ws, task_id, startup_events = await self._open_with_retry()
        except Exception as exc:
            self._breaker.record_failure()
            self._failed = True
            self._ready.clear()
            self._task_failed_event.set()
            self._ws = None
            raise APIConnectionError(
                f"FunASR provider task replacement failed: {exc}"
            ) from exc

        self._ws = ws
        self.task_id = task_id
        self._task_epoch += 1
        self._task_sample_origin = replay_start if replay else next_origin
        self._reset_task_audio_evidence()
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
        self._ready.set()
        self._remember_current_task_event_context()
        self._mark_ws_connected()
        try:
            for event in startup_events:
                await self.events.put(event)
            if replay:
                await ws.send(replay)
                self._record_task_audio_send(
                    pcm=replay,
                    start_sample=replay_start,
                    end_sample=replay_start + len(replay) // 2,
                )
        except Exception as exc:
            await self._fail_task_fence(
                task_id=task_id,
                task_epoch=self._task_epoch,
                error_code="provider_failure_replay_failed",
                error_message="FunASR provider-failure replay was ambiguous",
                failed_ws=ws,
            )
            raise self._failure_exception("FunASR provider-failure replay failed") from exc
        self._discard_queued_task_failure(failed_task_id)
        self._breaker.record_success()
        self._recv_task = asyncio.create_task(self._recv_loop(), name="funasr-recv-loop")
        logger.info(
            "FunASR replaced failed provider task task_id=%s task_epoch=%s "
            "origin=%s replay_samples=%s",
            task_id,
            self._task_epoch,
            self._task_sample_origin,
            len(replay) // 2,
        )

    def _discard_queued_task_failure(self, task_id: str) -> None:
        preserved: list[FunASRServerEvent] = []
        while True:
            try:
                event = self.events.get_nowait()
            except asyncio.QueueEmpty:
                break
            if event.event == "task-failed" and (
                not event.task_id or event.task_id == task_id
            ):
                continue
            preserved.append(event)
        for event in preserved:
            self.events.put_nowait(event)

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

    async def finish(self, *, terminal: bool = True) -> None:
        if self._ws is None or self.task_id is None:
            return
        boundary_observed = self._task_boundary_observed()
        if terminal and boundary_observed:
            # A client VAD boundary deliberately leaves the WebSocket between
            # tasks until the next PCM frame arrives.  Terminal shutdown must
            # not start an empty provider task merely to finish it again.
            self._finishing = True
            self._terminal_finishing = True
            self._rotation_pending = False
            self._idle_terminal_event.set()
            logger.info(
                "FunASR terminal finish satisfied by idle task boundary task_id=%s",
                self.task_id,
            )
            return
        if self._finishing:
            return
        self._finishing = True
        self._terminal_finishing = terminal
        if boundary_observed:
            # The provider already closed this task.  Clearing the boundary
            # state here would make wait_for_task_finished() wait for a
            # second task-finished that will never arrive.
            logger.info(
                "FunASR finish no-op: task boundary already observed task_id=%s",
                self.task_id,
            )
            return
        self._task_finished_received.clear()
        self._task_finished_consumed.clear()
        self._finished_task_id = None
        await self._wait_until_ready("finish task")
        ws = self._ws
        assert ws is not None
        try:
            await ws.send(json.dumps(build_finish_task(self.task_id), ensure_ascii=False))
            self._trace_ws(
                f"tx finish-task task_id={self.task_id} terminal={terminal}"
            )
        except Exception:
            if not await self._recover(ws):
                if not self._failure_matches_current_task():
                    self._record_task_failure(error_message="FunASR reconnect failed")
                raise self._failure_exception(
                    "FunASR reconnect failed while finishing"
                ) from None

    async def wait_for_task_finished(self, *, require_consumed: bool = False) -> None:
        """Wait for one provider task boundary; heartbeat packets cannot extend it."""

        boundary_observed = self._task_boundary_observed()
        if boundary_observed and (
            not require_consumed or self._task_finished_consumed.is_set()
        ):
            logger.info(
                "FunASR task boundary already satisfied task_id=%s consumed=%s",
                self.task_id,
                self._task_finished_consumed.is_set(),
            )
            return
        if self._closed:
            raise APIConnectionError("FunASR session closed before task boundary")
        if self._failed or self._task_failed_event.is_set():
            if self._has_replaceable_empty_audio_failure():
                return
            raise self._failure_exception("FunASR task failed before task boundary")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.result_timeout_s

        async def _wait_for_event_or_failure(event: asyncio.Event) -> None:
            event_task = asyncio.create_task(event.wait())
            failed_task = asyncio.create_task(self._task_failed_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {event_task, failed_task},
                    timeout=max(0.001, deadline - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    raise TimeoutError
                if self._closed:
                    raise APIConnectionError(
                        "FunASR session closed before task boundary"
                    )
                if self._task_failed_event.is_set():
                    if self._has_replaceable_empty_audio_failure():
                        return
                    raise self._failure_exception(
                        "FunASR task failed before task boundary"
                    )
            finally:
                for task in (event_task, failed_task):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

        try:
            if not boundary_observed:
                await _wait_for_event_or_failure(self._task_finished_received)
            if require_consumed:
                await _wait_for_event_or_failure(self._task_finished_consumed)
        except TimeoutError as exc:
            raise APIConnectionError(
                f"FunASR task boundary timed out after {self.config.result_timeout_s:.3f}s"
            ) from exc

    def acknowledge_task_finished(self, task_id: str) -> None:
        if task_id and task_id == self.task_id:
            self._task_finished_consumed.set()

    async def _maybe_rescue_segment(self) -> None:
        """Rescue an empty VAD segment through the offline SenseVoice fallback.

        Fires only when the provider produced no nonempty final for the whole
        segment while real speech energy was captured.  The transcript is
        enqueued as a normal provider final (plus a synthetic boundary when
        the provider rejected the task as empty audio), so timeline, fence
        and commit logic stay on their existing paths.  Never raises.
        """

        try:
            await self._rescue_segment_once()
        except Exception:
            self._rescue_deadline = None
            logger.warning("funasr segment rescue failed", exc_info=True)
            if self.metrics is not None:
                self.metrics.inc_funasr_rescue("failed")

    async def _rescue_segment_once(self) -> None:
        self._rescue_deadline = None
        rescue_config = self.config.rescue_config
        if (
            self._rescue is None
            or rescue_config is None
            or self._closed
            or self._segment_nonempty_final_seen
            or not self._segment_pcm
        ):
            return
        pcm = b"".join(self._segment_pcm)
        rms = audioop.rms(pcm, 2)
        if rms < rescue_config.min_rms:
            logger.info(
                "funasr segment rescue skipped: no speech energy rms=%s task_id=%s",
                rms,
                self.task_id or "unknown",
            )
            if self.metrics is not None:
                self.metrics.inc_funasr_rescue("skipped")
            return
        task_id = self.task_id or ""
        boundary = self._task_boundary_observed()
        empty_audio_boundary = self._has_replaceable_empty_audio_failure()
        if not task_id or not (boundary or empty_audio_boundary):
            return
        # Snapshot every timing input before the network call: a concurrent
        # recovery may rotate the provider task while the rescue is in flight.
        task_origin = self._task_sample_origin
        segment_start = self._segment_pcm_start_sample
        segment_end = self._segment_pcm_end_sample or segment_start
        if segment_start is None or segment_end is None:
            return
        loop = asyncio.get_running_loop()
        self._rescue_deadline = loop.time() + rescue_config.timeout_s
        try:
            text = await self._rescue.transcribe(
                pcm,
                sample_rate=self.config.sample_rate,
                language=self.config.language,
            )
        finally:
            self._rescue_deadline = None
        if self._closed or self._segment_nonempty_final_seen:
            # A late provider final crossed the queue while the rescue was in
            # flight; the provider result stays authoritative.
            return
        text = (text or "").strip()
        if len(text) < rescue_config.min_text_chars:
            logger.info(
                "funasr segment rescue produced no text task_id=%s rms=%s pcm_ms=%s",
                task_id,
                rms,
                round(self._segment_pcm_samples * 1000 / self.config.sample_rate),
            )
            if self.metrics is not None:
                self.metrics.inc_funasr_rescue("no_text")
            return
        begin_ms = max(
            0,
            round((segment_start - task_origin) * 1000 / self.config.sample_rate),
        )
        end_ms = max(
            begin_ms + 1,
            round((segment_end - task_origin) * 1000 / self.config.sample_rate),
        )
        sentence = FunASRSentence(
            sentence_id=0,
            text=text,
            begin_ms=begin_ms,
            end_ms=end_ms,
            sentence_end=True,
            heartbeat=False,
            words=(),
        )
        if empty_audio_boundary and not boundary:
            # The provider rejected the task as empty audio, so no
            # task-finished will ever arrive.  Synthesize the whole boundary
            # sequence and drop the queued task-failed so both consumers see
            # a normal final + boundary pair.
            self._discard_queued_task_failure(task_id)
            self._finished_task_id = task_id
            self._task_finished_received.set()
            await self.events.put(
                FunASRServerEvent(event="result-generated", task_id=task_id, sentence=sentence)
            )
            await self.events.put(FunASRServerEvent(event="task-finished", task_id=task_id))
        else:
            # Normal boundary: task-finished is already queued; the synthetic
            # final becomes the provider's last tail final.
            await self.events.put(
                FunASRServerEvent(event="result-generated", task_id=task_id, sentence=sentence)
            )
        self._segment_nonempty_final_seen = True
        if self.metrics is not None:
            self.metrics.inc_funasr_rescue("rescued")
        logger.info(
            "funasr segment rescued offline task_id=%s text_len=%s rms=%s pcm_ms=%s gaps=%s",
            task_id,
            len(text),
            rms,
            round(self._segment_pcm_samples * 1000 / self.config.sample_rate),
            self._segment_pcm_gaps,
        )

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        """Finish one VAD segment and arm lazy reuse for the next PCM frame."""

        async with self._task_rotation_lock:
            if self._closed or self._ws is None or self.task_id is None:
                raise APIConnectionError("FunASR task rotation requires an active session")
            if self._last_sent_sample <= self._last_rotation_sample:
                return
            if self._finishing:
                raise APIConnectionError("FunASR task rotation already in progress")
            empty_audio_boundary = self._has_replaceable_empty_audio_failure()
            if self._failed and not empty_audio_boundary:
                raise self._failure_exception(
                    "FunASR task rotation requires a healthy session"
                )
            boundary_observed = self._task_boundary_observed() or empty_audio_boundary
            if boundary_observed:
                logger.info(
                    "FunASR rotation reuses task without finish: boundary already observed "
                    "task_id=%s empty_audio=%s",
                    self.task_id,
                    empty_audio_boundary,
                )
            self._rotation_pending = True
            try:
                if not boundary_observed:
                    await self.finish(terminal=False)
                # The LiveKit consumer maps queued events with the session's
                # current task epoch/sample origin.  When the provider closes
                # early, do not advance that context until its task-finished
                # event has crossed the consumer barrier.  Direct adapters
                # pass require_consumed=False and retain their task-id snapshots.
                await self.wait_for_task_finished(require_consumed=require_consumed)
                await self._maybe_rescue_segment()
                self._last_rotation_sample = self._last_sent_sample
                self._finishing = False
                self._terminal_finishing = False
                self._reset_segment_evidence()
            except BaseException:
                self._rotation_pending = False
                self._failed = True
                self._task_failed_event.set()
                self._ready.clear()
                self._clear_pcm_ring()
                self._reset_segment_evidence()
                raise

    async def _start_task_for_pcm_if_needed(self, *, next_origin: int) -> None:
        boundary_observed = self._task_boundary_observed()
        if boundary_observed and not self._rotation_pending:
            # The provider may close a task before the client VAD boundary.
            # Treat the next PCM frame as the atomic handoff to a new task; the
            # caller will still finalize that new task at the real VAD edge.
            self._rotation_pending = True
            self._last_rotation_sample = max(
                self._last_rotation_sample,
                self._last_sent_sample,
            )
            self._finishing = False
            self._terminal_finishing = False
        if not self._rotation_pending:
            return
        await self._start_reused_task(next_origin=next_origin)

    async def _start_reused_task(self, *, next_origin: int | None = None) -> None:
        ws = self._ws
        if ws is None:
            raise APIConnectionError("FunASR WebSocket is unavailable for task reuse")

        run = self._build_run_task()
        task_id = str(run["header"]["task_id"])
        origin = self._last_sent_sample if next_origin is None else next_origin
        if origin < self._last_sent_sample:
            raise APIConnectionError("FunASR reused task origin moved backwards")
        self._task_started_event.clear()
        self._task_finished_received.clear()
        self._task_finished_consumed.clear()
        self._task_failed_event.clear()
        self._replaceable_provider_failure = False
        self.task_id = task_id
        self._task_epoch += 1
        self._segment_epoch += 1
        self._task_sample_origin = origin
        self._reset_task_audio_evidence()
        self._finishing = False
        self._terminal_finishing = False
        self._idle_terminal_event.clear()
        self._ready.clear()
        self._pcm_ring.clear()
        self._pcm_ring_ranges.clear()
        self._pcm_ring_bytes = 0
        self._remember_current_task_event_context()
        try:
            await ws.send(json.dumps(run, ensure_ascii=False))
            self._trace_ws(
                f"tx run-task task_id={task_id} epoch={self._task_epoch} origin={origin}"
            )
            await asyncio.wait_for(
                self._task_started_event.wait(),
                timeout=self.config.connect_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fail_task_fence(
                task_id=task_id,
                task_epoch=self._task_epoch,
                error_code="task_start_failed",
                error_message=f"FunASR reused task failed to start: {exc}",
                failed_ws=ws,
            )
            raise self._failure_exception("FunASR reused task failed to start") from exc
        if self._failed or not self._ready.is_set():
            if self._failed or self._task_failed_event.is_set():
                raise self._failure_exception("FunASR reused task was rejected")
            await self._fail_task_fence(
                task_id=task_id,
                task_epoch=self._task_epoch,
                error_code="task_start_rejected",
                error_message="FunASR reused task was rejected",
                failed_ws=ws,
            )
            raise self._failure_exception("FunASR reused task was rejected")
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
        if self._failed or self._task_failed_event.is_set():
            raise self._failure_exception(f"FunASR cannot {operation}")
        if self._ws is None:
            raise APIConnectionError(f"FunASR cannot {operation}: session unavailable")

    async def aclose(self) -> None:
        self._closed = True
        self._rescue_deadline = None
        self._reset_segment_evidence()
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
        chunk_bytes = samples_per_chunk * 2
        byte_stream = bytearray()
        capture_sample = 0
        preroll: deque[bytes] = deque()
        preroll_bytes = 0
        preroll_limit = max(
            chunk_bytes,
            self._config.sample_rate * 2 * self._config.reconnect_audio_ms // 1000,
        )

        def _keep_preroll(chunk: bytes) -> None:
            nonlocal preroll_bytes
            preroll.append(chunk)
            preroll_bytes += len(chunk)
            while preroll_bytes > preroll_limit and preroll:
                preroll_bytes -= len(preroll.popleft())

        def _clear_preroll() -> None:
            nonlocal preroll_bytes
            preroll.clear()
            preroll_bytes = 0

        def _sending() -> bool:
            return self._stt_instance.pcm_enabled or self._stt_instance.pcm_draining

        async def _send_pcm(chunk: bytes, *, start: int) -> None:
            self._stt_instance.observe_pcm(chunk)
            await session.send_pcm(chunk, capture_start_sample=start)

        async def _flush_preroll() -> None:
            nonlocal preroll_bytes
            if not preroll:
                return
            start = capture_sample - preroll_bytes // 2
            while preroll:
                chunk = preroll.popleft()
                preroll_bytes -= len(chunk)
                await _send_pcm(chunk, start=start)
                start += len(chunk) // 2

        async def _emit_or_hold(chunk: bytes) -> None:
            nonlocal capture_sample
            if self._stt_instance.pcm_enabled:
                await _flush_preroll()
                await _send_pcm(chunk, start=capture_sample)
            elif self._stt_instance.pcm_draining:
                await _send_pcm(chunk, start=capture_sample)
            else:
                _keep_preroll(chunk)
            capture_sample += len(chunk) // 2

        async for data in self._input_ch:
            if isinstance(data, rtc.AudioFrame):
                pcm = bytes(data.data)
                if data.sample_rate != self._config.sample_rate:
                    pcm = resample_pcm_16le(
                        pcm, src_rate=data.sample_rate, dst_rate=self._config.sample_rate
                    )
                byte_stream.extend(pcm)
                while len(byte_stream) >= chunk_bytes:
                    chunk = bytes(byte_stream[:chunk_bytes])
                    del byte_stream[:chunk_bytes]
                    await _emit_or_hold(chunk)
            elif isinstance(data, self._FlushSentinel):
                sending = _sending()
                if byte_stream:
                    remaining = bytes(byte_stream)
                    byte_stream.clear()
                    if sending:
                        await _emit_or_hold(remaining)
                    else:
                        capture_sample += len(remaining) // 2
                if not sending:
                    _clear_preroll()
                self._stt_instance.clear_pcm_drain()
                if not self._input_ch.closed:
                    await session.rotate_task()
        if byte_stream:
            remaining = bytes(byte_stream)
            if _sending():
                await _emit_or_hold(remaining)
        await session.finish()
        await session.wait_for_task_finished()

    async def _recv_events(self, session: FunASRSession) -> None:
        idle_timeouts = 0
        max_idle = 3
        pending_boundary_task_id = ""
        pending_boundary_deadline: float | None = None
        pending_terminal_return = False

        def finish_pending_boundary() -> bool:
            nonlocal pending_boundary_task_id
            nonlocal pending_boundary_deadline
            nonlocal pending_terminal_return
            if self._speaking:
                self._event_ch.send_nowait(
                    stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                )
                self._speaking = False
            should_return = pending_terminal_return
            pending_boundary_task_id = ""
            pending_boundary_deadline = None
            pending_terminal_return = False
            return should_return

        while True:
            loop = asyncio.get_running_loop()
            if pending_boundary_deadline is not None:
                rescue_deadline = session.pending_rescue_deadline()
                if rescue_deadline is not None and rescue_deadline > pending_boundary_deadline:
                    # The session is transcribing an empty segment offline;
                    # keep the bounded tail window open until it settles.
                    pending_boundary_deadline = rescue_deadline
            if (
                pending_boundary_deadline is not None
                and loop.time() >= pending_boundary_deadline
            ):
                if finish_pending_boundary():
                    return
                continue
            event_task = asyncio.create_task(session.events.get())
            idle_terminal_task: asyncio.Task[bool] | None = None
            waiters: set[asyncio.Task[Any]] = {event_task}
            if pending_boundary_deadline is None:
                idle_terminal_task = asyncio.create_task(
                    session._idle_terminal_event.wait()
                )
                waiters.add(idle_terminal_task)
            timeout_s = self._config.result_timeout_s
            if pending_boundary_deadline is not None:
                timeout_s = min(
                    timeout_s,
                    max(0.001, pending_boundary_deadline - loop.time()),
                )
            try:
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=timeout_s,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if event_task in done:
                    ev = event_task.result()
                elif idle_terminal_task is not None and idle_terminal_task in done:
                    return
                else:
                    raise TimeoutError
                idle_timeouts = 0
            except TimeoutError:
                if pending_boundary_deadline is not None:
                    rescue_deadline = session.pending_rescue_deadline()
                    if rescue_deadline is not None and rescue_deadline > pending_boundary_deadline:
                        pending_boundary_deadline = rescue_deadline
                        continue
                    if loop.time() >= pending_boundary_deadline:
                        if finish_pending_boundary():
                            return
                    continue
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
            finally:
                for task in waiters:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            if ev.event == "task-failed":
                if _is_empty_audio_error(ev.error_code):
                    if (
                        pending_boundary_task_id
                        and ev.task_id != pending_boundary_task_id
                        and finish_pending_boundary()
                    ):
                        return
                    if self._speaking:
                        self._event_ch.send_nowait(
                            stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                        )
                        self._speaking = False
                    # EmptyAudio is a task-local no-speech outcome.  The
                    # provider closes this WebSocket, but the send side keeps
                    # the long-lived stream alive and replaces the task on the
                    # next PCM frame.  A terminal empty task can end cleanly.
                    if session.finishing and not session.rotation_pending:
                        return
                    continue
                raise session._failure_exception("FunASR task failed")
            if ev.event == "task-finished":
                if (
                    pending_boundary_task_id
                    and ev.task_id != pending_boundary_task_id
                    and finish_pending_boundary()
                ):
                    return
                session.acknowledge_task_finished(ev.task_id)
                terminal_return = not session.rotation_pending
                if terminal_return and not session.finishing:
                    raise APIConnectionError(
                        "FunASR task finished without a client boundary"
                    )
                # A single provider task can emit more than one sentence-end
                # result.  Seeing an earlier final is not proof that the last
                # final has crossed the queue, so always leave a bounded tail
                # window before publishing END_OF_SPEECH.
                pending_boundary_task_id = ev.task_id
                pending_boundary_deadline = (
                    asyncio.get_running_loop().time()
                    + self._config.post_finish_tail_grace_s
                )
                pending_terminal_return = terminal_return
                continue
            if ev.event != "result-generated" or ev.sentence is None:
                continue
            if (
                pending_boundary_task_id
                and ev.task_id != pending_boundary_task_id
                and finish_pending_boundary()
            ):
                return
            sent = ev.sentence
            if sent.heartbeat and not sent.text:
                continue
            event_context = session.task_event_context(ev.task_id)
            if ev.task_id and ev.task_id != self._provider_task_id:
                self._provider_task_id = ev.task_id
                self._provider_task_epoch += 1
            task_epoch = max(
                1,
                event_context.task_epoch if event_context is not None else session.task_epoch,
            )
            segment_epoch = (
                event_context.segment_epoch
                if event_context is not None
                else session.segment_epoch
            )
            sample_offset = (
                event_context.sample_origin
                if event_context is not None
                else session.task_sample_origin
            )
            sentence_id = str(sent.sentence_id)
            sentence_key = (segment_epoch, sentence_id)
            revision = self._sentence_revisions.get(sentence_key, 0) + 1
            self._sentence_revisions[sentence_key] = revision
            asr_result = sentence_to_asr_result(
                sent,
                task_epoch=task_epoch,
                sample_rate=self._config.sample_rate,
                revision=revision,
                stream_epoch=self._stream_epoch,
                sample_offset=sample_offset,
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
                    segment_epoch,
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
            request_id = ev.task_id or session.task_id or ""
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
                if ev.task_id == pending_boundary_task_id:
                    # Reset the bounded tail window after every final in the
                    # provider's finished task so multiple late finals stay
                    # ahead of END_OF_SPEECH.
                    pending_boundary_deadline = (
                        asyncio.get_running_loop().time()
                        + self._config.post_finish_tail_grace_s
                    )
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
        self._nonempty_final: asyncio.Event | None = None
        self._nonempty_seq = 0
        self._last_nonempty_at = 0.0
        self._last_final_at = 0.0
        self._pcm_enabled = True
        self._pcm_draining = False

    def set_pcm_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled:
            self._pcm_enabled = True
            self._pcm_draining = False
            return
        if self._pcm_enabled:
            self._pcm_draining = True
        self._pcm_enabled = False

    @property
    def pcm_enabled(self) -> bool:
        return self._pcm_enabled

    @property
    def pcm_draining(self) -> bool:
        return self._pcm_draining

    def clear_pcm_drain(self) -> None:
        self._pcm_draining = False

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

    def _nonempty_event(self) -> asyncio.Event:
        event = self._nonempty_final
        if event is None:
            event = asyncio.Event()
            self._nonempty_final = event
        return event

    async def wait_for_nonempty_final(
        self,
        *,
        since: float,
        timeout: float,
        empty_grace_s: float = 2.0,
    ) -> bool:
        """Return whether a nonempty FunASR final arrived at or after ``since``."""

        start_seq = self._nonempty_seq
        event = self._nonempty_event()
        deadline = since + timeout
        empty_deadline: float | None = None
        while True:
            if self._last_nonempty_at >= since or self._nonempty_seq > start_seq:
                return True
            now = monotonic()
            remaining = deadline - now
            if self._last_final_at >= since and empty_deadline is None:
                empty_deadline = self._last_final_at + empty_grace_s
            if empty_deadline is not None:
                remaining = min(remaining, empty_deadline - now)
            if remaining <= 0:
                return False
            event.clear()
            if self._last_nonempty_at >= since or self._nonempty_seq > start_seq:
                return True
            try:
                await asyncio.wait_for(event.wait(), timeout=remaining)
            except TimeoutError:
                return self._last_nonempty_at >= since or self._nonempty_seq > start_seq

    def trace_result(self, sentence: FunASRSentence, *, task_epoch: int) -> None:
        metrics = result_trace_metrics(sentence, task_epoch=task_epoch)
        self._last_final_at = monotonic()
        self._nonempty_event().set()
        if metrics["text_len"]:
            self._last_nonempty_at = self._last_final_at
            self._nonempty_seq += 1
        logger.info(
            "funasr_final text_len=%s task_epoch=%s",
            metrics["text_len"],
            task_epoch,
        )
        callback = self._trace_callback
        if callback is None:
            return
        callback("funasr_final", "ok", metrics)

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
