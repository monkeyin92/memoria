"""Thin HTTP adapters for per-subject persona assignments on a device binding.

The write path is owner-only by design.  ``_can_switch_subject`` answers
``True`` when ``actor_id == subject_id``, which would let any binding member
pin a persona to themselves.  This stage has no multi-member speaker identity,
so an account cannot stand in for the subject it speaks as, and the product
rule is that the 主人 designates who uses which persona.  That narrowing lives
here; the shared matrix is deliberately left untouched.

Reads stay member-scoped, so a family member can see who is using which persona
without being able to change it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.multi_subject_runtime import (
    MultiSubjectRuntimeControl,
    PostgresMultiSubjectRuntimeControl,
)
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.identity.domain import (
    BindingManifest,
    IdentityAccessDeniedError,
    IdentityNotFoundError,
)
from services.identity.service import IdentityService

router = APIRouter(prefix="/v1/devices", tags=["persona-assignments"])

#: ``"{persona_id}:v{n}"``; a custom id is ``cu_`` plus hex, well inside this.
MAX_PERSONA_SELECTION = 64


def _identity(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity_service)


def _runtime(
    request: Request,
) -> MultiSubjectRuntimeControl | PostgresMultiSubjectRuntimeControl:
    return cast(
        MultiSubjectRuntimeControl | PostgresMultiSubjectRuntimeControl,
        request.app.state.multi_subject_runtime,
    )


class PersonaAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    persona_selection: str = Field(min_length=1, max_length=MAX_PERSONA_SELECTION)


def _forbidden(code: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code})


def _binding_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "binding_not_found"})


async def _member_manifest(
    request: Request,
    *,
    device_id: str,
    user_id: str,
    now: datetime,
) -> BindingManifest:
    try:
        return await _runtime(request).require_binding_member(
            device_id=device_id, user_id=user_id, now=now
        )
    except IdentityAccessDeniedError as exc:
        raise _forbidden("subject_not_binding_member") from exc
    except IdentityNotFoundError as exc:
        raise _binding_not_found() from exc


async def _admin_manifest(
    request: Request,
    *,
    device_id: str,
    user_id: str,
    now: datetime,
) -> BindingManifest:
    """Owner or device admin only -- see the module docstring for why."""
    manifest = await _member_manifest(
        request, device_id=device_id, user_id=user_id, now=now
    )
    if user_id not in {manifest.account_owner_id, *manifest.device_admin_ids}:
        raise _forbidden("subject_switch_forbidden")
    return manifest


@router.put("/{device_id}/persona-assignments/{person_id}")
async def set_persona_assignment(
    device_id: str,
    person_id: str,
    body: PersonaAssignmentRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Pin one persona to one subject.  Replaying the same one is idempotent."""
    now = datetime.now(UTC)
    manifest = await _admin_manifest(
        request, device_id=device_id, user_id=user.user_id, now=now
    )
    try:
        record = await _identity(request).set_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=person_id,
            persona_selection=body.persona_selection,
            actor_person_id=user.user_id,
            now=now,
        )
    except ValueError as exc:
        # Unknown id, another account's custom persona, or a malformed version
        # suffix.  The identity service resolves the catalogue and rejects;
        # this adapter only reports it.
        raise HTTPException(
            status_code=422,
            detail={"code": "persona_selection_invalid"},
        ) from exc
    except IdentityAccessDeniedError as exc:
        raise _forbidden("subject_switch_forbidden") from exc
    except IdentityNotFoundError as exc:
        raise _binding_not_found() from exc
    return record.to_dict()


@router.get("/{device_id}/persona-assignments")
async def list_persona_assignments(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Every subject override, plus the binding default they fall back to."""
    now = datetime.now(UTC)
    manifest = await _member_manifest(
        request, device_id=device_id, user_id=user.user_id, now=now
    )
    records = await _identity(request).list_persona_assignments(
        binding_id=manifest.binding_id, actor_person_id=user.user_id
    )
    return {
        "assignments": [record.to_dict() for record in records],
        "binding_default": manifest.persona_assignment_id,
    }


@router.delete("/{device_id}/persona-assignments/{person_id}")
async def delete_persona_assignment(
    device_id: str,
    person_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Drop the override; the subject falls back to the binding default."""
    now = datetime.now(UTC)
    manifest = await _admin_manifest(
        request, device_id=device_id, user_id=user.user_id, now=now
    )
    try:
        removed = await _identity(request).delete_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=person_id,
            actor_person_id=user.user_id,
            now=now,
        )
    except IdentityAccessDeniedError as exc:
        raise _forbidden("subject_switch_forbidden") from exc
    except IdentityNotFoundError as exc:
        raise _binding_not_found() from exc
    return {"removed": removed, "effective": manifest.persona_assignment_id}


__all__ = ["PersonaAssignmentRequest", "router"]
