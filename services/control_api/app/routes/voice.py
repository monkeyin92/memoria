"""Voice-clone consent, candidate evaluation, activation and provider sample APIs."""

from __future__ import annotations

import base64
import binascii
import hmac
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
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
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
    settings = cast(ControlSettings, request.app.state.settings)
    if settings.tts_provider == "doubao":
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
    if profile.deletion_status != "completed":
        raise HTTPException(
            status_code=503,
            detail="声音资产删除未完成，请稍后重试",
        )
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
    account_id = str(session["user_id"])
    resolution = await _manager(request).resolve(account_id=account_id)
    settings = cast(ControlSettings, request.app.state.settings)
    legacy_cosyvoice_profile = (
        settings.tts_provider == "doubao"
        and resolution.mode == "active"
        and resolution.model is not None
        and resolution.model.startswith("cosyvoice-v3.5-")
    )
    if resolution.mode == "fallback" or legacy_cosyvoice_profile:
        # The companion is frozen with the session. Profile changes only affect
        # future sessions and can never alter a running conversation's voice.
        designed_profile = designed_voice_profile(
            session.get("companion_style_id") or DEFAULT_COMPANION_ID
        )
        if designed_profile is not None:
            return {
                "mode": "designed",
                "profile_id": designed_profile,
                "model": DESIGNED_VOICE_MODEL,
                "voice_id": None,
            }
        if legacy_cosyvoice_profile:
            return {
                "mode": "fallback",
                "profile_id": None,
                "model": None,
                "voice_id": None,
            }
    return {
        "mode": resolution.mode,
        "profile_id": resolution.profile_id,
        "model": resolution.model,
        "voice_id": resolution.voice_id,
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
