from datetime import UTC, datetime

import pytest
from services.archive.skill_domain import SkillRun, SkillRunRequest
from services.evolution.curation import EvolutionControlPlane
from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport
from services.evolution.skill import SkillActivationObserver
from services.evolution.store import EvolutionStore


def _candidate() -> CandidateArtifact:
    now = datetime.now(UTC)
    return CandidateArtifact(
        candidate_id="candidate-skill",
        task_family="tools",
        kind="skill",
        scope="owner_private",
        account_id="account-a",
        version=1,
        payload={"proposal": {"tool": "weather.lookup"}},
        source_signal_ids=("signal-a", "signal-b"),
        expected_behavior="use the verified tool",
        regression_guards=("retention",),
        risk="medium",
        trusted_root_sha256="f" * 64,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_skill_observer_records_activation_and_outcome(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    store.create_candidate(_candidate())
    store.record_validation(
        ValidationReport(
            validation_id="skill-validation",
            candidate_id="candidate-skill",
            gates=tuple(
                GateResult(name, True, (name,))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )
    store.transition_candidate("candidate-skill", "validated")
    store.transition_candidate("candidate-skill", "canary")
    observer = SkillActivationObserver(
        EvolutionControlPlane(store, trusted_root_sha256="f" * 64)
    )
    request = SkillRunRequest(
        account_id="account-a",
        skill_id="skill-a",
        version=1,
        confirmation_event_id="confirmation-a",
        inputs={"date": "tomorrow"},
        evolution_candidate_id="candidate-skill",
    )
    run = SkillRun(
        run_id="run-a",
        account_id="account-a",
        skill_id="skill-a",
        version=1,
        status="succeeded",
        rollback_status="not_needed",
        confirmation_event_id="confirmation-a",
        inputs={"date": "tomorrow"},
        output={"ok": True},
        error_code=None,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    await observer.on_run(request, run, succeeded=True)
    await observer.on_run(request, run, succeeded=True)
    assert store.activation_metrics("candidate-skill") == {
        "activation_rate": 1.0,
        "adherence_rate": 1.0,
        "activation_outcome_rate": 1.0,
        "activation_observations": 1.0,
        "activated_observations": 1.0,
    }
