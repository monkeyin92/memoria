"""Server-owned tutor catalog, subject-scoped practice lifecycle, and study projection.

PR-13 contract: the login account is only ever an *actor*.  Every read and
write resolves the data *subject* from the server-side session authority (a
signed Runtime Profile for the referenced voice session); clients cannot
self-report a subject, and outcome/skill values are never accepted from the
body (``extra=forbid``).  Learning outcomes are derived by the server-owned
rubric from signed, one-time assessment evidence minted by the assessment
authority.  Missing authorities fail closed (503/403).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import LifeArchivePort
from services.control_api.app.account_gate import (
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.multi_subject_runtime import MultiSubjectRuntimeControl
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.guardian.domain import GuardianStorePort
from services.session_runtime.profile_service import RuntimeProfileRejected
from services.tutor.authority import (
    TUTOR_ACTION_PURPOSE,
    TutorAssessmentAuthorityPort,
    TutorEvidenceGate,
    TutorEvidenceRejected,
    TutorFenceSnapshot,
    TutorPolicyReceiptVerifierPort,
    TutorReceiptExpectation,
    TutorScoringRubric,
    TutorSessionFencePort,
)
from services.tutor.catalog import lesson_task, lessons_for
from services.tutor.domain import (
    LessonDifficulty,
    PracticeConflictError,
    PracticeEventAction,
    PracticeSession,
    TutorAggregateCommit,
    TutorFocus,
    TutorPracticeCommitPort,
    TutorProjectionStorePort,
)
from services.tutor.progress import (
    PRACTICE_TURN_EVENT,
    apply_practice_event,
    project_study_progress,
)
from services.tutor.projection import drain_commit_outbox

router = APIRouter(prefix="/v1/tutor", tags=["tutor"])
_PROGRESS_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_RECEIPT_CAPABILITY = "tutor"
_RECEIPT_PURPOSE = TUTOR_ACTION_PURPOSE


class PracticeSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    focus: TutorFocus
    task_id: str = Field(min_length=1, max_length=128)
    voice_session_id: str = Field(min_length=1, max_length=256)


class PracticeEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["active", "paused", "completed", "practice"]
    expected_revision: int = Field(ge=0)
    duration_seconds: int = Field(default=0, ge=0, le=3 * 60 * 60)
    voice_session_id: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_practice_payload(self) -> PracticeEventCreate:
        if self.action == "practice":
            if self.duration_seconds < 1:
                raise ValueError("practice events require a positive duration")
        elif self.duration_seconds:
            raise ValueError("only practice events may carry duration_seconds")
        return self


def _profiles(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _guardian(request: Request) -> GuardianStorePort:
    return cast(GuardianStorePort, request.app.state.guardian_store)


def _store(request: Request) -> TutorProjectionStorePort:
    return cast(TutorProjectionStorePort, request.app.state.tutor_store)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _fence_port(request: Request) -> TutorSessionFencePort | None:
    return cast(TutorSessionFencePort | None, request.app.state.tutor_session_fence)


def _assessment_authority(request: Request) -> TutorAssessmentAuthorityPort | None:
    return cast(
        TutorAssessmentAuthorityPort | None,
        request.app.state.tutor_assessment_authority,
    )


def _receipt_verifier(request: Request) -> TutorPolicyReceiptVerifierPort | None:
    return cast(
        TutorPolicyReceiptVerifierPort | None,
        request.app.state.tutor_receipt_verifier,
    )


def _gate(request: Request) -> TutorEvidenceGate:
    return cast(TutorEvidenceGate, request.app.state.tutor_evidence_gate)


def _rubric(request: Request) -> TutorScoringRubric:
    return cast(TutorScoringRubric, request.app.state.tutor_scoring_rubric)


def _commit_port(request: Request) -> TutorPracticeCommitPort:
    return cast(TutorPracticeCommitPort, request.app.state.tutor_store)


def _idempotency_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not 8 <= len(normalized) <= 128:
        raise HTTPException(status_code=400, detail={"code": "idempotency_key_required"})
    return normalized


def _uuid(namespace: str, subject_id: str, key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{namespace}:{subject_id}:{key}"))


class RuntimeProfileFencePort:
    """Server-side subject authority over the signed runtime profile."""

    def __init__(self, runtime: MultiSubjectRuntimeControl) -> None:
        self._runtime = runtime

    async def resolve_fence(
        self,
        *,
        actor_id: str,
        voice_session_id: str,
        now: datetime,
    ) -> TutorFenceSnapshot | None:
        profile = self._runtime.authority.current_profile(voice_session_id)
        if profile is None or profile.actor_id != actor_id:
            return None
        try:
            self._runtime.profiles.require_valid(profile, now=now)
        except RuntimeProfileRejected:
            return None
        if profile.active_subject_id is None or not profile.runtime_profile_id:
            return None
        return TutorFenceSnapshot(
            voice_session_id=voice_session_id,
            actor_id=profile.actor_id,
            active_subject_id=profile.active_subject_id,
            device_id=profile.device_id,
            binding_id=profile.binding_id,
            binding_version=profile.binding_version,
            subject_revision=profile.subject_revision,
            session_epoch=profile.session_epoch,
            runtime_profile_id=profile.runtime_profile_id,
            policy_receipt_ids=tuple(profile.policy_receipt_ids),
            expires_at=profile.expires_at,
            resolved_at=now,
        )


async def _resolve_self_subject_fence(
    request: Request,
    user: AuthenticatedUser,
    voice_session_id: str,
    *,
    now: datetime,
) -> TutorFenceSnapshot:
    """Resolve the subject from the session authority; never from the body."""

    port = _fence_port(request)
    if port is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_authority_unavailable"},
        )
    fence = await port.resolve_fence(
        actor_id=user.user_id,
        voice_session_id=voice_session_id,
        now=now,
    )
    if fence is None:
        raise HTTPException(status_code=403, detail={"code": "tutor_session_unknown"})
    # The web control plane is self-service: a guardian operating a child
    # subject must use the guardian aggregate paths, not this endpoint.
    if fence.actor_id != fence.active_subject_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "tutor_actor_subject_mismatch"},
        )
    return fence


def _receipt_expectation_template(
    *,
    fence: TutorFenceSnapshot,
    action_resource_id: str,
    action_revision: int,
) -> TutorReceiptExpectation:
    """The literal receipt expectation; the canonical action fence is built
    by the authority at issue time with the exact counters and evidence."""

    return TutorReceiptExpectation(
        capability=_RECEIPT_CAPABILITY,
        purpose=_RECEIPT_PURPOSE,
        actor_id=fence.actor_id,
        subject_id=fence.active_subject_id,
        device_id=fence.device_id,
        binding_id=fence.binding_id,
        binding_version=fence.binding_version,
        session_id=fence.voice_session_id,
        session_epoch=fence.session_epoch,
        runtime_profile_id=fence.runtime_profile_id,
        subject_revision=fence.subject_revision,
        action_resource_id=action_resource_id,
        action_revision=action_revision,
    )


def _require_receipt_verifier(request: Request) -> TutorPolicyReceiptVerifierPort:
    verifier = _receipt_verifier(request)
    if verifier is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "receipt_authority_unavailable"},
        )
    return verifier


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
        "subject_id": session.subject_id,
        "actor_id": session.actor_id,
        "voice_session_id": session.voice_session_id,
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
        "subject_id": progress.subject_id,
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
    now = datetime.now(UTC)
    fence = await _resolve_self_subject_fence(
        request,
        user,
        body.voice_session_id,
        now=now,
    )
    session_id = _uuid("tutor-practice", fence.active_subject_id, key)
    existing = await _store(request).practice_session(
        subject_id=fence.active_subject_id,
        session_id=session_id,
    )
    if existing is not None:
        if (
            existing.actor_id,
            existing.voice_session_id,
            existing.focus,
            existing.task_id,
        ) != (fence.actor_id, fence.voice_session_id, body.focus, body.task_id):
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
        return _session_payload(existing)
    event_id = f"{session_id}:create"
    session = apply_practice_event(
        None,
        event_id=event_id,
        subject_id=fence.active_subject_id,
        actor_id=fence.actor_id,
        voice_session_id=fence.voice_session_id,
        focus=body.focus,
        task_id=body.task_id,
        action="create",
        occurred_at=now,
    )
    # Lifecycle events are DO_NOT_PERSIST: they carry no receipt-authorized
    # progress write and must not enter the archive or any projection.  The
    # subject-fenced session row itself is the record.
    return _session_payload(await _store(request).save_practice_session(session))


@router.get("/practice-sessions/{session_id}")
async def get_practice_session(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    voice_session_id: Annotated[str, Query(min_length=1, max_length=256)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "tutor", store=_profiles(request))
    now = datetime.now(UTC)
    fence = await _resolve_self_subject_fence(
        request,
        user,
        voice_session_id,
        now=now,
    )
    session = await _store(request).practice_session(
        subject_id=fence.active_subject_id,
        session_id=session_id,
    )
    if session is None:
        raise HTTPException(status_code=404, detail={"code": "practice_session_not_found"})
    if session.actor_id != fence.actor_id or session.voice_session_id != fence.voice_session_id:
        raise HTTPException(status_code=403, detail={"code": "tutor_session_scope_mismatch"})
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
    now = datetime.now(UTC)
    fence = await _resolve_self_subject_fence(
        request,
        user,
        body.voice_session_id,
        now=now,
    )
    current = await _store(request).practice_session(
        subject_id=fence.active_subject_id,
        session_id=session_id,
    )
    if current is None:
        raise HTTPException(status_code=404, detail={"code": "practice_session_not_found"})
    if (
        current.actor_id != fence.actor_id
        or current.voice_session_id != fence.voice_session_id
    ):
        raise HTTPException(status_code=403, detail={"code": "tutor_session_scope_mismatch"})
    if body.action == "practice":
        return await _append_practice_turn(
            request,
            current,
            fence,
            user,
            body,
            key,
            now,
        )
    event_id = f"tutor-practice-event:{_uuid('tutor-event', fence.active_subject_id, key)}"
    if event_id in current.event_ids:
        return _session_payload(current)
    try:
        updated = apply_practice_event(
            current,
            event_id=event_id,
            subject_id=fence.active_subject_id,
            actor_id=fence.actor_id,
            voice_session_id=fence.voice_session_id,
            focus=current.focus,
            task_id=current.task_id,
            action=cast(PracticeEventAction, body.action),
            expected_revision=body.expected_revision,
            duration_seconds=body.duration_seconds,
            occurred_at=now,
        )
    except PracticeConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    # Session-state events (active/paused/completed) are DO_NOT_PERSIST:
    # without an exact action receipt they must not enter the archive or any
    # projection.  The subject-fenced session CAS is the record.
    try:
        saved = await _store(request).save_practice_session(updated)
    except PracticeConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    return _session_payload(saved)


async def _append_practice_turn(
    request: Request,
    current: PracticeSession,
    fence: TutorFenceSnapshot,
    user: AuthenticatedUser,
    body: PracticeEventCreate,
    key: str,
    now: datetime,
) -> dict[str, Any]:
    authority = _assessment_authority(request)
    if authority is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "assessment_authority_unavailable"},
        )
    task = lesson_task(current.task_id)
    if task is None:
        raise HTTPException(status_code=422, detail={"code": "tutor_task_invalid"})
    event_id = f"tutor-practice-event:{_uuid('tutor-evidence', fence.active_subject_id, key)}"
    if event_id in current.event_ids:
        return _session_payload(current)
    verifier = _require_receipt_verifier(request)
    receipt_template = _receipt_expectation_template(
        fence=fence,
        action_resource_id=event_id,
        action_revision=body.expected_revision + 1,
    )
    try:
        grant = await authority.issue_assessment(
            fence=fence,
            task=task,
            duration_seconds=body.duration_seconds,
            now=now,
            event_id=event_id,
            receipt_template=receipt_template,
            receipt_verifier=verifier,
        )
    except TutorEvidenceRejected as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code}) from exc
    assessment_id = grant.assessment_id
    evidence = await authority.fetch_assessment(assessment_id=assessment_id)
    if evidence is None:
        raise HTTPException(status_code=503, detail={"code": "assessment_unavailable"})
    if evidence.policy_receipt_id != grant.receipt_id:
        raise HTTPException(status_code=403, detail={"code": "receipt_mismatch"})
    if evidence.duration_seconds != body.duration_seconds:
        # The idempotency key binds the request: a retry with a different
        # duration must never mix the cached evidence with new session math.
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
    try:
        _gate(request).verify(
            evidence=evidence,
            fence=fence,
            known_event_ids=current.event_ids,
            now=now,
            receipt_expires_at=grant.receipt_expires_at,
        )
    except TutorEvidenceRejected as exc:
        raise HTTPException(
            status_code=409 if exc.code == "replayed_evidence" else 403,
            detail={"code": exc.code},
        ) from exc
    outcome, skill_key = _rubric(request).score(evidence, task)
    try:
        updated = apply_practice_event(
            current,
            event_id=event_id,
            subject_id=fence.active_subject_id,
            actor_id=fence.actor_id,
            voice_session_id=fence.voice_session_id,
            focus=current.focus,
            task_id=current.task_id,
            action="practice",
            expected_revision=body.expected_revision,
            duration_seconds=body.duration_seconds,
            occurred_at=now,
        )
    except PracticeConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    envelope = {
        **_unsigned_envelope(evidence),
        "assessment_id": assessment_id,
        "signature": evidence.signature,
    }
    archive_payload = {
        "event_id": event_id,
        "account_id": user.user_id,
        "event_type": PRACTICE_TURN_EVENT,
        "occurred_at": now.isoformat(),
        "speaker_class": "owner",
        "source": "tutor.control_api",
        "session_id": current.session_id,
        "payload": {
            "session_focus": current.focus,
            "task_id": current.task_id,
            "action": "practice",
            "duration_seconds": body.duration_seconds,
            "history_eligible": True,
            "owner_projection_eligible": True,
            **envelope,
            "outcome": outcome,
            "skill_key": skill_key,
        },
    }
    try:
        saved = await _commit_port(request).commit_aggregate(
            commit=TutorAggregateCommit(
                event_id=event_id,
                kind="tutor.practice_turn_recorded",
                subject_id=fence.active_subject_id,
                actor_id=fence.actor_id,
                receipt_id=grant.receipt_id,
                receipt_expectation=receipt_template,
                action_fence=grant.action_fence,
                assessment_id=assessment_id,
                evidence_envelope=envelope,
                outcome=outcome,
                skill_key=skill_key,
                archive_payload=archive_payload,
                session=updated,
                expected_revision=body.expected_revision,
                occurred_at=now,
            ),
            receipt_verifier=verifier,
            now=now,
        )
    except TutorEvidenceRejected as exc:
        raise HTTPException(
            status_code=403
            if exc.code in {"tutor_receipt_required", "receipt_expired"}
            else 409,
            detail={"code": exc.code},
        ) from exc
    except PracticeConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"}) from exc
    await _drain_best_effort(request, subject_id=fence.active_subject_id)
    return _session_payload(saved)


def _unsigned_envelope(evidence: Any) -> dict[str, Any]:
    return {
        "issuer": evidence.issuer,
        "event_id": evidence.event_id,
        "subject_id": evidence.subject_id,
        "actor_id": evidence.actor_id,
        "voice_session_id": evidence.voice_session_id,
        "device_id": evidence.device_id,
        "binding_id": evidence.binding_id,
        "binding_version": evidence.binding_version,
        "subject_revision": evidence.subject_revision,
        "task_id": evidence.task_id,
        "focus": evidence.focus,
        "occurred_at": evidence.occurred_at.isoformat(),
        "duration_seconds": evidence.duration_seconds,
        "session_epoch": evidence.session_epoch,
        "runtime_profile_id": evidence.runtime_profile_id,
        "generation_id": evidence.generation_id,
        "turn_id": evidence.turn_id,
        "tool_epoch": evidence.tool_epoch,
        "policy_receipt_id": evidence.policy_receipt_id,
        "criterion_ids": list(evidence.criterion_ids),
        "correctness_score": evidence.correctness_score,
        "evaluator_version": evidence.evaluator_version,
        "support_level": evidence.support_level,
        "completion": evidence.completion,
        "attempt_count": evidence.attempt_count,
    }


async def _drain_best_effort(
    request: Request,
    *,
    subject_id: str,
) -> None:
    """Best-effort outbox drain; the durable worker is the recovery path."""

    try:
        await drain_commit_outbox(
            _commit_port(request),
            _archive(request),
            worker_id=f"control-api:{subject_id}",
            subject_id=subject_id,
        )
    except Exception:
        # The commit is already durable; a drain failure must never fail the
        # response or hide state.  Pending rows are retried by the worker and
        # by later route calls.
        return


@router.get("/progress")
async def study_progress(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    voice_session_id: Annotated[str, Query(min_length=1, max_length=256)],
) -> dict[str, Any]:
    await _require_tutor_write(request, user)
    now = datetime.now(UTC)
    fence = await _resolve_self_subject_fence(
        request,
        user,
        voice_session_id,
        now=now,
    )
    await _drain_best_effort(request, subject_id=fence.active_subject_id)
    events = await _archive(request).evidence_window(
        account_id=user.user_id,
        occurred_after=_PROGRESS_EPOCH,
        occurred_before=now,
        event_types=(PRACTICE_TURN_EVENT,),
    )
    progress = project_study_progress(
        subject_id=fence.active_subject_id,
        events=events,
    )
    await _store(request).save_study_progress(progress, rebuilt_at=now)
    return _progress_payload(progress)
