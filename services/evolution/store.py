"""SQLite persistence for immutable learning signals and candidate lifecycles."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from services.evolution.account_fence import AccountWriteBlockedError
from services.evolution.domain import (
    CandidateArtifact,
    CandidateStatus,
    EvidenceRef,
    FenceSnapshot,
    GateResult,
    LayerVerdict,
    LearningSignal,
    LifecycleEvent,
    LifecycleEventType,
    SignalScope,
    SpeakerSnapshot,
    ValidationReport,
    Verdict,
    canonical_json,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evolution_learning_signals (
    signal_id TEXT PRIMARY KEY,
    task_family TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('owner_private', 'global_redacted')),
    account_id TEXT,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK ((scope = 'owner_private' AND account_id IS NOT NULL)
        OR (scope = 'global_redacted' AND account_id IS NULL))
);

CREATE TABLE IF NOT EXISTS evolution_candidates (
    candidate_id TEXT PRIMARY KEY,
    task_family TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('knowledge', 'prompt', 'skill', 'harness', 'parameter')),
    scope TEXT NOT NULL CHECK (scope IN ('owner_private', 'global_redacted')),
    account_id TEXT,
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'validated', 'canary', 'stable', 'rejected', 'retired')),
    payload_json TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    source_signal_ids_json TEXT NOT NULL,
    expected_behavior TEXT NOT NULL,
    regression_guards_json TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    trusted_root_sha256 TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((scope = 'owner_private' AND account_id IS NOT NULL)
        OR (scope = 'global_redacted' AND account_id IS NULL))
);

CREATE TABLE IF NOT EXISTS evolution_validations (
    validation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES evolution_candidates(candidate_id)
);

CREATE TABLE IF NOT EXISTS evolution_activation_events (
    activation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    activated INTEGER NOT NULL CHECK (activated IN (0, 1)),
    adhered INTEGER NOT NULL CHECK (adhered IN (0, 1)),
    outcome_passed INTEGER NOT NULL CHECK (outcome_passed IN (0, 1)),
    evidence_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES evolution_candidates(candidate_id)
);

CREATE TABLE IF NOT EXISTS evolution_lifecycle_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'transition', 'supersede', 'curate', 'rollback_retire', 'rollback_restore'
    )),
    from_status TEXT NOT NULL CHECK (from_status IN (
        'candidate', 'validated', 'canary', 'stable', 'rejected', 'retired'
    )),
    to_status TEXT NOT NULL CHECK (to_status IN (
        'candidate', 'validated', 'canary', 'stable', 'rejected', 'retired'
    )),
    reason TEXT NOT NULL DEFAULT '',
    related_candidate_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES evolution_candidates(candidate_id),
    FOREIGN KEY (related_candidate_id) REFERENCES evolution_candidates(candidate_id)
);

CREATE TABLE IF NOT EXISTS evolution_control_state (
    state_key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- A deletion fence is durable and intentionally retained after erasure.  It
-- prevents a restarted worker from recreating owner-private material for an
-- account whose identity is no longer valid.
CREATE TABLE IF NOT EXISTS evolution_account_deletion_fences (
    account_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_sleep_signal_receipts (
    signal_id TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES evolution_learning_signals(signal_id)
);

CREATE INDEX IF NOT EXISTS idx_evolution_deletion_fences_started
ON evolution_account_deletion_fences(started_at);

CREATE INDEX IF NOT EXISTS idx_evolution_signals_family
ON evolution_learning_signals(task_family, created_at);

-- A pair may be reviewed by multiple evaluator versions, but one evaluator
-- version must not manufacture multiple support rows by changing evaluation_id.
CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_signals_trajectory_evaluation
ON evolution_learning_signals(
    json_extract(payload_json, '$.source_event_ids[0]'),
    json_extract(payload_json, '$.source_event_ids[1]'),
    json_extract(payload_json, '$.artifact_versions.trajectory_evaluator')
)
WHERE json_valid(payload_json)
  AND json_array_length(payload_json, '$.source_event_ids') = 2
  AND json_type(payload_json, '$.artifact_versions.trajectory_evaluator') = 'text';

CREATE INDEX IF NOT EXISTS idx_evolution_candidates_status
ON evolution_candidates(status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_candidates_one_stable
ON evolution_candidates(task_family, kind, scope, COALESCE(account_id, ''))
WHERE status = 'stable';

CREATE INDEX IF NOT EXISTS idx_evolution_validations_candidate
ON evolution_validations(candidate_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_activation_candidate_evidence
ON evolution_activation_events(candidate_id, evidence_event_id);

CREATE INDEX IF NOT EXISTS idx_evolution_lifecycle_candidate
ON evolution_lifecycle_events(candidate_id, sequence);

CREATE TRIGGER IF NOT EXISTS evolution_learning_signals_no_update
BEFORE UPDATE ON evolution_learning_signals
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

DROP TRIGGER IF EXISTS evolution_learning_signals_scope_guard;
CREATE TRIGGER evolution_learning_signals_scope_guard
BEFORE INSERT ON evolution_learning_signals
WHEN NOT ((NEW.scope = 'owner_private' AND NEW.account_id IS NOT NULL)
       OR (NEW.scope = 'global_redacted' AND NEW.account_id IS NULL))
BEGIN
    SELECT RAISE(ABORT, 'evolution signal scope/account mismatch');
END;

DROP TRIGGER IF EXISTS evolution_learning_signals_deletion_fence;
CREATE TRIGGER evolution_learning_signals_deletion_fence
BEFORE INSERT ON evolution_learning_signals
WHEN NEW.scope = 'owner_private'
 AND evolution_account_deletion_authorized() = 0
 AND EXISTS (
     SELECT 1 FROM evolution_account_deletion_fences
     WHERE account_id = NEW.account_id
 )
BEGIN
    SELECT RAISE(ABORT, 'owner-private evolution write blocked by account deletion');
END;

CREATE TRIGGER IF NOT EXISTS evolution_learning_signals_no_delete
BEFORE DELETE ON evolution_learning_signals
WHEN evolution_account_deletion_authorized() = 0
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

CREATE TRIGGER IF NOT EXISTS evolution_candidates_no_delete
BEFORE DELETE ON evolution_candidates
WHEN evolution_account_deletion_authorized() = 0
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

DROP TRIGGER IF EXISTS evolution_candidates_scope_guard;
CREATE TRIGGER evolution_candidates_scope_guard
BEFORE INSERT ON evolution_candidates
WHEN NOT ((NEW.scope = 'owner_private' AND NEW.account_id IS NOT NULL)
       OR (NEW.scope = 'global_redacted' AND NEW.account_id IS NULL))
BEGIN
    SELECT RAISE(ABORT, 'evolution candidate scope/account mismatch');
END;

DROP TRIGGER IF EXISTS evolution_candidates_deletion_fence_insert;
CREATE TRIGGER evolution_candidates_deletion_fence_insert
BEFORE INSERT ON evolution_candidates
WHEN NEW.scope = 'owner_private'
 AND evolution_account_deletion_authorized() = 0
 AND EXISTS (
     SELECT 1 FROM evolution_account_deletion_fences
     WHERE account_id = NEW.account_id
 )
BEGIN
    SELECT RAISE(ABORT, 'owner-private evolution write blocked by account deletion');
END;

DROP TRIGGER IF EXISTS evolution_candidates_deletion_fence_update;
CREATE TRIGGER evolution_candidates_deletion_fence_update
BEFORE UPDATE ON evolution_candidates
WHEN NEW.scope = 'owner_private'
 AND evolution_account_deletion_authorized() = 0
 AND EXISTS (
     SELECT 1 FROM evolution_account_deletion_fences
     WHERE account_id = NEW.account_id
 )
BEGIN
    SELECT RAISE(ABORT, 'owner-private evolution write blocked by account deletion');
END;

DROP TRIGGER IF EXISTS evolution_candidate_manifest_guard;
CREATE TRIGGER evolution_candidate_manifest_guard
BEFORE UPDATE ON evolution_candidates
WHEN NEW.candidate_id <> OLD.candidate_id
  OR NEW.task_family <> OLD.task_family
  OR NEW.kind <> OLD.kind
  OR NEW.scope <> OLD.scope
  OR NEW.account_id IS NOT OLD.account_id
  OR NEW.version <> OLD.version
  OR NEW.payload_json <> OLD.payload_json
  OR NEW.artifact_hash <> OLD.artifact_hash
  OR NEW.source_signal_ids_json <> OLD.source_signal_ids_json
  OR NEW.expected_behavior <> OLD.expected_behavior
  OR NEW.regression_guards_json <> OLD.regression_guards_json
  OR NEW.risk <> OLD.risk
  OR NEW.trusted_root_sha256 <> OLD.trusted_root_sha256
  OR NEW.created_at <> OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'evolution candidate manifest is immutable');
END;

CREATE TRIGGER IF NOT EXISTS evolution_validations_no_update
BEFORE UPDATE ON evolution_validations
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

CREATE TRIGGER IF NOT EXISTS evolution_validations_no_delete
BEFORE DELETE ON evolution_validations
WHEN evolution_account_deletion_authorized() = 0
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

DROP TRIGGER IF EXISTS evolution_validations_deletion_fence;
CREATE TRIGGER evolution_validations_deletion_fence
BEFORE INSERT ON evolution_validations
WHEN evolution_account_deletion_authorized() = 0
 AND EXISTS (
     SELECT 1
     FROM evolution_candidates candidate
     JOIN evolution_account_deletion_fences fence
       ON fence.account_id = candidate.account_id
     WHERE candidate.candidate_id = NEW.candidate_id
       AND candidate.scope = 'owner_private'
 )
BEGIN
    SELECT RAISE(ABORT, 'owner-private evolution write blocked by account deletion');
END;

CREATE TRIGGER IF NOT EXISTS evolution_activation_events_no_update
BEFORE UPDATE ON evolution_activation_events
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

CREATE TRIGGER IF NOT EXISTS evolution_activation_events_no_delete
BEFORE DELETE ON evolution_activation_events
WHEN evolution_account_deletion_authorized() = 0
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

DROP TRIGGER IF EXISTS evolution_activation_events_deletion_fence;
CREATE TRIGGER evolution_activation_events_deletion_fence
BEFORE INSERT ON evolution_activation_events
WHEN evolution_account_deletion_authorized() = 0
 AND EXISTS (
     SELECT 1
     FROM evolution_candidates candidate
     JOIN evolution_account_deletion_fences fence
       ON fence.account_id = candidate.account_id
     WHERE candidate.candidate_id = NEW.candidate_id
       AND candidate.scope = 'owner_private'
 )
BEGIN
    SELECT RAISE(ABORT, 'owner-private evolution write blocked by account deletion');
END;

DROP TRIGGER IF EXISTS evolution_lifecycle_events_no_update;
CREATE TRIGGER evolution_lifecycle_events_no_update
BEFORE UPDATE ON evolution_lifecycle_events
BEGIN
    SELECT RAISE(ABORT, 'evolution lifecycle audit is append-only');
END;

DROP TRIGGER IF EXISTS evolution_lifecycle_events_no_delete;
CREATE TRIGGER evolution_lifecycle_events_no_delete
BEFORE DELETE ON evolution_lifecycle_events
WHEN evolution_account_deletion_authorized() = 0
BEGIN
    SELECT RAISE(ABORT, 'evolution lifecycle audit is append-only');
END;

DROP TRIGGER IF EXISTS evolution_lifecycle_events_deletion_fence;
CREATE TRIGGER evolution_lifecycle_events_deletion_fence
BEFORE INSERT ON evolution_lifecycle_events
WHEN evolution_account_deletion_authorized() = 0
 AND EXISTS (
     SELECT 1
     FROM evolution_candidates candidate
     JOIN evolution_account_deletion_fences fence
       ON fence.account_id = candidate.account_id
     WHERE candidate.candidate_id = NEW.candidate_id
       AND candidate.scope = 'owner_private'
 )
BEGIN
    SELECT RAISE(ABORT, 'owner-private evolution write blocked by account deletion');
END;

CREATE TRIGGER IF NOT EXISTS evolution_sleep_signal_receipts_no_update
BEFORE UPDATE ON evolution_sleep_signal_receipts
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

CREATE TRIGGER IF NOT EXISTS evolution_sleep_signal_receipts_no_delete
BEFORE DELETE ON evolution_sleep_signal_receipts
WHEN evolution_account_deletion_authorized() = 0
BEGIN
    SELECT RAISE(ABORT, 'evolution evidence is append-only');
END;

DROP TRIGGER IF EXISTS evolution_sleep_signal_receipts_deletion_fence;
CREATE TRIGGER evolution_sleep_signal_receipts_deletion_fence
BEFORE INSERT ON evolution_sleep_signal_receipts
WHEN evolution_account_deletion_authorized() = 0
 AND EXISTS (
     SELECT 1
     FROM evolution_learning_signals signal
     JOIN evolution_account_deletion_fences fence
       ON fence.account_id = signal.account_id
     WHERE signal.signal_id = NEW.signal_id
       AND signal.scope = 'owner_private'
 )
BEGIN
    SELECT RAISE(ABORT, 'owner-private evolution write blocked by account deletion');
END;
"""

_TRANSITIONS: dict[CandidateStatus, frozenset[CandidateStatus]] = {
    "candidate": frozenset({"validated", "rejected", "retired"}),
    "validated": frozenset({"canary", "rejected", "retired"}),
    "canary": frozenset({"stable", "rejected", "retired"}),
    "stable": frozenset({"retired"}),
    "rejected": frozenset(),
    "retired": frozenset(),
}
MIN_STABLE_CANARY_OBSERVATIONS = 3


class EvolutionConflictError(RuntimeError):
    """An immutable signal/candidate was submitted with different content."""


class EvolutionNotFoundError(LookupError):
    """A signal, candidate or validation does not exist in the scoped store."""


class EvolutionTransitionError(RuntimeError):
    """A candidate lifecycle transition violates its release gates."""


class EvolutionStore:
    """A small durable store suitable for offline curation and local harnesses."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self.initialize()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def healthcheck(self) -> None:
        """Verify the store schema without materializing evolution evidence."""

        with self._lock, self._connect() as connection:
            connection.execute("SELECT 1 FROM evolution_control_state LIMIT 1").fetchone()

    def mark_account_deleting(
        self,
        account_id: str,
        *,
        started_at: datetime | None = None,
    ) -> None:
        """Persist an irreversible owner-private deletion fence.

        The fence is deliberately idempotent and is never removed.  SQLite
        triggers enforce it for direct store callers; the control plane also
        checks it before doing expensive validation work.
        """

        account_id = _candidate_account_id(account_id)
        timestamp = started_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("account deletion fence timestamp must be timezone-aware")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evolution_account_deletion_fences(account_id, started_at)
                VALUES (?, ?)
                ON CONFLICT(account_id) DO NOTHING
                """,
                (account_id, timestamp.astimezone(UTC).isoformat()),
            )

    def is_account_deleting(self, account_id: str) -> bool:
        account_id = _candidate_account_id(account_id)
        with self._lock, self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM evolution_account_deletion_fences WHERE account_id = ?",
                    (account_id,),
                ).fetchone()
                is not None
            )

    def assert_account_writable(self, account_id: str) -> None:
        if self.is_account_deleting(account_id):
            raise AccountWriteBlockedError("account deletion is in progress")

    def append_signal(self, signal: LearningSignal) -> LearningSignal:
        payload = canonical_json(signal.to_dict())
        evaluation_key = signal.trajectory_evaluation_key
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT payload_hash, payload_json FROM evolution_learning_signals WHERE signal_id = ?",
                (signal.signal_id,),
            ).fetchone()
            if current is not None:
                if str(current["payload_hash"]) != signal.payload_hash:
                    raise EvolutionConflictError("learning signal id is immutable")
                return _verified_signal(
                    json.loads(str(current["payload_json"])),
                    str(current["payload_hash"]),
                )
            if evaluation_key is not None:
                duplicate = connection.execute(
                    """
                    SELECT signal_id FROM evolution_learning_signals
                    WHERE json_extract(payload_json, '$.source_event_ids[0]') = ?
                      AND json_extract(payload_json, '$.source_event_ids[1]') = ?
                      AND json_extract(payload_json, '$.artifact_versions.trajectory_evaluator') = ?
                    LIMIT 1
                    """,
                    evaluation_key,
                ).fetchone()
                if duplicate is not None:
                    raise EvolutionConflictError(
                        "canonical trajectory pair was already evaluated by this evaluator version"
                    )
            try:
                connection.execute(
                    """
                    INSERT INTO evolution_learning_signals(
                        signal_id, task_family, scope, account_id, payload_json, payload_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signal.signal_id,
                        signal.task_family,
                        signal.scope,
                        signal.account_id,
                        payload,
                        signal.payload_hash,
                        signal.created_at.astimezone(UTC).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if evaluation_key is not None and "idx_evolution_signals_trajectory_evaluation" in str(
                    exc
                ):
                    raise EvolutionConflictError(
                        "canonical trajectory pair was already evaluated by this evaluator version"
                    ) from exc
                raise
        return signal

    def get_signal(self, signal_id: str) -> LearningSignal:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, payload_hash FROM evolution_learning_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        if row is None:
            raise EvolutionNotFoundError(signal_id)
        return _verified_signal(
            json.loads(str(row["payload_json"])),
            str(row["payload_hash"]),
        )

    def list_signals(
        self,
        *,
        task_family: str | None = None,
        scope: SignalScope | None = None,
        account_id: str | None = None,
        failed_only: bool = False,
    ) -> tuple[LearningSignal, ...]:
        clauses: list[str] = []
        values: list[str] = []
        if task_family is not None:
            clauses.append("task_family = ?")
            values.append(task_family)
        if scope is not None:
            clauses.append("scope = ?")
            values.append(scope)
        if account_id is not None:
            clauses.append("account_id = ?")
            values.append(account_id)
        clauses.append(
            "(scope = 'global_redacted' OR NOT EXISTS ("
            "SELECT 1 FROM evolution_account_deletion_fences fence "
            "WHERE fence.account_id = evolution_learning_signals.account_id))"
        )
        predicate = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json, payload_hash FROM evolution_learning_signals{predicate} "
                "ORDER BY created_at, signal_id",
                values,
            ).fetchall()
        signals = tuple(
            _verified_signal(json.loads(str(row["payload_json"])), str(row["payload_hash"]))
            for row in rows
        )
        if failed_only:
            return tuple(signal for signal in signals if signal.failed)
        return signals

    def unprocessed_signals(self) -> tuple[LearningSignal, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT signal.payload_json, signal.payload_hash
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
            ).fetchall()
        return tuple(
            _verified_signal(json.loads(str(row["payload_json"])), str(row["payload_hash"]))
            for row in rows
        )

    def mark_signals_processed(
        self,
        signal_ids: tuple[str, ...],
        *,
        processed_at: datetime | None = None,
    ) -> None:
        if len(set(signal_ids)) != len(signal_ids):
            raise ValueError("sleep receipt signal ids must be unique")
        timestamp = (processed_at or datetime.now(UTC)).astimezone(UTC)
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO evolution_sleep_signal_receipts(signal_id, processed_at)
                VALUES (?, ?)
                ON CONFLICT(signal_id) DO NOTHING
                """,
                ((signal_id, timestamp.isoformat()) for signal_id in signal_ids),
            )

    def create_candidate(self, candidate: CandidateArtifact) -> CandidateArtifact:
        if candidate.status != "candidate":
            raise ValueError("new evolution artifacts must start as candidates")
        payload = _candidate_json(candidate)
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            if current is not None:
                if str(current["artifact_hash"]) != candidate.artifact_hash:
                    raise EvolutionConflictError("candidate id is immutable")
                return _candidate_from_row(current)
            connection.execute(
                """
                INSERT INTO evolution_candidates(
                    candidate_id, task_family, kind, scope, account_id, version, status,
                    payload_json, artifact_hash, source_signal_ids_json, expected_behavior,
                    regression_guards_json, risk, trusted_root_sha256, reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                    candidate.created_at.astimezone(UTC).isoformat(),
                    candidate.updated_at.astimezone(UTC).isoformat(),
                ),
            )
        return candidate

    def get_candidate(self, candidate_id: str) -> CandidateArtifact:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise EvolutionNotFoundError(candidate_id)
        return _candidate_from_row(row)

    def list_candidates(
        self,
        *,
        status: CandidateStatus | None = None,
        task_family: str | None = None,
        scope: SignalScope | None = None,
        account_id: str | None = None,
    ) -> tuple[CandidateArtifact, ...]:
        clauses: list[str] = []
        values: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        if task_family is not None:
            clauses.append("task_family = ?")
            values.append(task_family)
        if scope is not None:
            clauses.append("scope = ?")
            values.append(scope)
        if account_id is not None:
            clauses.append("account_id = ?")
            values.append(account_id)
        predicate = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM evolution_candidates{predicate} ORDER BY created_at, candidate_id",
                values,
            ).fetchall()
        return tuple(_candidate_from_row(row) for row in rows)

    def list_candidates_for_account(
        self,
        account_id: str,
        *,
        status: CandidateStatus | None = None,
        statuses: tuple[CandidateStatus, ...] | None = None,
        task_family: str | None = None,
    ) -> tuple[CandidateArtifact, ...]:
        """Return only global-redacted or one account's private candidates.

        This is intentionally separate from the offline controller's broad
        ``list_candidates`` query.  Runtime consumers must never materialize
        another account's owner-private candidate before applying visibility
        checks in Python.
        """

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

        clauses = [
            "((scope = 'global_redacted' AND account_id IS NULL) "
            "OR (scope = 'owner_private' AND account_id = ?))"
        ]
        values: list[str] = [scoped_account_id]
        if statuses is not None:
            clauses.insert(0, "status IN (" + ", ".join("?" for _ in statuses) + ")")
            values = [*statuses, *values]
        if task_family is not None:
            clauses.append("task_family = ?")
            values.append(task_family)
        predicate = " WHERE " + " AND ".join(clauses)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM evolution_candidates{predicate} ORDER BY created_at, candidate_id",
                values,
            ).fetchall()
        return tuple(_candidate_from_row(row) for row in rows)

    def list_lifecycle_events(
        self,
        *,
        candidate_id: str | None = None,
    ) -> tuple[LifecycleEvent, ...]:
        predicate = " WHERE candidate_id = ?" if candidate_id is not None else ""
        values = (candidate_id,) if candidate_id is not None else ()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM evolution_lifecycle_events{predicate} ORDER BY sequence",
                values,
            ).fetchall()
        return tuple(_lifecycle_from_row(row) for row in rows)

    def record_validation(self, report: ValidationReport) -> ValidationReport:
        self.get_candidate(report.candidate_id)
        payload = _validation_json(report)
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT payload_json FROM evolution_validations WHERE validation_id = ?",
                (report.validation_id,),
            ).fetchone()
            if current is not None:
                if str(current["payload_json"]) != payload:
                    raise EvolutionConflictError("validation id is immutable")
                return _validation_from_dict(json.loads(str(current["payload_json"])))
            connection.execute(
                """
                INSERT INTO evolution_validations(
                    validation_id, candidate_id, payload_json, passed, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    report.validation_id,
                    report.candidate_id,
                    payload,
                    int(report.passed),
                    report.created_at.astimezone(UTC).isoformat(),
                ),
            )
        return report

    def latest_validation(self, candidate_id: str) -> ValidationReport | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM evolution_validations
                WHERE candidate_id = ? ORDER BY created_at DESC, validation_id DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
        return None if row is None else _validation_from_dict(json.loads(str(row["payload_json"])))

    def transition_candidate(
        self,
        candidate_id: str,
        target: CandidateStatus,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> CandidateArtifact:
        updated_at = now or datetime.now(UTC)
        if updated_at.tzinfo is None:
            raise ValueError("candidate transition timestamp must be timezone-aware")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if current_row is None:
                raise EvolutionNotFoundError(candidate_id)
            current = _candidate_from_row(current_row)
            if target not in _TRANSITIONS[current.status]:
                raise EvolutionTransitionError(f"invalid transition {current.status}->{target}")
            if target in {"validated", "canary", "stable"}:
                validation_row = connection.execute(
                    """
                    SELECT payload_json FROM evolution_validations
                    WHERE candidate_id = ?
                    ORDER BY created_at DESC, validation_id DESC LIMIT 1
                    """,
                    (candidate_id,),
                ).fetchone()
                validation = (
                    _validation_from_dict(json.loads(str(validation_row["payload_json"])))
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
                metrics_row = connection.execute(
                    """
                    SELECT SUM(activated) AS activated,
                           SUM(CASE WHEN activated = 1 AND adhered = 1 THEN 1 ELSE 0 END)
                               AS adhered,
                           SUM(CASE WHEN activated = 1 AND outcome_passed = 1 THEN 1 ELSE 0 END)
                               AS outcomes
                    FROM evolution_activation_events WHERE candidate_id = ?
                    """,
                    (candidate_id,),
                ).fetchone()
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
                stable_peers = connection.execute(
                    """
                    SELECT candidate_id, version FROM evolution_candidates
                    WHERE status = 'stable' AND candidate_id != ?
                      AND task_family = ? AND kind = ? AND scope = ?
                      AND (account_id = ? OR (account_id IS NULL AND ? IS NULL))
                    """,
                    (
                        candidate_id,
                        current.task_family,
                        current.kind,
                        current.scope,
                        current.account_id,
                        current.account_id,
                    ),
                ).fetchall()
                if any(int(peer["version"]) >= current.version for peer in stable_peers):
                    raise EvolutionTransitionError(
                        "stable promotion must be newer than the active artifact"
                    )
                for peer in stable_peers:
                    peer_id = str(peer["candidate_id"])
                    supersede_reason = f"superseded_by:{candidate_id}"[:500]
                    connection.execute(
                        """
                        UPDATE evolution_candidates
                        SET status = 'retired', reason = ?, updated_at = ?
                        WHERE candidate_id = ?
                        """,
                        (
                            supersede_reason,
                            updated_at.astimezone(UTC).isoformat(),
                            peer_id,
                        ),
                    )
                    _insert_lifecycle_event(
                        connection,
                        candidate_id=peer_id,
                        event_type="supersede",
                        from_status="stable",
                        to_status="retired",
                        reason=supersede_reason,
                        related_candidate_id=candidate_id,
                        created_at=updated_at,
                    )
            transition_reason = reason[:500]
            changed = connection.execute(
                """
                UPDATE evolution_candidates SET status = ?, reason = ?, updated_at = ?
                WHERE candidate_id = ? AND status = ?
                """,
                (
                    target,
                    transition_reason,
                    updated_at.astimezone(UTC).isoformat(),
                    candidate_id,
                    current.status,
                ),
            )
            if changed.rowcount != 1:
                raise EvolutionTransitionError("candidate status changed during transition")
            _insert_lifecycle_event(
                connection,
                candidate_id=candidate_id,
                event_type="transition",
                from_status=current.status,
                to_status=target,
                reason=transition_reason,
                related_candidate_id=None,
                created_at=updated_at,
            )
            row = connection.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        assert row is not None
        return _candidate_from_row(row)

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
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target_row = connection.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if target_row is None:
                raise EvolutionNotFoundError(candidate_id)
            target = _candidate_from_row(target_row)
            if target.status != "retired":
                raise EvolutionTransitionError("rollback target must currently be retired")
            latest_retirement = connection.execute(
                """
                SELECT event_type, from_status, to_status FROM evolution_lifecycle_events
                WHERE candidate_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            if (
                latest_retirement is None
                or str(latest_retirement["event_type"]) != "supersede"
                or str(latest_retirement["from_status"]) != "stable"
                or str(latest_retirement["to_status"]) != "retired"
            ):
                raise EvolutionTransitionError(
                    "rollback target must currently be retired by supersede from stable"
                )
            stable_rows = connection.execute(
                """
                SELECT * FROM evolution_candidates
                WHERE status = 'stable' AND candidate_id != ?
                  AND task_family = ? AND kind = ? AND scope = ?
                  AND (account_id = ? OR (account_id IS NULL AND ? IS NULL))
                """,
                (
                    candidate_id,
                    target.task_family,
                    target.kind,
                    target.scope,
                    target.account_id,
                    target.account_id,
                ),
            ).fetchall()
            if len(stable_rows) != 1:
                raise EvolutionTransitionError(
                    "rollback requires exactly one current stable artifact in the cohort"
                )
            current = _candidate_from_row(stable_rows[0])
            if current.trusted_root_sha256 != target.trusted_root_sha256:
                raise EvolutionTransitionError(
                    "rollback target and current stable artifact must share the trusted root"
                )
            timestamp = updated_at.astimezone(UTC).isoformat()
            retire_reason = f"rollback_to:{candidate_id}"[:500]
            restore_reason = (reason.strip() or f"rollback_from:{current.candidate_id}")[:500]
            retired = connection.execute(
                """
                UPDATE evolution_candidates SET status = 'retired', reason = ?, updated_at = ?
                WHERE candidate_id = ? AND status = 'stable'
                """,
                (retire_reason, timestamp, current.candidate_id),
            )
            if retired.rowcount != 1:
                raise EvolutionTransitionError("current stable artifact changed during rollback")
            _insert_lifecycle_event(
                connection,
                candidate_id=current.candidate_id,
                event_type="rollback_retire",
                from_status="stable",
                to_status="retired",
                reason=retire_reason,
                related_candidate_id=candidate_id,
                created_at=updated_at,
            )
            restored = connection.execute(
                """
                UPDATE evolution_candidates SET status = 'stable', reason = ?, updated_at = ?
                WHERE candidate_id = ? AND status = 'retired'
                """,
                (restore_reason, timestamp, candidate_id),
            )
            if restored.rowcount != 1:
                raise EvolutionTransitionError("rollback target changed during rollback")
            _insert_lifecycle_event(
                connection,
                candidate_id=candidate_id,
                event_type="rollback_restore",
                from_status="retired",
                to_status="stable",
                reason=restore_reason,
                related_candidate_id=current.candidate_id,
                created_at=updated_at,
            )
            row = connection.execute(
                "SELECT * FROM evolution_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        assert row is not None
        return _candidate_from_row(row)

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
        candidate = self.get_candidate(candidate_id)
        if candidate.status not in {"canary", "stable"}:
            raise EvolutionTransitionError("activation telemetry requires canary or stable status")
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
        now = created_at or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("activation timestamp must be timezone-aware")
        with self._lock, self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM evolution_activation_events WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
            if current is None:
                current = connection.execute(
                    """
                    SELECT * FROM evolution_activation_events
                    WHERE candidate_id = ? AND evidence_event_id = ?
                    """,
                    (candidate_id, evidence_event_id),
                ).fetchone()
            expected = (
                candidate_id,
                task_id,
                int(activated),
                int(adhered),
                int(outcome_passed),
                evidence_event_id,
            )
            if current is not None:
                actual = (
                    str(current["candidate_id"]),
                    str(current["task_id"]),
                    int(current["activated"]),
                    int(current["adhered"]),
                    int(current["outcome_passed"]),
                    str(current["evidence_event_id"]),
                )
                if actual != expected:
                    raise EvolutionConflictError("activation id is immutable")
                return activation_id
            connection.execute(
                """
                INSERT INTO evolution_activation_events(
                    activation_id, candidate_id, task_id, activated, adhered,
                    outcome_passed, evidence_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activation_id,
                    candidate_id,
                    task_id,
                    int(activated),
                    int(adhered),
                    int(outcome_passed),
                    evidence_event_id,
                    now.astimezone(UTC).isoformat(),
                ),
            )
        return activation_id

    def activation_metrics(self, candidate_id: str) -> dict[str, float]:
        self.get_candidate(candidate_id)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(activated) AS activated,
                       SUM(CASE WHEN activated = 1 AND adhered = 1 THEN 1 ELSE 0 END) AS adhered,
                       SUM(CASE WHEN activated = 1 AND outcome_passed = 1 THEN 1 ELSE 0 END) AS outcomes
                FROM evolution_activation_events WHERE candidate_id = ?
                """,
                (candidate_id,),
            ).fetchone()
        total = int(row["total"] or 0)
        activated = int(row["activated"] or 0)
        return {
            "activation_rate": activated / total if total else 0.0,
            "adherence_rate": int(row["adhered"] or 0) / activated if activated else 0.0,
            "activation_outcome_rate": int(row["outcomes"] or 0) / activated if activated else 0.0,
            "activation_observations": float(total),
            "activated_observations": float(activated),
        }

    def curate(self, *, stale_before: datetime, now: datetime | None = None) -> dict[str, int]:
        if stale_before.tzinfo is None:
            raise ValueError("stale_before must be timezone-aware")
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        stale = stale_before.astimezone(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidates = connection.execute(
                "SELECT candidate_id, status FROM evolution_candidates WHERE updated_at < ?",
                (stale,),
            ).fetchall()
            rejected = retired = 0
            for row in candidates:
                status = cast(CandidateStatus, str(row["status"]))
                if status == "candidate":
                    connection.execute(
                        "UPDATE evolution_candidates SET status = 'rejected', reason = ?, updated_at = ? WHERE candidate_id = ?",
                        ("stale_candidate", timestamp, row["candidate_id"]),
                    )
                    _insert_lifecycle_event(
                        connection,
                        candidate_id=str(row["candidate_id"]),
                        event_type="curate",
                        from_status="candidate",
                        to_status="rejected",
                        reason="stale_candidate",
                        related_candidate_id=None,
                        created_at=datetime.fromisoformat(timestamp),
                    )
                    rejected += 1
                elif status in {"validated", "canary", "stable"}:
                    connection.execute(
                        "UPDATE evolution_candidates SET status = 'retired', reason = ?, updated_at = ? WHERE candidate_id = ?",
                        ("stale_artifact", timestamp, row["candidate_id"]),
                    )
                    _insert_lifecycle_event(
                        connection,
                        candidate_id=str(row["candidate_id"]),
                        event_type="curate",
                        from_status=status,
                        to_status="retired",
                        reason="stale_artifact",
                        related_candidate_id=None,
                        created_at=datetime.fromisoformat(timestamp),
                    )
                    retired += 1
        return {"rejected_candidates": rejected, "retired_artifacts": retired}

    def get_control_state(self, state_key: str) -> dict[str, object] | None:
        _state_key(state_key)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM evolution_control_state WHERE state_key = ?",
                (state_key,),
            ).fetchone()
        if row is None:
            return None
        value = json.loads(str(row["value_json"]))
        if not isinstance(value, dict):
            raise ValueError("evolution control state must be an object")
        return cast(dict[str, object], value)

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
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evolution_control_state(state_key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (state_key, payload, timestamp.astimezone(UTC).isoformat()),
            )

    def _connect(self) -> sqlite3.Connection:
        return _connect_evolution_sqlite(self._path)


def _connect_evolution_sqlite(
    path: Path,
    *,
    account_deletion_authorized: bool = False,
) -> sqlite3.Connection:
    """Open one evolution connection with a per-connection deletion capability.

    SQLite has no transaction-local setting equivalent to PostgreSQL's
    ``set_config``.  The trigger calls this registered function, so ordinary
    stores always fail closed while the account-governance repository can use
    a short-lived, explicitly authorized connection.
    """

    connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "evolution_account_deletion_authorized",
        0,
        lambda: int(account_deletion_authorized),
        deterministic=True,
    )
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _candidate_json(candidate: CandidateArtifact) -> str:
    return canonical_json({"payload": dict(candidate.payload)})


def _state_key(value: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 96:
        raise ValueError("evolution control state key is invalid")
    return clean


def _candidate_account_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("evolution account id is invalid")
    clean = value.strip()
    if not clean or len(clean) > 128:
        raise ValueError("evolution account id is invalid")
    return clean


def _candidate_from_row(row: sqlite3.Row) -> CandidateArtifact:
    payload = json.loads(str(row["payload_json"]))["payload"]
    candidate = CandidateArtifact(
        candidate_id=str(row["candidate_id"]),
        task_family=str(row["task_family"]),
        kind=cast(Any, row["kind"]),
        scope=cast(SignalScope, row["scope"]),
        account_id=str(row["account_id"]) if row["account_id"] is not None else None,
        version=int(row["version"]),
        payload=cast(dict[str, object], payload),
        source_signal_ids=tuple(json.loads(str(row["source_signal_ids_json"]))),
        expected_behavior=str(row["expected_behavior"]),
        regression_guards=tuple(json.loads(str(row["regression_guards_json"]))),
        risk=cast(Any, row["risk"]),
        trusted_root_sha256=str(row["trusted_root_sha256"]),
        status=cast(CandidateStatus, row["status"]),
        reason=str(row["reason"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
    )
    if str(row["artifact_hash"]) != candidate.artifact_hash:
        raise EvolutionConflictError("candidate artifact hash is invalid")
    return candidate


def _insert_lifecycle_event(
    connection: sqlite3.Connection,
    *,
    candidate_id: str,
    event_type: LifecycleEventType,
    from_status: CandidateStatus,
    to_status: CandidateStatus,
    reason: str,
    related_candidate_id: str | None,
    created_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO evolution_lifecycle_events(
            event_id, candidate_id, event_type, from_status, to_status,
            reason, related_candidate_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            candidate_id,
            event_type,
            from_status,
            to_status,
            reason[:500],
            related_candidate_id,
            created_at.astimezone(UTC).isoformat(),
        ),
    )


def _lifecycle_from_row(row: Any) -> LifecycleEvent:
    return LifecycleEvent(
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        candidate_id=str(row["candidate_id"]),
        event_type=cast(LifecycleEventType, row["event_type"]),
        from_status=cast(CandidateStatus, row["from_status"]),
        to_status=cast(CandidateStatus, row["to_status"]),
        reason=str(row["reason"]),
        related_candidate_id=(
            str(row["related_candidate_id"])
            if row["related_candidate_id"] is not None
            else None
        ),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


def _validation_json(report: ValidationReport) -> str:
    return canonical_json(
        {
            "validation_id": report.validation_id,
            "candidate_id": report.candidate_id,
            "gates": [
                {
                    "name": gate.name,
                    "passed": gate.passed,
                    "evidence": list(gate.evidence),
                    "reason": gate.reason,
                }
                for gate in report.gates
            ],
            "metrics": dict(report.metrics),
            "validator_version": report.validator_version,
            "created_at": report.created_at.astimezone(UTC).isoformat(),
        }
    )


def _validation_from_dict(value: dict[str, Any]) -> ValidationReport:
    return ValidationReport(
        validation_id=str(value["validation_id"]),
        candidate_id=str(value["candidate_id"]),
        gates=tuple(
            GateResult(
                name=str(item["name"]),
                passed=bool(item["passed"]),
                evidence=tuple(str(evidence) for evidence in item.get("evidence", [])),
                reason=str(item.get("reason", "")),
            )
            for item in value["gates"]
        ),
        metrics=tuple(
            (str(key), float(metric)) for key, metric in value.get("metrics", {}).items()
        ),
        validator_version=str(value["validator_version"]),
        created_at=datetime.fromisoformat(str(value["created_at"])),
    )


def _signal_from_dict(value: dict[str, Any]) -> LearningSignal:
    def verdict(raw: dict[str, Any]) -> LayerVerdict:
        return LayerVerdict(
            verdict=cast(Verdict, raw["verdict"]),
            reason_codes=tuple(str(item) for item in raw.get("reason_codes", [])),
            evidence_refs=tuple(
                EvidenceRef(
                    kind=str(ref["kind"]),
                    item_id=str(ref["item_id"]),
                    source_event_ids=tuple(str(event_id) for event_id in ref["source_event_ids"]),
                )
                for ref in raw.get("evidence_refs", [])
            ),
            confidence=float(raw["confidence"]),
        )

    fence = value["fence"]
    speaker = value["speaker"]
    return LearningSignal(
        signal_id=str(value["signal_id"]),
        task_family=str(value["task_family"]),
        scope=cast(SignalScope, value["scope"]),
        account_id=str(value["account_id"]) if value.get("account_id") is not None else None,
        fence=FenceSnapshot(
            session_id=str(fence["session_id"]),
            turn_id=int(fence["turn_id"]),
            generation_id=int(fence["generation_id"]),
            tool_epoch=int(fence["tool_epoch"]),
        ),
        speaker=SpeakerSnapshot(
            classification=cast(Any, speaker["classification"]),
            reason_code=str(speaker["reason_code"]),
            history_eligible=bool(speaker["history_eligible"]),
            owner_projection_eligible=bool(speaker["owner_projection_eligible"]),
            profile_id=(
                str(speaker["profile_id"]) if speaker.get("profile_id") is not None else None
            ),
        ),
        source_event_ids=tuple(str(event_id) for event_id in value["source_event_ids"]),
        result=verdict(value["result"]),
        process=verdict(value["process"]),
        quality=verdict(value["quality"]),
        environment_version=str(value["environment_version"]),
        failure_code=(str(value["failure_code"]) if value.get("failure_code") else None),
        diagnosis=str(value.get("diagnosis", "")),
        artifact_versions=tuple(
            (str(key), str(version)) for key, version in value.get("artifact_versions", {}).items()
        ),
        supporting_signal_ids=tuple(str(item) for item in value.get("supporting_signal_ids", [])),
        refuting_signal_ids=tuple(str(item) for item in value.get("refuting_signal_ids", [])),
        input_tokens=int(value.get("input_tokens", 0)),
        output_tokens=int(value.get("output_tokens", 0)),
        latency_ms=(float(value["latency_ms"]) if value.get("latency_ms") is not None else None),
        created_at=datetime.fromisoformat(str(value["created_at"])),
    )


def _verified_signal(value: dict[str, Any], expected_hash: str) -> LearningSignal:
    signal = _signal_from_dict(value)
    if signal.payload_hash != expected_hash:
        raise EvolutionConflictError("learning signal payload hash is invalid")
    return signal


__all__ = [
    "EvolutionConflictError",
    "EvolutionNotFoundError",
    "EvolutionStore",
    "EvolutionTransitionError",
    "MIN_STABLE_CANARY_OBSERVATIONS",
]
