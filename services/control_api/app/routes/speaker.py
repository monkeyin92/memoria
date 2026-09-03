"""Biometric speaker enrollment and internal three-state classification APIs."""

from __future__ import annotations

import base64
import binascii
import hmac
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.account_gate import (
    require_capability_for_account_id,
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
)
from services.control_api.app.subject_verification import speaker_enrollment_remediation
from services.speaker.domain import (
    EnrollmentQualityError,
    EnrollmentRequest,
    EnrollmentSample,
    RevokeSpeakerProfile,
    SpeakerAuthorityPort,
    SpeakerEvaluation,
    SpeakerProfileNotFoundError,
    SpeakerSample,
)

router = APIRouter(prefix="/v1/speakers", tags=["speakers"])
_MAX_PCM_BYTES = 4 * 1024 * 1024


class EnrollmentSampleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    audio_base64: str = Field(min_length=4, max_length=6 * 1024 * 1024)
    sample_rate: int = Field(ge=8000, le=48000)
    device: str = Field(default="unknown", max_length=80)
    scene: str = Field(default="unknown", max_length=80)


class EnrollmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    consent_policy_version: str = Field(
        default="speaker-biometric-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    consent_accepted: Literal[True]
    samples: list[EnrollmentSampleCreate] = Field(min_length=3, max_length=10)


class ActivationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    account_id: str = Field(min_length=1, max_length=128)
    evaluation_ref: str = Field(min_length=1, max_length=256)
    sample_count: int = Field(ge=200, le=1_000_000)
    far: float = Field(ge=0, le=1)
    frr: float = Field(ge=0, le=1)
    eer: float = Field(ge=0, le=1)
    unknown_rejection: float = Field(ge=0, le=1)
    passed: bool


class ClassificationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    audio_base64: str = Field(min_length=4, max_length=6 * 1024 * 1024)
    sample_rate: int = Field(ge=8000, le=48000)


class RevokeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=256)


class InternalEnrollmentCreate(BaseModel):
    """Enrollment submitted by the trusted Agent for one active voice session.

    The Agent may carry PCM, but it must never choose the account.  The
    control plane resolves the account from the active session record before
    applying the same subject-capability gate as the user-facing endpoint.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    intent_id: str = Field(min_length=1, max_length=128)
    samples: list[EnrollmentSampleCreate] = Field(min_length=3, max_length=10)


class EnrollmentIntentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    consent_policy_version: str = Field(
        default="speaker-biometric-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    consent_accepted: Literal[True]


def _authority(request: Request) -> SpeakerAuthorityPort:
    return cast(SpeakerAuthorityPort, request.app.state.speaker_authority)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _decode_pcm(value: str) -> bytes:
    try:
        pcm = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="audio_base64 is invalid") from exc
    if not pcm or len(pcm) > _MAX_PCM_BYTES or len(pcm) % 2:
        raise HTTPException(status_code=422, detail="PCM audio is empty, oversized or malformed")
    return pcm


def _require_internal_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Speaker-Token")] = None,
) -> None:
    settings = cast(ControlSettings, request.app.state.settings)
    expected = settings.speaker_internal_token.get_secret_value()
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal speaker token required")


def _require_registered_account(request: Request, user: AuthenticatedUser) -> None:
    if _store(request).get_account(user_id=user.user_id) is None:
        raise HTTPException(status_code=403, detail="register an account before biometric enrollment")


def _speaker_status_for_profile(
    profile: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    if profile is None:
        return False, "subject_category_unavailable"
    if profile.get("subject_category") == "minor":
        return False, "minor_forbidden"
    if (
        profile.get("subject_category") != "adult"
        or profile.get("birth_year_band") != "adult"
        or profile.get("age_evidence_status") != "verified"
    ):
        return False, "subject_capability_forbidden"
    return True, None


def _enrollment_samples(items: list[EnrollmentSampleCreate]) -> tuple[EnrollmentSample, ...]:
    samples = tuple(
        EnrollmentSample(
            pcm=_decode_pcm(item.audio_base64),
            sample_rate=item.sample_rate,
            device=item.device,
            scene=item.scene,
        )
        for item in items
    )
    if sum(len(sample.pcm) for sample in samples) > 16 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="speaker enrollment is too large")
    return samples


async def _enroll_for_account(
    request: Request,
    *,
    account_id: str,
    consent_policy_version: str = "speaker-biometric-v1",
    samples: tuple[EnrollmentSample, ...],
    intent_id: str | None = None,
) -> dict[str, Any]:
    require_capability_for_account_id(
        account_id,
        "speaker_enrollment",
        store=_store(request),
    )
    if _store(request).get_account(user_id=account_id) is None:
        raise HTTPException(status_code=403, detail="register an account before biometric enrollment")
    if intent_id is not None:
        try:
            intent = await _authority(request).consume_enrollment_intent(
                intent_id=intent_id,
                account_id=account_id,
                now=datetime.now(UTC).isoformat(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        consent_policy_version = intent.consent_policy_version
    try:
        result = await _authority(request).enroll(
            EnrollmentRequest(
                account_id=account_id,
                consent_grant_id=f"speaker:{consent_policy_version}:{uuid.uuid4()}",
                samples=samples,
            )
        )
    except EnrollmentQualityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="speaker embedding service unavailable") from exc
    return asdict(result)


async def _speaker_status_payload(request: Request, *, account_id: str) -> dict[str, Any]:
    """Build the shared redacted status for app and trusted Agent callers."""

    profile = _store(request).get_subject_profile(user_id=account_id)
    allowed, block_code = _speaker_status_for_profile(profile)
    registered = _store(request).get_account(user_id=account_id) is not None
    summaries: list[dict[str, Any]] = []
    if allowed and registered:
        summaries = [
            asdict(item) for item in await _authority(request).profiles(account_id)
        ]
    active = next((item for item in summaries if item.get("status") == "active"), None)
    shadow = next((item for item in summaries if item.get("status") == "shadow"), None)
    pending_intent = (
        await _authority(request).pending_enrollment_intent(
            account_id,
            now=datetime.now(UTC).isoformat(),
        )
        if allowed and registered and active is None and shadow is None
        else None
    )
    state = (
        "active"
        if active is not None
        else "pending"
        if shadow is not None
        else "requested"
        if pending_intent is not None
        else "required"
        if allowed and registered
        else "blocked"
    )
    subject_payload = (
        {
            "subject_category": profile.get("subject_category"),
            "birth_year_band": profile.get("birth_year_band"),
            "age_evidence_status": profile.get("age_evidence_status"),
            "subject_revision": profile.get("subject_revision"),
        }
        if profile is not None
        else None
    )
    resolved_block_code = None if allowed and registered else block_code or "account_not_registered"
    return {
        "capability": "speaker_enrollment",
        "capability_allowed": allowed and registered,
        "block_code": resolved_block_code,
        "subject": subject_payload,
        "remediation": speaker_enrollment_remediation(
            block_code=resolved_block_code,
            registered=registered,
            has_wechat_phone=_store(request).has_external_identity(
                user_id=account_id,
                provider="wechat_phone",
            ),
            subject=subject_payload,
        ),
        "enrollment": {
            "state": state,
            "profile_count": len(summaries),
            "active_profile_id": active.get("profile_id") if active else None,
            "intent_id": pending_intent.intent_id if pending_intent else None,
            "intent_expires_at": pending_intent.expires_at if pending_intent else None,
            "profiles": summaries,
        },
        "capture_location": "device",
        "phone_realtime_capture_allowed": False,
    }


@router.post("/enrollment-intents", status_code=status.HTTP_201_CREATED)
async def create_enrollment_intent(
    body: EnrollmentIntentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Record explicit consent before the device captures any audio."""

    require_capability_for_subject(user, "speaker_enrollment", store=_store(request))
    _require_registered_account(request, user)
    now = datetime.now(UTC)
    intent = await _authority(request).create_enrollment_intent(
        account_id=user.user_id,
        consent_policy_version=body.consent_policy_version,
        now=now.isoformat(),
        expires_at=(now + timedelta(hours=24)).isoformat(),
    )
    return {
        "intent_id": intent.intent_id,
        "state": intent.state,
        "consent_policy_version": intent.consent_policy_version,
        "created_at": intent.created_at,
        "expires_at": intent.expires_at,
        "capture_location": "device",
    }


@router.get("/status")
async def enrollment_status(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Return the redacted owner-enrollment state for the Mini Program.

    This endpoint intentionally reports blockers instead of turning the
    Mini Program into a biometric capture client.  Raw PCM and profile
    templates never cross this user-facing status boundary.
    """

    return await _speaker_status_payload(request, account_id=user.user_id)


@router.get("/status/internal", include_in_schema=False)
async def internal_enrollment_status(
    request: Request,
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    _: Annotated[None, Depends(_require_internal_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, session_id)
    return await _speaker_status_payload(request, account_id=str(session["user_id"]))


@router.post("/enrollments", status_code=status.HTTP_201_CREATED)
async def enroll(
    body: EnrollmentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    return await _enroll_for_account(
        request,
        account_id=user.user_id,
        consent_policy_version=body.consent_policy_version,
        samples=_enrollment_samples(body.samples),
    )


@router.post("/enrollments/internal", status_code=status.HTTP_201_CREATED, include_in_schema=False)
async def enroll_internal(
    body: InternalEnrollmentCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    return await _enroll_for_account(
        request,
        account_id=str(session["user_id"]),
        samples=_enrollment_samples(body.samples),
        intent_id=body.intent_id,
    )


@router.get("")
async def profiles(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "speaker_enrollment", store=_store(request))
    return {"items": [asdict(item) for item in await _authority(request).profiles(user.user_id)]}


@router.post("/{profile_id}/activate", status_code=status.HTTP_204_NO_CONTENT)
async def activate(
    profile_id: str,
    body: ActivationCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> Response:
    require_capability_for_account_id(
        body.account_id,
        "speaker_enrollment",
        store=_store(request),
    )
    try:
        await _authority(request).activate(
            profile_id,
            account_id=body.account_id,
            evaluation=SpeakerEvaluation(
                report_ref=body.evaluation_ref,
                sample_count=body.sample_count,
                far=body.far,
                frr=body.frr,
                eer=body.eer,
                unknown_rejection=body.unknown_rejection,
                passed=body.passed,
            ),
        )
    except SpeakerProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="speaker profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/classify")
async def classify(
    body: ClassificationCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    account_id = str(session["user_id"])
    require_capability_for_account_id(
        account_id,
        "speaker_enrollment",
        store=_store(request),
    )
    decision = await _authority(request).classify(
        SpeakerSample(
            account_id=account_id,
            session_id=body.session_id,
            pcm=_decode_pcm(body.audio_base64),
            sample_rate=body.sample_rate,
        )
    )
    profile = _store(request).get_profile(
        user_id=account_id,
        now=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    return {
        **asdict(decision),
        "reject_non_owner_voice": bool(profile["reject_non_owner_voice"]),
    }


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke(
    profile_id: str,
    body: RevokeCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> Response:
    try:
        await _authority(request).revoke(
            RevokeSpeakerProfile(
                account_id=user.user_id,
                profile_id=profile_id,
                reason=body.reason,
            )
        )
    except SpeakerProfileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="speaker profile not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
