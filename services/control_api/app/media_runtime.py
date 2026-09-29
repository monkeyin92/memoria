"""Short-lived media-runtime credentials minted by the control plane."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import jwt

from services.control_api.app.wake_words import DEFAULT_WAKE_WORD_ID, WAKE_WORD_IDS

DEVICE_AUDIO_MODES: Final = frozenset(
    {"half_duplex_safe", "interrupt_assist", "full_duplex_verified"}
)
DEVICE_WAKE_MODES: Final = frozenset({"button", "keyword", "button_or_keyword"})
DEVICE_BARGE_IN_KINDS: Final = frozenset({"none", "button", "keyword", "voice"})
DEVICE_LEARNING_MODES: Final = frozenset({"off", "tutor_english", "tutor_homework"})

# Direct hardware media ticket contract (ADR-0035, plan 10.2): the Go Media
# Edge verifies these EdDSA JWTs against its JWKS.  The audience is dedicated
# to the direct hardware path and differs from the H5 streamcore audience
# ("memoria-media"); the Go edge pins the same value through
# MEDIA_EDGE_DEVICE_JWT_AUDIENCE, so H5 tokens can never open a device socket
# and device tickets can never be replayed against WHIP.
DEVICE_MEDIA_AUDIENCE: Final = "memoria-media-edge"
DEVICE_MEDIA_TOKEN_TYPE: Final = "memoria_device_media"
DEVICE_UINT32_MAX: Final = (1 << 32) - 1
DEVICE_STREAM_EPOCH_MAX: Final = DEVICE_UINT32_MAX


@dataclass(frozen=True, slots=True)
class DeviceDirectMediaTicket:
    token: str
    expires_at: datetime
    jti: str


def _secret(settings: Any) -> str:
    value = getattr(settings, "streamcore_token_secret", "")
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if callable(getter) else value or "").strip()


def _private_key(settings: Any) -> str:
    value = getattr(settings, "streamcore_token_private_key_pem", "")
    getter = getattr(value, "get_secret_value", None)
    inline = str(getter() if callable(getter) else value or "").strip()
    path = str(getattr(settings, "streamcore_token_private_key_file", "") or "").strip()
    if inline and path:
        raise ValueError("configure one StreamCore private key source")
    if inline:
        return inline
    if not path:
        return ""
    try:
        material = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("StreamCore private key file is unavailable") from exc
    if len(material) > 16_384:
        raise ValueError("StreamCore private key is too large")
    return material.strip()


def mint_streamcore_token(
    settings: Any,
    *,
    session_id: str,
    user_id: str,
    client_platform: str,
    device_id: str | None = None,
    stream_epoch: int = 1,
) -> tuple[str, datetime]:
    """Mint a session-scoped JWT without exposing provider credentials."""

    environment = str(getattr(settings, "environment", "development"))
    private_key = _private_key(settings)
    secret = _secret(settings)
    if not private_key and not secret:
        # Development fallback is intentionally the auth secret, never a
        # hard-coded value. Production validation rejects an empty source.
        auth_secret = getattr(settings, "memoria_auth_secret", "")
        getter = getattr(auth_secret, "get_secret_value", None)
        secret = str(getter() if callable(getter) else auth_secret or "").strip()
    if private_key == "" and environment == "production" and len(secret) < 32:
        raise ValueError(
            "production StreamCore requires an independent signing secret or an Ed25519 private key"
        )
    if private_key == "" and not secret:
        raise ValueError("StreamCore signing material is not configured")
    if stream_epoch < 1:
        raise ValueError("stream_epoch must be positive")
    ttl = int(getattr(settings, "streamcore_token_ttl_s", 120))
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl)
    token_device_id = device_id or ("h5" if client_platform == "h5" else None)
    claims = {
        "iss": str(getattr(settings, "jwt_issuer", "memoria-control-api")),
        "aud": "memoria-media",
        "sub": user_id,
        "session_id": session_id,
        "client_type": client_platform,
        "device_id": token_device_id,
        "stream_epoch": stream_epoch,
        "jti": hashlib.sha256(f"{session_id}:{now.timestamp()}".encode()).hexdigest(),
        "iat": now,
        "exp": expires_at,
    }
    if private_key:
        key_id = str(getattr(settings, "streamcore_token_key_id", "streamcore-1") or "").strip()
        if not key_id:
            raise ValueError("StreamCore signing key id is not configured")
        encoded: str | bytes = jwt.encode(
            claims,
            private_key,
            algorithm="EdDSA",
            headers={"kid": key_id},
        )
    else:
        encoded = jwt.encode(claims, secret, algorithm="HS256")
    if isinstance(encoded, bytes):
        return encoded.decode("ascii"), expires_at
    return encoded, expires_at


def mint_device_direct_media_ticket(
    settings: Any,
    *,
    session_id: str,
    user_id: str,
    device_id: str,
    client_id: str,
    binding_id: str,
    binding_version: int,
    subject_id: str | None,
    runtime_profile_version: int,
    device_settings: Mapping[str, object] | None = None,
    stream_epoch: int,
    now: datetime | None = None,
) -> DeviceDirectMediaTicket:
    """Mint the EdDSA hardware ticket for the Go Media Edge (JWKS contract)."""

    for label, value in (
        ("session_id", session_id),
        ("user_id", user_id),
        ("device_id", device_id),
        ("client_id", client_id),
        ("binding_id", binding_id),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"direct device media {label} must be a non-empty string")
    if subject_id is None:
        ticket_subject_id = ""
    elif not isinstance(subject_id, str) or (subject_id and not subject_id.strip()):
        raise ValueError(
            "direct device media subject_id must be null, empty, or a non-empty string"
        )
    else:
        ticket_subject_id = subject_id
    if isinstance(binding_version, bool) or binding_version < 1:
        raise ValueError("direct device media binding_version must be positive")
    if (
        isinstance(stream_epoch, bool)
        or stream_epoch < 1
        or stream_epoch > DEVICE_STREAM_EPOCH_MAX
    ):
        raise ValueError("direct device media stream_epoch must be a positive uint32")
    if (
        isinstance(runtime_profile_version, bool)
        or runtime_profile_version < 1
        or runtime_profile_version > DEVICE_UINT32_MAX
    ):
        raise ValueError(
            "direct device media runtime_profile_version must be a positive uint32"
        )
    settings_claim = _validate_device_settings_claim(device_settings)
    private_key = _private_key(settings)
    if not private_key:
        raise ValueError(
            "direct device media requires an Ed25519 private key "
            "(STREAMCORE_TOKEN_PRIVATE_KEY_FILE or STREAMCORE_TOKEN_PRIVATE_KEY_PEM)"
        )
    key_id = str(getattr(settings, "streamcore_token_key_id", "streamcore-1") or "").strip()
    if not key_id:
        raise ValueError("direct device media signing key id is not configured")
    ttl = int(getattr(settings, "streamcore_token_ttl_s", 120))
    issued_at = datetime.now(UTC) if now is None else now
    expires_at = issued_at + timedelta(seconds=ttl)
    jti = uuid.uuid4().hex
    claims = {
        "iss": str(getattr(settings, "jwt_issuer", "memoria-control-api")),
        "aud": DEVICE_MEDIA_AUDIENCE,
        "typ": DEVICE_MEDIA_TOKEN_TYPE,
        "session_id": session_id,
        "sub": user_id,
        "device_id": device_id,
        "client_id": client_id,
        "binding_id": binding_id,
        "binding_version": binding_version,
        # An unresolved Runtime Profile has no active natural-person subject.
        # JWT keeps the claim present for a stable Go/Python wire shape and
        # uses the one canonical empty representation instead of omitting the
        # fence or falling back to the binding/account owner.
        "subject_id": ticket_subject_id,
        "client_type": "device",
        "stream_epoch": stream_epoch,
        "runtime_profile_version": runtime_profile_version,
        "device_settings": settings_claim,
        "jti": jti,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": expires_at,
    }
    encoded: str | bytes = jwt.encode(
        claims,
        private_key,
        algorithm="EdDSA",
        headers={"kid": key_id},
    )
    if isinstance(encoded, bytes):
        return DeviceDirectMediaTicket(encoded.decode("ascii"), expires_at, jti)
    return DeviceDirectMediaTicket(encoded, expires_at, jti)


def _validate_device_settings_claim(
    value: Mapping[str, object] | None,
) -> dict[str, object]:
    """Return the strict, signed device settings snapshot carried by a ticket."""

    if value is None:
        # Legacy direct-ticket callers remain safe and deterministic. Control's
        # production route always passes the current authoritative snapshot.
        return {
            "settings_version": 0,
            "volume_limit": 30,
            "screen_brightness": 80,
            "night_mode": False,
            "do_not_disturb": False,
            "learning_mode": "off",
            "audio_mode": "half_duplex_safe",
            "wake_mode": "button_or_keyword",
            "wake_word_id": DEFAULT_WAKE_WORD_ID,
            "wake_word_pinyin": "mo li",
            "wake_word_display": "茉莉",
            "allowed_barge_in": ["button"],
        }
    required = {
        "settings_version",
        "volume_limit",
        "screen_brightness",
        "night_mode",
        "do_not_disturb",
        "learning_mode",
        "audio_mode",
        "wake_mode",
        "allowed_barge_in",
    }
    provided = set(value)
    if not required.issubset(provided):
        raise ValueError("direct device media settings shape is invalid")
    optional = {"wake_word_id", "wake_word_pinyin", "wake_word_display"}
    if provided - required - optional:
        raise ValueError("direct device media settings shape is invalid")
    integer_values: dict[str, int] = {}
    for field in ("settings_version", "volume_limit", "screen_brightness"):
        field_value = value[field]
        if isinstance(field_value, bool) or not isinstance(field_value, int):
            raise ValueError(f"direct device media {field} must be an integer")
        integer_values[field] = field_value
    if not 0 <= integer_values["settings_version"] <= DEVICE_UINT32_MAX:
        raise ValueError("direct device media settings_version must be a uint32")
    if not 0 <= integer_values["volume_limit"] <= 100:
        raise ValueError("direct device media volume_limit must be between 0 and 100")
    if not 0 <= integer_values["screen_brightness"] <= 100:
        raise ValueError("direct device media screen_brightness must be between 0 and 100")
    for field in ("night_mode", "do_not_disturb"):
        if not isinstance(value[field], bool):
            raise ValueError(f"direct device media {field} must be a boolean")
    if value["learning_mode"] not in DEVICE_LEARNING_MODES:
        raise ValueError("direct device media learning_mode is invalid")
    if value["audio_mode"] not in DEVICE_AUDIO_MODES:
        raise ValueError("direct device media audio_mode is invalid")
    if value["wake_mode"] not in DEVICE_WAKE_MODES:
        raise ValueError("direct device media wake_mode is invalid")
    wake_word_id = str(value.get("wake_word_id", DEFAULT_WAKE_WORD_ID))
    if wake_word_id not in WAKE_WORD_IDS:
        raise ValueError("direct device media wake_word_id is invalid")
    wake_word_pinyin = str(value.get("wake_word_pinyin", "mo li"))
    wake_word_display = str(value.get("wake_word_display", "茉莉"))
    kinds = value["allowed_barge_in"]
    if (
        not isinstance(kinds, (list, tuple))
        or not kinds
        or len(kinds) != len(set(kinds))
        or any(kind not in DEVICE_BARGE_IN_KINDS for kind in kinds)
    ):
        raise ValueError("direct device media allowed_barge_in is invalid")
    return {
        "settings_version": integer_values["settings_version"],
        "volume_limit": integer_values["volume_limit"],
        "screen_brightness": integer_values["screen_brightness"],
        "night_mode": bool(value["night_mode"]),
        "do_not_disturb": bool(value["do_not_disturb"]),
        "learning_mode": str(value["learning_mode"]),
        "audio_mode": str(value["audio_mode"]),
        "wake_mode": str(value["wake_mode"]),
        "wake_word_id": wake_word_id,
        "wake_word_pinyin": wake_word_pinyin,
        "wake_word_display": wake_word_display,
        "allowed_barge_in": [str(kind) for kind in kinds],
    }


__all__ = [
    "DEVICE_MEDIA_AUDIENCE",
    "DEVICE_MEDIA_TOKEN_TYPE",
    "DeviceDirectMediaTicket",
    "mint_device_direct_media_ticket",
    "mint_streamcore_token",
]
