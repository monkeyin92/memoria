"""Small immutable records for tutor sessions, turns, and progress."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal, Protocol

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
    """Privacy-minimized learning signal derived from one eligible Evidence event."""

    event_id: str
    session_id: str
    task_id: str
    focus: TutorFocus
    occurred_at: datetime
    duration_seconds: int
    skill_key: str
    outcome: PracticeOutcome
    history_eligible: bool

    def __post_init__(self) -> None:
        for name in ("event_id", "session_id", "task_id", "skill_key"):
            _required(str(getattr(self, name)), name)
        if self.focus not in TUTOR_FOCUSES:
            raise ValueError("practice turn focus must be a tutor focus")
        if not 0 <= self.duration_seconds <= 3 * 60 * 60:
            raise ValueError("practice duration must be between zero and three hours")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))


@dataclass(frozen=True, slots=True)
class PracticeSession:
    """Event-replayed Pomodoro-style session, aligned with growth task states."""

    session_id: str
    account_id: str
    focus: TutorFocus
    task_id: str
    status: PracticeStatus
    revision: int
    event_ids: tuple[str, ...]
    practiced_seconds: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("session_id", "account_id", "task_id"):
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

    account_id: str
    practiced_seconds: int = 0
    active_days: tuple[date, ...] = ()
    current_streak_days: int = 0
    weak_points: tuple[tuple[str, int], ...] = ()
    mastered_skills: tuple[str, ...] = ()
    source_event_ids: tuple[str, ...] = ()
    last_practiced_at: datetime | None = None

    def __post_init__(self) -> None:
        _required(self.account_id, "account_id")
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
        account_id: str,
        session_id: str,
    ) -> PracticeSession | None: ...

    async def save_practice_session(self, session: PracticeSession) -> PracticeSession: ...

    async def study_progress(self, *, account_id: str) -> StudyProgress | None: ...

    async def save_study_progress(
        self,
        progress: StudyProgress,
        *,
        rebuilt_at: datetime,
    ) -> StudyProgress: ...
