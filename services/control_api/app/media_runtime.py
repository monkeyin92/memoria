"""Control-plane selection and short-lived media-runtime credentials."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import jwt

MediaRuntime = Literal["livekit", "streamcore"]


@dataclass(frozen=True, slots=True)
class MediaRuntimeDecision:
    runtime: MediaRuntime
    rollback_required: bool = False
    reason: str = "configured"


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


def select_media_runtime(
    settings: Any,
    *,
    user_id: str,
    client_platform: str,
    device_id: str | None = None,
) -> MediaRuntime:
    """Return a server-owned runtime flag; clients cannot force rollout."""

    if client_platform != "h5":
        return "livekit"
    if bool(getattr(settings, "streamcore_kill_switch", False)):
        return "livekit"
    if str(getattr(settings, "media_runtime_default", "livekit")) != "streamcore":
        return "livekit"
    whip_url = str(getattr(settings, "streamcore_whip_url", "")).strip()
    if not whip_url:
        return "livekit"
    percent = int(getattr(settings, "streamcore_experiment_percent", 0))
    if percent <= 0:
        return "livekit"
    if percent >= 100:
        return "streamcore"
    key = f"{user_id}:{device_id or ''}".encode()
    bucket = int.from_bytes(hashlib.sha256(key).digest()[:4], "big") % 100
    return "streamcore" if bucket < percent else "livekit"


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
            "production StreamCore requires an independent signing secret "
            "or an Ed25519 private key"
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


def decide_media_runtime(
    settings: Any,
    *,
    user_id: str,
    client_platform: str,
    device_id: str | None = None,
    observed_slo: Mapping[str, float] | None = None,
) -> MediaRuntimeDecision:
    """Apply the deterministic rollout flag and optional SLO rollback gate."""

    selected = select_media_runtime(
        settings,
        user_id=user_id,
        client_platform=client_platform,
        device_id=device_id,
    )
    if selected != "streamcore" or observed_slo is None:
        if (
            selected == "streamcore"
            and bool(getattr(settings, "streamcore_slo_gate_enabled", False))
            and observed_slo is None
        ):
            return MediaRuntimeDecision(
                "livekit",
                rollback_required=True,
                reason="missing_media_slo_snapshot",
            )
        return MediaRuntimeDecision(selected)
    from services.agent.src.voice_core.slo import evaluate_slo

    report = evaluate_slo(observed_slo)
    if report.rollback_required:
        return MediaRuntimeDecision(
            "livekit",
            rollback_required=True,
            reason=";".join(report.failures),
        )
    return MediaRuntimeDecision("streamcore")


__all__ = [
    "MediaRuntimeDecision",
    "decide_media_runtime",
    "mint_streamcore_token",
    "select_media_runtime",
]
