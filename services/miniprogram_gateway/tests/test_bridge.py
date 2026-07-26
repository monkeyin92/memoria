from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import jwt
import pytest
from livekit import rtc
from services.common.miniprogram_gateway_ticket import (
    MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
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


def test_default_downlink_queue_is_bounded_to_400_ms() -> None:
    settings = MiniProgramGatewaySettings()

    assert (
        settings.miniprogram_gateway_audio_queue_frames
        * settings.miniprogram_gateway_frame_ms
        == 400
    )


@pytest.mark.parametrize(
    ("aec_enabled", "apm_state", "expected_metadata"),
    [
        (True, "ready", MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA),
        (False, "disabled", None),
        (True, "init_failed", None),
        (True, "process_failed", None),
    ],
)
def test_gateway_dispatch_disables_agent_warmup_only_when_aec_is_ready(
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


def test_production_rejects_downlink_queue_over_400_ms() -> None:
    settings = MiniProgramGatewaySettings(
        environment="production",
        livekit_url="wss://livekit.example.com",
        livekit_api_key="livekit-key",
        livekit_api_secret="livekit-secret-that-is-long-enough",
        memoria_miniprogram_gateway_ticket_secret=(
            "gateway-ticket-secret-that-is-long-enough"
        ),
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
