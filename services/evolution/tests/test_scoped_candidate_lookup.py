from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.evolution.curation import EvolutionControlPlane
from services.evolution.domain import CandidateArtifact, GateResult, ValidationReport
from services.evolution.resolver import EvolutionResolver
from services.evolution.store import EvolutionStore, EvolutionTransitionError


def _candidate(
    candidate_id: str,
    *,
    account_id: str | None,
    version: int = 1,
) -> CandidateArtifact:
    now = datetime.now(UTC)
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family="weather",
        kind="prompt",
        scope="owner_private" if account_id is not None else "global_redacted",
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


def test_account_scoped_candidate_query_returns_only_global_and_matching_owner(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidates = (
        _candidate("global-candidate", account_id=None),
        _candidate("owner-a-candidate", account_id="account-a"),
        _candidate("owner-b-candidate", account_id="account-b"),
    )
    for candidate in candidates:
        store.create_candidate(candidate)

    visible = store.list_candidates_for_account("account-a")
    candidate_only = store.list_candidates_for_account("account-a", statuses=("candidate",))

    assert {candidate.candidate_id for candidate in visible} == {
        "global-candidate",
        "owner-a-candidate",
    }
    assert {candidate.candidate_id for candidate in candidate_only} == {
        "global-candidate",
        "owner-a-candidate",
    }
    assert store.list_candidates_for_account("account-a", statuses=("stable",)) == ()


def test_resolver_uses_account_scoped_read_without_broad_candidate_query(tmp_path: Path) -> None:
    class ScopedReadStore(EvolutionStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.scoped_reads: list[tuple[str, tuple[str, ...] | None]] = []

        def list_candidates(self, **kwargs: object) -> tuple[CandidateArtifact, ...]:
            raise AssertionError("resolver must not materialize candidates for every account")

        def list_candidates_for_account(
            self,
            account_id: str,
            *,
            statuses: tuple[str, ...] | None = None,
            task_family: str | None = None,
        ) -> tuple[CandidateArtifact, ...]:
            self.scoped_reads.append((account_id, statuses))
            return super().list_candidates_for_account(
                account_id,
                statuses=statuses,  # type: ignore[arg-type]
                task_family=task_family,
            )

    store = ScopedReadStore(tmp_path / "evolution.sqlite3")
    _promote(store, _candidate("global-stable", account_id=None))
    _promote(store, _candidate("owner-a-stable", account_id="account-a"))
    _promote(store, _candidate("owner-b-stable", account_id="account-b"))

    resolved = EvolutionResolver(store, trusted_root_sha256="a" * 64).resolve(
        account_id="account-a",
        session_id="session-a",
        speaker_class="owner",
        query="明天天气怎么样？",
    )

    assert {artifact.candidate_id for artifact in resolved} == {"global-stable", "owner-a-stable"}
    assert store.scoped_reads == [("account-a", ("stable", "canary"))]


def test_control_plane_rollback_rechecks_runtime_kind_and_trusted_root(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    harness_first = replace(_candidate("harness-v1", account_id="account-a"), kind="harness")
    harness_second = replace(
        _candidate("harness-v2", account_id="account-a", version=2),
        kind="harness",
    )
    _promote(store, harness_first)
    _promote(store, harness_second)
    plane = EvolutionControlPlane(store, trusted_root_sha256="a" * 64)
    with pytest.raises(EvolutionTransitionError, match="separately released runtime adapter"):
        plane.rollback(harness_first.candidate_id, reason="regression_detected")

    foreign_first = replace(
        _candidate("foreign-v1", account_id="account-b"),
        trusted_root_sha256="b" * 64,
    )
    foreign_second = replace(
        _candidate("foreign-v2", account_id="account-b", version=2),
        trusted_root_sha256="b" * 64,
    )
    _promote(store, foreign_first)
    _promote(store, foreign_second)
    with pytest.raises(EvolutionTransitionError, match="trusted root"):
        plane.rollback(foreign_first.candidate_id, reason="regression_detected")
