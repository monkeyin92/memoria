from __future__ import annotations

import asyncio

import pytest

from services.common.miniprogram_gateway_ticket import DeviceGatewayTicketClaims
from services.device_media_gateway.config import DeviceMediaGatewaySettings
from services.device_media_gateway.opus import OpusEncoder
from services.device_media_gateway.protocol import (
    DOWNLINK_FRAME_SAMPLES,
    UPLINK_FRAME_SAMPLES,
    FrameType,
    ProtocolError,
    decode_audio_frame,
    encode_audio_frame,
)
from services.device_media_gateway.session import DeviceMediaSession
from services.miniprogram_gateway.bridge import GatewayOutboundMessage
from services.miniprogram_gateway.protocol import (
    FrameType as PcmFrameType,
)
from services.miniprogram_gateway.protocol import encode_pcm_frame


def _claims() -> DeviceGatewayTicketClaims:
    return DeviceGatewayTicketClaims(
        session_id="session-1",
        user_id="person-1",
        device_id="device-1",
        client_id="installation-1",
        binding_id="binding-1",
        binding_version=1,
        room_name="voice-session-1",
        identity="user-person-1-session",
        agent_name="duplex-zh-agent",
        stream_epoch=3,
        voice_backend="cascade",
        issued_at_s=1,
        expires_at_s=301,
        ticket_id="ticket-1",
    )


class FakeBridge:
    def __init__(self) -> None:
        self.uplink: list[object] = []
        self.events: list[dict[str, object]] = []
        self.outbound: asyncio.Queue[GatewayOutboundMessage] = asyncio.Queue()

    async def connect(self) -> None:
        return None

    async def accept_uplink(self, frame: object) -> None:
        self.uplink.append(frame)

    def accept_transport_event(self, event: dict[str, object]) -> None:
        self.events.append(event)

    async def next_outbound(self) -> GatewayOutboundMessage:
        return await self.outbound.get()

    def outbound_sent(self, _message: GatewayOutboundMessage) -> None:
        return None

    async def wait_for_room_disconnect(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        return None


def _session(bridge: FakeBridge) -> DeviceMediaSession:
    return DeviceMediaSession(
        settings=DeviceMediaGatewaySettings(),
        claims=_claims(),
        bridge=bridge,
    )


@pytest.mark.asyncio
async def test_uplink_opus_keeps_transport_and_decoded_sample_clocks_separate() -> None:
    bridge = FakeBridge()
    session = _session(bridge)
    encoder = OpusEncoder(sample_rate=16_000, frame_samples=UPLINK_FRAME_SAMPLES)
    for sequence in range(3):
        packet = encoder.encode(bytes(UPLINK_FRAME_SAMPLES * 2))
        await session.accept_binary(
            encode_audio_frame(
                FrameType.UPLINK_AUDIO,
                stream_epoch=3,
                sequence=sequence,
                sample_start=sequence * UPLINK_FRAME_SAMPLES,
                frame_samples=UPLINK_FRAME_SAMPLES,
                generation_id=0,
                payload=packet,
            )
        )
    assert bridge.uplink
    first = bridge.uplink[0]
    assert first.sequence == 0
    assert first.timestamp_ms == 0
    with pytest.raises(ProtocolError, match="sequence"):
        await session.accept_binary(
            encode_audio_frame(
                FrameType.UPLINK_AUDIO,
                stream_epoch=3,
                sequence=5,
                sample_start=3 * UPLINK_FRAME_SAMPLES,
                frame_samples=UPLINK_FRAME_SAMPLES,
                generation_id=0,
                payload=encoder.encode(bytes(UPLINK_FRAME_SAMPLES * 2)),
            )
        )


@pytest.mark.asyncio
async def test_downlink_requires_generation_reset_and_bounds_playback_receipts() -> None:
    bridge = FakeBridge()
    session = _session(bridge)
    await bridge.outbound.put(
        GatewayOutboundMessage(
            event={"type": "audio_reset", "generation_id": 0, "barrier_sequence": 0}
        )
    )
    reset = await session.next_outbound()
    assert reset.event == {
        "type": "playback.flush",
        "stream_epoch": 3,
        "generation_id": 1,
        "barrier_sequence": 0,
    }
    pcm = bytes(DOWNLINK_FRAME_SAMPLES * 2)
    await bridge.outbound.put(
        GatewayOutboundMessage(
            binary=encode_pcm_frame(
                PcmFrameType.DOWNLINK_AUDIO,
                sequence=0,
                timestamp_ms=0,
                payload=pcm,
                generation_id=0,
            ),
            audio_reference=pcm,
            generation_id=0,
        )
    )
    outbound = await session.next_outbound()
    assert outbound.binary is not None
    frame = decode_audio_frame(outbound.binary, expected_type=FrameType.DOWNLINK_AUDIO)
    assert frame.stream_epoch == 3
    assert frame.sequence == 0
    assert frame.sample_start == 0
    assert frame.frame_samples == DOWNLINK_FRAME_SAMPLES
    assert frame.generation_id == 1
    session.accept_text(
        '{"type":"playback.ended","stream_epoch":3,"generation_id":1,'
        '"played_sample_end":480,"reason":"completed"}'
    )
    assert bridge.events[-1]["generation_id"] == 0
    with pytest.raises(ProtocolError, match="ahead"):
        session.accept_text(
            '{"type":"playback.progress","stream_epoch":3,"generation_id":1,'
            '"played_sample_end":481}'
        )


@pytest.mark.asyncio
async def test_each_bridge_generation_maps_to_one_positive_device_generation() -> None:
    bridge = FakeBridge()
    session = _session(bridge)

    for bridge_generation_id, device_generation_id in ((0, 1), (1, 2)):
        await bridge.outbound.put(
            GatewayOutboundMessage(
                event={
                    "type": "audio_reset",
                    "generation_id": bridge_generation_id,
                    "barrier_sequence": 0,
                }
            )
        )
        reset = await session.next_outbound()
        assert reset.event is not None
        assert reset.event["generation_id"] == device_generation_id

        await bridge.outbound.put(
            GatewayOutboundMessage(
                event={
                    "type": "ui_event",
                    "event": {
                        "type": "assistant_state",
                        "generation_id": bridge_generation_id,
                        "state": "speaking",
                    },
                }
            )
        )
        state = await session.next_outbound()
        assert state.event is not None
        assert state.event["generation_id"] == device_generation_id


def test_bridge_generation_mapping_rejects_uint32_overflow() -> None:
    with pytest.raises(ProtocolError, match="outside the device range"):
        DeviceMediaSession._device_generation_id(0xFFFFFFFF)
