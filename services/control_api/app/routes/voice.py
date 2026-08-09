"""Voice-clone consent, candidate evaluation, activation and provider sample APIs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from services.archive.domain import EvidenceNotFoundError
from services.common.companions import (
    DEFAULT_COMPANION_ID,
    DESIGNED_VOICE_MODEL,
    designed_voice_profile,
)
from services.control_api.app.account_gate import (
    require_capability_for_account_id,
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.mode_policy import FrozenMode
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
)
from services.digital_self.domain import RegistryPort, VersionNotFoundError
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessPurpose,
    LegacyAccessSnapshot,
    LegacyNotFoundError,
    LegacyRegistryPort,
)
from services.voice_profile.domain import (
    EvaluationRequiredError,
    VoiceConsentRequiredError,
    VoiceEnrollmentReconciliationRequiredError,
    VoiceEnrollmentRequest,
    VoiceEvaluationRequest,
    VoicePreviewRenderer,
    VoicePreviewUnavailableError,
    VoiceProfile,
    VoiceProfilePort,
    VoiceQualityMeasurementRequest,
)
from services.voice_profile.sample_url import VoiceSampleURLSigner

router = APIRouter(prefix="/v1/voices", tags=["voices"])


def _manager(request: Request) -> VoiceProfilePort:
    return cast(VoiceProfilePort, request.app.state.voice_profile_manager)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _signer(request: Request) -> VoiceSampleURLSigner:
    return cast(VoiceSampleURLSigner, request.app.state.voice_sample_signer)


def _preview_renderer(request: Request) -> VoicePreviewRenderer:
    return cast(VoicePreviewRenderer, request.app.state.voice_preview_renderer)


def _legacy_registry(request: Request) -> LegacyRegistryPort:
    return cast(LegacyRegistryPort, request.app.state.legacy_registry)


def _digital_self_registry(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


def _legacy_access_matches_frozen(
    access: LegacyAccessSnapshot,
    frozen: FrozenMode,
) -> bool:
    return (
        frozen.actor_account_id
        == (
            access.resource_owner_account_id
            if access.actor_role == "owner_preview"
            else access.grantee_account_id
        )
        and frozen.resource_owner_account_id == access.resource_owner_account_id
        and frozen.legacy_actor_role == access.actor_role
        and frozen.legacy_grantee_account_id == access.grantee_account_id
        and frozen.legacy_grant_id == access.grant_id
        and frozen.legacy_shell_id == access.shell_id
        and frozen.digital_self_version_id == access.version_id
        and frozen.manifest_sha256 == access.manifest_sha256
        and frozen.legacy_grant_snapshot_sha256 == access.grant_snapshot_sha256
        and frozen.legacy_scope_sha256 == access.scope_sha256
        and frozen.relationship_profile_id == access.relationship_profile_id
        and frozen.relationship_profile_version == access.relationship_profile_version
        and frozen.legacy_voice_allowed is access.voice_allowed
        and frozen.legacy_expires_at == access.expires_at.isoformat()
    )


def _require_registered(request: Request, user: AuthenticatedUser) -> None:
    if _store(request).get_account(user_id=user.user_id) is None:
        raise HTTPException(status_code=403, detail="register an account before voice cloning")


def _require_internal_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).internal_token("voice_resolution")
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal voice token required")


def _require_cleanup_token(
    request: Request,
    token: Annotated[
        str | None,
        Header(alias="X-Memoria-Voice-Cleanup-Token"),
    ] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).internal_token("voice_cleanup")
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=401,
            detail="valid internal voice cleanup token required",
        )


def _profile_payload(profile: VoiceProfile) -> dict[str, Any]:
    return {
        "profile_id": profile.profile_id,
        "sample_id": profile.sample_id,
        "version_number": profile.version_number,
        "provider": profile.provider,
        "provider_region": profile.provider_region,
        "target_model": profile.target_model,
        "status": profile.status,
        "evaluation_status": profile.evaluation_status,
        "quality_status": profile.quality_status,
        "deletion_status": profile.deletion_status,
        "provider_expires_at": (
            profile.provider_expires_at.isoformat() if profile.provider_expires_at else None
        ),
        "created_at": profile.created_at.isoformat(),
        "activated_at": profile.activated_at.isoformat() if profile.activated_at else None,
        "revoked_at": profile.revoked_at.isoformat() if profile.revoked_at else None,
    }


class ConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    accepted: Literal[True]
    policy_version: str = Field(
        default="voice-clone-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )


@router.post("/consent", status_code=status.HTTP_201_CREATED)
async def grant_consent(
    body: ConsentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "voice_clone", store=_store(request))
    _require_registered(request, user)
    consent = await _manager(request).grant_consent(
        account_id=user.user_id,
        policy_version=body.policy_version,
    )
    return {
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
        consent = await _manager(request).revoke_consent(account_id=user.user_id)
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=404, detail="voice-clone consent not found") from exc
    except VoiceEnrollmentReconciliationRequiredError as exc:
        raise HTTPException(
            status_code=503,
            detail="声音资产删除未完成，请稍后重试",
        ) from exc
    return {
        "policy_version": consent.policy_version,
        "granted_at": consent.granted_at.isoformat(),
        "revoked_at": consent.revoked_at.isoformat() if consent.revoked_at else None,
    }


class EnrollmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    audio_base64: str = Field(min_length=1, max_length=21_000_000)
    media_type: Literal[
        "audio/wav",
        "audio/x-wav",
        "audio/mpeg",
        "audio/mp4",
        "audio/aac",
        "audio/ogg",
        "audio/flac",
    ]
    duration_ms: int = Field(ge=10_000, le=60_000)
    sample_rate: int = Field(ge=16_000, le=192_000)
    enrollment_key: str | None = Field(default=None, min_length=1, max_length=128)


@router.post("/enrollments", status_code=status.HTTP_201_CREATED)
async def enroll_voice(
    body: EnrollmentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "voice_clone", store=_store(request))
    _require_registered(request, user)
    try:
        audio = base64.b64decode(body.audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="invalid base64 voice sample") from exc
    try:
        profile = await _manager(request).enroll(
            VoiceEnrollmentRequest(
                account_id=user.user_id,
                audio=audio,
                media_type=body.media_type,
                duration_ms=body.duration_ms,
                sample_rate=body.sample_rate,
                enrollment_key=body.enrollment_key,
            )
        )
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=403, detail="voice-clone consent is required") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VoiceEnrollmentReconciliationRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="voice provider enrollment failed") from exc
    return _profile_payload(profile)


@router.get("/profiles")
async def list_profiles(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "voice_clone", store=_store(request))
    manager = _manager(request)
    consent = await manager.consent(account_id=user.user_id)
    profiles = await manager.profiles(account_id=user.user_id)
    return {
        "consent": (
            {
                "policy_version": consent.policy_version,
                "granted_at": consent.granted_at.isoformat(),
                "revoked_at": consent.revoked_at.isoformat() if consent.revoked_at else None,
            }
            if consent is not None
            else None
        ),
        "items": [_profile_payload(profile) for profile in profiles],
    }


class EvaluationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    trial_id: str = Field(min_length=1, max_length=128)
    preferred_slot: Literal["A", "B"]
    similarity: float = Field(ge=1, le=5)
    naturalness: float = Field(ge=1, le=5)
    accent_similarity: float = Field(ge=1, le=5)
    emotion_adherence: float = Field(ge=1, le=5)
    instruction_adherence: float = Field(ge=1, le=5)
    uncanny: float = Field(ge=1, le=5)
    notes: str = Field(default="", max_length=2000)


@router.post(
    "/profiles/{profile_id}/blind-trials",
    status_code=status.HTTP_201_CREATED,
)
async def create_blind_trial(
    profile_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "voice_clone", store=_store(request))
    try:
        trial = await _manager(request).create_blind_trial(
            account_id=user.user_id,
            profile_id=profile_id,
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=403, detail="voice-clone consent is required") from exc
    except EvaluationRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "trial_id": trial.trial_id,
        "profile_id": trial.profile_id,
        "slots": list(trial.slots),
        "created_at": trial.created_at.isoformat(),
    }


class BlindPreviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    slot: Literal["A", "B"]
    text: str = Field(default="今天也想听你讲讲。", min_length=1, max_length=120)


@router.post("/blind-trials/{trial_id}/preview", response_class=Response)
async def preview_blind_trial(
    trial_id: str,
    body: BlindPreviewCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> Response:
    require_capability_for_subject(user, "voice_clone", store=_store(request))
    try:
        target = await _manager(request).blind_preview_target(
            account_id=user.user_id,
            trial_id=trial_id,
            slot=body.slot,
            text=body.text,
        )
        audio = await _preview_renderer(request).render(
            text=body.text,
            model=target.model,
            voice_id=target.voice_id,
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="blind voice trial not found") from exc
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=403, detail="voice-clone consent is required") from exc
    except EvaluationRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except VoicePreviewUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )


@router.post("/profiles/{profile_id}/evaluations")
async def evaluate_profile(
    profile_id: str,
    body: EvaluationCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "voice_clone", store=_store(request))
    try:
        manager = _manager(request)
        candidate_preferred = await manager.resolve_blind_preference(
            account_id=user.user_id,
            profile_id=profile_id,
            trial_id=body.trial_id,
            preferred_slot=body.preferred_slot,
        )
        evaluation = await manager.evaluate(
            VoiceEvaluationRequest(
                account_id=user.user_id,
                profile_id=profile_id,
                similarity=body.similarity,
                naturalness=body.naturalness,
                accent_similarity=body.accent_similarity,
                emotion_adherence=body.emotion_adherence,
                instruction_adherence=body.instruction_adherence,
                uncanny=body.uncanny,
                candidate_preferred=candidate_preferred,
                notes=body.notes,
            )
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=403, detail="voice-clone consent is required") from exc
    except EvaluationRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "evaluation_id": evaluation.evaluation_id,
        "profile_id": evaluation.profile_id,
        "status": evaluation.status,
        "created_at": evaluation.created_at.isoformat(),
    }


class QualityMeasurementCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_run_id: str = Field(min_length=1, max_length=128)
    first_audio_ms: int = Field(ge=0, le=60_000)
    cancel_tail_ms: int = Field(ge=0, le=60_000)
    timestamp_error_ms: int = Field(ge=0, le=60_000)
    long_sentence_chars: int = Field(ge=1, le=10_000)
    long_sentence_completion_ratio: float = Field(ge=0, le=1)


@router.post("/profiles/{profile_id}/quality-measurements")
async def record_quality_measurement(
    profile_id: str,
    body: QualityMeasurementCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> dict[str, Any]:
    manager = _manager(request)
    try:
        account_id = await manager.account_id_for_profile(profile_id=profile_id)
        require_capability_for_account_id(account_id, "voice_clone", store=_store(request))
        measurement = await manager.record_quality_measurement(
            VoiceQualityMeasurementRequest(
                account_id=account_id,
                profile_id=profile_id,
                **body.model_dump(),
            )
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=403, detail="voice-clone consent is required") from exc
    except EvaluationRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "measurement_id": measurement.measurement_id,
        "profile_id": measurement.profile_id,
        "source_run_id": measurement.source_run_id,
        "status": measurement.status,
        "created_at": measurement.created_at.isoformat(),
    }


@router.post("/profiles/{profile_id}/activate")
async def activate_profile(
    profile_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "voice_clone", store=_store(request))
    settings = cast(ControlSettings, request.app.state.settings)
    if settings.tts_provider == "doubao":
        profiles = await _manager(request).profiles(account_id=user.user_id)
        legacy = next((profile for profile in profiles if profile.profile_id == profile_id), None)
        if legacy is not None and (
            legacy.provider != "volcengine_doubao" or legacy.target_model != "seed-icl-2.0"
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="当前豆包语音链路不支持激活历史 CosyVoice 克隆音色",
            )
    try:
        profile = await _manager(request).activate(
            account_id=user.user_id,
            profile_id=profile_id,
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=403, detail="voice-clone consent is required") from exc
    except EvaluationRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _profile_payload(profile)


@router.delete("/profiles/{profile_id}")
async def revoke_profile(
    profile_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        profile = await _manager(request).revoke_profile(
            account_id=user.user_id,
            profile_id=profile_id,
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="声音资产删除未完成，请稍后重试",
        ) from exc
    if profile.deletion_status == "failed":
        raise HTTPException(
            status_code=503,
            detail="声音资产删除未完成，请稍后重试",
        )
    return _profile_payload(profile)


class ProviderDeletionConfirmationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_id: str = Field(min_length=1, max_length=128)
    evidence_reference: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )


@router.post("/profiles/{profile_id}/provider-deletion-confirmations")
async def confirm_provider_deletion(
    profile_id: str,
    body: ProviderDeletionConfirmationCreate,
    request: Request,
    _: Annotated[None, Depends(_require_cleanup_token)],
) -> dict[str, Any]:
    """Converge a manual provider cleanup after operator-side verification."""

    try:
        profile = await _manager(request).confirm_provider_deletion(
            account_id=body.account_id,
            profile_id=profile_id,
            evidence_reference=body.evidence_reference,
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="voice profile not found") from exc
    except EvaluationRequiredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _profile_payload(profile)


class SessionResolutionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)


@router.post("/session-resolution")
async def session_resolution(
    body: SessionResolutionCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    frozen = FrozenMode.from_session(session)
    account_id = str(session["user_id"])
    if frozen.interaction_mode == "companion":
        designed_profile = designed_voice_profile(frozen.companion_style_id or DEFAULT_COMPANION_ID)
        if designed_profile is not None:
            return {
                "mode": "designed",
                "profile_id": designed_profile,
                "provider": "volcengine_doubao",
                "voice_kind": "designed",
                "model": DESIGNED_VOICE_MODEL,
                "resource_id": DESIGNED_VOICE_MODEL,
                "voice_id": None,
                "speaker_sha256": None,
            }
    if frozen.interaction_mode in {"self_preview", "legacy"}:
        if frozen.interaction_mode == "legacy":
            if frozen.legacy_actor_role not in {"owner_preview", "grantee"} or not (
                frozen.legacy_grant_id and frozen.digital_self_version_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "legacy_voice_unavailable"},
                )
            purpose: LegacyAccessPurpose = (
                "owner_preview"
                if frozen.legacy_actor_role == "owner_preview"
                else "grantee_session"
            )
            try:
                access = await _legacy_registry(request).resolve_access(
                    actor_account_id=account_id,
                    grant_id=frozen.legacy_grant_id,
                    purpose=purpose,
                    now=datetime.now(UTC),
                )
                version = await _digital_self_registry(request).get(
                    account_id=access.resource_owner_account_id,
                    version_id=access.version_id,
                )
            except (LegacyAccessDeniedError, LegacyNotFoundError, VersionNotFoundError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "legacy_voice_unavailable"},
                ) from exc
            if (
                not _legacy_access_matches_frozen(access, frozen)
                or version.status != "frozen"
                or version.account_id != access.resource_owner_account_id
                or version.version_number != access.version_number
                or version.manifest_sha256 != access.manifest_sha256
                or _store(request).is_account_unavailable(user_id=access.resource_owner_account_id)
                or _store(request).is_account_unavailable(user_id=access.grantee_account_id)
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "legacy_voice_unavailable"},
                )
            manifest_voice = version.manifest.source_summary.voice_profile
            personal_allowed = access.voice_allowed and (
                manifest_voice is not None
                and frozen.voice_profile_id == manifest_voice.profile_id
                and frozen.voice_profile_version == manifest_voice.version_number
                and frozen.voice_provider == manifest_voice.provider
                and frozen.voice_model == manifest_voice.target_model
                and frozen.voice_resource_id == manifest_voice.resource_id
                and frozen.voice_provider_expires_at == manifest_voice.provider_expires_at
                and frozen.voice_speaker_sha256 == manifest_voice.speaker_sha256
            )
            resolution_account_id = access.resource_owner_account_id
            require_capability_for_account_id(
                access.grantee_account_id,
                "legacy_receive",
                store=_store(request),
            )
        else:
            personal_allowed = True
            resolution_account_id = account_id
        require_capability_for_account_id(
            resolution_account_id,
            "voice_clone",
            store=_store(request),
        )
        resolution = (
            await _manager(request).resolve(account_id=resolution_account_id)
            if personal_allowed
            else None
        )
        speaker_sha256 = (
            hashlib.sha256(resolution.voice_id.encode("utf-8")).hexdigest()
            if resolution is not None and resolution.voice_id is not None
            else None
        )
        if (
            resolution is not None
            and resolution.mode == "active"
            and resolution.voice_kind == "personal"
            and resolution.profile_id == frozen.voice_profile_id
            and resolution.version_number == frozen.voice_profile_version
            and resolution.provider == frozen.voice_provider
            and resolution.model == frozen.voice_model
            and resolution.resource_id == frozen.voice_resource_id
            and (
                resolution.provider_expires_at.isoformat()
                if resolution.provider_expires_at is not None
                else None
            )
            == frozen.voice_provider_expires_at
            and speaker_sha256 == frozen.voice_speaker_sha256
        ):
            return {
                "mode": resolution.mode,
                "profile_id": resolution.profile_id,
                "provider": resolution.provider,
                "voice_kind": resolution.voice_kind,
                "model": resolution.model,
                "resource_id": resolution.resource_id,
                "voice_id": resolution.voice_id,
                "speaker_sha256": speaker_sha256,
            }
        if (
            frozen.fallback_voice_profile_id is not None
            and frozen.fallback_voice_provider == "volcengine_doubao"
            and frozen.fallback_voice_model == DESIGNED_VOICE_MODEL
            and frozen.fallback_voice_resource_id == DESIGNED_VOICE_MODEL
        ):
            return {
                "mode": "designed",
                "profile_id": frozen.fallback_voice_profile_id,
                "provider": frozen.fallback_voice_provider,
                "voice_kind": "designed",
                "model": frozen.fallback_voice_model,
                "resource_id": frozen.fallback_voice_resource_id,
                "voice_id": None,
                "speaker_sha256": None,
            }
    return {
        "mode": "fallback",
        "profile_id": None,
        "provider": None,
        "voice_kind": None,
        "model": None,
        "resource_id": None,
        "voice_id": None,
        "speaker_sha256": None,
    }


@router.get("/provider-samples/{sample_id}", include_in_schema=False)
async def provider_sample(
    sample_id: str,
    request: Request,
    token: Annotated[str, Query(min_length=10, max_length=512)],
) -> Response:
    if not _signer(request).verify(sample_id=sample_id, token=token):
        raise HTTPException(status_code=404, detail="voice sample not found")
    try:
        sample = await _manager(request).provider_sample(sample_id=sample_id)
        require_capability_for_account_id(
            sample.account_id,
            "voice_clone",
            store=_store(request),
        )
    except VoiceConsentRequiredError as exc:
        raise HTTPException(status_code=404, detail="voice sample not found") from exc
    return Response(
        content=sample.data,
        media_type=sample.media_type,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
