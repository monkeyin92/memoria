from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.guardian.domain import ConsentRecord


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "archive-internal-token")
    monkeypatch.setenv("OFFLINE_MOCK", "true")


@pytest.mark.asyncio
async def test_tutor_catalog_practice_and_rebuildable_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "adult-tutor", "password": "safe-password"},
            )
        ).json()
        auth = {"Authorization": f"Bearer {identity['access_token']}"}
        english = await client.get("/v1/tutor/lessons", headers=auth)
        homework = await client.get(
            "/v1/tutor/lessons",
            params={"focus": "tutor_homework"},
            headers=auth,
        )
        created = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "practice-create-001"},
            json={"focus": "tutor_english", "task_id": "english-past-story"},
        )
        session_id = created.json()["session_id"]
        activated = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-active-001"},
            json={"action": "active", "expected_revision": 0},
        )
        practiced = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-turn-001"},
            json={
                "action": "practice",
                "expected_revision": 1,
                "duration_seconds": 600,
                "outcome": "struggled",
                "skill_key": "past-tense",
            },
        )
        replay = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-turn-001"},
            json={
                "action": "practice",
                "expected_revision": 1,
                "duration_seconds": 600,
                "outcome": "struggled",
                "skill_key": "past-tense",
            },
        )
        completed = await client.post(
            f"/v1/tutor/practice-sessions/{session_id}/events",
            headers={**auth, "Idempotency-Key": "practice-complete-001"},
            json={"action": "completed", "expected_revision": 2},
        )
        progress = await client.get("/v1/tutor/progress", headers=auth)

    assert english.status_code == 200
    assert english.json()["count"] == 50
    assert homework.json()["count"] == 1
    assert created.status_code == 201
    assert activated.json()["revision"] == 1
    assert practiced.json()["revision"] == 2
    assert replay.json()["revision"] == 2
    assert completed.json()["status"] == "completed"
    assert progress.json() == {
        "practiced_seconds": 600,
        "study_minutes": 10,
        "active_days": [datetime.now(UTC).date().isoformat()],
        "current_streak_days": 1,
        "weak_points": [{"skill_key": "past-tense", "weight": 1}],
        "mastered_skills": [],
        "source_event_count": 1,
        "last_practiced_at": progress.json()["last_practiced_at"],
        "rebuildable": True,
    }
    stored = await app.state.tutor_store.study_progress(account_id=identity["user_id"])
    assert stored is not None
    assert stored.source_event_ids
    exported = await app.state.guardian_store.export_for_account(
        account_id=identity["user_id"]
    )
    assert len(exported["tutor_practice_sessions"]) == 1
    assert exported["tutor_study_progress"] is not None


@pytest.mark.asyncio
async def test_minor_tutor_progress_requires_memory_retention_consent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class ConsentView:
        def __init__(self) -> None:
            self.allowed = False

        async def active_consent(self, **_: Any) -> ConsentRecord | None:
            if not self.allowed:
                return None
            return ConsentRecord(
                consent_id="00000000-0000-0000-0000-000000000001",
                link_id="00000000-0000-0000-0000-000000000002",
                consent_kind="memory_retention",
                policy_version="minor-memory-v1",
                granted_at=datetime.now(UTC),
                evidence_event_id="minor-memory-consent",
            )

    _configure(monkeypatch, tmp_path)
    app = create_app()
    consents = ConsentView()
    app.state.guardian_store = consents
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "minor-tutor", "password": "safe-password"},
            )
        ).json()
        app.state.memory_store.update_subject_profile(
            user_id=identity["user_id"],
            subject_category="minor",
            birth_year_band="14_to_17",
            now=datetime.now(UTC).isoformat(),
        )
        refreshed = (
            await client.post(
                "/v1/auth/login",
                json={"username": "minor-tutor", "password": "safe-password"},
            )
        ).json()
        auth = {"Authorization": f"Bearer {refreshed['access_token']}"}
        lessons = await client.get("/v1/tutor/lessons", headers=auth)
        denied = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "minor-practice-001"},
            json={"focus": "tutor_homework", "task_id": "homework-self-guided"},
        )
        consents.allowed = True
        allowed = await client.post(
            "/v1/tutor/practice-sessions",
            headers={**auth, "Idempotency-Key": "minor-practice-001"},
            json={"focus": "tutor_homework", "task_id": "homework-self-guided"},
        )

    assert lessons.status_code == 200
    assert denied.status_code == 403
    assert denied.json()["detail"] == {
        "code": "guardian_consent_required",
        "capability": "memory_retention",
    }
    assert allowed.status_code == 201
