"""Short-lived, audience-bound tickets for the Mini Program media gateway."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Final

import jwt

GATEWAY_TICKET_AUDIENCE: Final = "memoria-miniprogram-media-gateway"
GATEWAY_TICKET_ISSUER: Final = "memoria-control-api"
GATEWAY_TICKET_TYPE: Final = "memoria_miniprogram_gateway"
DEVICE_GATEWAY_TICKET_AUDIENCE: Final = "memoria-device-media-gateway"
DEVICE_GATEWAY_TICKET_TYPE: Final = "memoria_device_gateway"
MINIPROGRAM_AGENT_DISPATCH_METADATA: Final = "memoria.miniprogram.v1"
DEVICE_AGENT_DISPATCH_METADATA: Final = "memoria.device.v1"
MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA: Final = "memoria.miniprogram.aec.v1"
MINIPROGRAM_AEC_HEALTH_TOPIC: Final = "voice-agent.gateway-health"
MINIPROGRAM_AEC_HEALTH_ACK_TOPIC: Final = "voice-agent.gateway-health.ack"
MINIPROGRAM_AEC_FAILED: Final = "miniprogram_aec_failed"
MINIPROGRAM_AEC_FAILED_ACK: Final = "miniprogram_aec_failed_ack"


class GatewayTicketError(ValueError):
    """A gateway ticket is malformed, expired, or issued for another service."""


@dataclass(frozen=True, slots=True)
class GatewayTicketClaims:
    session_id: str
    user_id: str
    room_name: str
    identity: str
    agent_name: str
    voice_backend: str
    issued_at_s: int
    expires_at_s: int
    ticket_id: str


@dataclass(frozen=True, slots=True)
class DeviceGatewayTicketClaims:
    """Claims frozen by Control API for exactly one bound hardware session."""

    session_id: str
    user_id: str
    device_id: str
    client_id: str
    binding_id: str
    binding_version: int
    subject_id: str
    runtime_profile_version: int
    room_name: str
    identity: str
    agent_name: str
    stream_epoch: int
    voice_backend: str
    issued_at_s: int
    expires_at_s: int
    ticket_id: str


def _require_string(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise GatewayTicketError(f"invalid gateway ticket {name}")
    return value


def _require_timestamp(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GatewayTicketError(f"invalid gateway ticket {name}")
    return value


def _require_positive_int(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GatewayTicketError(f"invalid gateway ticket {name}")
    return value


def issue_gateway_ticket(
    *,
    secret: str,
    session_id: str,
    user_id: str,
    room_name: str,
    identity: str,
    agent_name: str,
    ttl_s: int,
    now_s: int | None = None,
) -> tuple[str, int]:
    """Create a bearer ticket that authorizes exactly one frozen cascade session."""
    if not secret:
        raise ValueError("gateway ticket signing secret is required")
    if ttl_s <= 0:
        raise ValueError("gateway ticket ttl must be positive")
    fields = {
        "session_id": session_id,
        "user_id": user_id,
        "room_name": room_name,
        "identity": identity,
        "agent_name": agent_name,
    }
    if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
        raise ValueError("gateway ticket claims must be non-empty strings")
    issued_at = int(time.time()) if now_s is None else now_s
    if issued_at < 0:
        raise ValueError("gateway ticket issue time must not be negative")
    payload = {
        "iss": GATEWAY_TICKET_ISSUER,
        "aud": GATEWAY_TICKET_AUDIENCE,
        "typ": GATEWAY_TICKET_TYPE,
        "sid": session_id,
        "sub": user_id,
        "room": room_name,
        "identity": identity,
        "agent_name": agent_name,
        "voice_backend": "cascade",
        "jti": str(uuid.uuid4()),
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + ttl_s,
    }
    encoded = jwt.encode(payload, secret, algorithm="HS256")
    return (encoded.decode("utf-8") if isinstance(encoded, bytes) else str(encoded), ttl_s)


def verify_gateway_ticket(
    token: str,
    *,
    secret: str,
    now_s: int | None = None,
    max_ttl_s: int = 300,
) -> GatewayTicketClaims:
    """Verify an independently scoped ticket without ever logging its contents."""
    if not token.strip() or not secret:
        raise GatewayTicketError("gateway ticket is missing")
    if max_ttl_s <= 0:
        raise ValueError("gateway ticket max ttl must be positive")
    try:
        decoded = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=GATEWAY_TICKET_AUDIENCE,
            issuer=GATEWAY_TICKET_ISSUER,
            options={
                "require": ["exp", "iat", "nbf", "sub", "sid", "aud", "iss", "jti"],
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise GatewayTicketError("gateway ticket verification failed") from exc
    if not isinstance(decoded, dict):
        raise GatewayTicketError("invalid gateway ticket payload")
    payload = dict(decoded)
    issued_at = _require_timestamp(payload, "iat")
    not_before = _require_timestamp(payload, "nbf")
    expires_at = _require_timestamp(payload, "exp")
    current = int(time.time()) if now_s is None else now_s
    if (
        not_before != issued_at
        or expires_at <= issued_at
        or expires_at - issued_at > max_ttl_s
        or current < not_before
        or current >= expires_at
    ):
        raise GatewayTicketError("gateway ticket is not currently valid")
    if payload.get("typ") != GATEWAY_TICKET_TYPE:
        raise GatewayTicketError("invalid gateway ticket type")
    if payload.get("voice_backend") != "cascade":
        raise GatewayTicketError("gateway ticket backend is not allowed")
    return GatewayTicketClaims(
        session_id=_require_string(payload, "sid"),
        user_id=_require_string(payload, "sub"),
        room_name=_require_string(payload, "room"),
        identity=_require_string(payload, "identity"),
        agent_name=_require_string(payload, "agent_name"),
        voice_backend="cascade",
        issued_at_s=issued_at,
        expires_at_s=expires_at,
        ticket_id=_require_string(payload, "jti"),
    )


def issue_device_gateway_ticket(
    *,
    secret: str,
    session_id: str,
    user_id: str,
    device_id: str,
    client_id: str,
    binding_id: str,
    binding_version: int,
    subject_id: str,
    runtime_profile_version: int,
    room_name: str,
    identity: str,
    agent_name: str,
    stream_epoch: int,
    ttl_s: int,
    now_s: int | None = None,
) -> tuple[str, int]:
    """Issue a device-only ticket that cannot authenticate a Mini Program socket."""

    if not secret:
        raise ValueError("device gateway ticket signing secret is required")
    if ttl_s <= 0:
        raise ValueError("device gateway ticket ttl must be positive")
    fields = {
        "session_id": session_id,
        "user_id": user_id,
        "device_id": device_id,
        "client_id": client_id,
        "binding_id": binding_id,
        "subject_id": subject_id,
        "room_name": room_name,
        "identity": identity,
        "agent_name": agent_name,
    }
    if any(not isinstance(value, str) or not value.strip() for value in fields.values()):
        raise ValueError("device gateway ticket claims must be non-empty strings")
    if isinstance(binding_version, bool) or binding_version < 1:
        raise ValueError("device gateway binding version must be positive")
    if isinstance(stream_epoch, bool) or stream_epoch < 1:
        raise ValueError("device gateway stream epoch must be positive")
    if isinstance(runtime_profile_version, bool) or runtime_profile_version < 1:
        raise ValueError("device gateway runtime profile version must be positive")
    issued_at = int(time.time()) if now_s is None else now_s
    if issued_at < 0:
        raise ValueError("device gateway ticket issue time must not be negative")
    payload = {
        "iss": GATEWAY_TICKET_ISSUER,
        "aud": DEVICE_GATEWAY_TICKET_AUDIENCE,
        "typ": DEVICE_GATEWAY_TICKET_TYPE,
        "sid": session_id,
        "sub": user_id,
        "device_id": device_id,
        "client_id": client_id,
        "binding_id": binding_id,
        "binding_version": binding_version,
        "subject_id": subject_id,
        "runtime_profile_version": runtime_profile_version,
        "room": room_name,
        "identity": identity,
        "agent_name": agent_name,
        "stream_epoch": stream_epoch,
        "voice_backend": "cascade",
        "jti": str(uuid.uuid4()),
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + ttl_s,
    }
    encoded = jwt.encode(payload, secret, algorithm="HS256")
    return (encoded.decode("utf-8") if isinstance(encoded, bytes) else str(encoded), ttl_s)


def verify_device_gateway_ticket(
    token: str,
    *,
    secret: str,
    now_s: int | None = None,
    max_ttl_s: int = 300,
) -> DeviceGatewayTicketClaims:
    """Verify a short-lived device media ticket and all binding fences."""

    if not token.strip() or not secret:
        raise GatewayTicketError("device gateway ticket is missing")
    if max_ttl_s <= 0:
        raise ValueError("device gateway ticket max ttl must be positive")
    try:
        decoded = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=DEVICE_GATEWAY_TICKET_AUDIENCE,
            issuer=GATEWAY_TICKET_ISSUER,
            options={
                "require": [
                    "exp",
                    "iat",
                    "nbf",
                    "sub",
                    "sid",
                    "aud",
                    "iss",
                    "jti",
                    "device_id",
                    "client_id",
                    "binding_id",
                    "binding_version",
                    "subject_id",
                    "runtime_profile_version",
                    "stream_epoch",
                ],
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError as exc:
        raise GatewayTicketError("device gateway ticket verification failed") from exc
    if not isinstance(decoded, dict):
        raise GatewayTicketError("invalid device gateway ticket payload")
    payload = dict(decoded)
    issued_at = _require_timestamp(payload, "iat")
    not_before = _require_timestamp(payload, "nbf")
    expires_at = _require_timestamp(payload, "exp")
    current = int(time.time()) if now_s is None else now_s
    if (
        not_before != issued_at
        or expires_at <= issued_at
        or expires_at - issued_at > max_ttl_s
        or current < not_before
        or current >= expires_at
    ):
        raise GatewayTicketError("device gateway ticket is not currently valid")
    if payload.get("typ") != DEVICE_GATEWAY_TICKET_TYPE:
        raise GatewayTicketError("invalid device gateway ticket type")
    if payload.get("voice_backend") != "cascade":
        raise GatewayTicketError("device gateway ticket backend is not allowed")
    return DeviceGatewayTicketClaims(
        session_id=_require_string(payload, "sid"),
        user_id=_require_string(payload, "sub"),
        device_id=_require_string(payload, "device_id"),
        client_id=_require_string(payload, "client_id"),
        binding_id=_require_string(payload, "binding_id"),
        binding_version=_require_positive_int(payload, "binding_version"),
        subject_id=_require_string(payload, "subject_id"),
        runtime_profile_version=_require_positive_int(payload, "runtime_profile_version"),
        room_name=_require_string(payload, "room"),
        identity=_require_string(payload, "identity"),
        agent_name=_require_string(payload, "agent_name"),
        stream_epoch=_require_positive_int(payload, "stream_epoch"),
        voice_backend="cascade",
        issued_at_s=issued_at,
        expires_at_s=expires_at,
        ticket_id=_require_string(payload, "jti"),
    )
