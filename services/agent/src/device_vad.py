"""Authenticated device VAD events projected onto the AgentSession state seam."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from livekit import rtc

DEVICE_VAD_TOPIC = "voice-agent.device-vad"
DEVICE_MICROPHONE_TRACK_NAME = "device-microphone"


@dataclass(slots=True)
class DeviceVadProjector:
    """Consume only reliable VAD facts from this session's device microphone."""

    session: Any
    session_id: str
    _last_sample: int = 0
    _active: bool = False
    _participant_sid: str | None = None
    _participant_identity: str | None = None

    def accept(self, packet: Any) -> bool:
        if getattr(packet, "topic", None) != DEVICE_VAD_TOPIC:
            return False
        if getattr(packet, "kind", None) != rtc.DataPacketKind.KIND_RELIABLE:
            return True
        participant = getattr(packet, "participant", None)
        if not _owns_device_microphone(participant):
            return True
        participant_sid = str(getattr(participant, "sid", ""))
        participant_identity = str(getattr(participant, "identity", ""))
        if not participant_sid or (
            self._participant_identity is not None
            and participant_identity != self._participant_identity
        ):
            return True
        try:
            event = json.loads(bytes(packet.data).decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return True
        if not isinstance(event, dict) or set(event) != {
            "type",
            "session_id",
            "sample_position",
        }:
            return True
        event_type = event.get("type")
        sample_position = event.get("sample_position")
        if (
            event.get("session_id") != self.session_id
            or event_type not in {"vad.start", "vad.end"}
            or isinstance(sample_position, bool)
            or not isinstance(sample_position, int)
            or not 0 <= sample_position <= 0xFFFFFFFFFFFFFFFF
            or sample_position < self._last_sample
        ):
            return True
        if event_type == "vad.start":
            if self._active:
                return True
            self._participant_sid = participant_sid
            self._participant_identity = participant_identity
            self._active = True
            state = "speaking"
        else:
            if not self._active:
                return True
            if participant_sid != self._participant_sid:
                self._participant_sid = participant_sid
            self._active = False
            state = "listening"
        self._last_sample = sample_position
        update_state = getattr(self.session, "_update_user_state", None)
        if callable(update_state):
            update_state(state)
        else:
            self.session.emit("user_state_changed", SimpleNamespace(new_state=state))
        return True


def _owns_device_microphone(participant: Any) -> bool:
    if participant is None or not str(getattr(participant, "identity", "")):
        return False
    publications = getattr(participant, "track_publications", None)
    if not isinstance(publications, Mapping):
        return False
    return any(
        str(getattr(publication, "name", "")) == DEVICE_MICROPHONE_TRACK_NAME
        and getattr(publication, "source", None) == rtc.TrackSource.SOURCE_MICROPHONE
        for publication in publications.values()
    )


__all__ = ["DEVICE_MICROPHONE_TRACK_NAME", "DEVICE_VAD_TOPIC", "DeviceVadProjector"]
