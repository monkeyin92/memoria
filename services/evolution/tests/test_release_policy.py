from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.evolution.curation import EvolutionControlPlane
from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport
from services.evolution.release_policy import EvolutionReleasePolicy, parse_runtime_prompt_families
from services.evolution.resolver import EvolutionResolver
from services.evolution.store import EvolutionStore, EvolutionTransitionError


def _candidate(**changes: object) -> CandidateArtifact:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "candidate_id": "weather-prompt-v1",
        "task_family": "weather",
        "kind": "prompt",
        "scope": "global_redacted",
        "account_id": None,
        "version": 1,
        "payload": {
            "proposal": {
                "instruction": "回答天气时保留用户指定日期。",
                "match_terms": ["天气"],
            }
        },
        "source_signal_ids": ("signal-a", "signal-b"),
        "expected_behavior": "preserve the requested forecast date",
        "regression_guards": ("privacy_leakage_zero", "retention"),
        "risk": "low",
        "trusted_root_sha256": "a" * 64,
        "created_at": now,
        "updated_at": now,
    }
    values.update(changes)
    return CandidateArtifact(**values)  # type: ignore[arg-type]


def _validate(store: EvolutionStore, candidate: CandidateArtifact) -> None:
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


def _promote_through_store(store: EvolutionStore, candidate: CandidateArtifact) -> None:
    _validate(store, candidate)
    store.transition_candidate(candidate.candidate_id, "validated")
    store.transition_candidate(candidate.candidate_id, "canary")
    for index in range(3):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"{candidate.candidate_id}-task-{index}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"{candidate.candidate_id}-event-{index}",
        )
    store.transition_candidate(candidate.candidate_id, "stable")


def test_runtime_policy_allows_only_low_risk_global_allowlisted_prompt() -> None:
    policy = EvolutionReleasePolicy(frozenset({"weather", "public_context"}))

    assert policy.allows_runtime_prompt(_candidate())
    assert policy.allows_runtime_prompt(_candidate(task_family="public_context"))
    assert not policy.allows_runtime_prompt(_candidate(risk="medium"))
    assert policy.allows_runtime_prompt(_candidate(scope="owner_private", account_id="account-a"))
    assert not policy.allows_runtime_prompt(_candidate(task_family="privacy"))
    assert not policy.allows_runtime_prompt(_candidate(kind="harness"))


@pytest.mark.parametrize(
    "value,expected", [("weather, public_context", {"weather", "public_context"}), ("", set())]
)
def test_runtime_prompt_family_parsing_is_explicit(value: str, expected: set[str]) -> None:
    assert parse_runtime_prompt_families(value) == expected


def test_runtime_policy_rejects_non_runtime_candidate() -> None:
    policy = EvolutionReleasePolicy()

    with pytest.raises(EvolutionTransitionError, match="low-risk scope-valid prompt"):
        policy.require_runtime_release(_candidate(task_family="identity"))


def test_runtime_prompt_family_parsing_rejects_deterministic_control_planes() -> None:
    with pytest.raises(ValueError, match="cannot be enabled as runtime prompts"):
        parse_runtime_prompt_families("weather,identity_privacy")


def test_medium_risk_allowlisted_candidate_can_be_validated_but_not_canaried(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidate = _candidate(risk="medium")
    _validate(store, candidate)
    plane = EvolutionControlPlane(store, trusted_root_sha256="a" * 64)

    validated = plane.transition(candidate.candidate_id, "validated")

    assert validated.status == "validated"
    with pytest.raises(EvolutionTransitionError, match="low-risk scope-valid prompt"):
        plane.transition(candidate.candidate_id, "canary")
    assert store.get_candidate(candidate.candidate_id).status == "validated"


def test_off_allowlist_candidate_cannot_enter_canary_or_stable(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidate = _candidate(
        candidate_id="public-context-v1",
        task_family="public_context",
    )
    _validate(store, candidate)
    plane = EvolutionControlPlane(
        store,
        trusted_root_sha256="a" * 64,
        release_policy=EvolutionReleasePolicy(frozenset({"weather"})),
    )
    plane.transition(candidate.candidate_id, "validated")

    with pytest.raises(EvolutionTransitionError, match="task-family allowlist"):
        plane.transition(candidate.candidate_id, "canary")

    # Simulate a caller bypassing the control plane for the canary transition.
    store.transition_candidate(candidate.candidate_id, "canary")
    for index in range(3):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"off-allowlist-task-{index}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"off-allowlist-event-{index}",
        )
    with pytest.raises(EvolutionTransitionError, match="task-family allowlist"):
        plane.transition(candidate.candidate_id, "stable")
    assert store.get_candidate(candidate.candidate_id).status == "canary"


def test_resolver_rejects_disallowed_candidate_written_directly_as_stable(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidate = _candidate(
        candidate_id="public-context-stable",
        task_family="public_context",
    )
    store.create_candidate(candidate)
    with store._connect() as connection:
        connection.execute(
            "UPDATE evolution_candidates SET status = 'stable' WHERE candidate_id = ?",
            (candidate.candidate_id,),
        )
    resolver = EvolutionResolver(
        store,
        trusted_root_sha256="a" * 64,
        release_policy=EvolutionReleasePolicy(frozenset({"weather"})),
    )

    assert store.get_candidate(candidate.candidate_id).status == "stable"
    assert (
        resolver.resolve(
            account_id="account-a",
            session_id="session-a",
            speaker_class="owner",
            query="看看天气和公共信息",
        )
        == ()
    )


def test_rollback_rechecks_current_release_policy_before_mutating_state(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    first = _candidate(candidate_id="weather-v1")
    second = _candidate(candidate_id="weather-v2", version=2)
    _promote_through_store(store, first)
    _promote_through_store(store, second)
    plane = EvolutionControlPlane(
        store,
        trusted_root_sha256="a" * 64,
        release_policy=EvolutionReleasePolicy(frozenset()),
    )

    with pytest.raises(EvolutionTransitionError, match="task-family allowlist"):
        plane.rollback(first.candidate_id, reason="regression_detected")

    assert store.get_candidate(first.candidate_id).status == "retired"
    assert store.get_candidate(second.candidate_id).status == "stable"
