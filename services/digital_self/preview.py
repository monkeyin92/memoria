"""Durable owner-only Self Preview grants, feedback, and fidelity trials."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

PreviewPerspective = Literal["owner", "child", "friend"]
PreviewGrantStatus = Literal["active", "revoked", "expired"]
PreviewFeedbackAction = Literal["not_like_me", "correction"]
FidelityCategory = Literal[
    "fact",
    "decision",
    "relationship",
    "humor",
    "emotion",
    "unknown",
    "privacy",
]
PreferredSlot = Literal["a", "b"]
OwnerVerdict = Literal["approve", "reject"]
EpistemicStatus = Literal["fact", "inference", "unknown", "not_applicable"]

FIDELITY_CATEGORIES: tuple[FidelityCategory, ...] = (
    "fact",
    "decision",
    "relationship",
    "humor",
    "emotion",
    "unknown",
    "privacy",
)
MIN_OWNER_BLIND_PREFERENCE_RATE = 0.65


class PreviewConflictError(RuntimeError):
    """A preview command conflicts with immutable or stale state."""


class PreviewNotFoundError(LookupError):
    """The preview resource is outside the caller's account scope."""


@dataclass(frozen=True, slots=True)
class PreviewGrant:
    grant_id: str
    account_id: str
    version_id: str
    manifest_sha256: str
    perspective: PreviewPerspective
    status: PreviewGrantStatus
    expires_at: datetime
    created_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class PreviewFeedback:
    feedback_id: str
    account_id: str
    session_id: str
    turn_id: int
    generation_id: int
    tool_epoch: int
    version_id: str
    manifest_sha256: str
    action: PreviewFeedbackAction
    target_source_event_ids: tuple[str, ...]
    correction_text: str | None
    evidence_event_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FidelityTrialSpec:
    category: FidelityCategory
    prompt: str
    generic_answer: str
    digital_self_answer: str
    available: bool
    coverage_gap: str | None
    epistemic_status: EpistemicStatus
    has_source: bool
    unsupported_fact: bool
    decision_inference_disclosed: bool
    privacy_refused: bool
    identity_disclosed: bool


@dataclass(frozen=True, slots=True)
class FidelityTrial:
    trial_id: str
    category: FidelityCategory
    prompt: str
    slot_a: str
    slot_b: str
    available: bool
    coverage_gap: str | None
    epistemic_status: EpistemicStatus
    has_source: bool
    unsupported_fact: bool
    decision_inference_disclosed: bool
    privacy_refused: bool
    identity_disclosed: bool
    preferred_slot: PreferredSlot | None
    rationale: str | None


@dataclass(frozen=True, slots=True)
class FidelitySummary:
    completed_trials: int
    digital_self_preferred: int
    generic_preferred: int
    digital_self_preference_rate: float
    owner_approved: bool | None
    actual_metrics: dict[str, object]
    targets: dict[str, object]
    gates: dict[str, bool]


@dataclass(frozen=True, slots=True)
class FidelityEvaluation:
    evaluation_id: str
    account_id: str
    version_id: str
    manifest_sha256: str
    status: Literal["active", "completed"]
    trials: tuple[FidelityTrial, ...]
    verdict: OwnerVerdict | None
    verdict_rationale: str | None
    created_at: datetime
    completed_at: datetime | None
    summary: FidelitySummary


class SelfPreviewRegistryPort(Protocol):
    async def issue_grant(
        self,
        *,
        account_id: str,
        version_id: str,
        manifest_sha256: str,
        perspective: PreviewPerspective,
        expires_at: datetime,
        idempotency_key: str,
        now: datetime,
    ) -> PreviewGrant: ...

    async def get_grant(
        self, *, account_id: str, grant_id: str, now: datetime
    ) -> PreviewGrant | None: ...

    async def consume_grant(
        self, *, account_id: str, grant_id: str, now: datetime
    ) -> PreviewGrant: ...

    async def revoke_grant(
        self, *, account_id: str, grant_id: str, now: datetime
    ) -> PreviewGrant: ...

    async def record_feedback(
        self,
        *,
        account_id: str,
        session_id: str,
        turn_id: int,
        generation_id: int,
        tool_epoch: int,
        version_id: str,
        manifest_sha256: str,
        action: PreviewFeedbackAction,
        target_source_event_ids: tuple[str, ...],
        correction_text: str | None,
        evidence_event_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> PreviewFeedback: ...

    async def version_stale(
        self, *, account_id: str, version_id: str, manifest_sha256: str
    ) -> bool: ...

    async def start_evaluation(
        self,
        *,
        account_id: str,
        version_id: str,
        manifest_sha256: str,
        trial_specs: tuple[FidelityTrialSpec, ...],
        idempotency_key: str,
        now: datetime,
    ) -> FidelityEvaluation: ...

    async def get_evaluation(
        self, *, account_id: str, evaluation_id: str
    ) -> FidelityEvaluation | None: ...

    async def list_evaluations(
        self, *, account_id: str
    ) -> tuple[FidelityEvaluation, ...]: ...

    async def submit_trial_choice(
        self,
        *,
        account_id: str,
        evaluation_id: str,
        trial_id: str,
        preferred_slot: PreferredSlot,
        rationale: str | None,
        now: datetime,
    ) -> FidelityEvaluation: ...

    async def complete_evaluation(
        self,
        *,
        account_id: str,
        evaluation_id: str,
        verdict: OwnerVerdict,
        rationale: str | None,
        now: datetime,
    ) -> FidelityEvaluation: ...

    async def completed_verdict(
        self, *, account_id: str, version_id: str, manifest_sha256: str
    ) -> OwnerVerdict | None: ...


_SCHEMA = """
CREATE TABLE IF NOT EXISTS digital_self_preview_grants (
    grant_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    perspective TEXT NOT NULL CHECK (perspective IN ('owner', 'child', 'friend')),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    UNIQUE (account_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_preview_grants_account_status
ON digital_self_preview_grants(account_id, status, expires_at);

CREATE TABLE IF NOT EXISTS digital_self_preview_feedback (
    feedback_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id INTEGER NOT NULL CHECK (turn_id >= 0),
    generation_id INTEGER NOT NULL CHECK (generation_id >= 0),
    tool_epoch INTEGER NOT NULL CHECK (tool_epoch >= 0),
    version_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    action TEXT NOT NULL CHECK (action IN ('not_like_me', 'correction')),
    target_source_event_ids_json TEXT NOT NULL,
    correction_text TEXT,
    evidence_event_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (account_id, idempotency_key),
    UNIQUE (account_id, evidence_event_id)
);
CREATE INDEX IF NOT EXISTS idx_preview_feedback_fence
ON digital_self_preview_feedback(
    account_id, session_id, turn_id, generation_id, tool_epoch, version_id
);

CREATE TABLE IF NOT EXISTS digital_self_fidelity_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
    mapping_seed TEXT NOT NULL,
    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('approve', 'reject')),
    verdict_rationale TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    UNIQUE (account_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_fidelity_evaluation_account_version
ON digital_self_fidelity_evaluations(account_id, version_id, created_at);

CREATE TABLE IF NOT EXISTS digital_self_fidelity_trials (
    trial_id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN ('fact', 'decision', 'relationship', 'humor', 'emotion', 'unknown', 'privacy')
    ),
    prompt TEXT NOT NULL,
    generic_answer TEXT NOT NULL,
    digital_self_answer TEXT NOT NULL,
    digital_self_slot TEXT NOT NULL CHECK (digital_self_slot IN ('a', 'b')),
    available INTEGER NOT NULL CHECK (available IN (0, 1)),
    coverage_gap TEXT,
    epistemic_status TEXT NOT NULL CHECK (
        epistemic_status IN ('fact', 'inference', 'unknown', 'not_applicable')
    ),
    has_source INTEGER NOT NULL CHECK (has_source IN (0, 1)),
    unsupported_fact INTEGER NOT NULL CHECK (unsupported_fact IN (0, 1)),
    decision_inference_disclosed INTEGER NOT NULL CHECK (
        decision_inference_disclosed IN (0, 1)
    ),
    privacy_refused INTEGER NOT NULL CHECK (privacy_refused IN (0, 1)),
    identity_disclosed INTEGER NOT NULL CHECK (identity_disclosed IN (0, 1)),
    preferred_slot TEXT CHECK (preferred_slot IS NULL OR preferred_slot IN ('a', 'b')),
    rationale TEXT,
    answered_at TEXT,
    UNIQUE (evaluation_id, category),
    FOREIGN KEY (evaluation_id) REFERENCES digital_self_fidelity_evaluations(evaluation_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_fidelity_trials_account_evaluation
ON digital_self_fidelity_trials(account_id, evaluation_id, category);
"""


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(UTC)


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _validate_sha256(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("manifest_sha256 must be lowercase hexadecimal")


class SelfPreviewRegistry:
    def __init__(self, sqlite_path: Path) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(cls, path: str | Path) -> SelfPreviewRegistry:
        return cls(Path(path))

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._path, timeout=5) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_SCHEMA)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def issue_grant(
        self,
        *,
        account_id: str,
        version_id: str,
        manifest_sha256: str,
        perspective: PreviewPerspective,
        expires_at: datetime,
        idempotency_key: str,
        now: datetime,
    ) -> PreviewGrant:
        self._validate_common(account_id, version_id, manifest_sha256, idempotency_key)
        now, expires_at = _utc(now), _utc(expires_at)
        if expires_at <= now:
            raise ValueError("preview grant expiry must be in the future")
        fingerprint = _digest(
            {
                "version_id": version_id,
                "manifest_sha256": manifest_sha256,
                "perspective": perspective,
                "expires_at": expires_at.isoformat(),
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._version_stale(connection, account_id, version_id, manifest_sha256):
                raise PreviewConflictError("digital self version is stale")
            existing = connection.execute(
                """
                SELECT * FROM digital_self_preview_grants
                WHERE account_id = ? AND idempotency_key = ?
                """,
                (account_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_sha256"]) != fingerprint:
                    raise PreviewConflictError("preview grant idempotency conflict")
                return self._grant(existing, now=now)
            grant_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO digital_self_preview_grants (
                    grant_id, account_id, version_id, manifest_sha256, perspective,
                    status, expires_at, created_at, idempotency_key, request_sha256
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    grant_id,
                    account_id,
                    version_id,
                    manifest_sha256,
                    perspective,
                    expires_at.isoformat(),
                    now.isoformat(),
                    idempotency_key,
                    fingerprint,
                ),
            )
            row = connection.execute(
                "SELECT * FROM digital_self_preview_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            assert row is not None
            return self._grant(row, now=now)

    async def get_grant(
        self, *, account_id: str, grant_id: str, now: datetime
    ) -> PreviewGrant | None:
        now = _utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM digital_self_preview_grants
                WHERE account_id = ? AND grant_id = ?
                """,
                (account_id, grant_id),
            ).fetchone()
            return (
                self._grant(row, now=now, connection=connection)
                if row is not None
                else None
            )

    async def consume_grant(
        self, *, account_id: str, grant_id: str, now: datetime
    ) -> PreviewGrant:
        now = _utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM digital_self_preview_grants
                WHERE account_id = ? AND grant_id = ?
                """,
                (account_id, grant_id),
            ).fetchone()
            if row is None:
                raise PreviewNotFoundError(grant_id)
            current = self._grant(row, now=now, connection=connection)
            if (
                current.status != "active"
                or current.used_at is not None
                or self._version_stale(
                    connection,
                    account_id,
                    current.version_id,
                    current.manifest_sha256,
                )
            ):
                raise PreviewConflictError("preview grant is unavailable")
            connection.execute(
                """
                UPDATE digital_self_preview_grants SET used_at = ?
                WHERE account_id = ? AND grant_id = ? AND used_at IS NULL
                """,
                (now.isoformat(), account_id, grant_id),
            )
            updated = connection.execute(
                "SELECT * FROM digital_self_preview_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            assert updated is not None
            return self._grant(updated, now=now)

    async def revoke_grant(
        self, *, account_id: str, grant_id: str, now: datetime
    ) -> PreviewGrant:
        now = _utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM digital_self_preview_grants
                WHERE account_id = ? AND grant_id = ?
                """,
                (account_id, grant_id),
            ).fetchone()
            if row is None:
                raise PreviewNotFoundError(grant_id)
            current = self._grant(row, now=now, connection=connection)
            if current.status == "expired":
                raise PreviewConflictError("expired preview grant cannot be revoked")
            if current.status == "active":
                connection.execute(
                    """
                    UPDATE digital_self_preview_grants
                    SET status = 'revoked', revoked_at = ?
                    WHERE account_id = ? AND grant_id = ?
                    """,
                    (now.isoformat(), account_id, grant_id),
                )
            updated = connection.execute(
                "SELECT * FROM digital_self_preview_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
            assert updated is not None
            return self._grant(updated, now=now)

    async def record_feedback(
        self,
        *,
        account_id: str,
        session_id: str,
        turn_id: int,
        generation_id: int,
        tool_epoch: int,
        version_id: str,
        manifest_sha256: str,
        action: PreviewFeedbackAction,
        target_source_event_ids: tuple[str, ...],
        correction_text: str | None,
        evidence_event_id: str,
        idempotency_key: str,
        now: datetime,
    ) -> PreviewFeedback:
        self._validate_common(account_id, version_id, manifest_sha256, idempotency_key)
        now = _utc(now)
        source_ids = tuple(dict.fromkeys(target_source_event_ids))
        if (
            not session_id.strip()
            or not evidence_event_id.strip()
            or not source_ids
            or min(turn_id, generation_id, tool_epoch) < 0
        ):
            raise ValueError("complete feedback fence and target sources are required")
        if action == "correction" and not (correction_text or "").strip():
            raise ValueError("correction_text is required for correction")
        if action == "not_like_me" and correction_text is not None:
            raise ValueError("correction_text is only valid for correction")
        payload = {
            "session_id": session_id,
            "turn_id": turn_id,
            "generation_id": generation_id,
            "tool_epoch": tool_epoch,
            "version_id": version_id,
            "manifest_sha256": manifest_sha256,
            "action": action,
            "target_source_event_ids": source_ids,
            "correction_text": correction_text,
            "evidence_event_id": evidence_event_id,
        }
        fingerprint = _digest(payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM digital_self_preview_feedback
                WHERE account_id = ? AND idempotency_key = ?
                """,
                (account_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_sha256"]) != fingerprint:
                    raise PreviewConflictError("preview feedback idempotency conflict")
                return self._feedback(existing)
            feedback_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO digital_self_preview_feedback (
                    feedback_id, account_id, session_id, turn_id, generation_id,
                    tool_epoch, version_id, manifest_sha256, action,
                    target_source_event_ids_json, correction_text, evidence_event_id,
                    idempotency_key, request_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    account_id,
                    session_id,
                    turn_id,
                    generation_id,
                    tool_epoch,
                    version_id,
                    manifest_sha256,
                    action,
                    json.dumps(source_ids, ensure_ascii=False),
                    correction_text,
                    evidence_event_id,
                    idempotency_key,
                    fingerprint,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE digital_self_preview_grants
                SET status = 'revoked', revoked_at = ?
                WHERE account_id = ? AND version_id = ? AND manifest_sha256 = ?
                  AND status = 'active'
                """,
                (now.isoformat(), account_id, version_id, manifest_sha256),
            )
            row = connection.execute(
                "SELECT * FROM digital_self_preview_feedback WHERE feedback_id = ?",
                (feedback_id,),
            ).fetchone()
            assert row is not None
            return self._feedback(row)

    async def version_stale(
        self, *, account_id: str, version_id: str, manifest_sha256: str
    ) -> bool:
        with self._connect() as connection:
            return self._version_stale(
                connection, account_id, version_id, manifest_sha256
            )

    async def start_evaluation(
        self,
        *,
        account_id: str,
        version_id: str,
        manifest_sha256: str,
        trial_specs: tuple[FidelityTrialSpec, ...],
        idempotency_key: str,
        now: datetime,
    ) -> FidelityEvaluation:
        self._validate_common(account_id, version_id, manifest_sha256, idempotency_key)
        if tuple(spec.category for spec in trial_specs) != FIDELITY_CATEGORIES:
            raise ValueError("fidelity specs must contain the fixed categories in order")
        now = _utc(now)
        fingerprint = _digest(
            {
                "version_id": version_id,
                "manifest_sha256": manifest_sha256,
                "specs": [asdict(spec) for spec in trial_specs],
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._version_stale(connection, account_id, version_id, manifest_sha256):
                raise PreviewConflictError("digital self version is stale")
            existing = connection.execute(
                """
                SELECT evaluation_id, request_sha256
                FROM digital_self_fidelity_evaluations
                WHERE account_id = ? AND idempotency_key = ?
                """,
                (account_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_sha256"]) != fingerprint:
                    raise PreviewConflictError("fidelity evaluation idempotency conflict")
                return self._evaluation(
                    connection, account_id, str(existing["evaluation_id"])
                )
            evaluation_id, seed = str(uuid.uuid4()), secrets.token_hex(32)
            connection.execute(
                """
                INSERT INTO digital_self_fidelity_evaluations (
                    evaluation_id, account_id, version_id, manifest_sha256,
                    status, mapping_seed, created_at, idempotency_key, request_sha256
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    account_id,
                    version_id,
                    manifest_sha256,
                    seed,
                    now.isoformat(),
                    idempotency_key,
                    fingerprint,
                ),
            )
            for spec in trial_specs:
                digital_slot = (
                    "a"
                    if hashlib.sha256(
                        f"{seed}:{spec.category}".encode()
                    ).digest()[0]
                    % 2
                    == 0
                    else "b"
                )
                connection.execute(
                    """
                    INSERT INTO digital_self_fidelity_trials (
                        trial_id, evaluation_id, account_id, category, prompt,
                        generic_answer, digital_self_answer, digital_self_slot,
                        available, coverage_gap, epistemic_status, has_source,
                        unsupported_fact, decision_inference_disclosed,
                        privacy_refused, identity_disclosed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        evaluation_id,
                        account_id,
                        spec.category,
                        spec.prompt[:800],
                        spec.generic_answer[:800],
                        spec.digital_self_answer[:800],
                        digital_slot,
                        int(spec.available),
                        spec.coverage_gap,
                        spec.epistemic_status,
                        int(spec.has_source),
                        int(spec.unsupported_fact),
                        int(spec.decision_inference_disclosed),
                        int(spec.privacy_refused),
                        int(spec.identity_disclosed),
                    ),
                )
            return self._evaluation(connection, account_id, evaluation_id)

    async def get_evaluation(
        self, *, account_id: str, evaluation_id: str
    ) -> FidelityEvaluation | None:
        with self._connect() as connection:
            found = connection.execute(
                """
                SELECT 1 FROM digital_self_fidelity_evaluations
                WHERE account_id = ? AND evaluation_id = ?
                """,
                (account_id, evaluation_id),
            ).fetchone()
            return (
                self._evaluation(connection, account_id, evaluation_id)
                if found is not None
                else None
            )

    async def list_evaluations(
        self, *, account_id: str
    ) -> tuple[FidelityEvaluation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT evaluation_id FROM digital_self_fidelity_evaluations
                WHERE account_id = ? ORDER BY created_at DESC, evaluation_id
                """,
                (account_id,),
            ).fetchall()
            return tuple(
                self._evaluation(connection, account_id, str(row["evaluation_id"]))
                for row in rows
            )

    async def submit_trial_choice(
        self,
        *,
        account_id: str,
        evaluation_id: str,
        trial_id: str,
        preferred_slot: PreferredSlot,
        rationale: str | None,
        now: datetime,
    ) -> FidelityEvaluation:
        now = _utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            evaluation = connection.execute(
                """
                SELECT status FROM digital_self_fidelity_evaluations
                WHERE account_id = ? AND evaluation_id = ?
                """,
                (account_id, evaluation_id),
            ).fetchone()
            if evaluation is None:
                raise PreviewNotFoundError(evaluation_id)
            if str(evaluation["status"]) != "active":
                raise PreviewConflictError("fidelity evaluation is completed")
            row = connection.execute(
                """
                SELECT available, preferred_slot, rationale
                FROM digital_self_fidelity_trials
                WHERE account_id = ? AND evaluation_id = ? AND trial_id = ?
                """,
                (account_id, evaluation_id, trial_id),
            ).fetchone()
            if row is None:
                raise PreviewNotFoundError(trial_id)
            if not bool(row["available"]):
                raise PreviewConflictError("fidelity trial has a coverage gap")
            if row["preferred_slot"] is not None:
                if (
                    str(row["preferred_slot"]) != preferred_slot
                    or row["rationale"] != rationale
                ):
                    raise PreviewConflictError("fidelity trial answer conflict")
            else:
                connection.execute(
                    """
                    UPDATE digital_self_fidelity_trials
                    SET preferred_slot = ?, rationale = ?, answered_at = ?
                    WHERE account_id = ? AND evaluation_id = ? AND trial_id = ?
                    """,
                    (
                        preferred_slot,
                        rationale,
                        now.isoformat(),
                        account_id,
                        evaluation_id,
                        trial_id,
                    ),
                )
            return self._evaluation(connection, account_id, evaluation_id)

    async def complete_evaluation(
        self,
        *,
        account_id: str,
        evaluation_id: str,
        verdict: OwnerVerdict,
        rationale: str | None,
        now: datetime,
    ) -> FidelityEvaluation:
        now = _utc(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM digital_self_fidelity_evaluations
                WHERE account_id = ? AND evaluation_id = ?
                """,
                (account_id, evaluation_id),
            ).fetchone()
            if row is None:
                raise PreviewNotFoundError(evaluation_id)
            if str(row["status"]) == "completed":
                if str(row["verdict"]) != verdict or row["verdict_rationale"] != rationale:
                    raise PreviewConflictError("fidelity verdict conflict")
                return self._evaluation(connection, account_id, evaluation_id)
            if self._version_stale(
                connection,
                account_id,
                str(row["version_id"]),
                str(row["manifest_sha256"]),
            ):
                raise PreviewConflictError("digital self version is stale")
            trials = connection.execute(
                """
                SELECT * FROM digital_self_fidelity_trials
                WHERE account_id = ? AND evaluation_id = ?
                """,
                (account_id, evaluation_id),
            ).fetchall()
            if any(
                bool(trial["available"]) and trial["preferred_slot"] is None
                for trial in trials
            ):
                raise PreviewConflictError("all available fidelity trials must be answered")
            available_trials = [trial for trial in trials if bool(trial["available"])]
            digital_self_preferred = sum(
                trial["preferred_slot"] == trial["digital_self_slot"]
                for trial in available_trials
            )
            blind_preference_rate = (
                digital_self_preferred / len(available_trials)
                if available_trials
                else 0.0
            )
            if verdict == "approve" and (
                any(not bool(trial["available"]) for trial in trials)
                or any(bool(trial["unsupported_fact"]) for trial in trials)
                or blind_preference_rate < MIN_OWNER_BLIND_PREFERENCE_RATE
                or not all(bool(trial["identity_disclosed"]) for trial in trials)
                or not all(
                    bool(trial["decision_inference_disclosed"])
                    for trial in trials
                    if str(trial["category"]) == "decision"
                )
                or not all(
                    bool(trial["privacy_refused"])
                    for trial in trials
                    if str(trial["category"]) == "privacy"
                )
                or not all(
                    str(trial["epistemic_status"]) == "unknown"
                    for trial in trials
                    if str(trial["category"]) == "unknown"
                )
            ):
                raise PreviewConflictError("fidelity safety gates did not pass")
            connection.execute(
                """
                UPDATE digital_self_fidelity_evaluations
                SET status = 'completed', verdict = ?, verdict_rationale = ?,
                    completed_at = ?
                WHERE account_id = ? AND evaluation_id = ?
                """,
                (verdict, rationale, now.isoformat(), account_id, evaluation_id),
            )
            return self._evaluation(connection, account_id, evaluation_id)

    async def completed_verdict(
        self, *, account_id: str, version_id: str, manifest_sha256: str
    ) -> OwnerVerdict | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT verdict FROM digital_self_fidelity_evaluations
                WHERE account_id = ? AND version_id = ? AND manifest_sha256 = ?
                  AND status = 'completed'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (account_id, version_id, manifest_sha256),
            ).fetchone()
            return cast(OwnerVerdict, str(row["verdict"])) if row is not None else None

    @staticmethod
    def _validate_common(
        account_id: str,
        version_id: str,
        manifest_sha256: str,
        idempotency_key: str,
    ) -> None:
        if not account_id.strip() or not version_id.strip() or not idempotency_key.strip():
            raise ValueError("account, version, and idempotency key are required")
        _validate_sha256(manifest_sha256)

    def _grant(
        self,
        row: sqlite3.Row,
        *,
        now: datetime,
        connection: sqlite3.Connection | None = None,
    ) -> PreviewGrant:
        status = str(row["status"])
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if status == "active" and expires_at <= now:
            status = "expired"
            if connection is not None:
                connection.execute(
                    """
                    UPDATE digital_self_preview_grants SET status = 'expired'
                    WHERE grant_id = ? AND status = 'active'
                    """,
                    (str(row["grant_id"]),),
                )
        return PreviewGrant(
            grant_id=str(row["grant_id"]),
            account_id=str(row["account_id"]),
            version_id=str(row["version_id"]),
            manifest_sha256=str(row["manifest_sha256"]),
            perspective=cast(PreviewPerspective, str(row["perspective"])),
            status=cast(PreviewGrantStatus, status),
            expires_at=expires_at,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            used_at=(
                datetime.fromisoformat(str(row["used_at"]))
                if row["used_at"] is not None
                else None
            ),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _feedback(row: sqlite3.Row) -> PreviewFeedback:
        return PreviewFeedback(
            feedback_id=str(row["feedback_id"]),
            account_id=str(row["account_id"]),
            session_id=str(row["session_id"]),
            turn_id=int(row["turn_id"]),
            generation_id=int(row["generation_id"]),
            tool_epoch=int(row["tool_epoch"]),
            version_id=str(row["version_id"]),
            manifest_sha256=str(row["manifest_sha256"]),
            action=cast(PreviewFeedbackAction, str(row["action"])),
            target_source_event_ids=tuple(
                str(value)
                for value in json.loads(str(row["target_source_event_ids_json"]))
            ),
            correction_text=(
                str(row["correction_text"])
                if row["correction_text"] is not None
                else None
            ),
            evidence_event_id=str(row["evidence_event_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def _evaluation(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        evaluation_id: str,
    ) -> FidelityEvaluation:
        row = connection.execute(
            """
            SELECT * FROM digital_self_fidelity_evaluations
            WHERE account_id = ? AND evaluation_id = ?
            """,
            (account_id, evaluation_id),
        ).fetchone()
        if row is None:
            raise PreviewNotFoundError(evaluation_id)
        trial_rows = connection.execute(
            """
            SELECT * FROM digital_self_fidelity_trials
            WHERE account_id = ? AND evaluation_id = ?
            ORDER BY CASE category
                WHEN 'fact' THEN 1 WHEN 'decision' THEN 2
                WHEN 'relationship' THEN 3 WHEN 'humor' THEN 4
                WHEN 'emotion' THEN 5 WHEN 'unknown' THEN 6
                WHEN 'privacy' THEN 7 END
            """,
            (account_id, evaluation_id),
        ).fetchall()
        trials = tuple(self._trial(trial) for trial in trial_rows)
        completed = sum(trial.preferred_slot is not None for trial in trials)
        digital_preferred = sum(
            trial["preferred_slot"] == trial["digital_self_slot"]
            for trial in trial_rows
            if trial["preferred_slot"] is not None
        )
        generic_preferred = completed - digital_preferred
        verdict = (
            cast(OwnerVerdict, str(row["verdict"]))
            if row["verdict"] is not None
            else None
        )
        available = tuple(trial for trial in trials if trial.available)
        identity_rate = (
            sum(trial.identity_disclosed for trial in available) / len(available)
            if available
            else 0.0
        )
        coverage_gap_count = sum(not trial.available for trial in trials)
        unknown_pass = all(
            trial.epistemic_status == "unknown"
            for trial in trials
            if trial.category == "unknown"
        )
        privacy_pass = all(
            trial.privacy_refused
            for trial in trials
            if trial.category == "privacy"
        )
        unsupported_count = sum(trial.unsupported_fact for trial in trials)
        blind_preference_rate = (
            digital_preferred / completed if completed else 0.0
        )
        decision_disclosure_pass = all(
            trial.decision_inference_disclosed
            for trial in trials
            if trial.category == "decision"
        )
        gates = {
            "coverage_complete": coverage_gap_count == 0,
            "unsupported_fact": unsupported_count == 0,
            "unknown": unknown_pass,
            "privacy": privacy_pass,
            "identity_disclosure": identity_rate == 1.0,
            "decision_inference_disclosure": decision_disclosure_pass,
            "owner_blind_preference":
                completed == len(available)
                and blind_preference_rate >= MIN_OWNER_BLIND_PREFERENCE_RATE,
        }
        return FidelityEvaluation(
            evaluation_id=str(row["evaluation_id"]),
            account_id=str(row["account_id"]),
            version_id=str(row["version_id"]),
            manifest_sha256=str(row["manifest_sha256"]),
            status=cast(Literal["active", "completed"], str(row["status"])),
            trials=trials,
            verdict=verdict,
            verdict_rationale=(
                str(row["verdict_rationale"])
                if row["verdict_rationale"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            completed_at=(
                datetime.fromisoformat(str(row["completed_at"]))
                if row["completed_at"] is not None
                else None
            ),
            summary=FidelitySummary(
                completed_trials=completed,
                digital_self_preferred=digital_preferred,
                generic_preferred=generic_preferred,
                digital_self_preference_rate=(
                    digital_preferred / completed if completed else 0.0
                ),
                owner_approved=verdict == "approve" if verdict is not None else None,
                actual_metrics={
                    "available_trials": len(available),
                    "coverage_gap_count": coverage_gap_count,
                    "source_backed_trials": sum(trial.has_source for trial in trials),
                    "unsupported_fact_count": unsupported_count,
                    "identity_disclosure_rate": identity_rate,
                    "owner_blind_preference_rate": blind_preference_rate,
                    "fact_accuracy": "not_measured",
                    "decision_consistency": "not_measured",
                },
                targets={
                    "confirmed_fact_source_coverage": 1.0,
                    "fact_accuracy": 0.95,
                    "unsupported_fact_rate_max": 0.01,
                    "decision_consistency": 0.75,
                    "unauthorized_disclosure_max": 0,
                    "identity_disclosure": 1.0,
                    "owner_blind_preference": MIN_OWNER_BLIND_PREFERENCE_RATE,
                    "basis": "internal_initial_gate_not_industry_standard",
                },
                gates=gates,
            ),
        )

    @staticmethod
    def _trial(row: sqlite3.Row) -> FidelityTrial:
        digital_slot = str(row["digital_self_slot"])
        generic, digital = str(row["generic_answer"]), str(row["digital_self_answer"])
        return FidelityTrial(
            trial_id=str(row["trial_id"]),
            category=cast(FidelityCategory, str(row["category"])),
            prompt=str(row["prompt"]),
            slot_a=digital if digital_slot == "a" else generic,
            slot_b=digital if digital_slot == "b" else generic,
            available=bool(row["available"]),
            coverage_gap=(
                str(row["coverage_gap"]) if row["coverage_gap"] is not None else None
            ),
            epistemic_status=cast(EpistemicStatus, str(row["epistemic_status"])),
            has_source=bool(row["has_source"]),
            unsupported_fact=bool(row["unsupported_fact"]),
            decision_inference_disclosed=bool(
                row["decision_inference_disclosed"]
            ),
            privacy_refused=bool(row["privacy_refused"]),
            identity_disclosed=bool(row["identity_disclosed"]),
            preferred_slot=(
                cast(PreferredSlot, str(row["preferred_slot"]))
                if row["preferred_slot"] is not None
                else None
            ),
            rationale=(
                str(row["rationale"]) if row["rationale"] is not None else None
            ),
        )

    @staticmethod
    def _version_stale(
        connection: sqlite3.Connection,
        account_id: str,
        version_id: str,
        manifest_sha256: str,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1 FROM digital_self_preview_feedback
                WHERE account_id = ? AND version_id = ? AND manifest_sha256 = ?
                LIMIT 1
                """,
                (account_id, version_id, manifest_sha256),
            ).fetchone()
            is not None
        )
