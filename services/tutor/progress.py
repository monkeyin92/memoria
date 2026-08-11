"""Pure replay for practice sessions and privacy-minimized study progress."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from services.archive.domain import EvidenceEvent
from services.tutor.catalog import criteria_for, lesson_task
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
    subject_id: str,
    actor_id: str,
    voice_session_id: str,
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
            subject_id=subject_id,
            actor_id=actor_id,
            voice_session_id=voice_session_id,
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
    if (subject_id, actor_id, voice_session_id, focus, task_id) != (
        session.subject_id,
        session.actor_id,
        session.voice_session_id,
        session.focus,
        session.task_id,
    ):
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
        subject_id=session.subject_id,
        actor_id=session.actor_id,
        voice_session_id=session.voice_session_id,
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


def practice_turn_from_evidence(
    event: EvidenceEvent,
    *,
    subject_id: str,
) -> PracticeTurn | None:
    """Fail closed unless the event is a subject-bound, fence-complete tutor signal."""

    payload = dict(event.payload)
    focus = payload.get("session_focus")
    outcome = payload.get("outcome")
    duration = payload.get("duration_seconds")
    session_epoch = payload.get("session_epoch")
    generation_id = payload.get("generation_id")
    turn_id = payload.get("turn_id")
    tool_epoch = payload.get("tool_epoch")
    subject_revision = payload.get("subject_revision")
    binding_version = payload.get("binding_version")
    if (
        event.event_type != PRACTICE_TURN_EVENT
        or event.speaker_class != "owner"
        or payload.get("history_eligible") is not True
        or payload.get("subject_id") != subject_id
        or not isinstance(payload.get("actor_id"), str)
        or not payload.get("actor_id")
        or not isinstance(payload.get("voice_session_id"), str)
        or not payload.get("voice_session_id")
        or focus not in {"tutor_english", "tutor_homework"}
        or outcome not in {"attempted", "supported", "mastered", "struggled", "gave_up"}
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 0 <= duration <= 3 * 60 * 60
        or isinstance(session_epoch, bool)
        or not isinstance(session_epoch, int)
        or session_epoch < 1
        or not isinstance(payload.get("runtime_profile_id"), str)
        or not payload.get("runtime_profile_id")
        or not isinstance(payload.get("policy_receipt_id"), str)
        or not payload.get("policy_receipt_id")
        or not isinstance(payload.get("device_id"), str)
        or not payload.get("device_id")
        or not isinstance(payload.get("binding_id"), str)
        or not payload.get("binding_id")
        or isinstance(binding_version, bool)
        or not isinstance(binding_version, int)
        or binding_version < 1
        or isinstance(subject_revision, bool)
        or not isinstance(subject_revision, int)
        or subject_revision < 0
        or not isinstance(payload.get("criterion_ids"), list)
        or not payload["criterion_ids"]
        or any(
            not isinstance(value, str) or not value.strip()
            for value in payload["criterion_ids"]
        )
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (generation_id, turn_id, tool_epoch)
        )
    ):
        return None
    session_id = event.session_id or _bounded_text(payload, "session_id")
    task_id = _bounded_text(payload, "task_id")
    skill_key = _bounded_text(payload, "skill_key")
    if session_id is None or task_id is None or skill_key is None:
        return None
    task = lesson_task(task_id)
    if task is None or skill_key not in task.skill_keys:
        return None
    criteria = criteria_for(task)
    criterion_ids = {str(value) for value in payload["criterion_ids"]}
    if not criterion_ids or not criterion_ids <= {item.criterion_id for item in criteria}:
        return None
    return PracticeTurn(
        event_id=event.event_id,
        subject_id=subject_id,
        actor_id=str(payload["actor_id"]),
        session_id=session_id,
        voice_session_id=str(payload["voice_session_id"]),
        device_id=str(payload["device_id"]),
        binding_id=str(payload["binding_id"]),
        binding_version=binding_version,
        subject_revision=subject_revision,
        task_id=task_id,
        focus=cast(TutorFocus, focus),
        occurred_at=event.occurred_at,
        duration_seconds=duration,
        skill_key=skill_key,
        outcome=cast(PracticeOutcome, outcome),
        history_eligible=True,
        session_epoch=session_epoch,
        runtime_profile_id=str(payload["runtime_profile_id"]),
        generation_id=cast(int, generation_id),
        turn_id=cast(int, turn_id),
        tool_epoch=cast(int, tool_epoch),
        policy_receipt_id=str(payload["policy_receipt_id"]),
    )


def project_study_progress(
    *,
    subject_id: str,
    events: Iterable[EvidenceEvent],
) -> StudyProgress:
    """Rebuild progress deterministically from immutable Evidence events."""

    turns_by_id: dict[str, PracticeTurn] = {}
    for event in events:
        if event.event_id in turns_by_id:
            continue
        turn = practice_turn_from_evidence(event, subject_id=subject_id)
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
        subject_id=subject_id,
        actor_id=turns[-1].actor_id if turns else None,
        practiced_seconds=sum(turn.duration_seconds for turn in turns),
        active_days=active_days,
        current_streak_days=streak,
        weak_points=tuple(sorted(weak.items(), key=lambda item: (-item[1], item[0]))),
        mastered_skills=tuple(sorted(mastered)),
        source_event_ids=tuple(turn.event_id for turn in turns),
        last_practiced_at=turns[-1].occurred_at if turns else None,
    )
