from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from services.common.miniprogram_gateway_ticket import issue_gateway_ticket
from services.miniprogram_gateway.app import (
    MEDIA_PATH,
    _receive_hello,
    _validate_control_text,
    create_app,
)
from services.miniprogram_gateway.bridge import GatewayOutboundMessage
from services.miniprogram_gateway.config import MiniProgramGatewaySettings
from services.miniprogram_gateway.protocol import FrameType, ProtocolError, encode_pcm_frame


@pytest.mark.asyncio
async def test_gateway_health_does_not_require_livekit_connection() -> None:
    app = create_app(settings=MiniProgramGatewaySettings())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_gateway_accepts_ticket_then_only_pcm_uplink_frames() -> None:
    secret = "gateway-ticket-secret-that-is-long-enough"
    settings = MiniProgramGatewaySettings(
        livekit_url="wss://livekit.example.com",
        livekit_api_key="livekit-key",
        livekit_api_secret="livekit-secret",
        memoria_miniprogram_gateway_ticket_secret=secret,
    )
    ticket, _ = issue_gateway_ticket(
        secret=secret,
        session_id="session-1",
        user_id="account-1",
        room_name="voice-session-1",
        identity="user-account-1-session",
        agent_name="duplex-zh-agent",
        ttl_s=90,
    )
    bridges: list[FakeBridge] = []

    def factory(_: MiniProgramGatewaySettings, __: object) -> FakeBridge:
        bridge = FakeBridge()
        bridges.append(bridge)
        return bridge

    with TestClient(create_app(settings=settings, bridge_factory=factory)) as client:
        with client.websocket_connect(MEDIA_PATH) as websocket:
            websocket.send_json(
                {
                    "type": "hello",
                    "protocol_version": 1,
                    "ticket": ticket,
                    "capabilities": {"downlink_generation": 2},
                }
            )
            assert websocket.receive_json()["type"] == "ready"
            websocket.send_bytes(
                encode_pcm_frame(
                    FrameType.UPLINK_AUDIO,
                    sequence=0,
                    timestamp_ms=1,
                    payload=b"\x00\x00",
                )
            )
            assert websocket.receive_json() == {"type": "accepted", "sequence": 0}
            websocket.send_json(
                {
                    "type": "playout_interrupt",
                    "generation_id": 7,
                    "client_timestamp_ms": 123,
                }
            )
            websocket.close()

    assert len(bridges) == 1
    assert [frame.sequence for frame in bridges[0].frames] == [0]
    assert bridges[0].transport_events == [
        {
            "type": "playout_interrupt",
            "generation_id": 7,
            "client_timestamp_ms": 123,
        }
    ]
    assert bridges[0].downlink_generation_protocol is True
    assert bridges[0].closed is True


@pytest.mark.asyncio
async def test_legacy_gateway_hello_keeps_v1_downlink_frames() -> None:
    secret = "gateway-ticket-secret-that-is-long-enough"
    settings = MiniProgramGatewaySettings(
        memoria_miniprogram_gateway_ticket_secret=secret,
    )
    ticket, _ = issue_gateway_ticket(
        secret=secret,
        session_id="session-1",
        user_id="account-1",
        room_name="voice-session-1",
        identity="user-account-1-session",
        agent_name="duplex-zh-agent",
        ttl_s=90,
    )

    class FakeWebSocket:
        async def receive_json(self) -> dict[str, object]:
            return {"type": "hello", "protocol_version": 1, "ticket": ticket}

    _claims, downlink_generation_protocol = await _receive_hello(
        FakeWebSocket(),  # type: ignore[arg-type]
        settings,
    )

    assert downlink_generation_protocol is False


def test_gateway_text_channel_only_allows_transport_ping() -> None:
    assert _validate_control_text('{"type":"ping"}') == {"type": "ping"}
    assert _validate_control_text(
        '{"type":"playout_reset","generation_id":3,'
        '"barrier_sequence":7,"client_timestamp_ms":123}'
    ) == {
        "type": "playout_reset",
        "generation_id": 3,
        "barrier_sequence": 7,
        "client_timestamp_ms": 123,
    }
    assert _validate_control_text(
        '{"type":"playout_interrupt","generation_id":3,"client_timestamp_ms":124}'
    ) == {
        "type": "playout_interrupt",
        "generation_id": 3,
        "client_timestamp_ms": 124,
    }

    with pytest.raises(ProtocolError, match="unsupported gateway control message"):
        _validate_control_text('{"type":"voice-agent.control","action":"stop"}')
    with pytest.raises(ProtocolError, match="invalid gateway playout event"):
        _validate_control_text(
            '{"type":"playout_interrupt","generation_id":3,'
            '"client_timestamp_ms":124,"action":"stop"}'
        )


class FakeBridge:
    def __init__(self) -> None:
        self.ready_event: dict[str, object] = {
            "type": "ready",
            "protocol_version": 1,
            "session_id": "session-1",
            "audio": {
                "sample_rate": 24000,
                "channels": 1,
                "sample_format": "s16le",
                "frame_ms": 20,
            },
        }
        self.frames: list[object] = []
        self._messages: asyncio.Queue[GatewayOutboundMessage] = asyncio.Queue()
        self._disconnected = asyncio.Event()
        self.closed = False
        self.sent: list[GatewayOutboundMessage] = []
        self.transport_events: list[dict[str, object]] = []
        self.downlink_generation_protocol = False

    async def connect(self) -> None:
        return None

    def set_downlink_generation_protocol(self, enabled: bool) -> None:
        self.downlink_generation_protocol = enabled

    async def accept_uplink(self, frame: object) -> None:
        self.frames.append(frame)
        await self._messages.put(
            GatewayOutboundMessage(
                event={"type": "accepted", "sequence": frame.sequence}
            )
        )

    def accept_transport_event(self, event: dict[str, object]) -> None:
        self.transport_events.append(event)

    async def next_outbound(self) -> GatewayOutboundMessage:
        return await self._messages.get()

    def outbound_sent(self, message: GatewayOutboundMessage) -> None:
        self.sent.append(message)

    async def wait_for_room_disconnect(self) -> None:
        await self._disconnected.wait()

    async def close(self) -> None:
        self.closed = True
        self._disconnected.set()
