from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from services.control_api.app.media_runtime import mint_streamcore_token
from services.control_api.app.media_slo import MediaSLOGate


def settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "streamcore_token_secret": SecretStr("streamcore-secret-material-that-is-long-enough"),
        "streamcore_token_ttl_s": 120,
        "environment": "development",
        "jwt_issuer": "memoria-control-api",
        "memoria_auth_secret": SecretStr("auth-secret-material-that-is-long-enough"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_streamcore_token_is_short_lived_and_session_scoped() -> None:
    token, expires_at = mint_streamcore_token(
        settings(),
        session_id="session-1",
        user_id="user-1",
        client_platform="h5",
        device_id="device-1",
    )
    assert token.count(".") == 2
    assert expires_at.tzinfo is not None


def test_streamcore_token_uses_configured_eddsa_private_key() -> None:
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    token, _ = mint_streamcore_token(
        settings(
            streamcore_token_secret=SecretStr(""),
            streamcore_token_private_key_pem=SecretStr(pem),
            streamcore_token_key_id="media-2026-08",
        ),
        session_id="session-eddsa",
        user_id="user-1",
        client_platform="h5",
    )
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "EdDSA", "kid": "media-2026-08", "typ": "JWT"}
    decoded = jwt.decode(
        token,
        private_key.public_key(),
        algorithms=["EdDSA"],
        audience="memoria-media",
        issuer="memoria-control-api",
    )
    assert decoded["session_id"] == "session-eddsa"


def test_production_token_requires_independent_secret() -> None:
    with pytest.raises(ValueError, match="independent signing secret"):
        mint_streamcore_token(
            settings(
                environment="production",
                streamcore_token_secret=SecretStr("short"),
            ),
            session_id="session-1",
            user_id="user-1",
            client_platform="h5",
        )


@pytest.mark.asyncio
async def test_media_slo_gate_ttl_and_report_are_authoritative() -> None:
    gate = MediaSLOGate(ttl_s=30)
    observed_at = datetime(2026, 8, 3, tzinfo=UTC)
    snapshot = await gate.publish(
        {
            "first_audio_p95_ms": 700,
            "tts_first_frame_p95_ms": 120,
            "interrupt_stop_p95_ms": 100,
            "session_failure_rate": 0.01,
            "stale_generation_total": 0,
            "stale_asr_final_total": 0,
        },
        source="test-agent",
        observed_at=observed_at,
    )
    assert snapshot.report.passed
    assert (await gate.current(now=observed_at + timedelta(seconds=29))) is not None
    assert await gate.current(now=observed_at + timedelta(seconds=30)) is None
