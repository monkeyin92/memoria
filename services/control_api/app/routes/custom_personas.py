"""Custom-persona three-stage API: structure (no state) -> confirm (freeze).

This router is the ONLY persistence entry for a custom persona.  ``POST
/v1/personas/structuring`` asks the LLM to fit one free-text description into
the controlled eleven-field allowlist and returns a deterministic ``draft_id``
without writing anything; the client may re-run it any number of times.  ``POST
/v1/personas`` independently re-validates that same strict model (never
trusting the draft), so a confirmed persona is exactly one immutable ``v1``
record (design increment-02 §1.1 難点 4).

Only the account owner may create or delete (enforced again in
``IdentityService``); an out-of-domain value is rejected -- never clamped --
with ``422 {"code": "persona_structuring_invalid", "field": ...}``.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.common.companions import COMPANION_IDS, COMPANIONS, DEFAULT_COMPANION_ID
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.identity.domain import (
    CustomPersonaLimitError,
    IdentityAccessDeniedError,
    IdentityConflictError,
    StructuredPersonaFields,
)
from services.identity.service import IdentityService
from services.persona.custom_persona_fields import (
    MAX_DISPLAY_NAME,
    StructuredPersona,
    invalid_structured_field,
    persona_draft_id,
    to_structured_fields,
)
from services.persona.custom_persona_structurer import (
    MAX_FREE_TEXT,
    PersonaStructuringError,
    QwenCustomPersonaStructurer,
)

router = APIRouter(prefix="/v1/personas", tags=["personas"])


def _identity(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity_service)


def _structurer(request: Request) -> QwenCustomPersonaStructurer | None:
    return cast(
        "QwenCustomPersonaStructurer | None",
        getattr(request.app.state, "persona_structurer", None),
    )


class StructuringRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    free_text: str = Field(min_length=1, max_length=MAX_FREE_TEXT)


class CreateCustomPersonaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME)
    structured: dict[str, Any]
    fallback_designed_voice: str = Field(default=DEFAULT_COMPANION_ID, min_length=1, max_length=64)


def _validated_structured(payload: object) -> StructuredPersona:
    """Re-validate the confirmed payload against the strict controlled model."""
    try:
        return StructuredPersona.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "persona_structuring_invalid",
                "field": invalid_structured_field(exc),
            },
        ) from exc


@router.post("/structuring")
async def structure_persona(
    body: StructuringRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """AI structuring stage: pure, stateless, writes nothing."""
    del user
    structurer = _structurer(request)
    if structurer is None:
        # Offline / unconfigured: the client falls back to hand-filling the
        # same controlled fields against ``POST /v1/personas`` (PRD P1-2).
        raise HTTPException(
            status_code=503,
            detail={"code": "persona_structuring_unavailable"},
        )
    try:
        structured = await structurer.structure(body.free_text)
    except PersonaStructuringError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "persona_structuring_invalid", "field": exc.field},
        ) from exc
    return {
        "structured": structured.model_dump(),
        "draft_id": persona_draft_id(structured, body.free_text),
        "structurer_version": structurer.version,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_persona(
    body: CreateCustomPersonaRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Confirm stage: the single write entry, one immutable ``v1`` record."""
    if body.fallback_designed_voice not in COMPANION_IDS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "persona_structuring_invalid",
                "field": "fallback_designed_voice",
            },
        )
    structured = _validated_structured(body.structured)
    fields: StructuredPersonaFields = to_structured_fields(structured)
    try:
        record = await _identity(request).create_custom_persona(
            owner_person_id=user.user_id,
            display_name=body.display_name,
            structured=fields,
            fallback_designed_voice=body.fallback_designed_voice,
            idempotency_key=idempotency_key,
            actor_person_id=user.user_id,
        )
    except CustomPersonaLimitError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "custom_persona_limit_reached", "limit": exc.limit},
        ) from exc
    except IdentityAccessDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": "persona_create_forbidden"}
        ) from exc
    except IdentityConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "idempotency_conflict"}
        ) from exc
    return record.to_dict()


@router.get("")
async def list_personas(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """The account's own personas plus the read-only built-in catalogue."""
    records = await _identity(request).list_custom_personas(
        owner_person_id=user.user_id,
        actor_person_id=user.user_id,
    )
    return {
        "custom_personas": [record.to_dict() for record in records],
        "builtin": [
            {
                "persona_id": companion_id,
                "display_name": COMPANIONS[companion_id].display_name,
                "style_description": COMPANIONS[companion_id].style_description,
            }
            for companion_id in sorted(COMPANION_IDS)
        ],
    }


@router.delete("/{persona_id}")
async def delete_persona(
    persona_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    confirm: bool = False,
) -> dict[str, Any]:
    """Owner-only delete; a still-referenced persona needs ``?confirm=true``."""
    identity = _identity(request)
    in_use = await identity.count_custom_persona_references(
        persona_id, actor_person_id=user.user_id
    )
    if in_use and not confirm:
        raise HTTPException(
            status_code=409,
            detail={"code": "persona_in_use", "in_use_count": in_use},
        )
    try:
        outcome = await identity.delete_custom_persona(
            persona_id=persona_id,
            owner_person_id=user.user_id,
            actor_person_id=user.user_id,
        )
    except IdentityAccessDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": "persona_delete_forbidden"}
        ) from exc
    if not outcome.deleted:
        raise HTTPException(status_code=404, detail={"code": "persona_not_found"})
    return {"deleted": True, "drifted_subjects": outcome.drifted_subjects}


__all__ = [
    "CreateCustomPersonaRequest",
    "StructuringRequest",
    "router",
]
