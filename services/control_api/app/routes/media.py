"""Device media sessions on the direct Media Edge, plus media internals.

A device proves possession of its certificate key, Control establishes the
persistent Session authority, and the device receives a one-use EdDSA ticket
for the Go Media Edge WSS (``direct_voice_core``).  That is the only device
media runtime; no client can select one.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from packages.contracts.generated.python.multi_subject_contracts import CapabilityValue
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.control_api.app.account_gate import (
    require_capability_for_account_id,
    require_writable_account,
)
from services.control_api.app.companion_delivery import (
    freeze_companion_delivery,
    voice_session_delivery_fields,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_control import (
    AcousticCapabilityAuthority,
    DeviceSettingsAuthority,
    RuntimeProfileLedger,
    allowed_audio_modes,
    stable_profile_fingerprint,
)
from services.control_api.app.device_registry import (
    DeviceChallengeRateLimited,
    DeviceNotFound,
)
from services.control_api.app.media_runtime import (
    DEVICE_STREAM_EPOCH_MAX,
    mint_device_direct_media_ticket,
)
from services.control_api.app.media_slo import MediaSLOUnavailable
from services.control_api.app.routes import session as session_routes
from services.control_api.app.security import (
    AuthenticatedUser,
    create_session_id,
)
from services.control_api.app.session_companion import session_companion
from services.control_api.app.session_directory import (
    SessionDirectory,
    SessionDirectoryUnavailable,
    SessionDraining,
    SessionEpochConflict,
    SessionNotFound,
    SessionOwnershipConflict,
    SessionRoute,
)
from services.device_fleet.bootstrap_domain import (
    OnboardingError,
    b64url_decode,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.guardian.domain import GuardianStorePort
from services.session_runtime.postgres_store import SessionRuntimeConflict
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    PostgresSessionRuntimeService,
    StartPersistentSessionCommand,
)

router = APIRouter(prefix="/v1/media", tags=["media"])
device_router = APIRouter(prefix="/v1/devices", tags=["media"])
internal_router = APIRouter(prefix="/v1/internal/media-runtime", tags=["media-internal"])
logger = logging.getLogger(__name__)


class RegisterDeviceIdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    public_key: str = Field(min_length=1, max_length=256)
    firmware_channel: str = Field(default="stable", pattern=r"^(stable|canary|lab)$")


class MediaSLOReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1, max_length=64)
    metrics: dict[str, float]
    ttl_s: int | None = Field(default=None, ge=30, le=900)


class MediaReplyDeliveryEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["reply-delivery-v1"]
    event_id: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    delivery_id: str = Field(min_length=1, max_length=512)
    session_id: str = Field(min_length=1, max_length=128)
    session_epoch: int = Field(ge=0)
    turn_id: int = Field(ge=0)
    generation_id: int = Field(ge=0)
    tool_epoch: int = Field(ge=0)
    event_type: Literal[
        "first_frame_sent",
        "provider_completed",
        "actual_heard",
        "playback_ended",
        "preempted",
        "transport_rejected",
        "error",
        "skipped",
        "no_audio",
    ]
    terminal_event: Literal[
        "playback_ended",
        "preempted",
        "transport_rejected",
        "error",
        "skipped",
        "no_audio",
    ] | None = None
    terminal_reason: str | None = Field(default=None, min_length=1, max_length=64)
    first_frame_sent: bool
    provider_completed: bool
    actual_heard: bool
    playback_ended: bool
    reason: str | None = Field(default=None, min_length=1, max_length=64)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _occurred_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)


def _legacy_device_protocol_versions() -> list[Literal[1, 2]]:
    return [1]


class DeviceMediaSessionProof(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    certificate_id: str = Field(min_length=1, max_length=128)
    challenge_id: str = Field(min_length=1, max_length=128)
    nonce: str = Field(min_length=43, max_length=43)
    signature: str = Field(min_length=86, max_length=86)
    client_id: str = Field(min_length=1, max_length=128)
    # Omitted by every legacy firmware build.  Absence means v1-only, which
    # the v2-only direct Media Edge refuses (409) rather than guessing.
    supported_protocol_versions: list[Literal[1, 2]] = Field(
        default_factory=_legacy_device_protocol_versions,
        min_length=1,
        max_length=2,
    )
    # A reconnect is explicit and possession-bound: the same freshly signed
    # media challenge is still required, and Control validates this id against
    # the active Session/Binding/Profile before issuing a higher stream epoch.
    resume_session_id: str | None = Field(default=None, min_length=1, max_length=128)

    def supports(self, version: int) -> bool:
        return version in self.supported_protocol_versions

    @model_validator(mode="after")
    def validate_protocol_negotiation(self) -> Self:
        if len(set(self.supported_protocol_versions)) != len(
            self.supported_protocol_versions
        ):
            raise ValueError("supported_protocol_versions must be unique")
        if self.resume_session_id is not None and not self.supports(2):
            raise ValueError("resume_session_id requires protocol version 2")
        return self


class DeviceOpusFormat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codec: Literal["opus"] = "opus"
    sample_rate: Literal[16000, 24000]
    channels: Literal[1] = 1
    frame_ms: Literal[20] = 20


class DeviceGatewaySessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    stream_epoch: int = Field(ge=1, le=DEVICE_STREAM_EPOCH_MAX)
    websocket_url: str
    media_token: str
    expires_in: int = Field(ge=30, le=300)
    protocol_version: Literal[2] = 2
    runtime: Literal["direct_voice_core"] = "direct_voice_core"
    interaction_authority: Literal["python_authoritative"] = "python_authoritative"
    binding_id: str
    binding_version: int = Field(ge=1)
    subject_id: str | None
    runtime_profile_version: int = Field(ge=1, le=DEVICE_STREAM_EPOCH_MAX)
    uplink: DeviceOpusFormat
    downlink: DeviceOpusFormat


def _device_onboarding_service(request: Request) -> DeviceOnboardingService:
    service = getattr(request.app.state, "device_onboarding_service", None)
    if not isinstance(service, DeviceOnboardingService):
        raise HTTPException(
            status_code=503,
            detail={"code": "device_onboarding_unavailable"},
        )
    return service


def _device_onboarding_error(error: OnboardingError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code})


@router.post("/sessions/{session_id}/stop", response_model=dict[str, Any])
async def stop_media_session(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Idempotent alias of ``POST /v1/sessions/{id}/stop-response``."""

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


@device_router.post("/{device_id}/media-challenge")
async def issue_fleet_device_media_challenge(
    device_id: str,
    request: Request,
    certificate_id: Annotated[
        str | None,
        Header(alias="X-Device-Certificate-ID"),
    ] = None,
    client_id: Annotated[str | None, Header(alias="X-Client-ID")] = None,
) -> dict[str, object]:
    if not certificate_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "device_certificate_required"},
        )
    if not client_id:
        raise HTTPException(
            status_code=401,
            detail={"code": "device_client_id_required"},
        )
    try:
        return _device_onboarding_service(request).issue_media_challenge(
            device_id=device_id,
            certificate_id=certificate_id,
            client_id=client_id,
        )
    except OnboardingError as error:
        raise _device_onboarding_error(error) from error


@device_router.post(
    "/{device_id}/media-sessions",
    response_model=DeviceGatewaySessionResponse,
)
async def create_fleet_device_media_session(
    device_id: str,
    body: DeviceMediaSessionProof,
    request: Request,
) -> DeviceGatewaySessionResponse:
    settings = request.app.state.settings
    try:
        authenticated = _device_onboarding_service(request).authenticate_media_challenge(
            device_id=device_id,
            certificate_id=body.certificate_id,
            client_id=body.client_id,
            challenge_id=body.challenge_id,
            nonce=body.nonce,
            signature=b64url_decode(
                body.signature,
                field="device_media_signature",
                exact_length=64,
            ),
        )
    except OnboardingError as error:
        raise _device_onboarding_error(error) from error
    account_id = str(authenticated["actor_id"])
    binding_id = str(authenticated["binding_id"])
    binding_version = int(cast(int, authenticated["binding_version"]))
    binding_subject_id = str(authenticated["subject_id"])
    activation_profile_version = int(cast(int, authenticated["runtime_profile_version"]))
    firmware_version = str(authenticated["firmware_version"])
    board_profile = str(authenticated["board_profile"])
    # Checked only after the signed challenge authenticated this device: the
    # direct Media Edge speaks protocol v2 only, and firmware that does not
    # advertise it has no other runtime to fall back to.
    if not body.supports(2):
        raise HTTPException(
            status_code=409,
            detail={"code": "device_media_protocol_v2_required"},
        )
    websocket_url = settings.device_direct_media_wss_url.strip()
    if not websocket_url:
        raise HTTPException(
            status_code=503,
            detail={"code": "device_direct_media_unavailable"},
        )
    if request.app.state.memory_store.is_account_unavailable(user_id=account_id):
        raise HTTPException(status_code=409, detail={"code": "device_owner_unavailable"})
    async with request.app.state.account_operations.write(account_id):
        return await _create_direct_device_media_session(
            request,
            device_id=device_id,
            account_id=account_id,
            client_id=body.client_id,
            binding_id=binding_id,
            binding_version=binding_version,
            binding_subject_id=binding_subject_id,
            activation_profile_version=activation_profile_version,
            firmware_version=firmware_version,
            board_profile=board_profile,
            websocket_url=websocket_url,
            resume_session_id=body.resume_session_id,
        )


async def _create_direct_device_media_session(
    request: Request,
    *,
    device_id: str,
    account_id: str,
    client_id: str,
    binding_id: str,
    binding_version: int,
    binding_subject_id: str,
    activation_profile_version: int,
    firmware_version: str,
    board_profile: str,
    websocket_url: str,
    resume_session_id: str | None = None,
) -> DeviceGatewaySessionResponse:
    """Issue a direct voice-core media session.

    The direct path must establish the persistent Session authority first.
    PostgresSessionRuntimeService.start commits the signed Runtime Profile;
    only after that commit succeeds do we project the sqlite voice_sessions
    read model used by /session-policy.  SQLite cannot participate in the
    Postgres transaction, so a projection failure immediately fails the
    committed authority session before any credential is minted.
    The device-visible runtime_profile_version is the ledger projection
    version derived from the profile's stable fingerprint (plan 6.3); the
    activation manifest/ledger version is diagnostic only and never overrides
    the projection.  No room name, participant identity or agent name is
    created or required: Python Voice Core keeps interaction authority and
    owns generation semantics (first generation is 1).
    """

    settings = request.app.state.settings
    store = cast(MemoryStore, request.app.state.memory_store)
    require_capability_for_account_id(account_id, "companion_chat", store=store)
    profile = store.get_subject_profile(user_id=account_id)
    if profile is not None and profile.get("subject_category") == "minor":
        guardian = cast(GuardianStorePort, request.app.state.guardian_store)
        consent = await guardian.active_consent(
            minor_user_id=account_id,
            consent_kind="minor_voice_session",
        )
        if consent is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "guardian_consent_required", "capability": "minor_voice_session"},
            )
    runtime_service = cast(
        PostgresSessionRuntimeService | None,
        getattr(request.app.state, "session_runtime_service", None),
    )
    if runtime_service is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        )
    if resume_session_id is not None:
        return await _resume_direct_device_media_session(
            request,
            session_id=resume_session_id,
            device_id=device_id,
            account_id=account_id,
            client_id=client_id,
            binding_id=binding_id,
            binding_version=binding_version,
            binding_subject_id=binding_subject_id,
            firmware_version=firmware_version,
            board_profile=board_profile,
            websocket_url=websocket_url,
        )

    session_id = create_session_id()
    # A reconnect must strictly advance the per-device transport epoch so it
    # can atomically supersede a half-open socket at Edge. The device session
    # row is retained as the durable counter; gaps after an aborted issuance
    # are harmless, while reuse would violate the generation fence.
    try:
        stream_epoch = store.next_device_media_stream_epoch(device_id=device_id)
    except ValueError as exc:
        if str(exc) != "device media stream_epoch exhausted":
            raise
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_stream_epoch_exhausted"},
        ) from exc
    now = datetime.now(UTC)
    created_at = now.isoformat().replace("+00:00", "Z")
    room_name = f"voice-{session_id}"
    owner = store.get_profile(user_id=account_id, now=created_at)
    device_settings = DeviceSettingsAuthority(store).current(device_id, now=now)
    if device_settings.learning_mode != "off":
        require_capability_for_account_id(account_id, "tutor", store=store)
    session_focus = cast(
        Literal["chat", "tutor_english", "tutor_homework"],
        device_settings.learning_mode if device_settings.learning_mode != "off" else "chat",
    )
    # The device serves its one bound person, so their own memory is requested
    # with the conversation; Policy still grants it only on recorded consent.
    requested_capabilities: list[CapabilityValue] = ["chat", "memory_recall_private"]
    if device_settings.learning_mode == "tutor_english":
        requested_capabilities.append("english_practice")
    elif device_settings.learning_mode == "tutor_homework":
        requested_capabilities.append("tutor")

    def persist_voice_session() -> None:
        """Project the already-committed authority into the local read model."""

        store.add_voice_session(
            session_id=session_id,
            user_id=account_id,
            resource_owner_account_id=account_id,
            room_name=room_name,
            voice_backend="cascade",
            created_at=created_at,
            interaction_mode=frozen.interaction_mode,
            mode_policy_version=frozen.mode_policy_version,
            session_focus=frozen.session_focus or "chat",
            digital_self_version_id=None,
            digital_self_manifest_sha256=None,
            preview_grant_id=None,
            self_preview_perspective=None,
            relationship_profile_id=None,
            relationship_profile_version=None,
            legacy_grant_id=None,
            legacy_actor_role=None,
            legacy_grantee_account_id=None,
            legacy_shell_id=None,
            legacy_grant_snapshot_sha256=None,
            legacy_scope_sha256=None,
            legacy_voice_allowed=None,
            legacy_expires_at=None,
            **voice_session_delivery_fields(frozen),
            learning_task_id=None,
        )

    # The bound subject is not a client claim: authenticate_media_challenge
    # already verified the binding primary subject, so the authority evaluates
    # that exact member as the one this device serves. Missing or incomplete category/age facts may still
    # atomically degrade the resulting Runtime Profile to unknown_safe.
    try:
        runtime_profile = await runtime_service.start(
            StartPersistentSessionCommand(
                session_id=session_id,
                actor_id=account_id,
                device_id=device_id,
                expected_binding_version=binding_version,
                idempotency_key=f"device-media-{session_id}",
                now=now,
                requested_capabilities=tuple(requested_capabilities),
                device_bound_subject_id=binding_subject_id,
                profile_ttl=timedelta(seconds=settings.device_runtime_profile_ttl_s),
            ),
        )
    except PersistentSessionDenied as exc:
        logger.warning(
            "persistent Session authority denied device media session_id=%s reason=%s",
            session_id,
            exc,
        )
        raise HTTPException(
            status_code=403,
            detail={"code": "session_runtime_authority_denied"},
        ) from exc
    except SessionRuntimeConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "session_runtime_conflict"},
        ) from exc
    except PersistentSessionUnavailable as exc:
        logger.exception(
            "persistent Session authority failed device media session_id=%s",
            session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc

    # The conversation speaks as the active subject's persona, which the signed
    # Runtime Profile names; the account's own companion stays the default.
    # Resolved only after the profile exists, so a subject switch reaches the
    # voice and not only the prompt.
    companion = await session_companion(
        identity=getattr(request.app.state, "identity_service", None),
        account_id=account_id,
        profile_row=owner,
        runtime_profile=runtime_profile,
    )
    if companion is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "companion_profile_unavailable"},
        )
    frozen = await freeze_companion_delivery(
        companion=companion,
        session_focus=session_focus,
        account_id=account_id,
        store=store,
        voice_manager=getattr(request.app.state, "voice_profile_manager", None),
    )

    # The ticket must carry exactly the authoritative session and binding
    # facts. The active subject comes from that same Runtime Profile and may
    # deliberately be null for unknown_safe.
    profile_facts = (
        ("session_id", runtime_profile.session_id, session_id),
        ("actor_id", runtime_profile.actor_id, account_id),
        ("device_id", runtime_profile.device_id, device_id),
        ("binding_id", runtime_profile.binding_id, binding_id),
        ("binding_version", runtime_profile.binding_version, binding_version),
    )
    mismatches = [field for field, actual, expected in profile_facts if actual != expected]
    if mismatches:
        await _abandon_direct_session(
            runtime_service,
            account_id=account_id,
            session_id=session_id,
            reason_code="ticket_authority_mismatch",
            now=now,
        )
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ticket_authority_mismatch",
                "fields": mismatches,
            },
        )
    # Binding/account ownership and the current natural-person subject are
    # separate authorities. An unknown-safe Runtime Profile deliberately has
    # no active subject, while the durable binding owner remains non-null for
    # lifecycle cleanup and access control.
    authoritative_subject_id = runtime_profile.active_subject_id
    # Device-visible profile projection version (plan 6.3): monotonic per
    # device, advanced only when the profile's stable fingerprint changes.
    # session_epoch stays the identity-switch fence and is never used as the
    # profile version; activation manifest/ledger versions are diagnostic.
    runtime_profile_version = 0
    try:
        ledger_entry = RuntimeProfileLedger(store).observe(
            device_id=device_id,
            runtime_profile_id=runtime_profile.runtime_profile_id,
            content_fingerprint=stable_profile_fingerprint(runtime_profile.model_dump(mode="json")),
            issued_at=runtime_profile.issued_at,
            expires_at=runtime_profile.expires_at,
            now=now,
        )
        runtime_profile_version = ledger_entry.profile_version
    except Exception:
        await _abandon_direct_session(
            runtime_service,
            account_id=account_id,
            session_id=session_id,
            reason_code="direct_media_ledger_unavailable",
            now=now,
        )
        raise
    if activation_profile_version != runtime_profile_version:
        logger.warning(
            "direct_media_activation_profile_version_lag device_id=%s activation=%s "
            "projection=%s (diagnostic only)",
            device_id,
            activation_profile_version,
            runtime_profile_version,
        )
    if device_settings.audio_mode not in allowed_audio_modes(
        AcousticCapabilityAuthority(store).current(device_id)
    ):
        await _abandon_direct_session(
            runtime_service,
            account_id=account_id,
            session_id=session_id,
            reason_code="direct_media_audio_mode_not_attested",
            now=now,
            store=store,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "direct_media_audio_mode_not_attested"},
        )
    try:
        persist_voice_session()
    except Exception:
        await _abandon_direct_session(
            runtime_service,
            account_id=account_id,
            session_id=session_id,
            reason_code="direct_media_projection_unavailable",
            now=now,
            store=store,
        )
        logger.exception(
            "direct device media read-model projection failed session_id=%s",
            session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "direct_media_projection_unavailable"},
        ) from None
    try:
        try:
            ticket = mint_device_direct_media_ticket(
                settings,
                session_id=session_id,
                user_id=account_id,
                device_id=device_id,
                client_id=client_id,
                binding_id=runtime_profile.binding_id,
                binding_version=runtime_profile.binding_version,
                subject_id=authoritative_subject_id,
                runtime_profile_version=runtime_profile_version,
                device_settings={
                    "settings_version": device_settings.settings_version,
                    "volume_limit": device_settings.volume_limit,
                    "screen_brightness": device_settings.screen_brightness,
                    "night_mode": device_settings.night_mode,
                    "do_not_disturb": device_settings.do_not_disturb,
                    "learning_mode": device_settings.learning_mode,
                    "audio_mode": device_settings.audio_mode,
                    "wake_mode": device_settings.wake_mode,
                    "wake_word_id": device_settings.wake_word_id,
                    "wake_word_pinyin": device_settings.wake_word_pinyin,
                    "wake_word_display": device_settings.wake_word_display,
                    "allowed_barge_in": list(device_settings.allowed_barge_in),
                },
                stream_epoch=stream_epoch,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "device_direct_media_unavailable"},
            ) from exc
        store.create_device_media_session(
            session_id=session_id,
            device_id=device_id,
            binding_id=runtime_profile.binding_id,
            binding_version=runtime_profile.binding_version,
            subject_id=binding_subject_id,
            active_subject_id=authoritative_subject_id,
            client_id=client_id,
            runtime="direct_voice_core",
            protocol_version=2,
            stream_epoch=stream_epoch,
            firmware_version=firmware_version,
            board_profile=board_profile,
            runtime_profile_version=runtime_profile_version,
            settings_version=device_settings.settings_version,
            audio_mode_requested=device_settings.audio_mode,
            ticket_jti=ticket.jti,
            created_at=created_at,
            expires_at=ticket.expires_at.isoformat().replace("+00:00", "Z"),
        )
        try:
            await _claim_direct_session_route(
                request,
                session_id=session_id,
                account_id=account_id,
                device_id=device_id,
                stream_epoch=stream_epoch,
            )
        except (SessionNotFound, SessionEpochConflict, SessionOwnershipConflict) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "media_session_resume_epoch_mismatch"},
            ) from exc
        except SessionDirectoryUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "media_session_directory_unavailable"},
            ) from exc
    except Exception:
        await _abandon_direct_session(
            runtime_service,
            account_id=account_id,
            session_id=session_id,
            reason_code="direct_media_session_aborted",
            now=now,
            store=store,
        )
        raise
    return DeviceGatewaySessionResponse(
        session_id=session_id,
        stream_epoch=stream_epoch,
        websocket_url=websocket_url,
        media_token=ticket.token,
        # This field is the bootstrap credential lifetime, not the direct
        # media Session/Profile lifetime. The accepted WSS remains valid
        # after the one-time ticket expires.
        expires_in=max(1, int((ticket.expires_at - now).total_seconds())),
        protocol_version=2,
        runtime="direct_voice_core",
        interaction_authority="python_authoritative",
        binding_id=runtime_profile.binding_id,
        binding_version=runtime_profile.binding_version,
        subject_id=authoritative_subject_id,
        runtime_profile_version=runtime_profile_version,
        uplink=DeviceOpusFormat(sample_rate=16000),
        downlink=DeviceOpusFormat(sample_rate=24000),
    )


async def _resume_direct_device_media_session(
    request: Request,
    *,
    session_id: str,
    device_id: str,
    account_id: str,
    client_id: str,
    binding_id: str,
    binding_version: int,
    binding_subject_id: str,
    firmware_version: str,
    board_profile: str,
    websocket_url: str,
) -> DeviceGatewaySessionResponse:
    """Mint a one-use v2 ticket for the same active Session at a higher epoch.

    This is transport recovery, not conversation creation.  The current
    PostgreSQL Runtime Profile is reloaded and checked against the freshly
    authenticated device binding; no new profile, persona snapshot, Voice
    Session row or provider Session is created.  Projection and directory
    updates use compare-and-swap fences so concurrent retries cannot issue two
    valid epochs for the same active Session.
    """

    settings = request.app.state.settings
    store = cast(MemoryStore, request.app.state.memory_store)
    runtime_service = cast(
        PostgresSessionRuntimeService | None,
        getattr(request.app.state, "session_runtime_service", None),
    )
    if runtime_service is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        )
    previous = store.get_device_media_session(session_id=session_id)
    voice_session = store.get_voice_session_by_id(session_id=session_id)
    if (
        previous is None
        or previous["device_id"] != device_id
        or previous["runtime"] != "direct_voice_core"
        or int(previous["protocol_version"]) != 2
        or previous["closed_at"] is not None
        or voice_session is None
        or str(voice_session.get("user_id") or "") != account_id
    ):
        # Do not reveal whether a foreign id exists.
        raise HTTPException(status_code=404, detail={"code": "media_session_not_found"})
    expected_epoch = int(previous["stream_epoch"])
    if expected_epoch >= DEVICE_STREAM_EPOCH_MAX:
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_stream_epoch_exhausted"},
        )
    try:
        runtime_profile, _context = await runtime_service.current(
            actor_id=account_id,
            session_id=session_id,
            now=datetime.now(UTC),
        )
    except PersistentSessionNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "media_session_authority_not_found"},
        ) from exc
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "session_runtime_authority_denied"},
        ) from exc
    except PersistentSessionUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc

    authority_facts = (
        (runtime_profile.session_id, session_id),
        (runtime_profile.actor_id, account_id),
        (runtime_profile.device_id, device_id),
        (runtime_profile.binding_id, binding_id),
        (runtime_profile.binding_version, binding_version),
        (str(previous["binding_id"]), binding_id),
        (int(previous["binding_version"]), binding_version),
        (str(previous["subject_id"]), binding_subject_id),
        (previous["active_subject_id"], runtime_profile.active_subject_id),
    )
    if any(actual != expected for actual, expected in authority_facts):
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_resume_authority_mismatch"},
        )

    ledger = RuntimeProfileLedger(store).current(device_id)
    if ledger is None or ledger.runtime_profile_id != runtime_profile.runtime_profile_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "runtime_profile_not_current"},
        )
    device_settings = DeviceSettingsAuthority(store).current(device_id, now=datetime.now(UTC))
    if device_settings.audio_mode not in allowed_audio_modes(
        AcousticCapabilityAuthority(store).current(device_id)
    ):
        raise HTTPException(
            status_code=503,
            detail={"code": "direct_media_audio_mode_not_attested"},
        )
    if str(voice_session.get("interaction_mode") or "") == "companion":
        owner = store.get_profile(
            user_id=account_id,
            now=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        companion = await session_companion(
            identity=getattr(request.app.state, "identity_service", None),
            account_id=account_id,
            profile_row=owner,
            runtime_profile=runtime_profile,
        )
        if companion is not None:
            session_focus = cast(
                Literal["chat", "tutor_english", "tutor_homework"],
                device_settings.learning_mode
                if device_settings.learning_mode != "off"
                else "chat",
            )
            frozen = await freeze_companion_delivery(
                companion=companion,
                session_focus=session_focus,
                account_id=account_id,
                store=store,
                voice_manager=getattr(request.app.state, "voice_profile_manager", None),
            )
            store.update_voice_session_companion_delivery(
                session_id=session_id,
                **voice_session_delivery_fields(frozen),
            )

    directory = cast(
        SessionDirectory | None,
        getattr(request.app.state, "session_directory", None),
    )
    route: SessionRoute | None = None
    if directory is not None:
        try:
            current_route = await directory.lookup(session_id)
            # Repair the one possible partial Saga: SQLite may have committed
            # epoch N while the directory CAS for N failed. A retry sees the
            # durable projection exactly one epoch ahead and completes that
            # previously authorized transition before minting N+1.
            if (
                current_route is not None
                and current_route.account_id == account_id
                and current_route.device_id == device_id
                and current_route.media_runtime == "direct_voice_core"
                and current_route.stream_epoch + 1 == expected_epoch
            ):
                current_route = await directory.reconnect(
                    session_id,
                    expected_stream_epoch=current_route.stream_epoch,
                    media_edge_id=settings.media_edge_id,
                    voice_core_id=settings.voice_core_id,
                    device_id=device_id,
                    ttl_s=int(settings.device_runtime_profile_ttl_s),
                    owner_instance_id=current_route.owner_instance_id,
                    expected_ownership_epoch=current_route.ownership_epoch,
                )
            if (
                current_route is None
                or current_route.account_id != account_id
                or current_route.device_id != device_id
                or current_route.media_runtime != "direct_voice_core"
                or current_route.stream_epoch != expected_epoch
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "media_session_resume_epoch_mismatch"},
                )
            route = current_route
        except HTTPException:
            raise
        except SessionDirectoryUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "media_session_directory_unavailable"},
            ) from exc
        except (SessionNotFound, SessionDraining, SessionEpochConflict) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "media_session_resume_epoch_mismatch"},
            ) from exc
    stream_epoch = expected_epoch + 1
    if stream_epoch <= expected_epoch:
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_resume_epoch_mismatch"},
        )

    now = datetime.now(UTC)
    try:
        ticket = mint_device_direct_media_ticket(
            settings,
            session_id=session_id,
            user_id=account_id,
            device_id=device_id,
            client_id=client_id,
            binding_id=binding_id,
            binding_version=binding_version,
            subject_id=runtime_profile.active_subject_id,
            runtime_profile_version=ledger.profile_version,
            device_settings={
                "settings_version": device_settings.settings_version,
                "volume_limit": device_settings.volume_limit,
                "screen_brightness": device_settings.screen_brightness,
                "night_mode": device_settings.night_mode,
                "do_not_disturb": device_settings.do_not_disturb,
                "learning_mode": device_settings.learning_mode,
                "audio_mode": device_settings.audio_mode,
                "wake_mode": device_settings.wake_mode,
                "wake_word_id": device_settings.wake_word_id,
                "wake_word_pinyin": device_settings.wake_word_pinyin,
                "wake_word_display": device_settings.wake_word_display,
                "allowed_barge_in": list(device_settings.allowed_barge_in),
            },
            stream_epoch=stream_epoch,
            now=now,
        )
        store.rotate_device_media_session_transport(
            session_id=session_id,
            expected_stream_epoch=expected_epoch,
            stream_epoch=stream_epoch,
            client_id=client_id,
            firmware_version=firmware_version,
            board_profile=board_profile,
            runtime_profile_version=ledger.profile_version,
            settings_version=device_settings.settings_version,
            audio_mode_requested=device_settings.audio_mode,
            ticket_jti=ticket.jti,
            expires_at=ticket.expires_at.isoformat().replace("+00:00", "Z"),
            counter_updated_at=now.isoformat().replace("+00:00", "Z"),
        )
    except ValueError as exc:
        # A concurrent reconnect may have won after the directory CAS.  Never
        # return the losing credential; it cannot displace the newer epoch.
        logger.info(
            "direct device reconnect CAS lost session_id=%s expected_epoch=%s",
            session_id,
            expected_epoch,
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_resume_epoch_mismatch"},
        ) from exc
    if directory is not None:
        assert route is not None
        try:
            advanced = await directory.reconnect(
                session_id,
                expected_stream_epoch=expected_epoch,
                media_edge_id=settings.media_edge_id,
                voice_core_id=settings.voice_core_id,
                device_id=device_id,
                ttl_s=int(settings.device_runtime_profile_ttl_s),
                owner_instance_id=route.owner_instance_id,
                expected_ownership_epoch=route.ownership_epoch,
            )
        except SessionDirectoryUnavailable as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "media_session_resume_epoch_mismatch"},
            ) from exc
        except (SessionNotFound, SessionDraining, SessionEpochConflict) as exc:
            # The durable projection already owns stream_epoch. A later retry
            # will repair a route exactly one epoch behind before issuing any
            # further ticket; larger divergence still fails closed.
            raise HTTPException(
                status_code=503,
                detail={"code": "media_session_directory_unavailable"},
            ) from exc
        if advanced.stream_epoch != stream_epoch:
            raise HTTPException(
                status_code=409,
                detail={"code": "media_session_resume_epoch_mismatch"},
            )

    return DeviceGatewaySessionResponse(
        session_id=session_id,
        stream_epoch=stream_epoch,
        websocket_url=websocket_url,
        media_token=ticket.token,
        expires_in=max(1, int((ticket.expires_at - now).total_seconds())),
        protocol_version=2,
        runtime="direct_voice_core",
        interaction_authority="python_authoritative",
        binding_id=binding_id,
        binding_version=binding_version,
        subject_id=runtime_profile.active_subject_id,
        runtime_profile_version=ledger.profile_version,
        uplink=DeviceOpusFormat(sample_rate=16000),
        downlink=DeviceOpusFormat(sample_rate=24000),
    )


async def _claim_direct_session_route(
    request: Request,
    *,
    session_id: str,
    account_id: str,
    device_id: str,
    stream_epoch: int,
) -> SessionRoute | None:
    """Claim the direct session directory route with the direct profile TTL.

    The route is leased for device_runtime_profile_ttl_s (not the directory's
    300 s default) so it cannot expire while the direct Runtime Profile, which
    uses the same TTL, is still authoritative.
    """

    directory = cast(
        SessionDirectory | None,
        getattr(request.app.state, "session_directory", None),
    )
    if directory is None:
        return None
    settings = request.app.state.settings
    return await directory.claim(
        session_id,
        media_edge_id=settings.media_edge_id,
        voice_core_id=settings.voice_core_id,
        device_id=device_id,
        account_id=account_id,
        stream_epoch=stream_epoch,
        generation=0,
        media_runtime="direct_voice_core",
        owner_instance_id=settings.media_edge_id,
        ttl_s=int(settings.device_runtime_profile_ttl_s),
    )


async def _abandon_direct_session(
    runtime_service: PostgresSessionRuntimeService,
    *,
    account_id: str,
    session_id: str,
    reason_code: str,
    now: datetime,
    store: MemoryStore | None = None,
) -> None:
    """Best-effort invalidation when a direct session cannot complete.

    The signed Runtime Profile is already committed when this helper runs.
    fail_session invalidates the Postgres authority so /session-policy fails
    closed even if a later SQLite projection or route step left a residue.
    Local projections are removed best-effort after the authority transition.
    Cleanup must never mask the original error, so failures are logged and
    swallowed.
    """

    try:
        await runtime_service.fail_session(
            actor_id=account_id,
            session_id=session_id,
            reason_code=reason_code,
            now=now,
        )
    except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
        logger.exception("direct device media authority cleanup failed session_id=%s", session_id)
    if store is not None:
        try:
            store.delete_device_media_session(session_id=session_id)
            store.delete_voice_session(session_id=session_id)
        except Exception:  # noqa: BLE001 - cleanup must not mask the original failure
            logger.exception(
                "direct device media projection cleanup failed session_id=%s", session_id
            )


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
        raise HTTPException(
            status_code=429, detail="too many outstanding device challenges"
        ) from exc


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


def _require_reply_delivery_token(request: Request, provided: str) -> None:
    expected = request.app.state.settings.media_reply_delivery_token.get_secret_value().strip()
    if not expected or not hmac.compare_digest(provided.strip(), expected):
        raise HTTPException(status_code=401, detail="media reply delivery token is invalid")


@internal_router.post("/reply-delivery")
async def record_media_reply_delivery(
    body: MediaReplyDeliveryEventRequest,
    request: Request,
    x_media_reply_delivery_token: Annotated[str, Header(alias="X-Media-Reply-Delivery-Token")],
) -> dict[str, Any]:
    """Persist one idempotent, text-free delivery observation."""

    _require_reply_delivery_token(request, x_media_reply_delivery_token)
    expected_delivery_id = (
        f"{body.session_id}/epoch-{body.session_epoch}"
        f"/turn-{body.turn_id}/generation-{body.generation_id}/tool-{body.tool_epoch}"
    )
    expected_event_id = hashlib.sha256(
        f"{expected_delivery_id}\0{body.event_type}".encode()
    ).hexdigest()
    if not hmac.compare_digest(body.delivery_id, expected_delivery_id) or not hmac.compare_digest(
        body.event_id, expected_event_id
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "media_reply_delivery_fence_mismatch"},
        )
    occurred_at = body.occurred_at.isoformat().replace("+00:00", "Z")
    received_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        inserted = request.app.state.memory_store.record_media_reply_delivery_event(
            event_id=body.event_id,
            schema_version=body.schema_version,
            delivery_id=body.delivery_id,
            session_id=body.session_id,
            session_epoch=body.session_epoch,
            turn_id=body.turn_id,
            generation_id=body.generation_id,
            tool_epoch=body.tool_epoch,
            event_type=body.event_type,
            terminal_event=body.terminal_event,
            terminal_reason=body.terminal_reason,
            first_frame_sent=body.first_frame_sent,
            provider_completed=body.provider_completed,
            actual_heard=body.actual_heard,
            playback_ended=body.playback_ended,
            reason=body.reason,
            occurred_at=occurred_at,
            received_at=received_at,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "media_reply_delivery_conflict", "message": str(exc)},
        ) from exc
    return {"event_id": body.event_id, "delivery_id": body.delivery_id, "inserted": inserted}


@internal_router.get("/reply-delivery")
async def read_media_reply_delivery(
    request: Request,
    delivery_id: str = Query(min_length=1, max_length=512),
    x_media_reply_delivery_token: Annotated[str, Header(alias="X-Media-Reply-Delivery-Token")]
    = "",
) -> dict[str, Any]:
    """Read one exact delivery projection for diagnostics or SLO consumers."""

    _require_reply_delivery_token(request, x_media_reply_delivery_token)
    events = request.app.state.memory_store.media_reply_delivery_events(
        delivery_id=delivery_id,
    )
    return {
        "delivery_id": delivery_id,
        "events": [
            {
                **event,
                "first_frame_sent": bool(event["first_frame_sent"]),
                "provider_completed": bool(event["provider_completed"]),
                "actual_heard": bool(event["actual_heard"]),
                "playback_ended": bool(event["playback_ended"]),
            }
            for event in events
        ],
    }


__all__ = [
    "RegisterDeviceIdentityRequest",
    "MediaSLOReportRequest",
    "MediaReplyDeliveryEventRequest",
    "device_router",
    "internal_router",
    "router",
]
