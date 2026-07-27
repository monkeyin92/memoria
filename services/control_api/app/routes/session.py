"""Session control routes (ch.20.2)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.common.companions import DEFAULT_COMPANION_ID, companion_definition
from services.control_api.app.account_gate import require_writable_account
from services.control_api.app.database import MemoryStore
from services.control_api.app.miniprogram_gateway_session import (
    CreateMiniProgramSessionResponse,
    MiniProgramMediaGateway,
    build_gateway_ticket,
    require_gateway_url,
)
from services.control_api.app.mode_policy import FrozenMode, ModePolicy
from services.control_api.app.security import (
    AuthenticatedUser,
    create_session_id,
    mint_participant_token,
    require_authenticated_user,
    require_matching_user,
)
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfVersion,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
    VersionNotFoundError,
    VoiceProfileManifestRef,
)
from services.digital_self.preview import (
    PreviewConflictError,
    PreviewNotFoundError,
    SelfPreviewRegistryPort,
)
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessSnapshot,
    LegacyGrant,
    LegacyManifestItemRef,
    LegacyNotFoundError,
    LegacyRegistryPort,
)
from services.self_model.domain import (
    RelationshipProfile,
    SelfModelNotFoundError,
    SelfModelRegistryPort,
)
from services.speaker.domain import SpeakerAuthorityPort
from services.voice_profile.domain import VoiceProfilePort, VoiceResolution

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)
telemetry_logger = logging.getLogger("uvicorn.error")
CONTROL_TOPIC = "voice-agent.control"
MINIPROGRAM_CLIENT_PLATFORM = "miniprogram"
QWEN_OMNI_FLASH_MODEL = "qwen3.5-omni-flash-realtime"
QWEN_OMNI_WORKSPACE_ID = "llm-qp8mf178biax7m6c"
# WebRTC realtime backends (browser media direct to DashScope).
OMNI_BACKENDS = frozenset({"qwen_omni"})
REALTIME_BACKENDS = OMNI_BACKENDS
OMNI_MODELS: dict[str, str] = {
    "qwen_omni": QWEN_OMNI_FLASH_MODEL,
}
OMNI_MAX_SDP_BYTES = 64 * 1024
OMNI_MAX_SDP_EXCHANGES = 2
_WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

_STOP_REQUESTS: list[dict[str, Any]] = []


def _matching_preview_voice(
    ref: VoiceProfileManifestRef | None,
    resolution: VoiceResolution,
) -> tuple[
    str | None,
    int | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    expires_at = (
        resolution.provider_expires_at.isoformat()
        if resolution.provider_expires_at is not None
        else None
    )
    speaker_sha256 = (
        hashlib.sha256(resolution.voice_id.encode("utf-8")).hexdigest()
        if resolution.voice_id is not None
        else None
    )
    if (
        ref is None
        or resolution.mode != "active"
        or resolution.voice_kind != "personal"
        or resolution.profile_id != ref.profile_id
        or resolution.version_number != ref.version_number
        or resolution.provider != ref.provider
        or resolution.model != ref.target_model
        or resolution.resource_id != ref.resource_id
        or expires_at != ref.provider_expires_at
        or speaker_sha256 != ref.speaker_sha256
    ):
        return None, None, None, None, None, None, None
    return (
        ref.profile_id,
        ref.version_number,
        ref.provider,
        ref.target_model,
        ref.resource_id,
        ref.provider_expires_at,
        ref.speaker_sha256,
    )


def _manifest_item_refs(version: DigitalSelfVersion) -> frozenset[LegacyManifestItemRef]:
    refs: set[LegacyManifestItemRef] = set()
    for entry in version.manifest.entries:
        if isinstance(entry, MemoryClaimManifestEntry):
            refs.add(LegacyManifestItemRef("memory_claim", entry.claim_id))
        elif isinstance(entry, PersonaTraitManifestEntry):
            refs.add(LegacyManifestItemRef("persona_trait", entry.trait_id))
        elif isinstance(entry, CognitiveClaimManifestEntry):
            refs.add(LegacyManifestItemRef("cognitive_claim", entry.claim_id))
        elif isinstance(entry, DecisionCaseManifestEntry):
            refs.add(LegacyManifestItemRef("decision_case", entry.case_id))
        elif isinstance(entry, RelationshipProfileManifestEntry):
            refs.add(LegacyManifestItemRef("relationship_profile", entry.profile_id))
    return frozenset(refs)


def _legacy_relationship_matches(
    grant: LegacyGrant,
    profile: RelationshipProfile,
    version: DigitalSelfVersion,
) -> bool:
    snapshot = grant.relationship
    manifest = next(
        (
            entry
            for entry in version.manifest.entries
            if isinstance(entry, RelationshipProfileManifestEntry)
            and entry.profile_id == snapshot.profile_id
            and entry.version_number == snapshot.version_number
        ),
        None,
    )
    expected = (
        snapshot.profile_id,
        snapshot.version_number,
        snapshot.relationship_id,
        snapshot.salutation,
        snapshot.tone,
        snapshot.advice_style,
        snapshot.sharing_scope,
        snapshot.boundaries,
    )
    live = (
        profile.profile_id,
        profile.version_number,
        profile.relationship_id,
        profile.salutation,
        profile.tone,
        profile.advice_style,
        profile.sharing_scope,
        profile.boundaries,
    )
    frozen = (
        manifest.profile_id,
        manifest.version_number,
        manifest.relationship_id,
        manifest.salutation,
        manifest.tone,
        manifest.advice_style,
        manifest.sharing_scope,
        manifest.boundaries,
    ) if manifest is not None else None
    return (
        profile.account_id == grant.owner_account_id
        and profile.status == "approved"
        and profile.step_up_verified
        and not profile.unresolved_conflict
        and expected == live == frozen
    )


class ClientInfo(BaseModel):
    platform: str = "web"
    timezone: str = "Asia/Shanghai"


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    voice_backend: Literal["cascade", "qwen_omni"] = "cascade"
    interaction_mode: Literal["companion", "self_preview", "legacy", "archive"] = "companion"
    # Owner/version/relationship remain server-owned. Legacy accepts only the
    # opaque grant id and resolves every other authority server-side.
    digital_self_version_id: str | None = Field(default=None, min_length=1, max_length=128)
    preview_grant_id: str | None = Field(default=None, min_length=1, max_length=128)
    relationship_profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    legacy_grant_id: str | None = Field(default=None, min_length=1, max_length=128)
    learning_task_id: str | None = Field(default=None, min_length=1, max_length=128)
    locale: str = "zh-CN"
    client: ClientInfo = Field(default_factory=ClientInfo)

    @model_validator(mode="after")
    def reject_client_owned_references(self) -> CreateSessionRequest:
        if any(
            value is not None
            for value in (
                self.digital_self_version_id,
                self.relationship_profile_id,
            )
        ):
            raise ValueError("Digital Self and relationship references are server-owned")
        if self.interaction_mode == "self_preview" and self.preview_grant_id is None:
            raise ValueError("self_preview requires a server-issued preview grant")
        if self.interaction_mode != "self_preview" and self.preview_grant_id is not None:
            raise ValueError("preview_grant_id is only valid for self_preview")
        if self.interaction_mode == "legacy" and self.legacy_grant_id is None:
            raise ValueError("legacy requires a server-issued grant id")
        if self.interaction_mode != "legacy" and self.legacy_grant_id is not None:
            raise ValueError("legacy_grant_id is only valid for legacy")
        return self


class CreateSessionResponse(BaseModel):
    session_id: str
    livekit_url: str
    room_name: str
    participant_token: str
    expires_in: int
    agent_name: str
    voice_backend: Literal["cascade"] = "cascade"
    config: dict[str, Any]
    interaction: dict[str, Any]
    learning_task_id: str | None = None


class CreateOmniSessionResponse(BaseModel):
    session_id: str
    voice_backend: Literal["qwen_omni"] = "qwen_omni"
    sdp_exchange_path: str
    config: dict[str, Any]
    interaction: dict[str, Any]
    learning_task_id: str | None = None


class StopResponseBody(BaseModel):
    reason: str = "user_button"


class WebRTCMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jitter: float | None = Field(default=None, ge=0)
    packets_lost: int | None = Field(default=None, ge=0)
    packets_received: int | None = Field(default=None, ge=0)
    packets_discarded: int | None = Field(default=None, ge=0)
    bytes_received: int | None = Field(default=None, ge=0)
    nack_count: int | None = Field(default=None, ge=0)
    concealed_samples: int | None = Field(default=None, ge=0)
    silent_concealed_samples: int | None = Field(default=None, ge=0)
    total_samples_received: int | None = Field(default=None, ge=0)
    concealment_events: int | None = Field(default=None, ge=0)
    concealment_ratio: float | None = Field(default=None, ge=0, le=1)
    non_silent_concealment_ratio: float | None = Field(default=None, ge=0, le=1)
    jitter_buffer_delay: float | None = Field(default=None, ge=0)
    jitter_buffer_target_delay: float | None = Field(default=None, ge=0)
    jitter_buffer_minimum_delay: float | None = Field(default=None, ge=0)
    jitter_buffer_emitted_count: int | None = Field(default=None, ge=0)
    total_samples_duration: float | None = Field(default=None, ge=0)
    average_jitter_buffer_delay_ms: float | None = Field(default=None, ge=0)
    average_jitter_buffer_target_delay_ms: float | None = Field(default=None, ge=0)
    encoded_audio_bitrate_kbps: float | None = Field(default=None, ge=0)
    inserted_samples_for_deceleration: int | None = Field(default=None, ge=0)
    removed_samples_for_acceleration: int | None = Field(default=None, ge=0)


class OmniTelemetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal[
        "session_created",
        "room_connected",
        "agent_ready",
        "track_subscribed",
        "audio_attached",
        "play_resolved",
        "play_rejected",
        "playing",
        "first_playback",
        "media_error",
        "omni_session_created",
        "omni_ab_profile",
        "omni_welcome_armed",
        "omni_welcome_requested",
        "omni_welcome_suppressed",
        "omni_enroll_prompt_requested",
        "omni_interrupt_ack_requested",
        "omni_controlled_create_queued",
        "omni_auto_response_deferred",
        "omni_active_response_conflict",
        "omni_cancel_settle_timeout",
        "omni_ghost_response_skipped",
        "omni_playout_buffer_configured",
        "omni_feedback_guard_started",
        "omni_feedback_suppressed",
        "omni_speech_started",
        "omni_speech_stopped",
        "omni_response_created",
        "omni_response_cancelled",
        "omni_cancelled_response_done",
        "omni_response_done",
        "omni_upstream_error",
        "omni_transcription_failed",
        "webrtc_inbound_audio",
        "audio_ws_connected",
        "audio_ws_closed",
    ]
    elapsed_ms: int = Field(ge=0, le=24 * 60 * 60 * 1000)
    turn_id: int = Field(ge=0)
    generation_id: int = Field(ge=0)
    metrics: WebRTCMetrics | None = None
    # Optional DashScope realtime error fields (short, no audio/credentials).
    error_type: str | None = Field(default=None, max_length=80)
    error_code: str | None = Field(default=None, max_length=80)
    error_param: str | None = Field(default=None, max_length=80)


@router.post(
    "",
    response_model=(
        CreateSessionResponse | CreateOmniSessionResponse | CreateMiniProgramSessionResponse
    ),
)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> CreateSessionResponse | CreateOmniSessionResponse | CreateMiniProgramSessionResponse:
    settings = request.app.state.settings
    user_id = require_matching_user(body.user_id, user) if body.user_id else user.user_id
    mini_program = body.client.platform == MINIPROGRAM_CLIENT_PLATFORM
    availability = ModePolicy.availability(body.interaction_mode)
    if not availability.conversational:
        raise HTTPException(
            status_code=409,
            detail={"code": "mode_not_conversational", "mode": body.interaction_mode},
        )
    if availability.status == "blocked" and body.interaction_mode != "legacy":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "mode_blocked",
                "mode": body.interaction_mode,
                "missing": list(availability.missing),
            },
        )
    if settings.environment == "production" and body.voice_backend in REALTIME_BACKENDS:
        raise HTTPException(status_code=409, detail="端到端实时模型仅限隔离 A/B 环境")
    if mini_program and body.voice_backend != "cascade":
        raise HTTPException(
            status_code=409,
            detail={"code": "miniprogram_requires_cascade"},
        )
    if mini_program:
        require_gateway_url(settings)
    if body.interaction_mode in {"self_preview", "legacy"} and body.voice_backend != "cascade":
        raise HTTPException(
            status_code=409,
            detail={"code": f"{body.interaction_mode}_requires_controlled_backend"},
        )
    store = cast(MemoryStore, request.app.state.memory_store)
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    companion = companion_definition(
        store.get_profile(user_id=user_id, now=created_at).get("companion_id")
        or DEFAULT_COMPANION_ID
    )
    if companion is None:
        raise HTTPException(status_code=409, detail="companion profile is unavailable")
    preview_registry = cast(SelfPreviewRegistryPort, request.app.state.self_preview_registry)
    legacy_access: LegacyAccessSnapshot | None = None
    legacy_grant: LegacyGrant | None = None
    if body.interaction_mode == "legacy":
        assert body.legacy_grant_id is not None
        legacy_registry = cast(LegacyRegistryPort, request.app.state.legacy_registry)
        try:
            legacy_grant = await legacy_registry.get_grant(
                actor_account_id=user_id,
                grant_id=body.legacy_grant_id,
            )
            if user_id == legacy_grant.owner_account_id:
                purpose: Literal["owner_preview", "grantee_session"] = "owner_preview"
            elif user_id == legacy_grant.grantee_account_id:
                purpose = "grantee_session"
            else:  # pragma: no cover - get_grant already enforces visibility
                raise LegacyNotFoundError(body.legacy_grant_id)
            legacy_access = await legacy_registry.resolve_access(
                actor_account_id=user_id,
                grant_id=body.legacy_grant_id,
                purpose=purpose,
                now=datetime.now(UTC),
            )
            if (
                legacy_access.grant_snapshot_sha256
                != legacy_grant.grant_snapshot_sha256
                or legacy_access.scope_sha256 != legacy_grant.scope_sha256
                or legacy_access.version_id != legacy_grant.version_id
                or legacy_access.version_number != legacy_grant.version_number
                or legacy_access.manifest_sha256 != legacy_grant.manifest_sha256
            ):
                raise LegacyAccessDeniedError("legacy grant changed during access resolution")
            if store.is_account_unavailable(
                user_id=legacy_access.resource_owner_account_id
            ) or store.is_account_unavailable(user_id=legacy_access.grantee_account_id):
                raise LegacyAccessDeniedError("legacy account is unavailable")
            version = await request.app.state.digital_self_registry.get(
                account_id=legacy_access.resource_owner_account_id,
                version_id=legacy_access.version_id,
            )
            if (
                version.status != "frozen"
                or version.version_number != legacy_access.version_number
                or version.manifest_sha256 != legacy_access.manifest_sha256
                or not set(legacy_access.allowed_items) <= _manifest_item_refs(version)
            ):
                raise LegacyAccessDeniedError("legacy Digital Self snapshot is unavailable")
            relationship = await cast(
                SelfModelRegistryPort, request.app.state.self_model_registry
            ).get_relationship_profile(
                account_id=legacy_access.resource_owner_account_id,
                profile_id=legacy_access.relationship_profile_id,
                version_number=legacy_access.relationship_profile_version,
            )
            if not _legacy_relationship_matches(legacy_grant, relationship, version):
                raise LegacyAccessDeniedError("legacy relationship snapshot is unavailable")
            speaker_authority = cast(SpeakerAuthorityPort, request.app.state.speaker_authority)
            if not any(
                profile.status == "active" for profile in await speaker_authority.profiles(user_id)
            ):
                raise LegacyAccessDeniedError("legacy actor voice is not verified")
        except (
            LegacyAccessDeniedError,
            LegacyNotFoundError,
            SelfModelNotFoundError,
            VersionNotFoundError,
        ) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "legacy_grant_unavailable"},
            ) from exc
        owner_profile = store.get_profile(
            user_id=legacy_access.resource_owner_account_id,
            now=created_at,
        )
        companion = companion_definition(
            owner_profile.get("companion_id") or DEFAULT_COMPANION_ID
        )
        if companion is None:  # pragma: no cover - static companion catalog
            raise HTTPException(status_code=409, detail="companion profile is unavailable")
    if body.interaction_mode == "self_preview":
        assert body.preview_grant_id is not None
        try:
            grant = await preview_registry.consume_grant(
                account_id=user_id,
                grant_id=body.preview_grant_id,
                now=datetime.now(UTC),
            )
            version = await request.app.state.digital_self_registry.get(
                account_id=user_id,
                version_id=grant.version_id,
            )
        except (
            PreviewConflictError,
            PreviewNotFoundError,
            VersionNotFoundError,
        ) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "preview_grant_unavailable"},
            ) from exc
        if (
            version.status not in {"approved", "frozen"}
            or version.manifest_sha256 != grant.manifest_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "preview_version_unavailable"},
            )
        if (
            await preview_registry.version_stale(
                account_id=user_id,
                version_id=version.version_id,
                manifest_sha256=version.manifest_sha256,
            )
            or await preview_registry.completed_verdict(
                account_id=user_id,
                version_id=version.version_id,
                manifest_sha256=version.manifest_sha256,
            )
            != "approve"
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "preview_version_unavailable"},
            )
        speaker_authority = cast(SpeakerAuthorityPort, request.app.state.speaker_authority)
        if not any(
            profile.status == "active" for profile in await speaker_authority.profiles(user_id)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "preview_prerequisite_missing",
                    "missing": ["verified_owner_voice"],
                },
            )
        voice_resolution = await cast(
            VoiceProfilePort, request.app.state.voice_profile_manager
        ).resolve(account_id=user_id)
        (
            voice_profile_id,
            voice_profile_version,
            voice_provider,
            voice_model,
            voice_resource_id,
            voice_provider_expires_at,
            voice_speaker_sha256,
        ) = _matching_preview_voice(
            version.manifest.source_summary.voice_profile,
            voice_resolution,
        )
        preview_frozen = ModePolicy.freeze_self_preview(
            version_id=version.version_id,
            manifest_sha256=version.manifest_sha256,
            preview_grant_id=grant.grant_id,
            perspective=grant.perspective,
            voice_profile_id=voice_profile_id,
            voice_profile_version=voice_profile_version,
            voice_provider=voice_provider,
            voice_model=voice_model,
            voice_resource_id=voice_resource_id,
            voice_provider_expires_at=voice_provider_expires_at,
            voice_speaker_sha256=voice_speaker_sha256,
            fallback_voice_profile_id=companion.designed_voice_profile,
            fallback_voice_provider="volcengine_doubao",
            fallback_voice_model="seed-tts-2.0",
            fallback_voice_resource_id="seed-tts-2.0",
        )
    else:
        preview_frozen = None
    legacy_frozen: FrozenMode | None = None
    if body.interaction_mode == "legacy":
        assert legacy_access is not None and legacy_grant is not None
        legacy_version = await request.app.state.digital_self_registry.get(
            account_id=legacy_access.resource_owner_account_id,
            version_id=legacy_access.version_id,
        )
        if legacy_access.voice_allowed:
            voice_resolution = await cast(
                VoiceProfilePort, request.app.state.voice_profile_manager
            ).resolve(account_id=legacy_access.resource_owner_account_id)
            (
                voice_profile_id,
                voice_profile_version,
                voice_provider,
                voice_model,
                voice_resource_id,
                voice_provider_expires_at,
                voice_speaker_sha256,
            ) = _matching_preview_voice(
                legacy_version.manifest.source_summary.voice_profile,
                voice_resolution,
            )
        else:
            (
                voice_profile_id,
                voice_profile_version,
                voice_provider,
                voice_model,
                voice_resource_id,
                voice_provider_expires_at,
                voice_speaker_sha256,
            ) = (None, None, None, None, None, None, None)
        legacy_frozen = ModePolicy.freeze_legacy(
            actor_account_id=user_id,
            resource_owner_account_id=legacy_access.resource_owner_account_id,
            actor_role=legacy_access.actor_role,
            grantee_account_id=legacy_access.grantee_account_id,
            grant_id=legacy_access.grant_id,
            grant_snapshot_sha256=legacy_access.grant_snapshot_sha256,
            version_id=legacy_access.version_id,
            manifest_sha256=legacy_access.manifest_sha256,
            relationship_profile_id=legacy_access.relationship_profile_id,
            relationship_profile_version=legacy_access.relationship_profile_version,
            scope_sha256=legacy_access.scope_sha256,
            shell_id=legacy_access.shell_id,
            voice_allowed=legacy_access.voice_allowed,
            expires_at=legacy_access.expires_at.isoformat(),
            voice_profile_id=voice_profile_id,
            voice_profile_version=voice_profile_version,
            voice_provider=voice_provider,
            voice_model=voice_model,
            voice_resource_id=voice_resource_id,
            voice_provider_expires_at=voice_provider_expires_at,
            voice_speaker_sha256=voice_speaker_sha256,
            fallback_voice_profile_id=companion.designed_voice_profile,
            fallback_voice_provider="volcengine_doubao",
            fallback_voice_model="seed-tts-2.0",
            fallback_voice_resource_id="seed-tts-2.0",
        )
    session_id = create_session_id()
    # The room name is also the Agent's trusted source for the public session id.
    room_name = f"voice-{session_id}"
    identity = f"user-{user_id}-{session_id[:8]}"
    if preview_frozen is not None:
        frozen = preview_frozen
    elif legacy_frozen is not None:
        frozen = legacy_frozen
    else:
        frozen = ModePolicy.freeze_companion(companion)

    async def persist_voice_session() -> str | None:
        learning_task_id: str | None = None
        lock = cast(asyncio.Lock, request.app.state.growth_task_lock)
        if body.learning_task_id is not None:
            async with lock:
                task = await request.app.state.growth_reader.task(
                    account_id=user_id,
                    task_id=body.learning_task_id,
                )
                if (
                    body.interaction_mode != "companion"
                    or task is None
                    or task.kind != "natural_chat"
                    or task.status != "active"
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "learning_task_not_active"},
                    )
                learning_task_id = task.task_id
                store.add_voice_session(
                    session_id=session_id,
                    user_id=user_id,
                    resource_owner_account_id=(
                        frozen.resource_owner_account_id or user_id
                    ),
                    room_name=room_name,
                    voice_backend=body.voice_backend,
                    created_at=created_at,
                    interaction_mode=frozen.interaction_mode,
                    mode_policy_version=frozen.mode_policy_version,
                    digital_self_version_id=frozen.digital_self_version_id,
                    digital_self_manifest_sha256=frozen.manifest_sha256,
                    preview_grant_id=frozen.preview_grant_id,
                    self_preview_perspective=frozen.perspective,
                    relationship_profile_id=frozen.relationship_profile_id,
                    relationship_profile_version=frozen.relationship_profile_version,
                    legacy_grant_id=frozen.legacy_grant_id,
                    legacy_actor_role=frozen.legacy_actor_role,
                    legacy_grantee_account_id=frozen.legacy_grantee_account_id,
                    legacy_shell_id=frozen.legacy_shell_id,
                    legacy_grant_snapshot_sha256=frozen.legacy_grant_snapshot_sha256,
                    legacy_scope_sha256=frozen.legacy_scope_sha256,
                    legacy_voice_allowed=frozen.legacy_voice_allowed,
                    legacy_expires_at=frozen.legacy_expires_at,
                    companion_style_id=frozen.companion_style_id,
                    companion_style_version=frozen.companion_style_version,
                    voice_profile_id=frozen.voice_profile_id,
                    voice_profile_version=frozen.voice_profile_version,
                    voice_provider=frozen.voice_provider,
                    voice_model=frozen.voice_model,
                    voice_resource_id=frozen.voice_resource_id,
                    voice_provider_expires_at=frozen.voice_provider_expires_at,
                    voice_speaker_sha256=frozen.voice_speaker_sha256,
                    fallback_voice_profile_id=frozen.fallback_voice_profile_id,
                    fallback_voice_provider=frozen.fallback_voice_provider,
                    fallback_voice_model=frozen.fallback_voice_model,
                    fallback_voice_resource_id=frozen.fallback_voice_resource_id,
                    learning_task_id=learning_task_id,
                )
                return learning_task_id
        store.add_voice_session(
            session_id=session_id,
            user_id=user_id,
            resource_owner_account_id=frozen.resource_owner_account_id or user_id,
            room_name=room_name,
            voice_backend=body.voice_backend,
            created_at=created_at,
            interaction_mode=frozen.interaction_mode,
            mode_policy_version=frozen.mode_policy_version,
            digital_self_version_id=frozen.digital_self_version_id,
            digital_self_manifest_sha256=frozen.manifest_sha256,
            preview_grant_id=frozen.preview_grant_id,
            self_preview_perspective=frozen.perspective,
            relationship_profile_id=frozen.relationship_profile_id,
            relationship_profile_version=frozen.relationship_profile_version,
            legacy_grant_id=frozen.legacy_grant_id,
            legacy_actor_role=frozen.legacy_actor_role,
            legacy_grantee_account_id=frozen.legacy_grantee_account_id,
            legacy_shell_id=frozen.legacy_shell_id,
            legacy_grant_snapshot_sha256=frozen.legacy_grant_snapshot_sha256,
            legacy_scope_sha256=frozen.legacy_scope_sha256,
            legacy_voice_allowed=frozen.legacy_voice_allowed,
            legacy_expires_at=frozen.legacy_expires_at,
            companion_style_id=frozen.companion_style_id,
            companion_style_version=frozen.companion_style_version,
            voice_profile_id=frozen.voice_profile_id,
            voice_profile_version=frozen.voice_profile_version,
            voice_provider=frozen.voice_provider,
            voice_model=frozen.voice_model,
            voice_resource_id=frozen.voice_resource_id,
            voice_provider_expires_at=frozen.voice_provider_expires_at,
            voice_speaker_sha256=frozen.voice_speaker_sha256,
            fallback_voice_profile_id=frozen.fallback_voice_profile_id,
            fallback_voice_provider=frozen.fallback_voice_provider,
            fallback_voice_model=frozen.fallback_voice_model,
            fallback_voice_resource_id=frozen.fallback_voice_resource_id,
            learning_task_id=None,
        )
        return None

    if body.voice_backend in OMNI_BACKENDS:
        if not settings.dashscope_api_key.get_secret_value():
            raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
        model = OMNI_MODELS[body.voice_backend]
        _omni_signaling_url(settings, model=model)
        learning_task_id = await persist_voice_session()
        turn_detection = _omni_turn_detection(settings)
        ab_profile = (
            f"{body.voice_backend}:silence={turn_detection['silence_duration_ms']}"
            f":th={turn_detection['threshold']}"
            f":pad={turn_detection['prefix_padding_ms']}"
        )
        # P1-8: fixed persona voice; optional clone id from DashScope voice product.
        clone_id = (getattr(settings, "qwen_omni_voice_clone_id", None) or "").strip()
        persona_voice = clone_id or (settings.qwen_omni_voice.strip() or "Liora Mira")
        persona_label = (
            getattr(settings, "qwen_omni_persona_label", None) or "Memoria 人设声"
        ).strip() or "Memoria 人设声"
        logger.info(
            "omni_session_ab_profile session_id=%s profile=%s model=%s voice=%s clone=%s",
            session_id,
            ab_profile,
            model,
            persona_voice,
            bool(clone_id),
        )
        return CreateOmniSessionResponse(
            session_id=session_id,
            voice_backend="qwen_omni",
            sdp_exchange_path=f"/v1/sessions/{session_id}/omni/sdp",
            config={
                "model": model,
                "voice": persona_voice,
                "transport": "webrtc",
                "persona": {
                    "label": persona_label,
                    "voice": persona_voice,
                    "cloned": bool(clone_id),
                },
                "turn_detection": turn_detection,
                "ab_profile": ab_profile,
                "ab_scan": {
                    "backends": ["qwen_omni"],
                    "silence_duration_ms_candidates": [500, 650, 800, 1000],
                    "threshold_candidates": [0.35, 0.45, 0.5, 0.55, 0.65],
                    "active": {
                        "voice_backend": body.voice_backend,
                        **turn_detection,
                    },
                },
            },
            interaction=_frozen_values(frozen),
            learning_task_id=learning_task_id,
        )

    if mini_program:
        learning_task_id = await persist_voice_session()
        return CreateMiniProgramSessionResponse(
            session_id=session_id,
            config={
                "locale": body.locale,
                "allow_text_fallback": True,
            },
            interaction=_frozen_values(frozen),
            learning_task_id=learning_task_id,
            media_gateway=build_gateway_ticket(
                settings,
                session_id=session_id,
                user_id=user_id,
                room_name=room_name,
                identity=identity,
            ),
        )

    token, ttl = mint_participant_token(
        settings,
        room_name=room_name,
        identity=identity,
        agent_name=settings.livekit_agent_name,
    )
    learning_task_id = await persist_voice_session()
    return CreateSessionResponse(
        session_id=session_id,
        livekit_url=settings.livekit_url,
        room_name=room_name,
        participant_token=token,
        expires_in=ttl,
        agent_name=settings.livekit_agent_name,
        config={
            "locale": body.locale,
            "allow_text_fallback": True,
        },
        interaction=_frozen_values(frozen),
        learning_task_id=learning_task_id,
    )


def _frozen_values(frozen: FrozenMode) -> dict[str, Any]:
    return ModePolicy.session_context(frozen)


@router.post(
    "/{session_id}/mini-program/gateway-ticket",
    response_model=MiniProgramMediaGateway,
)
async def refresh_miniprogram_gateway_ticket(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> MiniProgramMediaGateway:
    """Recover a media socket without creating a second frozen voice session."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    if rec["voice_backend"] != "cascade":
        raise HTTPException(
            status_code=409,
            detail={"code": "miniprogram_requires_cascade"},
        )
    settings = request.app.state.settings
    return build_gateway_ticket(
        settings,
        session_id=session_id,
        user_id=user.user_id,
        room_name=str(rec["room_name"]),
        identity=f"user-{user.user_id}-{session_id[:8]}",
    )


@router.post("/{session_id}/omni/sdp", response_class=Response)
async def exchange_omni_sdp(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> Response:
    """Exchange a browser WebRTC offer without exposing provider credentials."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    backend = str(rec["voice_backend"])
    if backend not in OMNI_BACKENDS:
        raise HTTPException(status_code=409, detail="session does not use Qwen Omni")

    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/sdp":
        raise HTTPException(status_code=415, detail="Content-Type must be application/sdp")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > OMNI_MAX_SDP_BYTES:
                raise HTTPException(status_code=413, detail="SDP offer is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    offer_sdp = await request.body()
    if len(offer_sdp) > OMNI_MAX_SDP_BYTES:
        raise HTTPException(status_code=413, detail="SDP offer is too large")
    if not offer_sdp.lstrip().startswith(b"v=0"):
        raise HTTPException(status_code=422, detail="invalid SDP offer")

    if not store.reserve_omni_sdp_exchange(
        session_id=session_id,
        user_id=user.user_id,
        max_exchanges=OMNI_MAX_SDP_EXCHANGES,
    ):
        raise HTTPException(status_code=429, detail="SDP exchange limit reached")
    answer_sdp = await _exchange_omni_sdp(
        request.app.state.settings,
        offer_sdp,
        model=OMNI_MODELS[backend],
    )
    return Response(
        content=answer_sdp,
        media_type="application/sdp",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{session_id}/telemetry", status_code=204)
async def publish_omni_telemetry(
    session_id: str,
    body: OmniTelemetryBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> Response:
    """Accept a small, owner-bound timeline without audio, text, or credentials."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    if str(rec["voice_backend"]) not in REALTIME_BACKENDS:
        raise HTTPException(status_code=409, detail="session does not use realtime voice")
    telemetry_logger.info(
        "omni_realtime_telemetry session_id=%s name=%s elapsed_ms=%s "
        "turn_id=%s generation_id=%s metrics=%s "
        "error_type=%s error_code=%s error_param=%s",
        session_id,
        body.name,
        body.elapsed_ms,
        body.turn_id,
        body.generation_id,
        body.metrics.model_dump(exclude_none=True) if body.metrics else {},
        body.error_type,
        body.error_code,
        body.error_param,
    )
    return Response(status_code=204)


def _omni_turn_detection(settings: Any) -> dict[str, Any]:
    """Build semantic_vad config for Omni Flash."""
    return {
        "type": "semantic_vad",
        "threshold": float(settings.qwen_omni_vad_threshold),
        "prefix_padding_ms": int(settings.qwen_omni_prefix_padding_ms),
        "silence_duration_ms": int(settings.qwen_omni_silence_duration_ms),
    }


def _omni_signaling_url(settings: Any, *, model: str = QWEN_OMNI_FLASH_MODEL) -> str:
    workspace_id = settings.dashscope_workspace_id.strip() or QWEN_OMNI_WORKSPACE_ID
    if not _WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
    if model not in OMNI_MODELS.values():
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
    return (
        f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/webrtc/realtime?model={model}"
    )


async def _exchange_omni_sdp(
    settings: Any,
    offer_sdp: bytes,
    *,
    model: str = QWEN_OMNI_FLASH_MODEL,
) -> bytes:
    api_key = settings.dashscope_api_key.get_secret_value()
    if not api_key:
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
    try:
        async with httpx.AsyncClient(
            timeout=settings.qwen_omni_sdp_timeout_s,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                _omni_signaling_url(settings, model=model),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/sdp",
                },
                content=offer_sdp,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Qwen3.5-Omni 建连超时") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Qwen3.5-Omni 建连失败") from exc

    if response.status_code == 429:
        logger.warning(
            "Qwen Omni SDP exchange rate-limited status=%s",
            response.status_code,
        )
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 暂时繁忙")
    if response.status_code < 200 or response.status_code >= 300:
        logger.warning(
            "Qwen Omni SDP exchange failed status=%s",
            response.status_code,
        )
        raise HTTPException(status_code=502, detail="Qwen3.5-Omni 建连失败")
    answer_sdp = response.content
    if not answer_sdp.lstrip().startswith(b"v=0") or len(answer_sdp) > OMNI_MAX_SDP_BYTES:
        logger.warning(
            "Qwen Omni SDP answer invalid status=%s body_bytes=%s",
            response.status_code,
            len(answer_sdp),
        )
        raise HTTPException(status_code=502, detail="Qwen3.5-Omni 返回了无效连接响应")
    return answer_sdp


async def _send_room_control(settings: Any, *, room_name: str, event: dict[str, Any]) -> None:
    """Send a server-originated reliable packet to one LiveKit room."""
    from livekit import api

    async with api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as livekit:
        await livekit.room.send_data(
            api.SendDataRequest(
                room=room_name,
                data=json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(),
                kind=api.DataPacket.Kind.RELIABLE,
                topic=CONTROL_TOPIC,
            )
        )


@router.post("/{session_id}/stop-response")
async def stop_response(
    session_id: str,
    body: StopResponseBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Same atomic cancel semantics as voice barge-in; no new user turn."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    event = {
        "type": "stop_response",
        "session_id": session_id,
        "reason": body.reason,
        "action": "atomic_cancel",
        "create_user_turn": False,
    }
    settings = request.app.state.settings
    if not settings.offline_mock:
        try:
            await _send_room_control(settings, room_name=str(rec["room_name"]), event=event)
        except Exception as exc:
            logger.warning("failed to route stop-response to LiveKit room", exc_info=True)
            raise HTTPException(status_code=502, detail="stop-response routing failed") from exc
    _STOP_REQUESTS.append(event)
    return {"ok": True, **event}


@router.post("/{session_id}/rtc-recovered")
async def rtc_recovered(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Fence off media/data queued before a successful client RTC reconnect."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    event = {
        "type": "rtc_recovered",
        "session_id": session_id,
        "action": "advance_generation",
        "create_user_turn": False,
    }
    settings = request.app.state.settings
    if not settings.offline_mock:
        try:
            await _send_room_control(settings, room_name=str(rec["room_name"]), event=event)
        except Exception as exc:
            logger.warning("failed to route rtc-recovered to LiveKit room", exc_info=True)
            raise HTTPException(status_code=502, detail="rtc recovery routing failed") from exc
    return {"ok": True, **event}


def get_stop_requests() -> list[dict[str, Any]]:
    return list(_STOP_REQUESTS)


def reset_session_state() -> None:
    _STOP_REQUESTS.clear()
