"""Thin HTTP adapters for multi-subject binding and runtime authority."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    CapabilityValue,
    DeviceDeclaredModeValue,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.consent.binding_snapshot import BINDING_OFFER_CATALOG
from services.control_api.app.device_binding_token import (
    DeviceBindingTokenError,
    verify_device_binding_token,
)
from services.control_api.app.multi_subject_runtime import (
    MultiSubjectRuntimeControl,
    PolicyActorMismatchError,
    PostgresMultiSubjectRuntimeControl,
    SubjectNotBindingMemberError,
    SubjectSwitchForbiddenError,
)
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.identity.authority import ConsentAuthorityUnavailableError
from services.identity.domain import (
    BindingManifest,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    ModeConstraintError,
    PersonSubject,
)
from services.identity.service import IdentityService
from services.session_runtime.profile_service import RuntimeProfile, RuntimeProfileRejected
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
)

router = APIRouter(tags=["multi-subject"])

PrimaryRelationship = Literal["self", "guardian_of", "child_of", "family_member_of"]

_RELATIONSHIP_FOR_MODE: dict[DeviceDeclaredModeValue, PrimaryRelationship] = {
    "parent_for_child": "guardian_of",
    "self_use": "self",
    "child_for_parent": "child_of",
    "family_shared": "family_member_of",
}

class SubjectDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=128)
    age_band: AgeBandValue


class PrimarySubjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    person_id: str = Field(min_length=1, max_length=128)
    relationship: PrimaryRelationship
    subject_draft: SubjectDraft | None = None

    @model_validator(mode="after")
    def validate_draft(self) -> PrimarySubjectRequest:
        if self.person_id == "new" and self.subject_draft is None:
            raise ValueError("person_id=new requires subject_draft")
        if self.person_id != "new" and self.subject_draft is not None:
            raise ValueError("subject_draft is only valid for person_id=new")
        return self


class CreateDeviceBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_claim_token: str = Field(min_length=16, max_length=4096)
    declared_mode: DeviceDeclaredModeValue
    account_owner_person_id: str = Field(min_length=1, max_length=128)
    primary_subject: PrimarySubjectRequest
    persona_selection: str = Field(min_length=1, max_length=64)
    service_preferences: dict[str, object] = Field(default_factory=dict)
    consent_offer_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_mode_contract(self) -> CreateDeviceBindingRequest:
        if self.primary_subject.relationship != _RELATIONSHIP_FOR_MODE[self.declared_mode]:
            raise ValueError("primary subject relationship does not match declared_mode")
        # This is only an HTTP-shape adapter.  Identity passes the same command
        # to the server-owned authority, which validates again before writing
        # the immutable acceptance snapshot.
        BINDING_OFFER_CATALOG.validate(
            declared_mode=self.declared_mode,
            consent_offer_ids=self.consent_offer_ids,
            service_preferences=self.service_preferences,
        )
        return self


class ResolveEnvironment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    multiple_speakers: bool = False
    offline: bool = False


class ResolveSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = Field(default=None, max_length=256)
    speaker_embedding_ref: str | None = Field(default=None, max_length=256)
    client_claimed_person_id: str | None = Field(default=None, max_length=128)
    environment: ResolveEnvironment = Field(default_factory=ResolveEnvironment)


class SwitchActiveSubjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    person_id: str = Field(min_length=1, max_length=128)
    # Only app-confirmation is exposed over HTTP. voice_question confirmation
    # must be driven by a trusted voice-evidence channel, not by a client flag.
    confirmation_method: Literal["app_confirm"] = "app_confirm"


class PolicyDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    runtime_profile_id: str = Field(min_length=1, max_length=256)
    capability: CapabilityValue
    data_classification: str = Field(default="ephemeral", max_length=64)
    safety_state: str = Field(default="normal", max_length=64)


def _identity(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity_service)


def _runtime(
    request: Request,
) -> MultiSubjectRuntimeControl | PostgresMultiSubjectRuntimeControl:
    return cast(
        MultiSubjectRuntimeControl | PostgresMultiSubjectRuntimeControl,
        request.app.state.multi_subject_runtime,
    )


def _binding_forbidden() -> HTTPException:
    return HTTPException(status_code=403, detail={"code": "binding_forbidden"})


def _binding_not_found() -> HTTPException:
    return HTTPException(status_code=404, detail={"code": "binding_not_found"})


async def _account_person(
    request: Request,
    *,
    user_id: str,
    now: datetime,
) -> PersonSubject:
    identity = _identity(request)
    try:
        return await identity.get_person(user_id)
    except IdentityNotFoundError:
        profile = request.app.state.memory_store.get_subject_profile(user_id=user_id) or {}
        return await identity.register_person(
            person_id=user_id,
            actor_person_id=user_id,
            display_name=str(profile.get("display_name") or "朋友"),
            timezone="Asia/Shanghai",
            subject_category=cast(Any, profile.get("subject_category") or "unknown"),
            age_band=cast(Any, profile.get("birth_year_band") or "unknown"),
            age_evidence_status=cast(
                Any,
                profile.get("age_evidence_status") or "unverified",
            ),
            now=now,
        )


async def _primary_subject(
    request: Request,
    *,
    body: CreateDeviceBindingRequest,
    owner: PersonSubject,
    now: datetime,
) -> PersonSubject:
    if body.primary_subject.person_id != "new":
        if body.declared_mode == "self_use" and body.primary_subject.person_id != owner.person_id:
            raise IdentityAccessDeniedError("self_use subject must be the account person")
        return await _identity(request).get_person(body.primary_subject.person_id)
    draft = body.primary_subject.subject_draft
    assert draft is not None
    category: Literal["unknown", "minor"] = (
        "minor" if draft.age_band in {"under_14", "14_17"} else "unknown"
    )
    return await _identity(request).register_person(
        display_name=draft.display_name,
        timezone="Asia/Shanghai",
        subject_category=category,
        age_band=draft.age_band,
        age_evidence_status="unverified",
        actor_person_id=owner.person_id,
        now=now,
    )


def _binding_roles(
    *,
    body: CreateDeviceBindingRequest,
    owner_id: str,
) -> tuple[tuple[str, Any], ...]:
    if body.declared_mode == "parent_for_child":
        roles: list[tuple[str, Any]] = [
            (owner_id, "guardian"),
            (owner_id, "device_admin"),
        ]
        if "offer_emergency_contact_v1" in body.consent_offer_ids:
            roles.append((owner_id, "emergency_contact"))
        return tuple(roles)
    if body.declared_mode == "child_for_parent":
        return (
            (owner_id, "device_admin"),
            (owner_id, "emergency_contact"),
        )
    if body.declared_mode == "family_shared":
        return ((owner_id, "device_admin"), (owner_id, "member"))
    return ()


@router.post(
    "/v1/device-bindings",
    status_code=status.HTTP_201_CREATED,
)
async def create_device_binding(
    body: CreateDeviceBindingRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    if body.account_owner_person_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "account_owner_mismatch"},
        )
    now = datetime.now(UTC)
    try:
        device_id = verify_device_binding_token(
            body.device_claim_token,
            secret=request.app.state.settings.device_binding_token_key(),
            now=now,
        )
        owner = await _account_person(request, user_id=user.user_id, now=now)
        subject = await _primary_subject(request, body=body, owner=owner, now=now)
        family_space_id = (
            f"family-{uuid.uuid4()}" if body.declared_mode == "family_shared" else None
        )
        manifest = await _identity(request).create_binding(
            device_id=device_id,
            declared_mode=body.declared_mode,
            account_owner_person_id=owner.person_id,
            primary_subject_ids=(subject.person_id,),
            roles=_binding_roles(body=body, owner_id=owner.person_id),
            family_space_id=family_space_id,
            service_profile_version=f"{body.declared_mode}-v1",
            policy_bundle_version="multi-subject-v1",
            consent_offer_ids=body.consent_offer_ids,
            service_preferences=body.service_preferences,
            persona_assignment_id=f"{body.persona_selection}:v1",
            actor_person_id=owner.person_id,
            now=now,
        )
        request.app.state.multi_subject_binding_manifests[device_id] = manifest
        return manifest.to_dict()
    except DeviceBindingTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "device_claim_invalid"},
        ) from exc
    except IdentityAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail={"code": "binding_forbidden"}) from exc
    except ConsentAuthorityUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "consent_authority_unavailable"},
        ) from exc
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "person_not_found"}) from exc
    except (IdentityConflictError, ModeConstraintError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "binding_conflict", "message": str(exc)},
        ) from exc


@router.get("/v1/devices/{device_id}/binding")
async def get_device_binding(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    manifest: BindingManifest | None = await _identity(request).get_active_manifest(device_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail={"code": "binding_not_found"})
    if user.user_id not in {manifest.account_owner_id, *manifest.device_admin_ids}:
        raise HTTPException(status_code=403, detail={"code": "binding_forbidden"})
    return manifest.to_dict()


@router.get("/v1/devices/{device_id}/runtime-profile")
async def get_device_runtime_profile(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    session_id: str | None = None,
    offline: bool = False,
    multiple_speakers: bool = False,
) -> dict[str, object]:
    control = _runtime(request)
    now = datetime.now(UTC)
    try:
        profile = await control.ensure_profile(
            device_id=device_id,
            session_id=session_id,
            actor_id=user.user_id,
            now=now,
            multiple_speakers=multiple_speakers,
            offline=offline,
        )
    except IdentityNotFoundError as exc:
        raise _binding_not_found() from exc
    except IdentityAccessDeniedError as exc:
        raise _binding_forbidden() from exc
    except PersistentSessionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "runtime_profile_not_found"},
        ) from exc
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "runtime_profile_rejected"},
        ) from exc
    except PersistentSessionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    if isinstance(control, PostgresMultiSubjectRuntimeControl):
        return control.serialize_profile(profile)
    return control.serialize_profile(cast(RuntimeProfile, profile))


@router.post("/v1/sessions/resolve-subject")
async def resolve_session_subject(
    body: ResolveSubjectRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    control = _runtime(request)
    now = datetime.now(UTC)
    try:
        result = await control.resolve_subject(
            device_id=body.device_id,
            session_id=body.session_id,
            actor_id=user.user_id,
            now=now,
            multiple_speakers=body.environment.multiple_speakers,
            offline=body.environment.offline,
            client_claimed_person_id=body.client_claimed_person_id,
        )
    except IdentityNotFoundError as exc:
        raise _binding_not_found() from exc
    except IdentityAccessDeniedError as exc:
        raise _binding_forbidden() from exc
    except PersistentSessionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "runtime_profile_not_found"},
        ) from exc
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_resolution_forbidden"},
        ) from exc
    except PersistentSessionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    return {
        "resolution": result.resolution,
        "candidate_subjects": result.candidate_subjects,
        "temporary_service_mode": result.profile.service_mode,
        "allowed_confirmation_methods": ["voice_question", "app_confirm"],
        "runtime_profile_id": result.profile.runtime_profile_id,
    }


@router.post("/v1/sessions/{session_id}/active-subject")
async def switch_active_subject(
    session_id: str,
    body: SwitchActiveSubjectRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    control = _runtime(request)
    now = datetime.now(UTC)
    try:
        profile = await control.switch_subject(
            session_id=session_id,
            subject_id=body.person_id,
            actor_id=user.user_id,
            now=now,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "runtime_profile_not_found"},
        ) from exc
    except SubjectNotBindingMemberError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_not_binding_member"},
        ) from exc
    except SubjectSwitchForbiddenError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_switch_forbidden"},
        ) from exc
    except IdentityAccessDeniedError as exc:
        raise _binding_forbidden() from exc
    except IdentityNotFoundError as exc:
        raise _binding_not_found() from exc
    except PersistentSessionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "runtime_profile_not_found"},
        ) from exc
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "subject_switch_forbidden"},
        ) from exc
    except PersistentSessionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    if isinstance(control, PostgresMultiSubjectRuntimeControl):
        return control.serialize_profile(profile)
    return control.serialize_profile(cast(RuntimeProfile, profile))


@router.post("/v1/policy/decisions")
async def policy_decision(
    body: PolicyDecisionRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    control = _runtime(request)
    now = datetime.now(UTC)
    try:
        decision = await control.decide(
            runtime_profile_id=body.runtime_profile_id,
            capability=body.capability,
            actor_id=user.user_id,
            data_classification=body.data_classification,
            safety_state=body.safety_state,
            now=now,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "runtime_profile_not_found"},
        ) from exc
    except RuntimeProfileRejected as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "runtime_profile_rejected", "message": str(exc)},
        ) from exc
    except PolicyActorMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "policy_actor_mismatch"},
        ) from exc
    except IdentityAccessDeniedError as exc:
        raise _binding_forbidden() from exc
    except IdentityNotFoundError as exc:
        raise _binding_not_found() from exc
    except PersistentSessionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "runtime_profile_not_found"},
        ) from exc
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "policy_actor_mismatch"},
        ) from exc
    except PersistentSessionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    return control.serialize_decision(decision)
