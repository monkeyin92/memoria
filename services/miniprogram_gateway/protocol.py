"""Versioned, bounded binary frames for the Mini Program media WebSocket."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

PROTOCOL_VERSION: Final = 1
HEADER_SIZE: Final = 20
MAX_AUDIO_PAYLOAD_BYTES: Final = 64 * 1024
_HEADER: Final = struct.Struct("!BBHIQI")


class ProtocolError(ValueError):
    """A WebSocket media message does not meet the published contract."""


class FrameType(IntEnum):
    UPLINK_AUDIO = 1
    DOWNLINK_AUDIO = 2


@dataclass(frozen=True, slots=True)
class PcmFrame:
    frame_type: FrameType
    sequence: int
    timestamp_ms: int
    payload: bytes


def encode_pcm_frame(
    frame_type: FrameType,
    *,
    sequence: int,
    timestamp_ms: int,
    payload: bytes,
) -> bytes:
    """Encode a PCM frame with a strict network-byte-order header."""
    _validate_fields(
        frame_type=frame_type,
        sequence=sequence,
        timestamp_ms=timestamp_ms,
        payload=payload,
    )
    return _HEADER.pack(
        int(frame_type),
        PROTOCOL_VERSION,
        0,
        sequence,
        timestamp_ms,
        len(payload),
    ) + payload


def decode_pcm_frame(
    raw: bytes | bytearray | memoryview,
    *,
    expected_type: FrameType | None = None,
) -> PcmFrame:
    """Decode one whole WebSocket binary message; streaming fragmentation is forbidden."""
    data = bytes(raw)
    if len(data) < HEADER_SIZE:
        raise ProtocolError("PCM frame header is incomplete")
    type_value, version, flags, sequence, timestamp_ms, payload_length = _HEADER.unpack(
        data[:HEADER_SIZE]
    )
    if version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported PCM protocol version")
    if flags != 0:
        raise ProtocolError("PCM frame flags must be zero")
    try:
        frame_type = FrameType(type_value)
    except ValueError as exc:
        raise ProtocolError("unsupported PCM frame type") from exc
    if expected_type is not None and frame_type is not expected_type:
        raise ProtocolError("unexpected PCM frame direction")
    payload = data[HEADER_SIZE:]
    if payload_length != len(payload):
        raise ProtocolError("PCM frame payload length does not match header")
    _validate_fields(
        frame_type=frame_type,
        sequence=sequence,
        timestamp_ms=timestamp_ms,
        payload=payload,
    )
    return PcmFrame(
        frame_type=frame_type,
        sequence=sequence,
        timestamp_ms=timestamp_ms,
        payload=payload,
    )


def _validate_fields(
    *,
    frame_type: FrameType,
    sequence: int,
    timestamp_ms: int,
    payload: bytes,
) -> None:
    if not isinstance(frame_type, FrameType):
        raise ProtocolError("unsupported PCM frame type")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 0xFFFFFFFF:
        raise ProtocolError("invalid PCM frame sequence")
    if (
        isinstance(timestamp_ms, bool)
        or not isinstance(timestamp_ms, int)
        or not 0 <= timestamp_ms <= 0xFFFFFFFFFFFFFFFF
    ):
        raise ProtocolError("invalid PCM frame timestamp")
    if not payload or len(payload) > MAX_AUDIO_PAYLOAD_BYTES:
        raise ProtocolError("invalid PCM frame payload length")
