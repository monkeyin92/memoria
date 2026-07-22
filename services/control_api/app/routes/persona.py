"""Consent, review, version and session-scoped PersonaCapsule APIs."""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import EvidenceNotFoundError
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
)
from services.persona.domain import (
    PersonaCounterexampleRequiredError,
    PersonaEnginePort,
    PersonaRequest,
    PersonaReview,
)

router = APIRouter(prefix="/v1/persona", tags=["persona"])


def _engine(request: Request) -> PersonaEnginePort:
    return cast(PersonaEnginePort, request.app.state.persona_engine)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _require_internal_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    settings = cast(ControlSettings, request.app.state.settings)
    expected = settings.internal_token("persona_read")
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal persona token required")


def _require_registered(request: Request, user: AuthenticatedUser) -> None:
    if _store(request).get_account(user_id=user.user_id) is None:
        raise HTTPException(status_code=403, detail="register an account before persona learning")


class ConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    accepted: Literal[True]
    policy_version: str = Field(
        default="persona-learning-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )


@router.get("/status")
async def persona_status(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, bool]:
    return {"learning_allowed": await _engine(request).learning_allowed(account_id=user.user_id)}


@router.post("/consent", status_code=status.HTTP_201_CREATED)
async def grant_consent(
    body: ConsentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _require_registered(request, user)
    consent = await _engine(request).grant_consent(
        account_id=user.user_id,
        policy_version=body.policy_version,
    )
    return {
        "account_id": consent.account_id,
        "policy_version": consent.policy_version,
        "granted_at": consent.granted_at.isoformat(),
        "revoked_at": None,
    }


@router.delete("/consent")
async def revoke_consent(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        consent = await _engine(request).revoke_consent(account_id=user.user_id)
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="persona consent not found") from exc
    return {
        "account_id": consent.account_id,
        "policy_version": consent.policy_version,
        "granted_at": consent.granted_at.isoformat(),
        "revoked_at": consent.revoked_at.isoformat() if consent.revoked_at else None,
    }


@router.get("/traits")
async def list_traits(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    include_candidates: bool = False,
) -> dict[str, Any]:
    traits = await _engine(request).traits(account_id=user.user_id)
    return {
        "items": [
            {
                "trait_id": trait.trait_id,
                "category": trait.category,
                "description": trait.description,
                "context": trait.context,
                "counterexample": trait.counterexample,
                "confidence": trait.confidence,
                "status": trait.status,
                "observation_count": trait.observation_count,
                "source_event_ids": list(trait.source_event_ids),
                "updated_at": trait.updated_at.isoformat(),
                "version_id": trait.version_id,
            }
            for trait in traits
            if include_candidates or trait.status != "candidate"
        ]
    }


class TraitReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["confirm", "correct", "disable"]
    corrected_description: str | None = Field(default=None, min_length=1, max_length=2000)
    counterexample: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_correction(self) -> TraitReviewBody:
        if self.action == "correct" and self.corrected_description is None:
            raise ValueError("corrected_description is required for correction")
        if self.action != "correct" and self.corrected_description is not None:
            raise ValueError("corrected_description is only valid for correction")
        return self


@router.post("/traits/{trait_id}/review")
async def review_trait(
    trait_id: str,
    body: TraitReviewBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        trait = await _engine(request).review(
            PersonaReview(
                account_id=user.user_id,
                trait_id=trait_id,
                action=body.action,
                corrected_description=body.corrected_description,
                counterexample=body.counterexample,
            )
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="persona trait not found") from exc
    except PersonaCounterexampleRequiredError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "trait_id": trait.trait_id,
        "category": trait.category,
        "description": trait.description,
        "context": trait.context,
        "counterexample": trait.counterexample,
        "confidence": trait.confidence,
        "status": trait.status,
        "observation_count": trait.observation_count,
        "source_event_ids": list(trait.source_event_ids),
        "updated_at": trait.updated_at.isoformat(),
        "version_id": trait.version_id,
    }


@router.get("/versions")
async def list_versions(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    versions = await _engine(request).versions(account_id=user.user_id)
    return {
        "items": [
            {
                "version_id": version.version_id,
                "version_number": version.version_number,
                "status": version.status,
                "reason": version.reason,
                "trait_ids": list(version.trait_ids),
                "created_at": version.created_at.isoformat(),
            }
            for version in versions
        ]
    }


@router.post("/versions/{version_id}/rollback")
async def rollback_version(
    version_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        version = await _engine(request).rollback(
            account_id=user.user_id,
            version_id=version_id,
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="persona version not found") from exc
    return {
        "version_id": version.version_id,
        "version_number": version.version_number,
        "status": version.status,
        "reason": version.reason,
        "trait_ids": list(version.trait_ids),
        "created_at": version.created_at.isoformat(),
    }


class SessionCapsuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    speaker_class: Literal["owner", "guest", "uncertain"]
    topic: str = Field(default="", max_length=1000)
    enabled: bool = True
    max_chars: int = Field(default=1200, ge=160, le=4000)


@router.post("/session-capsule")
async def session_capsule(
    body: SessionCapsuleCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    engine = _engine(request)
    account_id = str(session["user_id"])
    confirmed_style_only = (
        body.speaker_class == "uncertain"
        and _store(request).get_account(user_id=account_id) is not None
    )
    capsule = await engine.capsule(
        PersonaRequest(
            account_id=account_id,
            speaker_class=body.speaker_class,
            topic=body.topic,
            enabled=body.enabled,
            max_chars=body.max_chars,
            confirmed_style_only=confirmed_style_only,
        )
    )
    return {
        "version_id": capsule.version_id,
        "version_number": capsule.version_number,
        "prompt_fragment": capsule.prompt_fragment,
        "delivery_rate": capsule.delivery_rate,
        "entries": [
            {
                "trait_id": entry.trait_id,
                "category": entry.category,
                "description": entry.description,
                "context": entry.context,
                "counterexample": entry.counterexample,
                "confidence": entry.confidence,
                "source_event_ids": list(entry.source_event_ids),
            }
            for entry in capsule.entries
        ],
    }
