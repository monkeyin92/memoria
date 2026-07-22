from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from services.control_api.app.config import ControlSettings
from services.control_api.app.main import create_app
from services.speaker.authority import SpeakerAuthority
from services.speaker.domain import EmbeddingResult

ROOT = Path(__file__).resolve().parents[3]


class FakeEmbeddingAdapter:
    model_version = "campplus-api-test-v1"

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        _ = sample_rate
        vectors = {
            b"owner-01": (1.0, 0.0),
            b"owner-02": (0.99, 0.01),
            b"owner-03": (0.98, 0.02),
            b"owner-live": (0.97, 0.03),
            b"guest-live": (0.0, 1.0),
        }
        return EmbeddingResult(
            vector=vectors[pcm],
            speech_ms=1800,
            snr_db=20,
            quality_score=0.95,
            replay_risk=0.02,
            synthetic_risk=0.02,
        )


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-archive-token")
    monkeypatch.setenv("MEMORIA_SPEAKER_INTERNAL_TOKEN", "test-speaker-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


def _audio(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def test_shadow_guest_runtime_default_and_examples_are_040(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMORIA_SPEAKER_GUEST_THRESHOLD", raising=False)

    assert ControlSettings(_env_file=None).speaker_guest_threshold == 0.40
    assert "MEMORIA_SPEAKER_GUEST_THRESHOLD=0.40" in (ROOT / ".env.example").read_text(
        encoding="utf-8"
    )
    assert "MEMORIA_SPEAKER_GUEST_THRESHOLD=0.40" in (
        ROOT / "infra" / "memoria.env.production.example"
    ).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_registered_owner_manages_shadow_active_and_revoked_speaker_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    app.state.speaker_authority = SpeakerAuthority.sqlite(
        tmp_path / "speakers.sqlite3",
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=FakeEmbeddingAdapter(),
        owner_threshold=0.8,
        guest_threshold=0.4,
    )
    internal = {"X-Memoria-Speaker-Token": "test-speaker-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "speaker-owner", "password": "safe-password"},
            )
        ).json()
        owner_headers = {"Authorization": f"Bearer {identity['access_token']}"}
        enrolled = await client.post(
            "/v1/speakers/enrollments",
            headers=owner_headers,
            json={
                "consent_policy_version": "speaker-biometric-v1",
                "consent_accepted": True,
                "samples": [
                    {"audio_base64": _audio(value), "sample_rate": 16000}
                    for value in (b"owner-01", b"owner-02", b"owner-03")
                ],
            },
        )
        assert enrolled.status_code == 201, enrolled.text
        profile_id = enrolled.json()["profile_id"]
        profiles = await client.get("/v1/speakers", headers=owner_headers)
        activated = await client.post(
            f"/v1/speakers/{profile_id}/activate",
            headers=internal,
            json={
                "account_id": identity["user_id"],
                "evaluation_ref": "eval-authorized-api-001",
                "sample_count": 200,
                "far": 0.02,
                "frr": 0.08,
                "eer": 0.05,
                "unknown_rejection": 0.93,
                "passed": True,
            },
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=owner_headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        owner = await client.post(
            "/v1/speakers/classify",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "audio_base64": _audio(b"owner-live"),
                "sample_rate": 16000,
            },
        )
        policy_update = await client.put(
            f"/v1/memory/profile/{identity['user_id']}",
            headers=owner_headers,
            json={"reject_non_owner_voice": False},
        )
        guest = await client.post(
            "/v1/speakers/classify",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "audio_base64": _audio(b"guest-live"),
                "sample_rate": 16000,
            },
        )
        revoked = await client.request(
            "DELETE",
            f"/v1/speakers/{profile_id}",
            headers=owner_headers,
            json={"reason": "用户撤销"},
        )
        after_revoke = await client.post(
            "/v1/speakers/classify",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "audio_base64": _audio(b"owner-live"),
                "sample_rate": 16000,
            },
        )

    assert enrolled.json()["status"] == "shadow"
    assert profiles.json()["items"][0]["status"] == "shadow"
    assert activated.status_code == 204
    assert owner.json()["classification"] == "owner"
    assert owner.json()["permissions"]["read_private_memory"] is True
    assert owner.json()["reject_non_owner_voice"] is True
    assert policy_update.status_code == 200
    assert guest.json()["classification"] == "guest"
    assert guest.json()["reject_non_owner_voice"] is False
    assert guest.json()["permissions"] == {
        "normal_conversation": True,
        "read_private_memory": False,
        "write_long_term_memory": False,
        "sensitive_actions": False,
    }
    assert revoked.status_code == 204
    assert after_revoke.json()["classification"] == "uncertain"
    assert after_revoke.json()["reason_code"] == "no_active_profile"


@pytest.mark.asyncio
async def test_anonymous_or_untrusted_call_cannot_manage_biometric_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = (await client.post("/v1/auth/anonymous")).json()
        enrollment = await client.post(
            "/v1/speakers/enrollments",
            headers={"Authorization": f"Bearer {anonymous['access_token']}"},
            json={
                "consent_policy_version": "speaker-biometric-v1",
                "consent_accepted": True,
                "samples": [{"audio_base64": _audio(b"owner-01"), "sample_rate": 16000}] * 3,
            },
        )
        classify = await client.post(
            "/v1/speakers/classify",
            json={
                "session_id": "unknown",
                "audio_base64": _audio(b"owner-live"),
                "sample_rate": 16000,
            },
        )

    assert enrollment.status_code == 403
    assert classify.status_code == 401
