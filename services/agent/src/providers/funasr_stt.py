"""FunASR Realtime STT adapter — LiveKit stt.STT subclass (ch.12)."""

from __future__ import annotations

import asyncio
import audioop
import collections
import contextlib
import json
import logging
import weakref
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
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

from services.agent.src.orchestration.stable_prefix import StablePrefixTracker
from services.agent.src.providers.funasr_protocol import (
    FunASRServerEvent,
    build_continue_task_context,
    build_finish_task,
    build_run_task,
    conversation_item_to_funasr_context,
    parse_server_message,
)
from services.agent.src.providers.reliability import CircuitBreaker

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
    reconnect_audio_ms: int = 1500
    connect_timeout_s: float = 5.0
    result_timeout_s: float = 8.0
    conversation_context_enabled: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> FunASRConfig:
        import os

        e = env or dict(os.environ)
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
            reconnect_audio_ms=int(e.get("FUNASR_RECONNECT_AUDIO_MS", "1500")),
            connect_timeout_s=float(e.get("FUNASR_CONNECT_TIMEOUT_S", "5")),
            result_timeout_s=float(e.get("FUNASR_RESULT_TIMEOUT_S", "8")),
            conversation_context_enabled=(
                e.get("FUNASR_CONTEXT_ENABLED", "false").lower() == "true"
            ),
        )


class FunASRSession:
    """Standalone streaming FunASR session (mock or real WS). Used by offline path & stream."""

    def __init__(self, config: FunASRConfig) -> None:
        self.config = config
        self.task_id: str | None = None
        self._ws: ClientConnection | None = None
        self._started = asyncio.Event()
        self._closed = False
        self._failed = False
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self._recv_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._reconnect_lock = asyncio.Lock()
        self._breaker = CircuitBreaker()
        self._finishing = False
        self._pcm_ring: collections.deque[bytes] = collections.deque()
        self._pcm_ring_bytes = 0
        self._max_ring_bytes = int(config.sample_rate * 2 * config.reconnect_audio_ms / 1000)
        self._context: tuple[dict[str, object], ...] = ()

    @property
    def failed(self) -> bool:
        return self._failed

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
        self._failed = False
        self._started.set()
        self._ready.set()
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
            run = build_run_task(
                model=self.config.model,
                sample_rate=self.config.sample_rate,
                language_hints=[self.config.language],
                semantic_punctuation_enabled=self.config.semantic_punctuation,
                max_sentence_silence_ms=self.config.max_sentence_silence_ms,
                heartbeat=self.config.heartbeat,
                context=list(self._context),
            )
            task_id = str(run["header"]["task_id"])
            await ws.send(json.dumps(run, ensure_ascii=False))
            while True:
                message = await asyncio.wait_for(
                    ws.recv(), timeout=self.config.connect_timeout_s
                )
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
                    if ev.event == "task-failed":
                        self._failed = True
                        self._breaker.record_failure()
                    await self.events.put(ev)
                    if ev.event in ("task-finished", "task-failed"):
                        if ev.event == "task-failed":
                            self._ready.clear()
                            with contextlib.suppress(Exception):
                                await ws.close()
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
            with contextlib.suppress(Exception):
                await failed_ws.close()
            started_at = asyncio.get_running_loop().time()
            try:
                self._breaker.before_request()
                ws, task_id, startup_events = await self._open_with_retry()
            except Exception:
                self._breaker.record_failure()
                return False
            try:
                self._ws = ws
                self.task_id = task_id
                self._failed = False
                for ev in startup_events:
                    await self.events.put(ev)
                if asyncio.get_running_loop().time() - started_at <= 2.0:
                    replay = b"".join(self._pcm_ring)
                    if replay:
                        await ws.send(replay)
                if self._finishing:
                    await ws.send(json.dumps(build_finish_task(task_id), ensure_ascii=False))
            except Exception:
                self._failed = True
                with contextlib.suppress(Exception):
                    await ws.close()
                self._breaker.record_failure()
                return False
            self._breaker.record_success()
            self._ready.set()
            return True

    def _push_ring(self, pcm: bytes) -> None:
        self._pcm_ring.append(pcm)
        self._pcm_ring_bytes += len(pcm)
        while self._pcm_ring_bytes > self._max_ring_bytes and self._pcm_ring:
            old = self._pcm_ring.popleft()
            self._pcm_ring_bytes -= len(old)

    async def send_pcm(self, pcm: bytes, *, replayed: bool = False) -> None:
        if self._closed or self._ws is None:
            raise RuntimeError("FunASR session not connected")
        if not replayed:
            self._push_ring(pcm)
        await self._ready.wait()
        ws = self._ws
        if ws is None:
            raise RuntimeError("FunASR session not connected")
        try:
            await ws.send(pcm)
        except Exception:
            if not await self._recover(ws):
                raise APIConnectionError("FunASR reconnect failed") from None

    async def update_context(self, context: tuple[dict[str, object], ...]) -> None:
        self._context = context
        if self._ws is None or self.task_id is None:
            return
        await self._ready.wait()
        msg = build_continue_task_context(self.task_id, list(context))
        await self._ws.send(json.dumps(msg, ensure_ascii=False))

    async def finish(self) -> None:
        if self._ws is None or self.task_id is None:
            return
        self._finishing = True
        await self._ready.wait()
        ws = self._ws
        assert ws is not None
        try:
            await ws.send(json.dumps(build_finish_task(self.task_id), ensure_ascii=False))
        except Exception:
            if not await self._recover(ws):
                raise APIConnectionError("FunASR reconnect failed while finishing") from None

    async def aclose(self) -> None:
        self._closed = True
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
        self._final_sentence_ids: set[tuple[str, int]] = set()

    def update_context(self, context: tuple[dict[str, object], ...]) -> None:
        self._pending_context = tuple(context)
        if self._context_updates.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._context_updates.get_nowait()
        self._context_updates.put_nowait(self._pending_context)

    async def _run(self) -> None:
        session = FunASRSession(self._config)
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
            results = await asyncio.gather(send_task, recv_task, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(
                    result, asyncio.CancelledError
                ):
                    raise APIConnectionError(str(result)) from result
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
                    await session.send_pcm(chunk)
            elif isinstance(data, self._FlushSentinel):
                if byte_stream:
                    remaining = bytes(byte_stream)
                    self._stt_instance.observe_pcm(remaining)
                    await session.send_pcm(remaining)
                    byte_stream.clear()
        if byte_stream:
            remaining = bytes(byte_stream)
            self._stt_instance.observe_pcm(remaining)
            await session.send_pcm(remaining)
        await session.finish()

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
                if self._speaking:
                    self._event_ch.send_nowait(
                        stt.SpeechEvent(type=stt.SpeechEventType.END_OF_SPEECH)
                    )
                    self._speaking = False
                return
            if ev.event != "result-generated" or ev.sentence is None:
                continue
            sent = ev.sentence
            if sent.heartbeat and not sent.text:
                continue
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
                sentence_key = (ev.task_id, sent.sentence_id)
                if sent.sentence_id > 0 and sentence_key in self._final_sentence_ids:
                    logger.info(
                        "duplicate FunASR final ignored sentence_id=%s",
                        sent.sentence_id,
                    )
                    continue
                if sent.sentence_id > 0:
                    self._final_sentence_ids.add(sentence_key)
                self._event_ch.send_nowait(
                    stt.SpeechEvent(
                        type=stt.SpeechEventType.FINAL_TRANSCRIPT,
                        request_id=request_id,
                        alternatives=[sd],
                    )
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

    def __init__(self, config: FunASRConfig) -> None:
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
        self._context_items: deque[dict[str, object]] = deque(maxlen=10)
        self._streams: weakref.WeakSet[FunASRRecognizeStream] = weakref.WeakSet()
        self._pcm_observer: Callable[[bytes], None] | None = None

    @classmethod
    def from_env(cls) -> FunASRSTT:
        return cls(FunASRConfig.from_env())

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
        session = FunASRSession(self._config)
        session._context = tuple(self._context_items)
        return session

    async def aclose(self) -> None:
        await asyncio.gather(
            *(stream.aclose() for stream in tuple(self._streams)),
            return_exceptions=True,
        )
