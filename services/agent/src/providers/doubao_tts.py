"""LiveKit adapter for Volcengine Doubao bidirectional streaming TTS."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import websockets
from livekit.agents import APIConnectionError, APIConnectOptions, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, TimedString
from websockets.asyncio.client import ClientConnection

from services.agent.src.config import validate_doubao_auth
from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.providers.doubao_protocol import (
    EventType,
    MessageType,
    ServerMessage,
    align_subtitle_words,
    build_client_message,
    build_start_session_payload,
    build_task_request_payload,
    parse_server_message,
    parse_subtitle_words,
    pcm_duration_ms,
)
from services.agent.src.providers.doubao_voice_catalog import (
    DEFAULT_VOICE_PROFILE,
    DOUBAO_TTS_MODEL,
    catalog_by_id,
    resolve_approved_voice,
)
from services.agent.src.providers.reliability import CircuitBreaker

logger = logging.getLogger(__name__)
DEFAULT_WS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"


def _valid_websocket_url(value: str, *, require_tls: bool) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    schemes = {"wss"} if require_tls else {"ws", "wss"}
    return (
        parsed.scheme in schemes
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and "#" not in value
    )


@dataclass
class DoubaoTTSConfig:
    ws_url: str = DEFAULT_WS_URL
    api_key: str = field(default="", repr=False)
    app_id: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    resource_id: str = DOUBAO_TTS_MODEL
    voice_profile: str = DEFAULT_VOICE_PROFILE
    speaker: str = ""
    sample_rate: int = 24000
    speech_rate: int = 0
    loudness_rate: int = 0
    pitch: int = 0
    pool_size: int = 4
    connect_timeout_s: float = 5.0
    first_audio_timeout_s: float = 1.5
    total_timeout_s: float = 20.0
    instruction: str | None = None

    def __post_init__(self) -> None:
        if not _valid_websocket_url(self.ws_url, require_tls=False):
            raise ValueError(
                "Doubao TTS WebSocket URL must use ws/wss without userinfo or fragment"
            )
        if self.sample_rate != 24000:
            raise ValueError("Memoria requires Doubao PCM at 24000 Hz")
        if not -50 <= self.speech_rate <= 100:
            raise ValueError("Doubao speech_rate must be between -50 and 100")
        if not -50 <= self.loudness_rate <= 100:
            raise ValueError("Doubao loudness_rate must be between -50 and 100")
        if not -12 <= self.pitch <= 12:
            raise ValueError("Doubao pitch must be between -12 and 12")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DoubaoTTSConfig:
        import os

        values = dict(os.environ) if env is None else env
        profile = values.get("DOUBAO_TTS_VOICE_PROFILE", DEFAULT_VOICE_PROFILE)
        registry_raw = values.get("DOUBAO_TTS_VOICE_REGISTRY")
        registry_path = Path(registry_raw) if registry_raw else None
        approved = resolve_approved_voice(
            profile_id=profile,
            model=values.get("DOUBAO_TTS_RESOURCE_ID", DOUBAO_TTS_MODEL),
            registry_path=registry_path,
        )
        explicit = values.get("DOUBAO_TTS_SPEAKER", "").strip()
        production = values.get("ENVIRONMENT", "development").lower() == "production"
        if production and (approved is None or explicit not in {"", approved}):
            raise ValueError("production Doubao TTS requires an approved companion voice")
        speaker = explicit or approved or ""
        if not speaker:
            raise ValueError("Doubao TTS speaker is not configured")
        api_key = values.get("DOUBAO_TTS_API_KEY", "").strip()
        app_id = values.get("DOUBAO_TTS_APP_ID", "").strip()
        access_token = values.get("DOUBAO_TTS_ACCESS_TOKEN", "").strip()
        mock_url = values.get("DOUBAO_TTS_MOCK_WS_URL", "").strip()
        validate_doubao_auth(
            api_key=api_key,
            app_id=app_id,
            access_token=access_token,
            required=not mock_url,
        )
        ws_url = mock_url or values.get("DOUBAO_TTS_WS_URL", DEFAULT_WS_URL)
        if not _valid_websocket_url(ws_url, require_tls=production):
            raise ValueError(
                "Doubao TTS WebSocket URL must use wss in production and contain no userinfo "
                "or fragment"
            )
        return cls(
            ws_url=ws_url,
            api_key=api_key,
            app_id=app_id,
            access_token=access_token,
            resource_id=values.get("DOUBAO_TTS_RESOURCE_ID", DOUBAO_TTS_MODEL),
            voice_profile=profile,
            speaker=speaker,
            sample_rate=int(values.get("DOUBAO_TTS_SAMPLE_RATE", "24000")),
            speech_rate=int(values.get("DOUBAO_TTS_SPEECH_RATE", "0")),
            loudness_rate=int(values.get("DOUBAO_TTS_LOUDNESS_RATE", "0")),
            pitch=int(values.get("DOUBAO_TTS_PITCH", "0")),
            pool_size=int(values.get("DOUBAO_TTS_POOL_SIZE", "4")),
            connect_timeout_s=float(values.get("DOUBAO_TTS_CONNECT_TIMEOUT_S", "5")),
            first_audio_timeout_s=float(values.get("DOUBAO_TTS_FIRST_AUDIO_TIMEOUT_S", "1.5")),
            total_timeout_s=float(values.get("DOUBAO_TTS_TOTAL_TIMEOUT_S", "20")),
        )

    def auth_headers(self, *, connect_id: str) -> dict[str, str]:
        headers = {
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Connect-Id": connect_id,
        }
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        else:
            headers.update(
                {
                    "X-Api-App-Id": self.app_id,
                    "X-Api-Access-Key": self.access_token,
                    "X-Api-Request-Id": str(uuid.uuid4()),
                }
            )
        return headers


@dataclass
class PooledConnection:
    ws: ClientConnection
    conn_id: str
    in_use: bool = False
    burst: bool = False
    failed: bool = False
    closed: bool = False
    session_id: str = ""
    cancel_sent: bool = False


@dataclass(frozen=True, slots=True)
class SynthesizeResult:
    pcm: bytes
    words: tuple[TimedWord, ...]
    task_id: str
    alignment_status: str
    discarded: bool = False


class DoubaoBeforeAudioError(RuntimeError):
    pass


class DoubaoFirstAudioTimeoutError(TimeoutError):
    pass


class DoubaoTimestampError(RuntimeError):
    pass


class DoubaoPCMContinuityError(ValueError):
    pass


class _PcmContinuityGuard:
    def __init__(self) -> None:
        self._pending = bytearray()
        self.pcm_bytes = 0
        self.chunk_count = 0
        self.odd_chunk_count = 0
        self.min_chunk_bytes: int | None = None
        self.max_chunk_bytes = 0

    def add(self, payload: bytes) -> bytes:
        size = len(payload)
        self.pcm_bytes += size
        self.chunk_count += 1
        self.odd_chunk_count += size % 2
        self.min_chunk_bytes = (
            size if self.min_chunk_bytes is None else min(self.min_chunk_bytes, size)
        )
        self.max_chunk_bytes = max(self.max_chunk_bytes, size)
        self._pending.extend(payload)
        even_size = len(self._pending) & ~1
        if not even_size:
            return b""
        emitted = bytes(self._pending[:even_size])
        del self._pending[:even_size]
        return emitted

    def finish(self) -> None:
        if self._pending:
            raise DoubaoPCMContinuityError(
                f"Doubao PCM total length is odd ({self.pcm_bytes} bytes)"
            )

    def summary(self) -> dict[str, int]:
        return {
            "pcm_bytes": self.pcm_bytes,
            "chunk_count": self.chunk_count,
            "odd_chunk_count": self.odd_chunk_count,
            "min_chunk_bytes": self.min_chunk_bytes or 0,
            "max_chunk_bytes": self.max_chunk_bytes,
        }


def _error_message(message: ServerMessage) -> str:
    if not message.payload:
        return f"Doubao error {message.error_code or message.event}"
    try:
        value = message.json_payload()
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return message.payload.decode("utf-8", "replace")
    return str(value.get("message") or value.get("status_text") or value)


class DoubaoTTSPool:
    """Reusable connected sockets; canceled or failed sessions are discarded."""

    def __init__(self, config: DoubaoTTSConfig, metrics: MetricsRegistry | None = None) -> None:
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
        for _ in range(size if size is not None else self.config.pool_size):
            async with self._lock:
                await self._available.put(await self._open())
        self.metrics.set_tts_pool_available(self.available_approx)

    async def _open(self) -> PooledConnection:
        if self._closing:
            raise RuntimeError("Doubao TTS pool is closing")
        self._breaker.before_request()
        connect_id = str(uuid.uuid4())
        ws: ClientConnection | None = None
        try:
            ws = await websockets.connect(
                self.config.ws_url,
                additional_headers=self.config.auth_headers(connect_id=connect_id),
                open_timeout=self.config.connect_timeout_s,
                max_size=10 * 1024 * 1024,
            )
            await ws.send(build_client_message(EventType.START_CONNECTION))
            raw = await asyncio.wait_for(ws.recv(), timeout=self.config.connect_timeout_s)
            if not isinstance(raw, bytes):
                raise ValueError("Doubao returned a text WebSocket frame")
            started = parse_server_message(raw)
            if started.event != EventType.CONNECTION_STARTED:
                raise APIConnectionError(_error_message(started))
        except asyncio.CancelledError:
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            raise
        except Exception:
            self._breaker.record_failure()
            if ws is not None:
                with contextlib.suppress(Exception):
                    await ws.close()
            raise
        self._breaker.record_success()
        conn = PooledConnection(ws=ws, conn_id=connect_id)
        self._all[connect_id] = conn
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
                    raise RuntimeError("Doubao TTS pool is closing")
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
        conn.session_id = ""
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
            await conn.ws.send(build_client_message(EventType.FINISH_CONNECTION))
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
        if reason == "cancel" and conn.session_id:
            try:
                await conn.ws.send(
                    build_client_message(
                        EventType.CANCEL_SESSION,
                        session_id=conn.session_id,
                    )
                )
            except Exception:
                logger.warning("Doubao CancelSession send failed", exc_info=True)
            else:
                conn.cancel_sent = True
        elif reason == "shutdown" and not conn.session_id:
            with contextlib.suppress(Exception):
                await conn.ws.send(build_client_message(EventType.FINISH_CONNECTION))
        if reason not in {"cancel", "shutdown"}:
            self._breaker.record_failure()
        self._all.pop(conn.conn_id, None)
        conn.session_id = ""
        self.metrics.set_tts_pool_available(self.available_approx)
        with contextlib.suppress(Exception):
            await conn.ws.close()
        if not self._closing:
            task = asyncio.create_task(self._refill_one(), name="doubao-tts-pool-refill")
            self._refill_tasks.add(task)
            task.add_done_callback(self._refill_tasks.discard)

    async def _refill_one(self) -> None:
        async with self._lock:
            if self._closing or len(self._all) >= self.config.pool_size:
                return
            with contextlib.suppress(Exception):
                conn = await self._open()
                if self._closing:
                    await self.discard(conn, reason="shutdown")
                    return
                await self._available.put(conn)
                self.metrics.set_tts_pool_available(self.available_approx)

    def bind_active(
        self,
        fence: GenerationFence,
        conn: PooledConnection,
        *,
        session_id: str,
    ) -> None:
        conn.session_id = session_id
        self.active_by_fence[self._fence_key(fence)] = conn

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        conn = self.active_by_fence.pop(self._fence_key(fence), None)
        if conn is not None:
            await self.discard(conn, reason="cancel")

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


class DoubaoSynthesizeStream(tts.SynthesizeStream):
    def __init__(
        self,
        *,
        tts_instance: DoubaoTTS,
        config: DoubaoTTSConfig,
        pool: DoubaoTTSPool,
        conn_options: APIConnectOptions,
        fence: GenerationFence | None,
    ) -> None:
        super().__init__(tts=tts_instance, conn_options=conn_options)
        self._tts_instance = tts_instance
        self._config = config
        self._pool = pool
        self._fence = fence
        self._conn: PooledConnection | None = None

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        conn = await self._pool.acquire()
        self._conn = conn
        session_id = str(uuid.uuid4())
        if self._fence is not None:
            self._pool.bind_active(self._fence, conn, session_id=session_id)
        else:
            conn.session_id = session_id
        self._tts_instance._report_alignment(self._fence, session_id, "started")
        got_audio = False
        got_words = False
        pcm_guard = _PcmContinuityGuard()
        subtitle_words: list[TimedWord] = []
        output_emitter.initialize(
            request_id=session_id,
            sample_rate=self._config.sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
            frame_size_ms=20,
            stream=True,
        )
        try:
            await conn.ws.send(
                build_client_message(
                    EventType.START_SESSION,
                    session_id=session_id,
                    payload=build_start_session_payload(
                        speaker=self._config.speaker,
                        sample_rate=self._config.sample_rate,
                        speech_rate=self._config.speech_rate,
                        loudness_rate=self._config.loudness_rate,
                        pitch=self._config.pitch,
                        context_texts=(self._config.instruction,)
                        if self._config.instruction
                        else (),
                        uid=str(uuid.uuid4()),
                    ),
                )
            )
            started = await self._receive(conn, timeout_s=self._config.connect_timeout_s)
            if started.event != EventType.SESSION_STARTED:
                raise DoubaoBeforeAudioError(_error_message(started))
            self._tts_instance.trace("doubao_session_started")
            output_emitter.start_segment(segment_id=session_id)

            async def send_task_request(data: Any) -> bool:
                if isinstance(data, self._FlushSentinel):
                    return False
                text = str(data)
                if not text.strip():
                    return False
                await conn.ws.send(
                    build_client_message(
                        EventType.TASK_REQUEST,
                        session_id=session_id,
                        payload=build_task_request_payload(text),
                    )
                )
                return True

            async for first_data in self._input_ch:
                if await send_task_request(first_data):
                    break
            else:
                raise DoubaoBeforeAudioError("empty-input")

            async def sender() -> None:
                async for data in self._input_ch:
                    await send_task_request(data)
                await conn.ws.send(
                    build_client_message(EventType.FINISH_SESSION, session_id=session_id)
                )

            send_task = asyncio.create_task(sender(), name="doubao-tts-send")
            loop = asyncio.get_running_loop()
            first_audio_deadline = loop.time() + self._config.first_audio_timeout_s
            total_deadline = loop.time() + self._config.total_timeout_s
            try:
                while True:
                    deadline = (
                        total_deadline if got_audio else min(total_deadline, first_audio_deadline)
                    )
                    try:
                        message = await self._receive(
                            conn,
                            timeout_s=max(0.0, deadline - loop.time()),
                        )
                    except TimeoutError:
                        reason = "total-timeout" if got_audio else "first-audio-timeout"
                        raise APIConnectionError(reason) from None
                    if message.message_type == MessageType.AUDIO_ONLY_SERVER:
                        if not got_audio:
                            self._tts_instance.trace(
                                "doubao_first_pcm",
                                detail={"pcm_bytes": len(message.payload)},
                            )
                        got_audio = True
                        aligned_pcm = pcm_guard.add(message.payload)
                        if aligned_pcm:
                            output_emitter.push(aligned_pcm)
                    elif message.event == EventType.TTS_SUBTITLE:
                        words = parse_subtitle_words(message.payload)
                        if words:
                            if not got_words:
                                self._tts_instance.trace(
                                    "doubao_first_word_timestamps",
                                    detail={"word_count": len(words)},
                                )
                            got_words = True
                            subtitle_words.extend(words)
                    elif message.event == EventType.SESSION_FINISHED:
                        break
                    elif (
                        message.event
                        in {
                            EventType.SESSION_FAILED,
                            EventType.CONNECTION_FAILED,
                        }
                        or message.message_type == MessageType.ERROR
                    ):
                        raise APIConnectionError(_error_message(message))
            finally:
                send_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await send_task
            if not got_audio or not got_words:
                reason = "empty-audio" if not got_audio else "empty-timestamps"
                raise APIConnectionError(reason)
            pcm_guard.finish()
            aligned_words, alignment_status = align_subtitle_words(
                tuple(subtitle_words),
                pcm_duration_ms_value=int(
                    (pcm_guard.pcm_bytes // 2) * 1000 / self._config.sample_rate
                ),
            )
            if not aligned_words:
                raise APIConnectionError("empty-timestamps")
            self._tts_instance._report_alignment(
                self._fence,
                session_id,
                alignment_status,
            )
            output_emitter.push_timed_transcript(
                [
                    TimedString(
                        word.text + (word.punctuation or ""),
                        start_time=word.begin_ms / 1000,
                        end_time=word.end_ms / 1000,
                    )
                    for word in aligned_words
                ]
            )
            output_emitter.end_segment()
            await self._pool.release(conn)
            self._conn = None
        except asyncio.CancelledError:
            if self._conn is not None:
                await self._pool.discard(self._conn, reason="cancel")
                self._conn = None
            raise
        except Exception:
            if self._conn is not None:
                await self._pool.discard(self._conn, reason="error")
                self._conn = None
            raise
        finally:
            self._tts_instance.trace("doubao_pcm_summary", detail=pcm_guard.summary())

    @staticmethod
    async def _receive(conn: PooledConnection, *, timeout_s: float) -> ServerMessage:
        raw = await asyncio.wait_for(conn.ws.recv(), timeout=timeout_s)
        if not isinstance(raw, bytes):
            raise ValueError("Doubao returned a text WebSocket frame")
        return parse_server_message(raw)


class DoubaoTTS(tts.TTS[Any]):
    def __init__(self, config: DoubaoTTSConfig, pool: DoubaoTTSPool | None = None) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True, aligned_transcript=True),
            sample_rate=config.sample_rate,
            num_channels=1,
        )
        self._config = config
        self._baseline_profile = config.voice_profile
        self._baseline_speaker = config.speaker
        self._pool = pool or DoubaoTTSPool(config)
        self._active_fence: GenerationFence | None = None
        self._trace_callback: Callable[[str, str, dict[str, Any] | None], None] | None = None
        self._alignment_callback: Callable[[GenerationFence, str, str], None] | None = None

    @classmethod
    def from_env(cls) -> DoubaoTTS:
        config = DoubaoTTSConfig.from_env()
        return cls(config, DoubaoTTSPool(config))

    @property
    def provider(self) -> str:
        return "volcengine_doubao"

    @property
    def model(self) -> str:
        return self._config.resource_id

    @property
    def pool(self) -> DoubaoTTSPool:
        return self._pool

    def bind_fence(self, fence: GenerationFence) -> None:
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
            logger.warning("Doubao alignment callback failed", exc_info=True)

    def apply_speech_plan(self, *, emotion: str, rate: float) -> None:
        # Real TTS 2.0 probes showed context_texts could shift subtitle timing by
        # more than 1.4 s. Ignore emotion instructions for now and keep reliable
        # alignment; the reviewed native timbre carries the companion character.
        self._config.instruction = None
        clamped = min(1.05, max(0.95, rate))
        self._config.speech_rate = round((clamped - 1.0) * 100)

    def apply_voice_profile(self, *, model: str, voice: str) -> None:
        spec = next(
            (item for item in catalog_by_id().values() if item.speaker_id == voice),
            None,
        )
        if model != DOUBAO_TTS_MODEL or spec is None:
            raise ValueError("active runtime voice must be an approved Doubao TTS 2.0 voice")
        self._config.resource_id = model
        self._config.voice_profile = spec.profile_id
        self._config.speaker = voice

    def use_baseline_voice(self) -> None:
        self._config.resource_id = DOUBAO_TTS_MODEL
        self._config.voice_profile = self._baseline_profile
        self._config.speaker = self._baseline_speaker

    @property
    def current_model(self) -> str:
        return self._config.resource_id

    @property
    def current_voice(self) -> str:
        return self._config.speaker

    @property
    def current_instruction(self) -> str | None:
        return self._config.instruction

    @property
    def current_rate(self) -> float:
        rate = self._config.speech_rate
        return 1.0 + rate / 100

    def trace(
        self,
        name: str,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        if self._trace_callback is not None:
            self._trace_callback(name, status, detail)

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return self._synthesize_with_stream(text, conn_options=conn_options)

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> DoubaoSynthesizeStream:
        return DoubaoSynthesizeStream(
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
        config = replace(self._config)
        for attempt in range(2):
            try:
                return await self._synthesize_once(
                    texts,
                    fence=fence,
                    cancel_event=cancel_event,
                    config=config,
                )
            except (DoubaoBeforeAudioError, DoubaoFirstAudioTimeoutError, DoubaoTimestampError):
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    async def _synthesize_once(
        self,
        texts: list[str],
        *,
        fence: GenerationFence,
        cancel_event: asyncio.Event | None,
        config: DoubaoTTSConfig,
    ) -> SynthesizeResult:
        conn = await self._pool.acquire()
        session_id = str(uuid.uuid4())
        self._pool.bind_active(fence, conn, session_id=session_id)
        pcm_guard = _PcmContinuityGuard()
        pcm = bytearray()
        words: list[TimedWord] = []
        cancel_watcher: asyncio.Task[None] | None = None
        if cancel_event is not None:

            async def watch_cancel() -> None:
                await cancel_event.wait()
                await self._pool.discard(conn, reason="cancel")

            cancel_watcher = asyncio.create_task(watch_cancel(), name="doubao-tts-cancel-watch")

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
            await conn.ws.send(
                build_client_message(
                    EventType.START_SESSION,
                    session_id=session_id,
                    payload=build_start_session_payload(
                        speaker=config.speaker,
                        sample_rate=config.sample_rate,
                        speech_rate=config.speech_rate,
                        loudness_rate=config.loudness_rate,
                        pitch=config.pitch,
                        context_texts=(config.instruction,) if config.instruction else (),
                        uid=str(uuid.uuid4()),
                    ),
                )
            )
            started = await DoubaoSynthesizeStream._receive(
                conn,
                timeout_s=config.connect_timeout_s,
            )
            if started.event != EventType.SESSION_STARTED:
                raise DoubaoBeforeAudioError(_error_message(started))
            self.trace("doubao_session_started")
            for text in texts:
                if cancel_event is not None and cancel_event.is_set():
                    return SynthesizeResult(bytes(pcm), tuple(words), session_id, "degraded", True)
                if text:
                    await conn.ws.send(
                        build_client_message(
                            EventType.TASK_REQUEST,
                            session_id=session_id,
                            payload=build_task_request_payload(text),
                        )
                    )
            await conn.ws.send(
                build_client_message(EventType.FINISH_SESSION, session_id=session_id)
            )
            loop = asyncio.get_running_loop()
            first_audio_deadline = loop.time() + config.first_audio_timeout_s
            total_deadline = loop.time() + config.total_timeout_s
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return SynthesizeResult(bytes(pcm), tuple(words), session_id, "degraded", True)
                deadline = total_deadline if pcm else min(total_deadline, first_audio_deadline)
                try:
                    message = await DoubaoSynthesizeStream._receive(
                        conn,
                        timeout_s=max(0.0, deadline - loop.time()),
                    )
                except TimeoutError:
                    if not pcm:
                        raise DoubaoFirstAudioTimeoutError() from None
                    raise
                if message.message_type == MessageType.AUDIO_ONLY_SERVER:
                    if not pcm:
                        self.trace("doubao_first_pcm", detail={"pcm_bytes": len(message.payload)})
                    pcm.extend(pcm_guard.add(message.payload))
                elif message.event == EventType.TTS_SUBTITLE:
                    subtitle = parse_subtitle_words(message.payload)
                    if subtitle and not words:
                        self.trace(
                            "doubao_first_word_timestamps",
                            detail={"word_count": len(subtitle)},
                        )
                    words.extend(subtitle)
                elif message.event == EventType.SESSION_FINISHED:
                    break
                elif (
                    message.event
                    in {
                        EventType.SESSION_FAILED,
                        EventType.CONNECTION_FAILED,
                    }
                    or message.message_type == MessageType.ERROR
                ):
                    if not pcm:
                        raise DoubaoBeforeAudioError(_error_message(message))
                    raise RuntimeError(_error_message(message))
            pcm_guard.finish()
            if not pcm:
                raise DoubaoFirstAudioTimeoutError()
            aligned, status = align_subtitle_words(
                tuple(words),
                pcm_duration_ms_value=pcm_duration_ms(bytes(pcm), sample_rate=config.sample_rate),
            )
            if not aligned:
                raise DoubaoTimestampError("Doubao returned no word timestamps")
            if cancel_event is not None and cancel_event.is_set():
                await settle_cancel_watcher(cancellation_requested=True)
                return SynthesizeResult(bytes(pcm), tuple(words), session_id, "degraded", True)
            await settle_cancel_watcher(cancellation_requested=False)
            await self._pool.release(conn)
            return SynthesizeResult(bytes(pcm), aligned, session_id, status)
        except asyncio.CancelledError:
            await self._pool.discard(conn, reason="cancel")
            raise
        except Exception as exc:
            if cancel_event is not None and cancel_event.is_set():
                await self._pool.discard(conn, reason="cancel")
                return SynthesizeResult(bytes(pcm), tuple(words), session_id, "degraded", True)
            await self._pool.discard(conn, reason="error")
            if not pcm and not isinstance(
                exc,
                (
                    DoubaoBeforeAudioError,
                    DoubaoFirstAudioTimeoutError,
                    DoubaoPCMContinuityError,
                ),
            ):
                raise DoubaoBeforeAudioError(str(exc)) from exc
            raise
        finally:
            self.trace("doubao_pcm_summary", detail=pcm_guard.summary())
            await settle_cancel_watcher(
                cancellation_requested=cancel_event is not None and cancel_event.is_set()
            )

    async def aclose(self) -> None:
        await self._pool.aclose()
