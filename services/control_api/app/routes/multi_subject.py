"""Thin HTTP adapters for multi-subject binding and runtime authority."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    CapabilityValue,
    DeviceDeclaredModeValue,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.consent.binding_snapshot import BINDING_OFFER_CATALOG
from services.control_api.app.account_gate import require_capability_for_subject_category
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_binding_token import (
    DeviceBindingTokenError,
    verify_device_binding_token,
)
from services.control_api.app.device_control import (
    RuntimeProfileLedger,
    stable_profile_fingerprint,
)
from services.control_api.app.multi_subject_runtime import (
    MultiSubjectRuntimeControl,
    PolicyActorMismatchError,
    PostgresMultiSubjectRuntimeControl,
    SubjectNotBindingMemberError,
    SubjectSwitchForbiddenError,
)
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.control_api.app.subject_verification import ensure_account_person
from services.device_fleet.bootstrap_domain import (
    BindingInitialization,
    ClaimConflict,
    ClaimExpired,
    IntegrationUnavailable,
    OnboardingError,
)
from services.device_fleet.bootstrap_service import (
    BindingAuthorityResult,
    DeviceOnboardingService,
)
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

    device_claim_token: str | None = Field(default=None, min_length=16, max_length=4096)
    claim_id: str | None = Field(default=None, min_length=1, max_length=128)
    onboarding_session_id: str | None = Field(default=None, min_length=1, max_length=128)
    declared_mode: DeviceDeclaredModeValue
    account_owner_person_id: str = Field(min_length=1, max_length=128)
    primary_subject: PrimarySubjectRequest
    persona_selection: str = Field(min_length=1, max_length=64)
    service_preferences: dict[str, object] = Field(default_factory=dict)
    consent_offer_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_mode_contract(self) -> CreateDeviceBindingRequest:
        has_legacy_token = self.device_claim_token is not None
        has_claim_id = self.claim_id is not None
        has_session_id = self.onboarding_session_id is not None
        if has_claim_id != has_session_id:
            raise ValueError("claim_id and onboarding_session_id must be provided together")
        if has_legacy_token == has_claim_id:
            raise ValueError("provide either the onboarding claim pair or the offline legacy token")
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


def _onboarding(request: Request) -> DeviceOnboardingService:
    service = getattr(request.app.state, "device_onboarding_service", None)
    if not isinstance(service, DeviceOnboardingService):
        raise IntegrationUnavailable("device onboarding authority is unavailable")
    return service


def _onboarding_http_error(error: OnboardingError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code})


def _binding_initialization(
    *,
    body: CreateDeviceBindingRequest,
    owner: PersonSubject,
    subject: PersonSubject,
) -> BindingInitialization:
    return BindingInitialization.from_mapping(
        {
            "declared_mode": body.declared_mode,
            "account_owner_person_id": owner.person_id,
            "primary_subject": {
                "person_id": subject.person_id,
                "relationship": body.primary_subject.relationship,
            },
            "persona_selection": body.persona_selection,
            "service_preferences": body.service_preferences,
            "consent_offer_ids": body.consent_offer_ids,
        }
    )


def _same_binding_intent(
    *,
    body: CreateDeviceBindingRequest,
    intent: BindingInitialization,
) -> bool:
    requested_person_id = body.primary_subject.person_id
    stored_person_id = str(intent.primary_subject["person_id"])
    return (
        intent.declared_mode == body.declared_mode
        and intent.account_owner_person_id == body.account_owner_person_id
        and intent.primary_subject["relationship"] == body.primary_subject.relationship
        and (requested_person_id == "new" or requested_person_id == stored_person_id)
        and intent.persona_selection == body.persona_selection
        and dict(intent.service_preferences) == body.service_preferences
        and tuple(intent.consent_offer_ids) == body.consent_offer_ids
    )


def _same_identity_manifest(
    *,
    manifest: BindingManifest,
    device_id: str,
    body: CreateDeviceBindingRequest,
    owner: PersonSubject,
    subject: PersonSubject,
) -> bool:
    return (
        manifest.status == "active"
        and manifest.device_id == device_id
        and manifest.declared_mode == body.declared_mode
        and manifest.account_owner_id == owner.person_id
        and manifest.primary_subject_ids == (subject.person_id,)
        and manifest.service_profile_version == f"{body.declared_mode}-v1"
        and manifest.policy_bundle_version == "multi-subject-v1"
        and manifest.persona_assignment_id == f"{body.persona_selection}:v1"
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
    return await ensure_account_person(
        request.app.state.memory_store, _identity(request), user_id=user_id, now=now,
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
        return await _identity(request).get_person(
            body.primary_subject.person_id,
            actor_person_id=owner.person_id,
        )
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
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, object]:
    if body.account_owner_person_id != user.user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "account_owner_mismatch"},
        )
    now = datetime.now(UTC)
    try:
        onboarding: DeviceOnboardingService | None = None
        claim_status: str | None = None
        if body.device_claim_token is not None:
            if not request.app.state.settings.offline_mock:
                raise HTTPException(
                    status_code=410,
                    detail={"code": "legacy_device_claim_disabled"},
                )
            device_id = verify_device_binding_token(
                body.device_claim_token,
                secret=request.app.state.settings.device_binding_token_key(),
                now=now,
            )
        else:
            assert body.claim_id is not None
            assert body.onboarding_session_id is not None
            if idempotency_key is None or not 8 <= len(idempotency_key) <= 128:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "binding_idempotency_key_required"},
                )
            onboarding = _onboarding(request)
            claim = onboarding.get_claim(actor_id=user.user_id, claim_id=body.claim_id)
            if claim["onboarding_session_id"] != body.onboarding_session_id:
                raise ClaimConflict("claim does not belong to onboarding session")
            claim_status = str(claim["status"])
            if claim_status == "expired":
                raise ClaimExpired()
            if claim_status not in {"reserved", "binding_committing", "committed"}:
                raise ClaimConflict("claim is not available for binding")
            device_id = str(claim["device_id"])
        owner = await _account_person(request, user_id=user.user_id, now=now)
        intent = (
            onboarding.get_binding_intent(actor_id=user.user_id, claim_id=body.claim_id)
            if onboarding is not None and body.claim_id is not None
            else None
        )
        if intent is not None:
            if not _same_binding_intent(body=body, intent=intent.initialization):
                raise ClaimConflict("binding retry does not match the persisted intent")
            subject_id = str(intent.initialization.primary_subject["person_id"])
            subject = await _identity(request).get_person(
                subject_id,
                actor_person_id=owner.person_id,
            )
        else:
            subject = await _primary_subject(request, body=body, owner=owner, now=now)
        initialization = _binding_initialization(body=body, owner=owner, subject=subject)
        if onboarding is not None:
            assert body.claim_id is not None
            assert body.onboarding_session_id is not None
            assert idempotency_key is not None
            onboarding.binding_begin(
                actor_id=user.user_id,
                claim_id=body.claim_id,
                onboarding_session_id=body.onboarding_session_id,
                initialization=initialization,
                idempotency_key=idempotency_key,
            )
        family_space_id = (
            f"family-{uuid.uuid4()}" if body.declared_mode == "family_shared" else None
        )
        manifest = (
            await _identity(request).get_active_manifest(
                device_id,
                now=now,
                actor_person_id=owner.person_id,
            )
            if onboarding is not None
            else None
        )
        if manifest is not None:
            if not _same_identity_manifest(
                manifest=manifest,
                device_id=device_id,
                body=body,
                owner=owner,
                subject=subject,
            ):
                raise IdentityConflictError("device already has a different binding")
        else:
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
        if onboarding is not None:
            assert body.claim_id is not None
            gateway_url = request.app.state.settings.device_media_gateway_url.strip()
            onboarding.binding_commit_with_authority(
                actor_id=user.user_id,
                claim_id=body.claim_id,
                authority=BindingAuthorityResult(
                    binding_id=manifest.binding_id,
                    binding_version=manifest.binding_version,
                    persona_assignment_id=manifest.persona_assignment_id
                    or f"{body.persona_selection}:v1",
                    service_profile_version=manifest.service_profile_version,
                    policy_bundle_version=manifest.policy_bundle_version,
                    runtime_profile_version=manifest.binding_version,
                    robot_name=str(body.service_preferences.get("robot_name") or "Memoria"),
                    primary_subject_display_name=subject.display_name,
                    control_api_endpoint=request.app.state.settings.public_base_url.rstrip("/"),
                    device_media_endpoint=gateway_url or "wss://media.invalid",
                ),
            )
        request.app.state.multi_subject_binding_manifests[device_id] = manifest
        return manifest.to_dict()
    except DeviceBindingTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "device_claim_invalid"},
        ) from exc
    except OnboardingError as exc:
        raise _onboarding_http_error(exc) from exc
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
    # Resolve only the authenticated actor's category from the canonical
    # Identity authority, then apply the shared account capability table before
    # reading the device binding/runtime. The legacy MemoryStore can lag this
    # authority and therefore cannot be a second category decision point here.
    now = datetime.now(UTC)
    try:
        actor = await _account_person(request, user_id=user.user_id, now=now)
    except IdentityAccessDeniedError as exc:
        raise _binding_forbidden() from exc
    require_capability_for_subject_category(
        actor.subject_category,
        "device_runtime_profile_sync",
    )
    store_value = getattr(request.app.state, "memory_store", None)
    control = _runtime(request)
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
        payload = control.serialize_profile(profile)
    else:
        payload = control.serialize_profile(cast(RuntimeProfile, profile))
    if not isinstance(store_value, MemoryStore):
        return payload
    RuntimeProfileLedger(store_value).observe(
        device_id=device_id,
        runtime_profile_id=str(payload["runtime_profile_id"]),
        content_fingerprint=stable_profile_fingerprint(payload),
        issued_at=datetime.fromisoformat(str(payload["issued_at"]).replace("Z", "+00:00")),
        expires_at=datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00")),
        now=now,
    )
    # Never append device settings or a second version field to this payload:
    # the signature covers the exact RuntimeProfile v2 shape. Device settings
    # use /settings and /runtime-profile/changes as an explicit projection.
    return payload


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
