"""Owner-controlled procedural-memory proposals, approvals and run confirmations."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import EvidenceEvent, LifeArchivePort
from services.archive.memory_domain import DomainCategory, MemorySensitivity
from services.archive.skill_domain import (
    SkillApproval,
    SkillApprovalRequiredError,
    SkillCatalogPort,
    SkillConfirmationRequiredError,
    SkillNotFoundError,
    SkillProposal,
    SkillRun,
    SkillSchemaValidationError,
    SkillSourceKind,
    SkillStepDefinition,
    SkillVersion,
    skill_input_sha256,
)
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)

router = APIRouter(prefix="/v1/skills", tags=["skills"])


class SkillStepBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    step_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any]
    compensation_tool_name: str | None = Field(default=None, min_length=1, max_length=128)
    compensation_arguments: dict[str, Any] | None = None

    @model_validator(mode="after")
    def enforce_compensation_pair(self) -> SkillStepBody:
        if (self.compensation_tool_name is None) != (
            self.compensation_arguments is None
        ):
            raise ValueError(
                "compensation_tool_name and compensation_arguments are required together"
            )
        return self


class SkillProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    trigger_phrases: list[str] = Field(min_length=1, max_length=20)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    output_template: dict[str, Any]
    allowed_tools: list[str] = Field(min_length=1, max_length=32)
    steps: list[SkillStepBody] = Field(min_length=1, max_length=32)
    source_kind: SkillSourceKind
    source_event_ids: list[str] = Field(min_length=1, max_length=100)
    domain_category: DomainCategory = "daily_life"
    sensitivity: MemorySensitivity = "personal"
    salience: float = Field(default=0.7, ge=0, le=1)

    @model_validator(mode="after")
    def enforce_payload_bound(self) -> SkillProposalBody:
        if len(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ) > 64 * 1024:
            raise ValueError("skill proposal must not exceed 64 KiB")
        return self


class SkillRunConfirmationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any]

    @model_validator(mode="after")
    def enforce_input_bound(self) -> SkillRunConfirmationBody:
        if len(
            json.dumps(
                self.inputs,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ) > 32 * 1024:
            raise ValueError("skill inputs must not exceed 32 KiB")
        return self


def _catalog(request: Request) -> SkillCatalogPort:
    return cast(SkillCatalogPort, request.app.state.skill_catalog)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _version_payload(version: SkillVersion) -> dict[str, object]:
    return {
        "skill_id": version.skill_id,
        "account_id": version.account_id,
        "name": version.name,
        "description": version.description,
        "version": version.version,
        "status": version.status,
        "trigger_phrases": list(version.trigger_phrases),
        "input_schema": dict(version.input_schema),
        "output_schema": dict(version.output_schema),
        "output_template": dict(version.output_template),
        "allowed_tools": list(version.allowed_tools),
        "steps": [
            {
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "arguments": dict(step.arguments),
                "compensation_tool_name": step.compensation_tool_name,
                "compensation_arguments": (
                    dict(step.compensation_arguments)
                    if step.compensation_arguments is not None
                    else None
                ),
            }
            for step in version.steps
        ],
        "source_kind": version.source_kind,
        "source_event_ids": list(version.source_event_ids),
        "domain_category": version.domain_category,
        "sensitivity": version.sensitivity,
        "salience": version.salience,
        "created_at": version.created_at.isoformat(),
        "approved_at": (
            version.approved_at.isoformat() if version.approved_at is not None else None
        ),
        "approval_event_id": version.approval_event_id,
    }


def _run_payload(run: SkillRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "skill_id": run.skill_id,
        "version": run.version,
        "status": run.status,
        "rollback_status": run.rollback_status,
        "confirmation_event_id": run.confirmation_event_id,
        "inputs": dict(run.inputs),
        "output": dict(run.output) if run.output is not None else None,
        "error_code": run.error_code,
        "started_at": run.started_at.isoformat(),
        "completed_at": (
            run.completed_at.isoformat() if run.completed_at is not None else None
        ),
        "steps": [
            {
                "sequence": step.sequence,
                "step_id": step.step_id,
                "phase": step.phase,
                "tool_name": step.tool_name,
                "arguments": dict(step.arguments),
                "status": step.status,
                "output": step.output,
                "error_code": step.error_code,
                "started_at": step.started_at.isoformat(),
                "completed_at": step.completed_at.isoformat(),
            }
            for step in run.steps
        ],
    }


@router.get("")
async def list_skills(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    versions = await _catalog(request).list_versions(account_id=user.user_id)
    return {"items": [_version_payload(version) for version in versions]}


@router.post("/proposals", status_code=201)
async def propose_skill(
    body: SkillProposalBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, object]:
    try:
        version = await _catalog(request).propose(
            SkillProposal(
                account_id=user.user_id,
                name=body.name,
                description=body.description,
                trigger_phrases=tuple(body.trigger_phrases),
                input_schema=body.input_schema,
                output_schema=body.output_schema,
                output_template=body.output_template,
                allowed_tools=tuple(body.allowed_tools),
                steps=tuple(
                    SkillStepDefinition(
                        step_id=step.step_id,
                        tool_name=step.tool_name,
                        arguments=step.arguments,
                        compensation_tool_name=step.compensation_tool_name,
                        compensation_arguments=step.compensation_arguments,
                    )
                    for step in body.steps
                ),
                source_kind=body.source_kind,
                source_event_ids=tuple(body.source_event_ids),
                domain_category=body.domain_category,
                sensitivity=body.sensitivity,
                salience=body.salience,
            )
        )
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (SkillApprovalRequiredError, SkillSchemaValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _version_payload(version)


@router.post("/{skill_id}/versions/{version}/approve")
async def approve_skill(
    skill_id: str,
    version: int,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, object]:
    try:
        candidate = await _catalog(request).get_version(
            account_id=user.user_id,
            skill_id=skill_id,
            version=version,
        )
    except (SkillNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if candidate.status != "candidate":
        raise HTTPException(
            status_code=409,
            detail="only candidate skill versions can be approved",
        )
    approval_event_id = str(uuid.uuid4())
    await _archive(request).record(
        EvidenceEvent(
            event_id=approval_event_id,
            account_id=user.user_id,
            event_type="skill.approved",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="user.skill_approval",
            payload={"skill_id": skill_id, "version": version},
        )
    )
    try:
        approved = await _catalog(request).approve(
            SkillApproval(
                account_id=user.user_id,
                skill_id=skill_id,
                version=version,
                approval_event_id=approval_event_id,
            )
        )
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (SkillApprovalRequiredError, SkillConfirmationRequiredError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _version_payload(approved)


@router.post("/{skill_id}/versions/{version}/confirm-run", status_code=201)
async def confirm_skill_run(
    skill_id: str,
    version: int,
    body: SkillRunConfirmationBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, object]:
    try:
        skill = await _catalog(request).get_version(
            account_id=user.user_id,
            skill_id=skill_id,
            version=version,
        )
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if skill.status != "approved":
        raise HTTPException(status_code=409, detail="skill version is not approved")
    digest = skill_input_sha256(body.inputs)
    confirmation_event_id = str(uuid.uuid4())
    await _archive(request).record(
        EvidenceEvent(
            event_id=confirmation_event_id,
            account_id=user.user_id,
            event_type="skill.run_confirmed",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="user.skill_confirmation",
            payload={
                "skill_id": skill_id,
                "version": version,
                "input_sha256": digest,
            },
        )
    )
    return {
        "confirmation_event_id": confirmation_event_id,
        "skill_id": skill_id,
        "version": version,
        "input_sha256": digest,
        "single_use": True,
    }


@router.get("/runs/{run_id}")
async def get_skill_run(
    run_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    try:
        run = await _catalog(request).get_run(
            account_id=user.user_id,
            run_id=run_id,
        )
    except SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _run_payload(run)
