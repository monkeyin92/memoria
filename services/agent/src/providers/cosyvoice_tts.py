"""CosyVoice Realtime TTS adapter with connection pool (ch.15)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

import websockets
from livekit.agents import APIConnectionError, APIConnectOptions, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, TimedString
from websockets.asyncio.client import ClientConnection

from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.prosody import cosyvoice_instruction
from services.agent.src.providers.cosyvoice_protocol import (
    build_continue_text,
    build_finish_task,
    build_run_task,
    normalize_pcm16_peak,
    parse_server_message,
    pcm_duration_ms,
    scale_word_timestamps,
)
from services.agent.src.providers.reliability import CircuitBreaker

logger = logging.getLogger(__name__)
COSYVOICE_EMOTIONS = frozenset(
    {"neutral", "happy", "sad", "surprised", "angry", "fearful", "disgusted"}
)


@dataclass
class CosyVoiceConfig:
    api_key: str
    ws_url: str
    model: str = "cosyvoice-v3-flash"
    voice: str = "longanyang"
    sample_rate: int = 24000
    rate: float = 1.0
    pitch: float = 1.0
    # 50 is CosyVoice default but often reads soft/uneven on mobile WebRTC;
    # 70 keeps headroom without clipping on longanyang.
    volume: int = 70
    word_timestamps: bool = True
    pool_size: int = 4
    connect_timeout_s: float = 5.0
    first_audio_timeout_s: float = 1.5
    total_timeout_s: float = 20.0
    instruction: str | None = None

    def __post_init__(self) -> None:
        if self.voice != "longanyang" or self.instruction is None:
            return
        allowed = {cosyvoice_instruction(emotion) for emotion in COSYVOICE_EMOTIONS}
        if self.instruction not in allowed:
            raise ValueError("longanyang requires Alibaba's fixed Instruct format")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CosyVoiceConfig:
        import os

        e = env or dict(os.environ)
        return cls(
            api_key=e.get("DASHSCOPE_API_KEY", ""),
            ws_url=e.get("COSYVOICE_MOCK_WS_URL") or e.get("DASHSCOPE_WS_URL", ""),
            model=e.get("COSYVOICE_MODEL", "cosyvoice-v3-flash"),
            voice=e.get("COSYVOICE_VOICE", "longanyang"),
            sample_rate=int(e.get("COSYVOICE_SAMPLE_RATE", "24000")),
            rate=float(e.get("COSYVOICE_RATE", "1.0")),
            pitch=float(e.get("COSYVOICE_PITCH", "1.0")),
            volume=int(e.get("COSYVOICE_VOLUME", "70")),
            word_timestamps=e.get("COSYVOICE_WORD_TIMESTAMPS", "true").lower() == "true",
            pool_size=int(e.get("COSYVOICE_POOL_SIZE", "4")),
            connect_timeout_s=float(e.get("COSYVOICE_CONNECT_TIMEOUT_S", "5")),
            first_audio_timeout_s=float(e.get("COSYVOICE_FIRST_AUDIO_TIMEOUT_S", "1.5")),
            total_timeout_s=float(e.get("COSYVOICE_TOTAL_TIMEOUT_S", "20")),
            instruction=e.get("COSYVOICE_INSTRUCTION") or None,
        )


@dataclass
class PooledConnection:
    ws: ClientConnection
    conn_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    in_use: bool = False
    failed: bool = False
    closed: bool = False


@dataclass
class SynthesizeResult:
    pcm: bytes
    words: tuple[TimedWord, ...]
    task_id: str
    alignment_status: str
    discarded: bool = False


class CosyVoiceFirstAudioTimeoutError(TimeoutError):
    """Raised when a task produces no PCM within the first-audio deadline."""


class CosyVoiceTimestampError(RuntimeError):
    """Raised when a completed task has no usable word timestamps."""


class CosyVoicePool:
    """WebSocket pool: cancelled/failed connections are closed and never returned."""

    def __init__(self, config: CosyVoiceConfig, metrics: MetricsRegistry | None = None) -> None:
        self.config = config
        self.metrics = metrics or MetricsRegistry()
        self._available: asyncio.Queue[PooledConnection] = asyncio.Queue()
        self._all: dict[str, PooledConnection] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._refill_tasks: set[asyncio.Task[None]] = set()
        self._breaker = CircuitBreaker()
        self.active_by_fence: dict[str, PooledConnection] = {}
        self.discarded_count = 0

    async def warm(self, size: int | None = None) -> None:
        n = size if size is not None else self.config.pool_size
        for _ in range(n):
            conn = await self._open()
            await self._available.put(conn)
        self.metrics.set_tts_pool_available(self.available_approx)

    async def _open(self) -> PooledConnection:
        if self._closing:
            raise RuntimeError("CosyVoice pool is closing")
        self._breaker.before_request()
        try:
            ws = await websockets.connect(
                self.config.ws_url,
                additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
                open_timeout=self.config.connect_timeout_s,
            )
        except Exception:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        conn = PooledConnection(ws=ws)
        self._all[conn.conn_id] = conn
        return conn

    async def acquire(self, *, wait_s: float = 0.3) -> PooledConnection:
        self._breaker.before_request()
        try:
            conn = await asyncio.wait_for(self._available.get(), timeout=wait_s)
            if conn.closed or conn.failed:
                conn = await self._open()
            conn.in_use = True
            self.metrics.set_tts_pool_available(self.available_approx)
            return conn
        except TimeoutError:
            conn = await self._open()
            conn.in_use = True
            return conn

    async def release(self, conn: PooledConnection) -> None:
        self._unbind_connection(conn)
        if conn.failed or conn.closed:
            await self.discard(conn, reason="failed_or_closed")
            return
        conn.in_use = False
        self._breaker.record_success()
        await self._available.put(conn)
        self.metrics.set_tts_pool_available(self.available_approx)

    async def discard(self, conn: PooledConnection, *, reason: str) -> None:
        self._unbind_connection(conn)
        if conn.closed:
            return
        conn.failed = True
        conn.closed = True
        conn.in_use = False
        self.discarded_count += 1
        self.metrics.inc_tts_connections_discarded(reason)
        if reason not in {"cancel", "shutdown"}:
            self._breaker.record_failure()
        self._all.pop(conn.conn_id, None)
        self.metrics.set_tts_pool_available(self.available_approx)
        with contextlib.suppress(Exception):
            await conn.ws.close()
        if not self._closing:
            task = asyncio.create_task(self._refill_one(), name="cosyvoice-pool-refill")
            self._refill_tasks.add(task)
            task.add_done_callback(self._refill_tasks.discard)

    async def _refill_one(self) -> None:
        async with self._lock:
            if self._closing or len(self._all) >= self.config.pool_size:
                return
            with contextlib.suppress(Exception):
                conn = await self._open()
                if self._closing:
                    with contextlib.suppress(Exception):
                        await conn.ws.close()
                    self._all.pop(conn.conn_id, None)
                    return
                await self._available.put(conn)
                self.metrics.set_tts_pool_available(self.available_approx)

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        key = self._fence_key(fence)
        conn = self.active_by_fence.pop(key, None)
        if conn is not None:
            await self.discard(conn, reason="cancel")

    def bind_active(self, fence: GenerationFence, conn: PooledConnection) -> None:
        self.active_by_fence[self._fence_key(fence)] = conn

    @staticmethod
    def _fence_key(fence: GenerationFence) -> str:
        return (
            f"{fence.session_id}:{fence.turn_id}:"
            f"{fence.generation_id}:{fence.tool_epoch}"
        )

    def _unbind_connection(self, conn: PooledConnection) -> None:
        for key, active in tuple(self.active_by_fence.items()):
            if active is conn:
                self.active_by_fence.pop(key, None)

    @property
    def available_approx(self) -> int:
        return self._available.qsize()

    async def aclose(self) -> None:
        self._closing = True
        for task in tuple(self._refill_tasks):
            task.cancel()
        if self._refill_tasks:
            await asyncio.gather(*tuple(self._refill_tasks), return_exceptions=True)
        self._refill_tasks.clear()
        for conn in list(self._all.values()):
            await self.discard(conn, reason="shutdown")
        self.active_by_fence.clear()
        while not self._available.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._available.get_nowait()
        self.metrics.set_tts_pool_available(0)


class CosyVoiceSynthesizeStream(tts.SynthesizeStream):
    """LiveKit streaming synthesize; cancel closes CosyVoice WS and discards from pool."""

    def __init__(
        self,
        *,
        tts_instance: CosyVoiceTTS,
        config: CosyVoiceConfig,
        pool: CosyVoicePool,
        conn_options: APIConnectOptions,
        fence: GenerationFence | None = None,
    ) -> None:
        super().__init__(tts=tts_instance, conn_options=conn_options)
        self._tts_instance = tts_instance
        self._config = config
        self._pool = pool
        self._fence = fence
        self._conn: PooledConnection | None = None
        self._discarded = False

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        conn = await self._pool.acquire()
        self._conn = conn
        if self._fence is not None:
            self._pool.bind_active(self._fence, conn)
        task_id = str(uuid.uuid4())
        offset_ms = 0
        sentence_pcm: dict[int, bytearray] = {}
        current_index = 0
        got_audio = False
        got_words = False
        output_emitter.initialize(
            request_id=task_id,
            sample_rate=self._config.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            frame_size_ms=20,
            stream=True,
        )
        try:
            run = build_run_task(
                task_id=task_id,
                model=self._config.model,
                voice=self._config.voice,
                sample_rate=self._config.sample_rate,
                rate=self._config.rate,
                pitch=self._config.pitch,
                volume=self._config.volume,
                word_timestamp_enabled=self._config.word_timestamps,
                instruction=self._config.instruction,
            )
            await conn.ws.send(json.dumps(run, ensure_ascii=False))
            while True:
                msg = await asyncio.wait_for(conn.ws.recv(), timeout=self._config.connect_timeout_s)
                if isinstance(msg, bytes):
                    continue
                ev = parse_server_message(msg)
                if ev.event == "task-started":
                    self._tts_instance.trace("cosyvoice_task_started")
                    break
                if ev.event == "task-failed":
                    self._tts_instance.trace(
                        "cosyvoice_task_failed",
                        status="error",
                        detail={"phase": "start"},
                    )
                    await self._pool.discard(conn, reason="task-failed")
                    self._conn = None
                    raise APIConnectionError(ev.error_message or "cosyvoice task-failed")

            output_emitter.start_segment(segment_id=task_id)

            async def sender() -> None:
                async for data in self._input_ch:
                    if isinstance(data, self._FlushSentinel):
                        continue
                    text = str(data)
                    if not text:
                        continue
                    await conn.ws.send(
                        json.dumps(build_continue_text(task_id, text), ensure_ascii=False)
                    )
                await conn.ws.send(json.dumps(build_finish_task(task_id), ensure_ascii=False))

            send_task = asyncio.create_task(sender(), name="cosy-send")
            loop = asyncio.get_running_loop()
            first_audio_deadline = loop.time() + self._config.first_audio_timeout_s
            total_deadline = loop.time() + self._config.total_timeout_s
            try:
                while True:
                    deadline = total_deadline if got_audio else min(
                        total_deadline, first_audio_deadline
                    )
                    try:
                        msg = await asyncio.wait_for(
                            conn.ws.recv(),
                            timeout=max(0.0, deadline - loop.time()),
                        )
                    except TimeoutError:
                        reason = "total-timeout" if got_audio else "first-audio-timeout"
                        await self._pool.discard(conn, reason=reason)
                        self._conn = None
                        raise APIConnectionError(reason) from None
                    if isinstance(msg, bytes):
                        if not got_audio:
                            self._tts_instance.trace(
                                "cosyvoice_first_pcm",
                                detail={"pcm_bytes": len(msg)},
                            )
                        got_audio = True
                        # Peak-normalize each chunk so successive CosyVoice
                        # utterances do not swing between soft and loud.
                        pcm = normalize_pcm16_peak(msg)
                        output_emitter.push(pcm)
                        sentence_pcm.setdefault(current_index, bytearray()).extend(pcm)
                        continue
                    ev = parse_server_message(msg)
                    if ev.event == "sentence-begin" and ev.sentence_index is not None:
                        current_index = ev.sentence_index
                        sentence_pcm.setdefault(current_index, bytearray())
                    elif ev.event == "sentence-end":
                        idx = ev.sentence_index or 0
                        raw_pcm = bytes(sentence_pcm.get(idx, b""))
                        dur = pcm_duration_ms(raw_pcm, sample_rate=self._config.sample_rate)
                        scaled, _status = scale_word_timestamps(
                            ev.words, pcm_duration_ms_value=dur, offset_ms=offset_ms
                        )
                        timed = [
                            TimedString(
                                w.text + (w.punctuation or ""),
                                start_time=w.begin_ms / 1000.0,
                                end_time=w.end_ms / 1000.0,
                            )
                            for w in scaled
                        ]
                        if timed:
                            if not got_words:
                                self._tts_instance.trace(
                                    "cosyvoice_first_word_timestamps",
                                    detail={"word_count": len(timed)},
                                )
                            got_words = True
                            output_emitter.push_timed_transcript(timed)
                        offset_ms += dur
                    elif ev.event == "task-finished":
                        self._tts_instance.trace("cosyvoice_task_finished")
                        if not got_audio or not got_words:
                            reason = "empty-audio" if not got_audio else "empty-timestamps"
                            await self._pool.discard(conn, reason=reason)
                            self._conn = None
                            raise APIConnectionError(reason)
                        break
                    elif ev.event == "task-failed":
                        self._tts_instance.trace(
                            "cosyvoice_task_failed",
                            status="error",
                            detail={"phase": "stream"},
                        )
                        await self._pool.discard(conn, reason="task-failed")
                        self._conn = None
                        raise APIConnectionError(ev.error_message or "cosyvoice task-failed")
            finally:
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task

            output_emitter.end_segment()
            await self._pool.release(conn)
            self._conn = None
        except asyncio.CancelledError:
            self._discarded = True
            if self._conn is not None:
                await self._pool.discard(self._conn, reason="cancel")
                self._conn = None
            raise
        except Exception:
            if self._conn is not None:
                await self._pool.discard(self._conn, reason="error")
                self._conn = None
            raise


class CosyVoiceTTS(tts.TTS[Any]):
    """LiveKit TTS plugin for CosyVoice Realtime with discard-on-cancel pool."""

    def __init__(self, config: CosyVoiceConfig, pool: CosyVoicePool | None = None) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(
                streaming=True,
                aligned_transcript=True,
            ),
            sample_rate=config.sample_rate,
            num_channels=1,
        )
        self._config = config
        self._pool = pool or CosyVoicePool(config)
        self._active_fence: GenerationFence | None = None
        self._trace_callback: (
            Callable[[str, str, dict[str, Any] | None], None] | None
        ) = None

    @classmethod
    def from_env(cls) -> CosyVoiceTTS:
        cfg = CosyVoiceConfig.from_env()
        return cls(cfg, CosyVoicePool(cfg))

    @property
    def provider(self) -> str:
        return "alibaba_model_studio"

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def pool(self) -> CosyVoicePool:
        return self._pool

    def bind_fence(self, fence: GenerationFence) -> None:
        """Associate upcoming synthesize streams with orchestrator fence for cancel discard."""
        self._active_fence = fence

    def set_trace_callback(
        self,
        callback: Callable[[str, str, dict[str, Any] | None], None],
    ) -> None:
        self._trace_callback = callback

    def apply_speech_plan(self, *, emotion: str, rate: float) -> None:
        if emotion not in COSYVOICE_EMOTIONS:
            emotion = "neutral"
        self._config.instruction = cosyvoice_instruction(emotion)
        # Prefer 1.0; still clamp defensive ranges if a caller passes outliers.
        self._config.rate = min(1.05, max(0.95, rate))
        # Keep volume pinned so emotion switches do not change loudness.
        if self._config.volume < 60:
            self._config.volume = 70

    @property
    def current_instruction(self) -> str | None:
        return self._config.instruction

    @property
    def current_rate(self) -> float:
        return self._config.rate

    def trace(
        self,
        name: str,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        if self._trace_callback is not None:
            self._trace_callback(name, status, detail)

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> CosyVoiceSynthesizeStream:
        return CosyVoiceSynthesizeStream(
            tts_instance=self,
            config=replace(self._config),
            pool=self._pool,
            conn_options=conn_options,
            fence=self._active_fence,
        )

    async def synthesize_stream_text(
        self,
        texts: list[str],
        *,
        fence: GenerationFence,
        cancel_event: asyncio.Event | None = None,
    ) -> SynthesizeResult:
        """Synthesize with one fresh-connection retry for first-audio/timestamp failures."""
        for attempt in range(2):
            try:
                return await self._synthesize_once(
                    texts,
                    fence=fence,
                    cancel_event=cancel_event,
                )
            except (CosyVoiceFirstAudioTimeoutError, CosyVoiceTimestampError):
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    async def _synthesize_once(
        self,
        texts: list[str],
        *,
        fence: GenerationFence,
        cancel_event: asyncio.Event | None = None,
    ) -> SynthesizeResult:
        """Run one task; cancelled/failed connections are never returned to the pool."""
        conn = await self._pool.acquire()
        self._pool.bind_active(fence, conn)
        task_id = str(uuid.uuid4())
        pcm_buf = bytearray()
        all_words: list[TimedWord] = []
        offset_ms = 0
        sentence_pcm: dict[int, bytearray] = {}
        current_index = 0
        alignment_status = "ok"
        discarded = False
        cancel_watcher: asyncio.Task[None] | None = None

        if cancel_event is not None:

            async def watch_cancel() -> None:
                await cancel_event.wait()
                await self._pool.discard(conn, reason="cancel")

            cancel_watcher = asyncio.create_task(watch_cancel(), name="cosyvoice-cancel-watch")

        try:
            run = build_run_task(
                task_id=task_id,
                model=self._config.model,
                voice=self._config.voice,
                sample_rate=self._config.sample_rate,
                rate=self._config.rate,
                pitch=self._config.pitch,
                volume=self._config.volume,
                word_timestamp_enabled=self._config.word_timestamps,
                instruction=self._config.instruction,
            )
            await conn.ws.send(json.dumps(run, ensure_ascii=False))

            # Wait task-started
            started = False
            while not started:
                if cancel_event is not None and cancel_event.is_set():
                    discarded = True
                    await self._pool.discard(conn, reason="cancel")
                    return SynthesizeResult(b"", (), task_id, "degraded", discarded=True)
                msg = await asyncio.wait_for(conn.ws.recv(), timeout=self._config.connect_timeout_s)
                if isinstance(msg, bytes):
                    continue
                ev = parse_server_message(msg)
                if ev.event == "task-started":
                    self.trace("cosyvoice_task_started")
                    started = True
                elif ev.event == "task-failed":
                    self.trace(
                        "cosyvoice_task_failed",
                        status="error",
                        detail={"phase": "start"},
                    )
                    await self._pool.discard(conn, reason="task-failed")
                    raise RuntimeError(ev.error_message or "task-failed")

            for text in texts:
                if cancel_event is not None and cancel_event.is_set():
                    discarded = True
                    await self._pool.discard(conn, reason="cancel")
                    return SynthesizeResult(bytes(pcm_buf), tuple(all_words), task_id, alignment_status, True)
                await conn.ws.send(
                    json.dumps(build_continue_text(task_id, text), ensure_ascii=False)
                )

            await conn.ws.send(json.dumps(build_finish_task(task_id), ensure_ascii=False))

            loop = asyncio.get_running_loop()
            first_audio_deadline = loop.time() + self._config.first_audio_timeout_s
            total_deadline = loop.time() + self._config.total_timeout_s
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    discarded = True
                    await self._pool.discard(conn, reason="cancel")
                    return SynthesizeResult(bytes(pcm_buf), tuple(all_words), task_id, alignment_status, True)
                deadline = total_deadline
                if not pcm_buf:
                    deadline = min(deadline, first_audio_deadline)
                try:
                    msg = await asyncio.wait_for(
                        conn.ws.recv(),
                        timeout=max(0.0, deadline - loop.time()),
                    )
                except TimeoutError:
                    if not pcm_buf:
                        raise CosyVoiceFirstAudioTimeoutError() from None
                    raise
                if isinstance(msg, bytes):
                    if not pcm_buf:
                        self.trace(
                            "cosyvoice_first_pcm",
                            detail={"pcm_bytes": len(msg)},
                        )
                    pcm_buf.extend(msg)
                    sentence_pcm.setdefault(current_index, bytearray()).extend(msg)
                    continue
                ev = parse_server_message(msg)
                if ev.event == "sentence-begin" and ev.sentence_index is not None:
                    current_index = ev.sentence_index
                    sentence_pcm.setdefault(current_index, bytearray())
                elif ev.event == "sentence-end":
                    idx = ev.sentence_index or 0
                    raw_pcm = bytes(sentence_pcm.get(idx, b""))
                    dur = pcm_duration_ms(raw_pcm, sample_rate=self._config.sample_rate)
                    scaled, status = scale_word_timestamps(
                        ev.words, pcm_duration_ms_value=dur, offset_ms=offset_ms
                    )
                    if status == "degraded":
                        alignment_status = "degraded"
                    elif status == "scaled" and alignment_status == "ok":
                        alignment_status = "scaled"
                    all_words.extend(scaled)
                    if scaled and len(all_words) == len(scaled):
                        self.trace(
                            "cosyvoice_first_word_timestamps",
                            detail={"word_count": len(scaled)},
                        )
                    offset_ms += dur
                elif ev.event == "task-finished":
                    self.trace("cosyvoice_task_finished")
                    break
                elif ev.event == "task-failed":
                    self.trace(
                        "cosyvoice_task_failed",
                        status="error",
                        detail={"phase": "stream"},
                    )
                    await self._pool.discard(conn, reason="task-failed")
                    raise RuntimeError(ev.error_message or "task-failed")

            if not pcm_buf:
                raise CosyVoiceFirstAudioTimeoutError()
            if not all_words:
                raise CosyVoiceTimestampError("CosyVoice returned no word timestamps")
            if not discarded:
                await self._pool.release(conn)
            return SynthesizeResult(bytes(pcm_buf), tuple(all_words), task_id, alignment_status, discarded)
        except asyncio.CancelledError:
            await self._pool.discard(conn, reason="cancel")
            raise
        except Exception:
            if cancel_event is not None and cancel_event.is_set():
                await self._pool.discard(conn, reason="cancel")
                return SynthesizeResult(
                    bytes(pcm_buf), tuple(all_words), task_id, alignment_status, True
                )
            await self._pool.discard(conn, reason="error")
            raise
        finally:
            if cancel_watcher is not None:
                cancel_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_watcher

    async def aclose(self) -> None:
        await self._pool.aclose()
