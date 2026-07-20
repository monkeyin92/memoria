from __future__ import annotations

import base64
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.speaker.authority import SpeakerAuthority
from services.speaker.domain import EmbeddingResult


class _RestartEmbeddingAdapter:
    model_version = "campplus-restart-test-v1"

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        _ = sample_rate
        vectors = {
            b"owner-01": (1.0, 0.0),
            b"owner-02": (0.99, 0.01),
            b"owner-03": (0.98, 0.02),
        }
        return EmbeddingResult(
            vector=vectors[pcm],
            speech_ms=1800,
            snr_db=20,
            quality_score=0.95,
            replay_risk=0.02,
            synthetic_risk=0.02,
        )


def _configure_test_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


@pytest.mark.asyncio
async def test_registered_account_can_log_back_into_the_same_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = await client.post(
            "/v1/auth/register",
            json={"username": "MemoriaOwner", "password": "safe-passphrase"},
        )

        assert registered.status_code == 201
        first = registered.json()
        assert first["username"] == "MemoriaOwner"
        assert first["account_type"] == "registered"
        assert first["access_token"]

        logged_in = await client.post(
            "/v1/auth/login",
            json={"username": "memoriaowner", "password": "safe-passphrase"},
        )
        assert logged_in.status_code == 200
        second = logged_in.json()
        assert second["user_id"] == first["user_id"]
        assert second["access_token"]

        current = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {second['access_token']}"},
        )

    assert current.status_code == 200
    assert current.json() == {
        "user_id": first["user_id"],
        "username": "MemoriaOwner",
        "account_type": "registered",
    }


@pytest.mark.asyncio
async def test_registered_identity_keeps_chat_speaker_and_voice_data_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    speaker_key = Fernet.generate_key().decode("ascii")
    speaker_database = tmp_path / "speakers.sqlite3"
    monkeypatch.setenv("MEMORIA_SPEAKER_DB_PATH", str(speaker_database))
    monkeypatch.setenv("MEMORIA_SPEAKER_TEMPLATE_KEY", speaker_key)
    monkeypatch.setenv("MEMORIA_VOICE_SAMPLE_STORE_PATH", str(tmp_path / "voice-samples"))
    app = create_app()
    app.state.speaker_authority = SpeakerAuthority.sqlite(
        speaker_database,
        template_key=speaker_key,
        adapter=_RestartEmbeddingAdapter(),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        account = (
            await client.post(
                "/v1/auth/register",
                json={"username": "persistent-owner", "password": "safe-passphrase"},
            )
        ).json()
        original_headers = {"Authorization": f"Bearer {account['access_token']}"}
        message = await client.post(
            "/v1/memory/messages",
            headers=original_headers,
            json={
                "user_id": account["user_id"],
                "role": "user",
                "text": "这是需要跨重启保留的聊天。",
            },
        )
        voice_consent = await client.post(
            "/v1/voices/consent",
            headers=original_headers,
            json={"accepted": True, "policy_version": "voice-clone-v1"},
        )
        speaker = await client.post(
            "/v1/speakers/enrollments",
            headers=original_headers,
            json={
                "consent_policy_version": "speaker-biometric-v1",
                "consent_accepted": True,
                "samples": [
                    {
                        "audio_base64": base64.b64encode(sample).decode("ascii"),
                        "sample_rate": 16000,
                    }
                    for sample in (b"owner-01", b"owner-02", b"owner-03")
                ],
            },
        )

    assert message.status_code == 201
    assert voice_consent.status_code == 201
    assert speaker.status_code == 201
    with sqlite3.connect(tmp_path / "memoria.sqlite3") as connection:
        stored_password = connection.execute(
            "SELECT password_hash FROM accounts WHERE user_id = ?",
            (account["user_id"],),
        ).fetchone()[0]
    assert stored_password.startswith("scrypt$")
    assert "safe-passphrase" not in stored_password

    restarted_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=restarted_app),
        base_url="http://test",
    ) as client:
        resumed = await client.get("/v1/auth/me", headers=original_headers)
        logged_in = await client.post(
            "/v1/auth/login",
            json={"username": "persistent-owner", "password": "safe-passphrase"},
        )
        restarted_identity = logged_in.json()
        restarted_headers = {
            "Authorization": f"Bearer {restarted_identity['access_token']}",
        }
        days = await client.get(
            "/v1/memory/days",
            headers=restarted_headers,
            params={"user_id": restarted_identity["user_id"]},
        )
        voices = await client.get("/v1/voices/profiles", headers=restarted_headers)
        speakers = await client.get("/v1/speakers", headers=restarted_headers)

    assert resumed.status_code == 200
    assert logged_in.status_code == 200
    assert restarted_identity["user_id"] == account["user_id"]
    assert days.json()["items"][0]["message_count"] == 1
    assert voices.json()["consent"]["policy_version"] == "voice-clone-v1"
    assert speakers.json()["items"][0]["profile_id"] == speaker.json()["profile_id"]


@pytest.mark.asyncio
async def test_registration_rejects_an_existing_normalized_username(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/v1/auth/register",
            json={"username": "小忆_01", "password": "safe-passphrase"},
        )
        duplicate = await client.post(
            "/v1/auth/register",
            json={"username": "小忆_01", "password": "another-passphrase"},
        )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "用户名已存在，请换一个"}
    assert "access_token" not in duplicate.text


@pytest.mark.asyncio
async def test_registration_upgrades_anonymous_identity_without_losing_its_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = (await client.post("/v1/auth/anonymous")).json()
        anonymous_headers = {
            "Authorization": f"Bearer {anonymous['access_token']}",
        }
        saved = await client.post(
            "/v1/memory/messages",
            headers=anonymous_headers,
            json={
                "user_id": anonymous["user_id"],
                "role": "user",
                "text": "请记住我喜欢雨天散步。",
            },
        )
        upgraded = await client.post(
            "/v1/auth/register",
            headers=anonymous_headers,
            json={"username": "rainwalker", "password": "safe-passphrase"},
        )

        assert saved.status_code == 201
        assert upgraded.status_code == 201
        account = upgraded.json()
        assert account["user_id"] == anonymous["user_id"]

        days = await client.get(
            "/v1/memory/days",
            headers={"Authorization": f"Bearer {account['access_token']}"},
            params={"user_id": account["user_id"]},
        )

    assert days.status_code == 200
    assert days.json()["items"][0]["message_count"] == 1


@pytest.mark.asyncio
async def test_login_uses_one_safe_error_for_unknown_username_and_wrong_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post(
            "/v1/auth/register",
            json={"username": "memorykeeper", "password": "safe-passphrase"},
        )
        unknown = await client.post(
            "/v1/auth/login",
            json={"username": "unknown-user", "password": "safe-passphrase"},
        )
        wrong = await client.post(
            "/v1/auth/login",
            json={"username": "memorykeeper", "password": "wrong-passphrase"},
        )

    assert unknown.status_code == 401
    assert wrong.status_code == 401
    assert unknown.json() == wrong.json() == {"detail": "用户名或密码错误"}


@pytest.mark.asyncio
async def test_deleting_account_immediately_revokes_tokens_login_and_new_sessions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        registered = (
            await client.post(
                "/v1/auth/register",
                json={"username": "deleting-owner", "password": "safe-passphrase"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {registered['access_token']}"}
        app.state.memory_store.begin_account_deletion(
            user_id=registered["user_id"],
            started_at=datetime.now(UTC).isoformat(),
        )

        current = await client.get("/v1/auth/me", headers=headers)
        login = await client.post(
            "/v1/auth/login",
            json={"username": "deleting-owner", "password": "safe-passphrase"},
        )
        session = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"user_id": registered["user_id"], "voice_backend": "cascade"},
        )

    assert current.status_code == 401
    assert login.status_code == 401
    assert session.status_code == 401
