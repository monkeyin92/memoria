from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from services.common.miniprogram_gateway_ticket import issue_gateway_ticket
from services.miniprogram_gateway.app import MEDIA_PATH, _validate_control_text, create_app
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
                {"type": "hello", "protocol_version": 1, "ticket": ticket}
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
            websocket.close()

    assert len(bridges) == 1
    assert [frame.sequence for frame in bridges[0].frames] == [0]
    assert bridges[0].closed is True


def test_gateway_text_channel_only_allows_transport_ping() -> None:
    _validate_control_text('{"type":"ping"}')

    with pytest.raises(ProtocolError, match="unsupported gateway control message"):
        _validate_control_text('{"type":"voice-agent.control","action":"stop"}')


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

    async def connect(self) -> None:
        return None

    async def accept_uplink(self, frame: object) -> None:
        self.frames.append(frame)
        await self._messages.put(
            GatewayOutboundMessage(
                event={"type": "accepted", "sequence": frame.sequence}
            )
        )

    async def next_outbound(self) -> GatewayOutboundMessage:
        return await self._messages.get()

    async def wait_for_room_disconnect(self) -> None:
        await self._disconnected.wait()

    async def close(self) -> None:
        self.closed = True
        self._disconnected.set()
