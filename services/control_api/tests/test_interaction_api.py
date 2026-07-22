from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_RELEASE_TAG", "release-test-s2")
    monkeypatch.setenv("READINESS_GATE_TTL_S", "86400")
    monkeypatch.setenv("OFFLINE_MOCK", "true")
    monkeypatch.setenv(
        "MEMORIA_INTERACTION_POLICY_TOKEN", "interaction-policy-token-that-is-long-enough"
    )


async def _identity(client: AsyncClient) -> tuple[str, dict[str, str]]:
    response = await client.post("/v1/auth/anonymous")
    assert response.status_code == 200
    body = response.json()
    return str(body["user_id"]), {"Authorization": f"Bearer {body['access_token']}"}


@pytest.mark.asyncio
async def test_capabilities_expose_explicit_s2_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, headers = await _identity(client)
        response = await client.get("/v1/interaction/capabilities", headers=headers)

    assert response.status_code == 200
    modes = response.json()["modes"]
    assert modes["companion"]["status"] == "available"
    assert modes["archive"] == {"status": "available", "conversational": False}
    assert modes["self_preview"]["status"] == "blocked"
    assert modes["self_preview"]["missing"] == ["self_preview_runtime"]
    assert modes["legacy"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_only_companion_creates_voice_sessions_and_freezes_server_companion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        invalid_binding = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"digital_self_version_id": "untrusted-client-version"},
        )
        blocked = await client.post(
            "/v1/sessions", headers=headers, json={"interaction_mode": "self_preview"}
        )
        archive = await client.post(
            "/v1/sessions", headers=headers, json={"interaction_mode": "archive"}
        )
        created = await client.post(
            "/v1/sessions", headers=headers, json={"interaction_mode": "companion"}
        )

    assert invalid_binding.status_code == 422
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "mode_blocked"
    assert archive.status_code == 409
    assert archive.json()["detail"]["code"] == "mode_not_conversational"
    assert len(app.state.memory_store.list_voice_sessions(user_id=user_id)) == 1
    session = app.state.memory_store.get_voice_session_by_id(
        session_id=created.json()["session_id"]
    )
    assert session is not None
    assert session["interaction_mode"] == "companion"
    assert session["mode_policy_version"] == "s2-v1"
    assert session["companion_style_id"] == "starlight"
    assert session["digital_self_version_id"] is None
    assert session["relationship_profile_id"] is None
    assert session["legacy_grant_id"] is None


@pytest.mark.asyncio
async def test_companion_profile_change_only_applies_to_future_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        first = (await client.post("/v1/sessions", headers=headers, json={})).json()
        changed = await client.put(
            f"/v1/memory/profile/{user_id}",
            headers=headers,
            json={"companion_id": "xuanmo"},
        )
        second = (await client.post("/v1/sessions", headers=headers, json={})).json()

    assert changed.status_code == 200
    first_frozen = app.state.memory_store.get_voice_session_by_id(session_id=first["session_id"])
    second_frozen = app.state.memory_store.get_voice_session_by_id(session_id=second["session_id"])
    assert first_frozen is not None and second_frozen is not None
    assert first_frozen["companion_style_id"] == "starlight"
    assert second_frozen["companion_style_id"] == "xuanmo"


@pytest.mark.asyncio
async def test_internal_policy_uses_its_own_capability_and_returns_frozen_session_ceiling(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        _, headers = await _identity(client)
        session_id = (await client.post("/v1/sessions", headers=headers, json={})).json()[
            "session_id"
        ]
        denied = await client.post(
            "/v1/interaction/session-policy",
            json={"session_id": session_id, "speaker_class": "guest"},
        )
        response = await client.post(
            "/v1/interaction/session-policy",
            headers={"X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough"},
            json={"session_id": session_id, "speaker_class": "guest"},
        )

    assert denied.status_code == 401
    assert response.status_code == 200
    policy = response.json()
    assert policy["interaction_mode"] == "companion"
    assert policy["policy_scope"] == "session"
    assert policy["simulated_output"] is False
    assert policy["history_eligible"] is True
    assert policy["owner_projection_eligible"] is True
    assert policy["capabilities"]["private_memory"] is True
    assert policy["capabilities"]["persona"] is True
    assert policy["capabilities"]["persona_low_sensitivity"] is True
    assert policy["capabilities"]["tools"] is True
    assert policy["capabilities"]["history"] is True
    assert policy["capabilities"]["learning"] is True
    assert policy["capabilities"]["voice_profile"] is True
    assert policy["companion_style_id"] == "starlight"
    assert policy["companion_style_version"] == "companion-v1"
    assert policy["digital_self_version_id"] is None
    assert policy["relationship_profile_id"] is None
    assert policy["legacy_grant_id"] is None
