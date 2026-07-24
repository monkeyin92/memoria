"""FastAPI WebSocket service for the WeChat Mini Program media adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from services.common.miniprogram_gateway_ticket import (
    GatewayTicketClaims,
    GatewayTicketError,
    verify_gateway_ticket,
)
from services.miniprogram_gateway.bridge import (
    GatewayMediaError,
    MiniProgramLiveKitBridge,
)
from services.miniprogram_gateway.config import MiniProgramGatewaySettings
from services.miniprogram_gateway.protocol import (
    FrameType,
    ProtocolError,
    decode_pcm_frame,
)

logger = logging.getLogger(__name__)
MEDIA_PATH = "/v1/mini-program/media"
BridgeFactory = Callable[[MiniProgramGatewaySettings, GatewayTicketClaims], MiniProgramLiveKitBridge]


def _default_bridge_factory(
    settings: MiniProgramGatewaySettings,
    claims: GatewayTicketClaims,
) -> MiniProgramLiveKitBridge:
    return MiniProgramLiveKitBridge(settings=settings, claims=claims)


def create_app(
    *,
    settings: MiniProgramGatewaySettings | None = None,
    bridge_factory: BridgeFactory = _default_bridge_factory,
) -> FastAPI:
    configured = settings or MiniProgramGatewaySettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured.validate_production()
        app.state.settings = configured
        yield

    app = FastAPI(title="Memoria Mini Program Media Gateway", lifespan=lifespan)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket(MEDIA_PATH)
    async def media_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        bridge: MiniProgramLiveKitBridge | None = None
        try:
            claims = await _receive_hello(websocket, configured)
            bridge = bridge_factory(configured, claims)
            await bridge.connect()
            await websocket.send_json(bridge.ready_event)
            sender = asyncio.create_task(
                _send_outbound(websocket, bridge),
                name="mini-program-media-sender",
            )
            receiver = asyncio.create_task(
                _receive_media(websocket, bridge),
                name="mini-program-media-receiver",
            )
            room_disconnect = asyncio.create_task(
                bridge.wait_for_room_disconnect(),
                name="mini-program-livekit-disconnect",
            )
            done, pending = await asyncio.wait(
                {sender, receiver, room_disconnect},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if task.cancelled():
                    continue
                task.result()
            if room_disconnect in done:
                await _close_safely(websocket, 1011)
        except WebSocketDisconnect:
            pass
        except GatewayTicketError:
            await _close_safely(websocket, 4401)
        except (GatewayMediaError, ProtocolError):
            await _close_safely(websocket, 4400)
        except (TimeoutError, json.JSONDecodeError, TypeError, ValueError):
            await _close_safely(websocket, 4400)
        except Exception:
            logger.exception("Mini Program media socket failed without logging client payload")
            await _close_safely(websocket, 1011)
        finally:
            if bridge is not None:
                await bridge.close()

    return app


async def _receive_hello(
    websocket: WebSocket,
    settings: MiniProgramGatewaySettings,
) -> GatewayTicketClaims:
    hello = await asyncio.wait_for(
        websocket.receive_json(),
        timeout=settings.miniprogram_gateway_handshake_timeout_s,
    )
    if not isinstance(hello, dict):
        raise GatewayTicketError("invalid gateway hello")
    if hello.get("type") != "hello" or hello.get("protocol_version") != 1:
        raise GatewayTicketError("invalid gateway hello")
    ticket = hello.get("ticket")
    if not isinstance(ticket, str):
        raise GatewayTicketError("missing gateway ticket")
    return verify_gateway_ticket(
        ticket,
        secret=settings.memoria_miniprogram_gateway_ticket_secret.get_secret_value(),
        max_ttl_s=settings.miniprogram_gateway_ticket_max_ttl_s,
    )


async def _receive_media(websocket: WebSocket, bridge: MiniProgramLiveKitBridge) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            frame = decode_pcm_frame(data, expected_type=FrameType.UPLINK_AUDIO)
            await bridge.accept_uplink(frame)
            continue
        text = message.get("text")
        if text is None:
            raise ProtocolError("unsupported gateway WebSocket message")
        _validate_control_text(text)


def _validate_control_text(text: Any) -> None:
    if not isinstance(text, str) or len(text) > 1_024:
        raise ProtocolError("invalid gateway control message")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid gateway control JSON") from exc
    # WebSocket transport control deliberately has no business-command surface.
    # Existing stop/recovery actions remain authenticated Control API calls.
    if not isinstance(parsed, dict) or parsed.get("type") != "ping" or len(parsed) != 1:
        raise ProtocolError("unsupported gateway control message")


async def _send_outbound(websocket: WebSocket, bridge: MiniProgramLiveKitBridge) -> None:
    while True:
        message = await bridge.next_outbound()
        if message.binary is not None:
            await websocket.send_bytes(message.binary)
        elif message.event is not None:
            await websocket.send_json(message.event)
        else:  # pragma: no cover - dataclass invariant
            raise RuntimeError("invalid outbound media message")


async def _close_safely(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=code)


app = create_app()
