from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from services.control_api.app.database import MemoryStore
from services.control_api.app.main import create_app
from services.control_api.app.routes import memory as memory_routes
from services.control_api.app.routes.memory import DailySummaryContent
from services.control_api.app.security import mint_memoria_access_token


def _today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _configure_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "nested" / "memoria.sqlite3"
    monkeypatch.setenv("MEMORIA_DB_PATH", str(path))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("LLM_PROVIDER", "qwen")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    return path


async def _anonymous_identity(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200
    body = response.json()
    return str(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


@pytest.mark.asyncio
async def test_messages_summary_and_profile_persist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = _configure_database(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        user_message = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b00",
                "role": "user",
                "text": "今天完成了产品原型，我很开心。",
                "emotion": "happy",
            },
        )
        assistant_message = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b01",
                "role": "assistant",
                "text": "太棒了，可以记录下最满意的部分。",
            },
        )
        assert user_message.status_code == 201
        assert assistant_message.status_code == 201
        assert user_message.json()["local_date"] == _today()
        initial_profile = await client.get(f"/v1/memory/profile/{user_id}", headers=headers)
        assert initial_profile.json()["reject_non_owner_voice"] is True

        generated = await client.post(
            f"/v1/memory/days/{_today()}/summary",
            headers=headers,
            json={"user_id": user_id},
        )
        assert generated.status_code == 200
        generated_body = generated.json()
        assert generated_body["source"] == "fallback"
        assert generated_body["message_count"] == 2

        updated = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={
                "display_name": "小忆",
                "bio": "喜欢做产品",
                "timezone": "Asia/Shanghai",
                "auto_summary": False,
                "voice_reply": False,
                "gentle_reminders": True,
                "reject_non_owner_voice": False,
                "companion_id": "mianmian",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["display_name"] == "小忆"

    assert database_path.is_file()
    persisted_app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=persisted_app), base_url="http://test"
    ) as client:
        days = await client.get(
            "/v1/memory/days",
            headers=headers,
            params={"user_id": user_id},
        )
        profile = await client.get(f"/v1/memory/profile/{user_id}", headers=headers)
    assert days.status_code == 200
    assert days.json()["items"] == [generated_body]
    assert profile.status_code == 200
    assert profile.json()["auto_summary"] is False
    assert profile.json()["voice_reply"] is False
    assert profile.json()["gentle_reminders"] is True
    assert profile.json()["reject_non_owner_voice"] is False
    assert profile.json()["companion_id"] == "mianmian"
    exported = persisted_app.state.memory_store.export_account_data(user_id=user_id)
    assert exported["profile"]["reject_non_owner_voice"] is False


@pytest.mark.asyncio
async def test_messages_are_idempotent_per_client_message_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    app = create_app()
    client_message_id = "6c83b852-8c91-4e5e-91df-6fd4cb4fe7a8"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        payload = {
            "user_id": user_id,
            "client_message_id": client_message_id,
            "role": "user",
            "text": "  同一条消息  ",
            "emotion": "  happy  ",
        }
        created = await client.post("/v1/memory/messages", headers=headers, json=payload)
        retried = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={**payload, "text": "同一条消息", "emotion": "happy"},
        )
        conflicting = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={**payload, "text": "不同的消息"},
        )

    assert created.status_code == 201
    assert retried.status_code == 200
    assert retried.json() == created.json()
    assert conflicting.status_code == 409
    with sqlite3.connect(tmp_path / "nested" / "memoria.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_idempotency_rejects_different_sensitive_requests_that_redact_identically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    app = create_app()
    client_message_id = "9c39b09c-d5c7-4ab9-9d8d-dbd8b42300e8"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        created = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": client_message_id,
                "role": "user",
                "text": "请联系 13812345678",
            },
        )
        conflict = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": client_message_id,
                "role": "user",
                "text": "请联系 13912345678",
            },
        )

    assert created.status_code == 201
    assert conflict.status_code == 409
    assert "fingerprint" not in conflict.text.lower()
    with sqlite3.connect(tmp_path / "nested" / "memoria.sqlite3") as connection:
        fingerprint = connection.execute(
            "SELECT request_fingerprint FROM messages WHERE client_message_id = ?",
            (client_message_id,),
        ).fetchone()[0]
    assert len(fingerprint) == 64
    assert fingerprint not in created.text


@pytest.mark.asyncio
async def test_message_idempotency_survives_auth_secret_rotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET", "message-idempotency-secret-that-is-independent"
    )
    app = create_app()
    client_message_id = "ac3e9c24-3d4e-41a1-ae66-705dbd54aabe"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        payload = {
            "user_id": user_id,
            "client_message_id": client_message_id,
            "role": "user",
            "text": "认证轮换不应改变消息幂等性",
        }
        created = await client.post("/v1/memory/messages", headers=headers, json=payload)
        claims = jwt.decode(
            headers["Authorization"].removeprefix("Bearer "), options={"verify_signature": False}
        )
        app.state.settings.memoria_auth_secret = SecretStr(
            "rotated-auth-secret-that-is-long-enough"
        )
        rotated_access, _ = mint_memoria_access_token(
            app.state.settings,
            user_id=user_id,
            session_id=str(claims["sid"]),
        )
        retried = await client.post(
            "/v1/memory/messages",
            headers={"Authorization": f"Bearer {rotated_access}"},
            json=payload,
        )

    assert created.status_code == 201
    assert retried.status_code == 200
    assert retried.json() == created.json()


@pytest.mark.asyncio
async def test_missing_client_message_id_is_rejected_even_during_legacy_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "MEMORIA_LEGACY_AUTH_COMPAT_UNTIL",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    app = create_app()
    legacy_user_id = "anon-legacy-message-window"
    app.state.memory_store.get_profile(
        user_id=legacy_user_id,
        now=datetime.now(UTC).isoformat(),
    )
    now = int(time.time())
    legacy_access = jwt.encode(
        {
            "iss": "memoria-control-api",
            "aud": "memoria-h5",
            "sub": legacy_user_id,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "typ": "memoria_access",
        },
        "test-auth-material-that-is-long-enough",
        algorithm="HS256",
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        modern_user_id, modern_headers = await _anonymous_identity(client)
        modern = await client.post(
            "/v1/memory/messages",
            headers=modern_headers,
            json={"user_id": modern_user_id, "role": "user", "text": "现代页面必须带 ID"},
        )
        created = await client.post(
            "/v1/memory/messages",
            headers={"Authorization": f"Bearer {legacy_access}"},
            json={"user_id": legacy_user_id, "role": "user", "text": "旧页面兼容消息"},
        )

    assert modern.status_code == 422
    assert created.status_code == 401


@pytest.mark.asyncio
async def test_memory_routes_require_bearer_and_enforce_user_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/v1/memory/days", params={"user_id": "someone"})
        first_user, first_headers = await _anonymous_identity(client)
        second_user, _ = await _anonymous_identity(client)
        cross_user = await client.post(
            "/v1/memory/messages",
            headers=first_headers,
            json={
                "user_id": second_user,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b02",
                "role": "user",
                "text": "hello",
            },
        )
        own = await client.get(
            f"/v1/memory/profile/{first_user}",
            headers=first_headers,
        )
    assert missing.status_code == 401
    assert cross_user.status_code == 403
    assert own.status_code == 200


@pytest.mark.asyncio
async def test_messages_and_profile_are_redacted_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    app = create_app()
    sensitive_text = (
        "手机13812345678，身份证11010519491231002X，银行卡6222021234567890，"
        "邮箱demo@example.com，密钥sk-testonlyabcdefghijkl，"
        "地址北京市朝阳区建国路88号2号楼301室。"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        message = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b03",
                "role": "user",
                "text": sensitive_text,
            },
        )
        profile = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={
                "display_name": "小王13812345678",
                "bio": "联系demo@example.com，住北京市朝阳区建国路88号。",
            },
        )
        unsafe_avatar = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={"avatar_url": "https://example.com/avatar.png?access_token=dummy"},
        )

    assert message.status_code == 201
    stored_text = message.json()["text"]
    for marker in ("[手机号]", "[身份证]", "[银行卡]", "[邮箱]", "[密钥]", "[地址]"):
        assert marker in stored_text
    assert profile.status_code == 200
    assert profile.json()["display_name"] == "小王[手机号]"
    assert "[邮箱]" in profile.json()["bio"]
    assert "[地址]" in profile.json()["bio"]
    assert unsafe_avatar.status_code == 422


@pytest.mark.asyncio
async def test_memory_api_validates_user_and_text_lengths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        blank_user = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": "   ",
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b04",
                "role": "user",
                "text": "hello",
            },
        )
        blank_text = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b05",
                "role": "user",
                "text": "   ",
            },
        )
        long_text = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b06",
                "role": "user",
                "text": "x" * 8001,
            },
        )
        missing_client_message_id = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={"user_id": user_id, "role": "user", "text": "hello"},
        )
        malformed_client_message_id = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "not-a-uuid",
                "role": "user",
                "text": "hello",
            },
        )
        invalid_timezone = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={"timezone": "Mars/Olympus"},
        )
        invalid_companion = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={"companion_id": "unknown-robot"},
        )
    assert blank_user.status_code == 422
    assert blank_text.status_code == 422
    assert long_text.status_code == 422
    assert missing_client_message_id.status_code == 422
    assert malformed_client_message_id.status_code == 422
    assert invalid_timezone.status_code == 422
    assert invalid_companion.status_code == 422


def test_existing_registered_profiles_migrate_to_starlight(tmp_path: Path) -> None:
    path = tmp_path / "legacy-companion.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE profiles (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '朋友',
                bio TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE accounts (
                user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                username_normalized TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO profiles (user_id, created_at, updated_at)
            VALUES ('legacy-owner', '2026-07-19T00:00:00Z', '2026-07-19T00:00:00Z');
            INSERT INTO accounts (
                user_id, username, username_normalized, password_hash,
                created_at, updated_at
            ) VALUES (
                'legacy-owner', 'owner', 'owner', 'hash',
                '2026-07-19T00:00:00Z', '2026-07-19T00:00:00Z'
            );
            """
        )

    store = MemoryStore(str(path))
    store.initialize()

    profile = store.get_profile(user_id="legacy-owner", now="2026-07-20T00:00:00Z")
    assert profile["companion_id"] == "starlight"


def test_existing_messages_migrate_without_fabricating_request_fingerprints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-messages.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE profiles (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '朋友',
                bio TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                emotion TEXT,
                local_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO profiles (user_id, created_at, updated_at)
            VALUES ('legacy-user', '2026-07-21T00:00:00Z', '2026-07-21T00:00:00Z');
            INSERT INTO messages (user_id, role, text, local_date, created_at)
            VALUES (
                'legacy-user', 'user', '旧消息', '2026-07-21', '2026-07-21T00:00:00Z'
            );
            """
        )

    MemoryStore(str(path)).initialize()

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
        migrated = connection.execute(
            "SELECT client_message_id, request_fingerprint FROM messages"
        ).fetchone()
    assert {"client_message_id", "request_fingerprint"} <= columns
    assert migrated == (None, None)


@pytest.mark.asyncio
async def test_qwen_summary_is_used_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")

    async def fake_summary(*args: object, **kwargs: object) -> DailySummaryContent:
        _ = args, kwargs
        return DailySummaryContent(
            title="项目进展",
            overview="完成了产品原型。",
            highlights=["完成产品原型"],
            mood="positive",
            suggestion="明天验证核心交互。",
        )

    monkeypatch.setattr(memory_routes, "_dashscope_summary", fake_summary)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b07",
                "role": "user",
                "text": "完成了产品原型",
            },
        )
        response = await client.post(
            f"/v1/memory/days/{_today()}/summary",
            headers=headers,
            json={"user_id": user_id},
        )
    assert response.status_code == 200
    assert response.json()["source"] == "qwen"
    assert "test-only-key" not in response.text


@pytest.mark.asyncio
async def test_deepseek_is_only_used_when_explicitly_selected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "residual-qwen-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "selected-deepseek-key")

    async def fake_deepseek(*args: object, **kwargs: object) -> DailySummaryContent:
        _ = args, kwargs
        return DailySummaryContent(
            title="显式选择",
            overview="DeepSeek 被显式选择。",
            highlights=[],
            mood="calm",
            suggestion="继续。",
        )

    async def qwen_must_not_run(*args: object, **kwargs: object) -> DailySummaryContent:
        _ = args, kwargs
        raise AssertionError("Qwen should not run when LLM_PROVIDER=deepseek")

    monkeypatch.setattr(memory_routes, "_deepseek_summary", fake_deepseek)
    monkeypatch.setattr(memory_routes, "_dashscope_summary", qwen_must_not_run)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b08",
                "role": "user",
                "text": "测试显式选择",
            },
        )
        response = await client.post(
            f"/v1/memory/days/{_today()}/summary",
            headers=headers,
            json={"user_id": user_id},
        )
    assert response.status_code == 200
    assert response.json()["source"] == "deepseek"


@pytest.mark.asyncio
async def test_qwen_failure_uses_explicit_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_database(monkeypatch, tmp_path)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only-key")

    async def failed_summary(*args: object, **kwargs: object) -> DailySummaryContent:
        _ = args, kwargs
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(memory_routes, "_dashscope_summary", failed_summary)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b09",
                "role": "user",
                "text": "记录一条消息",
            },
        )
        response = await client.post(
            f"/v1/memory/days/{_today()}/summary",
            headers=headers,
            json={"user_id": user_id},
        )
    assert response.status_code == 200
    assert response.json()["source"] == "fallback"
    assert "本地规则" in response.json()["summary"]["suggestion"]


def test_old_summary_source_constraint_migrates_to_qwen(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE profiles (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '朋友',
                bio TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE daily_summaries (
                user_id TEXT NOT NULL,
                summary_date TEXT NOT NULL,
                content_json TEXT NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('deepseek', 'fallback')),
                message_count INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, summary_date)
            );
            """
        )
    store = MemoryStore(str(path))
    store.initialize()
    stored = store.upsert_summary(
        user_id="legacy-user",
        summary_date="2026-07-15",
        content={"title": "迁移"},
        source="qwen",
        message_count=1,
        generated_at="2026-07-15T00:00:00Z",
    )
    assert stored["source"] == "qwen"


def test_old_voice_sessions_default_to_cascade_backend(tmp_path: Path) -> None:
    path = tmp_path / "legacy-voice.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE profiles (
                user_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '朋友',
                bio TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE voice_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                room_name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            INSERT INTO profiles (user_id, created_at, updated_at)
            VALUES ('legacy-user', '2026-07-15T00:00:00Z', '2026-07-15T00:00:00Z');
            INSERT INTO voice_sessions (session_id, user_id, room_name, created_at)
            VALUES (
                'legacy-session',
                'legacy-user',
                'voice-legacy-session',
                '2026-07-15T00:00:00Z'
            );
            """
        )

    store = MemoryStore(str(path))
    store.initialize()
    stored = store.get_voice_session(
        session_id="legacy-session",
        user_id="legacy-user",
    )
    assert stored is not None
    assert stored["voice_backend"] == "cascade"
    assert stored["interaction_mode"] == "companion"
    assert stored["mode_policy_version"] == "s2-v1"
    assert stored["digital_self_version_id"] is None
    assert stored["relationship_profile_id"] is None
    assert stored["legacy_grant_id"] is None
    assert stored["companion_style_id"] == "starlight"
    assert stored["companion_style_version"] == "companion-v1"
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT omni_sdp_exchanges FROM voice_sessions WHERE session_id = ?",
            ("legacy-session",),
        ).fetchone()
    assert row == (0,)
