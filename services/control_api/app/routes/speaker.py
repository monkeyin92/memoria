"""Biometric speaker enrollment and internal three-state classification APIs."""

from __future__ import annotations

import base64
import binascii
import hmac
import uuid
from dataclasses import asdict
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
)
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


@router.post("/enrollments", status_code=status.HTTP_201_CREATED)
async def enroll(
    body: EnrollmentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _require_registered_account(request, user)
    samples = tuple(
        EnrollmentSample(
            pcm=_decode_pcm(item.audio_base64),
            sample_rate=item.sample_rate,
            device=item.device,
            scene=item.scene,
        )
        for item in body.samples
    )
    if sum(len(sample.pcm) for sample in samples) > 16 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="speaker enrollment is too large")
    try:
        result = await _authority(request).enroll(
            EnrollmentRequest(
                account_id=user.user_id,
                consent_grant_id=(
                    f"speaker:{body.consent_policy_version}:{uuid.uuid4()}"
                ),
                samples=samples,
            )
        )
    except EnrollmentQualityError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (httpx.HTTPError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail="speaker embedding service unavailable") from exc
    return asdict(result)


@router.get("")
async def profiles(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    return {"items": [asdict(item) for item in await _authority(request).profiles(user.user_id)]}


@router.post("/{profile_id}/activate", status_code=status.HTTP_204_NO_CONTENT)
async def activate(
    profile_id: str,
    body: ActivationCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> Response:
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
    decision = await _authority(request).classify(
        SpeakerSample(
            account_id=str(session["user_id"]),
            session_id=body.session_id,
            pcm=_decode_pcm(body.audio_base64),
            sample_rate=body.sample_rate,
        )
    )
    return asdict(decision)


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
