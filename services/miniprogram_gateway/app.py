"""FastAPI WebSocket service for the WeChat Mini Program media adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
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
    CLIENT_AUDIO_TRACE_DETAIL_FIELDS,
    CLIENT_AUDIO_TRACE_NAMES,
    FrameType,
    ProtocolError,
    decode_pcm_frame,
)

logger = logging.getLogger(__name__)
MEDIA_PATH = "/v1/mini-program/media"
HEADER_HANDSHAKE_PROTOCOL = "x-memoria-gateway-protocol"
HEADER_HANDSHAKE_TICKET = "x-memoria-gateway-ticket"
HEADER_HANDSHAKE_DOWNLINK_GENERATION = "x-memoria-downlink-generation"
BridgeFactory = Callable[
    [MiniProgramGatewaySettings, GatewayTicketClaims], MiniProgramLiveKitBridge
]


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
        bridge: MiniProgramLiveKitBridge | None = None
        handshake_started = time.monotonic()
        handshake_transport = "unknown"
        try:
            await websocket.accept()
            header_handshake = _read_header_handshake(websocket, configured)
            if header_handshake is None:
                claims, downlink_generation_protocol = await _receive_hello(
                    websocket,
                    configured,
                )
                handshake_transport = "hello"
            else:
                claims, downlink_generation_protocol = header_handshake
                handshake_transport = "header"
            await websocket.send_json(_handshake_ack(handshake_transport))
            logger.info(
                "mini_program_gateway_handshake transport=%s phase=ack_sent elapsed_ms=%d",
                handshake_transport,
                _elapsed_ms(handshake_started),
            )
            bridge = bridge_factory(configured, claims)
            bridge.set_downlink_generation_protocol(downlink_generation_protocol)
            await bridge.connect()
            await websocket.send_json(bridge.ready_event)
            logger.info(
                "mini_program_gateway_handshake transport=%s phase=ready_sent elapsed_ms=%d",
                handshake_transport,
                _elapsed_ms(handshake_started),
            )
            sender = asyncio.create_task(
                _send_outbound(websocket, bridge),
                name="mini-program-media-sender",
            )
            receiver = asyncio.create_task(
                _receive_media(
                    websocket,
                    bridge,
                    ignore_first_duplicate_hello=handshake_transport == "header",
                ),
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
            logger.info(
                "mini_program_gateway_handshake transport=%s phase=ticket_rejected elapsed_ms=%d",
                handshake_transport,
                _elapsed_ms(handshake_started),
            )
            await _close_safely(websocket, 4401)
        except (GatewayMediaError, ProtocolError):
            logger.info(
                "mini_program_gateway_handshake transport=%s phase=protocol_error elapsed_ms=%d",
                handshake_transport,
                _elapsed_ms(handshake_started),
            )
            await _close_safely(websocket, 4400)
        except (TimeoutError, json.JSONDecodeError, TypeError, ValueError):
            logger.info(
                "mini_program_gateway_handshake transport=%s phase=invalid_handshake elapsed_ms=%d",
                handshake_transport,
                _elapsed_ms(handshake_started),
            )
            await _close_safely(websocket, 4400)
        except Exception:
            logger.exception("Mini Program media socket failed without logging client payload")
            await _close_safely(websocket, 1011)
        finally:
            if bridge is not None:
                await bridge.close()

    return app


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _handshake_ack(transport: str) -> dict[str, object]:
    return {"type": "handshake_ack", "protocol_version": 1, "transport": transport}


def _read_header_handshake(
    websocket: WebSocket,
    settings: MiniProgramGatewaySettings,
) -> tuple[GatewayTicketClaims, bool] | None:
    """Authenticate the native client before its first SocketTask callback.

    iOS can complete the WebSocket HTTP Upgrade before delivering a SocketTask
    ``onOpen`` callback.  The same short-lived signed ticket is therefore
    accepted in TLS-protected handshake headers, while older clients keep using
    the first text ``hello`` message below.  The ticket never appears in the URL.
    """

    ticket = websocket.headers.get(HEADER_HANDSHAKE_TICKET)
    if ticket is None:
        return None
    if websocket.headers.get(HEADER_HANDSHAKE_PROTOCOL) != "1":
        raise GatewayTicketError("invalid gateway header handshake")
    downlink_generation_protocol = (
        websocket.headers.get(HEADER_HANDSHAKE_DOWNLINK_GENERATION) == "2"
    )
    return (
        verify_gateway_ticket(
            ticket,
            secret=settings.memoria_miniprogram_gateway_ticket_secret.get_secret_value(),
            max_ttl_s=settings.miniprogram_gateway_ticket_max_ttl_s,
        ),
        downlink_generation_protocol,
    )


async def _receive_hello(
    websocket: WebSocket,
    settings: MiniProgramGatewaySettings,
) -> tuple[GatewayTicketClaims, bool]:
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
    capabilities = hello.get("capabilities")
    downlink_generation_protocol = (
        isinstance(capabilities, dict) and capabilities.get("downlink_generation") == 2
    )
    return (
        verify_gateway_ticket(
            ticket,
            secret=settings.memoria_miniprogram_gateway_ticket_secret.get_secret_value(),
            max_ttl_s=settings.miniprogram_gateway_ticket_max_ttl_s,
        ),
        downlink_generation_protocol,
    )


async def _receive_media(
    websocket: WebSocket,
    bridge: MiniProgramLiveKitBridge,
    *,
    ignore_first_duplicate_hello: bool = False,
) -> None:
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
        if ignore_first_duplicate_hello:
            # A native SocketTask can report onOpen after header authentication
            # and even after its first PCM frame. Consume only that exact v1
            # fallback hello; every subsequent text frame remains strict.
            ignore_first_duplicate_hello = False
            if _is_duplicate_hello(text):
                continue
        bridge.accept_transport_event(_validate_control_text(text))


def _is_duplicate_hello(text: Any) -> bool:
    if not isinstance(text, str) or len(text) > 1_024:
        return False
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(text)
        return (
            isinstance(parsed, dict)
            and set(parsed) == {"type", "protocol_version", "ticket", "capabilities"}
            and parsed.get("type") == "hello"
            and parsed.get("protocol_version") == 1
            and isinstance(parsed.get("ticket"), str)
            and parsed.get("capabilities") == {"downlink_generation": 2}
        )
    return False


def _validate_control_text(text: Any) -> dict[str, object]:
    if not isinstance(text, str) or len(text) > 1_024:
        raise ProtocolError("invalid gateway control message")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("invalid gateway control JSON") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("unsupported gateway control message")
    if parsed.get("type") == "ping" and len(parsed) == 1:
        return {"type": "ping"}
    event_type = parsed.get("type")
    if not isinstance(event_type, str):
        raise ProtocolError("unsupported gateway control message")
    if event_type == "client_audio_trace":
        trace_required_keys = {
            "type",
            "name",
            "generation_id",
            "client_timestamp_ms",
            "detail",
        }
        detail = parsed.get("detail")
        if (
            set(parsed) != trace_required_keys
            or parsed.get("name") not in CLIENT_AUDIO_TRACE_NAMES
            or isinstance(parsed.get("generation_id"), bool)
            or not isinstance(parsed.get("generation_id"), int)
            or not 0 <= parsed["generation_id"] <= 0xFFFFFFFF
            or isinstance(parsed.get("client_timestamp_ms"), bool)
            or not isinstance(parsed.get("client_timestamp_ms"), int)
            or not 0 <= parsed["client_timestamp_ms"] <= 0xFFFFFFFFFFFFFFFF
            or not isinstance(detail, dict)
            or not detail
            or not set(detail).issubset(CLIENT_AUDIO_TRACE_DETAIL_FIELDS)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 0xFFFFFFFF
                for value in detail.values()
            )
        ):
            raise ProtocolError("invalid client audio trace")
        return {
            "type": event_type,
            "name": parsed["name"],
            "generation_id": parsed["generation_id"],
            "client_timestamp_ms": parsed["client_timestamp_ms"],
            "detail": dict(detail),
        }
    required_keys = {
        "playout_interrupt": {"type", "generation_id", "client_timestamp_ms"},
        "playout_reset": {
            "type",
            "generation_id",
            "barrier_sequence",
            "client_timestamp_ms",
        },
        "uplink_discontinuity": {
            "type",
            "next_sequence",
            "client_timestamp_ms",
        },
    }.get(event_type)
    if required_keys is None:
        raise ProtocolError("unsupported gateway control message")
    if set(parsed) != required_keys:
        raise ProtocolError("invalid gateway playout event")
    for key in required_keys - {"type"}:
        value = parsed.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 0xFFFFFFFFFFFFFFFF
        ):
            raise ProtocolError("invalid gateway playout event")
    if event_type == "uplink_discontinuity" and parsed["next_sequence"] > 0xFFFFFFFF:
        raise ProtocolError("invalid gateway playout event")
    return {key: parsed[key] for key in required_keys}


async def _send_outbound(websocket: WebSocket, bridge: MiniProgramLiveKitBridge) -> None:
    while True:
        message = await bridge.next_outbound()
        if message.binary is not None:
            await websocket.send_bytes(message.binary)
            bridge.outbound_sent(message)
        elif message.event is not None:
            await websocket.send_json(message.event)
            bridge.outbound_sent(message)
        else:  # pragma: no cover - dataclass invariant
            raise RuntimeError("invalid outbound media message")


async def _close_safely(websocket: WebSocket, code: int) -> None:
    with contextlib.suppress(RuntimeError):
        await websocket.close(code=code)


app = create_app()
