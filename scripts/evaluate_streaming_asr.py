"""Offline A/B of four streaming Chinese ASR services on real device recordings.

Engines (``--engines``, default all with credentials present):

``funasr``        DashScope ``fun-asr-realtime`` (production), task WebSocket
                  protocol (run-task / task-started / result-generated /
                  finish-task / task-finished) on ``/api-ws/v1/inference``,
                  raw PCM in binary frames.
``qwen-audio``    DashScope ``qwen-audio-3.1-asr-flash-streaming``, the same
                  task protocol (adds ``vad_model`` / ``keep_dialect``).
``qwen-realtime`` DashScope ``qwen3-asr-flash-realtime`` on
                  ``/api-ws/v1/realtime?model=...``: session.update,
                  input_audio_buffer.append (base64), manual commit per
                  segment (or ``--qwen-realtime-vad`` for server VAD),
                  session.finish -> session.finished.
``doubao``        Volcano Engine Doubao big-model streaming ASR
                  (``/api/v3/sauc/bigmodel_async`` by default): 4-byte binary
                  header + payload size + gzip JSON / gzip PCM, last packet
                  flagged in the header.

Input is one or more 16 kHz mono s16le WAVs (``MEDIA_PCM_TAP_DIR`` captures,
one per session/stream epoch) plus a UTF-8 reference script (one sentence per
line, optional ``category|sentence``).  Utterances are cut with an energy
detector, each one is streamed to every engine at real-time pace, segments are
aligned to reference lines by character similarity, and CER / latency are
reported per engine, overall and per category.  Segments that match no
reference line (robot echo, noise) are reported as false-trigger text.

Transcripts are private speech: text is only printed or written with
``--show-text``.  Credentials are read only from the environment
(``DASHSCOPE_API_KEY``; ``DOUBAO_ASR_APP_ID`` + ``DOUBAO_ASR_ACCESS_TOKEN``
for the old console or ``DOUBAO_ASR_API_KEY`` for the new console;
``DOUBAO_ASR_RESOURCE_ID``) and never printed.

    uv run python scripts/evaluate_streaming_asr.py \\
        --reference docs/asr-ab-reading-script-20260929.txt \\
        --output outputs/asr-ab/report.json /path/to/tap/*.wav
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import difflib
import gzip
import itertools
import json
import math
import os
import re
import statistics
import struct
import sys
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

SAMPLE_RATE = 16_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000
FRAME = SAMPLE_RATE * 30 // 1000  # 30 ms energy frames
FRAME_MS = 30
UNMATCHED_THRESHOLD = 0.3

DASHSCOPE_BASE = "wss://dashscope.aliyuncs.com"
DOUBAO_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
# Documented header example / 豆包流式语音识别模型1.0 小时版.  2.0 (seed ASR) is
# ``volc.seedasr.sauc.duration``; concurrency billing uses ``*.concurrent``.
DOUBAO_DEFAULT_RESOURCE_ID = "volc.bigasr.sauc.duration"

ENGINES = ("funasr", "qwen-audio", "qwen-realtime", "doubao")
WireProtocol = Literal["dashscope-task", "dashscope-realtime", "doubao-sauc"]

Samples = npt.NDArray[np.int16]


# --------------------------------------------------------------------------- audio


def read_wav_pcm(path: Path) -> Samples:
    """Samples of a 16 kHz mono s16le WAV; tolerates a tap file never closed.

    The PCM tap is fail-open, so a capture may end without its RIFF sizes
    being rewritten.  The ``data`` chunk is therefore located by hand and a
    zero/oversized length means "to the end of the file".
    """

    raw = path.read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path.name}: not a RIFF/WAVE file")
    offset = 12
    fmt: tuple[int, int, int, int] | None = None
    while offset + 8 <= len(raw):
        chunk_id = raw[offset : offset + 4]
        (size,) = struct.unpack_from("<I", raw, offset + 4)
        body = offset + 8
        if chunk_id == b"fmt ":
            audio_format, channels, rate = struct.unpack_from("<HHI", raw, body)
            (bits,) = struct.unpack_from("<H", raw, body + 14)
            fmt = (audio_format, channels, rate, bits)
        elif chunk_id == b"data":
            if fmt != (1, 1, SAMPLE_RATE, 16):
                raise ValueError(f"{path.name}: need 16 kHz mono s16le PCM, got {fmt}")
            end = len(raw) if size == 0 or body + size > len(raw) else body + size
            data = raw[body:end]
            data = data[: len(data) - len(data) % 2]
            return np.frombuffer(data, dtype="<i2").astype(np.int16)
        offset = body + size + (size & 1)
    raise ValueError(f"{path.name}: no data chunk")


@dataclass(frozen=True)
class Segment:
    index: int
    source: str
    start_s: float
    end_s: float
    pcm: bytes = field(repr=False)

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / (2 * SAMPLE_RATE)


def segments(
    samples: Samples,
    *,
    max_s: float = 30.0,
    pad_ms: int = 300,
    merge_gap_ms: int = 800,
    min_speech_ms: int = 300,
    min_rms: float = 150.0,
) -> list[tuple[int, int]]:
    """Utterance sample spans: frames above an adaptive energy floor.

    Same detector as ``evaluate_sensevoice_rescue.segments``: 30 ms frames are
    voiced above ``max(3 * p20(rms), min_rms)``; gaps up to ``merge_gap_ms``
    are merged, spans shorter than ``min_speech_ms`` dropped, then padded by
    ``pad_ms`` each side and capped at ``max_s``.
    """

    count = len(samples) // FRAME
    if count == 0:
        return []
    frames = samples[: count * FRAME].reshape(count, FRAME).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    voiced = rms > max(3 * float(np.percentile(rms, 20)), min_rms)
    gap = max(1, merge_gap_ms // FRAME_MS)
    spans: list[list[int]] = []
    for index in np.flatnonzero(voiced).tolist():
        if spans and index - spans[-1][1] <= gap:
            spans[-1][1] = index
        else:
            spans.append([index, index])
    pad = pad_ms // FRAME_MS
    cap = max(1, int(max_s * 1000 / FRAME_MS))
    min_frames = max(1, min_speech_ms // FRAME_MS)
    out: list[tuple[int, int]] = []
    for start, end in spans:
        if end - start + 1 < min_frames:
            continue
        start, end = max(0, start - pad), min(count - 1, end + pad)
        end = min(end, start + cap - 1)
        out.append((start * FRAME, (end + 1) * FRAME))
    return out


# --------------------------------------------------------------------------- results


@dataclass
class StreamResult:
    engine: str
    text: str = ""
    first_partial_s: float | None = None  # first non-empty partial, from first chunk sent
    final_latency_s: float | None = None  # last final text, from last chunk sent
    finish_latency_s: float | None = None  # terminal event, from last chunk sent
    error: str | None = None
    timed_out: bool = False
    log_id: str | None = None  # provider trace id (Doubao X-Tt-Logid), not a secret
    # The provider ended the task as "no usable audio" (DashScope EmptyAudio,
    # Doubao 45000002 空音频): an empty result, not an error.
    provider_empty: bool = False


class _Timing:
    def __init__(self) -> None:
        self.first_chunk: float | None = None
        self.last_chunk: float | None = None
        self.first_partial: float | None = None
        self.final: float | None = None
        self.finish: float | None = None

    def partial(self, text: str) -> None:
        if text and self.first_partial is None:
            self.first_partial = time.monotonic()

    def apply(self, result: StreamResult) -> None:
        if self.first_chunk is not None and self.first_partial is not None:
            result.first_partial_s = round(self.first_partial - self.first_chunk, 3)
        if self.last_chunk is not None:
            if self.final is not None:
                result.final_latency_s = round(self.final - self.last_chunk, 3)
            if self.finish is not None:
                result.finish_latency_s = round(self.finish - self.last_chunk, 3)


def _bounded(text: object, limit: int = 200) -> str:
    value = " ".join(str(text).split())
    return value[:limit] or "unknown"


async def send_paced(
    pcm: bytes,
    *,
    chunk_ms: int,
    pace: float,
    send: Callable[[bytes, bool], Awaitable[None]],
    timing: _Timing,
    stop: asyncio.Event | None = None,
) -> None:
    """Send ``pcm`` in ``chunk_ms`` pieces at ``pace`` x real time (0 = burst).

    Chunk ``i`` leaves at ``t0 + i * chunk / pace`` on an absolute schedule so
    slow sends never accumulate drift.  ``send`` gets ``is_last`` for engines
    that flag the final packet; ``stop`` (session already ended) aborts.
    """

    step = max(1, chunk_ms) * BYTES_PER_MS
    chunks = [pcm[i : i + step] for i in range(0, len(pcm), step)] or [b""]
    interval = chunk_ms / 1000 / pace if pace > 0 else 0.0
    started = time.monotonic()
    for index, chunk in enumerate(chunks):
        delay = started + index * interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        if stop is not None and stop.is_set():
            return
        if timing.first_chunk is None:
            timing.first_chunk = time.monotonic()
        await send(chunk, index == len(chunks) - 1)
        timing.last_chunk = time.monotonic()


@dataclass(frozen=True)
class EngineSpec:
    name: str
    protocol: WireProtocol
    model: str
    url: str
    chunk_ms: int
    headers: tuple[tuple[str, str], ...] = field(repr=False, default=())
    options: Mapping[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, object]:
        """Config for the report: header *names* only, never values."""

        return {
            "protocol": self.protocol,
            "model": self.model,
            "url": self.url,
            "chunk_ms": self.chunk_ms,
            "header_names": sorted(name for name, _ in self.headers),
            "options": dict(self.options),
        }


@dataclass
class _Session:
    """Protocol hooks plugged into the shared connect/stream/wait skeleton."""

    # Sends the opening request; may read until the provider acknowledges it.
    # Returns False when the session already ended (e.g. task-failed).
    start: Callable[[ClientConnection], Awaitable[bool]]
    # Handles one server message; True = terminal (finished or failed).
    on_message: Callable[[str | bytes], bool]
    send_chunk: Callable[[ClientConnection, bytes, bool], Awaitable[None]]
    after_audio: Callable[[ClientConnection], Awaitable[None]]
    missing_terminal: str


async def _stream(
    spec: EngineSpec,
    pcm: bytes,
    session: _Session,
    result: StreamResult,
    timing: _Timing,
    *,
    pace: float,
    connect_timeout_s: float,
    result_timeout_s: float,
) -> None:
    finished = asyncio.Event()

    async def receive(ws: ClientConnection) -> None:
        async for message in ws:
            if session.on_message(message):
                finished.set()
                return

    try:
        async with asyncio.timeout(connect_timeout_s):
            ws = await connect(spec.url, additional_headers=list(spec.headers), max_size=None)
        async with ws:
            if ws.response is not None:
                result.log_id = ws.response.headers.get("X-Tt-Logid")
            async with asyncio.timeout(connect_timeout_s):
                if not await session.start(ws):
                    return
            receiver = asyncio.create_task(receive(ws))

            async def send(chunk: bytes, last: bool) -> None:
                await session.send_chunk(ws, chunk, last)

            try:
                await send_paced(
                    pcm, chunk_ms=spec.chunk_ms, pace=pace, send=send, timing=timing, stop=finished
                )
                if not finished.is_set():
                    await session.after_audio(ws)
                async with asyncio.timeout(result_timeout_s):
                    await asyncio.shield(receiver)
                if not finished.is_set() and result.error is None:
                    result.error = session.missing_terminal
            finally:
                receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await receiver
    except TimeoutError:
        result.timed_out = True
        result.error = result.error or "timeout"
    except (OSError, WebSocketException, ValueError, struct.error) as exc:
        # ValueError covers JSON decode errors and malformed binary frames.
        result.error = result.error or _bounded(f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- DashScope task protocol


def dashscope_run_task(task_id: str, model: str, options: Mapping[str, Any]) -> dict[str, Any]:
    parameters: dict[str, Any] = {"format": "pcm", "sample_rate": SAMPLE_RATE}
    for key in ("language_hints", "max_sentence_silence", "vad_model", "vocabulary_id"):
        if options.get(key) is not None:
            parameters[key] = options[key]
    return {
        "header": {"action": "run-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": parameters,
            "input": {},
        },
    }


def dashscope_finish_task(task_id: str) -> dict[str, Any]:
    return {
        "header": {"action": "finish-task", "task_id": task_id, "streaming": "duplex"},
        "payload": {"input": {}},
    }


def _json_object(message: str | bytes) -> dict[str, Any]:
    if isinstance(message, bytes):
        raise ValueError("unexpected binary frame")
    obj = json.loads(message)
    if not isinstance(obj, dict):
        raise ValueError("server message is not a JSON object")
    return obj


async def run_dashscope_task(
    spec: EngineSpec, pcm: bytes, *, pace: float, connect_timeout_s: float, result_timeout_s: float
) -> StreamResult:
    """fun-asr-realtime / qwen-audio-3.x-asr-flash-streaming task protocol."""

    result = StreamResult(engine=spec.name)
    timing = _Timing()
    task_id = str(uuid.uuid4())
    finals: dict[int, str] = {}
    pending: dict[int, str] = {}

    def on_message(message: str | bytes) -> bool:
        obj = _json_object(message)
        header = obj.get("header") or {}
        event = str(header.get("event") or "")
        if event == "task-failed":
            code = header.get("error_code") or "task-failed"
            if str(code).strip().casefold() == "emptyaudio":
                result.provider_empty = True
                timing.finish = time.monotonic()
                return True
            result.error = _bounded(f"{code}: {header.get('error_message') or ''}")
            return True
        if event == "task-finished":
            timing.finish = time.monotonic()
            return True
        if event == "result-generated":
            sentence = ((obj.get("payload") or {}).get("output") or {}).get("sentence")
            if not isinstance(sentence, dict) or sentence.get("heartbeat"):
                return False
            text = str(sentence.get("text") or "")
            sid = int(sentence.get("sentence_id") or 0)
            timing.partial(text)
            if sentence.get("sentence_end"):
                finals[sid] = text
                pending.pop(sid, None)
                timing.final = time.monotonic()
            else:
                pending[sid] = text
        return False

    async def start(ws: ClientConnection) -> bool:
        await ws.send(json.dumps(dashscope_run_task(task_id, spec.model, spec.options)))
        while True:  # audio only after task-started
            message = await ws.recv()
            if (_json_object(message).get("header") or {}).get("event") == "task-started":
                return True
            if on_message(message):
                return False

    async def send_chunk(ws: ClientConnection, chunk: bytes, _last: bool) -> None:
        if chunk:
            await ws.send(chunk)  # raw PCM, binary frame

    async def after_audio(ws: ClientConnection) -> None:
        await ws.send(json.dumps(dashscope_finish_task(task_id)))

    session = _Session(start, on_message, send_chunk, after_audio, "closed-before-task-finished")
    await _stream(
        spec, pcm, session, result, timing, pace=pace,
        connect_timeout_s=connect_timeout_s, result_timeout_s=result_timeout_s,
    )
    texts = [finals[sid] for sid in sorted(finals)]
    if result.error is not None:  # keep what was heard; the error flags it
        texts += [pending[sid] for sid in sorted(pending) if sid not in finals]
    result.text = "".join(texts)
    timing.apply(result)
    return result


# --------------------------------------------------------------------------- DashScope realtime (Qwen-ASR-Realtime)


def _event_id() -> str:
    return f"event_{uuid.uuid4().hex[:20]}"


def realtime_session_update(options: Mapping[str, Any]) -> dict[str, Any]:
    turn_detection: dict[str, Any] | None = None  # null = Manual mode
    if options.get("server_vad"):
        turn_detection = {
            "type": "server_vad",
            "threshold": float(options.get("vad_threshold", 0.0)),
            "silence_duration_ms": int(options.get("silence_duration_ms", 400)),
        }
    session: dict[str, Any] = {
        "modalities": ["text"],
        "input_audio_format": "pcm",
        "sample_rate": SAMPLE_RATE,
        "input_audio_transcription": {"language": options.get("language", "zh")},
        "turn_detection": turn_detection,
    }
    return {"event_id": _event_id(), "type": "session.update", "session": session}


def realtime_append(chunk: bytes) -> dict[str, Any]:
    return {
        "event_id": _event_id(),
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(chunk).decode("ascii"),
    }


def realtime_simple(event_type: str) -> dict[str, Any]:
    return {"event_id": _event_id(), "type": event_type}


async def run_dashscope_realtime(
    spec: EngineSpec, pcm: bytes, *, pace: float, connect_timeout_s: float, result_timeout_s: float
) -> StreamResult:
    """qwen3-asr-flash-realtime (Qwen-ASR-Realtime) event protocol."""

    result = StreamResult(engine=spec.name)
    timing = _Timing()
    finals: list[str] = []
    partial = ""
    server_vad = bool(spec.options.get("server_vad"))

    def on_message(message: str | bytes) -> bool:
        nonlocal partial
        obj = _json_object(message)
        kind = str(obj.get("type") or "")
        if kind in ("error", "conversation.item.input_audio_transcription.failed"):
            error = obj.get("error") or {}
            result.error = result.error or _bounded(
                f"{error.get('code') or kind}: {error.get('message') or ''}"
            )
            return True
        if kind == "conversation.item.input_audio_transcription.text":
            partial = str(obj.get("text") or "") + str(obj.get("stash") or "")
            timing.partial(partial)
        elif kind == "conversation.item.input_audio_transcription.completed":
            transcript = str(obj.get("transcript") or "")
            timing.partial(transcript)
            finals.append(transcript)
            partial = ""
            timing.final = time.monotonic()
        elif kind == "session.finished":
            timing.finish = time.monotonic()
            return True
        return False

    async def start(ws: ClientConnection) -> bool:
        await ws.send(json.dumps(realtime_session_update(spec.options)))
        while True:  # session.created first, then session.updated for our update
            message = await ws.recv()
            if _json_object(message).get("type") == "session.updated":
                return True
            if on_message(message):
                return False

    async def send_chunk(ws: ClientConnection, chunk: bytes, _last: bool) -> None:
        if chunk:
            await ws.send(json.dumps(realtime_append(chunk)))

    async def after_audio(ws: ClientConnection) -> None:
        if not server_vad:
            await ws.send(json.dumps(realtime_simple("input_audio_buffer.commit")))
        await ws.send(json.dumps(realtime_simple("session.finish")))

    session = _Session(start, on_message, send_chunk, after_audio, "closed-before-session-finished")
    await _stream(
        spec, pcm, session, result, timing, pace=pace,
        connect_timeout_s=connect_timeout_s, result_timeout_s=result_timeout_s,
    )
    texts = list(finals)
    if result.error is not None and partial:
        texts.append(partial)
    result.text = "".join(texts)
    timing.apply(result)
    return result


# --------------------------------------------------------------------------- Doubao SAUC binary protocol

DOUBAO_VERSION = 0b0001
DOUBAO_HEADER_WORDS = 0b0001  # header size = 1 x 4 bytes
MSG_FULL_CLIENT_REQUEST = 0b0001
MSG_AUDIO_ONLY_REQUEST = 0b0010
MSG_FULL_SERVER_RESPONSE = 0b1001
MSG_SERVER_ERROR = 0b1111
FLAG_NO_SEQUENCE = 0b0000
FLAG_POSITIVE_SEQUENCE = 0b0001
FLAG_LAST_NO_SEQUENCE = 0b0010
FLAG_LAST_NEGATIVE_SEQUENCE = 0b0011
SERIAL_NONE = 0b0000
SERIAL_JSON = 0b0001
COMPRESS_NONE = 0b0000
COMPRESS_GZIP = 0b0001
DOUBAO_EMPTY_AUDIO = 45000002


def doubao_header(message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    return bytes(
        (
            (DOUBAO_VERSION << 4) | DOUBAO_HEADER_WORDS,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0x00,
        )
    )


def doubao_full_client_request(payload: Mapping[str, Any]) -> bytes:
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    header = doubao_header(MSG_FULL_CLIENT_REQUEST, FLAG_NO_SEQUENCE, SERIAL_JSON, COMPRESS_GZIP)
    return header + struct.pack(">I", len(body)) + body


def doubao_audio_request(chunk: bytes, *, last: bool) -> bytes:
    body = gzip.compress(chunk)
    flags = FLAG_LAST_NO_SEQUENCE if last else FLAG_NO_SEQUENCE
    header = doubao_header(MSG_AUDIO_ONLY_REQUEST, flags, SERIAL_NONE, COMPRESS_GZIP)
    return header + struct.pack(">I", len(body)) + body


def doubao_request_payload(options: Mapping[str, Any]) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": False,
        "show_utterances": True,
        "result_type": "full",
    }
    if options.get("enable_nonstream"):
        request["enable_nonstream"] = True
    return {
        "user": {"uid": "memoria-asr-ab"},
        "audio": {"format": "pcm", "codec": "raw", "rate": SAMPLE_RATE, "bits": 16, "channel": 1},
        "request": request,
    }


@dataclass(frozen=True)
class DoubaoFrame:
    message_type: int
    flags: int
    sequence: int | None
    payload: Any
    error_code: int | None = None

    @property
    def is_last(self) -> bool:
        return bool(self.flags & FLAG_LAST_NO_SEQUENCE)


def _decode_body(body: bytes, serialization: int, compression: int) -> Any:
    if compression == COMPRESS_GZIP:
        body = gzip.decompress(body)
    if serialization == SERIAL_JSON:
        return json.loads(body.decode("utf-8")) if body else {}
    return body.decode("utf-8", errors="replace")


def doubao_parse_frame(data: bytes) -> DoubaoFrame:
    """Parse a server frame: header, [sequence], payload size, payload.

    Error frames carry ``error code (u32) + message size (u32) + message``
    instead.  Integers are big-endian; the sequence is signed (the last
    response carries a negative one with flags ``0b0011``).
    """

    if len(data) < 4:
        raise ValueError("doubao frame shorter than header")
    header_size = (data[0] & 0x0F) * 4
    message_type, flags = data[1] >> 4, data[1] & 0x0F
    serialization, compression = data[2] >> 4, data[2] & 0x0F
    offset = header_size
    if message_type == MSG_SERVER_ERROR:
        code, size = struct.unpack_from(">II", data, offset)
        body = data[offset + 8 : offset + 8 + size]
        try:
            payload = _decode_body(body, serialization, compression)
        except (OSError, ValueError):
            payload = body.decode("utf-8", errors="replace")
        return DoubaoFrame(message_type, flags, None, payload, error_code=code)
    sequence: int | None = None
    if flags & FLAG_POSITIVE_SEQUENCE:
        (sequence,) = struct.unpack_from(">i", data, offset)
        offset += 4
    (size,) = struct.unpack_from(">I", data, offset)
    body = data[offset + 4 : offset + 4 + size]
    return DoubaoFrame(message_type, flags, sequence, _decode_body(body, serialization, compression))


def doubao_result_text(payload: Any) -> str:
    """``result.text``; the field table calls ``result`` a list, the example a dict."""

    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        return str(result.get("text") or "")
    if isinstance(result, list):
        return "".join(str(item.get("text") or "") for item in result if isinstance(item, dict))
    return ""


async def run_doubao(
    spec: EngineSpec, pcm: bytes, *, pace: float, connect_timeout_s: float, result_timeout_s: float
) -> StreamResult:
    """Doubao big-model streaming ASR (SAUC v3 binary protocol)."""

    result = StreamResult(engine=spec.name)
    timing = _Timing()
    latest = ""
    final: str | None = None

    def on_message(message: str | bytes) -> bool:
        nonlocal latest, final
        if isinstance(message, str):
            raise ValueError("unexpected text frame")
        frame = doubao_parse_frame(message)
        if frame.message_type == MSG_SERVER_ERROR:
            if frame.error_code == DOUBAO_EMPTY_AUDIO:
                result.provider_empty = True
                timing.finish = time.monotonic()
                return True
            result.error = _bounded(f"{frame.error_code}: {frame.payload}")
            return True
        if frame.message_type != MSG_FULL_SERVER_RESPONSE:
            return False
        text = doubao_result_text(frame.payload)
        if text:
            latest = text
            timing.partial(text)
        if frame.is_last:
            final = text or latest
            timing.final = timing.finish = time.monotonic()
            return True
        return False

    async def start(ws: ClientConnection) -> bool:
        # Not waiting for the ack: the optimized bigmodel_async endpoint only
        # answers when the result changes, and error frames reach the receiver.
        await ws.send(doubao_full_client_request(doubao_request_payload(spec.options)))
        return True

    async def send_chunk(ws: ClientConnection, chunk: bytes, last: bool) -> None:
        await ws.send(doubao_audio_request(chunk, last=last))

    async def after_audio(_ws: ClientConnection) -> None:
        return None  # the last audio packet already carries the last-packet flag

    session = _Session(start, on_message, send_chunk, after_audio, "closed-before-last-response")
    await _stream(
        spec, pcm, session, result, timing, pace=pace,
        connect_timeout_s=connect_timeout_s, result_timeout_s=result_timeout_s,
    )
    result.text = final if final is not None else latest
    timing.apply(result)
    return result


RUNNERS: dict[WireProtocol, Callable[..., Awaitable[StreamResult]]] = {
    "dashscope-task": run_dashscope_task,
    "dashscope-realtime": run_dashscope_realtime,
    "doubao-sauc": run_doubao,
}


# --------------------------------------------------------------------------- text normalization / CER

_DIGITS = "零一二三四五六七八九"
_NUMBER = re.compile(r"(\d{1,2}):(\d{2})(?!\d)|(\d+(?:\.\d+)?)%|(\d+)\.(\d+)|(\d+)")


def _below_10k(value: int) -> str:
    out: list[str] = []
    zero = False
    for unit_value, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit = value // unit_value % 10
        if digit == 0:
            zero = bool(out)
            continue
        if zero:
            out.append("零")
            zero = False
        out.append(_DIGITS[digit] + unit)
    return "".join(out)


def int_reading(value: int) -> str:
    """Spoken cardinal: 10 -> 十, 105 -> 一百零五, 20260 -> 二万零二百六十."""

    if value == 0:
        return "零"
    if value >= 10**8:
        return digit_reading(str(value))
    high, low = divmod(value, 10_000)
    text = _below_10k(high) + "万" if high else ""
    if low:
        if high and low < 1000:
            text += "零"
        text += _below_10k(low)
    if text.startswith("一十"):
        text = text[1:]
    return text


def digit_reading(digits: str) -> str:
    """Digit-by-digit: 2026 -> 二零二六, 3729 -> 三七二九."""

    return "".join(_DIGITS[int(char)] for char in digits)


def _keep(char: str) -> bool:
    return unicodedata.category(char)[0] in "LN"


def strip_text(text: str) -> str:
    """Raw CER form: NFKC, lower case, punctuation/whitespace/symbols removed."""

    return "".join(char for char in unicodedata.normalize("NFKC", text).lower() if _keep(char))


def _number_variants(match: re.Match[str]) -> list[str]:
    hour, minute, percent, int_part, frac, run = match.groups()
    if hour is not None:
        spoken = int_reading(int(hour)) + "点"
        if not int(minute):
            return [spoken]  # 7:00 -> 七点
        minutes = ("零" if minute[0] == "0" else "") + int_reading(int(minute))
        return [spoken + minutes + "分", spoken + minutes]  # 七点零五分 / 七点三十
    if percent is not None:
        whole, _, decimals = percent.partition(".")
        spoken = int_reading(int(whole)) + ("点" + digit_reading(decimals) if decimals else "")
        return ["百分之" + spoken]
    if int_part is not None:
        return [int_reading(int(int_part)) + "点" + digit_reading(frac)]
    if run.startswith("0") or len(run) > 8:
        return [digit_reading(run)]
    value, digits = int_reading(int(run)), digit_reading(run)
    return [value] if value == digits else [value, digits]


def numeral_normalize(text: str, reference: str | None = None) -> str:
    """Arabic numerals -> spoken Chinese, then the raw strip.

    Each number has one or two readings (``2026`` -> 二千零二十六 or 二零二六);
    with a reference the combination closest to it wins, otherwise digit
    runs of 3+ read digit by digit and shorter ones as cardinals.  ``两`` and
    ``〇`` fold to ``二`` / ``零`` so the spoken and written forms compare equal.
    """

    base = unicodedata.normalize("NFKC", text)
    matches = list(_NUMBER.finditer(base))
    options = [_number_variants(match) for match in matches]

    def build(choice: Sequence[str]) -> str:
        pieces: list[str] = []
        cursor = 0
        for match, reading in zip(matches, choice, strict=True):
            pieces.append(base[cursor : match.start()])
            pieces.append(reading)
            cursor = match.end()
        pieces.append(base[cursor:])
        return strip_text("".join(pieces)).replace("两", "二").replace("〇", "零")

    if not matches:
        return build(())
    if reference is not None and 1 < math.prod(len(o) for o in options) <= 256:
        return min(
            (build(choice) for choice in itertools.product(*options)),
            key=lambda candidate: edit_distance(reference, candidate),
        )
    default = [
        o[-1] if len(o) > 1 and len(m.group(6) or "") >= 3 else o[0]
        for m, o in zip(matches, options, strict=True)
    ]
    return build(default)


def edit_distance(reference: str, hypothesis: str) -> int:
    previous = list(range(len(hypothesis) + 1))
    for i, ref_char in enumerate(reference, 1):
        current = [i]
        for j, hyp_char in enumerate(hypothesis, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ref_char != hyp_char))
            )
        previous = current
    return previous[-1]


@dataclass(frozen=True)
class Score:
    edits: int
    ref_chars: int
    raw_edits: int
    raw_ref_chars: int
    exact: bool
    empty: bool

    @property
    def cer(self) -> float:
        return self.edits / max(1, self.ref_chars)

    @property
    def raw_cer(self) -> float:
        return self.raw_edits / max(1, self.raw_ref_chars)


def score(reference: str, hypothesis: str) -> Score:
    ref_norm = numeral_normalize(reference)
    hyp_norm = numeral_normalize(hypothesis, ref_norm)
    ref_raw, hyp_raw = strip_text(reference), strip_text(hypothesis)
    return Score(
        edits=edit_distance(ref_norm, hyp_norm),
        ref_chars=len(ref_norm),
        raw_edits=edit_distance(ref_raw, hyp_raw),
        raw_ref_chars=len(ref_raw),
        exact=ref_norm == hyp_norm,
        empty=not hyp_raw,
    )


def similarity(reference: str, hypothesis: str) -> float:
    ref_norm = numeral_normalize(reference)
    hyp_norm = numeral_normalize(hypothesis, ref_norm)
    if not ref_norm or not hyp_norm:
        return 0.0
    return difflib.SequenceMatcher(None, ref_norm, hyp_norm, autojunk=False).ratio()


# --------------------------------------------------------------------------- reference script & alignment


@dataclass(frozen=True)
class Reference:
    line: int
    category: str
    text: str


def read_reference(path: Path) -> list[Reference]:
    refs: list[Reference] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        category, sep, sentence = line.partition("|")
        if not sep:
            category, sentence = "all", line
        refs.append(Reference(line=number, category=category.strip() or "all", text=sentence.strip()))
    return refs


@dataclass(frozen=True)
class Alignment:
    assigned: list[int | None]  # reference index per segment (metrics)
    best: list[tuple[int | None, float]]  # best reference + similarity per segment
    kind: list[str]  # matched | retake | unmatched


def similarity_matrix(texts: Sequence[Sequence[str]], refs: Sequence[Reference]) -> list[list[float]]:
    """Best similarity of any engine's text for each (segment, reference)."""

    return [
        [max((similarity(ref.text, text) for text in engine_texts if text), default=0.0) for ref in refs]
        for engine_texts in texts
    ]


def align(
    matrix: Sequence[Sequence[float]],
    *,
    mode: Literal["ordered", "best"] = "ordered",
    threshold: float = UNMATCHED_THRESHOLD,
) -> Alignment:
    """Assign segments to reference lines.

    ``ordered``: one-to-one and monotonic in reading order (weighted LCS over
    pairs with similarity >= threshold), so skipped lines and echo segments do
    not shift the rest.  Segments left over that still resemble a line are
    ``retake`` (a re-read, or the robot echoing it) and stay out of metrics.
    ``best``: every segment independently takes its best line (repeats and
    any order allowed).
    """

    n = len(matrix)
    m = len(matrix[0]) if n else 0
    best: list[tuple[int | None, float]] = []
    for row in matrix:
        if m == 0:
            best.append((None, 0.0))
            continue
        j = max(range(m), key=lambda k: row[k])
        best.append((j if row[j] >= threshold else None, round(row[j], 3)))
    assigned: list[int | None] = [None] * n
    if mode == "best":
        assigned = [j for j, _ in best]
    else:
        dp = [[0.0] * (m + 1) for _ in range(n + 1)]
        for i in range(1, n + 1):
            for j in range(1, m + 1):
                value = max(dp[i - 1][j], dp[i][j - 1])
                s = matrix[i - 1][j - 1]
                if s >= threshold:
                    value = max(value, dp[i - 1][j - 1] + s)
                dp[i][j] = value
        i, j = n, m
        while i > 0 and j > 0:
            s = matrix[i - 1][j - 1]
            if s >= threshold and dp[i][j] == dp[i - 1][j - 1] + s:
                assigned[i - 1] = j - 1
                i, j = i - 1, j - 1
            elif dp[i][j] == dp[i - 1][j]:
                i -= 1
            else:
                j -= 1
    kind = [
        "matched" if a is not None else ("retake" if b is not None else "unmatched")
        for a, (b, _) in zip(assigned, best, strict=True)
    ]
    return Alignment(assigned=assigned, best=best, kind=kind)


# --------------------------------------------------------------------------- metrics & report


def _pct(values: Sequence[float], q: float) -> float | None:
    return round(float(np.percentile(values, q)), 3) if values else None


def engine_metrics(
    engine: str,
    rows: Sequence[tuple[Reference, StreamResult]],
    all_results: Sequence[StreamResult],
) -> dict[str, object]:
    scores = [score(ref.text, res.text) for ref, res in rows]
    edits, chars = sum(s.edits for s in scores), sum(s.ref_chars for s in scores)
    raw_edits, raw_chars = sum(s.raw_edits for s in scores), sum(s.raw_ref_chars for s in scores)
    finals = [r.final_latency_s for _, r in rows if r.final_latency_s is not None and r.text]
    partials = [r.first_partial_s for _, r in rows if r.first_partial_s is not None]
    count = len(rows)
    return {
        "engine": engine,
        "matched": count,
        "cer": round(edits / chars, 4) if chars else None,
        "cer_raw": round(raw_edits / raw_chars, 4) if raw_chars else None,
        "exact_rate": round(sum(s.exact for s in scores) / count, 3) if count else None,
        "empty_rate": round(sum(s.empty for s in scores) / count, 3) if count else None,
        "final_latency_p50_s": _pct(finals, 50),
        "final_latency_p90_s": _pct(finals, 90),
        "first_partial_p50_s": _pct(partials, 50),
        "errors": sum(r.error is not None for r in all_results),
        "timeouts": sum(r.timed_out for r in all_results),
    }


def build_report(
    segs: Sequence[Segment],
    refs: Sequence[Reference],
    engines: Sequence[str],
    results: Sequence[Mapping[str, StreamResult]],
    alignment: Alignment,
    *,
    show_text: bool,
) -> dict[str, Any]:
    matched = [i for i, k in enumerate(alignment.kind) if k == "matched"]
    categories = sorted({refs[alignment.assigned[i] or 0].category for i in matched})
    overall: list[dict[str, object]] = []
    per_category: dict[str, list[dict[str, object]]] = {c: [] for c in categories}
    for engine in engines:
        everything = [results[i][engine] for i in range(len(segs))]
        rows = [(refs[alignment.assigned[i] or 0], results[i][engine]) for i in matched]
        overall.append(engine_metrics(engine, rows, everything))
        for category in categories:
            subset = [(ref, res) for ref, res in rows if ref.category == category]
            cat_indices = [i for i in matched if refs[alignment.assigned[i] or 0].category == category]
            per_category[category].append(
                engine_metrics(engine, subset, [results[i][engine] for i in cat_indices])
            )

    sentences: list[dict[str, Any]] = []
    for j, ref in enumerate(refs):
        seg_ids = [i for i in matched if alignment.assigned[i] == j]
        row: dict[str, Any] = {"line": ref.line, "category": ref.category, "segments": seg_ids}
        if show_text:
            row["reference"] = ref.text
        for i in seg_ids:
            for engine in engines:
                res = results[i][engine]
                s = score(ref.text, res.text)
                entry: dict[str, Any] = {
                    "cer": round(s.cer, 3),
                    "exact": s.exact,
                    "final_latency_s": res.final_latency_s,
                    "error": res.error,
                }
                if show_text:
                    entry["text"] = res.text
                row.setdefault("engines", {})[engine] = entry
        sentences.append(row)

    others = [i for i, k in enumerate(alignment.kind) if k != "matched"]
    false_trigger = {
        engine: {
            "segments_with_text": sum(bool(strip_text(results[i][engine].text)) for i in others),
            "chars": sum(len(strip_text(results[i][engine].text)) for i in others),
        }
        for engine in engines
    }
    segment_rows: list[dict[str, Any]] = []
    for seg, kind, assigned, (best_ref, best_sim) in zip(
        segs, alignment.kind, alignment.assigned, alignment.best, strict=True
    ):
        seg_row: dict[str, Any] = {
            "index": seg.index,
            "source": seg.source,
            "start_s": round(seg.start_s, 2),
            "end_s": round(seg.end_s, 2),
            "kind": kind,
            "reference_line": refs[assigned].line if assigned is not None else None,
            "best_reference_line": refs[best_ref].line if best_ref is not None else None,
            "best_similarity": best_sim,
            "engines": {},
        }
        for engine in engines:
            res = results[seg.index][engine]
            detail: dict[str, Any] = {
                "text_chars": len(strip_text(res.text)),
                "first_partial_s": res.first_partial_s,
                "final_latency_s": res.final_latency_s,
                "finish_latency_s": res.finish_latency_s,
                "error": res.error,
                "timed_out": res.timed_out,
                "provider_empty": res.provider_empty,
            }
            if res.log_id:
                detail["log_id"] = res.log_id
            if show_text:
                detail["text"] = res.text
            seg_row["engines"][engine] = detail
        segment_rows.append(seg_row)

    return {
        "segments_total": len(segs),
        "matched": len(matched),
        "retake": alignment.kind.count("retake"),
        "unmatched": alignment.kind.count("unmatched"),
        "references": len(refs),
        "references_not_found": sum(1 for s in sentences if not s["segments"]),
        "overall": overall,
        "per_category": per_category,
        "sentences": sentences,
        "false_trigger_text": false_trigger,
        "segments": segment_rows,
    }


def _fmt(value: object, *, pct: bool = False) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value * 100:.1f}%" if pct else f"{value:.2f}"
    return str(value)


def render_markdown(report: Mapping[str, Any], engines: Sequence[str], *, show_text: bool) -> str:
    lines = [
        f"Segments {report['segments_total']}: matched {report['matched']}, "
        f"retake {report['retake']}, unmatched {report['unmatched']}; "
        f"reference lines {report['references']} (not found {report['references_not_found']})",
        "",
        "| engine | matched | CER | CER raw | exact | empty | final p50 s | final p90 s "
        "| first partial p50 s | errors | timeouts |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in report["overall"]:
        lines.append(
            f"| {m['engine']} | {m['matched']} | {_fmt(m['cer'], pct=True)} | "
            f"{_fmt(m['cer_raw'], pct=True)} | {_fmt(m['exact_rate'], pct=True)} | "
            f"{_fmt(m['empty_rate'], pct=True)} | {_fmt(m['final_latency_p50_s'])} | "
            f"{_fmt(m['final_latency_p90_s'])} | {_fmt(m['first_partial_p50_s'])} | "
            f"{m['errors']} | {m['timeouts']} |"
        )
    lines += ["", "CER by category", "", "| category | n | " + " | ".join(engines) + " |"]
    lines.append("|---|---|" + "---|" * len(engines))
    for category, metrics in report["per_category"].items():
        count = metrics[0]["matched"] if metrics else 0
        cells = " | ".join(_fmt(m["cer"], pct=True) for m in metrics)
        lines.append(f"| {category} | {count} | {cells} |")
    lines += ["", "Per sentence (CER; `!` = error)", ""]
    header = "| line | category | seg | " + " | ".join(engines) + " |"
    if show_text:
        header = "| line | category | reference | seg | " + " | ".join(engines) + " |"
    lines += [header, "|" + "---|" * (header.count("|") - 1)]
    for row in report["sentences"]:
        prefix = f"| {row['line']} | {row['category']} | "
        if show_text:
            prefix += f"{row['reference']} | "
        if not row["segments"]:
            lines.append(prefix + "not found | " + " | ".join("-" for _ in engines) + " |")
            continue
        for i in row["segments"]:
            engine_cells: list[str] = []
            for engine in engines:
                entry = row["engines"][engine]
                cell = f"{entry['cer'] * 100:.0f}%" + ("!" if entry["error"] else "")
                if show_text:
                    cell += f" {entry['text']}"
                engine_cells.append(cell)
            lines.append(prefix + f"{i} | " + " | ".join(engine_cells) + " |")
    lines += ["", "False-trigger text on retake/unmatched segments", ""]
    lines += ["| engine | segments with text | chars |", "|---|---|---|"]
    for engine, stats in report["false_trigger_text"].items():
        lines.append(f"| {engine} | {stats['segments_with_text']} | {stats['chars']} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- engines from env / CLI


def build_specs(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[list[EngineSpec], list[str]]:
    """Engine specs for the requested engines; skipped engines get a note."""

    chunk_ms = {"funasr": 100, "qwen-audio": 100, "qwen-realtime": 100, "doubao": 200}
    for item in args.chunk_ms or []:
        name, _, value = item.partition("=")
        if name not in chunk_ms or not value.isdigit():
            raise SystemExit(f"--chunk-ms expects ENGINE=MS with ENGINE in {', '.join(ENGINES)}")
        chunk_ms[name] = int(value)
    specs: list[EngineSpec] = []
    notes: list[str] = []
    dashscope_key = env.get("DASHSCOPE_API_KEY", "").strip()
    base = args.dashscope_base.rstrip("/")
    task_options: dict[str, Any] = {
        "language_hints": [args.language],
        "max_sentence_silence": args.max_sentence_silence_ms,
    }
    for name in args.engines:
        if name in ("funasr", "qwen-audio", "qwen-realtime") and not dashscope_key:
            notes.append(f"{name}: skipped, DASHSCOPE_API_KEY not set")
            continue
        auth = (("Authorization", f"Bearer {dashscope_key}"),)
        if name == "funasr":
            specs.append(
                EngineSpec(name, "dashscope-task", args.funasr_model, f"{base}/api-ws/v1/inference",
                           chunk_ms[name], auth, dict(task_options))
            )
        elif name == "qwen-audio":
            options = dict(task_options)
            if args.qwen_audio_vad_model:
                options["vad_model"] = args.qwen_audio_vad_model
            specs.append(
                EngineSpec(name, "dashscope-task", args.qwen_audio_model,
                           f"{base}/api-ws/v1/inference", chunk_ms[name], auth, options)
            )
        elif name == "qwen-realtime":
            realtime_options: dict[str, Any] = {"language": args.language}
            if args.qwen_realtime_vad:
                realtime_options.update(server_vad=True, vad_threshold=0.0, silence_duration_ms=400)
            specs.append(
                EngineSpec(name, "dashscope-realtime", args.qwen_realtime_model,
                           f"{base}/api-ws/v1/realtime?model={args.qwen_realtime_model}",
                           chunk_ms[name], (*auth, ("OpenAI-Beta", "realtime=v1")), realtime_options)
            )
        elif name == "doubao":
            app_id = env.get("DOUBAO_ASR_APP_ID", "").strip()
            token = env.get("DOUBAO_ASR_ACCESS_TOKEN", "").strip()
            api_key = env.get("DOUBAO_ASR_API_KEY", "").strip()
            resource = env.get("DOUBAO_ASR_RESOURCE_ID", "").strip() or DOUBAO_DEFAULT_RESOURCE_ID
            if app_id and token:
                creds: tuple[tuple[str, str], ...] = (
                    ("X-Api-App-Key", app_id), ("X-Api-Access-Key", token),
                )
            elif api_key:
                creds = (("X-Api-Key", api_key),)
            else:
                notes.append(
                    "doubao: skipped, need DOUBAO_ASR_APP_ID + DOUBAO_ASR_ACCESS_TOKEN "
                    "(or DOUBAO_ASR_API_KEY)"
                )
                continue
            headers = (*creds, ("X-Api-Resource-Id", resource), ("X-Api-Connect-Id", str(uuid.uuid4())))
            specs.append(
                EngineSpec(name, "doubao-sauc", f"bigmodel ({resource})", args.doubao_url,
                           chunk_ms[name], headers, {"enable_nonstream": args.doubao_nonstream,
                                                     "resource_id": resource})
            )
    return specs, notes


def _with_fresh_connect_id(spec: EngineSpec) -> EngineSpec:
    if spec.protocol != "doubao-sauc":
        return spec
    headers = tuple(
        (name, str(uuid.uuid4()) if name == "X-Api-Connect-Id" else value) for name, value in spec.headers
    )
    return EngineSpec(spec.name, spec.protocol, spec.model, spec.url, spec.chunk_ms, headers, spec.options)


async def run_all(
    segs: Sequence[Segment],
    specs: Sequence[EngineSpec],
    *,
    pace: float,
    concurrency: int,
    connect_timeout_s: float,
    result_timeout_s: float,
    progress: Callable[[str], None] | None = None,
) -> list[dict[str, StreamResult]]:
    """Stream every segment to every engine; engines of one segment run together."""

    semaphore = asyncio.Semaphore(max(1, concurrency))
    results: list[dict[str, StreamResult]] = [{} for _ in segs]
    done = 0

    async def one(seg: Segment, spec: EngineSpec) -> None:
        nonlocal done
        async with semaphore:
            runner = RUNNERS[spec.protocol]
            res = await runner(
                _with_fresh_connect_id(spec), seg.pcm, pace=pace,
                connect_timeout_s=connect_timeout_s, result_timeout_s=result_timeout_s,
            )
        results[seg.index][spec.name] = res
        done += 1
        if progress is not None:
            status = "error" if res.error else "ok"
            progress(f"[{done}/{len(segs) * len(specs)}] seg {seg.index} {spec.name}: {status}")

    await asyncio.gather(*(one(seg, spec) for seg in segs for spec in specs))
    return results


def load_segments(paths: Sequence[Path], args: argparse.Namespace) -> list[Segment]:
    out: list[Segment] = []
    for path in paths:
        samples = read_wav_pcm(path)
        for start, end in segments(
            samples, max_s=args.max_audio_s, pad_ms=args.pad_ms,
            merge_gap_ms=args.merge_gap_ms, min_rms=args.min_rms,
        ):
            out.append(
                Segment(len(out), path.name, start / SAMPLE_RATE, end / SAMPLE_RATE,
                        samples[start:end].astype("<i2").tobytes())
            )
    return out


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("recordings", nargs="+", type=Path, help="16 kHz mono s16le WAVs")
    parser.add_argument("--reference", type=Path, required=True, help="script: [category|]sentence per line")
    parser.add_argument("--engines", type=lambda s: [e.strip() for e in s.split(",") if e.strip()],
                        default=list(ENGINES), help=f"comma list from {','.join(ENGINES)}")
    parser.add_argument("--output", type=Path, help="JSON report path (default outputs/asr-ab/)")
    parser.add_argument("--show-text", action="store_true", help="include transcripts (private speech)")
    parser.add_argument("--pace", type=float, default=1.0, help="x real time; 0 = send as fast as possible")
    parser.add_argument("--concurrency", type=int, default=4, help="max simultaneous provider sessions")
    parser.add_argument("--chunk-ms", action="append", metavar="ENGINE=MS",
                        help="override chunk size (defaults 100 DashScope, 200 Doubao)")
    parser.add_argument("--connect-timeout-s", type=float, default=10.0)
    parser.add_argument("--result-timeout-s", type=float, default=10.0,
                        help="wait for the final result after the last chunk")
    parser.add_argument("--align", choices=("ordered", "best"), default="ordered")
    parser.add_argument("--max-segments", type=int, default=0, help="only the first N segments (smoke)")
    parser.add_argument("--dry-run", action="store_true", help="segment only; no network")
    parser.add_argument("--max-audio-s", type=float, default=30.0)
    parser.add_argument("--pad-ms", type=int, default=300)
    parser.add_argument("--merge-gap-ms", type=int, default=800)
    parser.add_argument("--min-rms", type=float, default=150.0)
    parser.add_argument("--language", default="zh")
    parser.add_argument("--dashscope-base", default=DASHSCOPE_BASE,
                        help="wss://dashscope.aliyuncs.com or wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com")
    parser.add_argument("--funasr-model", default="fun-asr-realtime")
    parser.add_argument("--qwen-audio-model", default="qwen-audio-3.1-asr-flash-streaming")
    parser.add_argument("--qwen-audio-vad-model", choices=("near_meeting_16k", "far_field_meeting_16k"))
    parser.add_argument("--max-sentence-silence-ms", type=int, default=550,
                        help="DashScope task VAD silence (production FUNASR_MAX_SENTENCE_SILENCE_MS)")
    parser.add_argument("--qwen-realtime-model", default="qwen3-asr-flash-realtime")
    parser.add_argument("--qwen-realtime-vad", action="store_true",
                        help="server_vad (threshold 0.0, 400 ms) instead of one manual commit per segment")
    parser.add_argument("--doubao-url", default=DOUBAO_URL, help="bigmodel_async (recommended) or bigmodel")
    parser.add_argument("--doubao-nonstream", action="store_true",
                        help="enable_nonstream second-pass recognition (bigmodel_async only)")
    args = parser.parse_args(argv)
    unknown = [e for e in args.engines if e not in ENGINES]
    if unknown:
        parser.error(f"unknown engines {unknown}; choose from {', '.join(ENGINES)}")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    refs = read_reference(args.reference)
    if not refs:
        raise SystemExit("reference script has no lines")
    segs = load_segments(args.recordings, args)
    if args.max_segments:
        segs = segs[: args.max_segments]
    if not segs:
        raise SystemExit("no utterances found")
    durations = [s.duration_s for s in segs]
    print(
        f"{len(args.recordings)} recording(s), {len(segs)} segment(s), "
        f"{sum(durations):.1f} s audio (median {statistics.median(durations):.2f} s); "
        f"{len(refs)} reference line(s)",
        file=sys.stderr,
    )
    if args.dry_run:
        for seg in segs:
            print(f"seg {seg.index} {seg.source} {seg.start_s:.2f}-{seg.end_s:.2f}s", file=sys.stderr)
        return 0
    specs, notes = build_specs(args, os.environ)
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    if not specs:
        raise SystemExit("no engine has credentials; nothing to run")
    started = time.monotonic()
    results = asyncio.run(
        run_all(
            segs, specs, pace=args.pace, concurrency=args.concurrency,
            connect_timeout_s=args.connect_timeout_s, result_timeout_s=args.result_timeout_s,
            progress=lambda line: print(line, file=sys.stderr),
        )
    )
    engines = [spec.name for spec in specs]
    texts = [[results[i][e].text for e in engines] for i in range(len(segs))]
    alignment = align(similarity_matrix(texts, refs), mode=args.align)
    report = build_report(segs, refs, engines, results, alignment, show_text=args.show_text)
    report["config"] = {
        "recordings": [path.name for path in args.recordings],
        "reference": args.reference.name,
        "pace": args.pace,
        "concurrency": args.concurrency,
        "align": args.align,
        "segmenter": {
            "pad_ms": args.pad_ms, "merge_gap_ms": args.merge_gap_ms,
            "min_rms": args.min_rms, "max_audio_s": args.max_audio_s,
        },
        "engines": {spec.name: spec.public() for spec in specs},
        "skipped": notes,
        "show_text": args.show_text,
        "wall_s": round(time.monotonic() - started, 1),
    }
    output = args.output or Path("outputs/asr-ab") / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render_markdown(report, engines, show_text=args.show_text))
    print(f"\nJSON report: {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
