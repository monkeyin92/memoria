"""Small, self-owned JSON media-v1 protocol models.

This is deliberately independent of a WebRTC or protobuf implementation.  It
is the contract seam used by tests and can later be encoded by a bridge without
pulling a third-party media framework into the Python voice core.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

MEDIA_PROTOCOL = "media-v1"
MAX_ENVELOPE_BYTES = 256 * 1024


class AudioEncoding(StrEnum):
    PCM_S16LE = "pcm_s16le"
    OPUS = "opus"


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    session_id: str
    account_id: str = ""
    participant_id: str = ""
    device_id: str = ""
    client_type: str = "h5"
    stream_epoch: int = 1

    def __post_init__(self) -> None:
        _required_string(self.session_id, "session_id")
        _non_negative_int(self.stream_epoch, "stream_epoch")
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")


@dataclass(frozen=True, slots=True)
class AudioFormat:
    encoding: AudioEncoding
    sample_rate: int
    channels: int = 1
    frame_ms: int = 20

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.channels <= 0 or self.frame_ms <= 0:
            raise ValueError("audio format values must be positive")


@dataclass(frozen=True, slots=True)
class AudioFrame:
    identity: SessionIdentity
    sequence: int
    capture_start_sample: int
    frame_samples: int
    payload: bytes
    crc32c: int | None = None
    discontinuity: bool = False

    def __post_init__(self) -> None:
        _non_negative_int(self.sequence, "sequence")
        _non_negative_int(self.capture_start_sample, "capture_start_sample")
        if self.frame_samples <= 0:
            raise ValueError("frame_samples must be positive")
        if not self.payload:
            raise ValueError("audio payload must not be empty")
        if self.crc32c is not None and not 0 <= self.crc32c <= 0xFFFFFFFF:
            raise ValueError("crc32c must fit an unsigned 32-bit integer")

    @property
    def capture_end_sample(self) -> int:
        return self.capture_start_sample + self.frame_samples

    def to_payload(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "capture_start_sample": self.capture_start_sample,
            "frame_samples": self.frame_samples,
            "payload_b64": base64.b64encode(self.payload).decode("ascii"),
            "crc32c": self.crc32c,
            "discontinuity": self.discontinuity,
        }


@dataclass(frozen=True, slots=True)
class PlaybackProgress:
    identity: SessionIdentity
    generation_id: int
    received_sequence: int
    rendered_sample_end: int
    client_monotonic_ms: int
    approximate: bool = True
    turn_id: int = 0
    tool_epoch: int = 0

    def __post_init__(self) -> None:
        for value, name in (
            (self.generation_id, "generation_id"),
            (self.received_sequence, "received_sequence"),
            (self.rendered_sample_end, "rendered_sample_end"),
            (self.client_monotonic_ms, "client_monotonic_ms"),
            (self.turn_id, "turn_id"),
            (self.tool_epoch, "tool_epoch"),
        ):
            _non_negative_int(value, name)

    def to_payload(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "received_sequence": self.received_sequence,
            "rendered_sample_end": self.rendered_sample_end,
            "client_monotonic_ms": self.client_monotonic_ms,
            "approximate": self.approximate,
            "turn_id": self.turn_id,
            "tool_epoch": self.tool_epoch,
        }


@dataclass(frozen=True, slots=True)
class MediaEnvelope:
    """Versioned event envelope with a bounded JSON representation."""

    type: str
    event_id: str
    session_id: str
    stream_epoch: int
    sequence: int
    turn_id: int = 0
    generation_id: int = 0
    tool_epoch: int = 0
    server_monotonic_ms: int = 0
    payload: Mapping[str, Any] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self) -> None:
        _required_string(self.type, "type")
        _required_string(self.event_id, "event_id")
        _required_string(self.session_id, "session_id")
        if self.version != 1:
            raise ValueError("unsupported media envelope version")
        for value, name in (
            (self.stream_epoch, "stream_epoch"),
            (self.sequence, "sequence"),
            (self.turn_id, "turn_id"),
            (self.generation_id, "generation_id"),
            (self.tool_epoch, "tool_epoch"),
            (self.server_monotonic_ms, "server_monotonic_ms"),
        ):
            _non_negative_int(value, name)
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be an object")

    @classmethod
    def create(
        cls,
        *,
        type: str,
        event_id: str,
        session_id: str,
        stream_epoch: int,
        sequence: int,
        turn_id: int = 0,
        generation_id: int = 0,
        tool_epoch: int = 0,
        server_monotonic_ms: int = 0,
        payload: Mapping[str, Any] | None = None,
    ) -> MediaEnvelope:
        return cls(
            type=type,
            event_id=event_id,
            session_id=session_id,
            stream_epoch=stream_epoch,
            sequence=sequence,
            turn_id=turn_id,
            generation_id=generation_id,
            tool_epoch=tool_epoch,
            server_monotonic_ms=server_monotonic_ms,
            payload=payload or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.version,
            "type": self.type,
            "event_id": self.event_id,
            "session_id": self.session_id,
            "stream_epoch": self.stream_epoch,
            "sequence": self.sequence,
            "turn_id": self.turn_id,
            "generation_id": self.generation_id,
            "tool_epoch": self.tool_epoch,
            "server_monotonic_ms": self.server_monotonic_ms,
            "payload": dict(self.payload),
        }

    def encode(self) -> bytes:
        raw = json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError("media envelope exceeds size limit")
        return raw

    @classmethod
    def decode(cls, raw: str | bytes | bytearray | memoryview) -> MediaEnvelope:
        data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if len(data) > MAX_ENVELOPE_BYTES:
            raise ValueError("media envelope exceeds size limit")
        try:
            decoded = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid media envelope JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("media envelope must be a JSON object")
        protocol = decoded.get("protocol")
        if protocol is not None and protocol != MEDIA_PROTOCOL:
            raise ValueError("unsupported media protocol")
        if decoded.get("v") != 1:
            raise ValueError("unsupported media envelope version")
        payload = decoded.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("media envelope payload must be an object")
        return cls(
            type=_required_string(decoded.get("type"), "type"),
            event_id=_required_string(decoded.get("event_id"), "event_id"),
            session_id=_required_string(decoded.get("session_id"), "session_id"),
            stream_epoch=_non_negative_int(decoded.get("stream_epoch"), "stream_epoch"),
            sequence=_non_negative_int(decoded.get("sequence"), "sequence"),
            turn_id=_non_negative_int(decoded.get("turn_id", 0), "turn_id"),
            generation_id=_non_negative_int(decoded.get("generation_id", 0), "generation_id"),
            tool_epoch=_non_negative_int(decoded.get("tool_epoch", 0), "tool_epoch"),
            server_monotonic_ms=_non_negative_int(
                decoded.get("server_monotonic_ms", 0),
                "server_monotonic_ms",
            ),
            payload=cast(Mapping[str, Any], payload),
            version=1,
        )


__all__ = [
    "MEDIA_PROTOCOL",
    "MAX_ENVELOPE_BYTES",
    "AudioEncoding",
    "AudioFormat",
    "AudioFrame",
    "MediaEnvelope",
    "PlaybackProgress",
    "SessionIdentity",
]
