"""Volcengine Doubao streaming ASR session (primary provider).

The session speaks the v3 ``sauc`` WebSocket protocol, accepts raw 16-bit
little-endian PCM at 16 kHz, and maps provider results onto the neutral
``FunASRServerEvent`` shape used by the voice core.

Credentials come from the environment only. Nothing in this module reads a
plaintext config file or logs a token.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import logging
import math
import os
import uuid
from dataclasses import dataclass
from time import monotonic
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection

from services.agent.src.providers.funasr_protocol import FunASRSentence, FunASRServerEvent
from services.agent.src.providers.volc_protocol import (
    SERVER_ERROR_RESPONSE,
    VolcEvent,
    build_audio,
    build_event,
    build_task_request,
    extract_transcript,
    parse_frame,
)

logger = logging.getLogger(__name__)

DEFAULT_VOLC_ASR_ENDPOINT = (
    "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
)
DEFAULT_VOLC_RESOURCE_ID = "volc.seedasr.sauc.duration"


class VolcASRError(RuntimeError):
    """Provider rejected a connection, session or task."""


@dataclass
class VolcASRConfig:
    app_id: str
    access_token: str
    resource_id: str = DEFAULT_VOLC_RESOURCE_ID
    ws_url: str = DEFAULT_VOLC_ASR_ENDPOINT
    model_name: str = "bigmodel"
    sample_rate: int = 16_000
    language: str = "zh"
    chunk_ms: int = 100
    end_window_ms: int = 550
    enable_nonstream: bool = True
    show_utterances: bool = True
    boosting_table_id: str | None = None
    connect_timeout_s: float = 5.0
    result_timeout_s: float = 8.0
    post_finish_tail_grace_s: float = 0.25
    # Test seam: replaces ``websockets.connect`` without touching the network.
    connector: Any = None

    def __post_init__(self) -> None:
        if self.sample_rate != 16_000:
            raise ValueError("Volc ASR only supports 16000 Hz input")
        if self.chunk_ms <= 0 or self.sample_rate * self.chunk_ms % 1000:
            raise ValueError("Volc ASR chunk_ms must produce whole samples")
        if not 0 < self.connect_timeout_s <= 30:
            raise ValueError("Volc ASR connect timeout must be within (0, 30] seconds")
        if not 0 < self.result_timeout_s <= 60:
            raise ValueError("Volc ASR result timeout must be within (0, 60] seconds")
        if not 0.0 <= self.post_finish_tail_grace_s <= 2.0:
            raise ValueError("Volc ASR post-finish tail grace must be between 0 and 2 seconds")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> VolcASRConfig:
        e = env or dict(os.environ)
        endpoint = (
            e.get("VOLC_ASR_WS_URL", "").strip()
            or e.get("VOLC_ASR_WS_ENDPOINT", "").strip()
            or DEFAULT_VOLC_ASR_ENDPOINT
        )
        return cls(
            app_id=e.get("VOLC_ASR_APP_ID", "").strip(),
            access_token=e.get("VOLC_ASR_ACCESS_TOKEN", "").strip(),
            resource_id=e.get("VOLC_ASR_RESOURCE_ID", "").strip()
            or DEFAULT_VOLC_RESOURCE_ID,
            ws_url=endpoint,
            model_name=e.get("VOLC_ASR_MODEL", "bigmodel").strip() or "bigmodel",
            chunk_ms=int(e.get("VOLC_ASR_CHUNK_MS", "100")),
            end_window_ms=int(e.get("VOLC_ASR_END_WINDOW_MS", "550")),
            enable_nonstream=e.get("VOLC_ASR_ENABLE_NONSTREAM", "true").lower() == "true",
            show_utterances=e.get("VOLC_ASR_SHOW_UTTERANCES", "true").lower() == "true",
            boosting_table_id=e.get("VOLC_ASR_BOOSTING_TABLE_ID", "").strip() or None,
            connect_timeout_s=float(e.get("VOLC_ASR_CONNECT_TIMEOUT_S", "5")),
            result_timeout_s=float(e.get("VOLC_ASR_RESULT_TIMEOUT_S", "8")),
            post_finish_tail_grace_s=float(
                e.get("VOLC_ASR_POST_FINISH_TAIL_GRACE_S", "0.25")
            ),
        )

    def auth_headers(self) -> dict[str, str]:
        if not self.app_id or not self.access_token:
            raise ValueError(
                "Volc ASR credentials are missing; set VOLC_ASR_APP_ID and "
                "VOLC_ASR_ACCESS_TOKEN"
            )
        return {
            "X-Api-App-Key": self.app_id,
            "X-Api-Access-Key": self.access_token,
            "X-Api-Resource-Id": self.resource_id,
            "X-Api-Request-Id": str(uuid.uuid4()),
        }


@dataclass
class _Evidence:
    start_sample: int | None = None
    end_sample: int | None = None
    send_count: int = 0
    sample_count: int = 0
    peak_abs: int = 0
    square_sum: float = 0.0

    def record(self, pcm: bytes, start: int, end: int) -> None:
        if self.start_sample is None:
            self.start_sample = start
        self.end_sample = end
        self.send_count += 1
        samples = len(pcm) // 2
        self.sample_count += samples
        peak = audioop.max(pcm, 2)
        rms = audioop.rms(pcm, 2)
        self.peak_abs = max(self.peak_abs, peak)
        self.square_sum += float(rms * rms * samples)

    def reset(self) -> None:
        self.start_sample = None
        self.end_sample = None
        self.send_count = 0
        self.sample_count = 0
        self.peak_abs = 0
        self.square_sum = 0.0

    @property
    def rms(self) -> float | None:
        if not self.sample_count:
            return None
        return math.sqrt(self.square_sum / self.sample_count)


class VolcASRSession:
    """One streaming session; each VAD segment is a new provider task."""

    def __init__(self, config: VolcASRConfig, *, metrics: Any = None) -> None:
        self.config = config
        self.metrics = metrics
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self.task_id: str | None = None
        self._task_epoch = 0
        self._segment_epoch = 1
        self._task_sample_origin = 0
        self._last_sent_sample = 0
        self._last_committed_sample = 0
        self._evidence = _Evidence()
        self._ws: ClientConnection | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._control: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._audio_buffer = bytearray()
        self._audio_sequence = 0
        self._sentence_id = 0
        self._last_final_text = ""
        self._rotation_pending = False
        self._failed = False
        self._closed = False

    @property
    def task_epoch(self) -> int:
        return self._task_epoch

    @property
    def segment_epoch(self) -> int:
        return self._segment_epoch

    @property
    def rotation_pending(self) -> bool:
        return self._rotation_pending

    @property
    def task_sample_origin(self) -> int:
        return self._task_sample_origin

    @property
    def task_audio_start_sample(self) -> int | None:
        return self._evidence.start_sample

    @property
    def task_audio_end_sample(self) -> int | None:
        return self._evidence.end_sample

    @property
    def task_audio_send_count(self) -> int:
        return self._evidence.send_count

    @property
    def task_pcm_sample_count(self) -> int:
        return self._evidence.sample_count

    @property
    def task_pcm_peak_abs(self) -> int | None:
        return self._evidence.peak_abs if self._evidence.sample_count else None

    @property
    def task_pcm_rms(self) -> float | None:
        return self._evidence.rms

    @property
    def last_sent_sample(self) -> int:
        return self._last_sent_sample

    def mark_committed_sample(self, sample: int) -> None:
        if sample < 0:
            raise ValueError("committed sample must be non-negative")
        self._last_committed_sample = max(self._last_committed_sample, sample)

    async def connect(self) -> None:
        if self._closed:
            raise RuntimeError("Volc ASR session is closed")
        if self._ws is not None:
            return
        headers = self.config.auth_headers()
        connector = self.config.connector or websockets.connect
        ws = await asyncio.wait_for(
            connector(
                self.config.ws_url,
                additional_headers=headers,
                open_timeout=self.config.connect_timeout_s,
            ),
            timeout=self.config.connect_timeout_s,
        )
        self._ws = ws
        self._recv_task = asyncio.create_task(self._recv_loop(), name="volc-asr-recv-loop")
        try:
            await self._send_event(VolcEvent.START_CONNECTION)
            await self._wait_control(VolcEvent.CONNECTION_STARTED)
            await self._start_task(origin=0)
        except BaseException:
            await self.aclose()
            raise

    async def _start_task(self, *, origin: int) -> None:
        await self._send_event(VolcEvent.START_SESSION)
        await self._wait_control(VolcEvent.SESSION_STARTED)
        await self._send_event(
            VolcEvent.TASK_REQUEST,
            build_task_request(
                model_name=self.config.model_name,
                sample_rate=self.config.sample_rate,
                end_window_ms=self.config.end_window_ms,
                enable_nonstream=self.config.enable_nonstream,
                show_utterances=self.config.show_utterances,
                boosting_table_id=self.config.boosting_table_id,
            ),
        )
        await self._wait_control(VolcEvent.TASK_STARTED)
        self.task_id = str(uuid.uuid4())
        self._task_epoch += 1
        self._task_sample_origin = origin
        self._audio_sequence = 0
        self._audio_buffer.clear()
        self._sentence_id = 0
        self._last_final_text = ""
        self._evidence.reset()
        self._failed = False
        self._rotation_pending = False

    async def _send_event(
        self,
        event: VolcEvent,
        payload: dict[str, Any] | None = None,
    ) -> None:
        ws = self._ws
        if ws is None:
            raise VolcASRError("Volc ASR WebSocket is not connected")
        await ws.send(build_event(event, payload))

    async def _wait_control(self, expected: VolcEvent) -> None:
        deadline = monotonic() + self.config.connect_timeout_s
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise VolcASRError(f"Volc ASR timed out waiting for {expected.name}")
            kind, payload = await asyncio.wait_for(self._control.get(), timeout=remaining)
            if kind == "failed":
                raise VolcASRError(f"Volc ASR {expected.name} failed: {payload}")
            if int(kind) == int(expected):
                return

    async def _recv_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for message in ws:
                if isinstance(message, str):
                    logger.info("volc ASR ignored text frame")
                    continue
                try:
                    frame = parse_frame(message)
                except ValueError:
                    logger.warning("volc ASR dropped an undecodable frame")
                    continue
                if frame.message_type == SERVER_ERROR_RESPONSE:
                    code = frame.error_code
                    self._failed = True
                    await self._control.put(("failed", code))
                    await self.events.put(
                        FunASRServerEvent(
                            event="task-failed",
                            task_id=self.task_id or "",
                            error_code=str(code) if code is not None else None,
                            error_message="volc server error",
                        )
                    )
                    return
                event = frame.event
                if event in (
                    VolcEvent.CONNECTION_STARTED,
                    VolcEvent.SESSION_STARTED,
                    VolcEvent.TASK_STARTED,
                ):
                    await self._control.put((str(int(event)), frame.payload))
                    continue
                if event == VolcEvent.TASK_FAILED:
                    self._failed = True
                    code = frame.payload.get("error_code")
                    await self._control.put(("failed", code))
                    await self.events.put(
                        FunASRServerEvent(
                            event="task-failed",
                            task_id=self.task_id or "",
                            error_code=str(code) if code is not None else None,
                            error_message="volc task failed",
                        )
                    )
                    return
                if event in (
                    VolcEvent.CONNECTION_FAILED,
                    VolcEvent.SESSION_FAILED,
                ):
                    self._failed = True
                    await self._control.put(("failed", frame.payload))
                    await self.events.put(
                        FunASRServerEvent(
                            event="task-failed",
                            task_id=self.task_id or "",
                            error_message="volc session failed",
                        )
                    )
                    return
                if event in (
                    VolcEvent.TASK_FINISHED,
                    VolcEvent.SESSION_FINISHED,
                    VolcEvent.CONNECTION_FINISHED,
                ):
                    await self._control.put((str(int(event)), frame.payload))
                    if event == VolcEvent.TASK_FINISHED:
                        await self.events.put(
                            FunASRServerEvent(
                                event="task-finished",
                                task_id=self.task_id or "",
                            )
                        )
                    continue
                transcript = extract_transcript(frame.payload)
                if transcript is None:
                    continue
                sentence_end = transcript.definite
                if sentence_end and transcript.text == self._last_final_text:
                    continue
                if sentence_end:
                    self._sentence_id += 1
                    self._last_final_text = transcript.text
                words: tuple[Any, ...] = ()
                await self.events.put(
                    FunASRServerEvent(
                        event="result-generated",
                        task_id=self.task_id or "",
                        sentence=FunASRSentence(
                            sentence_id=self._sentence_id,
                            text=transcript.text,
                            begin_ms=transcript.begin_ms,
                            end_ms=transcript.end_ms,
                            sentence_end=sentence_end,
                            heartbeat=False,
                            words=words,
                            word_timing_valid=False,
                        ),
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._closed:
                return
            logger.warning("volc ASR receive loop ended", exc_info=True)
            self._failed = True
            await self._control.put(("failed", "receive-loop"))
            await self.events.put(
                FunASRServerEvent(
                    event="task-failed",
                    task_id=self.task_id or "",
                    error_message="volc receive loop ended",
                )
            )

    async def send_pcm(
        self,
        pcm: bytes,
        *,
        capture_start_sample: int | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("Volc ASR session is not connected")
        if self._failed:
            raise VolcASRError("Volc ASR session failed")
        if not pcm or len(pcm) % 2:
            raise ValueError("Volc ASR PCM must be non-empty 16-bit samples")
        start = self._last_sent_sample if capture_start_sample is None else capture_start_sample
        if start < self._last_sent_sample:
            raise ValueError("capture sample position moved backwards")
        end = start + len(pcm) // 2
        if self._rotation_pending:
            await self._start_task(origin=start)
        ws = self._ws
        if ws is None:
            raise VolcASRError("Volc ASR WebSocket is not connected")
        if self._last_sent_sample == 0 and self._task_sample_origin == 0:
            # The media session may join a stream at a non-zero capture
            # position; the provider task starts at zero relative to that.
            self._task_sample_origin = start
        self._audio_buffer.extend(pcm)
        chunk_bytes = self.config.sample_rate * 2 * self.config.chunk_ms // 1000
        while len(self._audio_buffer) >= chunk_bytes:
            chunk = bytes(self._audio_buffer[:chunk_bytes])
            del self._audio_buffer[:chunk_bytes]
            self._audio_sequence += 1
            await ws.send(build_audio(chunk, sequence=self._audio_sequence))
        self._evidence.record(pcm, start, end)
        self._last_sent_sample = end
        if self.metrics is not None:
            self.metrics.inc_asr_request("sent", self.config.model_name)

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        if self._closed:
            return
        if self._ws is None or self.task_id is None:
            raise VolcASRError("Volc ASR task rotation requires an active session")
        ws = self._ws
        if self._audio_buffer and not self._failed:
            self._audio_sequence += 1
            await ws.send(
                build_audio(
                    bytes(self._audio_buffer),
                    sequence=self._audio_sequence,
                    last=True,
                )
            )
            self._audio_buffer.clear()
        if not self._failed:
            await self._send_event(VolcEvent.FINISH_SESSION)
        try:
            await self._wait_for_boundary()
        finally:
            self._rotation_pending = True
            self._segment_epoch += 1
            self._evidence.reset()

    async def _wait_for_boundary(self) -> None:
        deadline = monotonic() + self.config.result_timeout_s
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise VolcASRError("Volc ASR timed out waiting for task boundary")
            kind, payload = await asyncio.wait_for(self._control.get(), timeout=remaining)
            if kind == "failed":
                raise VolcASRError(f"Volc ASR task boundary failed: {payload}")
            if int(kind) in (
                int(VolcEvent.TASK_FINISHED),
                int(VolcEvent.SESSION_FINISHED),
            ):
                return

    async def reconnect_with_replay(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
            self._recv_task = None
        await self._close_ws()
        self._failed = False
        await self.connect()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._recv_task is not None:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task
            self._recv_task = None
        await self._close_ws()

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.close()


__all__ = [
    "DEFAULT_VOLC_ASR_ENDPOINT",
    "DEFAULT_VOLC_RESOURCE_ID",
    "VolcASRConfig",
    "VolcASRError",
    "VolcASRSession",
]
