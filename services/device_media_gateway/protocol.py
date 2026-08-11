"""Strict MemoriaAudioFrameV1 and device JSON message validation."""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

PROTOCOL_VERSION: Final = 1
HEADER_SIZE: Final = 30
MAX_OPUS_PAYLOAD_BYTES: Final = 4_096
KNOWN_FLAGS: Final = 0

# version, type, flags, stream_epoch, sequence, sample_start,
# frame_samples, generation_id, payload_size
_HEADER: Final = struct.Struct("!BBHIIQIIH")

UPLINK_SAMPLE_RATE: Final = 16_000
DOWNLINK_SAMPLE_RATE: Final = 24_000
FRAME_MS: Final = 20
UPLINK_FRAME_SAMPLES: Final = UPLINK_SAMPLE_RATE * FRAME_MS // 1_000
DOWNLINK_FRAME_SAMPLES: Final = DOWNLINK_SAMPLE_RATE * FRAME_MS // 1_000


class ProtocolError(ValueError):
    """A device message violates the Memoria hardware contract."""


class FrameType(IntEnum):
    UPLINK_AUDIO = 1
    DOWNLINK_AUDIO = 2


@dataclass(frozen=True, slots=True)
class MemoriaAudioFrameV1:
    frame_type: FrameType
    flags: int
    stream_epoch: int
    sequence: int
    sample_start: int
    frame_samples: int
    generation_id: int
    payload: bytes

    @property
    def payload_size(self) -> int:
        return len(self.payload)


def encode_audio_frame(
    frame_type: FrameType,
    *,
    stream_epoch: int,
    sequence: int,
    sample_start: int,
    frame_samples: int,
    generation_id: int,
    payload: bytes,
    flags: int = 0,
    max_payload_bytes: int = MAX_OPUS_PAYLOAD_BYTES,
) -> bytes:
    """Encode one complete network-order MemoriaAudioFrameV1."""
    _validate_frame_fields(
        frame_type=frame_type,
        flags=flags,
        stream_epoch=stream_epoch,
        sequence=sequence,
        sample_start=sample_start,
        frame_samples=frame_samples,
        generation_id=generation_id,
        payload=payload,
        max_payload_bytes=max_payload_bytes,
    )
    return (
        _HEADER.pack(
            PROTOCOL_VERSION,
            int(frame_type),
            flags,
            stream_epoch,
            sequence,
            sample_start,
            frame_samples,
            generation_id,
            len(payload),
        )
        + payload
    )


def decode_audio_frame(
    raw: bytes | bytearray | memoryview,
    *,
    expected_type: FrameType | None = None,
    max_payload_bytes: int = MAX_OPUS_PAYLOAD_BYTES,
) -> MemoriaAudioFrameV1:
    """Decode one whole WebSocket binary message; fragmentation is forbidden."""
    data = bytes(raw)
    if len(data) < HEADER_SIZE:
        raise ProtocolError("Memoria audio frame header is incomplete")
    (
        version,
        type_value,
        flags,
        stream_epoch,
        sequence,
        sample_start,
        frame_samples,
        generation_id,
        payload_size,
    ) = _HEADER.unpack(data[:HEADER_SIZE])
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported Memoria audio protocol version")
    try:
        frame_type = FrameType(type_value)
    except ValueError as exc:
        raise ProtocolError("unsupported Memoria audio frame type") from exc
    if expected_type is not None and frame_type is not expected_type:
        raise ProtocolError("unexpected Memoria audio frame direction")
    payload = data[HEADER_SIZE:]
    if payload_size != len(payload):
        raise ProtocolError("Memoria audio payload length does not match header")
    frame = MemoriaAudioFrameV1(
        frame_type=frame_type,
        flags=flags,
        stream_epoch=stream_epoch,
        sequence=sequence,
        sample_start=sample_start,
        frame_samples=frame_samples,
        generation_id=generation_id,
        payload=payload,
    )
    _validate_frame_fields(
        frame_type=frame.frame_type,
        flags=frame.flags,
        stream_epoch=frame.stream_epoch,
        sequence=frame.sequence,
        sample_start=frame.sample_start,
        frame_samples=frame.frame_samples,
        generation_id=frame.generation_id,
        payload=frame.payload,
        max_payload_bytes=max_payload_bytes,
    )
    return frame


def validate_audio_shape(frame: MemoriaAudioFrameV1) -> None:
    """Require the fixed first-version mono 20 ms sample clock."""
    expected = (
        UPLINK_FRAME_SAMPLES
        if frame.frame_type is FrameType.UPLINK_AUDIO
        else DOWNLINK_FRAME_SAMPLES
    )
    if frame.frame_samples != expected:
        raise ProtocolError("Memoria audio frame_samples does not match direction")


def _validate_frame_fields(
    *,
    frame_type: FrameType,
    flags: int,
    stream_epoch: int,
    sequence: int,
    sample_start: int,
    frame_samples: int,
    generation_id: int,
    payload: bytes,
    max_payload_bytes: int,
) -> None:
    if not isinstance(frame_type, FrameType):
        raise ProtocolError("unsupported Memoria audio frame type")
    if isinstance(flags, bool) or not isinstance(flags, int) or not 0 <= flags <= 0xFFFF:
        raise ProtocolError("invalid Memoria audio flags")
    if flags & ~KNOWN_FLAGS:
        raise ProtocolError("Memoria audio flags are not supported")
    _require_uint(stream_epoch, 32, "stream_epoch")
    if stream_epoch == 0:
        raise ProtocolError("Memoria audio stream_epoch must be positive")
    _require_uint(sequence, 32, "sequence")
    _require_uint(sample_start, 64, "sample_start")
    _require_uint(frame_samples, 32, "frame_samples")
    if frame_samples == 0:
        raise ProtocolError("Memoria audio frame_samples must be positive")
    _require_uint(generation_id, 32, "generation_id")
    if not isinstance(payload, bytes):
        raise ProtocolError("Memoria audio payload must be bytes")
    if (
        isinstance(max_payload_bytes, bool)
        or not isinstance(max_payload_bytes, int)
        or not 1 <= max_payload_bytes <= 0xFFFF
    ):
        raise ValueError("invalid maximum Opus payload size")
    if not payload or len(payload) > max_payload_bytes:
        raise ProtocolError("Memoria audio payload size is invalid")


def _require_uint(value: int, bits: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << bits:
        raise ProtocolError(f"invalid Memoria audio {name}")


DEVICE_HELLO_KEYS: Final = frozenset(
    {
        "type",
        "version",
        "device_id",
        "firmware_version",
        "board_profile",
        "stream_epoch",
        "capabilities",
        "audio",
    }
)
DEVICE_CAPABILITY_KEYS: Final = frozenset(
    {"display", "microphone", "speaker", "device_aec", "physical_button"}
)
DEVICE_AUDIO_KEYS: Final = frozenset(
    {"uplink_codec", "uplink_sample_rate", "downlink_sample_rate", "channels", "frame_ms"}
)


def validate_device_hello(
    hello: object,
    *,
    device_id: str,
    stream_epoch: int,
) -> dict[str, object]:
    """Validate the only accepted first text message from a device."""
    if not isinstance(hello, dict) or set(hello) != DEVICE_HELLO_KEYS:
        raise ProtocolError("invalid device.hello schema")
    if hello.get("type") != "device.hello" or hello.get("version") != 1:
        raise ProtocolError("invalid device.hello version")
    if hello.get("device_id") != device_id:
        raise ProtocolError("device.hello device_id does not match ticket")
    if hello.get("stream_epoch") != stream_epoch:
        raise ProtocolError("device.hello stream_epoch does not match ticket")
    for key in ("firmware_version", "board_profile"):
        value = hello.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ProtocolError("invalid device.hello identity metadata")
    capabilities = hello.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != DEVICE_CAPABILITY_KEYS:
        raise ProtocolError("invalid device.hello capabilities")
    if any(not isinstance(value, bool) for value in capabilities.values()):
        raise ProtocolError("invalid device.hello capability value")
    audio = hello.get("audio")
    if not isinstance(audio, dict) or set(audio) != DEVICE_AUDIO_KEYS:
        raise ProtocolError("invalid device.hello audio schema")
    if (
        audio.get("uplink_codec") != "opus"
        or audio.get("uplink_sample_rate") != UPLINK_SAMPLE_RATE
        or audio.get("downlink_sample_rate") != DOWNLINK_SAMPLE_RATE
        or audio.get("channels") != 1
        or audio.get("frame_ms") != FRAME_MS
    ):
        raise ProtocolError("device.hello audio capabilities are unsupported")
    return {
        "type": "device.hello",
        "version": 1,
        "device_id": device_id,
        "firmware_version": hello["firmware_version"],
        "board_profile": hello["board_profile"],
        "stream_epoch": stream_epoch,
        "capabilities": dict(capabilities),
        "audio": dict(audio),
    }


_EVENT_REQUIRED_KEYS: Final[dict[str, frozenset[str]]] = {
    "listen.start": frozenset({"type", "stream_epoch", "sample_start"}),
    "listen.stop": frozenset({"type", "stream_epoch", "sample_start"}),
    "button.event": frozenset({"type", "stream_epoch", "button", "action", "generation_id"}),
    "device.telemetry": frozenset({"type", "stream_epoch", "metrics"}),
    "playback.started": frozenset({"type", "stream_epoch", "generation_id", "played_sample_end"}),
    "playback.progress": frozenset({"type", "stream_epoch", "generation_id", "played_sample_end"}),
    "playback.ended": frozenset(
        {"type", "stream_epoch", "generation_id", "played_sample_end", "reason"}
    ),
    "playback.error": frozenset(
        {"type", "stream_epoch", "generation_id", "played_sample_end", "error_code"}
    ),
    "device.command_ack": frozenset({"type", "stream_epoch", "command_id", "status"}),
    "session.close": frozenset({"type", "stream_epoch", "reason"}),
}
DEVICE_EVENT_TYPES: Final = frozenset(_EVENT_REQUIRED_KEYS)
PLAYBACK_RECEIPT_TYPES: Final = frozenset(
    {"playback.started", "playback.progress", "playback.ended", "playback.error"}
)


def validate_device_event(
    raw: object,
    *,
    stream_epoch: int,
) -> dict[str, object]:
    """Validate a bounded allowlist of device-to-server JSON messages."""
    if not isinstance(raw, dict):
        raise ProtocolError("device event must be an object")
    event_type = raw.get("type")
    if not isinstance(event_type, str) or event_type not in _EVENT_REQUIRED_KEYS:
        raise ProtocolError("unsupported device event")
    if set(raw) != _EVENT_REQUIRED_KEYS[event_type]:
        raise ProtocolError("invalid device event schema")
    if raw.get("stream_epoch") != stream_epoch:
        raise ProtocolError("device event stream_epoch does not match session")
    result = dict(raw)
    if event_type in {"listen.start", "listen.stop"}:
        _require_event_uint(result, "sample_start", 64)
    elif event_type == "button.event":
        _require_bounded_string(result, "button", 64)
        _require_bounded_string(result, "action", 64)
        _require_event_uint(result, "generation_id", 32)
    elif event_type == "device.telemetry":
        metrics = result["metrics"]
        if not isinstance(metrics, dict) or not metrics or len(metrics) > 32:
            raise ProtocolError("invalid device telemetry")
        for key, value in metrics.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ProtocolError("invalid device telemetry key")
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                raise ProtocolError("invalid device telemetry value")
            if isinstance(value, str) and len(value) > 256:
                raise ProtocolError("device telemetry value is too long")
    elif event_type in PLAYBACK_RECEIPT_TYPES:
        _require_event_uint(result, "generation_id", 32)
        _require_event_uint(result, "played_sample_end", 64)
        if event_type == "playback.ended":
            _require_bounded_string(result, "reason", 64)
        elif event_type == "playback.error":
            _require_bounded_string(result, "error_code", 128)
    elif event_type == "device.command_ack":
        _require_bounded_string(result, "command_id", 128)
        _require_bounded_string(result, "status", 32)
    elif event_type == "session.close":
        _require_bounded_string(result, "reason", 128)
    return result


def parse_json_message(text: object, *, max_bytes: int = 16_384) -> object:
    if not isinstance(text, str) or len(text.encode("utf-8")) > max_bytes:
        raise ProtocolError("device JSON message is too large")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid device JSON message") from exc


def _require_event_uint(event: dict[str, object], name: str, bits: int) -> None:
    _require_uint(event.get(name), bits, name)  # type: ignore[arg-type]


def _require_bounded_string(event: dict[str, object], name: str, max_length: int) -> None:
    value = event.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ProtocolError(f"invalid device event {name}")
