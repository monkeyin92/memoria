"""StepFun StepAudio 3 TTS WebSocket adapter.

Protocol source (checked 2026-10-20):
https://platform.stepfun.com/docs/zh/api-reference/audio/ws-audio.md

The documented order is ``tts.connection.done`` -> client ``tts.create`` ->
``tts.response.created`` -> client ``tts.text.delta`` -> server
``tts.response.audio.delta`` (base64 PCM) -> client ``tts.text.done`` ->
server ``tts.response.audio.done``. A connection carries one reply; a barge-in
or provider failure closes it. Error events are ``tts.response.error`` with
``data.code`` / ``data.message``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.protocol import State

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers.generation_budget import GenerationBudget
from services.agent.src.providers.provider_errors import (
    DEFAULT_API_CONNECT_OPTIONS,
    APIConnectOptions,
    APIError,
    APIStatusError,
)
from services.agent.src.providers.tts_stream import AudioEmitter, SynthesizeStream
from services.common.tts_defaults import (
    STEPFUN_PROVIDER,
    STEPFUN_TTS_MODEL,
    STEPFUN_TTS_VOICE,
)

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://api.stepfun.com/v1/realtime/audio"
STEPFUN_TEXT_DELTA_MAX_CHARS = 1000
STEPFUN_INSTRUCTION_MAX_CHARS = 500

#: Default delivery guard; StepFun treats parenthesised text as an inline
#: instruction and has no official laughter/stutter switch, so the delivery
#: guard below is the prompt-level control required by the acceptance criteria.
#: Deployment wording from STEPFUN_TTS_INSTRUCTION (the .env example carries
#: it). It is a delivery guard for the voice, never child-facing script text;
#: product-approved lines stay external. A blank value drops the guard.
STEPFUN_INSTRUCTION_GUARD = os.getenv("STEPFUN_TTS_INSTRUCTION", "").strip()


def _effective_instruction(instruction: str) -> str:
    plan_instruction = instruction.strip()
    if plan_instruction and STEPFUN_INSTRUCTION_GUARD:
        return f"{plan_instruction}。{STEPFUN_INSTRUCTION_GUARD}"[:STEPFUN_INSTRUCTION_MAX_CHARS]
    return (plan_instruction or STEPFUN_INSTRUCTION_GUARD)[:STEPFUN_INSTRUCTION_MAX_CHARS]


_OPEN_BRACKETS = frozenset("（(")
_CLOSE_BRACKETS = frozenset("）)")
_AUTH_STATUS_CODES = frozenset({401, 402, 403})
_AUTH_CLOSE_CODES = frozenset({1008})
_AUTH_MARKERS = (
    "auth",
    "unauthorized",
    "forbidden",
    "invalid api key",
    "api key",
    "quota",
    "balance",
    "insufficient",
    "欠费",
    "余额",
    "鉴权",
    "未授权",
)


def strip_bracket_instructions(text: str) -> str:
    """Remove full-width/half-width parenthesised content, including the brackets.

    StepFun reads ``（）`` content as an inline instruction instead of speech.
    A model that emits ``（笑）`` would therefore make the voice laugh unless
    the text is cleaned before it reaches the vendor.
    """

    if not text:
        return ""
    out: list[str] = []
    inside = False
    for character in text:
        if inside:
            if character in _CLOSE_BRACKETS:
                inside = False
            continue
        if character in _OPEN_BRACKETS:
            inside = True
            continue
        out.append(character)
    return "".join(out)


class BracketInstructionFilter:
    """Apply :func:`strip_bracket_instructions` across split text pushes."""

    def __init__(self) -> None:
        self._inside = False

    def feed(self, text: str) -> str:
        out: list[str] = []
        for character in text:
            if self._inside:
                if character in _CLOSE_BRACKETS:
                    self._inside = False
                continue
            if character in _OPEN_BRACKETS:
                self._inside = True
                continue
            out.append(character)
        return "".join(out)

    def flush(self) -> str:
        # An unterminated instruction runs to the end of the reply; drop it.
        self._inside = False
        return ""


class StepFunTTSError(APIError):
    """Base error for the StepFun TTS adapter."""

    auth_failure = False


class StepFunHandshakeError(StepFunTTSError):
    """The WebSocket handshake or ``tts.response.created`` failed."""


class StepFunAuthError(StepFunTTSError):
    """Authentication, permission or account-balance failure."""

    auth_failure = True

    def __init__(self, message: str = "StepFun TTS authentication or quota failure") -> None:
        super().__init__(message, retryable=False)


class StepFunFirstAudioTimeoutError(StepFunTTSError, TimeoutError):
    """No PCM arrived within the first-packet budget."""


class StepFunPCMError(StepFunTTSError):
    """The provider returned non-16-bit or inconsistent PCM."""

    def __init__(self, message: str = "StepFun TTS returned invalid PCM") -> None:
        super().__init__(message, retryable=False)


class StepFunProtocolError(StepFunTTSError):
    """A documented protocol event was malformed or an error event arrived."""


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
class StepFunTTSConfig:
    """One WebSocket endpoint, voice and generation budget."""

    ws_url: str = DEFAULT_WS_URL
    api_key: str = field(default="", repr=False)
    model: str = STEPFUN_TTS_MODEL
    voice: str = STEPFUN_TTS_VOICE
    sample_rate: int = 24000
    instruction: str = STEPFUN_INSTRUCTION_GUARD
    pool_size: int = 2
    connect_timeout_s: float = 5.0
    first_audio_timeout_s: float = 1.5
    total_timeout_s: float = 20.0
    hard_deadline_s: float = 180.0

    def __post_init__(self) -> None:
        if not _valid_websocket_url(self.ws_url, require_tls=False):
            raise ValueError(
                "StepFun TTS WebSocket URL must use ws/wss without userinfo or fragment"
            )
        if not self.model.strip():
            raise ValueError("StepFun TTS model must not be blank")
        if not self.voice.strip():
            raise ValueError("StepFun TTS voice must not be blank")
        if self.sample_rate != 24000:
            raise ValueError("Memoria requires StepFun PCM at 24000 Hz")
        if len(self.instruction) > STEPFUN_INSTRUCTION_MAX_CHARS:
            raise ValueError("StepFun TTS instruction must be at most 500 characters")
        if self.pool_size < 1:
            raise ValueError("StepFun TTS pool_size must be positive")
        for name in (
            "connect_timeout_s",
            "first_audio_timeout_s",
            "total_timeout_s",
            "hard_deadline_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"StepFun TTS {name} must be finite and positive")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> StepFunTTSConfig:
        import os

        values = dict(os.environ) if env is None else env
        production = values.get("ENVIRONMENT", "development").lower() == "production"
        mock_url = values.get("STEPFUN_TTS_MOCK_WS_URL", "").strip()
        ws_url = mock_url or values.get("STEPFUN_TTS_WS_URL", DEFAULT_WS_URL).strip()
        api_key = values.get("STEPFUN_API_KEY", "").strip()
        if not api_key and not mock_url:
            raise ValueError("StepFun TTS requires STEPFUN_API_KEY")
        if not _valid_websocket_url(ws_url, require_tls=production):
            raise ValueError(
                "StepFun TTS WebSocket URL must use wss in production and contain no "
                "userinfo or fragment"
            )
        instruction = _effective_instruction(values.get("STEPFUN_TTS_INSTRUCTION", ""))
        return cls(
            ws_url=ws_url,
            api_key=api_key,
            model=values.get("STEPFUN_TTS_MODEL", STEPFUN_TTS_MODEL).strip() or STEPFUN_TTS_MODEL,
            voice=values.get("STEPFUN_TTS_VOICE", STEPFUN_TTS_VOICE).strip() or STEPFUN_TTS_VOICE,
            sample_rate=int(values.get("STEPFUN_TTS_SAMPLE_RATE", "24000")),
            instruction=instruction,
            pool_size=int(values.get("STEPFUN_TTS_POOL_SIZE", "2")),
            connect_timeout_s=float(values.get("STEPFUN_TTS_CONNECT_TIMEOUT_S", "5")),
            first_audio_timeout_s=float(values.get("STEPFUN_TTS_FIRST_AUDIO_TIMEOUT_S", "1.5")),
            total_timeout_s=float(values.get("STEPFUN_TTS_TOTAL_TIMEOUT_S", "20")),
            hard_deadline_s=float(values.get("STEPFUN_TTS_HARD_DEADLINE_S", "180")),
        )

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}


@dataclass
class StepFunConnection:
    ws: ClientConnection
    conn_id: str
    session_id: str
    in_use: bool = False
    failed: bool = False
    closed: bool = False

    @property
    def live(self) -> bool:
        return not self.closed and not self.failed and self.ws.state is State.OPEN


def _parse_event(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StepFunProtocolError("StepFun sent non-UTF-8 data") from exc
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StepFunProtocolError("StepFun sent malformed JSON") from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise StepFunProtocolError("StepFun sent an event without a type")
    return event


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    if not isinstance(data, dict):
        raise StepFunProtocolError("StepFun event has no data object")
    return data


def _decode_audio(value: object) -> bytes:
    if not isinstance(value, str):
        raise StepFunProtocolError("StepFun audio event has no base64 audio")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise StepFunProtocolError("StepFun audio event has invalid base64") from exc
    if len(decoded) % 2:
        raise StepFunPCMError(f"StepFun PCM length is odd ({len(decoded)} bytes)")
    return decoded


def _is_auth_event(data: dict[str, Any]) -> bool:
    code = str(data.get("code", "")).strip()
    if code in {str(status) for status in _AUTH_STATUS_CODES}:
        return True
    message = str(data.get("message", "")).lower()
    details = data.get("details")
    if isinstance(details, dict):
        message = f"{message} {str(details.get('error', '')).lower()}"
    return any(marker in message for marker in _AUTH_MARKERS)


def _provider_event_error(event: dict[str, Any]) -> StepFunTTSError:
    data = _event_data(event)
    if _is_auth_event(data):
        return StepFunAuthError()
    code = str(data.get("code", "unknown"))
    return StepFunProtocolError(f"StepFun TTS error {code}", retryable=False)


def _handshake_error(exc: BaseException) -> StepFunTTSError:
    if isinstance(exc, InvalidStatus):
        status = exc.response.status_code
        if status in _AUTH_STATUS_CODES:
            return StepFunAuthError()
        return StepFunHandshakeError(f"StepFun TTS handshake failed with HTTP {status}")
    if isinstance(exc, ConnectionClosed) and exc.code in _AUTH_CLOSE_CODES:
        return StepFunAuthError()
    return StepFunHandshakeError(f"StepFun TTS handshake failed ({type(exc).__name__})")


def _text_chunks(text: str, limit: int = STEPFUN_TEXT_DELTA_MAX_CHARS) -> list[str]:
    return [text[index : index + limit] for index in range(0, len(text), limit)]


class StepFunTTSPool:
    """Preconnected single-use sockets; every connection carries one reply."""

    def __init__(self, config: StepFunTTSConfig) -> None:
        self.config = config
        self._available: asyncio.Queue[StepFunConnection] = asyncio.Queue()
        self._all: dict[str, StepFunConnection] = {}
        self._lock = asyncio.Lock()
        self._closing = False
        self._refill_tasks: set[asyncio.Task[None]] = set()
        self.active_by_fence: dict[str, StepFunConnection] = {}

    async def warm(self, size: int | None = None) -> None:
        count = self.config.pool_size if size is None else size
        opened: list[StepFunConnection] = []
        try:
            for _ in range(count):
                async with self._lock:
                    opened.append(await self._open())
            for connection in opened:
                await self._available.put(connection)
        except Exception:
            for connection in opened:
                await self._discard(connection, reason="warm_error")
            raise

    async def _open(self) -> StepFunConnection:
        if self._closing:
            raise RuntimeError("StepFun TTS pool is closing")
        conn_id = f"stepfun-{time.monotonic_ns()}"
        separator = "&" if "?" in self.config.ws_url else "?"
        try:
            ws = await websockets.connect(
                f"{self.config.ws_url}{separator}model={self.config.model}",
                additional_headers=self.config.auth_headers(),
                open_timeout=self.config.connect_timeout_s,
                max_size=10 * 1024 * 1024,
            )
            raw = await asyncio.wait_for(
                ws.recv(),
                timeout=self.config.connect_timeout_s,
            )
            event = _parse_event(raw)
            if event["type"] != "tts.connection.done":
                raise StepFunHandshakeError(
                    f"StepFun TTS expected tts.connection.done, got {event['type']}"
                )
            session_id = str(_event_data(event).get("session_id", "")).strip()
            if not session_id:
                raise StepFunHandshakeError("StepFun TTS connection has no session id")
        except (StepFunTTSError, asyncio.CancelledError):
            with contextlib.suppress(Exception):
                if "ws" in locals():
                    await ws.close()
            raise
        except Exception as exc:
            with contextlib.suppress(Exception):
                if "ws" in locals():
                    await ws.close()
            raise _handshake_error(exc) from exc
        connection = StepFunConnection(ws=ws, conn_id=conn_id, session_id=session_id)
        self._all[conn_id] = connection
        return connection

    async def acquire(self, *, wait_s: float = 0.05) -> StepFunConnection:
        connection: StepFunConnection | None = None
        with contextlib.suppress(TimeoutError):
            connection = await asyncio.wait_for(self._available.get(), timeout=wait_s)
        while connection is not None and not connection.live:
            await self._discard(connection, reason="expired")
            try:
                connection = self._available.get_nowait()
            except asyncio.QueueEmpty:
                connection = None
        if connection is None:
            async with self._lock:
                if self._closing:
                    raise RuntimeError("StepFun TTS pool is closing")
                connection = await self._open()
        connection.in_use = True
        return connection

    async def discard(self, connection: StepFunConnection, *, reason: str) -> None:
        self._unbind(connection)
        await self._discard(connection, reason=reason)

    async def _discard(self, connection: StepFunConnection, *, reason: str) -> None:
        was_closed = connection.closed
        connection.failed = True
        connection.closed = True
        connection.in_use = False
        self._all.pop(connection.conn_id, None)
        if not was_closed:
            with contextlib.suppress(Exception):
                await connection.ws.close()
        if not self._closing and reason not in {"shutdown", "warm_error"}:
            task = asyncio.create_task(self._refill_one(), name="stepfun-tts-pool-refill")
            self._refill_tasks.add(task)
            task.add_done_callback(self._refill_tasks.discard)

    async def _refill_one(self) -> None:
        async with self._lock:
            if self._closing or len(self._all) >= self.config.pool_size:
                return
            try:
                connection = await self._open()
            except Exception as exc:
                logger.warning(
                    "StepFun TTS pool refill failed error_type=%s",
                    type(exc).__name__,
                )
                return
            if self._closing:
                await self._discard(connection, reason="shutdown")
                return
            await self._available.put(connection)

    def bind_active(self, fence: GenerationFence, connection: StepFunConnection) -> None:
        self.active_by_fence[self._fence_key(fence)] = connection

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        connection = self.active_by_fence.pop(self._fence_key(fence), None)
        if connection is not None:
            await self.discard(connection, reason="cancel")

    def _unbind(self, connection: StepFunConnection) -> None:
        for key, active in tuple(self.active_by_fence.items()):
            if active is connection:
                self.active_by_fence.pop(key, None)

    @staticmethod
    def _fence_key(fence: GenerationFence) -> str:
        return f"{fence.session_id}:{fence.turn_id}:{fence.generation_id}:{fence.tool_epoch}"

    @property
    def available_approx(self) -> int:
        return self._available.qsize()

    async def aclose(self) -> None:
        self._closing = True
        tasks = tuple(self._refill_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refill_tasks.clear()
        async with self._lock:
            connections = list(self._all.values())
            self.active_by_fence.clear()
            while not self._available.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._available.get_nowait()
        for connection in connections:
            await self._discard(connection, reason="shutdown")


class StepFunSynthesizeStream(SynthesizeStream):
    """One StepFun connection for one pushed reply segment."""

    def __init__(
        self,
        *,
        config: StepFunTTSConfig,
        pool: StepFunTTSPool,
        conn_options: APIConnectOptions,
        fence: GenerationFence | None,
        trace: Callable[[str, str, dict[str, Any] | None], None],
    ) -> None:
        super().__init__(conn_options=conn_options)
        self._config = replace(config)
        self._pool = pool
        self._fence = fence
        self._trace = trace
        self._connection: StepFunConnection | None = None
        self._started_at = time.monotonic()

    async def _run(self, output_emitter: AudioEmitter) -> None:
        connection = await self._pool.acquire()
        self._connection = connection
        if self._fence is not None:
            self._pool.bind_active(self._fence, connection)
        send_task: asyncio.Task[None] | None = None
        emitter_started = False
        got_audio = False
        pcm_bytes = 0
        loop = asyncio.get_running_loop()
        budget = GenerationBudget(
            started_at=loop.time(),
            first_audio_timeout_s=self._config.first_audio_timeout_s,
            total_timeout_s=self._config.total_timeout_s,
            hard_deadline_s=self._config.hard_deadline_s,
        )
        try:
            create_data: dict[str, Any] = {
                "session_id": connection.session_id,
                "voice_id": self._config.voice,
                "response_format": "pcm",
                "sample_rate": self._config.sample_rate,
                "text_normalization": "standard",
                "mode": "default",
            }
            if self._config.instruction:
                create_data["instruction"] = self._config.instruction
            await connection.ws.send(
                json.dumps({"type": "tts.create", "data": create_data}, ensure_ascii=False)
            )
            try:
                raw = await asyncio.wait_for(
                    connection.ws.recv(),
                    timeout=self._config.connect_timeout_s,
                )
            except TimeoutError as exc:
                raise StepFunHandshakeError("StepFun TTS tts.response.created timed out") from exc
            created = _parse_event(raw)
            if created["type"] == "tts.response.error":
                raise _provider_event_error(created)
            if created["type"] != "tts.response.created":
                raise StepFunHandshakeError(
                    f"StepFun TTS expected tts.response.created, got {created['type']}"
                )
            self._trace("stepfun_session_created", "ok", None)

            async def sender() -> None:
                bracket_filter = BracketInstructionFilter()
                async for data in self._input_ch:
                    if isinstance(data, self._FlushSentinel):
                        continue
                    text = bracket_filter.feed(str(data))
                    for chunk in _text_chunks(text):
                        if not chunk:
                            continue
                        await connection.ws.send(
                            json.dumps(
                                {
                                    "type": "tts.text.delta",
                                    "data": {
                                        "session_id": connection.session_id,
                                        "text": chunk,
                                    },
                                },
                                ensure_ascii=False,
                            )
                        )
                for chunk in _text_chunks(bracket_filter.flush()):
                    if chunk:
                        await connection.ws.send(
                            json.dumps(
                                {
                                    "type": "tts.text.delta",
                                    "data": {
                                        "session_id": connection.session_id,
                                        "text": chunk,
                                    },
                                },
                                ensure_ascii=False,
                            )
                        )
                await connection.ws.send(
                    json.dumps(
                        {
                            "type": "tts.text.done",
                            "data": {"session_id": connection.session_id},
                        },
                        ensure_ascii=False,
                    )
                )

            send_task = asyncio.create_task(sender(), name="stepfun-tts-send")
            while True:
                try:
                    raw = await asyncio.wait_for(
                        connection.ws.recv(),
                        timeout=budget.timeout_s(loop.time()),
                    )
                except TimeoutError as exc:
                    if not got_audio:
                        raise StepFunFirstAudioTimeoutError(
                            "StepFun TTS first audio timeout"
                        ) from exc
                    raise StepFunTTSError(
                        "StepFun TTS stalled after audio started",
                        retryable=False,
                    ) from exc
                except ConnectionClosed as exc:
                    if exc.code != 1000:
                        raise
                    # A user stop or barge-in closes the socket normally. That is
                    # the end of this reply, not a provider outage: the failover
                    # wrapper must not move the rest of the session to Doubao.
                    if emitter_started:
                        output_emitter.end_segment()
                    await self._pool.discard(connection, reason="normal_close")
                    self._connection = None
                    if not got_audio:
                        raise APIStatusError(
                            "StepFun TTS closed normally before audio",
                            status_code=499,
                        ) from exc
                    return
                event = _parse_event(raw)
                event_type = event["type"]
                if event_type == "tts.response.audio.delta":
                    payload = _event_data(event)
                    pcm = _decode_audio(payload.get("audio"))
                    if pcm:
                        if not got_audio:
                            output_emitter.initialize(
                                request_id=connection.session_id,
                                sample_rate=self._config.sample_rate,
                                num_channels=1,
                                mime_type="audio/pcm",
                                frame_size_ms=20,
                                stream=True,
                            )
                            output_emitter.start_segment(segment_id=connection.session_id)
                            emitter_started = True
                            self._trace(
                                "stepfun_first_pcm",
                                "ok",
                                {"pcm_bytes": len(pcm)},
                            )
                        got_audio = True
                        budget.renew(loop.time())
                        pcm_bytes += len(pcm)
                        output_emitter.push(pcm)
                elif event_type == "tts.response.audio.done":
                    payload = _event_data(event)
                    if not got_audio:
                        # The docs include the complete audio in audio.done.
                        # Deltas already stream; only a delta-less reply uses it.
                        pcm = _decode_audio(payload.get("audio"))
                        if pcm:
                            output_emitter.initialize(
                                request_id=connection.session_id,
                                sample_rate=self._config.sample_rate,
                                num_channels=1,
                                mime_type="audio/pcm",
                                frame_size_ms=20,
                                stream=True,
                            )
                            output_emitter.start_segment(segment_id=connection.session_id)
                            emitter_started = True
                            got_audio = True
                            pcm_bytes += len(pcm)
                            output_emitter.push(pcm)
                    break
                elif event_type == "tts.response.error":
                    raise _provider_event_error(event)
                elif got_audio:
                    budget.renew(loop.time())
            if not got_audio:
                raise StepFunTTSError(
                    "StepFun TTS returned no audio",
                    retryable=False,
                )
            if emitter_started:
                output_emitter.end_segment()
            await self._pool.discard(connection, reason="complete")
            self._connection = None
        except asyncio.CancelledError:
            if self._connection is not None:
                await self._pool.discard(self._connection, reason="cancel")
                self._connection = None
            raise
        except Exception:
            if self._connection is not None:
                await self._pool.discard(self._connection, reason="error")
                self._connection = None
            raise
        finally:
            if send_task is not None:
                send_task.cancel()
                # A clean socket close can leave the sender with
                # ConnectionClosedOK. It is the same normal termination as the
                # receive side and must not replace a cancellation or a complete
                # audio take. Any other sender failure is a real bug and must
                # surface.
                with contextlib.suppress(asyncio.CancelledError, ConnectionClosed):
                    await send_task
            self._trace(
                "stepfun_pcm_summary",
                "ok" if got_audio else "error",
                {
                    "pcm_bytes": pcm_bytes,
                    "chunk_count": 0,
                    "got_audio": int(got_audio),
                },
            )


class StepFunTTS:
    """Runtime StepFun adapter with the same surface Doubao exposes."""

    def __init__(self, config: StepFunTTSConfig, pool: StepFunTTSPool | None = None) -> None:
        self._base_config = replace(config)
        self._config = replace(config)
        self._pool = pool or StepFunTTSPool(self._config)
        self._active_fence: GenerationFence | None = None
        self._trace_callback: Callable[[str, str, dict[str, Any] | None], None] | None = None

    @classmethod
    def from_env(cls) -> StepFunTTS:
        config = StepFunTTSConfig.from_env()
        return cls(config)

    @property
    def provider(self) -> str:
        return STEPFUN_PROVIDER

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def sample_rate(self) -> int:
        return self._config.sample_rate

    @property
    def pool(self) -> StepFunTTSPool:
        return self._pool

    def bind_fence(self, fence: GenerationFence) -> None:
        self._active_fence = fence

    def set_trace_callback(
        self,
        callback: Callable[[str, str, dict[str, Any] | None], None],
    ) -> None:
        self._trace_callback = callback

    def trace(
        self,
        name: str,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        if self._trace_callback is not None:
            self._trace_callback(name, status, detail)

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
        del emotion, pitch, reference_contexts, fence
        self._config.instruction = _effective_instruction(instruction)
        self._config.sample_rate = self._base_config.sample_rate

    def apply_voice_profile(
        self,
        *,
        model: str,
        voice: str,
        profile_id: str | None = None,
        provider: str | None = None,
        voice_kind: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        # The approved clone/designed registry is Doubao-only. The wrapper
        # exposes Doubao's identity to policy code and records the actual
        # StepFun voice per sentence, so this adapter keeps its fixed voice.
        del model, voice, profile_id, provider, voice_kind, resource_id

    def use_baseline_voice(self) -> None:
        self._config.voice = self._base_config.voice
        self._config.model = self._base_config.model
        self._config.instruction = self._base_config.instruction

    @property
    def current_model(self) -> str:
        return self._config.model

    @property
    def current_voice(self) -> str:
        return self._config.voice

    @property
    def current_voice_profile_id(self) -> str | None:
        return None

    @property
    def current_voice_kind(self) -> str:
        return "designed"

    @property
    def current_instruction(self) -> str:
        return self._config.instruction

    @property
    def current_rate(self) -> float:
        return 1.0

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> StepFunSynthesizeStream:
        return StepFunSynthesizeStream(
            config=replace(self._config),
            pool=self._pool,
            conn_options=conn_options,
            fence=self._active_fence,
            trace=self.trace,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


__all__ = [
    "STEPFUN_INSTRUCTION_GUARD",
    "STEPFUN_TTS_MODEL",
    "STEPFUN_TTS_VOICE",
    "DEFAULT_WS_URL",
    "STEPFUN_INSTRUCTION_MAX_CHARS",
    "STEPFUN_PROVIDER",
    "STEPFUN_TEXT_DELTA_MAX_CHARS",
    "STEPFUN_TTS_MODEL",
    "STEPFUN_TTS_VOICE",
    "StepFunAuthError",
    "StepFunFirstAudioTimeoutError",
    "StepFunHandshakeError",
    "StepFunPCMError",
    "StepFunProtocolError",
    "StepFunSynthesizeStream",
    "StepFunTTS",
    "StepFunTTSConfig",
    "StepFunTTSError",
    "StepFunTTSPool",
    "BracketInstructionFilter",
    "strip_bracket_instructions",
]
