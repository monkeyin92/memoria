"""Allowlisted device-command contract for future hardware clients."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

DEVICE_TOPICS = frozenset(
    {
        "face.expression.set",
        "face.animation.play",
        "screen.subtitle.set",
        "screen.brightness.set",
        "led.color.set",
        "audio.volume.set",
        "audio.mute.set",
        "motor.motion.play",
        "sensor.snapshot.request",
        "device.status.request",
        "device.ota.check",
        "device.ota.apply",
        "device.reboot",
    }
)

DeviceCommandStatus = Literal["applied", "rejected", "expired", "failed"]


@dataclass(frozen=True, slots=True)
class DeviceCommand:
    command_id: str
    topic: str
    ttl_ms: int
    issued_monotonic_ms: int
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.command_id.strip():
            raise ValueError("command_id is required")
        if self.topic not in DEVICE_TOPICS:
            raise ValueError("device topic is not allowlisted")
        if not 1 <= self.ttl_ms <= 60_000:
            raise ValueError("device command ttl must be between 1 and 60000 ms")
        if self.issued_monotonic_ms < 0:
            raise ValueError("issued_monotonic_ms must be non-negative")

    def expired(self, now_monotonic_ms: int) -> bool:
        if now_monotonic_ms < 0:
            raise ValueError("now_monotonic_ms must be non-negative")
        return now_monotonic_ms >= self.issued_monotonic_ms + self.ttl_ms

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "v": 1,
                "type": "device.command",
                "command_id": self.command_id,
                "topic": self.topic,
                "ttl_ms": self.ttl_ms,
                "issued_at_monotonic_ms": self.issued_monotonic_ms,
                "payload": self.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str) -> DeviceCommand:
        try:
            value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid device command JSON") from exc
        if not isinstance(value, dict) or value.get("type") != "device.command":
            raise ValueError("invalid device command envelope")
        payload = value.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("device command payload must be an object")
        issued = value.get("issued_at_monotonic_ms")
        ttl = value.get("ttl_ms")
        if isinstance(issued, bool) or not isinstance(issued, int):
            raise ValueError("issued_at_monotonic_ms must be an integer")
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise ValueError("ttl_ms must be an integer")
        command_id = value.get("command_id")
        topic = value.get("topic")
        if not isinstance(command_id, str) or not isinstance(topic, str):
            raise ValueError("command_id and topic must be strings")
        return cls(
            command_id=command_id,
            topic=topic,
            ttl_ms=ttl,
            issued_monotonic_ms=issued,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class DeviceCommandAck:
    command_id: str
    status: DeviceCommandStatus
    device_monotonic_ms: int
    message: str = ""

    def __post_init__(self) -> None:
        if not self.command_id.strip():
            raise ValueError("command_id is required")
        if self.device_monotonic_ms < 0:
            raise ValueError("device_monotonic_ms must be non-negative")

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "v": 1,
                "type": "device.command_ack",
                "command_id": self.command_id,
                "status": self.status,
                "device_monotonic_ms": self.device_monotonic_ms,
                "message": self.message,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str) -> DeviceCommandAck:
        try:
            value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid device command ack JSON") from exc
        if not isinstance(value, dict) or value.get("type") != "device.command_ack":
            raise ValueError("invalid device command ack envelope")
        monotonic = value.get("device_monotonic_ms")
        if isinstance(monotonic, bool) or not isinstance(monotonic, int):
            raise ValueError("device_monotonic_ms must be an integer")
        status = value.get("status")
        if status not in {"applied", "rejected", "expired", "failed"}:
            raise ValueError("invalid device command ack status")
        message = value.get("message", "")
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        command_id = value.get("command_id")
        if not isinstance(command_id, str):
            raise ValueError("command_id must be a string")
        return cls(
            command_id=command_id,
            status=status,
            device_monotonic_ms=monotonic,
            message=message,
        )


__all__ = ["DEVICE_TOPICS", "DeviceCommand", "DeviceCommandAck", "DeviceCommandStatus"]
