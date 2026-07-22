"""Authenticated S4 growth-map and ledger-backed learning task APIs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from services.archive.domain import EvidenceEvent, IdempotencyConflictError
from services.control_api.app.account_gate import AccountDeletingError, AccountOperationGate
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.growth.domain import GrowthTask, TaskConflictError, TaskKind
from services.growth.reader import GrowthReader
from services.growth.tasks import apply_task_event
from services.growth.tasks_catalog import prompt_for, prompt_kind_for

router = APIRouter(prefix="/v1/growth", tags=["growth"])


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _reader(request: Request) -> GrowthReader:
    return cast(GrowthReader, request.app.state.growth_reader)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _task_lock(request: Request) -> asyncio.Lock:
    return cast(asyncio.Lock, request.app.state.growth_task_lock)


def _registered(request: Request, user: AuthenticatedUser) -> None:
    if _store(request).is_account_unavailable(user_id=user.user_id):
        raise _error(status.HTTP_409_CONFLICT, "account_deletion_in_progress")
    if _store(request).get_account(user_id=user.user_id) is None:
        raise _error(status.HTTP_403_FORBIDDEN, "account_not_registered")


async def _write(request: Request, user: AuthenticatedUser, event: EvidenceEvent) -> bool:
    gate = cast(AccountOperationGate, request.app.state.account_operations)
    try:
        async with gate.write(user.user_id):
            if _store(request).is_account_unavailable(user_id=user.user_id):
                raise _error(status.HTTP_409_CONFLICT, "account_deletion_in_progress")
            result = await request.app.state.life_archive.record(event)
    except AccountDeletingError as exc:
        raise _error(status.HTTP_409_CONFLICT, "account_deletion_in_progress") from exc
    except IdempotencyConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "revision_conflict") from exc
    worker = getattr(request.app.state, "memory_compiler_worker", None)
    if worker is not None:
        worker.wake()
    return bool(result.duplicate)


def _task_payload(task: GrowthTask) -> dict[str, Any]:
    prompt_id, prompt = prompt_for(task.kind, task.prompt_id)
    return {
        "task_id": task.task_id,
        "kind": task.kind,
        "status": task.status,
        "revision": task.revision,
        "prompt_id": prompt_id,
        "prompt": prompt,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_id: str = Field(min_length=1, max_length=128)
    kind: TaskKind
    prompt_id: str | None = Field(default=None, max_length=64)


class TransitionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_id: str = Field(min_length=1, max_length=128)
    to_status: Literal["active", "paused", "completed", "cancelled"]
    expected_revision: int = Field(ge=0)


class ResponseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    answer: str = Field(default="", max_length=8000)


class OwnerActionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    event_id: str = Field(min_length=1, max_length=128)
    action: Literal["not_me", "would_not_say"]
    target_kind: Literal[
        "memory_claim",
        "persona_trait",
        "source_event",
        "digital_self_version",
        "person_entity",
        "timeline_entry",
        "relationship",
        "voice_profile",
    ]
    target_id: str = Field(min_length=1, max_length=128)


@router.get("/overview")
async def overview(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _registered(request, user)
    return await _reader(request).overview(account_id=user.user_id)


@router.get("/tasks")
async def list_tasks(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _registered(request, user)
    return {"items": [_task_payload(task) for task in await _reader(request).tasks(account_id=user.user_id)]}


@router.post("/tasks")
async def create_task(
    body: TaskCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    _registered(request, user)
    try:
        prompt_id, _ = prompt_for(body.kind, body.prompt_id)
    except ValueError as exc:
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "ineligible_owner_source") from exc
    task_id = body.event_id
    async with _task_lock(request):
        event = EvidenceEvent(
            event_id=body.event_id,
            account_id=user.user_id,
            event_type="learning.task_created",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="user.growth_task",
            payload={"task_id": task_id, "task_kind": body.kind, "prompt_id": prompt_id},
        )
        duplicate = await _write(request, user, event)
        task = await _reader(request).task(account_id=user.user_id, task_id=task_id)
        if task is None:
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "ineligible_owner_source")
    response = _task_payload(task)
    response["duplicate"] = duplicate
    return JSONResponse(status_code=status.HTTP_200_OK if duplicate else status.HTTP_201_CREATED, content=response)


@router.post("/tasks/{task_id}/transitions")
async def transition_task(
    task_id: str,
    body: TransitionCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _registered(request, user)
    async with _task_lock(request):
        task = await _reader(request).task(account_id=user.user_id, task_id=task_id)
        if task is None:
            raise _error(status.HTTP_404_NOT_FOUND, "task_not_found")
        event = EvidenceEvent(event_id=body.event_id, account_id=user.user_id, event_type="learning.task_transitioned", occurred_at=datetime.now(UTC), speaker_class="owner", source="user.growth_task", payload={"task_id": task_id, "task_kind": task.kind, "to_status": body.to_status, "expected_revision": body.expected_revision, "prompt_id": task.prompt_id})
        if body.event_id in task.event_ids:
            await _write(request, user, event)
            return _task_payload(task)
        try:
            apply_task_event(task, event_id=body.event_id, kind=task.kind, action=body.to_status, expected_revision=body.expected_revision)
        except TaskConflictError as exc:
            raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
        await _write(request, user, event)
        updated = await _reader(request).task(account_id=user.user_id, task_id=task_id)
        if updated is None or updated.status == task.status:
            raise _error(status.HTTP_409_CONFLICT, "invalid_transition")
    return _task_payload(updated)


@router.post("/tasks/{task_id}/responses")
async def record_response(
    task_id: str,
    body: ResponseCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _registered(request, user)
    async with _task_lock(request):
        task = await _reader(request).task(account_id=user.user_id, task_id=task_id)
        if task is None:
            raise _error(status.HTTP_404_NOT_FOUND, "task_not_found")
        if task.kind != "natural_chat" and not body.answer.strip():
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "ineligible_owner_source")
        action_type = {"natural_chat": "reflection", "life_interview": "reflection", "scenario_choice": "choice", "decision_review": "decision_review"}[task.kind]
        event = EvidenceEvent(event_id=body.event_id, account_id=user.user_id, event_type="owner.action_recorded", occurred_at=datetime.now(UTC), speaker_class="owner", source="user.growth_response", payload={"task_id": task_id, "task_kind": task.kind, "action_type": action_type, "prompt_id": task.prompt_id, "prompt_kind": prompt_kind_for(task.kind), "text": body.answer, "expected_revision": body.expected_revision, "owner_projection_eligible": True})
        if body.event_id in task.event_ids:
            await _write(request, user, event)
            return _task_payload(task)
        try:
            apply_task_event(task, event_id=body.event_id, kind=task.kind, action="response", expected_revision=body.expected_revision)
        except TaskConflictError as exc:
            raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
        await _write(request, user, event)
        updated = await _reader(request).task(account_id=user.user_id, task_id=task_id)
        if updated is None:
            raise _error(status.HTTP_409_CONFLICT, "invalid_transition")
    return _task_payload(updated)


@router.post("/owner-actions")
async def owner_action(
    body: OwnerActionCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    _registered(request, user)
    if not await _reader(request).target_belongs(account_id=user.user_id, target_kind=body.target_kind, target_id=body.target_id):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "feedback_target_mismatch")
    event = EvidenceEvent(event_id=body.event_id, account_id=user.user_id, event_type="owner.action_recorded", occurred_at=datetime.now(UTC), speaker_class="owner", source="user.growth_feedback", payload={"action_type": body.action, "target_kind": body.target_kind, "target_id": body.target_id, "owner_projection_eligible": True})
    duplicate = await _write(request, user, event)
    return JSONResponse(
        status_code=status.HTTP_200_OK if duplicate else status.HTTP_201_CREATED,
        content={"event_id": event.event_id, "action": body.action, "target_kind": body.target_kind, "target_id": body.target_id},
    )
