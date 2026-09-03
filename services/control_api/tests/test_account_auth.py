from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import jwt
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from services.control_api.app import wechat_auth
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


async def _register_verified_adult(
    client: AsyncClient,
    app: Any,
    *,
    username: str,
    password: str,
) -> dict[str, object]:
    registered = (
        await client.post(
            "/v1/auth/register",
            json={"username": username, "password": password},
        )
    ).json()
    app.state.memory_store.update_subject_profile(
        user_id=registered["user_id"],
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=datetime.now(UTC).isoformat(),
    )
    logged_in = await client.post(
        "/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert logged_in.status_code == 200
    return logged_in.json()


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


def test_production_miniprogram_requires_wechat_credentials_and_independent_identity_key() -> None:
    common = {
        "ENVIRONMENT": "production",
        "PUBLIC_BASE_URL": "https://voice.example.com",
        "ALLOWED_ORIGINS": "https://voice.example.com",
        "LIVEKIT_URL": "wss://livekit.example.com",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "test-livekit-material-long-enough",
        "MEMORIA_AUTH_SECRET": "test-auth-material-that-is-long-enough",
        "MINIPROGRAM_MEDIA_GATEWAY_URL": "wss://voice.example.com/media",
    }
    with pytest.raises(ValueError, match="WECHAT_MINIPROGRAM_APPID"):
        ControlSettings(_env_file=None, **common).validate_production()
    with pytest.raises(ValueError, match="MEMORIA_WECHAT_IDENTITY_SECRET"):
        ControlSettings(
            _env_file=None,
            **common,
            WECHAT_MINIPROGRAM_APPID="wx-test",
            WECHAT_MINIPROGRAM_APPSECRET="wechat-secret",
        ).validate_production()
    with pytest.raises(ValueError, match="MEMORIA_WECHAT_IDENTITY_SECRET"):
        ControlSettings(
            _env_file=None,
            **common,
            WECHAT_MINIPROGRAM_APPID="wx-test",
            WECHAT_MINIPROGRAM_APPSECRET="wechat-secret",
            MEMORIA_WECHAT_IDENTITY_SECRET=common["MEMORIA_AUTH_SECRET"],
        ).validate_production()


def test_production_device_gateway_requires_wss_and_an_independent_ticket_key() -> None:
    common = {
        "ENVIRONMENT": "production",
        "PUBLIC_BASE_URL": "https://voice.example.com",
        "ALLOWED_ORIGINS": "https://voice.example.com",
        "LIVEKIT_URL": "wss://livekit.example.com",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "test-livekit-material-long-enough",
        "MEMORIA_AUTH_SECRET": "test-auth-material-that-is-long-enough",
    }
    with pytest.raises(ValueError, match="secure WSS"):
        ControlSettings(
            _env_file=None,
            **common,
            DEVICE_MEDIA_GATEWAY_URL="ws://voice.example.com/v1/device/media",
        ).validate_production()
    with pytest.raises(ValueError, match="device gateway ticket secret"):
        ControlSettings(
            _env_file=None,
            **common,
            DEVICE_MEDIA_GATEWAY_URL="wss://voice.example.com/v1/device/media",
        ).validate_production()
    with pytest.raises(ValueError, match="device gateway ticket secret"):
        ControlSettings(
            _env_file=None,
            **common,
            DEVICE_MEDIA_GATEWAY_URL="wss://voice.example.com/v1/device/media",
            MEMORIA_DEVICE_GATEWAY_TICKET_SECRET=common["MEMORIA_AUTH_SECRET"],
        ).validate_production()


@pytest.mark.asyncio
async def test_wechat_phone_exchange_reuses_the_app_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeResponse:
        is_error = False

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            _ = timeout

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            _ = args

        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            _ = kwargs
            calls.append(("GET", url))
            return FakeResponse({"access_token": "cached-token", "expires_in": 7200})

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            _ = kwargs
            calls.append(("POST", url))
            return FakeResponse(
                {
                    "phone_info": {
                        "purePhoneNumber": "18100008880",
                        "countryCode": "86",
                    }
                }
            )

    monkeypatch.setattr(wechat_auth.httpx, "AsyncClient", FakeClient)
    wechat_auth._ACCESS_TOKEN_CACHE.clear()
    settings = ControlSettings(
        _env_file=None,
        OFFLINE_MOCK=False,
        WECHAT_MINIPROGRAM_APPID="wx-test",
        WECHAT_MINIPROGRAM_APPSECRET="wechat-secret",
        WECHAT_ACCESS_TOKEN_ENDPOINT="https://wechat.example/token",
        WECHAT_PHONE_NUMBER_ENDPOINT="https://wechat.example/phone",
    )

    first = await wechat_auth.code_to_phone(settings, "phone-code-1")
    second = await wechat_auth.code_to_phone(settings, "phone-code-2")

    assert first.phone_number == second.phone_number == "18100008880"
    assert calls.count(("GET", "https://wechat.example/token")) == 1
    assert calls.count(("POST", "https://wechat.example/phone")) == 2
    wechat_auth._ACCESS_TOKEN_CACHE.clear()


@pytest.mark.asyncio
async def test_wechat_phone_exchange_refreshes_an_expired_cached_access_token_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    token_responses = iter(("stale-token", "fresh-token"))
    phone_calls = 0

    class FakeResponse:
        is_error = False

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, *, timeout: float) -> None:
            _ = timeout

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            _ = args

        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            _ = kwargs
            calls.append(("GET", url))
            return FakeResponse(
                {"access_token": next(token_responses), "expires_in": 7200}
            )

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            nonlocal phone_calls
            calls.append(("POST", url))
            token = str((kwargs.get("params") or {}).get("access_token"))
            phone_calls += 1
            if token == "stale-token":
                return FakeResponse({"errcode": 42001, "errmsg": "access_token expired"})
            return FakeResponse(
                {
                    "phone_info": {
                        "purePhoneNumber": "18100008880",
                        "countryCode": "86",
                    }
                }
            )

    monkeypatch.setattr(wechat_auth.httpx, "AsyncClient", FakeClient)
    wechat_auth._ACCESS_TOKEN_CACHE.clear()
    settings = ControlSettings(
        _env_file=None,
        OFFLINE_MOCK=False,
        WECHAT_MINIPROGRAM_APPID="wx-test",
        WECHAT_MINIPROGRAM_APPSECRET="wechat-secret",
        WECHAT_ACCESS_TOKEN_ENDPOINT="https://wechat.example/token",
        WECHAT_PHONE_NUMBER_ENDPOINT="https://wechat.example/phone",
    )

    phone = await wechat_auth.code_to_phone(settings, "phone-code")

    assert phone.phone_number == "18100008880"
    assert calls.count(("GET", "https://wechat.example/token")) == 2
    assert phone_calls == 2
    wechat_auth._ACCESS_TOKEN_CACHE.clear()


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
            json={
                "username": "MemoriaOwner",
                "password": "safe-passphrase",
                "display_name": "主人",
            },
        )

        assert registered.status_code == 201
        first = registered.json()
        assert first["username"] == "MemoriaOwner"
        assert first["account_type"] == "registered"
        assert first["access_token"]
        assert first["display_name"] == "主人"

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
        profile = await client.get(
            f"/v1/memory/profile/{first['user_id']}",
            headers={"Authorization": f"Bearer {second['access_token']}"},
        )

    assert current.status_code == 200
    assert current.json() == {
        "user_id": first["user_id"],
        "username": "MemoriaOwner",
        "account_type": "registered",
    }
    assert profile.status_code == 200
    assert profile.json()["display_name"] == "主人"


@pytest.mark.asyncio
async def test_wechat_phone_login_creates_one_stable_registered_identity_and_restores_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unknown = await client.post(
            "/v1/auth/wechat-login",
            json={"login_code": "dev-wechat-owner"},
        )
        first = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": "dev-wechat-owner",
                "phone_code": "dev-phone-owner",
                "display_name": "小林",
            },
        )
        restored = await client.post(
            "/v1/auth/wechat-login",
            json={"login_code": "dev-wechat-owner"},
        )
        current = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {restored.json()['access_token']}"},
        )
        profile = await client.get(
            f"/v1/memory/profile/{restored.json()['user_id']}",
            headers={"Authorization": f"Bearer {restored.json()['access_token']}"},
        )
        subject = app.state.memory_store.get_subject_profile(user_id=first.json()["user_id"])

    assert unknown.status_code == 428
    assert unknown.json()["detail"]["code"] == "phone_authorization_required"
    assert first.status_code == 200
    assert subject is not None
    assert subject["subject_category"] == "adult"
    assert subject["birth_year_band"] == "adult"
    assert subject["age_evidence_status"] == "verified"
    assert first.json()["user_id"].startswith("wx_")
    assert first.json()["account_type"] == "registered"
    assert first.json()["display_name"] == "小林"
    assert first.json()["phone_number_masked"] == "181****8880"
    assert restored.status_code == 200
    assert restored.json()["user_id"] == first.json()["user_id"]
    assert current.json() == {
        "user_id": first.json()["user_id"],
        "username": None,
        "account_type": "registered",
    }
    assert profile.json()["display_name"] == "小林"
    assert profile.json()["phone_number_masked"] == "181****8880"


@pytest.mark.asyncio
async def test_openid_only_legacy_account_must_authorize_phone_before_restore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Silent restore must not skip phone for openid-only legacy accounts."""

    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()
    login_code = "dev-wechat-legacy-openid"
    openid = f"dev-openid-{login_code}"
    now = datetime.now(UTC).isoformat()
    user_id, _ = app.state.memory_store.bind_external_identities(
        preferred_user_id="wx_legacy_openid_only",
        identities={"wechat_openid": wechat_auth.openid_hash(openid)},
        now=now,
    )
    app.state.memory_store.update_external_profile(
        user_id=user_id,
        display_name="旧号",
        phone_number_masked=None,
        now=now,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        blocked = await client.post(
            "/v1/auth/wechat-login",
            json={"login_code": login_code},
        )
        completed = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": login_code,
                "phone_code": "dev-phone-legacy",
                "display_name": "旧号",
            },
        )
        restored = await client.post(
            "/v1/auth/wechat-login",
            json={"login_code": login_code},
        )
        subject = app.state.memory_store.get_subject_profile(user_id=user_id)

    assert blocked.status_code == 428
    assert blocked.json()["detail"]["code"] == "phone_authorization_required"
    assert completed.status_code == 200
    assert completed.json()["user_id"] == user_id
    assert completed.json()["phone_number_masked"] == "181****8880"
    assert restored.status_code == 200
    assert restored.json()["user_id"] == user_id
    assert subject is not None
    assert subject["subject_category"] == "adult"
    assert subject["age_evidence_status"] == "verified"


@pytest.mark.asyncio
async def test_wechat_phone_identity_cannot_be_claimed_by_another_openid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": "dev-wechat-phone-owner",
                "phone_code": "dev-phone-shared",
            },
        )
        attempted_takeover = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": "dev-wechat-phone-attacker",
                "phone_code": "dev-phone-shared",
            },
        )

    assert owner.status_code == 200
    assert attempted_takeover.status_code == 409
    assert attempted_takeover.json()["detail"] == {
        "code": "wechat_identity_conflict"
    }


@pytest.mark.asyncio
async def test_wechat_avatar_upload_persists_profile_image_without_a_local_wx_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "WECHAT_AVATAR_PUBLIC_BASE_URL",
        "https://mini.example.com/memoria-api",
    )
    app = create_app()
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": "dev-wechat-avatar",
                "phone_code": "dev-phone-avatar",
                "display_name": "头像用户",
            },
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        uploaded = await client.post(
            "/v1/auth/wechat-avatar",
            headers=headers,
            json={
                "file_base64": base64.b64encode(png).decode("ascii"),
                "content_type": "image/png",
            },
        )
        avatar_path = urlsplit(uploaded.json()["avatar_url"]).path.removeprefix(
            "/memoria-api"
        )
        downloaded = await client.get(avatar_path)
        profile = await client.get(
            f"/v1/memory/profile/{login.json()['user_id']}",
            headers=headers,
        )

    assert uploaded.status_code == 200
    assert uploaded.json()["size"] == len(png)
    assert uploaded.json()["avatar_url"].startswith(
        "https://mini.example.com/memoria-api/v1/auth/wechat-avatars/"
    )
    assert "wxfile:" not in uploaded.json()["avatar_url"]
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "image/png"
    assert downloaded.headers["cache-control"] == "private, no-store"
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.content == png
    assert profile.json()["avatar_url"] == uploaded.json()["avatar_url"]


@pytest.mark.asyncio
async def test_wechat_account_deletion_requires_a_fresh_matching_login_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": "dev-wechat-delete",
                "phone_code": "dev-phone-delete",
            },
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        wrong = await client.post(
            "/v1/archive/deletion-requests",
            headers=headers,
            json={
                "wechat_login_code": "dev-wechat-someone-else",
                "confirmation": "永久删除我的全部数据",
            },
        )
        deleted = await client.post(
            "/v1/archive/deletion-requests",
            headers=headers,
            json={
                "wechat_login_code": "dev-wechat-delete",
                "confirmation": "永久删除我的全部数据",
            },
        )
        current = await client.get("/v1/auth/me", headers=headers)
        replacement = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": "dev-wechat-delete",
                "phone_code": "dev-phone-delete",
            },
        )
        replacement_current = await client.get(
            "/v1/auth/me",
            headers={"Authorization": f"Bearer {replacement.json()['access_token']}"},
        )

    assert wrong.status_code == 403
    assert deleted.status_code == 200
    assert current.status_code == 401
    assert replacement.status_code == 200
    assert replacement.json()["user_id"] != login.json()["user_id"]
    assert replacement_current.status_code == 200


@pytest.mark.asyncio
async def test_wechat_login_rejects_an_identity_while_account_deletion_is_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post(
            "/v1/auth/wechat-login",
            json={
                "login_code": "dev-wechat-pending-delete",
                "phone_code": "dev-phone-pending-delete",
            },
        )
        user_id = login.json()["user_id"]
        app.state.memory_store.begin_account_deletion(
            user_id=user_id,
            started_at=datetime.now(UTC).isoformat(),
        )

        repeated = await client.post(
            "/v1/auth/wechat-login",
            json={"login_code": "dev-wechat-pending-delete"},
        )

    assert repeated.status_code == 409
    assert repeated.json()["detail"] == {"code": "account_deletion_in_progress"}


def test_echolife_identity_profile_import_is_idempotent_and_keeps_conversations_unfabricated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    source = tmp_path / "echolife-sessions"
    source.mkdir()
    (source / "ECHOLIFE_SESSION_V2_user_wx_legacyhash.json").write_text(
        json.dumps(
            {
                "auth": {
                    "status": "authenticated",
                    "wechatOpenidHash": "aaaaaaaaaaaaaaaaaaaaaaaa",
                    "user": {
                        "id": "wx_aaaaaaaaaaaaaaaaaaaaaaaa",
                        "nickname": "旧用户",
                        "avatarUrl": "",
                        "phoneNumberMasked": "139****5678",
                    },
                },
                "storyFragments": [{"id": "story-1", "text": "旧故事只保留在源数据"}],
                "timeline": [{"id": "timeline-1", "title": "旧时间线"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    target = tmp_path / "memoria.sqlite3"

    from scripts.migrate_echolife_users import migrate_echolife_users

    first = migrate_echolife_users(source=source, target_db=target, dry_run=False)
    second = migrate_echolife_users(source=source, target_db=target, dry_run=False)

    assert first == {
        "files_scanned": 1,
        "users_discovered": 1,
        "users_imported": 1,
        "users_unchanged": 0,
        "conflicts": 0,
        "legacy_content_deferred": 1,
        "avatars_imported": 0,
        "avatars_deferred": 0,
    }
    assert second == {
        "files_scanned": 1,
        "users_discovered": 1,
        "users_imported": 0,
        "users_unchanged": 1,
        "conflicts": 0,
        "legacy_content_deferred": 1,
        "avatars_imported": 0,
        "avatars_deferred": 0,
    }
    backups = list(tmp_path.glob("memoria.sqlite3.pre-echolife-*.bak"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target) as connection:
        profile = connection.execute(
            "SELECT display_name, phone_number_masked FROM profiles WHERE user_id = ?",
            ("wx_aaaaaaaaaaaaaaaaaaaaaaaa",),
        ).fetchone()
        messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()
        summaries = connection.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()
    assert profile == ("旧用户", "139****5678")
    assert messages == (0,)
    assert summaries == (0,)


def test_echolife_sqlite_import_reads_real_store_and_copies_server_avatar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    source = tmp_path / "echolife.sqlite"
    avatar_root = tmp_path / "avatars"
    avatar_root.mkdir()
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )
    (avatar_root / "avatar-legacy.png").write_bytes(png)
    payload = {
        "auth": {
            "status": "authenticated",
            "wechatOpenidHash": "bbbbbbbbbbbbbbbbbbbbbbbb",
            "user": {
                "id": "wx_bbbbbbbbbbbbbbbbbbbbbbbb",
                "nickname": "SQLite 旧用户",
                "avatarUrl": "https://old.example/profile/avatars/avatar-legacy.png",
                "phoneNumberMasked": "139****5678",
            },
        },
        "storyFragments": [{"id": "legacy-story"}],
    }
    with sqlite3.connect(source) as connection:
        connection.executescript(
            """
            CREATE TABLE app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE users (
                id TEXT PRIMARY KEY,
                open_id TEXT,
                nickname TEXT NOT NULL,
                avatar_url TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE story_fragments (id TEXT PRIMARY KEY);
            """
        )
        connection.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
            ("ECHOLIFE_SESSION_V2", json.dumps(payload, ensure_ascii=False), 1),
        )
        connection.execute(
            "INSERT INTO users (id, open_id, nickname, avatar_url) VALUES (?, ?, ?, ?)",
            (
                "wx_bbbbbbbbbbbbbbbbbbbbbbbb",
                "",
                "SQLite 旧用户",
                "https://old.example/profile/avatars/avatar-legacy.png",
            ),
        )
        connection.execute("INSERT INTO story_fragments (id) VALUES (?)", ("legacy-story",))
    target = tmp_path / "memoria.sqlite3"

    from scripts.migrate_echolife_users import migrate_echolife_users

    first = migrate_echolife_users(
        source=source,
        target_db=target,
        dry_run=False,
        avatar_root=avatar_root,
        public_base_url="https://aigcnice.com:8443/memoria-api",
    )
    second = migrate_echolife_users(
        source=source,
        target_db=target,
        dry_run=False,
        avatar_root=avatar_root,
        public_base_url="https://aigcnice.com:8443/memoria-api",
    )

    assert first == {
        "files_scanned": 1,
        "users_discovered": 1,
        "users_imported": 1,
        "users_unchanged": 0,
        "conflicts": 0,
        "legacy_content_deferred": 2,
        "avatars_imported": 1,
        "avatars_deferred": 0,
    }
    assert second == {
        "files_scanned": 1,
        "users_discovered": 1,
        "users_imported": 0,
        "users_unchanged": 1,
        "conflicts": 0,
        "legacy_content_deferred": 2,
        "avatars_imported": 0,
        "avatars_deferred": 0,
    }
    with sqlite3.connect(target) as connection:
        profile = connection.execute(
            "SELECT display_name, phone_number_masked, avatar_url FROM profiles WHERE user_id = ?",
            ("wx_bbbbbbbbbbbbbbbbbbbbbbbb",),
        ).fetchone()
        avatar = connection.execute(
            "SELECT content_type, content, sha256 FROM profile_avatars WHERE user_id = ?",
            ("wx_bbbbbbbbbbbbbbbbbbbbbbbb",),
        ).fetchone()
    assert profile is not None
    assert profile[:2] == ("SQLite 旧用户", "139****5678")
    assert str(profile[2]).startswith(
        "https://aigcnice.com:8443/memoria-api/v1/auth/wechat-avatars/"
    )
    assert avatar == ("image/png", png, hashlib.sha256(png).hexdigest())


def test_echolife_import_does_not_resurrect_a_deleted_wechat_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_test_app(monkeypatch, tmp_path)
    source = tmp_path / "echolife"
    source.mkdir()
    subject_hash = "cccccccccccccccccccccccc"
    (source / "session.json").write_text(
        json.dumps(
            {
                "auth": {
                    "status": "authenticated",
                    "wechatOpenidHash": subject_hash,
                    "user": {
                        "id": f"wx_{subject_hash}",
                        "nickname": "已注销用户",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    target = tmp_path / "memoria.sqlite3"

    from scripts.migrate_echolife_users import migrate_echolife_users
    from services.control_api.app.database import MemoryStore

    store = MemoryStore(str(target))
    store.initialize()
    store.begin_account_deletion(
        user_id=f"wx_{subject_hash}",
        started_at=datetime.now(UTC).isoformat(),
    )
    result = migrate_echolife_users(source=source, target_db=target, dry_run=False)

    assert result == {
        "files_scanned": 1,
        "users_discovered": 1,
        "users_imported": 0,
        "users_unchanged": 0,
        "conflicts": 1,
        "legacy_content_deferred": 0,
        "avatars_imported": 0,
        "avatars_deferred": 0,
    }
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM external_identities").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM profiles WHERE user_id = ?",
            (f"wx_{subject_hash}",),
        ).fetchone() == (0,)


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
        account = await _register_verified_adult(
            client,
            app,
            username="persistent-owner",
            password="safe-passphrase",
        )
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
