"""Media-session façade for H5 and future device clients.

The legacy ``/v1/sessions`` endpoint remains the source of frozen persona and
account policy.  This route only projects a media-specific response and never
reads long-term memory or accepts a client-selected runtime.
"""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.agent.src.voice_core.device_security import SignedChallenge
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.device_registry import (
    DeviceChallengeRateLimited,
    DeviceChallengeRejected,
    DeviceNotFound,
)
from services.control_api.app.media_slo import MediaSLOUnavailable
from services.control_api.app.routes import session as session_routes
from services.control_api.app.security import (
    AuthenticatedUser,
    optional_authenticated_user,
)
from services.control_api.app.session_directory import SessionDirectoryUnavailable

router = APIRouter(prefix="/v1/media", tags=["media"])
device_router = APIRouter(prefix="/v1/devices", tags=["media"])
internal_router = APIRouter(prefix="/v1/internal/media-runtime", tags=["media-internal"])


class MediaCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_duplex: bool = True
    playback_ack: str = Field(default="approximate", pattern=r"^(none|approximate|sample)$")
    data_channel: bool = True


class DeviceProof(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    nonce: str = Field(min_length=1, max_length=256)
    issued_at_ms: int = Field(ge=0)
    signature: str = Field(min_length=1, max_length=256)


class RegisterDeviceIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    public_key: str = Field(min_length=1, max_length=256)
    firmware_channel: str = Field(default="stable", pattern=r"^(stable|canary|lab)$")


class MediaSLOReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=64)
    metrics: dict[str, float]
    ttl_s: int | None = Field(default=None, ge=30, le=900)


class CreateMediaSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    client_type: str = Field(default="h5", pattern=r"^(h5|device)$")
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    capabilities: MediaCapabilities = Field(default_factory=MediaCapabilities)
    device_proof: DeviceProof | None = None


class MediaSessionResponse(BaseModel):
    session_id: str
    media_runtime: str
    whip_url: str | None = None
    token: str | None = None
    expires_at: str | None = None
    ice_servers: list[dict[str, Any]] = Field(default_factory=list)
    stream_epoch: int = Field(ge=1)
    owner_instance_id: str | None = None
    ownership_epoch: int | None = Field(default=None, ge=1)
    fallback_runtime: str = "livekit"
    streamcore: dict[str, Any] | None = None
    fallback: dict[str, str]
    livekit: dict[str, str] | None = None
    device_id: str | None = None


async def _create(
    body: CreateMediaSessionRequest,
    request: Request,
    user: AuthenticatedUser,
    *,
    device_id: str | None = None,
) -> MediaSessionResponse:
    effective_device_id = device_id or body.device_id
    if device_id is not None and body.device_id not in {None, device_id}:
        raise HTTPException(status_code=422, detail="device_id mismatch")
    if body.client_type == "h5" and effective_device_id is not None and body.device_id not in {
        None,
        effective_device_id,
    }:
        raise HTTPException(status_code=422, detail="device_id mismatch")
    # The current production migration intentionally permits StreamCore only
    # for H5. Device sessions still receive the stable LiveKit fallback until
    # the Linux AEC/identity adapter is deployed.
    platform = "h5" if body.client_type == "h5" else "device"
    created = await session_routes.create_session(
        session_routes.CreateSessionRequest(
            client=session_routes.ClientInfo(platform=platform, device_id=effective_device_id),
        ),
        request,
        user,
    )
    if not isinstance(created, session_routes.CreateSessionResponse):
        raise HTTPException(status_code=409, detail="media session requires cascade backend")
    try:
        claimed_route = await session_routes.claim_session_route(
            request,
            session_id=created.session_id,
            account_id=user.user_id,
            device_id=effective_device_id or "h5",
            stream_epoch=created.stream_epoch,
            media_runtime=created.media_runtime,
        )
    except SessionDirectoryUnavailable as exc:
        raise HTTPException(status_code=503, detail="media session directory unavailable") from exc
    streamcore = created.streamcore or {}
    expires_at = cast(str | None, streamcore.get("expires_at"))
    return MediaSessionResponse(
        session_id=created.session_id,
        media_runtime=created.media_runtime,
        whip_url=cast(str | None, streamcore.get("whip_url")),
        token=cast(str | None, streamcore.get("token")),
        expires_at=expires_at,
        ice_servers=session_routes.turn_ice_servers(
            request.app.state.settings,
            session_id=created.session_id,
            device_id=effective_device_id,
        ),
        stream_epoch=created.stream_epoch,
        fallback_runtime=created.fallback_runtime,
        streamcore=created.streamcore,
        owner_instance_id=claimed_route.owner_instance_id if claimed_route else created.owner_instance_id,
        ownership_epoch=claimed_route.ownership_epoch if claimed_route else created.ownership_epoch,
        fallback={"media_runtime": created.fallback_runtime},
        livekit={
            "url": created.livekit_url,
            "room_name": created.room_name,
            "participant_token": created.participant_token,
        },
        device_id=effective_device_id,
    )


@router.post("/sessions", response_model=MediaSessionResponse)
async def create_media_session(
    body: CreateMediaSessionRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> MediaSessionResponse:
    return await _create(body, request, user)


@router.post("/sessions/{session_id}/stop", response_model=dict[str, Any])
async def stop_media_session(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Idempotent media stop alias for clients that do not use room paths."""

    record = cast(Any, request.app.state.memory_store).get_voice_session(
        session_id=session_id,
        user_id=user.user_id,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="session not found")
    # Reuse the existing idempotency-safe control implementation instead of
    # inventing a second cancellation path.
    return await session_routes.stop_response(
        session_id=session_id,
        body=session_routes.StopResponseBody(),
        request=request,
        user=user,
        idempotency_key=idempotency_key,
    )


@device_router.post("/{device_id}/media-session", response_model=MediaSessionResponse)
async def create_device_media_session(
    device_id: str,
    body: CreateMediaSessionRequest,
    request: Request,
    user: Annotated[AuthenticatedUser | None, Depends(optional_authenticated_user)],
) -> MediaSessionResponse:
    if body.client_type != "device":
        raise HTTPException(status_code=422, detail="device endpoint requires client_type=device")
    settings = request.app.state.settings
    account_id = user.user_id if user is not None else None
    if body.device_proof is None:
        # Bearer bootstrap remains available only to offline/dev fixtures. A
        # production device must prove possession of its registered key.
        if account_id is None or not settings.offline_mock:
            raise HTTPException(status_code=401, detail="signed device challenge is required")
    else:
        try:
            account_id = request.app.state.device_registry.authenticate(
                device_id,
                SignedChallenge(
                    device_id=device_id,
                    nonce=body.device_proof.nonce,
                    issued_at_ms=body.device_proof.issued_at_ms,
                    signature_b64=body.device_proof.signature,
                ),
            )
        except DeviceNotFound as exc:
            raise HTTPException(status_code=401, detail="device identity is not registered") from exc
        except DeviceChallengeRejected as exc:
            raise HTTPException(status_code=401, detail="device challenge was rejected") from exc
        if user is not None and user.user_id != account_id:
            raise HTTPException(status_code=403, detail="device owner does not match bearer account")
    assert account_id is not None
    if request.app.state.memory_store.is_account_unavailable(user_id=account_id):
        raise HTTPException(status_code=409, detail="device owner is unavailable")
    device_user = user or AuthenticatedUser(user_id=account_id, session_id=None, jti=None)
    async with request.app.state.account_operations.write(account_id):
        return await _create(body, request, device_user, device_id=device_id)


@device_router.post("/{device_id}/identity")
async def register_device_identity(
    device_id: str,
    body: RegisterDeviceIdentityRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        identity = request.app.state.device_registry.register(
            device_id=device_id,
            account_id=user.user_id,
            public_key_b64=body.public_key,
            firmware_channel=body.firmware_channel,
            now=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {**identity.to_dict(), "account_id": user.user_id}


@device_router.post("/{device_id}/challenge")
async def issue_device_challenge(device_id: str, request: Request) -> dict[str, Any]:
    try:
        challenge = request.app.state.device_registry.issue_challenge(device_id)
        return cast(dict[str, Any], challenge.to_dict())
    except DeviceNotFound as exc:
        raise HTTPException(status_code=404, detail="device identity not found") from exc
    except DeviceChallengeRateLimited as exc:
        raise HTTPException(status_code=429, detail="too many outstanding device challenges") from exc


@device_router.post("/{device_id}/revoke")
async def revoke_device_identity(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    revoked = request.app.state.memory_store.revoke_device_identity(
        device_id=device_id,
        account_id=user.user_id,
        now=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    if not revoked:
        raise HTTPException(status_code=404, detail="device identity not found")
    return {"device_id": device_id, "revoked": True}


@internal_router.post("/slo")
async def report_media_slo(
    body: MediaSLOReportRequest,
    request: Request,
) -> dict[str, Any]:
    expected = request.app.state.settings.media_slo_report_token.get_secret_value().strip()
    provided = request.headers.get("X-Media-SLO-Token", "").strip()
    if not expected or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="media SLO report token is invalid")
    try:
        snapshot = await request.app.state.media_slo_gate.publish(
            body.metrics,
            source=body.source,
            ttl_s=body.ttl_s,
        )
    except (MediaSLOUnavailable, ValueError) as exc:
        raise HTTPException(status_code=503, detail="media SLO report was rejected") from exc
    return {
        "source": snapshot.source,
        "observed_at": snapshot.observed_at.isoformat().replace("+00:00", "Z"),
        "expires_at": snapshot.expires_at.isoformat().replace("+00:00", "Z"),
        "rollback_required": snapshot.report.rollback_required,
        "failures": list(snapshot.report.failures),
    }


__all__ = [
    "CreateMediaSessionRequest",
    "DeviceProof",
    "MediaSessionResponse",
    "RegisterDeviceIdentityRequest",
    "MediaSLOReportRequest",
    "device_router",
    "internal_router",
    "router",
]
