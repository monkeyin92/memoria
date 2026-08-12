from __future__ import annotations

import json
from types import SimpleNamespace

from livekit import rtc
from services.agent.src.device_vad import DEVICE_VAD_TOPIC, DeviceVadProjector


class Session:
    def __init__(self) -> None:
        self.user_state = "listening"
        self.transitions: list[str] = []

    def _update_user_state(self, state: str) -> None:
        if state == self.user_state:
            return
        self.user_state = state
        self.transitions.append(state)


def _packet(
    event: object,
    *,
    topic: str = DEVICE_VAD_TOPIC,
    kind: int = rtc.DataPacketKind.KIND_RELIABLE,
    sid: str = "PA_device",
    track_name: str = "device-microphone",
    source: int = rtc.TrackSource.SOURCE_MICROPHONE,
    identity: str = "user-account-session",
) -> SimpleNamespace:
    publication = SimpleNamespace(name=track_name, source=source)
    participant = SimpleNamespace(
        sid=sid,
        identity=identity,
        track_publications={"TR_device": publication},
    )
    return SimpleNamespace(
        topic=topic,
        kind=kind,
        participant=participant,
        data=json.dumps(event, separators=(",", ":")).encode(),
    )


def test_device_vad_drives_one_session_state_transition_per_fenced_boundary() -> None:
    session = Session()
    projector = DeviceVadProjector(session, "session-1")

    assert projector.accept(
        _packet({"type": "vad.start", "session_id": "session-1", "sample_position": 320})
    )
    assert projector.accept(
        _packet({"type": "vad.start", "session_id": "session-1", "sample_position": 320})
    )
    assert projector.accept(
        _packet({"type": "vad.end", "session_id": "session-1", "sample_position": 640})
    )

    assert session.transitions == ["speaking", "listening"]


def test_device_vad_rejects_unreliable_spoofed_stale_and_cross_session_facts() -> None:
    session = Session()
    projector = DeviceVadProjector(session, "session-1")
    event = {"type": "vad.start", "session_id": "session-1", "sample_position": 320}

    assert projector.accept(_packet(event, kind=rtc.DataPacketKind.KIND_LOSSY))
    assert projector.accept(_packet(event, track_name="miniprogram-microphone"))
    assert projector.accept(_packet(event, source=rtc.TrackSource.SOURCE_UNKNOWN))
    assert projector.accept(_packet({**event, "session_id": "session-2"}))
    assert projector.accept(_packet({**event, "extra": "private"}))
    assert session.transitions == []

    assert projector.accept(_packet(event))
    assert projector.accept(
        _packet(
            {"type": "vad.end", "session_id": "session-1", "sample_position": 640},
            sid="PA_attacker",
            identity="user-attacker-session",
        )
    )
    assert projector.accept(
        _packet({"type": "vad.end", "session_id": "session-1", "sample_position": 319})
    )
    assert session.transitions == ["speaking"]
    assert projector.accept(_packet(event, topic="voice-agent.telemetry")) is False


def test_device_vad_allows_same_identity_to_reconnect_with_a_new_participant_sid() -> None:
    session = Session()
    projector = DeviceVadProjector(session, "session-1")

    assert projector.accept(
        _packet({"type": "vad.start", "session_id": "session-1", "sample_position": 320})
    )
    assert projector.accept(
        _packet(
            {"type": "vad.end", "session_id": "session-1", "sample_position": 640},
            sid="PA_device_reconnected",
        )
    )

    assert session.transitions == ["speaking", "listening"]


def test_device_vad_deduplicates_a_later_livekit_vad_transition() -> None:
    session = Session()
    projector = DeviceVadProjector(session, "session-1")
    event = {"type": "vad.start", "session_id": "session-1", "sample_position": 320}

    assert projector.accept(_packet(event))
    session._update_user_state("speaking")

    assert session.transitions == ["speaking"]
