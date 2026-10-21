"""Volcengine (Doubao) streaming ASR v3 binary protocol, ``sauc`` family.

Pure codec, no network. Every frame is a 4-byte header, an optional 4-byte
sequence, a 4-byte big-endian payload size and the payload. Client frames carry
a positive sequence; the final audio packet carries a negative one. The service
answers each request with a full server response and marks the last one with the
same "last package" flag.

There are no event codes and no connection/session handshake frames in this
protocol. After the WebSocket upgrade the client sends exactly one full client
request (JSON configuration), then audio-only packets, then a negative packet;
the recognition task is complete when the server responds with the last flag.
Event framing belongs to the duplex TTS and realtime dialogue protocols.

Reference: https://www.volcengine.com/docs/6561/1354869 (大模型流式语音识别API).
"""

from __future__ import annotations

import gzip
import json
import logging
import struct
import zlib
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR_RESPONSE = 0b1111

NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
LAST_NO_SEQUENCE = 0b0010
LAST_WITH_SEQUENCE = 0b0011

NO_SERIALIZATION = 0b0000
JSON = 0b0001

NO_COMPRESSION = 0b0000
GZIP = 0b0001


@dataclass(frozen=True, slots=True)
class VolcFrame:
    message_type: int
    sequence: int | None
    is_last: bool
    payload: dict[str, Any]
    error_code: int | None = None


def _header(
    message_type: int,
    flags: int,
    *,
    serialization: int = JSON,
    compression: int = GZIP,
) -> bytes:
    return bytes(
        [
            (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE,
            (message_type << 4) | flags,
            (serialization << 4) | compression,
            0x00,
        ]
    )


def _gzip_json(payload: dict[str, Any]) -> bytes:
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def build_full_client_request(
    payload: dict[str, Any],
    *,
    sequence: int = 1,
) -> bytes:
    """Build the JSON configuration frame that starts one recognition task."""

    if sequence < 1:
        raise ValueError("volc ASR client sequence must be positive")
    body = _gzip_json(payload)
    return (
        _header(FULL_CLIENT_REQUEST, POS_SEQUENCE)
        + struct.pack(">i", sequence)
        + struct.pack(">I", len(body))
        + body
    )


def build_audio(pcm: bytes, *, sequence: int, last: bool = False) -> bytes:
    """Build an audio-only frame; the last frame is marked as the final one.

    The document defines both the negative-sequence final packet and the
    "last package, no sequence" flag. This builder uses the negative sequence,
    which is what the official Python and Java demo clients send.
    """

    if not pcm:
        raise ValueError("volc ASR audio frame must not be empty")
    if sequence < 1:
        raise ValueError("volc ASR audio sequence must be positive")
    body = gzip.compress(pcm)
    flags = LAST_WITH_SEQUENCE if last else POS_SEQUENCE
    seq = -sequence if last else sequence
    return (
        _header(AUDIO_ONLY_REQUEST, flags, serialization=NO_SERIALIZATION)
        + struct.pack(">i", seq)
        + struct.pack(">I", len(body))
        + body
    )


def build_empty_last_audio() -> bytes:
    """Build the "last package, no sequence" frame for an already-drained task.

    The document defines flag ``0b0010`` for exactly this case, and the
    official Go SDK allows an empty payload only on the last packet. A task
    whose audio ended on a chunk boundary is closed with this frame instead of
    resending audio the service already has.
    """

    body = gzip.compress(b"")
    return (
        _header(AUDIO_ONLY_REQUEST, LAST_NO_SEQUENCE, serialization=NO_SERIALIZATION)
        + struct.pack(">I", len(body))
        + body
    )


def build_task_request(
    *,
    uid: str = "memoria",
    model_name: str = "bigmodel",
    sample_rate: int = 16_000,
    end_window_ms: int = 550,
    enable_nonstream: bool = True,
    show_utterances: bool = True,
    boosting_table_id: str | None = None,
) -> dict[str, Any]:
    """The full client request payload for one VAD segment."""

    request: dict[str, Any] = {
        "model_name": model_name,
        "enable_nonstream": enable_nonstream,
        "show_utterances": show_utterances,
        "end_window_size": end_window_ms,
    }
    if boosting_table_id:
        # The document nests hotword tables under ``request.corpus``.
        request["corpus"] = {"boosting_table_id": boosting_table_id}
    return {
        "user": {"uid": uid},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": sample_rate,
            "bits": 16,
            "channel": 1,
        },
        "request": request,
    }


def parse_frame(data: bytes | str) -> VolcFrame:
    """Decode one server frame without trusting provider-controlled sizes."""

    if isinstance(data, str):
        raise ValueError("volc ASR server frames must be binary")
    if len(data) < 4:
        raise ValueError("volc ASR frame is shorter than its header")
    header_size = (data[0] & 0x0F) * 4
    if header_size < 4 or header_size > len(data):
        raise ValueError("volc ASR frame has an invalid header size")
    message_type = data[1] >> 4
    flags = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    offset = header_size
    sequence: int | None = None
    if flags & POS_SEQUENCE:
        if offset + 4 > len(data):
            raise ValueError("volc ASR frame is missing its sequence")
        sequence = struct.unpack(">i", data[offset : offset + 4])[0]
        offset += 4
    is_last = bool(flags & LAST_NO_SEQUENCE)
    error_code: int | None = None
    if message_type == SERVER_ERROR_RESPONSE:
        if offset + 4 > len(data):
            raise ValueError("volc ASR error frame is missing its code")
        error_code = struct.unpack(">i", data[offset : offset + 4])[0]
        offset += 4
    if offset + 4 > len(data):
        raise ValueError("volc ASR frame is missing its payload size")
    payload_size = struct.unpack(">I", data[offset : offset + 4])[0]
    offset += 4
    if offset + payload_size > len(data):
        raise ValueError("volc ASR frame payload is truncated")
    body = data[offset : offset + payload_size]
    if compression == GZIP and body:
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError(f"volc ASR gzip decompression failed: {exc}") from exc
    payload: dict[str, Any] = {}
    if body and serialization == JSON:
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("volc ASR frame payload is not JSON") from exc
        if isinstance(decoded, dict):
            payload = decoded
        else:
            logger.warning(
                "volc ASR frame payload is not a JSON object: %s",
                type(decoded).__name__,
            )
    return VolcFrame(
        message_type=message_type,
        sequence=sequence,
        is_last=is_last,
        payload=payload,
        error_code=error_code,
    )


@dataclass(frozen=True, slots=True)
class VolcTranscript:
    text: str
    definite: bool
    begin_ms: int
    end_ms: int
    word_times: tuple[tuple[str, int, int], ...] = ()


def extract_transcript(payload: dict[str, Any]) -> VolcTranscript | None:
    """Pull text and word timestamps out of a result payload.

    ``result.text`` is the whole task transcript when ``result_type`` is the
    document default (``full``). A transcript is ``definite`` when the service
    flags the result or every utterance as final; the session treats the
    server's last-package response, not this flag, as the task boundary.
    """

    result = payload.get("result")
    if not isinstance(result, dict):
        result = payload
    raw_text = result.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        return None
    utterances = result.get("utterances")
    words: list[tuple[str, int, int]] = []
    if isinstance(utterances, list):
        for utterance in utterances:
            if not isinstance(utterance, dict):
                continue
            utterance_words = utterance.get("words")
            if not isinstance(utterance_words, list):
                continue
            for word in utterance_words:
                if not isinstance(word, dict):
                    continue
                text = word.get("text")
                if not isinstance(text, str) or not text:
                    continue
                begin = _as_int(word.get("start_time"))
                end = _as_int(word.get("end_time"), begin)
                words.append((text, begin, max(begin, end)))
    begin_ms = _as_int(result.get("start_time"), -1)
    end_ms = _as_int(result.get("end_time"), -1)
    if isinstance(utterances, list) and utterances:
        first = utterances[0]
        last = utterances[-1]
        if isinstance(first, dict):
            first_start = _as_int(first.get("start_time"), -1)
            if first_start >= 0:
                begin_ms = first_start if begin_ms < 0 else min(begin_ms, first_start)
        if isinstance(last, dict):
            last_end = _as_int(last.get("end_time"), -1)
            if last_end >= 0:
                end_ms = last_end if end_ms < 0 else max(end_ms, last_end)
    if words:
        word_begin = min(word[1] for word in words)
        word_end = max(word[2] for word in words)
        begin_ms = word_begin if begin_ms < 0 else min(begin_ms, word_begin)
        end_ms = word_end if end_ms < 0 else max(end_ms, word_end)
    if begin_ms < 0:
        begin_ms = 0
    if end_ms < 0:
        end_ms = begin_ms
    definite = bool(result.get("definite"))
    if isinstance(utterances, list) and utterances:
        # ``definite`` is an utterance-level flag in the document, so the whole
        # transcript is only definite when every utterance is.
        flags = [
            bool(utterance.get("definite"))
            for utterance in utterances
            if isinstance(utterance, dict)
        ]
        definite = definite or (bool(flags) and all(flags))
    return VolcTranscript(
        text=raw_text.strip(),
        definite=definite,
        begin_ms=max(0, begin_ms),
        end_ms=max(begin_ms, end_ms),
        word_times=tuple(words),
    )


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "AUDIO_ONLY_REQUEST",
    "FULL_CLIENT_REQUEST",
    "FULL_SERVER_RESPONSE",
    "GZIP",
    "JSON",
    "LAST_WITH_SEQUENCE",
    "NO_SERIALIZATION",
    "POS_SEQUENCE",
    "SERVER_ERROR_RESPONSE",
    "VolcFrame",
    "VolcTranscript",
    "build_audio",
    "build_empty_last_audio",
    "build_full_client_request",
    "build_task_request",
    "extract_transcript",
    "parse_frame",
]
