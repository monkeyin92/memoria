from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import asyncpg
import pytest
from services.evolution.domain import (
    CandidateArtifact,
    FenceSnapshot,
    GateResult,
    LayerVerdict,
    LearningSignal,
    SpeakerSnapshot,
    ValidationReport,
)
from services.evolution.postgres_store import PostgresEvolutionStore
from services.evolution.store import EvolutionConflictError, EvolutionTransitionError


def _candidate(
    candidate_id: str,
    *,
    task_family: str,
    version: int = 1,
    now: datetime | None = None,
) -> CandidateArtifact:
    timestamp = now or datetime.now(UTC)
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family=task_family,
        kind="harness",
        scope="global_redacted",
        account_id=None,
        version=version,
        payload={"proposal": {"rule": "target_date"}},
        source_signal_ids=(f"signal-a-{candidate_id}", f"signal-b-{candidate_id}"),
        expected_behavior="use the target forecast date",
        regression_guards=("privacy_leakage_zero", "retention"),
        risk="high",
        trusted_root_sha256="b" * 64,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _validation(candidate_id: str, *, now: datetime | None = None) -> ValidationReport:
    return ValidationReport(
        validation_id=f"validation-{candidate_id}",
        candidate_id=candidate_id,
        gates=tuple(
            GateResult(name, True, (f"{name}-{candidate_id}",))
            for name in ("failure_replay", "retention", "transfer", "safety")
        ),
        created_at=now or datetime.now(UTC),
    )


def _signal(signal_id: str, *, task_family: str, now: datetime) -> LearningSignal:
    return LearningSignal(
        signal_id=signal_id,
        task_family=task_family,
        scope="global_redacted",
        account_id=None,
        fence=FenceSnapshot(f"session-{signal_id}", 1, 1, 0),
        speaker=SpeakerSnapshot(
            classification="uncertain",
            reason_code="redacted_test",
            history_eligible=False,
            owner_projection_eligible=False,
        ),
        source_event_ids=(f"event-{signal_id}",),
        result=LayerVerdict("fail", ("task_not_completed",)),
        process=LayerVerdict("pass", ("policy_verified",)),
        quality=LayerVerdict("pass", ("quality_verified",)),
        environment_version="postgres-contract-v1",
        failure_code="target_date_mismatch",
        created_at=now,
    )


def _evaluated_signal(signal_id: str, *, task_family: str, now: datetime) -> LearningSignal:
    signal = _signal(signal_id, task_family=task_family, now=now)
    return replace(
        signal,
        source_event_ids=(f"user-{task_family}", f"assistant-{task_family}"),
        artifact_versions=(("trajectory_evaluator", "offline-rubric-v1"),),
    )


def _prepare_canary(store: PostgresEvolutionStore, candidate: CandidateArtifact) -> None:
    store.create_candidate(candidate)
    store.record_validation(_validation(candidate.candidate_id, now=candidate.created_at))
    store.transition_candidate(candidate.candidate_id, "validated")
    store.transition_candidate(candidate.candidate_id, "canary")
    for index in range(3):
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"task-{index}-{candidate.candidate_id}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"event-{index}-{candidate.candidate_id}",
        )


async def _cleanup(
    dsn: str,
    *,
    candidate_ids: list[str],
    signal_ids: list[str] | None = None,
    state_keys: list[str] | None = None,
) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        # Test fixtures are the only caller allowed to remove append-only evidence.
        await connection.execute("SELECT set_config('app.evolution_account_deletion', '1', false)")
        if candidate_ids:
            await connection.execute(
                "DELETE FROM evolution_lifecycle_events WHERE candidate_id = ANY($1::text[]) "
                "OR related_candidate_id = ANY($1::text[])",
                candidate_ids,
            )
            await connection.execute(
                "DELETE FROM evolution_activation_events WHERE candidate_id = ANY($1::text[])",
                candidate_ids,
            )
            await connection.execute(
                "DELETE FROM evolution_validations WHERE candidate_id = ANY($1::text[])",
                candidate_ids,
            )
            await connection.execute(
                "DELETE FROM evolution_candidates WHERE candidate_id = ANY($1::text[])",
                candidate_ids,
            )
        if signal_ids:
            await connection.execute(
                "DELETE FROM evolution_learning_signals WHERE signal_id = ANY($1::text[])",
                signal_ids,
            )
        if state_keys:
            await connection.execute(
                "DELETE FROM evolution_control_state WHERE state_key = ANY($1::text[])",
                state_keys,
            )
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL evolution contract",
)
async def test_postgres_evolution_store_matches_candidate_lifecycle() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    store = PostgresEvolutionStore(dsn)
    suffix = str(uuid.uuid4())
    candidate_id = f"evolution-{suffix}"
    now = datetime.now(UTC)
    candidate = _candidate(candidate_id, task_family=f"weather-{suffix}", now=now)
    report = _validation(candidate_id, now=now)
    try:
        stored = store.create_candidate(candidate)
        assert store.create_candidate(candidate) == stored
        store.record_validation(report)
        assert store.transition_candidate(candidate_id, "validated").status == "validated"
        assert store.transition_candidate(candidate_id, "canary").status == "canary"
        for index in range(3):
            store.record_activation(
                candidate_id=candidate_id,
                task_id=f"task-{index}-{suffix}",
                activated=True,
                adhered=True,
                outcome_passed=True,
                evidence_event_id=f"event-{index}-{suffix}",
            )
        assert store.transition_candidate(candidate_id, "stable").status == "stable"
        assert store.activation_metrics(candidate_id)["activation_outcome_rate"] == 1.0
    finally:
        await _cleanup(dsn, candidate_ids=[candidate_id])


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL evolution contract",
)
async def test_postgres_immutable_writes_are_concurrently_idempotent() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    store = PostgresEvolutionStore(dsn)
    suffix = str(uuid.uuid4())
    candidate_id = f"evolution-idempotent-{suffix}"
    signal_id = f"signal-idempotent-{suffix}"
    state_key = f"idempotent-{suffix}"
    now = datetime.now(UTC)
    signal = _signal(signal_id, task_family=f"weather-{suffix}", now=now)
    candidate = _candidate(candidate_id, task_family=f"weather-{suffix}", now=now)
    report = _validation(candidate_id, now=now)
    try:
        signals = await asyncio.gather(
            *(asyncio.to_thread(store.append_signal, signal) for _ in range(12))
        )
        candidates = await asyncio.gather(
            *(asyncio.to_thread(store.create_candidate, candidate) for _ in range(12))
        )
        validations = await asyncio.gather(
            *(asyncio.to_thread(store.record_validation, report) for _ in range(12))
        )
        assert signals == [signal] * 12
        assert candidates == [candidate] * 12
        assert validations == [report] * 12

        def set_control_state() -> None:
            store.set_control_state(
                state_key,
                {"signal_id": signal_id},
                updated_at=now,
            )

        await asyncio.gather(*(asyncio.to_thread(set_control_state) for _ in range(12)))
        assert store.get_control_state(state_key) == {"signal_id": signal_id}

        store.transition_candidate(candidate_id, "validated")
        store.transition_candidate(candidate_id, "canary")

        def record_activation() -> str:
            return store.record_activation(
                candidate_id=candidate_id,
                task_id=f"task-idempotent-{suffix}",
                activated=True,
                adhered=True,
                outcome_passed=True,
                evidence_event_id=f"event-idempotent-{suffix}",
            )

        activation_ids = await asyncio.gather(
            *(asyncio.to_thread(record_activation) for _ in range(12))
        )
        assert len(set(activation_ids)) == 1
        store.transition_candidate(candidate_id, "retired")
        assert record_activation() == activation_ids[0]

        connection = await asyncpg.connect(dsn)
        try:
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM evolution_learning_signals WHERE signal_id = $1",
                    signal_id,
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM evolution_candidates WHERE candidate_id = $1",
                    candidate_id,
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM evolution_validations WHERE validation_id = $1",
                    report.validation_id,
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT COUNT(*) FROM evolution_activation_events WHERE candidate_id = $1",
                    candidate_id,
                )
                == 1
            )
        finally:
            await connection.close()
    finally:
        await _cleanup(
            dsn,
            candidate_ids=[candidate_id],
            signal_ids=[signal_id],
            state_keys=[state_key],
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL evolution contract",
)
async def test_postgres_rejects_rekeyed_duplicate_trajectory_evaluation() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    store = PostgresEvolutionStore(dsn)
    suffix = str(uuid.uuid4())
    family = f"weather-dedupe-{suffix}"
    first_id = f"signal-dedupe-first-{suffix}"
    second_id = f"signal-dedupe-second-{suffix}"
    now = datetime.now(UTC)
    first = _evaluated_signal(first_id, task_family=family, now=now)
    second = _evaluated_signal(second_id, task_family=family, now=now)
    try:
        assert store.append_signal(first) == first
        with pytest.raises(
            EvolutionConflictError,
            match="already evaluated by this evaluator version",
        ):
            store.append_signal(second)
        assert len(store.list_signals(task_family=family)) == 1
    finally:
        await _cleanup(dsn, candidate_ids=[], signal_ids=[first_id, second_id])


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL evolution contract",
)
async def test_postgres_concurrent_promotions_leave_one_stable_per_cohort() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    store = PostgresEvolutionStore(dsn)
    suffix = str(uuid.uuid4())
    candidate_ids: list[str] = []
    try:
        for trial in range(4):
            family = f"weather-stable-{suffix}-{trial}"
            first = _candidate(
                f"evolution-stable-{suffix}-{trial}-v1",
                task_family=family,
                version=1,
            )
            second = _candidate(
                f"evolution-stable-{suffix}-{trial}-v2",
                task_family=family,
                version=2,
            )
            candidate_ids.extend((first.candidate_id, second.candidate_id))
            _prepare_canary(store, first)
            _prepare_canary(store, second)

            results = await asyncio.gather(
                asyncio.to_thread(store.transition_candidate, first.candidate_id, "stable"),
                asyncio.to_thread(store.transition_candidate, second.candidate_id, "stable"),
                return_exceptions=True,
            )
            assert all(
                isinstance(result, (CandidateArtifact, EvolutionTransitionError))
                for result in results
            )
            stable = store.list_candidates(status="stable", task_family=family)
            assert [(item.candidate_id, item.version) for item in stable] == [
                (second.candidate_id, 2)
            ]
    finally:
        await _cleanup(dsn, candidate_ids=candidate_ids)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL evolution contract",
)
async def test_postgres_lifecycle_audit_and_last_known_good_rollback() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    store = PostgresEvolutionStore(dsn)
    suffix = str(uuid.uuid4())
    family = f"weather-rollback-{suffix}"
    first = _candidate(f"evolution-rollback-{suffix}-v1", task_family=family, version=1)
    second = _candidate(f"evolution-rollback-{suffix}-v2", task_family=family, version=2)
    candidate_ids = [first.candidate_id, second.candidate_id]
    try:
        _prepare_canary(store, first)
        _prepare_canary(store, second)
        assert store.transition_candidate(first.candidate_id, "stable", reason="first_canary").status == "stable"
        assert store.transition_candidate(second.candidate_id, "stable", reason="second_canary").status == "stable"
        restored = store.rollback_to(first.candidate_id, reason="regression_detected")
        assert restored.status == "stable"
        assert restored.reason == "regression_detected"
        assert store.get_candidate(second.candidate_id).reason == f"rollback_to:{first.candidate_id}"
        events = store.list_lifecycle_events()
        scoped = [event for event in events if event.candidate_id in candidate_ids]
        assert [event.sequence for event in scoped] == sorted(event.sequence for event in scoped)
        assert [event.event_type for event in scoped[-4:]] == [
            "supersede",
            "transition",
            "rollback_retire",
            "rollback_restore",
        ]
        assert scoped[-1].candidate_id == first.candidate_id
        assert scoped[-1].related_candidate_id == second.candidate_id
    finally:
        await _cleanup(dsn, candidate_ids=candidate_ids)
