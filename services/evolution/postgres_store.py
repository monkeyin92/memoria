"""PostgreSQL persistence for the offline evolution control plane.

The public control plane is deliberately synchronous because it is also used by
CLI/offline curation.  The adapter opens a short-lived asyncpg connection per
operation and bridges that work through a thread only when called from an
already-running event loop.  Control API runtime capture already calls it in a
worker thread, so the request loop is never blocked by database I/O.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Callable, Coroutine, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import asyncpg

from services.evolution.account_fence import AccountWriteBlockedError
from services.evolution.domain import (
    CandidateArtifact,
    CandidateStatus,
    LearningSignal,
    LifecycleEvent,
    LifecycleEventType,
    SignalScope,
    ValidationReport,
    canonical_json,
)
from services.evolution.store import (
    _TRANSITIONS,
    MIN_STABLE_CANARY_OBSERVATIONS,
    EvolutionConflictError,
    EvolutionNotFoundError,
    EvolutionStore,
    EvolutionTransitionError,
    _candidate_account_id,
    _candidate_from_row,
    _candidate_json,
    _lifecycle_from_row,
    _state_key,
    _validation_from_dict,
    _validation_json,
    _verified_signal,
)

_T = TypeVar("_T")
_REQUIRED_EVOLUTION_TABLES = frozenset(
    {
        "evolution_learning_signals",
        "evolution_candidates",
        "evolution_validations",
        "evolution_activation_events",
        "evolution_lifecycle_events",
        "evolution_control_state",
        "evolution_account_deletion_fences",
        "evolution_sleep_signal_receipts",
    }
)


class PostgresEvolutionStore(EvolutionStore):
    """Production backend with the same lifecycle contract as ``EvolutionStore``."""

    def __init__(self, dsn: str, *, initialize_schema: bool = True) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("evolution DSN must use PostgreSQL")
        self._dsn = dsn
        self._initialize_schema = initialize_schema
        self._initialized = False
        self._initialize_lock = threading.RLock()

    def initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            self._run(self._initialize_async)
            self._initialized = True

    async def _initialize_async(self) -> None:
        connection = await asyncpg.connect(self._dsn, command_timeout=15)
        try:
            if self._initialize_schema:
                schema = Path(__file__).with_name("postgres_schema.sql").read_text(
                    encoding="utf-8"
                )
                await connection.execute(schema)
            else:
                rows = await connection.fetch(
                    """
                    SELECT relname, relrowsecurity, relforcerowsecurity
                    FROM pg_class
                    WHERE relnamespace = current_schema()::regnamespace
                      AND relname = ANY($1::text[])
                    """,
                    list(_REQUIRED_EVOLUTION_TABLES),
                )
                by_name = {str(row["relname"]): row for row in rows}
                if set(by_name) != _REQUIRED_EVOLUTION_TABLES or any(
                    not bool(row["relrowsecurity"])
                    or not bool(row["relforcerowsecurity"])
                    for row in by_name.values()
                ):
                    raise RuntimeError(
                        "production evolution schema and FORCE RLS must be installed before startup"
                    )
        finally:
            await connection.close()

    def close(self) -> None:
        """Connections are short-lived; retained for lifecycle symmetry."""

    def healthcheck(self) -> None:
        """Verify connectivity, schema and controller privileges in constant space."""

        self.initialize()

        async def operation() -> None:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                await connection.fetchval("SELECT 1 FROM evolution_control_state LIMIT 1")
            finally:
                await connection.close()

        self._run(operation)

    def mark_account_deleting(
        self,
        account_id: str,
        *,
        started_at: datetime | None = None,
    ) -> None:
        """Persist the cross-process owner-private deletion fence."""

        if not account_id.strip() or len(account_id) > 128:
            raise ValueError("evolution account id is invalid")
        timestamp = started_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("account deletion fence timestamp must be timezone-aware")
        self.initialize()

        async def operation() -> None:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        account_id,
                    )
                    await connection.execute(
                        """
                        INSERT INTO evolution_account_deletion_fences(account_id, started_at)
                        VALUES ($1, $2)
                        ON CONFLICT(account_id) DO NOTHING
                        """,
                        account_id,
                        timestamp.astimezone(UTC),
                    )
            finally:
                await connection.close()

        self._run(operation)

    def is_account_deleting(self, account_id: str) -> bool:
        if not account_id.strip() or len(account_id) > 128:
            raise ValueError("evolution account id is invalid")
        self.initialize()

        async def operation() -> bool:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                return bool(
                    await connection.fetchval(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM evolution_account_deletion_fences
                            WHERE account_id = $1
                        )
                        """,
                        account_id,
                    )
                )
            finally:
                await connection.close()

        return bool(self._run(operation))

    def assert_account_writable(self, account_id: str) -> None:
        if self.is_account_deleting(account_id):
            raise AccountWriteBlockedError("account deletion is in progress")

    def append_signal(self, signal: LearningSignal) -> LearningSignal:
        self.initialize()
        payload = canonical_json(signal.to_dict())
        evaluation_key = signal.trajectory_evaluation_key

        async def operation() -> LearningSignal:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    if evaluation_key is not None:
                        duplicate = await connection.fetchval(
                            """
                            SELECT signal_id FROM evolution_learning_signals
                            WHERE payload->'source_event_ids'->>0 = $1
                              AND payload->'source_event_ids'->>1 = $2
                              AND payload->'artifact_versions'->>'trajectory_evaluator' = $3
                            LIMIT 1
                            """,
                            *evaluation_key,
                        )
                        if duplicate is not None and str(duplicate) != signal.signal_id:
                            raise EvolutionConflictError(
                                "canonical trajectory pair was already evaluated by this evaluator version"
                            )
                    inserted = await connection.fetchrow(
                        """
                        INSERT INTO evolution_learning_signals(
                            signal_id, task_family, scope, account_id, payload, payload_hash, created_at
                        ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                        ON CONFLICT(signal_id) DO NOTHING
                        RETURNING payload_hash, payload::text AS payload_json
                        """,
                        signal.signal_id,
                        signal.task_family,
                        signal.scope,
                        signal.account_id,
                        payload,
                        signal.payload_hash,
                        signal.created_at.astimezone(UTC),
                    )
                    if inserted is not None:
                        return signal
                    current = await connection.fetchrow(
                        "SELECT payload_hash, payload::text AS payload_json "
                        "FROM evolution_learning_signals WHERE signal_id = $1 FOR SHARE",
                        signal.signal_id,
                    )
                    if current is None:
                        raise RuntimeError("conflicting learning signal disappeared")
                    if str(current["payload_hash"]) != signal.payload_hash:
                        raise EvolutionConflictError("learning signal id is immutable")
                    return _verified_signal(
                        _json_object(current["payload_json"]),
                        str(current["payload_hash"]),
                    )
            except asyncpg.exceptions.UniqueViolationError as exc:
                if evaluation_key is not None and "idx_evolution_signals_trajectory_evaluation" in str(
                    exc
                ):
                    raise EvolutionConflictError(
                        "canonical trajectory pair was already evaluated by this evaluator version"
                    ) from exc
                raise
            finally:
                await connection.close()

        return self._run(operation)

    def get_signal(self, signal_id: str) -> LearningSignal:
        self.initialize()

        async def operation() -> LearningSignal:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                row = await connection.fetchrow(
                    "SELECT payload::text AS payload_json, payload_hash "
                    "FROM evolution_learning_signals "
                    "WHERE signal_id = $1",
                    signal_id,
                )
            finally:
                await connection.close()
            if row is None:
                raise EvolutionNotFoundError(signal_id)
            return _verified_signal(
                _json_object(row["payload_json"]),
                str(row["payload_hash"]),
            )

        return self._run(operation)

    def list_signals(
        self,
        *,
        task_family: str | None = None,
        scope: SignalScope | None = None,
        account_id: str | None = None,
        failed_only: bool = False,
    ) -> tuple[LearningSignal, ...]:
        self.initialize()

        async def operation() -> tuple[LearningSignal, ...]:
            clauses: list[str] = []
            values: list[str] = []
            if task_family is not None:
                values.append(task_family)
                clauses.append(f"task_family = ${len(values)}")
            if scope is not None:
                values.append(scope)
                clauses.append(f"scope = ${len(values)}")
            if account_id is not None:
                values.append(account_id)
                clauses.append(f"account_id = ${len(values)}")
            clauses.append(
                "(scope = 'global_redacted' OR NOT EXISTS ("
                "SELECT 1 FROM evolution_account_deletion_fences fence "
                "WHERE fence.account_id = evolution_learning_signals.account_id))"
            )
            predicate = " WHERE " + " AND ".join(clauses) if clauses else ""
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                rows = await connection.fetch(
                    "SELECT payload::text AS payload_json, payload_hash FROM evolution_learning_signals"
                    f"{predicate} ORDER BY created_at, signal_id",
                    *values,
                )
            finally:
                await connection.close()
            signals = tuple(
                _verified_signal(_json_object(row["payload_json"]), str(row["payload_hash"]))
                for row in rows
            )
            return tuple(signal for signal in signals if signal.failed) if failed_only else signals

        return self._run(operation)

    def unprocessed_signals(self) -> tuple[LearningSignal, ...]:
        self.initialize()

        async def operation() -> tuple[LearningSignal, ...]:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                rows = await connection.fetch(
                    """
                    SELECT signal.payload::text AS payload_json, signal.payload_hash
                    FROM evolution_learning_signals signal
                    LEFT JOIN evolution_sleep_signal_receipts receipt
                      ON receipt.signal_id = signal.signal_id
                    WHERE receipt.signal_id IS NULL
                      AND (
                          signal.scope = 'global_redacted'
                          OR NOT EXISTS (
                              SELECT 1 FROM evolution_account_deletion_fences fence
                              WHERE fence.account_id = signal.account_id
                          )
                      )
                    ORDER BY signal.created_at, signal.signal_id
                    """
                )
            finally:
                await connection.close()
            return tuple(
                _verified_signal(_json_object(row["payload_json"]), str(row["payload_hash"]))
                for row in rows
            )

        return self._run(operation)

    def mark_signals_processed(
        self,
        signal_ids: tuple[str, ...],
        *,
        processed_at: datetime | None = None,
    ) -> None:
        if len(set(signal_ids)) != len(signal_ids):
            raise ValueError("sleep receipt signal ids must be unique")
        timestamp = (processed_at or datetime.now(UTC)).astimezone(UTC)
        self.initialize()

        async def operation() -> None:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                await connection.executemany(
                    """
                    INSERT INTO evolution_sleep_signal_receipts(signal_id, processed_at)
                    VALUES ($1, $2)
                    ON CONFLICT(signal_id) DO NOTHING
                    """,
                    ((signal_id, timestamp) for signal_id in signal_ids),
                )
            finally:
                await connection.close()

        self._run(operation)

    def create_candidate(self, candidate: CandidateArtifact) -> CandidateArtifact:
        if candidate.status != "candidate":
            raise ValueError("new evolution artifacts must start as candidates")
        self.initialize()
        payload = _candidate_json(candidate)

        async def operation() -> CandidateArtifact:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    inserted = await connection.fetchrow(
                        """
                        INSERT INTO evolution_candidates(
                            candidate_id, task_family, kind, scope, account_id, version, status,
                            payload, artifact_hash, source_signal_ids, expected_behavior,
                            regression_guards, risk, trusted_root_sha256, reason,
                            created_at, updated_at
                        ) VALUES (
                            $1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10::jsonb, $11,
                            $12::jsonb, $13, $14, $15, $16, $17
                        )
                        ON CONFLICT(candidate_id) DO NOTHING
                        RETURNING """
                        + _CANDIDATE_COLUMNS,
                        candidate.candidate_id,
                        candidate.task_family,
                        candidate.kind,
                        candidate.scope,
                        candidate.account_id,
                        candidate.version,
                        candidate.status,
                        payload,
                        candidate.artifact_hash,
                        canonical_json(list(candidate.source_signal_ids)),
                        candidate.expected_behavior,
                        canonical_json(list(candidate.regression_guards)),
                        candidate.risk,
                        candidate.trusted_root_sha256,
                        candidate.reason[:500],
                        candidate.created_at.astimezone(UTC),
                        candidate.updated_at.astimezone(UTC),
                    )
                    if inserted is not None:
                        return _candidate_from_row(inserted)
                    current = await connection.fetchrow(
                        _CANDIDATE_SELECT + " WHERE candidate_id = $1 FOR SHARE",
                        candidate.candidate_id,
                    )
                    if current is None:
                        raise RuntimeError("conflicting evolution candidate disappeared")
                    if str(current["artifact_hash"]) != candidate.artifact_hash:
                        raise EvolutionConflictError("candidate id is immutable")
                    return _candidate_from_row(current)
            finally:
                await connection.close()

        return self._run(operation)

    def list_lifecycle_events(
        self,
        *,
        candidate_id: str | None = None,
    ) -> tuple[LifecycleEvent, ...]:
        self.initialize()

        async def operation() -> tuple[LifecycleEvent, ...]:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                if candidate_id is None:
                    rows = await connection.fetch(
                        "SELECT * FROM evolution_lifecycle_events ORDER BY sequence"
                    )
                else:
                    rows = await connection.fetch(
                        "SELECT * FROM evolution_lifecycle_events "
                        "WHERE candidate_id = $1 ORDER BY sequence",
                        candidate_id,
                    )
            finally:
                await connection.close()
            return tuple(_lifecycle_from_row(row) for row in rows)

        return self._run(operation)

    def get_candidate(self, candidate_id: str) -> CandidateArtifact:
        self.initialize()

        async def operation() -> CandidateArtifact:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                row = await connection.fetchrow(
                    _CANDIDATE_SELECT + " WHERE candidate_id = $1", candidate_id
                )
            finally:
                await connection.close()
            if row is None:
                raise EvolutionNotFoundError(candidate_id)
            return _candidate_from_row(row)

        return self._run(operation)

    def list_candidates(
        self,
        *,
        status: CandidateStatus | None = None,
        task_family: str | None = None,
        scope: SignalScope | None = None,
        account_id: str | None = None,
    ) -> tuple[CandidateArtifact, ...]:
        self.initialize()

        async def operation() -> tuple[CandidateArtifact, ...]:
            clauses: list[str] = []
            values: list[str] = []
            for column, value in (
                ("status", status),
                ("task_family", task_family),
                ("scope", scope),
                ("account_id", account_id),
            ):
                if value is not None:
                    values.append(value)
                    clauses.append(f"{column} = ${len(values)}")
            predicate = " WHERE " + " AND ".join(clauses) if clauses else ""
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                rows = await connection.fetch(
                    _CANDIDATE_SELECT + f"{predicate} ORDER BY created_at, candidate_id", *values
                )
            finally:
                await connection.close()
            return tuple(_candidate_from_row(row) for row in rows)

        return self._run(operation)

    def list_candidates_for_account(
        self,
        account_id: str,
        *,
        status: CandidateStatus | None = None,
        statuses: tuple[CandidateStatus, ...] | None = None,
        task_family: str | None = None,
    ) -> tuple[CandidateArtifact, ...]:
        """Return only global-redacted or one account's private candidates."""

        if status is not None:
            if statuses is not None:
                raise ValueError("candidate status and statuses are mutually exclusive")
            statuses = (status,)
        scoped_account_id = _candidate_account_id(account_id)
        if statuses is not None:
            if not statuses:
                return ()
            if any(status not in _TRANSITIONS for status in statuses):
                raise ValueError("candidate statuses are invalid")
        self.initialize()

        async def operation() -> tuple[CandidateArtifact, ...]:
            clauses: list[str] = []
            values: list[object] = []
            if statuses is not None:
                values.append(list(statuses))
                clauses.append(f"status = ANY(${len(values)}::text[])")
            values.append(scoped_account_id)
            clauses.append(
                "((scope = 'global_redacted' AND account_id IS NULL) "
                f"OR (scope = 'owner_private' AND account_id = ${len(values)}))"
            )
            if task_family is not None:
                values.append(task_family)
                clauses.append(f"task_family = ${len(values)}")
            predicate = " WHERE " + " AND ".join(clauses)
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                rows = await connection.fetch(
                    _CANDIDATE_SELECT + f"{predicate} ORDER BY created_at, candidate_id", *values
                )
            finally:
                await connection.close()
            return tuple(_candidate_from_row(row) for row in rows)

        return self._run(operation)

    def record_validation(self, report: ValidationReport) -> ValidationReport:
        self.initialize()
        payload = _validation_json(report)

        async def operation() -> ValidationReport:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    candidate = await connection.fetchval(
                        "SELECT candidate_id FROM evolution_candidates WHERE candidate_id = $1 FOR KEY SHARE",
                        report.candidate_id,
                    )
                    if candidate is None:
                        raise EvolutionNotFoundError(report.candidate_id)
                    inserted = await connection.fetchrow(
                        """
                        INSERT INTO evolution_validations(
                            validation_id, candidate_id, payload, passed, created_at
                        ) VALUES ($1, $2, $3::jsonb, $4, $5)
                        ON CONFLICT(validation_id) DO NOTHING
                        RETURNING payload::text AS payload_json
                        """,
                        report.validation_id,
                        report.candidate_id,
                        payload,
                        report.passed,
                        report.created_at.astimezone(UTC),
                    )
                    if inserted is not None:
                        return report
                    current = await connection.fetchrow(
                        "SELECT payload::text AS payload_json FROM evolution_validations "
                        "WHERE validation_id = $1 FOR SHARE",
                        report.validation_id,
                    )
                    if current is None:
                        raise RuntimeError("conflicting evolution validation disappeared")
                    if _json_object(current["payload_json"]) != _json_object(payload):
                        raise EvolutionConflictError("validation id is immutable")
                    return _validation_from_dict(_json_object(current["payload_json"]))
            finally:
                await connection.close()

        return self._run(operation)

    def latest_validation(self, candidate_id: str) -> ValidationReport | None:
        self.initialize()

        async def operation() -> ValidationReport | None:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                row = await connection.fetchrow(
                    "SELECT payload::text AS payload_json FROM evolution_validations "
                    "WHERE candidate_id = $1 ORDER BY created_at DESC, validation_id DESC LIMIT 1",
                    candidate_id,
                )
            finally:
                await connection.close()
            return None if row is None else _validation_from_dict(_json_object(row["payload_json"]))

        return self._run(operation)

    def transition_candidate(
        self,
        candidate_id: str,
        target: CandidateStatus,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> CandidateArtifact:
        self.initialize()
        updated_at = now or datetime.now(UTC)
        if updated_at.tzinfo is None:
            raise ValueError("candidate transition timestamp must be timezone-aware")

        async def operation() -> CandidateArtifact:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    current_row = await connection.fetchrow(
                        _CANDIDATE_SELECT + " WHERE candidate_id = $1 FOR UPDATE", candidate_id
                    )
                    if current_row is None:
                        raise EvolutionNotFoundError(candidate_id)
                    current = _candidate_from_row(current_row)
                    if target not in _TRANSITIONS[current.status]:
                        raise EvolutionTransitionError(
                            f"invalid transition {current.status}->{target}"
                        )
                    if target in {"validated", "canary", "stable"}:
                        validation_row = await connection.fetchrow(
                            "SELECT payload::text AS payload_json FROM evolution_validations "
                            "WHERE candidate_id = $1 ORDER BY created_at DESC, validation_id DESC LIMIT 1",
                            candidate_id,
                        )
                        validation = (
                            _validation_from_dict(_json_object(validation_row["payload_json"]))
                            if validation_row is not None
                            else None
                        )
                        if validation is None or not validation.passed:
                            raise EvolutionTransitionError(
                                "a passed validation report with failure replay, retention, "
                                "transfer and safety gates is required"
                            )
                    if target == "canary" and current.status != "validated":
                        raise EvolutionTransitionError("canary requires validated candidate")
                    if target == "stable" and current.status != "canary":
                        raise EvolutionTransitionError("stable requires canary candidate")
                    if target == "stable":
                        await connection.fetchval(
                            "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                            canonical_json(
                                [
                                    current.task_family,
                                    current.kind,
                                    current.scope,
                                    current.account_id,
                                ]
                            ),
                        )
                        metrics_row = await connection.fetchrow(
                            """
                            SELECT COUNT(*) AS total,
                                   COUNT(*) FILTER (WHERE activated) AS activated,
                                   COUNT(*) FILTER (WHERE activated AND adhered) AS adhered,
                                   COUNT(*) FILTER (WHERE activated AND outcome_passed) AS outcomes
                            FROM evolution_activation_events WHERE candidate_id = $1
                            """,
                            candidate_id,
                        )
                        assert metrics_row is not None
                        activated = int(metrics_row["activated"] or 0)
                        if (
                            activated < MIN_STABLE_CANARY_OBSERVATIONS
                            or int(metrics_row["adhered"] or 0) != activated
                            or int(metrics_row["outcomes"] or 0) != activated
                        ):
                            raise EvolutionTransitionError(
                                "stable requires at least three successful canary activations"
                            )
                        stable_peers = await connection.fetch(
                            _CANDIDATE_SELECT + " WHERE status = 'stable' AND candidate_id != $1"
                            " AND task_family = $2 AND kind = $3 AND scope = $4"
                            " AND account_id IS NOT DISTINCT FROM $5 FOR UPDATE",
                            candidate_id,
                            current.task_family,
                            current.kind,
                            current.scope,
                            current.account_id,
                        )
                        if any(int(peer["version"]) >= current.version for peer in stable_peers):
                            raise EvolutionTransitionError(
                                "stable promotion must be newer than the active artifact"
                            )
                        await connection.execute(
                            """
                            UPDATE evolution_candidates
                            SET status = 'retired', reason = $1, updated_at = $2
                            WHERE status = 'stable' AND candidate_id != $3
                              AND task_family = $4 AND kind = $5 AND scope = $6
                              AND account_id IS NOT DISTINCT FROM $7
                            """,
                            f"superseded_by:{candidate_id}"[:500],
                            updated_at.astimezone(UTC),
                            candidate_id,
                            current.task_family,
                            current.kind,
                            current.scope,
                            current.account_id,
                        )
                        supersede_reason = f"superseded_by:{candidate_id}"[:500]
                        for peer in stable_peers:
                            await _insert_lifecycle_event_pg(
                                connection,
                                candidate_id=str(peer["candidate_id"]),
                                event_type="supersede",
                                from_status="stable",
                                to_status="retired",
                                reason=supersede_reason,
                                related_candidate_id=candidate_id,
                                created_at=updated_at,
                            )
                    transition_reason = reason[:500]
                    row = await connection.fetchrow(
                        "UPDATE evolution_candidates SET status = $1, reason = $2, updated_at = $3 "
                        "WHERE candidate_id = $4 RETURNING " + _CANDIDATE_COLUMNS,
                        target,
                        transition_reason,
                        updated_at.astimezone(UTC),
                        candidate_id,
                    )
                    await _insert_lifecycle_event_pg(
                        connection,
                        candidate_id=candidate_id,
                        event_type="transition",
                        from_status=current.status,
                        to_status=target,
                        reason=transition_reason,
                        related_candidate_id=None,
                        created_at=updated_at,
                    )
            finally:
                await connection.close()
            assert row is not None
            return _candidate_from_row(row)

        return self._run(operation)

    def rollback_to(
        self,
        candidate_id: str,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> CandidateArtifact:
        updated_at = now or datetime.now(UTC)
        if updated_at.tzinfo is None:
            raise ValueError("candidate rollback timestamp must be timezone-aware")
        self.initialize()

        async def operation() -> CandidateArtifact:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    target_row = await connection.fetchrow(
                        _CANDIDATE_SELECT + " WHERE candidate_id = $1 FOR UPDATE", candidate_id
                    )
                    if target_row is None:
                        raise EvolutionNotFoundError(candidate_id)
                    target = _candidate_from_row(target_row)
                    if target.status != "retired":
                        raise EvolutionTransitionError(
                            "rollback target must currently be retired"
                        )
                    latest_retirement = await connection.fetchrow(
                        """
                        SELECT event_type, from_status, to_status
                        FROM evolution_lifecycle_events WHERE candidate_id = $1
                        ORDER BY sequence DESC LIMIT 1
                        """,
                        candidate_id,
                    )
                    if (
                        latest_retirement is None
                        or str(latest_retirement["event_type"]) != "supersede"
                        or str(latest_retirement["from_status"]) != "stable"
                        or str(latest_retirement["to_status"]) != "retired"
                    ):
                        raise EvolutionTransitionError(
                            "rollback target must currently be retired by supersede from stable"
                        )
                    cohort_key = canonical_json(
                        [target.task_family, target.kind, target.scope, target.account_id]
                    )
                    await connection.fetchval(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
                        cohort_key,
                    )
                    stable_rows = await connection.fetch(
                        _CANDIDATE_SELECT
                        + " WHERE status = 'stable' AND candidate_id != $1"
                        " AND task_family = $2 AND kind = $3 AND scope = $4"
                        " AND account_id IS NOT DISTINCT FROM $5 FOR UPDATE",
                        candidate_id,
                        target.task_family,
                        target.kind,
                        target.scope,
                        target.account_id,
                    )
                    if len(stable_rows) != 1:
                        raise EvolutionTransitionError(
                            "rollback requires exactly one current stable artifact in the cohort"
                        )
                    current = _candidate_from_row(stable_rows[0])
                    if current.trusted_root_sha256 != target.trusted_root_sha256:
                        raise EvolutionTransitionError(
                            "rollback target and current stable artifact must share the trusted root"
                        )
                    retire_reason = f"rollback_to:{candidate_id}"[:500]
                    restore_reason = (
                        reason.strip() or f"rollback_from:{current.candidate_id}"
                    )[:500]
                    retired = await connection.execute(
                        """
                        UPDATE evolution_candidates
                        SET status = 'retired', reason = $1, updated_at = $2
                        WHERE candidate_id = $3 AND status = 'stable'
                        """,
                        retire_reason,
                        updated_at.astimezone(UTC),
                        current.candidate_id,
                    )
                    if not retired.endswith(" 1"):
                        raise EvolutionTransitionError(
                            "current stable artifact changed during rollback"
                        )
                    await _insert_lifecycle_event_pg(
                        connection,
                        candidate_id=current.candidate_id,
                        event_type="rollback_retire",
                        from_status="stable",
                        to_status="retired",
                        reason=retire_reason,
                        related_candidate_id=candidate_id,
                        created_at=updated_at,
                    )
                    restored = await connection.execute(
                        """
                        UPDATE evolution_candidates
                        SET status = 'stable', reason = $1, updated_at = $2
                        WHERE candidate_id = $3 AND status = 'retired'
                        """,
                        restore_reason,
                        updated_at.astimezone(UTC),
                        candidate_id,
                    )
                    if not restored.endswith(" 1"):
                        raise EvolutionTransitionError("rollback target changed during rollback")
                    await _insert_lifecycle_event_pg(
                        connection,
                        candidate_id=candidate_id,
                        event_type="rollback_restore",
                        from_status="retired",
                        to_status="stable",
                        reason=restore_reason,
                        related_candidate_id=current.candidate_id,
                        created_at=updated_at,
                    )
                    row = await connection.fetchrow(
                        _CANDIDATE_SELECT + " WHERE candidate_id = $1", candidate_id
                    )
            finally:
                await connection.close()
            assert row is not None
            return _candidate_from_row(row)

        return self._run(operation)

    def record_activation(
        self,
        *,
        candidate_id: str,
        task_id: str,
        activated: bool,
        adhered: bool,
        outcome_passed: bool,
        evidence_event_id: str,
        created_at: datetime | None = None,
    ) -> str:
        if not task_id.strip() or len(task_id) > 128:
            raise ValueError("activation task id is invalid")
        if not evidence_event_id.strip() or len(evidence_event_id) > 128:
            raise ValueError("activation evidence event id is invalid")
        if not activated and (adhered or outcome_passed):
            raise ValueError("inactive candidates cannot be adhered or outcome-passing")
        activation_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"memoria:evolution:activation:{candidate_id}:{evidence_event_id}",
            )
        )
        timestamp = created_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("activation timestamp must be timezone-aware")
        self.initialize()

        async def operation() -> str:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    expected = (
                        candidate_id,
                        task_id,
                        activated,
                        adhered,
                        outcome_passed,
                        evidence_event_id,
                    )
                    current = await connection.fetchrow(
                        """
                        SELECT candidate_id, task_id, activated, adhered,
                               outcome_passed, evidence_event_id
                        FROM evolution_activation_events
                        WHERE activation_id = $1 FOR SHARE
                        """,
                        uuid.UUID(activation_id),
                    )
                    if current is None:
                        current = await connection.fetchrow(
                            """
                            SELECT candidate_id, task_id, activated, adhered,
                                   outcome_passed, evidence_event_id
                            FROM evolution_activation_events
                            WHERE candidate_id = $1 AND evidence_event_id = $2
                            FOR SHARE
                            """,
                            candidate_id,
                            evidence_event_id,
                        )
                    if current is not None:
                        actual = (
                            str(current["candidate_id"]),
                            str(current["task_id"]),
                            bool(current["activated"]),
                            bool(current["adhered"]),
                            bool(current["outcome_passed"]),
                            str(current["evidence_event_id"]),
                        )
                        if actual != expected:
                            raise EvolutionConflictError("activation id is immutable")
                        return activation_id
                    status = await connection.fetchval(
                        "SELECT status FROM evolution_candidates "
                        "WHERE candidate_id = $1 FOR KEY SHARE",
                        candidate_id,
                    )
                    if status is None:
                        raise EvolutionNotFoundError(candidate_id)
                    if status not in {"canary", "stable"}:
                        raise EvolutionTransitionError(
                            "activation telemetry requires canary or stable status"
                        )
                    inserted = await connection.fetchrow(
                        """
                        INSERT INTO evolution_activation_events(
                            activation_id, candidate_id, task_id, activated, adhered,
                            outcome_passed, evidence_event_id, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT DO NOTHING
                        RETURNING candidate_id, task_id, activated, adhered,
                                  outcome_passed, evidence_event_id
                        """,
                        uuid.UUID(activation_id),
                        candidate_id,
                        task_id,
                        activated,
                        adhered,
                        outcome_passed,
                        evidence_event_id,
                        timestamp.astimezone(UTC),
                    )
                    if inserted is not None:
                        return activation_id
                    current = await connection.fetchrow(
                        """
                        SELECT candidate_id, task_id, activated, adhered,
                               outcome_passed, evidence_event_id
                        FROM evolution_activation_events
                        WHERE candidate_id = $1 AND evidence_event_id = $2
                        FOR SHARE
                        """,
                        candidate_id,
                        evidence_event_id,
                    )
                    if current is None:
                        raise RuntimeError("conflicting evolution activation disappeared")
                    actual = (
                        str(current["candidate_id"]),
                        str(current["task_id"]),
                        bool(current["activated"]),
                        bool(current["adhered"]),
                        bool(current["outcome_passed"]),
                        str(current["evidence_event_id"]),
                    )
                    if actual != expected:
                        raise EvolutionConflictError("activation id is immutable")
                    return activation_id
            finally:
                await connection.close()

        return self._run(operation)

    def activation_metrics(self, candidate_id: str) -> dict[str, float]:
        self.get_candidate(candidate_id)
        self.initialize()

        async def operation() -> dict[str, float]:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                row = await connection.fetchrow(
                    """
                    SELECT COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE activated) AS activated,
                           COUNT(*) FILTER (WHERE activated AND adhered) AS adhered,
                           COUNT(*) FILTER (WHERE activated AND outcome_passed) AS outcomes
                    FROM evolution_activation_events WHERE candidate_id = $1
                    """,
                    candidate_id,
                )
            finally:
                await connection.close()
            assert row is not None
            total = int(row["total"] or 0)
            activated = int(row["activated"] or 0)
            return {
                "activation_rate": activated / total if total else 0.0,
                "adherence_rate": int(row["adhered"] or 0) / activated if activated else 0.0,
                "activation_outcome_rate": int(row["outcomes"] or 0) / activated
                if activated
                else 0.0,
                "activation_observations": float(total),
                "activated_observations": float(activated),
            }

        return self._run(operation)

    def curate(self, *, stale_before: datetime, now: datetime | None = None) -> dict[str, int]:
        if stale_before.tzinfo is None:
            raise ValueError("stale_before must be timezone-aware")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        self.initialize()

        async def operation() -> dict[str, int]:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                async with connection.transaction():
                    candidates = await connection.fetch(
                        "SELECT candidate_id, status FROM evolution_candidates "
                        "WHERE updated_at < $1 FOR UPDATE",
                        stale_before.astimezone(UTC),
                    )
                    rejected = retired = 0
                    for item in candidates:
                        status = cast(CandidateStatus, str(item["status"]))
                        if status == "candidate":
                            target: CandidateStatus = "rejected"
                            event_reason = "stale_candidate"
                            rejected += 1
                        elif status in {"validated", "canary", "stable"}:
                            target = "retired"
                            event_reason = "stale_artifact"
                            retired += 1
                        else:
                            continue
                        await connection.execute(
                            "UPDATE evolution_candidates SET status = $1, reason = $2, updated_at = $3 "
                            "WHERE candidate_id = $4",
                            target,
                            event_reason,
                            timestamp,
                            item["candidate_id"],
                        )
                        await _insert_lifecycle_event_pg(
                            connection,
                            candidate_id=str(item["candidate_id"]),
                            event_type="curate",
                            from_status=status,
                            to_status=target,
                            reason=event_reason,
                            related_candidate_id=None,
                            created_at=timestamp,
                        )
            finally:
                await connection.close()
            return {
                "rejected_candidates": int(rejected or 0),
                "retired_artifacts": int(retired or 0),
            }

        return self._run(operation)

    def get_control_state(self, state_key: str) -> dict[str, object] | None:
        _state_key(state_key)
        self.initialize()

        async def operation() -> dict[str, object] | None:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                row = await connection.fetchrow(
                    "SELECT value::text AS value_json FROM evolution_control_state "
                    "WHERE state_key = $1",
                    state_key,
                )
            finally:
                await connection.close()
            return None if row is None else cast(dict[str, object], _json_object(row["value_json"]))

        return self._run(operation)

    def set_control_state(
        self,
        state_key: str,
        value: dict[str, object],
        *,
        updated_at: datetime | None = None,
    ) -> None:
        _state_key(state_key)
        timestamp = updated_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("control state timestamp must be timezone-aware")
        payload = canonical_json(value)
        self.initialize()

        async def operation() -> None:
            connection = await asyncpg.connect(self._dsn, command_timeout=15)
            try:
                await connection.execute(
                    """
                    INSERT INTO evolution_control_state(state_key, value, updated_at)
                    VALUES ($1, $2::jsonb, $3)
                    ON CONFLICT(state_key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = excluded.updated_at
                    """,
                    state_key,
                    payload,
                    timestamp.astimezone(UTC),
                )
            finally:
                await connection.close()

        self._run(operation)

    def _run(self, operation: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(operation())
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(operation())).result()


_CANDIDATE_COLUMNS = """
candidate_id, task_family, kind, scope, account_id, version, status,
payload::text AS payload_json, artifact_hash, source_signal_ids::text AS source_signal_ids_json,
expected_behavior, regression_guards::text AS regression_guards_json, risk, trusted_root_sha256,
reason,
created_at::text AS created_at, updated_at::text AS updated_at
"""
_CANDIDATE_SELECT = "SELECT " + _CANDIDATE_COLUMNS + " FROM evolution_candidates"


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        decoded = json.loads(value)
    elif isinstance(value, Mapping):
        decoded = dict(value)
    else:
        raise ValueError("evolution PostgreSQL payload is not an object")
    if not isinstance(decoded, dict):
        raise ValueError("evolution PostgreSQL payload is not an object")
    return cast(dict[str, Any], decoded)


async def _insert_lifecycle_event_pg(
    connection: asyncpg.Connection,
    *,
    candidate_id: str,
    event_type: LifecycleEventType,
    from_status: CandidateStatus,
    to_status: CandidateStatus,
    reason: str,
    related_candidate_id: str | None,
    created_at: datetime,
) -> None:
    await connection.execute(
        """
        INSERT INTO evolution_lifecycle_events(
            event_id, candidate_id, event_type, from_status, to_status,
            reason, related_candidate_id, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        uuid.uuid4(),
        candidate_id,
        event_type,
        from_status,
        to_status,
        reason[:500],
        related_candidate_id,
        created_at.astimezone(UTC),
    )


__all__ = ["PostgresEvolutionStore"]
