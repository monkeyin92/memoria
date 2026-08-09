from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from services.archive.domain import EvidenceEvent
from services.control_api.app.main import create_app
from services.evolution.domain import (
    CandidateArtifact,
    FenceSnapshot,
    LayerVerdict,
    LearningSignal,
    SpeakerSnapshot,
)


def _configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMORIA_DB_PATH", str(tmp_path / "memoria.sqlite3"))
    monkeypatch.setenv("MEMORIA_AUTH_SECRET", "test-auth-material-that-is-long-enough")
    monkeypatch.setenv("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "archive-internal-token")
    monkeypatch.setenv("MEMORIA_EVOLUTION_CONTROL_TOKEN", "evolution-control-token")
    monkeypatch.setenv("MEMORIA_EVOLUTION_VALIDATOR_TOKEN", "evolution-validator-token")
    monkeypatch.setenv("MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256", "a" * 64)
    monkeypatch.setenv("OFFLINE_MOCK", "true")


def _register_adult_profile(app: FastAPI, account_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    app.state.memory_store.register_account(
        user_id=account_id,
        username=f"test-{account_id}",
        username_normalized=f"test-{account_id}".casefold(),
        password_hash="not-used-by-internal-route-tests",
        now=now,
    )


def _candidate(candidate_id: str, account_id: str) -> CandidateArtifact:
    now = datetime.now(UTC)
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family="weather",
        kind="prompt",
        scope="owner_private",
        account_id=account_id,
        version=1,
        payload={
            "proposal": {
                "instruction": "回答天气时必须使用用户请求的目标日期。",
                "match_terms": ["天气"],
            }
        },
        source_signal_ids=(f"{candidate_id}-signal-a", f"{candidate_id}-signal-b"),
        expected_behavior="answer the requested forecast date",
        regression_guards=("privacy_leakage_zero", "old_task_retention_non_regressing"),
        risk="low",
        trusted_root_sha256="a" * 64,
        created_at=now,
        updated_at=now,
    )


def _failed_signal(signal_id: str, account_id: str, *, family: str = "weather") -> LearningSignal:
    return LearningSignal(
        signal_id=signal_id,
        task_family=family,
        scope="owner_private",
        account_id=account_id,
        fence=FenceSnapshot(f"session-{signal_id}", 1, 1, 0),
        speaker=SpeakerSnapshot(
            classification="owner",
            reason_code="formal_owner",
            history_eligible=True,
            owner_projection_eligible=True,
            profile_id="owner-profile",
        ),
        source_event_ids=(f"{signal_id}-user", f"{signal_id}-assistant"),
        result=LayerVerdict("fail", ("task_not_completed",)),
        process=LayerVerdict("pass", ("policy_verified",)),
        quality=LayerVerdict("pass", ("quality_verified",)),
        environment_version="test",
        failure_code="target_date_mismatch",
    )


@pytest.mark.asyncio
async def test_minor_account_evolution_routes_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "minor-evolution", "password": "safe-password"},
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
                json={"username": "minor-evolution", "password": "safe-password"},
            )
        ).json()
        listed = await client.get(
            "/v1/evolution/candidates",
            headers={"Authorization": f"Bearer {refreshed['access_token']}"},
        )
        created = await client.post(
            "/v1/evolution/candidates",
            headers={"X-Memoria-Internal-Token": "evolution-control-token"},
            json={
                "candidate_id": "minor-private-candidate",
                "task_family": "weather",
                "kind": "prompt",
                "scope": "owner_private",
                "account_id": identity["user_id"],
                "version": 1,
                "payload": {
                    "proposal": {"instruction": "use target date", "match_terms": ["weather"]}
                },
                "source_signal_ids": ["minor-signal-a", "minor-signal-b"],
                "expected_behavior": "use the requested date",
                "regression_guards": ["privacy_leakage_zero"],
                "risk": "low",
            },
        )

    for response in (listed, created):
        assert response.status_code == 403
        assert response.json()["detail"] == {
            "code": "minor_forbidden",
            "capability": "account_evolution",
        }


@pytest.mark.asyncio
async def test_evolution_control_api_scopes_candidates_and_enforces_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        identity = (
            await client.post(
                "/v1/auth/register",
                json={"username": "evolution-api-owner", "password": "safe-password"},
            )
        ).json()
        bearer = {"Authorization": f"Bearer {identity['access_token']}"}
        app.state.evolution_store.create_candidate(
            _candidate("visible-candidate", identity["user_id"])
        )
        app.state.evolution_store.create_candidate(
            _candidate("other-candidate", "account-other")
        )
        listed = await client.get("/v1/evolution/candidates", headers=bearer)
        validation = {
            "validation_id": "validation-visible",
            "gates": [
                {"name": "failure_replay", "passed": True, "evidence": ["replay-1"]},
                {"name": "retention", "passed": True, "evidence": ["retain-1"]},
                {"name": "transfer", "passed": True, "evidence": ["transfer-1"]},
                {"name": "safety", "passed": True, "evidence": ["safety-1"]},
            ],
            "metrics": {"transfer_accuracy": 1.0, "retention_accuracy": 1.0},
        }
        wrong_token = await client.post(
            "/v1/evolution/candidates/visible-candidate/validations",
            headers={"X-Memoria-Internal-Token": "archive-internal-token"},
            json=validation,
        )
        validator = {"X-Memoria-Internal-Token": "evolution-validator-token"}
        control = {"X-Memoria-Internal-Token": "evolution-control-token"}
        validated_report = await client.post(
            "/v1/evolution/candidates/visible-candidate/validations",
            headers=validator,
            json=validation,
        )
        transitions = []
        for target in ("validated", "canary"):
            transitions.append(
                await client.post(
                    "/v1/evolution/candidates/visible-candidate/transition",
                    headers=control,
                    json={"target": target, "reason": f"test-{target}"},
                )
            )
        candidate = app.state.evolution_store.get_candidate("visible-candidate")
        activation_responses = []
        for index in range(3):
            evidence_event_id = f"canary-event-{index}"
            await app.state.life_archive.record(
                EvidenceEvent(
                    event_id=evidence_event_id,
                    account_id=identity["user_id"],
                    event_type="assistant.playout_stopped",
                    occurred_at=datetime.now(UTC),
                    speaker_class="assistant",
                    source="test.evolution.canary",
                    payload={
                        "response_provenance": {
                            "evolution_artifacts": [
                                {
                                    "candidate_id": candidate.candidate_id,
                                    "version": candidate.version,
                                    "kind": "prompt",
                                    "status": "canary",
                                    "artifact_hash": candidate.artifact_hash,
                                }
                            ]
                        }
                    },
                )
            )
            activation_responses.append(
                await client.post(
                    "/v1/evolution/candidates/visible-candidate/activations",
                    headers=validator,
                    json={
                        "account_id": identity["user_id"],
                        "task_id": f"canary-task-{index}",
                        "activated": True,
                        "adhered": True,
                        "outcome_passed": True,
                        "evidence_event_id": evidence_event_id,
                    },
                )
            )
        transitions.append(
            await client.post(
                "/v1/evolution/candidates/visible-candidate/transition",
                headers=control,
                json={"target": "stable", "reason": "test-stable"},
            )
        )
        metrics = await client.get(
            "/v1/evolution/candidates/visible-candidate/metrics",
            headers=bearer,
        )

    assert listed.status_code == 200
    assert [item["candidate_id"] for item in listed.json()["items"]] == ["visible-candidate"]
    assert wrong_token.status_code == 401
    assert validated_report.status_code == 201
    assert validated_report.json()["required_gates_present"] is True
    assert [response.status_code for response in transitions] == [200, 200, 200]
    assert [response.status_code for response in activation_responses] == [201, 201, 201]
    assert transitions[-1].json()["status"] == "stable"
    assert metrics.json()["metrics"]["activation_observations"] == 3.0
    assert metrics.json()["metrics"]["activated_observations"] == 3.0


@pytest.mark.asyncio
async def test_evolution_transition_rejects_incomplete_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    _register_adult_profile(app, "account-a")
    app.state.evolution_store.create_candidate(_candidate("incomplete", "account-a"))
    control = {"X-Memoria-Internal-Token": "evolution-control-token"}
    validator = {"X-Memoria-Internal-Token": "evolution-validator-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        report = await client.post(
            "/v1/evolution/candidates/incomplete/validations",
            headers=validator,
            json={
                "validation_id": "incomplete-validation",
                "gates": [
                    {"name": "failure_replay", "passed": True},
                    {"name": "retention", "passed": True},
                    {"name": "safety", "passed": True},
                ],
            },
        )
        transition = await client.post(
            "/v1/evolution/candidates/incomplete/transition",
            headers=control,
            json={"target": "validated"},
        )

    assert report.status_code == 201
    assert report.json()["missing_required_gates"] == ["transfer"]
    assert report.json()["passed"] is False
    assert transition.status_code == 409


@pytest.mark.asyncio
async def test_candidate_creation_binds_trusted_root_and_requires_matching_failed_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    _register_adult_profile(app, "account-a")
    app.state.evolution_store.append_signal(_failed_signal("failed-a", "account-a"))
    app.state.evolution_store.append_signal(_failed_signal("failed-b", "account-a"))
    app.state.evolution_store.append_signal(
        _failed_signal("other-family", "account-a", family="reliability")
    )
    control = {"X-Memoria-Internal-Token": "evolution-control-token"}
    body = {
        "candidate_id": "created-through-control-plane",
        "task_family": "weather",
        "kind": "prompt",
        "scope": "owner_private",
        "account_id": "account-a",
        "version": 1,
        "payload": {
            "proposal": {
                "instruction": "回答天气时校验目标日期。",
                "match_terms": ["天气"],
            }
        },
        "source_signal_ids": ["failed-a", "failed-b"],
        "expected_behavior": "use the target forecast date",
        "regression_guards": ["privacy_leakage_zero", "retention"],
        "risk": "medium",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/v1/evolution/candidates", headers=control, json=body)
        mismatched_body = dict(body)
        mismatched_body["candidate_id"] = "mismatched-source"
        mismatched_body["source_signal_ids"] = ["failed-a", "other-family"]
        mismatched = await client.post(
            "/v1/evolution/candidates",
            headers=control,
            json=mismatched_body,
        )

    assert created.status_code == 201
    assert created.json()["trusted_root_sha256"] == "a" * 64
    assert created.json()["artifact_hash"] == app.state.evolution_store.get_candidate(
        "created-through-control-plane"
    ).artifact_hash
    assert mismatched.status_code == 422
    assert "scope does not match" in mismatched.json()["detail"]


@pytest.mark.asyncio
async def test_activation_rejects_missing_or_forged_canonical_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    _register_adult_profile(app, "account-a")
    candidate = _candidate("activation-evidence", "account-a")
    app.state.evolution_store.create_candidate(candidate)
    from services.evolution.domain import GateResult, ValidationReport

    app.state.evolution_store.record_validation(
        ValidationReport(
            validation_id="activation-validation",
            candidate_id=candidate.candidate_id,
            gates=tuple(
                GateResult(name, True, (name,))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )
    app.state.evolution_store.transition_candidate(candidate.candidate_id, "validated")
    app.state.evolution_store.transition_candidate(candidate.candidate_id, "canary")
    await app.state.life_archive.record(
        EvidenceEvent(
            event_id="forged-evidence",
            account_id="account-a",
            event_type="assistant.playout_stopped",
            occurred_at=datetime.now(UTC),
            speaker_class="assistant",
            source="test.evolution.canary",
            payload={"response_provenance": {"evolution_artifacts": []}},
        )
    )
    validator = {"X-Memoria-Internal-Token": "evolution-validator-token"}
    base = {
        "account_id": "account-a",
        "task_id": "task-a",
        "activated": True,
        "adhered": True,
        "outcome_passed": True,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.post(
            f"/v1/evolution/candidates/{candidate.candidate_id}/activations",
            headers=validator,
            json={**base, "evidence_event_id": "missing-evidence"},
        )
        forged = await client.post(
            f"/v1/evolution/candidates/{candidate.candidate_id}/activations",
            headers=validator,
            json={**base, "evidence_event_id": "forged-evidence"},
        )

    assert missing.status_code == 422
    assert forged.status_code == 422
    assert app.state.evolution_store.activation_metrics(candidate.candidate_id)[
        "activation_observations"
    ] == 0.0


@pytest.mark.asyncio
async def test_independent_validator_records_structured_signal_from_canonical_pair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    now = datetime.now(UTC)
    account_id = "account-evaluated"
    _register_adult_profile(app, account_id)
    user_event = EvidenceEvent(
        event_id="evaluated-user",
        account_id=account_id,
        event_type="speech.utterance_finalized",
        occurred_at=now,
        speaker_class="owner",
        source="funasr.authoritative_final",
        session_id="evaluated-session",
        turn_id=2,
        generation_id=4,
        payload={
            "text": "不应进入学习信号的原始问题",
            "tool_epoch": 1,
            "history_eligible": True,
            "owner_projection_eligible": True,
            "speaker_reason_code": "formal_owner",
            "speaker_profile_id": "owner-profile",
        },
    )
    assistant_event = EvidenceEvent(
        event_id="evaluated-assistant",
        account_id=account_id,
        event_type="assistant.playout_stopped",
        occurred_at=now,
        speaker_class="assistant",
        source="generation_fence.actual_heard",
        session_id="evaluated-session",
        turn_id=2,
        generation_id=4,
        payload={
            "text": "不应进入学习信号的原始回答",
            "tool_epoch": 1,
            "actual_heard": True,
        },
    )
    await app.state.life_archive.record(user_event)
    await app.state.life_archive.record(assistant_event)
    body = {
        "evaluation_id": "weather-eval-001",
        "evaluator_version": "offline-rubric-v1",
        "account_id": account_id,
        "user_event_id": user_event.event_id,
        "assistant_event_id": assistant_event.event_id,
        "task_family": "weather",
        "task_completed": False,
        "expected_state": {"target_date": "requested"},
        "actual_state": {"target_date": "today"},
        "actions": [],
        "allowed_tools": ["weather.lookup"],
        "forbidden_tools": [],
        "privacy_violation": False,
        "authorization_violation": False,
        "stale_fence": False,
        "commitment_action_consistent": True,
        "quality_dimensions": {"factuality": "fail", "clarity": "pass"},
        "environment_version": "weather-eval-v1",
        "failure_code": "target_date_mismatch",
        "diagnosis_code": "forecast_date_not_bound",
        "input_tokens": 20,
        "output_tokens": 12,
        "latency_ms": 350,
    }
    validator = {"X-Memoria-Internal-Token": "evolution-validator-token"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        replay_bundle = await client.post(
            "/v1/evolution/trajectory-replay-bundles",
            headers=validator,
            json={
                "account_id": account_id,
                "user_event_id": user_event.event_id,
                "assistant_event_id": assistant_event.event_id,
            },
        )
        denied = await client.post(
            "/v1/evolution/trajectory-evaluations",
            headers={"X-Memoria-Internal-Token": "evolution-control-token"},
            json=body,
        )
        recorded = await client.post(
            "/v1/evolution/trajectory-evaluations",
            headers=validator,
            json=body,
        )
        duplicate = await client.post(
            "/v1/evolution/trajectory-evaluations",
            headers=validator,
            json=body,
        )
        rekeyed = await client.post(
            "/v1/evolution/trajectory-evaluations",
            headers=validator,
            json={**body, "evaluation_id": "weather-eval-002"},
        )

    assert denied.status_code == 401
    assert replay_bundle.status_code == 200
    replay_payload = replay_bundle.json()
    assert len(replay_payload["bundle_sha256"]) == 64
    assert replay_payload["bundle"]["scope"] == "owner_private"
    assert replay_payload["bundle"]["account_id"] == account_id
    assert replay_payload["bundle"]["user"]["text"] == user_event.payload["text"]
    assert replay_payload["bundle"]["assistant"]["text"] == assistant_event.payload["text"]
    assert recorded.status_code == 201
    assert duplicate.status_code == 201
    assert duplicate.json() == recorded.json()
    assert rekeyed.status_code == 409
    assert recorded.json() == {
        "signal_id": recorded.json()["signal_id"],
        "task_family": "weather",
        "scope": "owner_private",
        "failed": True,
        "failure_code": "target_date_mismatch",
        "result": "fail",
        "process": "pass",
        "quality": "fail",
        "source_event_ids": ["evaluated-user", "evaluated-assistant"],
    }
    signal = app.state.evolution_store.get_signal(recorded.json()["signal_id"])
    serialized = repr(signal.to_dict())
    assert "原始问题" not in serialized
    assert "原始回答" not in serialized


@pytest.mark.asyncio
async def test_replay_bundle_removes_guest_identity_and_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    now = datetime.now(UTC)
    account_id = "account-guest-replay"
    _register_adult_profile(app, account_id)
    await app.state.life_archive.record(
        EvidenceEvent(
            event_id="guest-replay-user",
            account_id=account_id,
            event_type="speech.utterance_finalized",
            occurred_at=now,
            speaker_class="guest",
            source="funasr.authoritative_final",
            session_id="guest-replay-session",
            turn_id=1,
            generation_id=1,
            payload={
                "text": "访客原始文本不得进入 bundle",
                "tool_epoch": 0,
                "history_eligible": False,
                "owner_projection_eligible": False,
                "speaker_reason_code": "手机号13812345678",
            },
        )
    )
    await app.state.life_archive.record(
        EvidenceEvent(
            event_id="guest-replay-assistant",
            account_id=account_id,
            event_type="assistant.playout_stopped",
            occurred_at=now,
            speaker_class="assistant",
            source="generation_fence.actual_heard",
            session_id="guest-replay-session",
            turn_id=1,
            generation_id=1,
            payload={
                "text": "助手原始文本也不得进入 bundle",
                "tool_epoch": 0,
                "actual_heard": True,
            },
        )
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/evolution/trajectory-replay-bundles",
            headers={"X-Memoria-Internal-Token": "evolution-validator-token"},
            json={
                "account_id": account_id,
                "user_event_id": "guest-replay-user",
                "assistant_event_id": "guest-replay-assistant",
            },
        )

    assert response.status_code == 200
    bundle = response.json()["bundle"]
    assert bundle["scope"] == "global_redacted"
    assert bundle["account_id"] is None
    assert bundle["speaker"]["profile_id"] is None
    assert "text" not in bundle["user"]
    assert "text" not in bundle["assistant"]
    assert bundle["speaker"]["reason_code"] == "redacted_guest"
    assert bundle["fence"]["session_id"] != "guest-replay-session"
    assert all(len(value) == 64 for value in bundle["source_event_ids"])
    assert "guest-replay-user" not in response.text
    assert "guest-replay-assistant" not in response.text
    assert "访客原始文本" not in response.text
    assert "助手原始文本" not in response.text


@pytest.mark.asyncio
async def test_evolution_replay_and_evaluation_fail_closed_during_account_deletion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    account_id = "account-evolution-deleting"
    app.state.evolution_store.mark_account_deleting(account_id)
    validator = {"X-Memoria-Internal-Token": "evolution-validator-token"}
    evaluation = {
        "evaluation_id": "deleting-evaluation",
        "account_id": account_id,
        "user_event_id": "missing-user",
        "assistant_event_id": "missing-assistant",
        "evaluator_version": "offline-rubric-v1",
        "task_family": "weather",
        "task_completed": False,
        "quality_dimensions": {"clarity": "uncertain"},
        "environment_version": "test-v1",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        replay = await client.post(
            "/v1/evolution/trajectory-replay-bundles",
            headers=validator,
            json={
                "account_id": account_id,
                "user_event_id": "missing-user",
                "assistant_event_id": "missing-assistant",
            },
        )
        recorded = await client.post(
            "/v1/evolution/trajectory-evaluations",
            headers=validator,
            json=evaluation,
        )

    assert replay.status_code == 409
    assert recorded.status_code == 409
