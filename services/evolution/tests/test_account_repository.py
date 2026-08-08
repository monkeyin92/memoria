from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest
from services.evolution.account_repository import (
    EVOLUTION_ACCOUNT_TABLES,
    PostgresEvolutionAccountRepository,
    SqliteEvolutionAccountRepository,
)
from services.evolution.domain import (
    CandidateArtifact,
    EvidenceRef,
    FenceSnapshot,
    GateResult,
    LayerVerdict,
    LearningSignal,
    SpeakerSnapshot,
    ValidationReport,
)
from services.evolution.postgres_store import PostgresEvolutionStore
from services.evolution.store import EvolutionStore


def _speaker(scope: str) -> SpeakerSnapshot:
    if scope == "owner_private":
        return SpeakerSnapshot(
            classification="owner",
            reason_code="formal_owner",
            history_eligible=True,
            owner_projection_eligible=True,
        )
    return SpeakerSnapshot(
        classification="uncertain",
        reason_code="redacted_global",
        history_eligible=False,
        owner_projection_eligible=False,
    )


def _signal(
    signal_id: str,
    *,
    scope: str,
    account_id: str | None,
    now: datetime,
) -> LearningSignal:
    event_id = f"event-{signal_id}"
    evidence = EvidenceRef("trajectory", signal_id, (event_id,))
    return LearningSignal(
        signal_id=signal_id,
        task_family="account-governance-contract",
        scope=scope,  # type: ignore[arg-type]
        account_id=account_id,
        fence=FenceSnapshot(f"session-{signal_id}", 1, 1, 0),
        speaker=_speaker(scope),
        source_event_ids=(event_id,),
        result=LayerVerdict("fail", reason_codes=("test_failure",), evidence_refs=(evidence,)),
        process=LayerVerdict("pass", evidence_refs=(evidence,)),
        quality=LayerVerdict("fail", reason_codes=("test_quality",), evidence_refs=(evidence,)),
        environment_version="contract-v1",
        failure_code="contract_failure",
        diagnosis="repository contract fixture",
        created_at=now,
    )


def _candidate(
    candidate_id: str,
    *,
    scope: str,
    account_id: str | None,
    signal_id: str,
    now: datetime,
) -> CandidateArtifact:
    return CandidateArtifact(
        candidate_id=candidate_id,
        task_family="account-governance-contract",
        kind="prompt",
        scope=scope,  # type: ignore[arg-type]
        account_id=account_id,
        version=1,
        payload={
            "proposal": {
                "instruction": "保留账号归属边界。",
                "match_terms": ["账号"],
            }
        },
        source_signal_ids=(signal_id,),
        expected_behavior="preserve account ownership",
        regression_guards=("privacy", "retention"),
        risk="low",
        trusted_root_sha256="a" * 64,
        created_at=now,
        updated_at=now,
    )


def _validation(candidate_id: str, *, now: datetime) -> ValidationReport:
    return ValidationReport(
        validation_id=f"validation-{candidate_id}",
        candidate_id=candidate_id,
        gates=tuple(
            GateResult(name, True, (f"evidence-{candidate_id}-{name}",))
            for name in ("failure_replay", "retention", "transfer", "safety")
        ),
        created_at=now,
    )


def _seed(store: EvolutionStore, *, suffix: str) -> tuple[str, str, str, str]:
    owner_id = f"owner-{suffix}"
    other_id = f"other-{suffix}"
    owner_signal_id = f"signal-owner-{suffix}"
    global_signal_id = f"signal-global-{suffix}"
    owner_candidate_id = f"candidate-owner-{suffix}"
    global_candidate_id = f"candidate-global-{suffix}"
    now = datetime.now(UTC)
    owner_signal = _signal(
        owner_signal_id,
        scope="owner_private",
        account_id=owner_id,
        now=now,
    )
    global_signal = _signal(
        global_signal_id,
        scope="global_redacted",
        account_id=None,
        now=now,
    )
    store.append_signal(owner_signal)
    store.append_signal(global_signal)
    store.mark_signals_processed((owner_signal_id, global_signal_id), processed_at=now)
    for candidate in (
        _candidate(
            owner_candidate_id,
            scope="owner_private",
            account_id=owner_id,
            signal_id=owner_signal_id,
            now=now,
        ),
        _candidate(
            global_candidate_id,
            scope="global_redacted",
            account_id=None,
            signal_id=global_signal_id,
            now=now,
        ),
    ):
        store.create_candidate(candidate)
        store.record_validation(_validation(candidate.candidate_id, now=now))
        store.transition_candidate(candidate.candidate_id, "validated")
        store.transition_candidate(candidate.candidate_id, "canary")
        store.record_activation(
            candidate_id=candidate.candidate_id,
            task_id=f"task-{candidate.candidate_id}",
            activated=True,
            adhered=True,
            outcome_passed=True,
            evidence_event_id=f"activation-event-{candidate.candidate_id}",
        )
    store.set_control_state(f"account-governance-{suffix}", {"owner": owner_id}, updated_at=now)
    return owner_id, other_id, owner_candidate_id, global_candidate_id


@pytest.mark.asyncio
async def test_sqlite_account_repository_exports_and_erases_only_owner_private_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evolution.sqlite3"
    store = EvolutionStore(path)
    owner_id, _other_id, owner_candidate_id, global_candidate_id = _seed(
        store, suffix=uuid.uuid4().hex[:10]
    )
    repository = SqliteEvolutionAccountRepository(path)

    exported = await repository.export_account(owner_id)
    assert set(exported) == set(EVOLUTION_ACCOUNT_TABLES)
    assert exported["evolution_learning_signals"]
    assert exported["evolution_candidates"]
    assert all(
        row.get("account_id") == owner_id
        for table in ("evolution_learning_signals", "evolution_candidates")
        for row in exported[table]
    )
    assert all(
        row.get("candidate_id") == owner_candidate_id
        for table in (
            "evolution_validations",
            "evolution_activation_events",
            "evolution_lifecycle_events",
        )
        for row in exported[table]
    )

    # A normal store connection cannot erase append-only evidence.
    with store._connect() as connection:  # noqa: SLF001 - contract test
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM evolution_learning_signals WHERE signal_id LIKE ?",
                ("signal-owner-%",),
            )

    deleted = await repository.delete_account(owner_id)
    assert set(deleted) == set(EVOLUTION_ACCOUNT_TABLES)
    assert all(count > 0 for count in deleted.values())
    assert await repository.remaining_account_rows(owner_id) == {}

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM evolution_candidates WHERE candidate_id = ?",
                (global_candidate_id,),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM evolution_control_state"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL evolution account contract",
)
async def test_postgres_account_repository_exports_and_erases_only_owner_private_rows() -> None:
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    suffix = uuid.uuid4().hex
    store = PostgresEvolutionStore(dsn)
    store.initialize()
    owner_id, _other_id, owner_candidate_id, global_candidate_id = _seed(store, suffix=suffix)
    repository = PostgresEvolutionAccountRepository(dsn)
    try:
        exported = await repository.export_account(owner_id)
        assert set(exported) == set(EVOLUTION_ACCOUNT_TABLES)
        assert exported["evolution_learning_signals"]
        assert exported["evolution_candidates"]
        assert all(
            row.get("account_id") == owner_id
            for table in ("evolution_learning_signals", "evolution_candidates")
            for row in exported[table]
        )
        assert all(
            row.get("candidate_id") == owner_candidate_id
            for table in (
                "evolution_validations",
                "evolution_activation_events",
                "evolution_lifecycle_events",
            )
            for row in exported[table]
        )

        connection = await asyncpg.connect(dsn)
        try:
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute(
                    "DELETE FROM evolution_learning_signals WHERE signal_id = $1",
                    f"signal-owner-{suffix}",
                )
        finally:
            await connection.close()

        deleted = await repository.delete_account(owner_id)
        assert set(deleted) == set(EVOLUTION_ACCOUNT_TABLES)
        assert all(count > 0 for count in deleted.values())
        assert await repository.remaining_account_rows(owner_id) == {}

        connection = await asyncpg.connect(dsn)
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM evolution_candidates WHERE candidate_id = $1",
                    global_candidate_id,
                )
                == 1
            )
        finally:
            await connection.close()
    finally:
        connection = await asyncpg.connect(dsn)
        try:
            await connection.execute("SELECT set_config('app.evolution_account_deletion', '1', false)")
            await connection.execute(
                "DELETE FROM evolution_lifecycle_events WHERE candidate_id LIKE $1 OR related_candidate_id LIKE $1",
                f"%{suffix}%",
            )
            await connection.execute(
                "DELETE FROM evolution_activation_events WHERE candidate_id LIKE $1",
                f"%{suffix}%",
            )
            await connection.execute(
                "DELETE FROM evolution_validations WHERE candidate_id LIKE $1",
                f"%{suffix}%",
            )
            await connection.execute(
                "DELETE FROM evolution_candidates WHERE candidate_id LIKE $1",
                f"%{suffix}%",
            )
            await connection.execute(
                "DELETE FROM evolution_sleep_signal_receipts WHERE signal_id LIKE $1",
                f"%{suffix}%",
            )
            await connection.execute(
                "DELETE FROM evolution_learning_signals WHERE signal_id LIKE $1",
                f"%{suffix}%",
            )
            await connection.execute(
                "DELETE FROM evolution_control_state WHERE state_key LIKE $1",
                f"%{suffix}%",
            )
        finally:
            await connection.close()
