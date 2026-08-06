"""Volcengine bidirectional TTS binary WebSocket protocol."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from services.agent.src.contracts.events import TimedWord


class MessageType(IntEnum):
    FULL_CLIENT_REQUEST = 0x1
    FULL_SERVER_RESPONSE = 0x9
    AUDIO_ONLY_SERVER = 0xB
    ERROR = 0xF


class MessageFlag(IntEnum):
    NO_SEQUENCE = 0x0
    POSITIVE_SEQUENCE = 0x1
    LAST_WITHOUT_SEQUENCE = 0x2
    NEGATIVE_SEQUENCE = 0x3
    WITH_EVENT = 0x4


class EventType(IntEnum):
    START_CONNECTION = 1
    FINISH_CONNECTION = 2
    CONNECTION_STARTED = 50
    CONNECTION_FAILED = 51
    CONNECTION_FINISHED = 52
    START_SESSION = 100
    CANCEL_SESSION = 101
    FINISH_SESSION = 102
    SESSION_STARTED = 150
    SESSION_CANCELED = 151
    SESSION_FINISHED = 152
    SESSION_FAILED = 153
    USAGE_RESPONSE = 154
    TASK_REQUEST = 200
    TTS_SENTENCE_START = 350
    TTS_SENTENCE_END = 351
    TTS_RESPONSE = 352
    TTS_ENDED = 359
    TTS_SUBTITLE = 364


_CONNECTION_EVENTS = frozenset(
    {
        EventType.START_CONNECTION,
        EventType.FINISH_CONNECTION,
        EventType.CONNECTION_STARTED,
        EventType.CONNECTION_FAILED,
        EventType.CONNECTION_FINISHED,
    }
)
_CONNECTION_RESPONSE_EVENTS = frozenset(
    {
        EventType.CONNECTION_STARTED,
        EventType.CONNECTION_FAILED,
        EventType.CONNECTION_FINISHED,
    }
)


@dataclass(frozen=True, slots=True)
class ServerMessage:
    message_type: int
    flag: int
    event: int
    session_id: str
    connect_id: str
    payload: bytes
    error_code: int = 0

    def json_payload(self) -> dict[str, Any]:
        if not self.payload:
            return {}
        value = json.loads(self.payload)
        if not isinstance(value, dict):
            raise ValueError("Doubao response payload must be a JSON object")
        return value


def _sized(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def build_client_message(
    event: EventType,
    *,
    session_id: str = "",
    payload: dict[str, Any] | None = None,
) -> bytes:
    if event not in _CONNECTION_EVENTS and not session_id:
        raise ValueError("session_id is required for session and task events")
    encoded = json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")).encode()
    frame = bytearray((0x11, 0x14, 0x10, 0x00))
    frame.extend(struct.pack(">i", int(event)))
    if event not in _CONNECTION_EVENTS:
        frame.extend(_sized(session_id.encode()))
    frame.extend(_sized(encoded))
    return bytes(frame)


def parse_server_message(data: bytes) -> ServerMessage:
    if len(data) < 8:
        raise ValueError("Doubao frame is shorter than its header")
    version = data[0] >> 4
    header_words = data[0] & 0x0F
    if version != 1 or header_words < 1:
        raise ValueError("unsupported Doubao protocol header")
    header_size = header_words * 4
    if len(data) < header_size:
        raise ValueError("truncated Doubao protocol header")

    message_type = data[1] >> 4
    flag = data[1] & 0x0F
    serialization = data[2] >> 4
    compression = data[2] & 0x0F
    if compression != 0:
        raise ValueError("compressed Doubao frames are not supported")
    if message_type not in {
        MessageType.FULL_SERVER_RESPONSE,
        MessageType.AUDIO_ONLY_SERVER,
        MessageType.ERROR,
    }:
        raise ValueError(f"unsupported Doubao server message type: {message_type}")

    offset = header_size

    def take_uint32() -> int:
        nonlocal offset
        if offset + 4 > len(data):
            raise ValueError("truncated Doubao uint32 field")
        value = int(struct.unpack_from(">I", data, offset)[0])
        offset += 4
        return value

    def take_int32() -> int:
        nonlocal offset
        if offset + 4 > len(data):
            raise ValueError("truncated Doubao int32 field")
        value = int(struct.unpack_from(">i", data, offset)[0])
        offset += 4
        return value

    def take_sized() -> bytes:
        nonlocal offset
        size = take_uint32()
        if offset + size > len(data):
            raise ValueError("truncated Doubao sized field")
        value = data[offset : offset + size]
        offset += size
        return value

    if flag in {MessageFlag.POSITIVE_SEQUENCE, MessageFlag.NEGATIVE_SEQUENCE}:
        take_int32()

    error_code = take_uint32() if message_type == MessageType.ERROR else 0
    event = 0
    session_id = ""
    connect_id = ""
    if flag == MessageFlag.WITH_EVENT:
        event = take_int32()
        if event not in _CONNECTION_EVENTS:
            session_id = take_sized().decode("utf-8")
        if event in _CONNECTION_RESPONSE_EVENTS:
            connect_id = take_sized().decode("utf-8")

    payload = take_sized()
    if offset != len(data):
        raise ValueError("unexpected trailing bytes in Doubao frame")
    if message_type == MessageType.AUDIO_ONLY_SERVER and serialization != 0:
        raise ValueError("Doubao audio frame must use raw serialization")
    if message_type != MessageType.AUDIO_ONLY_SERVER and serialization not in {0, 1}:
        raise ValueError("unsupported Doubao response serialization")
    return ServerMessage(
        message_type=message_type,
        flag=flag,
        event=event,
        session_id=session_id,
        connect_id=connect_id,
        payload=payload,
        error_code=error_code,
    )


def build_start_session_payload(
    *,
    speaker: str,
    sample_rate: int = 24000,
    speech_rate: int = 0,
    loudness_rate: int = 0,
    pitch: int = 0,
    context_texts: tuple[str, ...] = (),
    uid: str,
) -> dict[str, Any]:
    req_params: dict[str, Any] = {
        "speaker": speaker,
        "audio_params": {
            "format": "pcm",
            "sample_rate": sample_rate,
            "speech_rate": speech_rate,
            "loudness_rate": loudness_rate,
            "enable_subtitle": True,
        },
        "additions": json.dumps(
            {"disable_markdown_filter": True},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    if pitch:
        req_params["post_process"] = {"pitch": pitch}
    if context_texts:
        req_params["context_texts"] = list(context_texts)
    return {
        "user": {"uid": uid},
        "event": int(EventType.START_SESSION),
        "namespace": "BidirectionalTTS",
        "req_params": req_params,
    }


def build_task_request_payload(text: str) -> dict[str, Any]:
    return {
        "event": int(EventType.TASK_REQUEST),
        "namespace": "BidirectionalTTS",
        "req_params": {"text": text},
    }


def parse_subtitle_words(payload: bytes | dict[str, Any]) -> tuple[TimedWord, ...]:
    value: Any = json.loads(payload) if isinstance(payload, bytes) else payload
    if not isinstance(value, dict):
        return ()
    raw_words = value.get("words")
    if not isinstance(raw_words, list):
        nested = value.get("payload")
        raw_words = nested.get("words") if isinstance(nested, dict) else None
    if not isinstance(raw_words, list):
        return ()
    words: list[TimedWord] = []
    for raw in raw_words:
        if not isinstance(raw, dict):
            continue
        word = raw.get("word")
        start = raw.get("startTime")
        end = raw.get("endTime")
        if not isinstance(word, str) or not isinstance(start, (int, float)):
            continue
        if not isinstance(end, (int, float)) or end < start:
            continue
        words.append(
            TimedWord(
                text=word,
                begin_ms=round(float(start) * 1000),
                end_ms=round(float(end) * 1000),
            )
        )
    return tuple(words)


def pcm_duration_ms(pcm_bytes: bytes, *, sample_rate: int = 24000) -> int:
    return int((len(pcm_bytes) // 2) * 1000 / sample_rate)


_RAW_ALIGNMENT_MAX_ERROR_MS = 120
_SCALED_ALIGNMENT_MAX_ERROR_MS = 700
_ALIGNMENT_MAX_RELATIVE_ERROR = 0.20


def align_subtitle_words(
    words: tuple[TimedWord, ...],
    *,
    pcm_duration_ms_value: int,
) -> tuple[tuple[TimedWord, ...], str]:
    if not words or words[-1].end_ms <= 0:
        return (), "degraded"
    subtitle_duration_ms = words[-1].end_ms
    difference = abs(subtitle_duration_ms - pcm_duration_ms_value)
    relative_error = difference / max(subtitle_duration_ms, pcm_duration_ms_value)
    if (
        difference <= _RAW_ALIGNMENT_MAX_ERROR_MS
        and relative_error <= _ALIGNMENT_MAX_RELATIVE_ERROR
    ):
        return words, "ok"
    factor = pcm_duration_ms_value / subtitle_duration_ms
    aligned = tuple(
        TimedWord(
            text=word.text,
            begin_ms=round(word.begin_ms * factor),
            end_ms=round(word.end_ms * factor),
            punctuation=word.punctuation,
        )
        for word in words
    )
    if (
        difference <= _SCALED_ALIGNMENT_MAX_ERROR_MS
        and relative_error <= _ALIGNMENT_MAX_RELATIVE_ERROR
    ):
        return aligned, "scaled"
    return aligned, "degraded"
