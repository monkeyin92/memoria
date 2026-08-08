from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from services.control_api.app.main import create_app
from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport
from services.evolution.store import EvolutionStore


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "archive-internal-token")
    monkeypatch.setenv("MEMORIA_EVOLUTION_CONTROL_TOKEN", "evolution-control-token")
    monkeypatch.setenv("MEMORIA_EVOLUTION_VALIDATOR_TOKEN", "evolution-validator-token")
    monkeypatch.setenv("MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256", "a" * 64)
    monkeypatch.setenv("OFFLINE_MOCK", "true")


def _candidate(candidate_id: str, account_id: str, *, version: int) -> CandidateArtifact:
    now = datetime.now(UTC)
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family="weather",
        kind="prompt",
        scope="owner_private",
        account_id=account_id,
        version=version,
        payload={
            "proposal": {
                "instruction": "回答天气时必须使用用户请求的目标日期。",
                "match_terms": ["天气"],
            }
        },
        source_signal_ids=(f"{candidate_id}-signal-a", f"{candidate_id}-signal-b"),
        expected_behavior="answer the requested forecast date",
        regression_guards=("privacy_leakage_zero", "retention"),
        risk="low",
        trusted_root_sha256="a" * 64,
        created_at=now,
        updated_at=now,
    )


def _promote(store: EvolutionStore, candidate: CandidateArtifact) -> None:
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id=f"validation-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            gates=tuple(
                GateResult(name, True, (f"{name}-evidence",))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )
    store.transition_candidate(candidate.candidate_id, "validated", reason="reviewed")
    store.transition_candidate(candidate.candidate_id, "canary", reason="canary_ready")
    for index in range(3):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"{candidate.candidate_id}-task-{index}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"{candidate.candidate_id}-event-{index}",
        )
    store.transition_candidate(candidate.candidate_id, "stable", reason="canary_passed")


@pytest.mark.asyncio
async def test_lifecycle_api_exposes_reason_and_controls_last_known_good_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    control = {"X-Memoria-Internal-Token": "evolution-control-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        owner = (
            await client.post(
                "/v1/auth/register",
                json={"username": "evolution-lifecycle-owner", "password": "safe-password"},
            )
        ).json()
        other = (
            await client.post(
                "/v1/auth/register",
                json={"username": "evolution-lifecycle-other", "password": "safe-password"},
            )
        ).json()
        owner_bearer = {"Authorization": f"Bearer {owner['access_token']}"}
        other_bearer = {"Authorization": f"Bearer {other['access_token']}"}
        first = _candidate("rollback-v1", owner["user_id"], version=1)
        second = _candidate("rollback-v2", owner["user_id"], version=2)
        _promote(app.state.evolution_store, first)
        _promote(app.state.evolution_store, second)

        candidate = await client.get("/v1/evolution/candidates/rollback-v1", headers=owner_bearer)
        lifecycle = await client.get(
            "/v1/evolution/candidates/rollback-v1/lifecycle-events",
            headers=owner_bearer,
        )
        hidden = await client.get(
            "/v1/evolution/candidates/rollback-v1/lifecycle-events",
            headers=other_bearer,
        )
        unauthenticated_rollback = await client.post(
            "/v1/evolution/candidates/rollback-v1/rollback",
            json={"reason": "regression_detected"},
        )
        missing_reason = await client.post(
            "/v1/evolution/candidates/rollback-v1/rollback",
            headers=control,
            json={"reason": "   "},
        )
        invalid_target = await client.post(
            "/v1/evolution/candidates/rollback-v2/rollback",
            headers=control,
            json={"reason": "regression_detected"},
        )
        restored = await client.post(
            "/v1/evolution/candidates/rollback-v1/rollback",
            headers=control,
            json={"reason": "regression_detected"},
        )
        lifecycle_after = await client.get(
            "/v1/evolution/candidates/rollback-v1/lifecycle-events",
            headers=owner_bearer,
        )

    assert candidate.status_code == 200
    assert candidate.json()["status"] == "retired"
    assert candidate.json()["reason"] == "superseded_by:rollback-v2"
    assert lifecycle.status_code == 200
    assert [event["event_type"] for event in lifecycle.json()["items"]] == [
        "transition",
        "transition",
        "transition",
        "supersede",
    ]
    assert lifecycle.json()["items"][-1]["reason"] == "superseded_by:rollback-v2"
    assert lifecycle.json()["items"][-1]["related_candidate_id"] == "rollback-v2"
    assert hidden.status_code == 404
    assert unauthenticated_rollback.status_code == 401
    assert missing_reason.status_code == 422
    assert invalid_target.status_code == 409
    assert restored.status_code == 200
    assert restored.json()["status"] == "stable"
    assert restored.json()["reason"] == "regression_detected"
    assert [event["event_type"] for event in lifecycle_after.json()["items"]][-1] == "rollback_restore"
    assert lifecycle_after.json()["items"][-1]["related_candidate_id"] == "rollback-v2"
