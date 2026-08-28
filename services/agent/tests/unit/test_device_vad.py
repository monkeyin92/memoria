from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from livekit import rtc
from services.agent.src.device_vad import (
    DEVICE_TURN_TRANSCRIPT_TIMEOUT_S,
    DEVICE_VAD_TOPIC,
    DeviceVadProjector,
    commit_device_user_turn,
    commit_device_user_turn_after_asr,
    device_turn_commit_busy,
)


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


def test_device_vad_end_invokes_endpoint_callback_after_listening() -> None:
    session = Session()
    endpoints: list[str] = []
    projector = DeviceVadProjector(
        session,
        "session-1",
        on_endpoint=lambda: endpoints.append(session.user_state),
    )

    assert projector.accept(
        _packet({"type": "vad.start", "session_id": "session-1", "sample_position": 320})
    )
    assert projector.accept(
        _packet({"type": "vad.end", "session_id": "session-1", "sample_position": 640})
    )

    assert endpoints == ["listening"]


def test_commit_device_user_turn_skips_assistant_output() -> None:
    class SessionWithCommit:
        agent_state = "speaking"
        commits: list[dict[str, float]] = []

        def commit_user_turn(self, **kwargs: float) -> None:
            self.commits.append(kwargs)

    session = SessionWithCommit()

    assert commit_device_user_turn(session, session_id="session-1") is False
    assert session.commits == []


def test_commit_device_user_turn_commits_while_listening() -> None:
    class SessionWithCommit:
        agent_state = "listening"
        commits: list[dict[str, float]] = []

        def commit_user_turn(self, **kwargs: float) -> None:
            self.commits.append(kwargs)

    session = SessionWithCommit()

    assert commit_device_user_turn(session, session_id="session-1") is True
    assert session.commits == [
        {
            "transcript_timeout": DEVICE_TURN_TRANSCRIPT_TIMEOUT_S,
            "stt_flush_duration": 0.0,
        }
    ]
    assert DEVICE_TURN_TRANSCRIPT_TIMEOUT_S == 8.0


@pytest.mark.asyncio
async def test_commit_after_asr_waits_for_nonempty_final() -> None:
    class SessionWithCommit:
        agent_state = "listening"
        commits: list[dict[str, float]] = []

        def commit_user_turn(self, **kwargs: float) -> None:
            self.commits.append(kwargs)

    class ReadySTT:
        async def wait_for_nonempty_final(
            self,
            *,
            since: float,
            timeout: float,
            empty_grace_s: float = 2.0,
        ) -> bool:
            assert since == 10.0
            assert timeout == DEVICE_TURN_TRANSCRIPT_TIMEOUT_S
            assert empty_grace_s == 2.0
            return True

    session = SessionWithCommit()

    assert (
        await commit_device_user_turn_after_asr(
            session,
            session_id="session-1",
            stt=ReadySTT(),
            since=10.0,
        )
        is True
    )
    assert session.commits == [
        {
            "transcript_timeout": DEVICE_TURN_TRANSCRIPT_TIMEOUT_S,
            "stt_flush_duration": 0.0,
        }
    ]


@pytest.mark.asyncio
async def test_commit_after_asr_skips_empty_transcript() -> None:
    class SessionWithCommit:
        agent_state = "listening"
        commits: list[dict[str, float]] = []

        def commit_user_turn(self, **kwargs: float) -> None:
            self.commits.append(kwargs)

    class EmptySTT:
        async def wait_for_nonempty_final(
            self,
            *,
            since: float,
            timeout: float,
            empty_grace_s: float = 2.0,
        ) -> bool:
            return False

    session = SessionWithCommit()

    assert (
        await commit_device_user_turn_after_asr(
            session,
            session_id="session-1",
            stt=EmptySTT(),
            since=10.0,
        )
        is False
    )
    assert session.commits == []


def test_device_turn_commit_busy_while_a_wait_is_running() -> None:
    class Running:
        def done(self) -> bool:
            return False

    class Finished:
        def done(self) -> bool:
            return True

    assert device_turn_commit_busy(None) is False
    assert device_turn_commit_busy(Finished()) is False
    assert device_turn_commit_busy(Running()) is True
