from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from services.control_api.app.media_runtime import (
    decide_media_runtime,
    mint_streamcore_token,
    select_media_runtime,
)
from services.control_api.app.media_slo import MediaSLOGate


def settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "media_runtime_default": "streamcore",
        "streamcore_experiment_percent": 100,
        "streamcore_kill_switch": False,
        "streamcore_whip_url": "https://media.example/whip",
        "streamcore_token_secret": SecretStr("streamcore-secret-material-that-is-long-enough"),
        "streamcore_token_ttl_s": 120,
        "environment": "development",
        "jwt_issuer": "memoria-control-api",
        "memoria_auth_secret": SecretStr("auth-secret-material-that-is-long-enough"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_selection_is_server_owned_and_h5_only() -> None:
    configured = settings()
    assert select_media_runtime(
        configured,
        user_id="user",
        client_platform="h5",
    ) == "streamcore"
    assert select_media_runtime(
        configured,
        user_id="user",
        client_platform="miniprogram",
    ) == "livekit"
    assert select_media_runtime(
        settings(streamcore_kill_switch=True),
        user_id="user",
        client_platform="h5",
    ) == "livekit"


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


def test_slo_gate_rolls_streamcore_back_to_livekit() -> None:
    decision = decide_media_runtime(
        settings(),
        user_id="user",
        client_platform="h5",
        observed_slo={"stale_generation_total": 1},
    )
    assert decision.runtime == "livekit"
    assert decision.rollback_required


def test_enabled_slo_gate_fails_closed_without_a_snapshot() -> None:
    decision = decide_media_runtime(
        settings(streamcore_slo_gate_enabled=True),
        user_id="user",
        client_platform="h5",
    )
    assert decision.runtime == "livekit"
    assert decision.reason == "missing_media_slo_snapshot"


@pytest.mark.asyncio
async def test_media_slo_gate_ttl_and_report_are_authoritative() -> None:
    gate = MediaSLOGate(ttl_s=30)
    observed_at = datetime(2026, 8, 3, tzinfo=UTC)
    snapshot = await gate.publish(
        {
            "first_audio_p95_ms": 700,
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
