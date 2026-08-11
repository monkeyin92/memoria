from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app

_LEGACY_MEMORY_WRITE_UNAVAILABLE_DETAIL = (
    "legacy memory writes are unavailable in production"
)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, production: bool) -> Path:
    path = tmp_path / "nested" / "memoria.sqlite3"
    monkeypatch.setenv("MEMORIA_DB_PATH", str(path))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv("LLM_PROVIDER", "bailian_deepseek")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    if production:
        monkeypatch.setenv("ENVIRONMENT", "production")
    else:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    return path


async def _anonymous_identity(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200
    body = response.json()
    return str(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


def _utc_today() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


@pytest.mark.asyncio
async def test_production_blocks_legacy_message_writes_without_touching_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, production=True)
    app = create_app()
    calls: list[dict[str, Any]] = []

    def unexpected_add_message(**kwargs: Any) -> tuple[dict[str, Any], bool]:
        calls.append(kwargs)
        raise AssertionError("legacy message write reached the store in production")

    monkeypatch.setattr(app.state.memory_store, "add_message", unexpected_add_message)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        response = await client.post(
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

    assert response.status_code == 503
    assert response.json()["detail"] == _LEGACY_MEMORY_WRITE_UNAVAILABLE_DETAIL
    assert calls == []


@pytest.mark.asyncio
async def test_production_blocks_legacy_summary_writes_without_touching_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, production=True)
    app = create_app()
    calls: list[tuple[str, dict[str, Any]]] = []

    def unexpected_list_messages(**kwargs: Any) -> list[dict[str, Any]]:
        calls.append(("list_messages", kwargs))
        raise AssertionError("legacy summary read reached the store in production")

    def unexpected_upsert_summary(**kwargs: Any) -> dict[str, Any]:
        calls.append(("upsert_summary", kwargs))
        raise AssertionError("legacy summary write reached the store in production")

    monkeypatch.setattr(app.state.memory_store, "list_messages", unexpected_list_messages)
    monkeypatch.setattr(app.state.memory_store, "upsert_summary", unexpected_upsert_summary)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        response = await client.post(
            f"/v1/memory/days/{_utc_today()}/summary",
            headers=headers,
            json={"user_id": user_id},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == _LEGACY_MEMORY_WRITE_UNAVAILABLE_DETAIL
    assert calls == []


@pytest.mark.asyncio
async def test_production_profile_preference_updates_still_hit_legacy_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path, production=True)
    app = create_app()
    calls: list[dict[str, Any]] = []

    def legacy_update_profile(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "user_id": kwargs["user_id"],
            "display_name": kwargs["values"].get("display_name", "朋友"),
            "bio": kwargs["values"].get("bio", ""),
            "avatar_url": kwargs["values"].get("avatar_url", ""),
            "phone_number_masked": "",
            "companion_id": kwargs["values"].get("companion_id"),
            "timezone": kwargs["values"].get("timezone", "Asia/Shanghai"),
            "auto_summary": kwargs["values"].get("auto_summary", True),
            "voice_reply": kwargs["values"].get("voice_reply", True),
            "gentle_reminders": kwargs["values"].get("gentle_reminders", False),
            "reject_non_owner_voice": kwargs["values"].get(
                "reject_non_owner_voice",
                True,
            ),
            "subject_category": "unknown",
            "birth_year_band": "unknown",
            "age_evidence_status": "unverified",
            "subject_revision": 1,
            "created_at": "2026-08-10T00:00:00Z",
            "updated_at": "2026-08-10T00:00:00Z",
        }

    monkeypatch.setattr(app.state.memory_store, "update_profile", legacy_update_profile)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        response = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={"auto_summary": False, "voice_reply": False},
        )

    assert response.status_code == 200
    assert calls and calls[0]["user_id"] == user_id
    assert calls[0]["values"]["auto_summary"] is False
    assert calls[0]["values"]["voice_reply"] is False
    assert response.json()["display_name"] == "朋友"
    assert response.json()["auto_summary"] is False
    assert response.json()["voice_reply"] is False


@pytest.mark.asyncio
async def test_legacy_writes_still_work_outside_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_path = _configure(monkeypatch, tmp_path, production=False)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _anonymous_identity(client)
        created = await client.post(
            "/v1/memory/messages",
            headers=headers,
            json={
                "user_id": user_id,
                "client_message_id": "b66e1d57-2e02-4e3c-bc75-ef7058218b11",
                "role": "user",
                "text": "今天完成了产品原型，我很开心。",
                "emotion": "happy",
            },
        )
        summarized = await client.post(
            f"/v1/memory/days/{_utc_today()}/summary",
            headers=headers,
            json={"user_id": user_id},
        )

    assert created.status_code == 201
    assert summarized.status_code == 200
    assert summarized.json()["source"] == "fallback"
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM daily_summaries").fetchone()[0] == 1
