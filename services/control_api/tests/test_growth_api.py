from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.control_api.app.main import create_app


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "test-internal-archive-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


async def _registered(client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/v1/auth/register", json={"username": "growth-owner", "password": "safe-password"}
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.asyncio
async def test_growth_tasks_replay_transitions_cas_and_duplicate_event_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _registered(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        created = await client.post(
            "/v1/growth/tasks",
            headers=headers,
            json={"event_id": "growth-create", "kind": "natural_chat"},
        )
        duplicate = await client.post(
            "/v1/growth/tasks",
            headers=headers,
            json={"event_id": "growth-create", "kind": "natural_chat"},
        )
        task_id = created.json()["task_id"]
        active = await client.post(
            f"/v1/growth/tasks/{task_id}/transitions",
            headers=headers,
            json={"event_id": "growth-active", "to_status": "active", "expected_revision": 0},
        )
        stale = await client.post(
            f"/v1/growth/tasks/{task_id}/transitions",
            headers=headers,
            json={"event_id": "growth-stale", "to_status": "paused", "expected_revision": 0},
        )
        response = await client.post(
            f"/v1/growth/tasks/{task_id}/responses",
            headers=headers,
            json={"event_id": "growth-response", "expected_revision": 1, "answer": ""},
        )
        listed = await client.get("/v1/growth/tasks", headers=headers)
        overview = await client.get("/v1/growth/overview", headers=headers)
        invalid = await client.post(
            f"/v1/growth/tasks/{task_id}/transitions",
            headers=headers,
            json={"event_id": "growth-invalid", "expected_revision": 2, "to_status": "active"},
        )
        invalid_event = await app.state.life_archive.event(
            account_id=owner["user_id"], event_id="growth-invalid"
        )

    assert created.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["task_id"] == task_id
    assert active.json()["revision"] == 1
    assert stale.json()["detail"] == {"code": "revision_conflict"}
    assert response.json()["revision"] == 2
    assert invalid.json()["detail"] == {"code": "invalid_transition"}
    assert invalid_event is None
    assert listed.json()["items"][0]["task_id"] == task_id
    assert "percentage" not in str(overview.json())
    assert {item["key"] for item in overview.json()["dimensions"]} == {
        "life_chapters", "important_people", "expression", "decision_cases",
        "relationship_models", "voice", "legacy",
    }


@pytest.mark.asyncio
async def test_growth_task_concurrency_and_event_payload_idempotency(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _registered(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        created = await client.post(
            "/v1/growth/tasks",
            headers=headers,
            json={"event_id": "concurrent-task", "kind": "life_interview"},
        )
        task_id = created.json()["task_id"]
        await client.post(
            f"/v1/growth/tasks/{task_id}/transitions",
            headers=headers,
            json={"event_id": "concurrent-active", "to_status": "active", "expected_revision": 0},
        )
        first, second = await asyncio.gather(
            client.post(
                f"/v1/growth/tasks/{task_id}/transitions",
                headers=headers,
                json={"event_id": "concurrent-paused", "to_status": "paused", "expected_revision": 1},
            ),
            client.post(
                f"/v1/growth/tasks/{task_id}/transitions",
                headers=headers,
                json={"event_id": "concurrent-completed", "to_status": "completed", "expected_revision": 1},
            ),
        )
        conflicting_create = await client.post(
            "/v1/growth/tasks",
            headers=headers,
            json={"event_id": "concurrent-task", "kind": "scenario_choice"},
        )
        feedback = {
            "event_id": "feedback-idempotent",
            "action": "not_me",
            "target_kind": "source_event",
            "target_id": "concurrent-task",
        }
        feedback_first = await client.post("/v1/growth/owner-actions", headers=headers, json=feedback)
        feedback_duplicate = await client.post(
            "/v1/growth/owner-actions", headers=headers, json=feedback
        )
        feedback_conflict = await client.post(
            "/v1/growth/owner-actions",
            headers=headers,
            json={**feedback, "action": "would_not_say"},
        )

    assert sorted((first.status_code, second.status_code)) == [200, 409]
    assert conflicting_create.status_code == 409
    assert (feedback_first.status_code, feedback_duplicate.status_code) == (201, 200)
    assert feedback_conflict.status_code == 409


@pytest.mark.asyncio
async def test_natural_chat_task_is_frozen_into_the_voice_session_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _registered(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        created = await client.post(
            "/v1/growth/tasks",
            headers=headers,
            json={"event_id": "natural-create", "kind": "natural_chat"},
        )
        task_id = created.json()["task_id"]
        await client.post(
            f"/v1/growth/tasks/{task_id}/transitions",
            headers=headers,
            json={
                "event_id": "natural-active",
                "to_status": "active",
                "expected_revision": 0,
            },
        )
        session = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"learning_task_id": task_id},
        )
        internal = {"X-Memoria-Internal-Token": "test-internal-archive-token"}
        archived = await client.post(
            "/v1/archive/session-events",
            headers=internal,
            json={
                "event_id": "natural-speech",
                "session_id": session.json()["session_id"],
                "turn_id": 1,
                "generation_id": 1,
                "event_type": "speech.utterance_finalized",
                "occurred_at": datetime.now(UTC).isoformat(),
                "speaker_class": "owner",
                "source": "test",
                "payload": {
                    "text": "今天我想聊聊一件事。",
                    "learning_task_id": "forged-task",
                    "learning_task_kind": "decision_review",
                },
            },
        )
        timeline = await client.get("/v1/archive/timeline", headers=headers)
        inactive = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"learning_task_id": "missing-task"},
        )

    assert session.status_code == 200
    assert session.json()["learning_task_id"] == task_id
    stored = app.state.memory_store.get_voice_session_by_id(
        session_id=session.json()["session_id"]
    )
    assert stored is not None
    assert stored["learning_task_id"] == task_id
    assert archived.status_code == 201
    payload = next(
        item["payload"]
        for item in timeline.json()["items"]
        if item["event_id"] == "natural-speech"
    )
    assert payload["learning_task_id"] == task_id
    assert payload["learning_task_kind"] == "natural_chat"
    assert inactive.status_code == 409
    assert inactive.json()["detail"] == {"code": "learning_task_not_active"}


@pytest.mark.asyncio
async def test_session_binding_and_task_completion_are_serialized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        owner = await _registered(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        created = await client.post(
            "/v1/growth/tasks",
            headers=headers,
            json={"event_id": "serialized-create", "kind": "natural_chat"},
        )
        task_id = created.json()["task_id"]
        await client.post(
            f"/v1/growth/tasks/{task_id}/transitions",
            headers=headers,
            json={
                "event_id": "serialized-active",
                "to_status": "active",
                "expected_revision": 0,
            },
        )

        original_task = app.state.growth_reader.task
        validation_entered = asyncio.Event()
        release_validation = asyncio.Event()
        calls = 0

        async def blocking_task(*, account_id: str, task_id: str):
            nonlocal calls
            calls += 1
            result = await original_task(account_id=account_id, task_id=task_id)
            if calls == 1:
                validation_entered.set()
                await release_validation.wait()
            return result

        monkeypatch.setattr(app.state.growth_reader, "task", blocking_task)
        session_request = asyncio.create_task(
            client.post(
                "/v1/sessions",
                headers=headers,
                json={"learning_task_id": task_id},
            )
        )
        await validation_entered.wait()
        completion_request = asyncio.create_task(
            client.post(
                f"/v1/growth/tasks/{task_id}/transitions",
                headers=headers,
                json={
                    "event_id": "serialized-completed",
                    "to_status": "completed",
                    "expected_revision": 1,
                },
            )
        )
        await asyncio.sleep(0)
        assert completion_request.done() is False
        release_validation.set()
        session, completed = await asyncio.gather(
            session_request,
            completion_request,
        )

    assert session.status_code == 200
    assert session.json()["learning_task_id"] == task_id
    stored = app.state.memory_store.get_voice_session_by_id(
        session_id=session.json()["session_id"]
    )
    assert stored is not None
    assert stored["learning_task_id"] == task_id
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_negative_feedback_vetoes_a_claim_and_retraction_immediately_lowers_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    path = tmp_path / "memoria.sqlite3"
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = await _registered(client)
        headers = {"Authorization": f"Bearer {owner['access_token']}"}
        await app.state.life_archive.record(
            EvidenceEvent(
                event_id="growth-confirmed-source", account_id=owner["user_id"],
                event_type="speech.utterance_finalized", occurred_at=datetime(2026, 7, 22, tzinfo=UTC),
                speaker_class="owner", source="test",
                payload={"text": "我在杭州读过书。", "owner_projection_eligible": True},
            )
        )
        app.state.memory_catalog.initialize()
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                INSERT INTO memory_claims (
                    claim_id, account_id, category, subject_key, predicate, value, confidence,
                    status, sensitive_domain, extractor_version, source_event_id, valid_at, created_at
                ) VALUES ('growth-claim', ?, 'life_story', 'self', 'education', '杭州读书', .9,
                    'confirmed', 'personal', 'test', 'growth-confirmed-source', ?, ?)
                """,
                (owner["user_id"], datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat()),
            )
        before = await client.get("/v1/growth/overview", headers=headers)
        feedback = await client.post(
            "/v1/growth/owner-actions", headers=headers,
            json={"event_id": "growth-not-me", "action": "not_me", "target_kind": "memory_claim", "target_id": "growth-claim"},
        )
        vetoed = await client.get("/v1/growth/overview", headers=headers)
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE memory_claims SET status = 'retracted' WHERE claim_id = 'growth-claim'")
        retracted = await client.get("/v1/growth/overview", headers=headers)

    def life_chapters(response: object) -> dict[str, object]:
        return next(
            item
            for item in response.json()["dimensions"]  # type: ignore[union-attr]
            if item["key"] == "life_chapters"
        )

    assert life_chapters(before)["status"] == "supported"
    assert feedback.status_code == 201
    assert life_chapters(vetoed)["status"] == "conflicted"
    assert life_chapters(retracted)["status"] == "emerging"
