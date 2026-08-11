"""Small immutable records for tutor sessions, turns, and progress."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from packages.contracts.generated.python.multi_subject_contracts import (
        PolicyActionResourceFence,
    )

    from services.tutor.authority import (
        TutorPolicyReceiptVerifierPort,
        TutorReceiptExpectation,
    )

SessionFocus = Literal["chat", "tutor_english", "tutor_homework"]
TutorFocus = Literal["tutor_english", "tutor_homework"]
LessonDifficulty = Literal["starter", "beginner", "intermediate"]
PracticeStatus = Literal["draft", "active", "paused", "completed"]
PracticeOutcome = Literal["attempted", "supported", "mastered", "struggled", "gave_up"]
PracticeEventAction = Literal["create", "active", "paused", "completed", "practice"]

SESSION_FOCUSES: frozenset[str] = frozenset(
    {"chat", "tutor_english", "tutor_homework"}
)
TUTOR_FOCUSES: frozenset[str] = frozenset({"tutor_english", "tutor_homework"})


class PracticeConflictError(ValueError):
    """A practice-session event cannot be replayed onto the current revision."""


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LessonTask:
    """One server-owned, voice-native practice card."""

    task_id: str
    focus: TutorFocus
    title: str
    objective: str
    opening_prompt: str
    difficulty: LessonDifficulty = "beginner"
    skill_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("task_id", "title", "objective", "opening_prompt"):
            _required(str(getattr(self, name)), name)
        if self.focus not in TUTOR_FOCUSES:
            raise ValueError("lesson focus must be a tutor focus")
        if not self.skill_keys or any(not key.strip() for key in self.skill_keys):
            raise ValueError("lesson task requires bounded skill keys")


@dataclass(frozen=True, slots=True)
class PracticeTurn:
    """Privacy-minimized learning signal derived from one eligible evidence event."""

    event_id: str
    subject_id: str
    actor_id: str
    session_id: str
    voice_session_id: str
    device_id: str
    binding_id: str
    binding_version: int
    subject_revision: int
    task_id: str
    focus: TutorFocus
    occurred_at: datetime
    duration_seconds: int
    skill_key: str
    outcome: PracticeOutcome
    history_eligible: bool
    session_epoch: int
    runtime_profile_id: str
    generation_id: int
    turn_id: int
    tool_epoch: int
    policy_receipt_id: str

    def __post_init__(self) -> None:
        for name in (
            "event_id",
            "subject_id",
            "actor_id",
            "session_id",
            "voice_session_id",
            "device_id",
            "binding_id",
            "task_id",
            "skill_key",
            "runtime_profile_id",
            "policy_receipt_id",
        ):
            _required(str(getattr(self, name)), name)
        if isinstance(self.binding_version, bool) or self.binding_version < 1:
            raise ValueError("practice turn binding_version must be positive")
        if self.focus not in TUTOR_FOCUSES:
            raise ValueError("practice turn focus must be a tutor focus")
        if not 0 <= self.duration_seconds <= 3 * 60 * 60:
            raise ValueError("practice duration must be between zero and three hours")
        if self.session_epoch < 1:
            raise ValueError("practice turn session_epoch must be positive")
        for name in (
            "subject_revision",
            "generation_id",
            "turn_id",
            "tool_epoch",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 0:
                raise ValueError(f"practice turn {name} must be non-negative")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))

@dataclass(frozen=True, slots=True)
class PracticeSession:
    """Event-replayed Pomodoro-style session, aligned with growth task states."""

    session_id: str
    subject_id: str
    actor_id: str
    voice_session_id: str
    focus: TutorFocus
    task_id: str
    status: PracticeStatus
    revision: int
    event_ids: tuple[str, ...]
    practiced_seconds: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("session_id", "subject_id", "actor_id", "voice_session_id", "task_id"):
            _required(str(getattr(self, name)), name)
        if self.focus not in TUTOR_FOCUSES:
            raise ValueError("practice session focus must be a tutor focus")
        if self.revision < 0 or self.practiced_seconds < 0:
            raise ValueError("practice revision and duration must be non-negative")
        for name in ("created_at", "updated_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))


@dataclass(frozen=True, slots=True)
class StudyProgress:
    """Rebuildable projection; never an authoritative grade or diagnosis."""

    subject_id: str
    actor_id: str | None = None
    practiced_seconds: int = 0
    active_days: tuple[date, ...] = ()
    current_streak_days: int = 0
    weak_points: tuple[tuple[str, int], ...] = ()
    mastered_skills: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    last_practiced_at: datetime | None = None

    def __post_init__(self) -> None:
        _required(self.subject_id, "subject_id")
        if self.actor_id is not None:
            _required(self.actor_id, "actor_id")
        if self.practiced_seconds < 0 or self.current_streak_days < 0:
            raise ValueError("study progress counters must be non-negative")
        if self.last_practiced_at is not None:
            object.__setattr__(
                self,
                "last_practiced_at",
                _utc(self.last_practiced_at, "last_practiced_at"),
            )


class TutorProjectionStorePort(Protocol):
    async def practice_session(
        self,
        *,
        subject_id: str,
        session_id: str,
    ) -> PracticeSession | None: ...

    async def save_practice_session(self, session: PracticeSession) -> PracticeSession: ...

    async def study_progress(self, *, subject_id: str) -> StudyProgress | None: ...

    async def save_study_progress(
        self,
        progress: StudyProgress,
        *,
        rebuilt_at: datetime,
    ) -> StudyProgress: ...


TutorAggregateKind = Literal["tutor.practice_turn_recorded", "tutor.practice_completed"]


@dataclass(frozen=True, slots=True)
class TutorAggregateCommit:
    """One atomic subject-scoped aggregate write.

    The store commits the one-time assessment consume (UNIQUE assessment id),
    the session expected-revision CAS, the immutable evidence row and the
    archive outbox row in a single transaction; the archive/guardian
    projection is rebuilt from the outbox afterwards.
    """

    event_id: str
    kind: TutorAggregateKind
    subject_id: str
    actor_id: str
    receipt_id: str
    receipt_expectation: TutorReceiptExpectation
    action_fence: PolicyActionResourceFence
    assessment_id: str | None
    evidence_envelope: dict[str, Any]
    outcome: PracticeOutcome | None
    skill_key: str | None
    archive_payload: dict[str, Any]
    session: PracticeSession
    expected_revision: int
    occurred_at: datetime

    def __post_init__(self) -> None:
        for name in ("event_id", "subject_id", "actor_id"):
            _required(str(getattr(self, name)), name)
        _required(self.receipt_id, "receipt_id")
        if self.kind not in {
            "tutor.practice_turn_recorded",
            "tutor.practice_completed",
        }:
            raise ValueError("aggregate commit kind is not supported")
        if self.kind == "tutor.practice_turn_recorded" and (
            self.assessment_id is None or self.outcome is None or self.skill_key is None
        ):
            raise ValueError("practice turn commits require assessment, outcome, skill")
        if self.expected_revision != self.session.revision - 1:
            raise ValueError("commit expected_revision must precede the session revision")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))

    def commit_sha256(self) -> str:
        """Canonical identity of the whole commit (idempotency fingerprint).

        Covers the receipt binding (id, literal expectation, canonical action
        fence hash), the evidence, the derived outcome/skill, the session
        state, and the archive payload: any retry with the same event id but
        different content fails closed with 409.
        """

        payload = {
            "event_id": self.event_id,
            "kind": self.kind,
            "subject_id": self.subject_id,
            "actor_id": self.actor_id,
            "receipt_id": self.receipt_id,
            "receipt_expectation": asdict(self.receipt_expectation),
            "action_fence_canonical_hash": self.action_fence.canonical_hash,
            "assessment_id": self.assessment_id,
            "evidence_envelope": self.evidence_envelope,
            "outcome": self.outcome,
            "skill_key": self.skill_key,
            "session_id": self.session.session_id,
            "session_revision": self.session.revision,
            "archive_payload": self.archive_payload,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PendingTutorCommit:
    """One undelivered outbox row waiting for archive projection."""

    event_id: str
    kind: TutorAggregateKind
    subject_id: str
    actor_id: str
    archive_payload: dict[str, Any]
    created_at: datetime


class TutorPracticeCommitPort(Protocol):
    """Atomic one-time aggregate commit plus archive outbox drain."""

    async def commit_aggregate(
        self,
        *,
        commit: TutorAggregateCommit,
        receipt_verifier: TutorPolicyReceiptVerifierPort,
        now: datetime,
    ) -> PracticeSession:
        """Re-verify the action receipt inside the same transaction, then
        consume the assessment once, CAS the session revision, and persist
        the immutable evidence plus outbox row -- all or nothing."""

    async def claim_commit_events(
        self,
        *,
        worker_id: str,
        subject_id: str | None = None,
        limit: int = 64,
        lease_ttl_s: int = 60,
    ) -> tuple[PendingTutorCommit, ...]: ...

    async def mark_commit_delivered(self, *, event_id: str) -> None: ...

    async def release_commit_claim(self, *, event_id: str) -> None: ...
