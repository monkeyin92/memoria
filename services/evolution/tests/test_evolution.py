from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from services.evolution.account_fence import AccountSubjectBlockedError, AccountWriteBlockedError
from services.evolution.curation import EvolutionControlPlane, SleepLearningPolicy
from services.evolution.diagnosis import CandidateGenerator, aggregate_failure_clusters
from services.evolution.domain import (
    CandidateArtifact,
    EvidenceRef,
    FenceSnapshot,
    GateResult,
    SpeakerSnapshot,
    ValidationReport,
)
from services.evolution.evaluation import (
    default_evolution_tasks,
    evaluate_all_modes,
    evaluate_runtime_control_plane_all_modes,
)
from services.evolution.runtime import EvolutionRuntimeCapture
from services.evolution.store import (
    EvolutionConflictError,
    EvolutionStore,
    EvolutionTransitionError,
)
from services.evolution.verifier import ToolAction, TrajectoryObservation, verify_trajectory
from services.evolution.worker import EvolutionSleepWorker


def _owner_speaker() -> SpeakerSnapshot:
    return SpeakerSnapshot(
        classification="owner",
        reason_code="formal_owner",
        history_eligible=True,
        owner_projection_eligible=True,
    )


def _observation(signal_id: str, *, failed: bool = True) -> TrajectoryObservation:
    return TrajectoryObservation(
        signal_id=signal_id,
        task_family="weather",
        account_id="account-a",
        scope="owner_private",
        fence=FenceSnapshot("session-a", 1, 1, 0),
        speaker=_owner_speaker(),
        source_event_ids=(f"event-{signal_id}",),
        canonicalized=True,
        task_completed=not failed,
        expected_state={"target_date": "tomorrow"},
        actual_state={"target_date": "today"} if failed else {"target_date": "tomorrow"},
        actions=(ToolAction("weather.lookup", "succeeded", "session-a:1:1:0"),),
        allowed_tools=("weather.lookup",),
        quality_dimensions=(("factuality", "fail" if failed else "pass"),),
        evidence_refs=(EvidenceRef("tool_result", f"result-{signal_id}", (f"event-{signal_id}",)),),
        environment_version="weather-v1",
        failure_code="state_mismatch:target_date" if failed else None,
    )


def _lifecycle_candidate(candidate_id: str, *, version: int, updated_at: datetime) -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family="lifecycle-weather",
        kind="prompt",
        scope="owner_private",
        account_id="account-a",
        version=version,
        payload={"proposal": {"instruction": f"rule {version}", "match_terms": ["weather"]}},
        source_signal_ids=(f"{candidate_id}-a", f"{candidate_id}-b"),
        expected_behavior="preserve the requested target date",
        regression_guards=("privacy", "retention"),
        risk="low",
        trusted_root_sha256="1" * 64,
        created_at=updated_at,
        updated_at=updated_at,
    )


def _promote_to_stable(store: EvolutionStore, candidate: CandidateArtifact) -> None:
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id=f"validation-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            gates=tuple(
                GateResult(name, True, (f"{name}-{candidate.candidate_id}",))
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


def test_three_layer_verifier_separates_environment_process_and_quality() -> None:
    report = verify_trajectory(_observation("failed", failed=True))
    assert report.result.verdict == "fail"
    assert report.process.verdict == "pass"
    assert report.quality.verdict == "fail"
    assert report.learning_signal().scope == "owner_private"


def test_sleep_learning_never_accepts_single_trajectory_support() -> None:
    with pytest.raises(ValueError, match="at least two failure signals"):
        SleepLearningPolicy(min_failure_support=1)


def test_durable_account_fence_blocks_private_writes_after_store_restart_but_allows_global(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evolution.sqlite3"
    store = EvolutionStore(path)
    store.mark_account_deleting("account-a")

    # A new store instance represents a restarted process with no in-memory
    # AccountOperationGate state.
    restarted = EvolutionStore(path)
    private_signal = verify_trajectory(_observation("fenced-direct")).learning_signal()
    with pytest.raises(sqlite3.IntegrityError, match="write blocked by account deletion"):
        restarted.append_signal(private_signal)
    plane = EvolutionControlPlane(restarted, trusted_root_sha256="f" * 64)
    with pytest.raises(AccountWriteBlockedError, match="account deletion"):
        plane.append_signal(replace(private_signal, signal_id="fenced-private"))

    global_signal = replace(
        verify_trajectory(_observation("fenced-global")).learning_signal(),
        signal_id="fenced-global",
        scope="global_redacted",
        account_id=None,
    )
    assert plane.append_signal(global_signal).scope == "global_redacted"
    assert restarted.list_signals(scope="global_redacted") == (global_signal,)


def test_deletion_fence_hides_owner_signals_from_sleep_cycle_reads(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    private_signal = verify_trajectory(_observation("hidden-after-deletion")).learning_signal()
    store.append_signal(private_signal)
    global_signal = replace(
        verify_trajectory(_observation("visible-global-after-deletion")).learning_signal(),
        signal_id="visible-global-after-deletion",
        scope="global_redacted",
        account_id=None,
        speaker=SpeakerSnapshot(
            classification="guest",
            reason_code="redacted_guest",
            history_eligible=False,
            owner_projection_eligible=False,
        ),
    )
    store.append_signal(global_signal)

    store.mark_account_deleting(private_signal.account_id or "")

    assert store.list_signals() == (global_signal,)
    assert store.unprocessed_signals() == (global_signal,)


def test_sleep_cycle_skips_historical_private_signals_after_subject_becomes_minor(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    store.append_signal(verify_trajectory(_observation("minor-old-a")).learning_signal())
    store.append_signal(verify_trajectory(_observation("minor-old-b")).learning_signal())

    def block_minor(account_id: str) -> None:
        assert account_id == "account-a"
        raise AccountSubjectBlockedError("minor accounts cannot use account evolution")

    plane = EvolutionControlPlane(
        store,
        trusted_root_sha256="a" * 64,
        policy=SleepLearningPolicy(min_new_signals=2, min_failure_support=2),
        account_subject_guard=block_minor,
    )

    report = plane.sleep_cycle()

    assert report.ran is True
    assert report.candidates == ()
    assert store.unprocessed_signals() == ()


def test_process_privacy_and_fence_fail_closed() -> None:
    observation = _observation("unsafe", failed=False)
    unsafe = replace(
        observation,
        privacy_violation=True,
        stale_fence=True,
    )
    report = verify_trajectory(unsafe)
    assert report.process.verdict == "fail"
    assert "privacy_violation" in report.process.reason_codes
    assert "stale_fence" in report.process.reason_codes


def test_evaluator_cannot_relabel_a_process_violation_as_a_prompt_failure() -> None:
    observation = replace(
        _observation("masked-process", failed=False),
        privacy_violation=True,
        failure_code="style_issue",
    )

    report = verify_trajectory(observation)

    assert report.failure_code == "privacy_violation"


def test_runtime_capture_requires_independent_evaluation_before_persisting_signal(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    plane = EvolutionControlPlane(store, trusted_root_sha256="c" * 64)
    capture = EvolutionRuntimeCapture(plane)
    user_event = {
        "event_id": "user-event",
        "account_id": "account-a",
        "event_type": "speech.utterance_finalized",
        "speaker_class": "owner",
        "session_id": "session-a",
        "turn_id": 4,
        "generation_id": 2,
        "tool_epoch": 1,
        "occurred_at": "2026-08-08T08:00:00+00:00",
        "payload": {
            "text": "这段原文不应该进入进化存储",
            "history_eligible": True,
            "owner_projection_eligible": True,
            "speaker_reason_code": "formal_owner",
        },
    }
    assistant_event = {
        "event_id": "assistant-event",
        "account_id": "account-a",
        "event_type": "assistant.playout_stopped",
        "speaker_class": "assistant",
        "session_id": "session-a",
        "turn_id": 4,
        "generation_id": 2,
        "tool_epoch": 1,
        "occurred_at": "2026-08-08T08:00:00+00:00",
        "payload": {
            "actual_heard": True,
            "evolution_observation": {"task_family": "weather", "task_completed": True}
        },
    }
    assert capture.capture_pair(user_event, assistant_event) is None
    assert store.list_signals() == ()
    signal_id = capture.capture_evaluation_pair(
        user_event,
        assistant_event,
        evaluation_id="weather-replay-v1",
        evaluation={
            "evaluator_version": "offline-rubric-v1",
            "task_family": "weather",
            "environment_version": "weather-replay-v1",
            "task_completed": True,
            "quality_dimensions": {"factuality": "pass"},
            "commitment_action_consistent": True,
        },
    )
    assert signal_id is not None and signal_id.startswith("evaluation:")
    signal = store.list_signals()[0]
    assert signal.source_event_ids == ("user-event", "assistant-event")
    assert "这段原文" not in signal.to_dict().__repr__()


def test_runtime_capture_fails_closed_on_missing_fence(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    capture = EvolutionRuntimeCapture(EvolutionControlPlane(store, trusted_root_sha256="d" * 64))
    assert (
        capture.capture_event(
            {
                "event_id": "missing-fence",
                "account_id": "account-a",
                "event_type": "speech.utterance_finalized",
                "speaker_class": "owner",
                "session_id": "session-a",
                "payload": {
                    "history_eligible": True,
                    "owner_projection_eligible": True,
                    "speaker_reason_code": "formal_owner",
                },
            }
        )
        is None
    )
    assert capture.pending_count() == 0


def test_store_diagnosis_candidate_validation_and_lifecycle(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    first = verify_trajectory(_observation("one")).learning_signal()
    second = verify_trajectory(_observation("two")).learning_signal()
    store.append_signal(first)
    store.append_signal(second)
    with pytest.raises(EvolutionConflictError):
        store.append_signal(replace(first, diagnosis="different"))
    clusters = aggregate_failure_clusters(store.list_signals(), min_support=2)
    assert len(clusters) == 1
    candidate = CandidateGenerator(trusted_root_sha256="a" * 64).from_cluster(
        clusters[0],
        kind="harness",
        expected_behavior="use target date",
        regression_guards=("privacy_leakage_zero", "old_task_retention_non_regressing"),
    )
    store.create_candidate(candidate)
    report = ValidationReport(
        validation_id="validation-1",
        candidate_id=candidate.candidate_id,
        gates=(
            GateResult("failure_replay", True, ("replay-1",)),
            GateResult("retention", True, ("retain-1",)),
            GateResult("transfer", True, ("transfer-1",)),
            GateResult("safety", True, ("safety-1",)),
        ),
        metrics=(
            ("transfer_accuracy", 1.0),
            ("retention_accuracy", 1.0),
        ),
    )
    store.record_validation(report)
    store.transition_candidate(candidate.candidate_id, "validated")
    store.transition_candidate(candidate.candidate_id, "canary")
    with pytest.raises(EvolutionTransitionError):
        store.transition_candidate(candidate.candidate_id, "stable")
    for index in range(3):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"task-{index}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"event-activation-{index}",
        )
    store.transition_candidate(candidate.candidate_id, "stable")
    assert store.activation_metrics(candidate.candidate_id)["adherence_rate"] == 1.0
    with pytest.raises(EvolutionTransitionError):
        store.transition_candidate(candidate.candidate_id, "validated")


def test_lifecycle_audit_records_supersede_and_last_known_good_rollback(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    now = datetime.now(UTC)
    first = _lifecycle_candidate("lifecycle-v1", version=1, updated_at=now)
    second = _lifecycle_candidate("lifecycle-v2", version=2, updated_at=now)
    assert replace(first, reason="review note").artifact_hash == first.artifact_hash
    _promote_to_stable(store, first)
    _promote_to_stable(store, second)

    assert store.get_candidate(first.candidate_id).status == "retired"
    assert store.get_candidate(first.candidate_id).reason == "superseded_by:lifecycle-v2"
    restored = store.rollback_to(first.candidate_id, reason="regression_detected")
    assert restored.status == "stable"
    assert restored.reason == "regression_detected"
    assert store.get_candidate(second.candidate_id).status == "retired"
    assert store.get_candidate(second.candidate_id).reason == "rollback_to:lifecycle-v1"

    events = store.list_lifecycle_events()
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert [event.event_type for event in events[-4:]] == [
        "supersede",
        "transition",
        "rollback_retire",
        "rollback_restore",
    ]
    assert events[-4].candidate_id == first.candidate_id
    assert events[-4].related_candidate_id == second.candidate_id
    assert events[-1].candidate_id == first.candidate_id
    assert events[-1].related_candidate_id == second.candidate_id
    assert [event.to_status for event in store.list_lifecycle_events(candidate_id=first.candidate_id)] == [
        "validated",
        "canary",
        "stable",
        "retired",
        "stable",
    ]


def test_rollback_rejects_retired_artifact_without_supersede_history(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidate = _lifecycle_candidate("ordinary-retired", version=1, updated_at=datetime.now(UTC))
    store.create_candidate(candidate)
    store.transition_candidate(candidate.candidate_id, "retired", reason="operator_cancelled")
    with pytest.raises(EvolutionTransitionError, match="retired by supersede"):
        store.rollback_to(candidate.candidate_id)
    assert [event.event_type for event in store.list_lifecycle_events()] == ["transition"]


def test_curate_records_expiry_as_an_audit_event(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    old = datetime.now(UTC) - timedelta(days=2)
    candidate = _lifecycle_candidate("stale-candidate", version=1, updated_at=old)
    store.create_candidate(candidate)
    assert store.curate(stale_before=datetime.now(UTC) - timedelta(days=1)) == {
        "rejected_candidates": 1,
        "retired_artifacts": 0,
    }
    event = store.list_lifecycle_events(candidate_id=candidate.candidate_id)
    assert [(item.event_type, item.from_status, item.to_status, item.reason) for item in event] == [
        ("curate", "candidate", "rejected", "stale_candidate")
    ]
    with store._connect() as connection, pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM evolution_lifecycle_events")


def test_sqlite_candidate_manifest_is_immutable_but_lifecycle_fields_can_change(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidate = _lifecycle_candidate(
        "manifest-guard",
        version=1,
        updated_at=datetime.now(UTC),
    )
    store.create_candidate(candidate)
    with store._connect() as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="candidate manifest is immutable",
    ):
        connection.execute(
            "UPDATE evolution_candidates SET payload_json = ? WHERE candidate_id = ?",
            ("{\"payload\":{\"proposal\":{\"instruction\":\"forged\"}}}", candidate.candidate_id),
        )

    retired = store.transition_candidate(candidate.candidate_id, "retired", reason="cancelled")
    assert retired.status == "retired"
    assert retired.reason == "cancelled"


def test_sqlite_scope_account_guard_rejects_private_metadata_on_global_rows(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    with store._connect() as connection, pytest.raises(
        sqlite3.IntegrityError,
        match="scope/account mismatch",
    ):
        connection.execute(
            """
            INSERT INTO evolution_learning_signals(
                signal_id, task_family, scope, account_id, payload_json,
                payload_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "scope-guard",
                "privacy",
                "global_redacted",
                "owner-a",
                "{}",
                "0" * 64,
                datetime.now(UTC).isoformat(),
            ),
        )


def test_sqlite_transition_check_and_update_are_atomic_across_store_instances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evolution.sqlite3"
    store = EvolutionStore(path)
    cluster = aggregate_failure_clusters(
        [
            verify_trajectory(_observation("atomic-1")).learning_signal(),
            verify_trajectory(_observation("atomic-2")).learning_signal(),
        ],
        min_support=2,
    )[0]
    candidate = CandidateGenerator(trusted_root_sha256="f" * 64).from_cluster(
        cluster,
        kind="prompt",
        expected_behavior="preserve the target date",
        regression_guards=("retention",),
    )
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id="atomic-validation",
            candidate_id=candidate.candidate_id,
            gates=tuple(
                GateResult(name, True, (name,))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )

    class CoordinatedStore(EvolutionStore):
        def __init__(self) -> None:
            self._validation_barrier = validation_barrier
            super().__init__(path)

        def latest_validation(self, candidate_id: str) -> ValidationReport | None:
            report = super().latest_validation(candidate_id)
            self._validation_barrier.wait(timeout=5)
            return report

    validation_barrier = Barrier(2)
    stores = (CoordinatedStore(), CoordinatedStore())

    def transition(current_store: EvolutionStore) -> object:
        try:
            return current_store.transition_candidate(candidate.candidate_id, "validated")
        except EvolutionTransitionError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(transition, stores))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, EvolutionTransitionError) for result in results) == 1
    assert store.get_candidate(candidate.candidate_id).status == "validated"


def test_validation_requires_all_independent_release_gates(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidate = CandidateGenerator(trusted_root_sha256="e" * 64).from_cluster(
        aggregate_failure_clusters(
            [verify_trajectory(_observation("gate-1")), verify_trajectory(_observation("gate-2"))],
            min_support=2,
        )[0],
        kind="prompt",
        expected_behavior="preserve the verified date",
        regression_guards=("retention",),
    )
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id="missing-transfer",
            candidate_id=candidate.candidate_id,
            gates=(
                GateResult("failure_replay", True, ("replay",)),
                GateResult("retention", True, ("retain",)),
                GateResult("safety", True, ("safe",)),
            ),
        )
    )
    report = store.latest_validation(candidate.candidate_id)
    assert report is not None
    assert report.missing_required_gates == ("transfer",)
    assert not report.passed
    with pytest.raises(EvolutionTransitionError):
        store.transition_candidate(candidate.candidate_id, "validated")


def test_control_plane_separates_supporting_failures_from_refuting_successes(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    signals = (
        verify_trajectory(_observation("support-1")).learning_signal(),
        verify_trajectory(_observation("support-2")).learning_signal(),
        verify_trajectory(_observation("refute-1", failed=False)).learning_signal(),
    )
    for signal in signals:
        store.append_signal(signal)
    cluster = aggregate_failure_clusters(signals, min_support=2)[0]
    candidate = CandidateGenerator(trusted_root_sha256="e" * 64).from_cluster(
        cluster,
        kind="prompt",
        expected_behavior="preserve the verified target date",
        regression_guards=("retention",),
    )
    plane = EvolutionControlPlane(store, trusted_root_sha256="e" * 64)

    stored = plane.create_candidate(candidate)

    assert stored.payload["refuting_signal_ids"] == ["refute-1"]
    with pytest.raises(ValueError, match="failed source trajectories"):
        plane.create_candidate(
            replace(
                candidate,
                candidate_id="mixed-support",
                source_signal_ids=("support-1", "refute-1"),
            )
        )


def test_sleep_cycle_only_proposes_supported_candidates(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    plane = EvolutionControlPlane(
        store,
        trusted_root_sha256="b" * 64,
        policy=SleepLearningPolicy(min_new_signals=2, min_failure_support=2),
    )
    plane.observe(_observation("sleep-1"))
    plane.observe(_observation("sleep-2"))
    result = plane.sleep_cycle()
    assert result.ran
    assert result.new_signal_count == 2
    assert len(result.candidates) == 1
    assert result.candidates[0].status == "candidate"
    repeated = plane.sleep_cycle()
    assert not repeated.ran
    assert repeated.new_signal_count == 0
    plane.observe(_observation("sleep-3"))
    assert not plane.sleep_cycle().ran
    plane.observe(_observation("sleep-4"))
    resumed = plane.sleep_cycle()
    assert resumed.ran
    assert resumed.new_signal_count == 2


def test_sleep_cycle_routes_permission_and_fence_failures_to_harness_candidates(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    plane = EvolutionControlPlane(
        store,
        trusted_root_sha256="b" * 64,
        policy=SleepLearningPolicy(min_new_signals=2, min_failure_support=2),
    )
    for signal_id in ("privacy-1", "privacy-2"):
        plane.observe(
            replace(
                _observation(signal_id, failed=False),
                privacy_violation=True,
            )
        )

    report = plane.sleep_cycle(candidate_kind="prompt")

    assert report.ran is True
    assert len(report.candidates) == 1
    assert report.candidates[0].kind == "harness"
    assert report.candidates[0].risk == "high"


def test_sleep_cycle_builds_resolvable_versioned_prompt_and_processes_late_signal(
    tmp_path: Path,
) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    plane = EvolutionControlPlane(
        store,
        trusted_root_sha256="b" * 64,
        policy=SleepLearningPolicy(min_new_signals=2, min_failure_support=2),
    )
    plane.observe(_observation("version-1"))
    plane.observe(_observation("version-2"))
    first = plane.sleep_cycle()
    assert first.candidates[0].version == 1
    proposal = first.candidates[0].payload["proposal"]
    assert isinstance(proposal, dict)
    assert proposal["instruction"]
    assert proposal["match_terms"] == ["天气", "天气预报", "气温", "预报"]

    old = datetime.now(UTC) - timedelta(days=90)
    plane.observe(replace(_observation("late-backfill"), created_at=old))
    assert len(store.unprocessed_signals()) == 1
    plane.observe(_observation("version-3"))
    second = plane.sleep_cycle()
    assert second.new_signal_count == 2
    assert second.candidates[0].version == 2
    assert store.unprocessed_signals() == ()


@pytest.mark.asyncio
async def test_sleep_worker_triggers_offline_cycle(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    plane = EvolutionControlPlane(
        store,
        trusted_root_sha256="b" * 64,
        policy=SleepLearningPolicy(min_new_signals=1, min_failure_support=2),
    )
    plane.observe(_observation("worker-signal"))
    worker = EvolutionSleepWorker(plane, interval_s=0.01)
    worker.start()
    try:
        for _ in range(20):
            if not store.unprocessed_signals():
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop()
    assert store.unprocessed_signals() == ()


def test_one_canonical_activation_event_cannot_be_counted_as_three_tasks(tmp_path: Path) -> None:
    store = EvolutionStore(tmp_path / "evolution.sqlite3")
    candidate = CandidateGenerator(trusted_root_sha256="e" * 64).from_cluster(
        aggregate_failure_clusters(
            [verify_trajectory(_observation("replay-1")), verify_trajectory(_observation("replay-2"))],
            min_support=2,
        )[0],
        kind="prompt",
        expected_behavior="preserve the requested date",
        regression_guards=("retention",),
    )
    store.create_candidate(candidate)
    store.record_validation(
        ValidationReport(
            validation_id="replay-validation",
            candidate_id=candidate.candidate_id,
            gates=tuple(
                GateResult(name, True, (name,))
                for name in ("failure_replay", "retention", "transfer", "safety")
            ),
        )
    )
    store.transition_candidate(candidate.candidate_id, "validated")
    store.transition_candidate(candidate.candidate_id, "canary")
    store.record_activation(
        candidate_id=candidate.candidate_id,
        task_id="task-1",
        activated=True,
        adhered=True,
        outcome_passed=True,
        evidence_event_id="same-canonical-event",
    )
    with pytest.raises(EvolutionConflictError):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id="task-2",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id="same-canonical-event",
        )
    assert store.activation_metrics(candidate.candidate_id)["activated_observations"] == 1.0
    with pytest.raises(EvolutionTransitionError):
        store.transition_candidate(candidate.candidate_id, "stable")


def test_longitudinal_evaluation_distinguishes_replacement() -> None:
    reports = evaluate_all_modes(default_evolution_tasks())
    assert (
        reports["evolving"].metrics.rule_replacement_accuracy
        > reports["append_only"].metrics.rule_replacement_accuracy
    )
    assert reports["append_only"].metrics.stale_reference_rate > 0
    assert reports["evolving"].metrics.version_count > 0


def test_runtime_control_plane_evaluation_exercises_real_resolver_and_scope() -> None:
    reports = evaluate_runtime_control_plane_all_modes()
    assert reports["static"].metrics.transfer_accuracy == 0.0
    assert reports["append_only"].metrics.transfer_accuracy == 1.0
    assert reports["append_only"].metrics.replacement_accuracy == 0.0
    assert reports["evolving"].metrics.replacement_accuracy == 1.0
    assert reports["evolving"].metrics.privacy_pass_rate == 1.0
    assert reports["evolving"].metrics.negative_transfer_rate == 0.0
