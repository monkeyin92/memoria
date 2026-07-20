from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-internal-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


@pytest.mark.asyncio
async def test_consent_drives_non_blocking_owner_learning_and_session_scoped_capsule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "persona-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        status_before = await client.get("/v1/persona/status", headers=headers)
        consent = await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        status_after = await client.get("/v1/persona/status", headers=headers)
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        for index, text in enumerate(
            (
                "我觉得先把事实弄清楚。",
                "我觉得应该先听完对方。",
                "我觉得答应的事要做到。",
            )
        ):
            recorded = await client.post(
                "/v1/archive/session-events",
                headers=internal,
                json={
                    "event_id": f"persona-api-style-{index}",
                    "session_id": session["session_id"],
                    "event_type": "speech.utterance_finalized",
                    "occurred_at": datetime(2026, 7, 19, 15, index, tzinfo=UTC).isoformat(),
                    "speaker_class": "owner",
                    "source": "test",
                    "payload": {"text": text},
                    "turn_id": index + 1,
                },
            )
            assert recorded.status_code == 201

        traits = await client.get("/v1/persona/traits", headers=headers)
        versions = await client.get("/v1/persona/versions", headers=headers)
        owner_capsule = await client.post(
            "/v1/persona/session-capsule",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "speaker_class": "owner",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )
        guest_capsule = await client.post(
            "/v1/persona/session-capsule",
            headers=internal,
            json={
                "session_id": session["session_id"],
                "speaker_class": "guest",
                "topic": "表达看法",
                "max_chars": 500,
            },
        )
        revoked = await client.delete("/v1/persona/consent", headers=headers)

    assert consent.status_code == 201
    assert status_before.json() == {"learning_allowed": False}
    assert status_after.json() == {"learning_allowed": True}
    assert any(item["status"] == "confirmed" for item in traits.json()["items"])
    assert versions.json()["items"][0]["status"] == "active"
    assert "我觉得" in owner_capsule.json()["prompt_fragment"]
    assert guest_capsule.json()["entries"] == []
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None


@pytest.mark.asyncio
async def test_value_or_decision_trait_requires_authenticated_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "decision-owner", "password": "safe-password"},
            )
        ).json()
        headers = {"Authorization": f"Bearer {identity['access_token']}"}
        await client.post(
            "/v1/persona/consent",
            headers=headers,
            json={"accepted": True, "policy_version": "persona-learning-v1"},
        )
        session = (
            await client.post(
                "/v1/sessions",
                headers=headers,
                json={"user_id": identity["user_id"], "voice_backend": "cascade"},
            )
        ).json()
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "persona-api-decision",
                "session_id": session["session_id"],
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": {"text": "做重大决定时，我习惯先列事实，再睡一晚。"},
                "turn_id": 1,
            },
        )
        traits = (await client.get("/v1/persona/traits", headers=headers)).json()["items"]
        decision = next(item for item in traits if item["category"] == "decision_habit")
        unauthenticated = await client.post(
            f"/v1/persona/traits/{decision['trait_id']}/review",
            json={"action": "confirm"},
        )
        missing_counterexample = await client.post(
            f"/v1/persona/traits/{decision['trait_id']}/review",
            headers=headers,
            json={"action": "confirm"},
        )
        confirmed = await client.post(
            f"/v1/persona/traits/{decision['trait_id']}/review",
            headers=headers,
            json={
                "action": "confirm",
                "counterexample": "紧急安全风险出现时会立即行动。",
            },
        )

    assert decision["status"] == "candidate"
    assert unauthenticated.status_code == 401
    assert missing_counterexample.status_code == 422
    assert missing_counterexample.json()["detail"] == (
        "decision and value traits require a counterexample before confirmation"
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"
    assert confirmed.json()["version_id"]
