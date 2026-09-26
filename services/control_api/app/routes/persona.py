"""Persona learning consent, review and version APIs, and a binder's view of a bound subject's persona."""

from __future__ import annotations

from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import EvidenceNotFoundError
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.identity.service import IdentityService
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


#: One-to-one bindings where the binder manages another person's device.
_BINDER_MODES = frozenset({"parent_for_child", "child_for_parent"})


async def _require_binder_of(request: Request, *, user: AuthenticatedUser, subject_id: str) -> None:
    """Only the owner of an active binding that serves this person may look.

    The persona follows the person using the device (P1-03): a child's or an
    elder's persona is learned inside the binder's account, attributed to them.
    """

    if subject_id == user.user_id:
        raise HTTPException(status_code=404, detail={"code": "use_own_persona_endpoints"})
    identity = getattr(request.app.state, "identity_service", None)
    if not isinstance(identity, IdentityService):
        raise HTTPException(status_code=503, detail={"code": "identity_authority_unavailable"})
    manifests = await identity.list_active_manifests_for_person(
        subject_id, actor_person_id=user.user_id
    )
    if not any(
        manifest.declared_mode in _BINDER_MODES
        and manifest.account_owner_id == user.user_id
        and subject_id in manifest.primary_subject_ids
        for manifest in manifests
    ):
        raise HTTPException(status_code=403, detail={"code": "binding_owner_required"})


@router.get("/subjects/{subject_id}/style")
async def subject_persona_style(
    subject_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """The bound person's confirmed expression style, as fixed labels only.

    Parents and adult children see how the device learned to talk with the
    person -- the same non-personal style labels an unverified speaker may get
    -- never trait descriptions, values, decisions or the words they came from.
    """

    await _require_binder_of(request, user=user, subject_id=subject_id)
    capsule = await _engine(request).capsule(
        PersonaRequest(
            account_id=user.user_id,
            subject_id=subject_id,
            speaker_class="uncertain",
            confirmed_style_only=True,
        )
    )
    return {
        "subject_id": subject_id,
        "version_number": capsule.version_number,
        "style_labels": [entry.description for entry in capsule.entries],
        "descriptions_included": False,
    }


@router.post("/subjects/{subject_id}/reset")
async def reset_subject_persona(
    subject_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Forget everything the device learned about this person's persona."""

    await _require_binder_of(request, user=user, subject_id=subject_id)
    deleted = await _engine(request).forget_subject(
        account_id=user.user_id, subject_id=subject_id
    )
    return {"subject_id": subject_id, "deleted_rows": deleted}

