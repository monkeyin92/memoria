"""FastAPI WebSocket endpoint for Memoria hardware devices."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from services.common.miniprogram_gateway_ticket import (
    DeviceGatewayTicketClaims,
    GatewayTicketError,
    verify_device_gateway_ticket,
)
from services.device_media_gateway.config import DeviceMediaGatewaySettings
from services.device_media_gateway.protocol import (
    ProtocolError,
    parse_json_message,
    validate_device_hello,
)
from services.device_media_gateway.session import (
    DeviceBridge,
    DeviceMediaSession,
    MiniProgramBridgeAdapter,
)

logger = logging.getLogger("uvicorn.error")
MEDIA_PATH = "/v1/device/media"
HEADER_PROTOCOL_VERSION = "protocol-version"
HEADER_DEVICE_ID = "device-id"
HEADER_CLIENT_ID = "client-id"
HEADER_AUTHORIZATION = "authorization"
BridgeFactory = Callable[[DeviceMediaGatewaySettings, DeviceGatewayTicketClaims], DeviceBridge]


def _default_bridge_factory(
    settings: DeviceMediaGatewaySettings,
    claims: DeviceGatewayTicketClaims,
) -> DeviceBridge:
    return MiniProgramBridgeAdapter(settings=settings, claims=claims)


def create_app(
    *,
    settings: DeviceMediaGatewaySettings | None = None,
    bridge_factory: BridgeFactory = _default_bridge_factory,
) -> FastAPI:
    configured = settings or DeviceMediaGatewaySettings()
    active_tickets: set[str] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configured.validate_production()
        app.state.settings = configured
        yield

    app = FastAPI(title="Memoria Device Media Gateway", lifespan=lifespan)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.websocket(MEDIA_PATH)
    async def media_socket(websocket: WebSocket) -> None:
        session: DeviceMediaSession | None = None
        ticket_id: str | None = None
        protocol_component = "handshake"
        started = time.monotonic()
        try:
            await websocket.accept()
            claims = _authenticate_headers(websocket, configured)
            ticket_id = claims.ticket_id
            if ticket_id in active_tickets:
                raise GatewayTicketError("device gateway ticket is already active")
            active_tickets.add(ticket_id)
            hello = await _receive_hello(websocket, configured, claims)
            bridge = bridge_factory(configured, claims)
            session = DeviceMediaSession(settings=configured, claims=claims, bridge=bridge)
            await session.connect()
            await websocket.send_json(session.ready_event)
            sender = asyncio.create_task(
                _send_outbound(websocket, session), name="device-media-sender"
            )
            receiver = asyncio.create_task(
                _receive_media(websocket, session), name="device-media-receiver"
            )
            room_disconnect = asyncio.create_task(
                session.wait_for_room_disconnect(), name="device-media-room-disconnect"
            )
            done, pending = await asyncio.wait(
                {sender, receiver, room_disconnect}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                if not task.cancelled():
                    protocol_component = task.get_name().removeprefix("device-media-")
                    task.result()
            if room_disconnect in done:
                await _close_safely(websocket, 1011)
            _ = hello
        except WebSocketDisconnect:
            pass
        except GatewayTicketError:
            logger.info("device_media_handshake rejected elapsed_ms=%d", _elapsed_ms(started))
            await _close_safely(websocket, 4401)
        except ProtocolError as exc:
            logger.info(
                "device_media_protocol rejected component=%s reason=%s elapsed_ms=%d",
                protocol_component,
                str(exc),
                _elapsed_ms(started),
            )
            await _close_safely(websocket, 4400)
        except (TimeoutError, json.JSONDecodeError) as exc:
            logger.info(
                "device_media_protocol rejected component=%s reason=%s elapsed_ms=%d",
                protocol_component,
                "timeout" if isinstance(exc, TimeoutError) else "invalid_json",
                _elapsed_ms(started),
            )
            await _close_safely(websocket, 4400)
        except Exception:
            logger.exception("device media socket failed without logging client payload")
            await _close_safely(websocket, 1011)
        finally:
            if session is not None:
                await session.close()
            if ticket_id is not None:
                active_tickets.discard(ticket_id)

    return app


def _authenticate_headers(
    websocket: WebSocket,
    settings: DeviceMediaGatewaySettings,
) -> DeviceGatewayTicketClaims:
    authorization = websocket.headers.get(HEADER_AUTHORIZATION)
    if not isinstance(authorization, str):
        raise GatewayTicketError("missing device authorization")
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0] != "Bearer" or not parts[1] or any(not part for part in parts):
        raise GatewayTicketError("invalid device authorization")
    if websocket.headers.get(HEADER_PROTOCOL_VERSION) != "1":
        raise ProtocolError("Protocol-Version must be 1")
    device_id = _header_string(websocket, HEADER_DEVICE_ID)
    client_id = _header_string(websocket, HEADER_CLIENT_ID)
    claims = verify_device_gateway_ticket(
        parts[1],
        secret=settings.memoria_device_gateway_ticket_secret.get_secret_value(),
        max_ttl_s=settings.device_media_gateway_ticket_max_ttl_s,
    )
    if claims.device_id != device_id:
        raise GatewayTicketError("Device-Id does not match ticket")
    if claims.client_id != client_id:
        raise GatewayTicketError("Client-Id does not match ticket")
    return claims


async def _receive_hello(
    websocket: WebSocket,
    settings: DeviceMediaGatewaySettings,
    claims: DeviceGatewayTicketClaims,
) -> dict[str, object]:
    message = await asyncio.wait_for(
        websocket.receive(), timeout=settings.device_media_gateway_handshake_timeout_s
    )
    if message.get("type") == "websocket.disconnect":
        raise ProtocolError("device disconnected before hello")
    text = message.get("text")
    if text is None:
        raise ProtocolError("device.hello must be a text message")
    return validate_device_hello(
        parse_json_message(text), device_id=claims.device_id, stream_epoch=claims.stream_epoch
    )


async def _receive_media(websocket: WebSocket, session: DeviceMediaSession) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            await session.accept_binary(data)
            continue
        text = message.get("text")
        if text is None:
            raise ProtocolError("unsupported device WebSocket message")
        session.accept_text(text)


async def _send_outbound(websocket: WebSocket, session: DeviceMediaSession) -> None:
    while True:
        message = await session.next_outbound()
        if message.binary is not None:
            await websocket.send_bytes(message.binary)
        elif message.event is not None:
            await websocket.send_json(message.event)
        else:
            raise ProtocolError("empty device outbound message")
        session.outbound_sent(message)


def _header_string(websocket: WebSocket, name: str) -> str:
    value = websocket.headers.get(name)
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > 128
    ):
        raise ProtocolError(f"invalid {name} header")
    return value


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1_000)


async def _close_safely(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=code)


app = create_app()
