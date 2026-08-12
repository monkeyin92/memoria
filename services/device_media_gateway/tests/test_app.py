from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from services.common.miniprogram_gateway_ticket import issue_device_gateway_ticket
from services.device_media_gateway.app import MEDIA_PATH, create_app
from services.device_media_gateway.config import DeviceMediaGatewaySettings
from services.device_media_gateway.protocol import (
    DOWNLINK_FRAME_SAMPLES,
    FrameType,
    decode_audio_frame,
)
from services.miniprogram_gateway.bridge import GatewayOutboundMessage
from services.miniprogram_gateway.protocol import (
    FrameType as PcmFrameType,
)
from services.miniprogram_gateway.protocol import encode_pcm_frame

SECRET = "device-gateway-secret-that-is-long-enough-for-tests"


def _settings() -> DeviceMediaGatewaySettings:
    return DeviceMediaGatewaySettings(
        memoria_device_gateway_ticket_secret=SECRET,
        livekit_url="wss://livekit.example",
        livekit_api_key="key",
        livekit_api_secret="secret",
    )


def _ticket(
    *, device_id: str = "dev-1", client_id: str = "client-1", now_s: int | None = None
) -> str:
    token, _ = issue_device_gateway_ticket(
        secret=SECRET,
        session_id="session-1",
        user_id="account-1",
        device_id=device_id,
        client_id=client_id,
        binding_id="binding-1",
        binding_version=1,
        room_name="room-1",
        identity="device-dev-1",
        agent_name="duplex-zh-agent",
        stream_epoch=1,
        ttl_s=90,
        now_s=now_s,
    )
    return token


def _hello(device_id: str = "dev-1") -> dict[str, object]:
    return {
        "type": "device.hello",
        "version": 1,
        "device_id": device_id,
        "firmware_version": "0.1.0",
        "board_profile": "memoria-atk-dnesp32s3-v1",
        "stream_epoch": 1,
        "capabilities": {
            "display": True,
            "microphone": True,
            "speaker": True,
            "device_aec": False,
            "physical_button": True,
        },
        "audio": {
            "uplink_codec": "opus",
            "uplink_sample_rate": 16000,
            "downlink_sample_rate": 24000,
            "channels": 1,
            "frame_ms": 20,
        },
    }


class FakeBridge:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.closed = False
        self._disconnect = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def accept_uplink(self, _frame: object) -> None:
        return None

    def accept_transport_event(self, event: dict[str, object]) -> None:
        self.events.append(event)

    async def next_outbound(self) -> GatewayOutboundMessage:
        await self._disconnect.wait()
        raise RuntimeError("fake bridge stopped")

    def outbound_sent(self, _message: GatewayOutboundMessage) -> None:
        return None

    async def wait_for_room_disconnect(self) -> None:
        await self._disconnect.wait()

    async def close(self) -> None:
        self.closed = True
        self._disconnect.set()


class FailingBridge(FakeBridge):
    async def connect(self) -> None:
        raise ValueError("internal LiveKit connection failed")


class WelcomeBridge(FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        pcm = bytes(DOWNLINK_FRAME_SAMPLES * 2)
        self._outbound: asyncio.Queue[GatewayOutboundMessage] = asyncio.Queue()
        self._outbound.put_nowait(
            GatewayOutboundMessage(
                event={"type": "audio_reset", "generation_id": 0, "barrier_sequence": 0}
            )
        )
        self._outbound.put_nowait(
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

    async def next_outbound(self) -> GatewayOutboundMessage:
        return await self._outbound.get()


def _headers(
    token: str, *, device_id: str = "dev-1", client_id: str = "client-1"
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Protocol-Version": "1",
        "Device-Id": device_id,
        "Client-Id": client_id,
    }


def test_device_wss_requires_client_id_bound_to_ticket_and_accepts_strict_hello() -> None:
    bridges: list[FakeBridge] = []

    def factory(_settings: DeviceMediaGatewaySettings, _claims: object) -> FakeBridge:
        bridge = FakeBridge()
        bridges.append(bridge)
        return bridge

    app = create_app(settings=_settings(), bridge_factory=factory)
    with TestClient(app) as client:
        with client.websocket_connect(MEDIA_PATH, headers=_headers(_ticket())) as websocket:
            websocket.send_json(_hello())
            assert websocket.receive_json() == {
                "type": "session.ready",
                "session_id": "session-1",
                "stream_epoch": 1,
                "turn_id": 0,
                "generation_id": 0,
                "tool_epoch": 0,
            }
            websocket.send_json({"type": "listen.start", "stream_epoch": 1, "sample_start": 0})
            websocket.close()

    assert len(bridges) == 1
    assert bridges[0].events == [{"type": "listen.start", "stream_epoch": 1, "sample_start": 0}]
    assert bridges[0].closed


def test_device_wss_projects_welcome_generation_zero_onto_positive_device_fence() -> None:
    bridge = WelcomeBridge()
    app = create_app(settings=_settings(), bridge_factory=lambda _settings, _claims: bridge)
    with TestClient(app) as client:
        with client.websocket_connect(MEDIA_PATH, headers=_headers(_ticket())) as websocket:
            websocket.send_json(_hello())
            assert websocket.receive_json()["type"] == "session.ready"
            assert websocket.receive_json() == {
                "type": "playback.flush",
                "stream_epoch": 1,
                "generation_id": 1,
                "barrier_sequence": 0,
            }
            frame = decode_audio_frame(
                websocket.receive_bytes(), expected_type=FrameType.DOWNLINK_AUDIO
            )
            assert frame.generation_id == 1
            assert frame.frame_samples == DOWNLINK_FRAME_SAMPLES
            websocket.send_json(
                {
                    "type": "playback.ended",
                    "stream_epoch": 1,
                    "generation_id": 1,
                    "played_sample_end": DOWNLINK_FRAME_SAMPLES,
                    "reason": "drained",
                }
            )
            websocket.close()

    assert bridge.events[-1]["generation_id"] == 0


@pytest.mark.parametrize(
    "headers",
    [
        {"Device-Id": "dev-1", "Client-Id": "client-1"},
        _headers(_ticket(), client_id="wrong"),
        _headers(_ticket(), device_id="dev-2"),
    ],
)
def test_device_wss_rejects_missing_or_cross_bound_headers(headers: dict[str, str]) -> None:
    with TestClient(create_app(settings=_settings())) as client:
        with client.websocket_connect(MEDIA_PATH, headers=headers) as websocket:
            close = websocket.receive()
    assert close["type"] == "websocket.close"
    assert close["code"] in {4400, 4401, 403}


def test_device_wss_rejects_expired_ticket_before_hello() -> None:
    expired = _ticket(now_s=int(time.time()) - 200)
    with TestClient(create_app(settings=_settings())) as client:
        with client.websocket_connect(MEDIA_PATH, headers=_headers(expired)) as websocket:
            close = websocket.receive()
    assert close["type"] == "websocket.close"
    assert close["code"] == 4401


def test_device_wss_reports_internal_bridge_failure_as_server_error() -> None:
    app = create_app(
        settings=_settings(),
        bridge_factory=lambda _settings, _claims: FailingBridge(),
    )
    with TestClient(app) as client:
        with client.websocket_connect(MEDIA_PATH, headers=_headers(_ticket())) as websocket:
            websocket.send_json(_hello())
            close = websocket.receive()
    assert close["type"] == "websocket.close"
    assert close["code"] == 1011


def test_production_gateway_rejects_the_device_development_ticket_secret() -> None:
    settings = DeviceMediaGatewaySettings(
        environment="production",
        livekit_url="wss://livekit.example",
        livekit_api_key="key",
        livekit_api_secret="livekit-secret-material-long-enough",
    )
    with pytest.raises(ValueError, match="independent ticket secret"):
        settings.validate_production()
