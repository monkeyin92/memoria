"""Short-lived signed claims proving a physical device may be bound."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta


class DeviceBindingTokenError(ValueError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def mint_device_binding_token(
    *,
    device_id: str,
    secret: bytes,
    now: datetime,
    ttl: timedelta,
    nonce: str,
) -> str:
    if len(secret) < 16:
        raise ValueError("device binding token secret is too short")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if ttl <= timedelta(0):
        raise ValueError("ttl must be positive")
    if not device_id.strip() or not nonce.strip():
        raise ValueError("device_id and nonce must not be empty")
    payload = json.dumps(
        {
            "aud": "memoria-device-binding",
            "device_id": device_id,
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
            "nonce": nonce,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = _b64encode(payload)
    signature = hmac.new(secret, f"v1.{encoded}".encode("ascii"), hashlib.sha256).digest()
    return f"v1.{encoded}.{_b64encode(signature)}"


def verify_device_binding_token(
    token: str,
    *,
    secret: bytes,
    now: datetime,
) -> str:
    if len(secret) < 16:
        raise DeviceBindingTokenError("device binding token secret is too short")
    try:
        version, encoded, supplied_signature = token.split(".")
    except ValueError as exc:
        raise DeviceBindingTokenError("device binding token format is invalid") from exc
    if version != "v1":
        raise DeviceBindingTokenError("device binding token version is invalid")
    expected = hmac.new(
        secret,
        f"v1.{encoded}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        supplied = _b64decode(supplied_signature)
    except Exception as exc:
        raise DeviceBindingTokenError("device binding token signature is invalid") from exc
    if not hmac.compare_digest(expected, supplied):
        raise DeviceBindingTokenError("device binding token signature is invalid")
    try:
        payload = json.loads(_b64decode(encoded))
    except Exception as exc:
        raise DeviceBindingTokenError("device binding token payload is invalid") from exc
    if payload.get("aud") != "memoria-device-binding":
        raise DeviceBindingTokenError("device binding token audience is invalid")
    expires_at = payload.get("exp")
    issued_at = payload.get("iat")
    device_id = payload.get("device_id")
    if (
        not isinstance(expires_at, int)
        or not isinstance(issued_at, int)
        or not isinstance(device_id, str)
        or not device_id
    ):
        raise DeviceBindingTokenError("device binding token payload is invalid")
    current = int(now.timestamp())
    if current < issued_at:
        raise DeviceBindingTokenError("device binding token is not yet valid")
    if current >= expires_at:
        raise DeviceBindingTokenError("device binding token expired")
    return device_id
