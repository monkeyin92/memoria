from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from services.control_api.app.main import create_app
from services.speaker.authority import SpeakerAuthority
from services.speaker.domain import EmbeddingResult


class FakeEmbeddingAdapter:
    model_version = "campplus-api-test-v1"

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        _ = pcm, sample_rate
        return EmbeddingResult(
            vector=(1.0, 0.0),
            speech_ms=1800,
            snr_db=20,
            quality_score=0.95,
            replay_risk=0.02,
            synthetic_risk=0.02,
        )


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


@pytest.mark.asyncio
async def test_delivered_capabilities_reports_identity_signals_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.speaker_authority = SpeakerAuthority.sqlite(
        tmp_path / "speakers.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "dash-owner", "password": "safe-password"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        response = await client.get("/v1/account/delivered-capabilities", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["advertised_duplex_level"] == "none"
    assert payload["speaker_profiles_active"] == 0
    assert payload["speaker_enrollment_ready"] is False
    assert payload["product_positioning"] == "family_archive_terminal"
    assert payload["ai_disclosure_required"] is True
    assert payload["model_training_contribution_enabled"] is False
    assert payload["account_export_available"] is True
    assert payload["account_deletion_available"] is True
    assert payload["devices_bound"] == 0
    assert payload["memory_days_with_activity"] == 0
    assert payload["total_messages"] == 0
    assert "TAM" not in payload["notes"]


@pytest.mark.asyncio
async def test_delivered_capabilities_counts_memory_and_devices(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.speaker_authority = SpeakerAuthority.sqlite(
        tmp_path / "speakers.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
    )
    store = app.state.memory_store
    now = datetime.now(UTC).isoformat()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "dash-metrics", "password": "safe-password"},
        )
        user_id = registered.json()["user_id"]
        headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
        store.register_device_identity(
            device_id="dev_metrics",
            account_id=user_id,
            public_key_b64="dGVzdC1wdWJsaWMta2V5",
            firmware_channel="stable",
            now=now,
        )
        store.add_message(
            user_id=user_id,
            client_message_id="msg-1",
            request_fingerprint="fp-1",
            role="user",
            text="hello",
            emotion="neutral",
            local_date="2026-08-31",
            created_at=now,
        )
        store.add_message(
            user_id=user_id,
            client_message_id="msg-2",
            request_fingerprint="fp-2",
            role="assistant",
            text="hi",
            emotion="neutral",
            local_date="2026-08-31",
            created_at=now,
        )
        response = await client.get("/v1/account/delivered-capabilities", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["devices_bound"] == 1
    assert payload["memory_days_with_activity"] == 1
    assert payload["total_messages"] == 2
