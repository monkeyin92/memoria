from __future__ import annotations

import base64
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from services.control_api.app.config import ControlSettings
from services.control_api.app.main import create_app
from services.control_api.app.routes import auth as auth_routes
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


def test_legacy_auth_compat_cutoff_is_absolute_utc_and_closes_at_the_boundary() -> None:
    cutoff = datetime.now(UTC) + timedelta(hours=1)
    settings = ControlSettings(
        _env_file=None,
        MEMORIA_LEGACY_AUTH_COMPAT_UNTIL=cutoff.isoformat(),
    )

    assert settings.legacy_auth_compat_active(now=cutoff - timedelta(microseconds=1))
    assert not settings.legacy_auth_compat_active(now=cutoff)
    assert ControlSettings(
        _env_file=None,
        MEMORIA_LEGACY_AUTH_COMPAT_UNTIL="",
    ).legacy_auth_compat_until is None
    with pytest.raises(ValidationError, match="absolute UTC"):
        ControlSettings(
            _env_file=None,
            MEMORIA_LEGACY_AUTH_COMPAT_UNTIL="2026-07-22T20:00:00+08:00",
        )
    expired = ControlSettings(
        _env_file=None,
        MEMORIA_LEGACY_AUTH_COMPAT_UNTIL=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )
    assert not expired.legacy_auth_compat_active()
    with pytest.raises(ValidationError, match="24 hours"):
        ControlSettings(
            _env_file=None,
            MEMORIA_LEGACY_AUTH_COMPAT_UNTIL=(datetime.now(UTC) + timedelta(hours=24, seconds=1)).isoformat(),
        )
    with pytest.raises(ValidationError):
        ControlSettings(_env_file=None, MEMORIA_AUTH_TOKEN_TTL_S=599)
    with pytest.raises(ValidationError):
        ControlSettings(_env_file=None, MEMORIA_AUTH_TOKEN_TTL_S=901)


@pytest.mark.asyncio
async def test_concurrent_refresh_returns_retryable_conflict_without_revoking_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        issued = await client.post("/v1/auth/anonymous")
        access = issued.json()["access_token"]
        claims = jwt.decode(access, options={"verify_signature": False})
        first_refresh = client.cookies.get("memoria_refresh")

        assert issued.status_code == 200
        assert isinstance(claims.get("sid"), str)
        assert isinstance(claims.get("jti"), str)
        assert first_refresh
        assert "refresh_token" not in issued.text

        rotated = await client.post("/v1/auth/refresh")
        rotated_access = rotated.json()["access_token"]
        concurrent = await client.post(
            "/v1/auth/refresh",
            cookies={"memoria_refresh": first_refresh},
        )
        current = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {rotated_access}"},
        )

    assert rotated.status_code == 200
    assert concurrent.status_code == 409
    assert concurrent.headers["retry-after"] == "1"
    assert "set-cookie" not in concurrent.headers
    assert current.status_code == 200


@pytest.mark.asyncio
async def test_refresh_replay_after_grace_revokes_the_rotated_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        issued = await client.post("/v1/auth/anonymous")
        first_refresh = client.cookies.get("memoria_refresh")
        rotated = await client.post("/v1/auth/refresh")
        rotated_access = rotated.json()["access_token"]
        replayed_at = datetime.now(UTC) + timedelta(seconds=6)
        monkeypatch.setattr(auth_routes, "_utc_now", lambda: replayed_at.isoformat())

        replay = await client.post(
            "/v1/auth/refresh",
            cookies={"memoria_refresh": first_refresh},
        )
        revoked = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {rotated_access}"},
        )

    assert issued.status_code == 200
    assert rotated.status_code == 200
    assert replay.status_code == 401
    assert revoked.status_code == 401


@pytest.mark.asyncio
async def test_legacy_sidless_access_is_rejected_when_compat_window_is_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()
    user_id = "anon-legacy-upgrade"
    app.state.memory_store.get_profile(user_id=user_id, now=datetime.now(UTC).isoformat())
    now = int(time.time())
    legacy = jwt.encode(
        {
            "iss": "memoria-control-api",
            "aud": "memoria-h5",
            "sub": user_id,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "typ": "memoria_access",
        },
        "test-auth-material-that-is-long-enough",
        algorithm="HS256",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        normal = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {legacy}"})
        upgraded = await client.post(
            "/v1/auth/upgrade",
            headers={"Authorization": f"Bearer {legacy}"},
        )

    assert normal.status_code == 401
    assert upgraded.status_code == 401


@pytest.mark.asyncio
async def test_legacy_sidless_access_can_only_use_upgrade_during_compat_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_LEGACY_AUTH_COMPAT_UNTIL",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    app = create_app()
    user_id = "anon-legacy-window"
    app.state.memory_store.get_profile(user_id=user_id, now=datetime.now(UTC).isoformat())
    now = int(time.time())
    legacy = jwt.encode(
        {
            "iss": "memoria-control-api",
            "aud": "memoria-h5",
            "sub": user_id,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "typ": "memoria_access",
        },
        "test-auth-material-that-is-long-enough",
        algorithm="HS256",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        current = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {legacy}"},
        )
        upgraded = await client.post(
            "/v1/auth/upgrade",
            headers={"Authorization": f"Bearer {legacy}"},
        )
        replaced = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {legacy}"},
        )
        repeated = await client.post(
            "/v1/auth/upgrade",
            headers={"Authorization": f"Bearer {legacy}"},
        )

    assert current.status_code == 401
    assert upgraded.status_code == 200
    assert replaced.status_code == 401
    assert repeated.status_code == 200


@pytest.mark.asyncio
async def test_legacy_upgrade_recovers_once_after_response_loss_then_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_LEGACY_AUTH_COMPAT_UNTIL",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    app = create_app()
    user_id = "anon-legacy-recovery"
    app.state.memory_store.get_profile(user_id=user_id, now=datetime.now(UTC).isoformat())
    now = int(time.time())
    legacy = jwt.encode(
        {
            "iss": "memoria-control-api",
            "aud": "memoria-h5",
            "sub": user_id,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "typ": "memoria_access",
        },
        "test-auth-material-that-is-long-enough",
        algorithm="HS256",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/v1/auth/upgrade", headers={"Authorization": f"Bearer {legacy}"})
        retry = await client.post("/v1/auth/upgrade", headers={"Authorization": f"Bearer {legacy}"})
        old_access = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {legacy}"})
        recovered_access = await client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {retry.json()['access_token']}"}
        )
        app.state.settings.legacy_auth_compat_until = datetime.now(UTC) - timedelta(seconds=1)
        expired = await client.post("/v1/auth/upgrade", headers={"Authorization": f"Bearer {legacy}"})

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()["user_id"] == first.json()["user_id"]
    assert old_access.status_code == 401
    assert recovered_access.status_code == 200
    assert expired.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_current_access_session_and_clears_refresh_cookie(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        issued = await client.post("/v1/auth/anonymous")
        access = issued.json()["access_token"]
        logged_out = await client.post(
            "/v1/auth/logout",
            headers={"Authorization": f"Bearer {access}"},
        )
        denied = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {access}"})

    assert logged_out.status_code == 204
    assert "memoria_refresh=" in logged_out.headers["set-cookie"]
    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_production_refresh_cookie_is_http_only_strict_and_secure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://aigcnice.com:8443/memoria-api")
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        issued = await client.post("/v1/auth/anonymous")

    cookie = issued.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "secure" in cookie
    assert "path=/memoria-api/v1/auth" in cookie


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
                "client_message_id": "11111111-1111-4111-8111-111111111111",
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
                "client_message_id": "22222222-2222-4222-8222-222222222222",
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
async def test_registration_replaces_the_anonymous_auth_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        anonymous = await client.post("/v1/auth/anonymous")
        anonymous_access = anonymous.json()["access_token"]
        anonymous_refresh = client.cookies.get("memoria_refresh")
        registered = await client.post(
            "/v1/auth/register",
            headers={"Authorization": f"Bearer {anonymous_access}"},
            json={"username": "session-owner", "password": "safe-passphrase"},
        )
        registered_access = registered.json()["access_token"]

        old_access = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {anonymous_access}"},
        )
        old_refresh = await client.post(
            "/v1/auth/refresh",
            cookies={"memoria_refresh": anonymous_refresh},
        )
        current_access = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {registered_access}"},
        )

    assert registered.status_code == 201
    assert old_access.status_code == 401
    assert old_refresh.status_code == 401
    assert current_access.status_code == 200


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
