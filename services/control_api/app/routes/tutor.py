"""Server-owned tutor catalog, practice lifecycle, and study projection routes."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import EvidenceEvent, IdempotencyConflictError, LifeArchivePort
from services.control_api.app.account_gate import (
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.guardian.domain import GuardianStorePort
from services.tutor.catalog import lesson_task, lessons_for
from services.tutor.domain import (
    LessonDifficulty,
    PracticeConflictError,
    PracticeEventAction,
    PracticeOutcome,
    PracticeSession,
    TutorFocus,
    TutorProjectionStorePort,
)
from services.tutor.progress import (
    PRACTICE_TURN_EVENT,
    apply_practice_event,
    project_study_progress,
)

router = APIRouter(prefix="/v1/tutor", tags=["tutor"])
_PROGRESS_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PracticeSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    focus: TutorFocus
    task_id: str = Field(min_length=1, max_length=128)


class PracticeEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["active", "paused", "completed", "practice"]
    expected_revision: int = Field(ge=0)
    duration_seconds: int = Field(default=0, ge=0, le=3 * 60 * 60)
    outcome: PracticeOutcome | None = None
    skill_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_practice_payload(self) -> PracticeEventCreate:
        if self.action == "practice":
            if self.outcome is None or self.skill_key is None or self.duration_seconds < 1:
                raise ValueError("practice events require outcome, skill_key, and duration")
        elif self.outcome is not None or self.skill_key is not None or self.duration_seconds:
            raise ValueError("only practice events may carry outcome, skill_key, or duration")
        return self


def _profiles(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _guardian(request: Request) -> GuardianStorePort:
    return cast(GuardianStorePort, request.app.state.guardian_store)


def _store(request: Request) -> TutorProjectionStorePort:
    return cast(TutorProjectionStorePort, request.app.state.tutor_store)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _idempotency_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not 8 <= len(normalized) <= 128:
        raise HTTPException(status_code=400, detail={"code": "idempotency_key_required"})
    return normalized


def _uuid(namespace: str, account_id: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{namespace}:{account_id}:{key}"))


def _lesson_payload(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "focus": task.focus,
        "title": task.title,
        "objective": task.objective,
        "opening_prompt": task.opening_prompt,
        "difficulty": task.difficulty,
        "skill_keys": list(task.skill_keys),
    }


def _session_payload(session: PracticeSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "focus": session.focus,
        "task_id": session.task_id,
        "status": session.status,
        "revision": session.revision,
        "practiced_seconds": session.practiced_seconds,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _progress_payload(progress: Any) -> dict[str, Any]:
    return {
        "practiced_seconds": progress.practiced_seconds,
        "study_minutes": progress.practiced_seconds // 60,
        "active_days": [value.isoformat() for value in progress.active_days],
        "current_streak_days": progress.current_streak_days,
        "weak_points": [
            {"skill_key": skill_key, "weight": weight}
            for skill_key, weight in progress.weak_points
        ],
        "mastered_skills": list(progress.mastered_skills),
        "source_event_count": len(progress.source_event_ids),
        "last_practiced_at": (
            progress.last_practiced_at.isoformat()
            if progress.last_practiced_at is not None
            else None
        ),
        "rebuildable": True,
    }


async def _require_tutor_write(request: Request, user: AuthenticatedUser) -> None:
    profile = _profiles(request).get_subject_profile(user_id=user.user_id)
    require_capability_for_subject(user, "tutor", store=_profiles(request))
    if profile is not None and profile.get("subject_category") == "minor":
        consent = await _guardian(request).active_consent(
            minor_user_id=user.user_id,
            consent_kind="memory_retention",
        )
        if consent is None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "guardian_consent_required",
                    "capability": "memory_retention",
                },
            )


@router.get("/lessons")
async def list_lessons(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    focus: TutorFocus = "tutor_english",
    difficulty: Annotated[LessonDifficulty | None, Query()] = None,
) -> dict[str, Any]:
    require_capability_for_subject(user, "tutor", store=_profiles(request))
    lessons = lessons_for(focus=focus, difficulty=difficulty)
    return {"items": [_lesson_payload(task) for task in lessons], "count": len(lessons)}


@router.post("/practice-sessions", status_code=status.HTTP_201_CREATED)
async def create_practice_session(
    body: PracticeSessionCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    await _require_tutor_write(request, user)
    key = _idempotency_key(idempotency_key)
    task = lesson_task(body.task_id)
    if task is None or task.focus != body.focus:
        raise HTTPException(status_code=422, detail={"code": "tutor_task_invalid"})
    session_id = _uuid("tutor-practice", user.user_id, key)
    existing = await _store(request).practice_session(
        account_id=user.user_id,
        session_id=session_id,
    )
    if existing is not None:
        if (existing.focus, existing.task_id) != (body.focus, body.task_id):
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
        return _session_payload(existing)
    event_id = f"{session_id}:create"
    now = datetime.now(UTC)
    session = apply_practice_event(
        None,
        event_id=event_id,
        account_id=user.user_id,
        focus=body.focus,
        task_id=body.task_id,
        action="create",
        occurred_at=now,
    )
    await _archive(request).record(
        EvidenceEvent(
            event_id=event_id,
            account_id=user.user_id,
            event_type="tutor.practice_session_created",
            occurred_at=now,
            speaker_class="owner",
            source="tutor.control_api",
            session_id=session_id,
            payload={
                "session_focus": body.focus,
                "task_id": body.task_id,
                "action": "create",
                "history_eligible": True,
                "owner_projection_eligible": True,
            },
        )
    )
    return _session_payload(await _store(request).save_practice_session(session))


@router.get("/practice-sessions/{session_id}")
async def get_practice_session(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "tutor", store=_profiles(request))
    session = await _store(request).practice_session(
        account_id=user.user_id,
        session_id=session_id,
    )
    if session is None:
        raise HTTPException(status_code=404, detail={"code": "practice_session_not_found"})
    return _session_payload(session)


@router.post("/practice-sessions/{session_id}/events")
async def append_practice_event(
    session_id: str,
    body: PracticeEventCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    await _require_tutor_write(request, user)
    key = _idempotency_key(idempotency_key)
    current = await _store(request).practice_session(
        account_id=user.user_id,
        session_id=session_id,
    )
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "practice_session_not_found"})
    event_id = f"tutor-practice-event:{_uuid('tutor-event', user.user_id, key)}"
    now = datetime.now(UTC)
    try:
        updated = apply_practice_event(
            current,
            event_id=event_id,
            account_id=user.user_id,
            focus=current.focus,
            task_id=current.task_id,
            action=cast(PracticeEventAction, body.action),
            expected_revision=body.expected_revision,
            duration_seconds=body.duration_seconds,
            occurred_at=now,
        )
    except PracticeConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    event_type = (
        PRACTICE_TURN_EVENT
        if body.action == "practice"
        else "tutor.practice_completed"
        if body.action == "completed"
        else "tutor.practice_session_changed"
    )
    payload: dict[str, Any] = {
        "session_focus": current.focus,
        "task_id": current.task_id,
        "action": body.action,
        "duration_seconds": (
            updated.practiced_seconds if body.action == "completed" else body.duration_seconds
        ),
        "history_eligible": True,
        "owner_projection_eligible": True,
    }
    if body.action == "practice":
        payload.update({"outcome": body.outcome, "skill_key": body.skill_key})
    try:
        await _archive(request).record(
            EvidenceEvent(
                event_id=event_id,
                account_id=user.user_id,
                event_type=event_type,
                occurred_at=now,
                speaker_class="owner",
                source="tutor.control_api",
                session_id=session_id,
                payload=payload,
            )
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"}) from exc
    try:
        saved = await _store(request).save_practice_session(updated)
    except PracticeConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    return _session_payload(saved)


@router.get("/progress")
async def study_progress(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    await _require_tutor_write(request, user)
    now = datetime.now(UTC)
    events = await _archive(request).evidence_window(
        account_id=user.user_id,
        occurred_after=_PROGRESS_EPOCH,
        occurred_before=now,
        event_types=(PRACTICE_TURN_EVENT,),
    )
    progress = project_study_progress(account_id=user.user_id, events=events)
    await _store(request).save_study_progress(progress, rebuilt_at=now)
    return _progress_payload(progress)
