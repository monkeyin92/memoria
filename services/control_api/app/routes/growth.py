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
from services.self_model.domain import (
    InvalidSelfModelTransitionError,
    RelationshipProfile,
    SelfModelIdempotencyConflictError,
    SelfModelItem,
    SelfModelItemKind,
    SelfModelNotFoundError,
    SelfModelRegistryPort,
    SelfModelVersionConflictError,
    SourceInput,
    UntrustedSelfModelSourceError,
)

router = APIRouter(prefix="/v1/growth", tags=["growth"])


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _reader(request: Request) -> GrowthReader:
    return cast(GrowthReader, request.app.state.growth_reader)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _self_model_registry(request: Request) -> SelfModelRegistryPort:
    return cast(SelfModelRegistryPort, request.app.state.self_model_registry)


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


async def _project_growth_decision_candidate(
    request: Request,
    *,
    account_id: str,
    task: GrowthTask,
    event: EvidenceEvent,
    body: ResponseCreate,
) -> None:
    if task.kind not in {"scenario_choice", "decision_review"}:
        return
    registry = _self_model_registry(request)
    idempotency_key = f"growth-response:{event.event_id}:decision"
    try:
        await registry.create_decision_case(
            account_id=account_id,
            **_decision_projection(task=task, body=body),
            idempotency_key=idempotency_key,
            sources=(SourceInput(source_event_id=event.event_id),),
        )
    except (
        InvalidSelfModelTransitionError,
        SelfModelIdempotencyConflictError,
        SelfModelNotFoundError,
        SelfModelVersionConflictError,
        UntrustedSelfModelSourceError,
        ValueError,
    ) as exc:
        raise _error(status.HTTP_409_CONFLICT, "self_model_projection_conflict") from exc


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
    options: list[Annotated[str, Field(min_length=1, max_length=1000)]] | None = Field(
        default=None, max_length=32
    )
    constraints: list[Annotated[str, Field(min_length=1, max_length=1000)]] | None = Field(
        default=None, max_length=32
    )
    chosen_option: str | None = Field(default=None, min_length=1, max_length=1000)
    rejected_options: list[Annotated[str, Field(min_length=1, max_length=1000)]] | None = Field(
        default=None, max_length=32
    )
    outcome: str | None = Field(default=None, min_length=1, max_length=4000)
    reflection: str | None = Field(default=None, min_length=1, max_length=4000)
    still_endorsed: bool | None = None


_UNRESOLVED_DECISION_OPTION = "信息不足（未提供可核验的结构化备选）"


def _has_structured_decision_fields(body: ResponseCreate) -> bool:
    return any(
        value is not None
        for value in (
            body.options,
            body.constraints,
            body.chosen_option,
            body.rejected_options,
            body.outcome,
            body.reflection,
            body.still_endorsed,
        )
    )


def _is_complete_decision_review(body: ResponseCreate) -> bool:
    options = body.options
    constraints = body.constraints
    rejected_options = body.rejected_options
    return (
        options is not None
        and len(options) >= 2
        and body.chosen_option in options
        and constraints is not None
        and bool(constraints)
        and rejected_options is not None
        and bool(rejected_options)
        and all(option in options and option != body.chosen_option for option in rejected_options)
        and body.outcome is not None
        and body.reflection is not None
        and body.still_endorsed is not None
    )


def _decision_projection(*, task: GrowthTask, body: ResponseCreate) -> dict[str, Any]:
    _, prompt = prompt_for(task.kind, task.prompt_id)
    complete_real_review = (
        task.kind == "decision_review" and _is_complete_decision_review(body)
    )
    options = body.options
    if not options or body.chosen_option not in options:
        return {
            "kind": "hypothetical",
            "context": f"{prompt}\n\n候选状态：信息不足，尚未提供结构化选择。",
            "options": (_UNRESOLVED_DECISION_OPTION,),
            "constraints": (),
            "chosen_option": _UNRESOLVED_DECISION_OPTION,
            "rejected_options": (),
            "outcome": "",
            "reflection": body.reflection or body.answer,
            "still_endorsed": False,
        }
    return {
        "kind": "real" if complete_real_review else "hypothetical",
        "context": prompt,
        "options": tuple(options),
        "constraints": tuple(body.constraints or ()),
        "chosen_option": body.chosen_option,
        "rejected_options": tuple(body.rejected_options or ()),
        "outcome": body.outcome or "",
        "reflection": body.reflection or body.answer,
        "still_endorsed": (
            body.still_endorsed
            if complete_real_review
            or (task.kind == "scenario_choice" and body.still_endorsed is not None)
            else False
        ),
    }


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
        "cognitive_claim",
        "decision_case",
        "relationship_profile",
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
        if (
            task.kind != "natural_chat"
            and not body.answer.strip()
            and not (
                task.kind in {"scenario_choice", "decision_review"}
                and _has_structured_decision_fields(body)
            )
        ):
            raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "ineligible_owner_source")
        action_type = {"natural_chat": "reflection", "life_interview": "reflection", "scenario_choice": "choice", "decision_review": "decision_review"}[task.kind]
        event_payload: dict[str, Any] = {
            "task_id": task_id,
            "task_kind": task.kind,
            "action_type": action_type,
            "prompt_id": task.prompt_id,
            "prompt_kind": prompt_kind_for(task.kind),
            "text": body.answer,
            "expected_revision": body.expected_revision,
            "owner_projection_eligible": True,
        }
        if task.kind in {"scenario_choice", "decision_review"}:
            event_payload.update(
                {
                    field: value
                    for field, value in (
                        ("options", body.options),
                        ("constraints", body.constraints),
                        ("chosen_option", body.chosen_option),
                        ("rejected_options", body.rejected_options),
                        ("outcome", body.outcome),
                        ("reflection", body.reflection),
                        ("still_endorsed", body.still_endorsed),
                    )
                    if value is not None
                }
            )
            event_payload["decision_projection"] = (
                "real"
                if task.kind == "decision_review" and _is_complete_decision_review(body)
                else "unresolved"
            )
        event = EvidenceEvent(
            event_id=body.event_id,
            account_id=user.user_id,
            event_type="owner.action_recorded",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="user.growth_response",
            payload=event_payload,
        )
        if body.event_id in task.event_ids:
            await _write(request, user, event)
            await _project_growth_decision_candidate(
                request,
                account_id=user.user_id,
                task=task,
                event=event,
                body=body,
            )
            return _task_payload(task)
        try:
            apply_task_event(task, event_id=body.event_id, kind=task.kind, action="response", expected_revision=body.expected_revision)
        except TaskConflictError as exc:
            raise _error(status.HTTP_409_CONFLICT, str(exc)) from exc
        await _write(request, user, event)
        await _project_growth_decision_candidate(
            request,
            account_id=user.user_id,
            task=task,
            event=event,
            body=body,
        )
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
    if body.target_kind in {
        "cognitive_claim",
        "decision_case",
        "relationship_profile",
    }:
        registry = _self_model_registry(request)
        try:
            item: SelfModelItem
            if body.target_kind == "cognitive_claim":
                item = await registry.get_cognitive_claim(
                    account_id=user.user_id,
                    claim_id=body.target_id,
                )
                item_id = item.claim_id
            elif body.target_kind == "decision_case":
                item = await registry.get_decision_case(
                    account_id=user.user_id,
                    case_id=body.target_id,
                )
                item_id = item.case_id
            else:
                item = await registry.get_relationship_profile(
                    account_id=user.user_id,
                    profile_id=body.target_id,
                )
                item_id = item.profile_id
            expected_version = (
                item.version_number
                if isinstance(item, RelationshipProfile)
                else item.version
            )
            if not any(
                source.source_event_id == event.event_id and source.negative
                for source in item.sources
            ):
                await registry.add_source(
                    account_id=user.user_id,
                    item_kind=cast(SelfModelItemKind, body.target_kind),
                    item_id=item_id,
                    source_event_id=event.event_id,
                    relation="counterexample",
                    adopted=False,
                    negative=True,
                    expected_version=expected_version,
                    idempotency_key=f"growth-feedback:{event.event_id}",
                )
        except (
            SelfModelIdempotencyConflictError,
            InvalidSelfModelTransitionError,
            SelfModelNotFoundError,
            SelfModelVersionConflictError,
            UntrustedSelfModelSourceError,
        ) as exc:
            raise _error(status.HTTP_409_CONFLICT, "self_model_feedback_conflict") from exc
    return JSONResponse(
        status_code=status.HTTP_200_OK if duplicate else status.HTTP_201_CREATED,
        content={"event_id": event.event_id, "action": body.action, "target_kind": body.target_kind, "target_id": body.target_id},
    )
