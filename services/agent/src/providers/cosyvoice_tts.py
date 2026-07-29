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
    parse_server_message,
    pcm_duration_ms,
    scale_word_timestamps,
)
from services.agent.src.providers.cosyvoice_voice_catalog import (
    DEFAULT_VOICE_PROFILE,
    catalog_by_id,
    resolve_approved_designed_voice,
    resolve_voice_id,
    uses_freeform_instruct,
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
    # Leave true-peak headroom before Opus encoding. The designed v3.5 voices
    # clipped at 55+ in production probes; 45 measured at about -1 dBFS.
    volume: int = 45
    word_timestamps: bool = True
    pool_size: int = 4
    connect_timeout_s: float = 5.0
    first_audio_timeout_s: float = 1.5
    total_timeout_s: float = 20.0
    instruction: str | None = None
    # auto | fixed | freeform — auto picks freeform for v3.5 / designed voices.
    instruct_style: str = "auto"
    voice_profile: str = DEFAULT_VOICE_PROFILE

    def __post_init__(self) -> None:
        if self.instruction is None:
            return
        if self.uses_freeform_instruct:
            # Freeform: only length-ish sanity; Alibaba limit is model-side.
            if len(self.instruction) > 200:
                raise ValueError("CosyVoice freeform instruction is too long")
            return
        if self.voice != "longanyang":
            return
        allowed = {cosyvoice_instruction(emotion, freeform=False) for emotion in COSYVOICE_EMOTIONS}
        if self.instruction not in allowed:
            raise ValueError("longanyang requires Alibaba's fixed Instruct format")

    @property
    def uses_freeform_instruct(self) -> bool:
        style = (
            self.instruct_style if self.instruct_style in {"auto", "fixed", "freeform"} else "auto"
        )
        return uses_freeform_instruct(
            model=self.model,
            voice=self.voice,
            instruct_style=style,  # type: ignore[arg-type]
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CosyVoiceConfig:
        import os

        e = env or dict(os.environ)
        from pathlib import Path

        model = e.get("COSYVOICE_MODEL", "cosyvoice-v3.5-flash")
        profile = e.get("COSYVOICE_VOICE_PROFILE", DEFAULT_VOICE_PROFILE)
        explicit = e.get("COSYVOICE_VOICE") or None
        registry_raw = e.get("COSYVOICE_VOICE_REGISTRY")
        registry_path = Path(registry_raw) if registry_raw else None
        production = e.get("ENVIRONMENT", "development").lower() == "production"
        resolved: str | None
        if production and "v3.5" in model:
            approved = resolve_approved_designed_voice(
                profile_id=profile,
                model=model,
                registry_path=registry_path,
            )
            if approved is None or (explicit is not None and explicit not in {profile, approved}):
                raise ValueError(
                    "production CosyVoice requires an approved designed baseline voice"
                )
            resolved = approved
        elif production and model == "cosyvoice-v3-flash":
            if explicit not in {None, "longanyang"}:
                raise ValueError(
                    "production CosyVoice requires an approved designed baseline voice"
                )
            resolved = "longanyang"
        else:
            # Prefer explicit voice id; else resolve designed profile registry;
            # else keep longanyang for v3 system-voice mainline.
            resolved = resolve_voice_id(
                profile_id=profile,
                explicit_voice=explicit,
                registry_path=registry_path,
            )
        profile_names = catalog_by_id()
        if resolved:
            voice = resolved
        elif explicit and explicit not in profile_names:
            # Real vendor voice_id (system or designed), not a catalog key.
            voice = explicit
        elif "v3.5" in model:
            raise ValueError(
                "cosyvoice-v3.5 requires a designed voice_id. "
                "Run scripts/design_cosyvoice_voices.py then set "
                "COSYVOICE_VOICE or COSYVOICE_VOICE_PROFILE + registry."
            )
        else:
            voice = "longanyang"
        return cls(
            api_key=e.get("DASHSCOPE_API_KEY", ""),
            ws_url=e.get("COSYVOICE_MOCK_WS_URL") or e.get("DASHSCOPE_WS_URL", ""),
            model=model,
            voice=voice,
            sample_rate=int(e.get("COSYVOICE_SAMPLE_RATE", "24000")),
            rate=float(e.get("COSYVOICE_RATE", "1.0")),
            pitch=float(e.get("COSYVOICE_PITCH", "1.0")),
            volume=int(e.get("COSYVOICE_VOLUME", "45")),
            word_timestamps=e.get("COSYVOICE_WORD_TIMESTAMPS", "true").lower() == "true",
            pool_size=int(e.get("COSYVOICE_POOL_SIZE", "4")),
            connect_timeout_s=float(e.get("COSYVOICE_CONNECT_TIMEOUT_S", "5")),
            first_audio_timeout_s=float(e.get("COSYVOICE_FIRST_AUDIO_TIMEOUT_S", "1.5")),
            total_timeout_s=float(e.get("COSYVOICE_TOTAL_TIMEOUT_S", "20")),
            instruction=e.get("COSYVOICE_INSTRUCTION") or None,
            instruct_style=e.get("COSYVOICE_INSTRUCT_STYLE", "auto"),
            voice_profile=profile,
        )


@dataclass
class PooledConnection:
    ws: ClientConnection
    conn_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    in_use: bool = False
    burst: bool = False
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


class CosyVoiceBeforeAudioError(RuntimeError):
    """Raised when a task fails before any PCM can reach the caller."""


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
            async with self._lock:
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
        except TimeoutError:
            conn = None
        if conn is None or conn.closed or conn.failed:
            async with self._lock:
                if self._closing:
                    raise RuntimeError("CosyVoice pool is closing")
                while True:
                    try:
                        conn = self._available.get_nowait()
                    except asyncio.QueueEmpty:
                        conn = await self._open()
                        conn.burst = len(self._all) > self.config.pool_size
                        break
                    if not conn.closed and not conn.failed:
                        break
        conn.in_use = True
        self.metrics.set_tts_pool_available(self.available_approx)
        return conn

    async def release(self, conn: PooledConnection) -> None:
        self._unbind_connection(conn)
        async with self._lock:
            if self._closing:
                return
            should_discard = conn.failed or conn.closed
            if not should_discard and not (conn.burst and len(self._all) > self.config.pool_size):
                conn.burst = False
                conn.in_use = False
                self._breaker.record_success()
                await self._available.put(conn)
                self.metrics.set_tts_pool_available(self.available_approx)
                return
            conn.in_use = False
        if should_discard:
            await self.discard(conn, reason="failed_or_closed")
            return
        with contextlib.suppress(Exception):
            await conn.ws.close()
        conn.closed = True
        self._all.pop(conn.conn_id, None)
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
        return f"{fence.session_id}:{fence.turn_id}:{fence.generation_id}:{fence.tool_epoch}"

    def _unbind_connection(self, conn: PooledConnection) -> None:
        for key, active in tuple(self.active_by_fence.items()):
            if active is conn:
                self.active_by_fence.pop(key, None)

    @property
    def available_approx(self) -> int:
        return self._available.qsize()

    async def aclose(self) -> None:
        self._closing = True
        refill_tasks = tuple(self._refill_tasks)
        for task in refill_tasks:
            task.cancel()
        if refill_tasks:
            await asyncio.gather(*refill_tasks, return_exceptions=True)
        self._refill_tasks.clear()
        async with self._lock:
            connections = list(self._all.values())
            self.active_by_fence.clear()
            while not self._available.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._available.get_nowait()
        for conn in connections:
            await self.discard(conn, reason="shutdown")
        self.metrics.set_tts_pool_available(0)


class CosyVoiceSynthesizeStream(tts.SynthesizeStream):
    """LiveKit streaming synthesize; cancel closes CosyVoice WS and discards from pool."""

    def __init__(
        self,
        *,
        tts_instance: CosyVoiceTTS,
        config: CosyVoiceConfig,
        fallback_config: CosyVoiceConfig,
        pool: CosyVoicePool,
        conn_options: APIConnectOptions,
        fence: GenerationFence | None = None,
    ) -> None:
        super().__init__(tts=tts_instance, conn_options=conn_options)
        self._tts_instance = tts_instance
        self._config = config
        self._fallback_config = fallback_config
        self._baseline_attempted = False
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
        self._tts_instance._report_alignment(self._fence, task_id, "started")
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
                    deadline = (
                        total_deadline if got_audio else min(total_deadline, first_audio_deadline)
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
                        # WebSocket chunks are transport boundaries, not audio
                        # boundaries. Per-chunk gain changes create audible clicks.
                        output_emitter.push(msg)
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
                        self._tts_instance._report_alignment(self._fence, task_id, status)
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
        except Exception as exc:
            if self._conn is not None:
                await self._pool.discard(self._conn, reason="error")
                self._conn = None
            active_voice = (self._config.model, self._config.voice)
            fallback_voice = (self._fallback_config.model, self._fallback_config.voice)
            if not got_audio and not self._baseline_attempted and active_voice != fallback_voice:
                self._config = replace(self._fallback_config)
                self._baseline_attempted = True
                self._tts_instance.trace(
                    "cosyvoice_clone_fallback",
                    status="degraded",
                    detail={"reason": type(exc).__name__},
                )
                raise APIConnectionError(
                    "clone failed before audio; retrying approved baseline"
                ) from exc
            if self._baseline_attempted:
                raise APIConnectionError(str(exc), retryable=False) from exc
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
        self._baseline_model = config.model
        self._baseline_voice = config.voice
        self._pool = pool or CosyVoicePool(config)
        self._active_fence: GenerationFence | None = None
        self._trace_callback: Callable[[str, str, dict[str, Any] | None], None] | None = None
        self._alignment_callback: Callable[[GenerationFence, str, str], None] | None = None

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

    def set_alignment_callback(
        self,
        callback: Callable[[GenerationFence, str, str], None],
    ) -> None:
        """Observe LiveKit stream alignment without losing its generation/task identity."""
        self._alignment_callback = callback

    def _report_alignment(
        self,
        fence: GenerationFence | None,
        utterance_id: str,
        status: str,
    ) -> None:
        if fence is None or self._alignment_callback is None:
            return
        try:
            self._alignment_callback(fence, utterance_id, status)
        except Exception:
            logger.warning("CosyVoice alignment callback failed", exc_info=True)

    def apply_speech_plan(
        self,
        *,
        emotion: str,
        rate: float,
        instruction: str = "",
        pitch: int = 0,
        reference_contexts: tuple[str, ...] = (),
        fence: GenerationFence | None = None,
    ) -> None:
        del instruction, pitch, reference_contexts, fence
        if emotion not in COSYVOICE_EMOTIONS:
            emotion = "neutral"
        self._config.instruction = cosyvoice_instruction(
            emotion,
            freeform=self._config.uses_freeform_instruct,
        )
        # Prefer 1.0; still clamp defensive ranges if a caller passes outliers.
        self._config.rate = min(1.05, max(0.95, rate))

    def apply_voice_profile(self, *, model: str, voice: str) -> None:
        if not model.startswith("cosyvoice-v3.5-") or not voice.strip():
            raise ValueError("active voice profile must use CosyVoice v3.5")
        self._config.model = model
        self._config.voice = voice

    def use_baseline_voice(self) -> None:
        self._config.model = self._baseline_model
        self._config.voice = self._baseline_voice

    @property
    def current_model(self) -> str:
        return self._config.model

    @property
    def current_voice(self) -> str:
        return self._config.voice

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
        config = replace(self._config)
        return CosyVoiceSynthesizeStream(
            tts_instance=self,
            config=config,
            fallback_config=replace(
                config,
                model=self._baseline_model,
                voice=self._baseline_voice,
            ),
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
        config = replace(self._config)
        baseline = replace(
            config,
            model=self._baseline_model,
            voice=self._baseline_voice,
        )
        for attempt in range(2):
            try:
                return await self._synthesize_once(
                    texts,
                    fence=fence,
                    cancel_event=cancel_event,
                    config=config,
                )
            except (
                CosyVoiceBeforeAudioError,
                CosyVoiceFirstAudioTimeoutError,
                CosyVoiceTimestampError,
            ) as exc:
                if attempt == 1:
                    raise
                if (config.model, config.voice) != (baseline.model, baseline.voice):
                    config = baseline
                    self.trace(
                        "cosyvoice_clone_fallback",
                        status="degraded",
                        detail={"reason": type(exc).__name__},
                    )
        raise AssertionError("unreachable")

    async def _synthesize_once(
        self,
        texts: list[str],
        *,
        fence: GenerationFence,
        cancel_event: asyncio.Event | None = None,
        config: CosyVoiceConfig,
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

        async def settle_cancel_watcher(*, cancellation_requested: bool) -> None:
            nonlocal cancel_watcher
            if cancel_watcher is None:
                return
            if cancellation_requested:
                await cancel_watcher
            else:
                cancel_watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_watcher
            cancel_watcher = None

        try:
            run = build_run_task(
                task_id=task_id,
                model=config.model,
                voice=config.voice,
                sample_rate=config.sample_rate,
                rate=config.rate,
                pitch=config.pitch,
                volume=config.volume,
                word_timestamp_enabled=config.word_timestamps,
                instruction=config.instruction,
            )
            await conn.ws.send(json.dumps(run, ensure_ascii=False))

            # Wait task-started
            started = False
            while not started:
                if cancel_event is not None and cancel_event.is_set():
                    discarded = True
                    await self._pool.discard(conn, reason="cancel")
                    return SynthesizeResult(b"", (), task_id, "degraded", discarded=True)
                msg = await asyncio.wait_for(conn.ws.recv(), timeout=config.connect_timeout_s)
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
                    raise CosyVoiceBeforeAudioError(ev.error_message or "task-failed")

            for text in texts:
                if cancel_event is not None and cancel_event.is_set():
                    discarded = True
                    await self._pool.discard(conn, reason="cancel")
                    return SynthesizeResult(
                        bytes(pcm_buf), tuple(all_words), task_id, alignment_status, True
                    )
                await conn.ws.send(
                    json.dumps(build_continue_text(task_id, text), ensure_ascii=False)
                )

            await conn.ws.send(json.dumps(build_finish_task(task_id), ensure_ascii=False))

            loop = asyncio.get_running_loop()
            first_audio_deadline = loop.time() + config.first_audio_timeout_s
            total_deadline = loop.time() + config.total_timeout_s
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    discarded = True
                    await self._pool.discard(conn, reason="cancel")
                    return SynthesizeResult(
                        bytes(pcm_buf), tuple(all_words), task_id, alignment_status, True
                    )
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
                    dur = pcm_duration_ms(raw_pcm, sample_rate=config.sample_rate)
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
            if cancel_event is not None and cancel_event.is_set():
                await settle_cancel_watcher(cancellation_requested=True)
                return SynthesizeResult(
                    bytes(pcm_buf), tuple(all_words), task_id, alignment_status, True
                )
            await settle_cancel_watcher(cancellation_requested=False)
            if not discarded:
                await self._pool.release(conn)
            return SynthesizeResult(
                bytes(pcm_buf), tuple(all_words), task_id, alignment_status, discarded
            )
        except asyncio.CancelledError:
            await self._pool.discard(conn, reason="cancel")
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                await self._pool.discard(conn, reason="cancel")
                return SynthesizeResult(
                    bytes(pcm_buf), tuple(all_words), task_id, alignment_status, True
                )
            await self._pool.discard(conn, reason="error")
            if not pcm_buf and not isinstance(
                exc,
                (CosyVoiceBeforeAudioError, CosyVoiceFirstAudioTimeoutError),
            ):
                raise CosyVoiceBeforeAudioError(str(exc)) from exc
            raise
        finally:
            await settle_cancel_watcher(
                cancellation_requested=cancel_event is not None and cancel_event.is_set()
            )

    async def aclose(self) -> None:
        await self._pool.aclose()
