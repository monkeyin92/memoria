"""Pure replay for practice sessions and privacy-minimized study progress."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from services.archive.domain import EvidenceEvent
from services.tutor.domain import (
    PracticeConflictError,
    PracticeEventAction,
    PracticeOutcome,
    PracticeSession,
    PracticeStatus,
    PracticeTurn,
    StudyProgress,
    TutorFocus,
)

PRACTICE_TURN_EVENT = "tutor.practice_turn_recorded"
_ALLOWED: dict[PracticeStatus, frozenset[PracticeEventAction]] = {
    "draft": frozenset({"active"}),
    "active": frozenset({"paused", "completed", "practice"}),
    "paused": frozenset({"active", "completed"}),
    "completed": frozenset(),
}


def apply_practice_event(
    session: PracticeSession | None,
    *,
    event_id: str,
    account_id: str,
    focus: TutorFocus,
    task_id: str,
    action: PracticeEventAction,
    expected_revision: int | None = None,
    duration_seconds: int = 0,
    occurred_at: datetime | None = None,
) -> PracticeSession:
    """Replay one idempotent CAS transition using the established task pattern."""

    now = (occurred_at or datetime.now(UTC)).astimezone(UTC)
    if session is None:
        if action != "create" or duration_seconds:
            raise PracticeConflictError("practice_session_not_found")
        return PracticeSession(
            session_id=event_id.removesuffix(":create"),
            account_id=account_id,
            focus=focus,
            task_id=task_id,
            status="draft",
            revision=0,
            event_ids=(event_id,),
            created_at=now,
            updated_at=now,
        )
    if event_id in session.event_ids:
        return session
    if (account_id, focus, task_id) != (session.account_id, session.focus, session.task_id):
        raise PracticeConflictError("practice_session_scope_mismatch")
    if expected_revision != session.revision:
        raise PracticeConflictError("revision_conflict")
    if action not in _ALLOWED[session.status]:
        raise PracticeConflictError("invalid_transition")
    if action == "practice" and not 1 <= duration_seconds <= 3 * 60 * 60:
        raise PracticeConflictError("invalid_practice_duration")
    if action != "practice" and duration_seconds:
        raise PracticeConflictError("unexpected_practice_duration")
    status = cast(PracticeStatus, session.status if action == "practice" else action)
    return PracticeSession(
        session_id=session.session_id,
        account_id=session.account_id,
        focus=session.focus,
        task_id=session.task_id,
        status=status,
        revision=session.revision + 1,
        event_ids=(*session.event_ids, event_id),
        practiced_seconds=session.practiced_seconds + duration_seconds,
        created_at=session.created_at,
        updated_at=now,
    )


def _bounded_text(payload: Any, key: str, *, max_length: int = 128) -> str | None:
    value = payload.get(key) if isinstance(payload, dict) else None
    return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= max_length else None


def practice_turn_from_evidence(event: EvidenceEvent) -> PracticeTurn | None:
    """Fail closed unless the event is an owner-eligible tutor learning signal."""

    payload = dict(event.payload)
    focus = payload.get("session_focus")
    outcome = payload.get("outcome")
    duration = payload.get("duration_seconds")
    if (
        event.event_type != PRACTICE_TURN_EVENT
        or event.speaker_class != "owner"
        or payload.get("history_eligible") is not True
        or focus not in {"tutor_english", "tutor_homework"}
        or outcome not in {"attempted", "supported", "mastered", "struggled", "gave_up"}
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 <= duration <= 3 * 60 * 60
    ):
        return None
    session_id = event.session_id or _bounded_text(payload, "session_id")
    task_id = _bounded_text(payload, "task_id")
    skill_key = _bounded_text(payload, "skill_key")
    if session_id is None or task_id is None or skill_key is None:
        return None
    return PracticeTurn(
        event_id=event.event_id,
        session_id=session_id,
        task_id=task_id,
        focus=cast(TutorFocus, focus),
        occurred_at=event.occurred_at,
        duration_seconds=duration,
        skill_key=skill_key,
        outcome=cast(PracticeOutcome, outcome),
        history_eligible=True,
    )


def project_study_progress(
    *,
    account_id: str,
    events: Iterable[EvidenceEvent],
) -> StudyProgress:
    """Rebuild progress deterministically from immutable Evidence events."""

    turns_by_id: dict[str, PracticeTurn] = {}
    for event in events:
        if event.account_id != account_id or event.event_id in turns_by_id:
            continue
        turn = practice_turn_from_evidence(event)
        if turn is not None:
            turns_by_id[event.event_id] = turn
    turns = sorted(turns_by_id.values(), key=lambda item: (item.occurred_at, item.event_id))
    weak = Counter[str]()
    mastered: set[str] = set()
    for turn in turns:
        if turn.outcome == "mastered":
            mastered.add(turn.skill_key)
            weak.pop(turn.skill_key, None)
        elif turn.outcome == "struggled":
            mastered.discard(turn.skill_key)
            weak[turn.skill_key] += 1
        elif turn.outcome == "gave_up":
            mastered.discard(turn.skill_key)
            weak[turn.skill_key] += 2
    active_days = tuple(sorted({turn.occurred_at.date() for turn in turns}))
    streak = 0
    if active_days:
        expected = active_days[-1]
        for day in reversed(active_days):
            if day != expected:
                break
            streak += 1
            expected -= timedelta(days=1)
    return StudyProgress(
        account_id=account_id,
        practiced_seconds=sum(turn.duration_seconds for turn in turns),
        active_days=active_days,
        current_streak_days=streak,
        weak_points=tuple(sorted(weak.items(), key=lambda item: (-item[1], item[0]))),
        mastered_skills=tuple(sorted(mastered)),
        source_event_ids=tuple(turn.event_id for turn in turns),
        last_practiced_at=turns[-1].occurred_at if turns else None,
    )

