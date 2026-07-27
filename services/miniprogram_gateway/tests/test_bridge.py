from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from livekit import rtc
from services.common.miniprogram_gateway_ticket import (
    MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    MINIPROGRAM_AGENT_DISPATCH_METADATA,
    GatewayTicketClaims,
)
from services.miniprogram_gateway import audio_processing as audio_processing_module
from services.miniprogram_gateway import bridge as bridge_module
from services.miniprogram_gateway.bridge import (
    GatewayMediaError,
    GatewayOutboundMessage,
    MiniProgramLiveKitBridge,
    PcmFrameAccumulator,
)
from services.miniprogram_gateway.config import MiniProgramGatewaySettings
from services.miniprogram_gateway.protocol import FrameType, PcmFrame, decode_pcm_frame

CONTRACT = json.loads(
    (Path(__file__).parents[3] / "packages" / "contracts" / "miniprogram-media.json").read_text(
        encoding="utf-8"
    )
)


def test_gateway_ready_event_matches_shared_contract() -> None:
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge.set_downlink_generation_protocol(True)

    ready = bridge.ready_event

    assert set(ready) == set(CONTRACT["ready"]["required_fields"])
    assert ready["type"] == CONTRACT["ready"]["type"]
    assert ready["protocol_version"] == CONTRACT["ready"]["protocol_version"]
    audio = ready["audio"]
    assert isinstance(audio, dict)
    assert set(audio) == set(CONTRACT["ready"]["audio_required_fields"])
    assert audio == {
        "sample_rate": CONTRACT["audio"]["downlink_sample_rate"],
        "channels": CONTRACT["audio"]["channels"],
        "sample_format": CONTRACT["audio"]["sample_format"],
        "frame_ms": CONTRACT["audio"]["frame_ms"],
        "frame_protocol_version": CONTRACT["audio"]["downlink_frame_protocol_versions"][1],
    }


def test_pcm_accumulator_reframes_recorder_chunks_without_losing_tail() -> None:
    accumulator = PcmFrameAccumulator(sample_rate=16_000, frame_ms=20)
    first = accumulator.feed(b"\x01\x00" * 100)
    second = accumulator.feed(b"\x02\x00" * 220)

    assert first == ()
    assert len(second) == 1
    assert len(second[0]) == 640
    assert second[0][:200] == b"\x01\x00" * 100
    assert second[0][200:] == b"\x02\x00" * 220


def test_pcm_accumulator_rejects_partial_sample_and_unbounded_buffer() -> None:
    accumulator = PcmFrameAccumulator(sample_rate=16_000, frame_ms=20, max_buffered_frames=1)
    with pytest.raises(GatewayMediaError, match="whole samples"):
        accumulator.feed(b"\x00")
    with pytest.raises(GatewayMediaError, match="buffer exceeded"):
        accumulator.feed(b"\x00\x00" * 321)


def test_pcm_accumulator_reset_discards_a_partial_recorder_chunk() -> None:
    accumulator = PcmFrameAccumulator(sample_rate=16_000, frame_ms=20)

    assert accumulator.feed(b"\x01\x00" * 100) == ()
    accumulator.reset()

    assert accumulator.feed(b"\x02\x00" * 320) == (b"\x02\x00" * 320,)


def test_default_downlink_queue_is_bounded_to_400_ms() -> None:
    settings = MiniProgramGatewaySettings()

    assert (
        settings.miniprogram_gateway_audio_queue_frames * settings.miniprogram_gateway_frame_ms
        == 400
    )


@pytest.mark.parametrize(
    ("aec_enabled", "apm_state", "expected_metadata"),
    [
        (True, "ready", MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA),
        (False, "disabled", MINIPROGRAM_AGENT_DISPATCH_METADATA),
        (True, "init_failed", MINIPROGRAM_AGENT_DISPATCH_METADATA),
        (True, "process_failed", MINIPROGRAM_AGENT_DISPATCH_METADATA),
    ],
)
def test_gateway_dispatch_always_identifies_miniprogram_and_marks_ready_aec(
    monkeypatch: pytest.MonkeyPatch,
    aec_enabled: bool,
    apm_state: str,
    expected_metadata: str | None,
) -> None:
    class FakeApm:
        def __init__(self, **_kwargs: object) -> None:
            if apm_state == "init_failed":
                raise RuntimeError("APM unavailable")

        def set_stream_delay_ms(self, _delay_ms: int) -> None:
            pass

        def process_reverse_stream(self, _frame: rtc.AudioFrame) -> None:
            if apm_state == "process_failed":
                raise RuntimeError("APM reverse processing unavailable")

        def process_stream(self, _frame: rtc.AudioFrame) -> None:
            if apm_state == "process_failed":
                raise RuntimeError("APM capture processing unavailable")

    monkeypatch.setattr(audio_processing_module.rtc, "AudioProcessingModule", FakeApm)
    secret = "livekit-secret-that-is-long-enough"
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(
            livekit_api_key="livekit-key",
            livekit_api_secret=secret,
            miniprogram_gateway_aec_enabled=aec_enabled,
        ),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )

    claims = jwt.decode(
        bridge._mint_livekit_token(),
        secret,
        algorithms=["HS256"],
        options={"verify_aud": False},
    )

    agent_dispatch = claims["roomConfig"]["agents"][0]
    assert agent_dispatch["agentName"] == "duplex-zh-agent"
    assert agent_dispatch.get("metadata") == expected_metadata


@pytest.mark.asyncio
async def test_runtime_aec_failure_waits_for_agent_ack_before_raw_uplink() -> None:
    published: list[tuple[dict[str, object], str]] = []
    captured: list[bytes] = []

    class Participant:
        async def publish_data(self, payload: str, *, reliable: bool, topic: str) -> None:
            assert reliable is True
            published.append((json.loads(payload), topic))

    class Source:
        async def capture_frame(self, frame: rtc.AudioFrame) -> None:
            captured.append(bytes(frame.data))

    class Processor:
        aec_ready = True

        def process_uplink(self, payload: bytes) -> bytes:
            self.aec_ready = False
            return payload

    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(miniprogram_gateway_aec_enabled=True),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._audio_processor = Processor()  # type: ignore[assignment]
    bridge._aec_dispatched_ready = True
    bridge._room = SimpleNamespace(local_participant=Participant())
    bridge._audio_source = Source()

    first = asyncio.create_task(
        bridge.accept_uplink(
            PcmFrame(
                frame_type=FrameType.UPLINK_AUDIO,
                sequence=0,
                timestamp_ms=0,
                payload=b"\x01\x00" * 320,
            )
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert captured == []
    assert len(published) == 1
    failure, topic = published[0]
    assert topic == "voice-agent.gateway-health"
    assert failure["type"] == "miniprogram_aec_failed"
    assert failure["session_id"] == "session-1"
    failure_id = failure["failure_id"]
    assert isinstance(failure_id, str) and failure_id

    bridge._on_data_received(
        SimpleNamespace(
            participant=SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT),
            topic="voice-agent.gateway-health.ack",
            data=json.dumps(
                {
                    "type": "miniprogram_aec_failed_ack",
                    "session_id": "session-1",
                    "failure_id": failure_id,
                }
            ).encode(),
        )
    )
    await first
    assert captured == []

    await bridge.accept_uplink(
        PcmFrame(
            frame_type=FrameType.UPLINK_AUDIO,
            sequence=1,
            timestamp_ms=20,
            payload=b"\x02\x00" * 320,
        )
    )
    assert captured == [b"\x02\x00" * 320]
    assert len(published) == 1


@pytest.mark.parametrize("publish_hangs", [False, True])
@pytest.mark.asyncio
async def test_runtime_aec_failure_timeout_never_forwards_raw_uplink(
    publish_hangs: bool,
) -> None:
    captured: list[bytes] = []

    class Participant:
        async def publish_data(self, _payload: str, *, reliable: bool, topic: str) -> None:
            assert reliable is True
            assert topic == "voice-agent.gateway-health"
            if publish_hangs:
                await asyncio.Event().wait()

    class Source:
        async def capture_frame(self, frame: rtc.AudioFrame) -> None:
            captured.append(bytes(frame.data))

    class Processor:
        aec_ready = True

        def process_uplink(self, payload: bytes) -> bytes:
            self.aec_ready = False
            return payload

    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(miniprogram_gateway_aec_enabled=True),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._settings.miniprogram_gateway_handshake_timeout_s = 0.01
    bridge._audio_processor = Processor()  # type: ignore[assignment]
    bridge._aec_dispatched_ready = True
    bridge._room = SimpleNamespace(local_participant=Participant())
    bridge._audio_source = Source()

    with pytest.raises(GatewayMediaError, match="revocation timed out"):
        await bridge.accept_uplink(
            PcmFrame(
                frame_type=FrameType.UPLINK_AUDIO,
                sequence=0,
                timestamp_ms=0,
                payload=b"\x01\x00" * 320,
            )
        )

    assert captured == []


@pytest.mark.asyncio
async def test_downlink_aec_failure_revokes_trust_before_the_next_uplink() -> None:
    published: list[dict[str, object]] = []
    captured: list[bytes] = []

    class Participant:
        async def publish_data(self, payload: str, *, reliable: bool, topic: str) -> None:
            assert reliable is True
            assert topic == "voice-agent.gateway-health"
            published.append(json.loads(payload))

    class Source:
        async def capture_frame(self, frame: rtc.AudioFrame) -> None:
            captured.append(bytes(frame.data))

    class Processor:
        aec_ready = True

        def observe_downlink(self, _payload: bytes) -> None:
            self.aec_ready = False

        def process_uplink(self, payload: bytes) -> bytes:
            return payload

        def reset(self) -> None:
            self.aec_ready = True

    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(miniprogram_gateway_aec_enabled=True),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._audio_processor = Processor()  # type: ignore[assignment]
    bridge._aec_dispatched_ready = True
    bridge._room = SimpleNamespace(local_participant=Participant())
    bridge._audio_source = Source()
    bridge.outbound_sent(GatewayOutboundMessage(binary=b"audio", audio_reference=b"\x00" * 960))

    uplink = asyncio.create_task(
        bridge.accept_uplink(
            PcmFrame(
                frame_type=FrameType.UPLINK_AUDIO,
                sequence=0,
                timestamp_ms=0,
                payload=b"\x01\x00" * 320,
            )
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert captured == []
    assert len(published) == 1

    failure = published[0]
    bridge._on_data_received(
        SimpleNamespace(
            participant=SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT),
            topic="voice-agent.gateway-health.ack",
            data=json.dumps(
                {
                    "type": "miniprogram_aec_failed_ack",
                    "session_id": "session-1",
                    "failure_id": failure["failure_id"],
                }
            ).encode(),
        )
    )
    await uplink
    assert captured == [b"\x01\x00" * 320]

    bridge._audio_processor.reset()
    await bridge.accept_uplink(
        PcmFrame(
            frame_type=FrameType.UPLINK_AUDIO,
            sequence=1,
            timestamp_ms=20,
            payload=b"\x02\x00" * 320,
        )
    )
    assert len(published) == 1


def test_production_rejects_downlink_queue_over_400_ms() -> None:
    settings = MiniProgramGatewaySettings(
        environment="production",
        livekit_url="wss://livekit.example.com",
        livekit_api_key="livekit-key",
        livekit_api_secret="livekit-secret-that-is-long-enough",
        memoria_miniprogram_gateway_ticket_secret=("gateway-ticket-secret-that-is-long-enough"),
        miniprogram_gateway_audio_queue_frames=21,
    )

    with pytest.raises(ValueError, match="must not exceed 400 ms"):
        settings.validate_production()


def test_downlink_queue_drop_is_counted_and_keeps_latest_audio() -> None:
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(miniprogram_gateway_audio_queue_frames=10),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )

    for sequence in range(11):
        bridge._enqueue_audio(GatewayOutboundMessage(binary=bytes([sequence])))

    assert bridge._downlink_drop_count == 1
    assert bridge._audio_messages.qsize() == 10
    assert bridge._audio_messages.get_nowait().binary == b"\x01"


@pytest.mark.asyncio
async def test_audio_reset_barrier_survives_a_full_best_effort_event_queue() -> None:
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(miniprogram_gateway_event_queue_size=8),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    agent = SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)

    for index in range(8):
        bridge._enqueue_event(
            GatewayOutboundMessage(event={"type": "transcription", "index": index})
        )
    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 1,
                    "generation_id": 1,
                    "state": "speaking",
                }
            ).encode(),
        )
    )

    assert (await bridge.next_outbound()).event == {
        "type": "audio_reset",
        "generation_id": 1,
        "barrier_sequence": 0,
    }


@pytest.mark.asyncio
async def test_next_outbound_does_not_starve_audio_behind_best_effort_events() -> None:
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._enqueue_audio(GatewayOutboundMessage(binary=b"audio"))
    for index in range(3):
        bridge._enqueue_event(
            GatewayOutboundMessage(event={"type": "transcription", "index": index})
        )

    assert (await bridge.next_outbound()).binary == b"audio"


@pytest.mark.asyncio
async def test_matching_playout_interrupt_suppresses_its_generation_reference_until_reset() -> None:
    observed: list[bytes] = []
    resets = 0

    class Processor:
        aec_ready = True

        def observe_downlink(self, payload: bytes) -> None:
            observed.append(payload)

        def reset(self) -> None:
            nonlocal resets
            resets += 1

    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._audio_processor = Processor()  # type: ignore[assignment]
    bridge._generation_id = 7

    bridge.accept_transport_event(
        {"type": "playout_interrupt", "generation_id": 7, "client_timestamp_ms": 1}
    )
    bridge.outbound_sent(
        GatewayOutboundMessage(
            binary=b"audio",
            audio_reference=b"reference",
            generation_id=7,
        )
    )

    assert observed == []
    assert resets == 1
    bridge._on_data_received(
        SimpleNamespace(
            participant=SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT),
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 2,
                    "generation_id": 8,
                    "state": "thinking",
                }
            ).encode(),
        )
    )
    bridge.outbound_sent(
        GatewayOutboundMessage(
            binary=b"in-flight-old-audio",
            audio_reference=b"old-reference",
            generation_id=7,
        )
    )
    assert observed == []

    reset = await bridge.next_outbound()
    assert reset.event == {
        "type": "audio_reset",
        "generation_id": 8,
        "barrier_sequence": 0,
    }
    bridge.outbound_sent(reset)
    bridge.outbound_sent(
        GatewayOutboundMessage(
            binary=b"new-audio",
            audio_reference=b"new-reference",
            generation_id=8,
        )
    )
    assert observed == [b"new-reference"]
    assert resets == 2


@pytest.mark.parametrize("generation_id", [6, 8])
def test_stale_or_unknown_playout_interrupt_does_not_suppress_current_reference(
    generation_id: int,
) -> None:
    observed: list[bytes] = []

    class Processor:
        aec_ready = True

        def observe_downlink(self, payload: bytes) -> None:
            observed.append(payload)

        def reset(self) -> None:
            pass

    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._audio_processor = Processor()  # type: ignore[assignment]
    bridge._generation_id = 7

    bridge.accept_transport_event(
        {"type": "playout_interrupt", "generation_id": generation_id, "client_timestamp_ms": 1}
    )
    bridge.outbound_sent(
        GatewayOutboundMessage(
            binary=b"audio",
            audio_reference=b"reference",
            generation_id=7,
        )
    )

    assert observed == [b"reference"]


@pytest.mark.asyncio
async def test_client_audio_trace_is_forwarded_to_the_agent_without_text() -> None:
    published: list[tuple[dict[str, object], bool, str]] = []

    class Participant:
        async def publish_data(self, payload: str, *, reliable: bool, topic: str) -> None:
            published.append((json.loads(payload), reliable, topic))

    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._room = SimpleNamespace(local_participant=Participant())
    bridge._turn_id = 4

    bridge.accept_transport_event(
        {
            "type": "client_audio_trace",
            "name": "miniprogram_playback_underrun",
            "generation_id": 8,
            "client_timestamp_ms": 126,
            "detail": {
                "queue_lead_ms": 0,
                "pending_audio_ms": 80,
                "scheduled_sources": 1,
            },
        }
    )
    bridge.accept_transport_event(
        {
            "type": "client_audio_trace",
            "name": "miniprogram_playback_underrun",
            "generation_id": 8,
            "client_timestamp_ms": 127,
            "detail": {
                "queue_lead_ms": 0,
                "pending_audio_ms": 80,
                "scheduled_sources": 1,
            },
        }
    )
    await asyncio.sleep(0.01)

    assert published == [
        (
            {
                "type": "audio_trace",
                "source": "miniprogram",
                "session_id": "session-1",
                "name": "miniprogram_playback_underrun",
                "status": "ok",
                "turn_id": 4,
                "generation_id": 8,
                "client_timestamp_ms": 126,
                "detail": {
                    "queue_lead_ms": 0,
                    "pending_audio_ms": 80,
                    "scheduled_sources": 1,
                },
            },
            False,
            "voice-agent.telemetry",
        )
    ]
    await bridge.close()


@pytest.mark.asyncio
async def test_uplink_discontinuity_resets_partial_pcm_and_accepts_declared_sequence() -> None:
    captured: list[bytes] = []
    resets = 0

    class Source:
        async def capture_frame(self, frame: rtc.AudioFrame) -> None:
            captured.append(bytes(frame.data))

    class Processor:
        aec_ready = False

        def process_uplink(self, payload: bytes) -> bytes:
            return payload

        def reset(self) -> None:
            nonlocal resets
            resets += 1

    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._audio_source = Source()
    bridge._audio_processor = Processor()  # type: ignore[assignment]

    await bridge.accept_uplink(
        PcmFrame(
            frame_type=FrameType.UPLINK_AUDIO,
            sequence=0,
            timestamp_ms=0,
            payload=b"\x01\x00" * 100,
        )
    )
    bridge.accept_transport_event(
        {
            "type": "uplink_discontinuity",
            "next_sequence": 1,
            "client_timestamp_ms": 20,
        }
    )
    await bridge.accept_uplink(
        PcmFrame(
            frame_type=FrameType.UPLINK_AUDIO,
            sequence=1,
            timestamp_ms=20,
            payload=b"\x02\x00" * 320,
        )
    )

    assert captured == [b"\x02\x00" * 320]
    assert resets == 1


@pytest.mark.asyncio
async def test_bridge_feeds_downlink_reference_before_processing_uplink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int, int]] = []

    class FakeApm:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def set_stream_delay_ms(self, delay_ms: int) -> None:
            assert delay_ms == 120

        def process_reverse_stream(self, frame: rtc.AudioFrame) -> None:
            calls.append(("reverse", frame.sample_rate, frame.samples_per_channel))

        def process_stream(self, frame: rtc.AudioFrame) -> None:
            calls.append(("capture", frame.sample_rate, frame.samples_per_channel))
            frame._data = bytearray(frame.samples_per_channel * frame.num_channels * 2)

    downlink = b"\x01\x00" * 480

    class FakeAudioStream:
        @classmethod
        def from_track(cls, **_kwargs: object) -> FakeAudioStream:
            return cls()

        def __aiter__(self) -> FakeAudioStream:
            return self

        async def __anext__(self) -> SimpleNamespace:
            if hasattr(self, "sent"):
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(
                frame=rtc.AudioFrame(
                    data=downlink,
                    sample_rate=24_000,
                    num_channels=1,
                    samples_per_channel=480,
                )
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(audio_processing_module.rtc, "AudioProcessingModule", FakeApm)
    monkeypatch.setattr(bridge_module.rtc, "AudioStream", FakeAudioStream)
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(miniprogram_gateway_aec_enabled=True),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge.set_downlink_generation_protocol(True)
    bridge._generation_id = 0
    bridge._audio_generation_id = 0
    probe_calls = [
        ("reverse", 24_000, 240),
        ("capture", 16_000, 160),
    ]
    assert calls == probe_calls

    await bridge._pump_downlink_track(object())
    outbound = await bridge.next_outbound()
    assert outbound.binary is not None
    decoded = decode_pcm_frame(outbound.binary)
    assert decoded.payload == downlink
    assert decoded.generation_id == 0
    assert outbound.generation_id == 0
    assert calls == probe_calls
    bridge.outbound_sent(outbound)

    captured: list[bytes] = []

    class FakeAudioSource:
        async def capture_frame(self, frame: rtc.AudioFrame) -> None:
            captured.append(bytes(frame.data))

    bridge._audio_source = FakeAudioSource()
    await bridge.accept_uplink(
        PcmFrame(
            frame_type=FrameType.UPLINK_AUDIO,
            sequence=0,
            timestamp_ms=0,
            payload=b"\x02\x00" * 320,
        )
    )

    assert calls == [
        ("reverse", 24_000, 240),
        ("capture", 16_000, 160),
        ("reverse", 24_000, 240),
        ("reverse", 24_000, 240),
        ("capture", 16_000, 160),
        ("capture", 16_000, 160),
    ]
    assert captured == [bytes(640)]


@pytest.mark.asyncio
async def test_bridge_captures_aligned_aec_input_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[tuple[bytes, bytes]] = []
    closed: list[bool] = []

    class FakeCapture:
        def write(self, pre_aec: bytes, post_aec: bytes) -> None:
            writes.append((pre_aec, post_aec))

        def close(self) -> None:
            closed.append(True)

    fake_capture = FakeCapture()
    monkeypatch.setattr(
        bridge_module.AecPcmCapture,
        "try_create",
        lambda **_kwargs: fake_capture,
    )
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(
            miniprogram_gateway_aec_capture_session_id="session-1",
        ),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )

    class FakeProcessor:
        aec_ready = False

        def process_uplink(self, payload: bytes) -> bytes:
            return b"\x03\x00" * (len(payload) // 2)

    captured: list[bytes] = []

    class FakeAudioSource:
        async def capture_frame(self, frame: rtc.AudioFrame) -> None:
            captured.append(bytes(frame.data))

    bridge._audio_processor = FakeProcessor()  # type: ignore[assignment]
    bridge._audio_source = FakeAudioSource()
    await bridge.accept_uplink(
        PcmFrame(
            frame_type=FrameType.UPLINK_AUDIO,
            sequence=0,
            timestamp_ms=0,
            payload=b"\x02\x00" * 320,
        )
    )
    await bridge.close()

    assert writes == [(b"\x02\x00" * 320, b"\x03\x00" * 320)]
    assert captured == [b"\x03\x00" * 320]
    assert closed == [True]


@pytest.mark.asyncio
async def test_bridge_drops_downlink_frames_that_break_the_pcm_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAudioStream:
        @classmethod
        def from_track(cls, **_kwargs: object) -> FakeAudioStream:
            return cls()

        def __aiter__(self) -> FakeAudioStream:
            return self

        async def __anext__(self) -> SimpleNamespace:
            if hasattr(self, "sent"):
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(frame=SimpleNamespace(data=bytes(958)))

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bridge_module.rtc, "AudioStream", FakeAudioStream)
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge._generation_id = 0
    bridge._audio_generation_id = 0

    await bridge._pump_downlink_track(object())

    assert bridge._downlink_sequence == 0
    assert bridge._audio_messages.empty()


@pytest.mark.asyncio
async def test_generation_quarantine_drops_late_audio_before_new_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downlink = b"\x01\x00" * 480

    class FakeAudioStream:
        @classmethod
        def from_track(cls, **_kwargs: object) -> FakeAudioStream:
            return cls()

        def __aiter__(self) -> FakeAudioStream:
            return self

        async def __anext__(self) -> SimpleNamespace:
            if hasattr(self, "sent"):
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(
                frame=rtc.AudioFrame(
                    data=downlink,
                    sample_rate=24_000,
                    num_channels=1,
                    samples_per_channel=480,
                )
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bridge_module.rtc, "AudioStream", FakeAudioStream)
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    agent = SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)
    bridge._generation_id = 1
    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 2,
                    "generation_id": 2,
                    "state": "thinking",
                }
            ).encode(),
        )
    )

    reset = await bridge.next_outbound()
    assert reset.event == {
        "type": "audio_reset",
        "generation_id": 2,
        "barrier_sequence": 0,
    }
    await bridge._pump_downlink_track(object())

    assert bridge._downlink_sequence == 1
    assert bridge._audio_messages.empty()


@pytest.mark.asyncio
async def test_new_speaking_barrier_releases_quarantine_without_clipping_first_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downlink = b"\x01\x00" * 480

    class FakeAudioStream:
        @classmethod
        def from_track(cls, **_kwargs: object) -> FakeAudioStream:
            return cls()

        def __aiter__(self) -> FakeAudioStream:
            self.index = 0
            return self

        async def __anext__(self) -> SimpleNamespace:
            if self.index == 1:
                raise StopAsyncIteration
            self.index += 1
            return SimpleNamespace(
                frame=rtc.AudioFrame(
                    data=downlink,
                    sample_rate=24_000,
                    num_channels=1,
                    samples_per_channel=480,
                )
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bridge_module.rtc, "AudioStream", FakeAudioStream)
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge.set_downlink_generation_protocol(True)
    bridge._generation_id = 1
    agent = SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)
    for state in ("thinking", "speaking"):
        bridge._on_data_received(
            SimpleNamespace(
                participant=agent,
                topic="voice-agent.ui",
                data=json.dumps(
                    {
                        "type": "assistant_state",
                        "turn_id": 2,
                        "generation_id": 2,
                        "state": state,
                    }
                ).encode(),
            )
        )

    assert (await bridge.next_outbound()).event == {
        "type": "audio_reset",
        "generation_id": 2,
        "barrier_sequence": 0,
    }
    assert (await bridge.next_outbound()).event is not None
    assert (await bridge.next_outbound()).event is not None
    await bridge._pump_downlink_track(object())

    outbound = await bridge.next_outbound()
    assert outbound.binary is not None
    frame = decode_pcm_frame(outbound.binary)
    assert frame.sequence == 0
    assert frame.generation_id == 2
    assert bridge._audio_messages.empty()


@pytest.mark.asyncio
async def test_control_ack_track_uses_current_generation_during_interrupted_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downlink = b"\x01\x00" * 480

    class FakeAudioStream:
        @classmethod
        def from_track(cls, **_kwargs: object) -> FakeAudioStream:
            return cls()

        def __aiter__(self) -> FakeAudioStream:
            return self

        async def __anext__(self) -> SimpleNamespace:
            if hasattr(self, "sent"):
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(
                frame=rtc.AudioFrame(
                    data=downlink,
                    sample_rate=24_000,
                    num_channels=1,
                    samples_per_channel=480,
                )
            )

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bridge_module.rtc, "AudioStream", FakeAudioStream)
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    bridge.set_downlink_generation_protocol(True)
    bridge._generation_id = 2
    bridge._audio_generation_id = None
    bridge._downlink_quarantine_until = asyncio.get_running_loop().time() + 1

    await bridge._pump_downlink_track(object(), control_track=True)

    outbound = await bridge.next_outbound()
    assert outbound.binary is not None
    frame = decode_pcm_frame(outbound.binary)
    assert frame.sequence == 0
    assert frame.generation_id == 2
    assert frame.payload == downlink


@pytest.mark.asyncio
async def test_bridge_publishes_uplink_as_microphone(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeAudioSource:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class FakeLocalAudioTrack:
        @staticmethod
        def create_audio_track(_name: str, _source: object) -> object:
            return object()

    class FakeTrackPublishOptions:
        def __init__(self, *, source: object) -> None:
            self.source = source

    class FakeLocalParticipant:
        def __init__(self) -> None:
            self.options: FakeTrackPublishOptions | None = None

        async def publish_track(
            self,
            _track: object,
            options: FakeTrackPublishOptions | None = None,
        ) -> SimpleNamespace:
            self.options = options
            return SimpleNamespace(sid="publication-1")

        async def unpublish_track(self, _sid: str) -> None:
            pass

    class FakeRoom:
        last_instance: FakeRoom | None = None

        def __init__(self) -> None:
            self.local_participant = FakeLocalParticipant()
            self.remote_participants: dict[str, object] = {}
            FakeRoom.last_instance = self

        def on(self, _event: str, _callback: object) -> None:
            pass

        async def connect(self, _url: str, _token: str) -> None:
            pass

        async def disconnect(self) -> None:
            pass

    fake_rtc = SimpleNamespace(
        Room=FakeRoom,
        AudioSource=FakeAudioSource,
        LocalAudioTrack=FakeLocalAudioTrack,
        TrackPublishOptions=FakeTrackPublishOptions,
        TrackSource=SimpleNamespace(SOURCE_MICROPHONE="microphone"),
    )
    monkeypatch.setattr(bridge_module, "rtc", fake_rtc)
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(
            livekit_url="wss://livekit.example.com",
            livekit_api_key="livekit-key",
            livekit_api_secret="livekit-secret-that-is-long-enough",
            memoria_miniprogram_gateway_ticket_secret="gateway-ticket-secret-that-is-long-enough",
        ),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )

    await bridge.connect()

    assert FakeRoom.last_instance is not None
    assert FakeRoom.last_instance.local_participant.options is not None
    assert (
        FakeRoom.last_instance.local_participant.options.source
        == fake_rtc.TrackSource.SOURCE_MICROPHONE
    )


@pytest.mark.asyncio
async def test_new_generation_discards_buffered_audio_before_forwarding_ui_event() -> None:
    bridge = MiniProgramLiveKitBridge(
        settings=MiniProgramGatewaySettings(),
        claims=GatewayTicketClaims(
            session_id="session-1",
            user_id="account-1",
            room_name="voice-session-1",
            identity="user-account-1-session",
            agent_name="duplex-zh-agent",
            voice_backend="cascade",
            issued_at_s=1,
            expires_at_s=91,
            ticket_id="ticket-1",
        ),
    )
    agent = SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)

    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 1,
                    "generation_id": 1,
                    "state": "speaking",
                }
            ).encode(),
        )
    )
    assert (await bridge.next_outbound()).event == {
        "type": "audio_reset",
        "generation_id": 1,
        "barrier_sequence": 0,
    }
    assert (await bridge.next_outbound()).event == {
        "type": "ui_event",
        "topic": "voice-agent.ui",
        "event": {
            "type": "assistant_state",
            "turn_id": 1,
            "generation_id": 1,
            "state": "speaking",
        },
    }
    assert bridge._audio_generation_id == 1

    bridge._enqueue_audio(GatewayOutboundMessage(binary=b"stale-audio"))
    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 2,
                    "generation_id": 2,
                    "state": "thinking",
                }
            ).encode(),
        )
    )

    assert (await bridge.next_outbound()).event == {
        "type": "audio_reset",
        "generation_id": 2,
        "barrier_sequence": 0,
    }
    assert (await bridge.next_outbound()).event == {
        "type": "ui_event",
        "topic": "voice-agent.ui",
        "event": {
            "type": "assistant_state",
            "turn_id": 2,
            "generation_id": 2,
            "state": "thinking",
        },
    }
    assert bridge._audio_messages.empty()

    bridge._on_data_received(
        SimpleNamespace(
            participant=agent,
            topic="voice-agent.ui",
            data=json.dumps(
                {
                    "type": "assistant_state",
                    "turn_id": 1,
                    "generation_id": 1,
                    "state": "speaking",
                }
            ).encode(),
        )
    )
    assert bridge._generation_id == 2
    assert bridge._event_messages.empty()
