"""Authenticated archive ledger and timeline APIs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import logging
import math
import wave
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.archive.compiler_worker import MemoryCompilerWorker
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    LifeArchivePort,
    RawVoiceConsent,
    RawVoiceConsentRequiredError,
)
from services.archive.memory_domain import (
    ConflictState,
    DomainCategory,
    MemoryCatalogPort,
    MemoryCategory,
    MemoryClaimReview,
    MemoryKind,
    MemorySearchQuery,
    MemorySensitivity,
)
from services.archive.memory_write_policy import (
    EXPLICIT_MEMORY_INTENT,
    explicit_remember_content,
)
from services.archive.object_store import ObjectRef, ObjectStore
from services.archive.recall_planner import RecallPlanner
from services.common.companions import (
    DEFAULT_COMPANION_ID,
    DESIGNED_VOICE_MODEL,
    designed_voice_profile,
    designed_voice_speaker_sha256,
)
from services.common.realtime_information import current_local_time
from services.control_api.app.account_gate import (
    AccountDeletingError,
    AccountOperationGate,
    require_writable_account,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.mode_policy import FrozenMode, ModePolicy, SpeakerClass
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
    verify_password,
)
from services.control_api.app.wechat_auth import WechatAuthError, code_to_session, openid_hash
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfVersion,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RegistryPort,
    RelationshipProfileManifestEntry,
    VersionNotFoundError,
)
from services.digital_self.response_planner import PLANNER_POLICY_VERSION
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionIncompleteError,
)
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessPurpose,
    LegacyAccessSnapshot,
    LegacyAuditTarget,
    LegacyFence,
    LegacyIdempotencyConflictError,
    LegacyNotFoundError,
    LegacyRegistryPort,
)
from services.persona.domain import PersonaEnginePort, PersonaEvidence
from services.persona.rules import trusted_uncertain_profile

router = APIRouter(prefix="/v1/archive", tags=["archive"])
logger = logging.getLogger(__name__)
_LOCAL_SAFE_PLANNER_POLICY_VERSION = "local-safe-fallback-v1"
_DOUBAO_TTS_PROVIDER = "volcengine_doubao"
_DOUBAO_PERSONAL_VOICE_MODEL = "seed-icl-2.0"
MAX_RAW_VOICE_WAV_BYTES = 2 * 1024 * 1024
MAX_RAW_VOICE_BASE64_CHARS = ((MAX_RAW_VOICE_WAV_BYTES + 2) // 3) * 4
SESSION_BOUND_EVENT_TYPES = frozenset(
    {
        "speech.utterance_finalized",
        "speaker.classified",
        "assistant.playout_progressed",
        "assistant.playout_stopped",
    }
)
SERVER_OWNED_EVENT_TYPES = frozenset(
    {
        "owner.action_recorded",
        "learning.task_created",
        "learning.task_transitioned",
        "memory.claim_reviewed",
    }
)
SERVER_INTERACTION_PAYLOAD_KEYS = frozenset(
    {
        "interaction",
        "interaction_mode",
        "mode_policy_version",
        "simulated_output",
        "history_eligible",
        "owner_projection_eligible",
        "prompt_kind",
        "learning_task_id",
        "learning_task_kind",
        "memory_write_intent",
        "response_provenance",
        "tool_epoch",
    }
)


class EvidenceEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=96)
    occurred_at: datetime
    speaker_class: Literal["owner", "guest", "uncertain", "assistant", "system"]
    source: str = Field(min_length=1, max_length=96)
    payload: dict[str, Any]
    session_id: str | None = Field(default=None, max_length=128)
    turn_id: int | None = Field(default=None, ge=0)
    generation_id: int | None = Field(default=None, ge=0)
    speaker_identity_id: str | None = Field(default=None, max_length=128)
    consent_grant_id: str | None = Field(default=None, max_length=128)
    schema_version: int = Field(default=1, ge=1, le=100)
    supersedes_event_id: str | None = Field(default=None, max_length=128)

    @field_validator("payload")
    @classmethod
    def limit_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return _limit_payload(payload)


class SessionEvidenceEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    event_type: Literal[
        "speech.utterance_finalized",
        "speaker.classified",
        "assistant.playout_progressed",
        "assistant.playout_stopped",
    ]
    occurred_at: datetime
    speaker_class: Literal["owner", "guest", "uncertain", "assistant"]
    source: str = Field(min_length=1, max_length=96)
    payload: dict[str, Any]
    turn_id: int | None = Field(default=None, ge=0)
    generation_id: int | None = Field(default=None, ge=0)
    tool_epoch: int | None = Field(default=None, ge=0)
    speaker_identity_id: str | None = Field(default=None, max_length=128)
    consent_grant_id: str | None = Field(default=None, max_length=128)
    schema_version: int = Field(default=1, ge=1, le=100)
    supersedes_event_id: str | None = Field(default=None, max_length=128)

    @field_validator("payload")
    @classmethod
    def limit_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return _limit_payload(payload)

    @model_validator(mode="after")
    def enforce_playout_evidence(self) -> SessionEvidenceEventCreate:
        if self.event_type.startswith("assistant."):
            if self.speaker_class != "assistant" or self.payload.get("actual_heard") is not True:
                raise ValueError("assistant archive events require actual-heard evidence")
            if self.turn_id is None or self.generation_id is None:
                raise ValueError("assistant archive events require turn_id and generation_id")
            if self.payload.get("response_provenance") is not None and self.tool_epoch is None:
                raise ValueError("response provenance requires the complete generation fence")
        elif self.speaker_class == "assistant":
            raise ValueError("assistant speaker_class is only valid for assistant events")
        return self


class RawVoiceConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    policy_version: str = Field(
        default="raw-voice-archive-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    retention_policy: Literal["account_lifetime"] = "account_lifetime"


class SessionRawAudioCreate(SessionEvidenceEventCreate):
    event_type: Literal["speech.utterance_finalized"]
    speaker_class: Literal["owner", "guest", "uncertain"]
    audio_base64: str = Field(min_length=1, max_length=MAX_RAW_VOICE_BASE64_CHARS)
    media_type: Literal["audio/wav"] = "audio/wav"
    retention_policy: Literal["account_lifetime"] = "account_lifetime"


class SessionMemoryContextCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    speaker_class: Literal["owner", "guest", "uncertain"]
    topic: str = Field(default="", max_length=1000)
    limit: int = Field(default=8, ge=1, le=20)


class ResponseSourceRefCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal[
        "memory_claim",
        "persona_trait",
        "cognitive_claim",
        "decision_case",
        "relationship_profile",
    ]
    item_id: str = Field(min_length=1, max_length=128)
    source_event_ids: list[str] = Field(min_length=1, max_length=32)

    @field_validator("source_event_ids")
    @classmethod
    def validate_source_event_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 128 for value in values):
            raise ValueError("response provenance source ids are invalid")
        if len(values) != len(set(values)):
            raise ValueError("response provenance source ids must be unique")
        return values


class ResponseGenerationFenceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    turn_id: int = Field(ge=0, le=2**31 - 1)
    generation_id: int = Field(ge=0, le=2**31 - 1)
    tool_epoch: int = Field(ge=0, le=2**31 - 1)


class ResponseProvenanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fence: ResponseGenerationFenceCreate
    planner_policy_version: str = Field(min_length=1, max_length=64)
    interaction_mode: Literal["companion", "self_preview", "legacy", "archive"]
    mode_policy_version: str = Field(min_length=1, max_length=128)
    digital_self_version_id: str | None = Field(default=None, max_length=128)
    manifest_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    relationship_profile_id: str | None = Field(default=None, max_length=128)
    relationship_profile_version: int | None = Field(default=None, ge=1)
    actor_account_id: str | None = Field(default=None, max_length=128)
    resource_owner_account_id: str | None = Field(default=None, max_length=128)
    legacy_actor_role: Literal["owner_preview", "grantee"] | None = None
    legacy_grantee_account_id: str | None = Field(default=None, max_length=128)
    legacy_grant_id: str | None = Field(default=None, max_length=128)
    legacy_grant_snapshot_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    legacy_scope_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    legacy_shell_id: str | None = Field(default=None, max_length=128)
    legacy_voice_allowed: bool | None = None
    legacy_expires_at: str | None = Field(default=None, min_length=1, max_length=64)
    speaker_class: Literal["owner", "guest", "uncertain"]
    speaker_reason_code: str = Field(min_length=1, max_length=96)
    speaker_profile_id: str | None = Field(default=None, max_length=128)
    speaker_model_version: str = Field(min_length=1, max_length=128)
    speaker_template_version: int | None = Field(default=None, ge=1)
    persona_version_id: str | None = Field(default=None, max_length=128)
    persona_version_number: int | None = Field(default=None, ge=1)
    persona_style_only: bool = False
    source_refs: list[ResponseSourceRefCreate] = Field(default_factory=list, max_length=32)
    epistemic_status: Literal["not_applicable", "fact", "inference", "unknown", "mixed"]
    epistemic_reason_codes: list[str] = Field(default_factory=list, max_length=16)
    disclosures: list[Literal["digital_identity", "inference", "unknown", "privacy_refusal"]] = (
        Field(default_factory=list, max_length=4)
    )
    llm_provider: str | None = Field(default=None, max_length=64)
    llm_model: str | None = Field(default=None, max_length=128)
    tts_provider: str | None = Field(default=None, max_length=64)
    tts_model: str | None = Field(default=None, max_length=128)
    actual_voice_profile_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    actual_voice_profile_version: int | None = Field(default=None, ge=1)
    actual_voice_resource_id: Literal["seed-tts-2.0", "seed-icl-2.0"] | None = None
    actual_voice_provider_expires_at: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )
    actual_voice_speaker_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("epistemic_reason_codes")
    @classmethod
    def validate_reason_codes(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 64 for value in values):
            raise ValueError("response provenance reason codes are invalid")
        if len(values) != len(set(values)):
            raise ValueError("response provenance reason codes must be unique")
        return values

    @model_validator(mode="after")
    def validate_persona_snapshot(self) -> ResponseProvenanceCreate:
        legacy_values = (
            self.actor_account_id,
            self.resource_owner_account_id,
            self.legacy_actor_role,
            self.legacy_grantee_account_id,
            self.legacy_grant_id,
            self.legacy_grant_snapshot_sha256,
            self.legacy_scope_sha256,
            self.legacy_shell_id,
            self.legacy_voice_allowed,
            self.legacy_expires_at,
        )
        if self.interaction_mode != "legacy" and any(value is not None for value in legacy_values):
            raise ValueError("Legacy provenance is forbidden outside Legacy mode")
        if bool(self.persona_version_id) != (self.persona_version_number is not None):
            raise ValueError("persona provenance version fields must be paired")
        if self.persona_style_only and self.persona_version_id is None:
            raise ValueError("persona style-only provenance requires a persona snapshot")
        has_tts = self.tts_provider is not None or self.tts_model is not None
        if has_tts and (self.tts_provider is None or self.tts_model is None):
            raise ValueError("tts provenance fields must be paired")
        has_actual_voice = any(
            value is not None
            for value in (
                self.actual_voice_profile_id,
                self.actual_voice_profile_version,
                self.actual_voice_resource_id,
                self.actual_voice_provider_expires_at,
                self.actual_voice_speaker_sha256,
            )
        )
        if has_tts != has_actual_voice:
            raise ValueError("tts provenance requires an actual voice snapshot")
        if has_actual_voice and (
            self.actual_voice_resource_id is None or self.actual_voice_speaker_sha256 is None
        ):
            raise ValueError("actual voice resource and digest are required")
        if self.actual_voice_resource_id == _DOUBAO_PERSONAL_VOICE_MODEL and (
            self.actual_voice_profile_id is None
            or self.actual_voice_profile_version is None
            or self.actual_voice_provider_expires_at is None
        ):
            raise ValueError("personal voice provenance requires version and expiry")
        if self.actual_voice_resource_id == DESIGNED_VOICE_MODEL and (
            self.actual_voice_profile_version is not None
            or self.actual_voice_provider_expires_at is not None
        ):
            raise ValueError("designed voice provenance cannot claim personal metadata")
        if has_actual_voice and (
            self.tts_provider != _DOUBAO_TTS_PROVIDER
            or self.tts_model != self.actual_voice_resource_id
        ):
            raise ValueError("actual voice snapshot does not match the tts runtime")
        return self


def _limit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    import json

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError("payload must not exceed 64 KiB")
    return payload


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _archive_objects(request: Request) -> ObjectStore:
    return cast(ObjectStore, request.app.state.archive_object_store)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _catalog(request: Request) -> MemoryCatalogPort:
    return cast(MemoryCatalogPort, request.app.state.memory_catalog)


def _digital_self_registry(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


def _legacy_registry(request: Request) -> LegacyRegistryPort:
    return cast(LegacyRegistryPort, request.app.state.legacy_registry)


def _legacy_snapshot_matches_session(
    access: LegacyAccessSnapshot,
    frozen: FrozenMode,
) -> bool:
    return bool(
        access.actor_role == frozen.legacy_actor_role
        and access.resource_owner_account_id == frozen.resource_owner_account_id
        and access.grantee_account_id == frozen.legacy_grantee_account_id
        and access.grant_id == frozen.legacy_grant_id
        and access.shell_id == frozen.legacy_shell_id
        and access.version_id == frozen.digital_self_version_id
        and access.manifest_sha256 == frozen.manifest_sha256
        and access.grant_snapshot_sha256 == frozen.legacy_grant_snapshot_sha256
        and access.scope_sha256 == frozen.legacy_scope_sha256
        and access.relationship_profile_id == frozen.relationship_profile_id
        and access.relationship_profile_version == frozen.relationship_profile_version
        and access.voice_allowed is frozen.legacy_voice_allowed
        and access.expires_at.isoformat() == frozen.legacy_expires_at
    )


async def _resolve_legacy_access(
    request: Request,
    *,
    session: Mapping[str, Any],
    now: datetime,
) -> LegacyAccessSnapshot:
    frozen = FrozenMode.from_session(session)
    if ModePolicy.availability(frozen).status != "available" or not frozen.legacy_grant_id:
        raise HTTPException(status_code=409, detail={"code": "legacy_access_unavailable"})
    purpose: LegacyAccessPurpose = (
        "owner_preview" if frozen.legacy_actor_role == "owner_preview" else "grantee_session"
    )
    try:
        access = await _legacy_registry(request).resolve_access(
            actor_account_id=str(session["user_id"]),
            grant_id=frozen.legacy_grant_id,
            purpose=purpose,
            now=now,
        )
    except (LegacyAccessDeniedError, LegacyNotFoundError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "legacy_access_unavailable"},
        ) from exc
    if not _legacy_snapshot_matches_session(access, frozen):
        raise HTTPException(status_code=409, detail={"code": "legacy_access_stale"})
    return access


def _legacy_provenance_matches(
    submitted: ResponseProvenanceCreate,
    access: LegacyAccessSnapshot,
    *,
    actor_account_id: str,
) -> bool:
    return bool(
        submitted.interaction_mode == "legacy"
        and submitted.digital_self_version_id == access.version_id
        and submitted.manifest_sha256 == access.manifest_sha256
        and submitted.relationship_profile_id == access.relationship_profile_id
        and submitted.actor_account_id == actor_account_id
        and submitted.resource_owner_account_id == access.resource_owner_account_id
        and submitted.legacy_actor_role == access.actor_role
        and submitted.legacy_grantee_account_id == access.grantee_account_id
        and submitted.legacy_grant_id == access.grant_id
        and submitted.legacy_grant_snapshot_sha256 == access.grant_snapshot_sha256
        and submitted.legacy_scope_sha256 == access.scope_sha256
        and submitted.legacy_shell_id == access.shell_id
        and submitted.legacy_voice_allowed is access.voice_allowed
        and submitted.legacy_expires_at == access.expires_at.isoformat()
        and submitted.relationship_profile_version == access.relationship_profile_version
    )


def _manifest_source_refs(
    version: DigitalSelfVersion,
) -> dict[tuple[str, str], frozenset[str]]:
    refs: dict[tuple[str, str], frozenset[str]] = {}
    for entry in version.manifest.entries:
        if isinstance(entry, MemoryClaimManifestEntry):
            refs[("memory_claim", entry.claim_id)] = frozenset({entry.source_event_id})
        elif isinstance(entry, PersonaTraitManifestEntry):
            refs[("persona_trait", entry.trait_id)] = frozenset(entry.source_event_ids)
        elif isinstance(entry, CognitiveClaimManifestEntry):
            refs[("cognitive_claim", entry.claim_id)] = frozenset(
                (
                    *entry.support_source_event_ids,
                    *entry.counterexample_source_event_ids,
                )
            )
        elif isinstance(entry, DecisionCaseManifestEntry):
            refs[("decision_case", entry.case_id)] = frozenset(
                (
                    *entry.support_source_event_ids,
                    *entry.counterexample_source_event_ids,
                )
            )
        elif isinstance(entry, RelationshipProfileManifestEntry):
            refs[("relationship_profile", entry.profile_id)] = frozenset(
                (
                    *entry.support_source_event_ids,
                    *entry.counterexample_source_event_ids,
                )
            )
    return refs


def _canonical_epistemic_provenance(
    *,
    source_refs: list[ResponseSourceRefCreate],
    interaction_mode: str,
    parent: EvidenceEvent,
    submitted_disclosures: list[str],
    persona_style_only: bool,
) -> tuple[str, list[str], list[str]]:
    """Derive claim certainty from verified source kinds, never caller labels."""

    if persona_style_only:
        return "not_applicable", ["persona_style_only"], []
    kinds = {ref.kind for ref in source_refs}
    if "decision_case" in kinds:
        status, reason_codes, disclosures = (
            "inference",
            ["decision_precedent"],
            ["inference"],
        )
    elif kinds & {"memory_claim", "cognitive_claim"}:
        status, reason_codes, disclosures = "fact", ["grounded_manifest"], []
    elif interaction_mode == "companion":
        status, reason_codes, disclosures = "not_applicable", ["no_grounded_items"], []
    else:
        status, reason_codes, disclosures = "unknown", ["no_approved_source"], ["unknown"]

    if interaction_mode in {"self_preview", "legacy"}:
        disclosures.insert(0, "digital_identity")
    if not source_refs and "privacy_refusal" in submitted_disclosures:
        disclosures.append("privacy_refusal")
    return status, reason_codes, list(dict.fromkeys(disclosures))


def _canonical_actual_voice(
    *,
    submitted: ResponseProvenanceCreate,
    session: Mapping[str, Any],
    interaction_mode: str,
) -> tuple[str | None, int | None, str | None, str | None, str | None]:
    """Validate the applied voice against the exact session-frozen contract."""

    resource_id = submitted.actual_voice_resource_id
    speaker_sha256 = submitted.actual_voice_speaker_sha256
    profile_id = submitted.actual_voice_profile_id
    profile_version = submitted.actual_voice_profile_version
    provider_expires_at = submitted.actual_voice_provider_expires_at
    if all(
        value is None
        for value in (
            resource_id,
            speaker_sha256,
            profile_id,
            profile_version,
            provider_expires_at,
        )
    ):
        return None, None, None, None, None
    frozen = FrozenMode.from_session(session)
    valid = False
    if interaction_mode == "companion":
        expected_profile = designed_voice_profile(frozen.companion_style_id or DEFAULT_COMPANION_ID)
        valid = (
            expected_profile is not None
            and profile_id == expected_profile
            and submitted.tts_provider == _DOUBAO_TTS_PROVIDER
            and resource_id == DESIGNED_VOICE_MODEL
            and speaker_sha256 == designed_voice_speaker_sha256(expected_profile)
        )
    elif interaction_mode in {"self_preview", "legacy"}:
        personal = (
            (interaction_mode != "legacy" or frozen.legacy_voice_allowed is True)
            and frozen.voice_profile_id is not None
            and profile_id == frozen.voice_profile_id
            and frozen.voice_profile_version is not None
            and profile_version == frozen.voice_profile_version
            and frozen.voice_provider is not None
            and submitted.tts_provider == frozen.voice_provider
            and frozen.voice_model is not None
            and submitted.tts_model == frozen.voice_model
            and frozen.voice_resource_id is not None
            and resource_id == frozen.voice_resource_id
            and frozen.voice_provider_expires_at is not None
            and provider_expires_at == frozen.voice_provider_expires_at
            and frozen.voice_speaker_sha256 is not None
            and speaker_sha256 == frozen.voice_speaker_sha256
        )
        safe_baseline = (
            frozen.fallback_voice_profile_id is not None
            and profile_id == frozen.fallback_voice_profile_id
            and frozen.fallback_voice_provider is not None
            and submitted.tts_provider == frozen.fallback_voice_provider
            and frozen.fallback_voice_model is not None
            and submitted.tts_model == frozen.fallback_voice_model
            and frozen.fallback_voice_resource_id is not None
            and resource_id == frozen.fallback_voice_resource_id
            and speaker_sha256 == designed_voice_speaker_sha256(frozen.fallback_voice_profile_id)
        )
        valid = personal or safe_baseline
    elif interaction_mode == "archive":
        valid = profile_id is None and resource_id == DESIGNED_VOICE_MODEL
    if not valid:
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_voice_mismatch"},
        )
    return profile_id, profile_version, resource_id, provider_expires_at, speaker_sha256


async def _canonical_response_provenance(
    request: Request,
    *,
    raw: object,
    session: Mapping[str, Any],
    parent: EvidenceEvent | None,
    tool_epoch: int,
    legacy_access: LegacyAccessSnapshot | None = None,
) -> dict[str, Any]:
    try:
        submitted = ResponseProvenanceCreate.model_validate(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "response_provenance_invalid"},
        ) from exc
    interaction_mode = str(session["interaction_mode"])
    account_id = (
        legacy_access.resource_owner_account_id
        if legacy_access is not None
        else str(session["user_id"])
    )
    source_refs = submitted.source_refs
    local_safe_plan = submitted.planner_policy_version == _LOCAL_SAFE_PLANNER_POLICY_VERSION
    if submitted.planner_policy_version not in {
        PLANNER_POLICY_VERSION,
        _LOCAL_SAFE_PLANNER_POLICY_VERSION,
    }:
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_planner_invalid"},
        )
    if local_safe_plan and (
        source_refs or submitted.persona_version_id is not None or submitted.persona_style_only
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_planner_invalid"},
        )
    if (
        submitted.fence.session_id != str(session["session_id"])
        or (parent is not None and submitted.fence.turn_id != parent.turn_id)
        or (parent is not None and submitted.fence.generation_id != parent.generation_id)
        or submitted.fence.tool_epoch != tool_epoch
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_fence_mismatch"},
        )
    if legacy_access is not None and not _legacy_provenance_matches(
        submitted,
        legacy_access,
        actor_account_id=str(session["user_id"]),
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_legacy_mismatch"},
        )
    if legacy_access is not None and submitted.mode_policy_version != str(
        session["mode_policy_version"]
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_legacy_mismatch"},
        )
    legacy_fields = (
        submitted.actor_account_id,
        submitted.resource_owner_account_id,
        submitted.legacy_actor_role,
        submitted.legacy_grantee_account_id,
        submitted.legacy_grant_id,
        submitted.legacy_grant_snapshot_sha256,
        submitted.legacy_scope_sha256,
        submitted.legacy_shell_id,
        submitted.legacy_voice_allowed,
        submitted.legacy_expires_at,
    )
    if legacy_access is None and any(value is not None for value in legacy_fields):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_legacy_mismatch"},
        )
    shadow_owner_candidate = bool(
        parent is not None
        and parent.speaker_class == "uncertain"
        and parent.payload.get("speaker_reason_code") == "shadow_owner_candidate"
    )
    if submitted.persona_style_only and (
        not shadow_owner_candidate or source_refs or str(session["interaction_mode"]) != "companion"
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_private_source_denied"},
        )
    if (
        submitted.persona_version_id is not None
        and not submitted.persona_style_only
        and (
            parent is None
            or parent.speaker_class != "owner"
            or str(session["interaction_mode"]) != "companion"
        )
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_private_source_denied"},
        )
    if (
        legacy_access is None
        and (parent is None or parent.speaker_class != "owner")
        and source_refs
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_private_source_denied"},
        )
    version_id = session.get("digital_self_version_id")
    manifest_sha256: str | None = None
    allowed_manifest_refs: dict[tuple[str, str], frozenset[str]] | None = None
    if isinstance(version_id, str) and version_id:
        try:
            version = await _digital_self_registry(request).get(
                account_id=account_id,
                version_id=version_id,
            )
        except VersionNotFoundError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "response_provenance_version_unavailable"},
            ) from exc
        if (
            interaction_mode == "self_preview" and version.status not in {"approved", "frozen"}
        ) or (interaction_mode == "legacy" and version.status != "frozen"):
            raise HTTPException(
                status_code=409,
                detail={"code": "response_provenance_version_unavailable"},
            )
        manifest_sha256 = version.manifest_sha256
        allowed_manifest_refs = _manifest_source_refs(version)
        if legacy_access is not None and (
            version.account_id != legacy_access.resource_owner_account_id
            or version.version_id != legacy_access.version_id
            or version.version_number != legacy_access.version_number
            or version.manifest_sha256 != legacy_access.manifest_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "response_provenance_version_unavailable"},
            )
    elif submitted.manifest_sha256 is not None or interaction_mode in {"self_preview", "legacy"}:
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_version_unavailable"},
        )
    elif interaction_mode == "companion" and version_id is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_version_unavailable"},
        )
    relationship_profile_id = (
        str(session["relationship_profile_id"])
        if isinstance(session.get("relationship_profile_id"), str)
        else None
    )
    relationship_profile_version: int | None = None
    if relationship_profile_id is not None:
        if allowed_manifest_refs is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "response_provenance_relationship_unavailable"},
            )
        relationship_entry = next(
            (
                entry
                for entry in version.manifest.entries
                if isinstance(entry, RelationshipProfileManifestEntry)
                and entry.profile_id == relationship_profile_id
            ),
            None,
        )
        if relationship_entry is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "response_provenance_relationship_unavailable"},
            )
        relationship_profile_version = relationship_entry.version_number
        if legacy_access is not None and (
            relationship_profile_id != legacy_access.relationship_profile_id
            or relationship_profile_version != legacy_access.relationship_profile_version
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "response_provenance_relationship_unavailable"},
            )
    source_event_ids = tuple(
        dict.fromkeys(
            source_event_id for ref in source_refs for source_event_id in ref.source_event_ids
        )
    )
    if allowed_manifest_refs is not None:
        allowed_legacy_items = (
            {(item.kind, item.item_id) for item in legacy_access.allowed_items}
            if legacy_access is not None
            else None
        )
        for ref in source_refs:
            allowed = allowed_manifest_refs.get((ref.kind, ref.item_id))
            if (
                allowed is None
                or not set(ref.source_event_ids).issubset(allowed)
                or (
                    allowed_legacy_items is not None
                    and (ref.kind, ref.item_id) not in allowed_legacy_items
                )
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "response_provenance_source_invalid"},
                )
    events = await asyncio.gather(
        *(
            _archive(request).event(
                account_id=account_id,
                event_id=source_event_id,
            )
            for source_event_id in source_event_ids
        )
    )
    if any(event is None for event in events):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_source_invalid"},
        )
    resolved_events = tuple(cast(EvidenceEvent, event) for event in events)
    if any(
        event.speaker_class != "owner"
        or event.event_type not in {"speech.utterance_finalized", "owner.action_recorded"}
        or event.payload.get("owner_projection_eligible") is not True
        for event in resolved_events
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_source_invalid"},
        )
    parent_payload = parent.payload if parent is not None else {}
    reason_code = parent_payload.get("speaker_reason_code")
    model_version = parent_payload.get("speaker_model_version")
    profile_id = parent_payload.get("speaker_profile_id")
    template_version = parent_payload.get("speaker_template_version")
    epistemic_status, epistemic_reason_codes, disclosures = _canonical_epistemic_provenance(
        source_refs=source_refs,
        interaction_mode=interaction_mode,
        parent=cast(EvidenceEvent, parent),
        submitted_disclosures=list(submitted.disclosures),
        persona_style_only=submitted.persona_style_only,
    )
    (
        actual_voice_profile_id,
        actual_voice_profile_version,
        actual_voice_resource_id,
        actual_voice_provider_expires_at,
        actual_voice_speaker_sha256,
    ) = _canonical_actual_voice(
        submitted=submitted,
        session=session,
        interaction_mode=interaction_mode,
    )
    canonical = {
        "fence": submitted.fence.model_dump(),
        "planner_policy_version": (
            _LOCAL_SAFE_PLANNER_POLICY_VERSION if local_safe_plan else PLANNER_POLICY_VERSION
        ),
        "interaction_mode": interaction_mode,
        "mode_policy_version": str(session["mode_policy_version"]),
        "digital_self_version_id": version_id if isinstance(version_id, str) else None,
        "manifest_sha256": manifest_sha256,
        "relationship_profile_id": relationship_profile_id,
        "relationship_profile_version": relationship_profile_version,
        "speaker_class": parent.speaker_class if parent is not None else "owner",
        "speaker_reason_code": (
            reason_code if isinstance(reason_code, str) and reason_code else "unavailable"
        ),
        "speaker_profile_id": profile_id if isinstance(profile_id, str) else None,
        "speaker_model_version": (
            model_version if isinstance(model_version, str) and model_version else "unavailable"
        ),
        "speaker_template_version": (
            template_version if isinstance(template_version, int) and template_version > 0 else None
        ),
        "persona_version_id": submitted.persona_version_id,
        "persona_version_number": submitted.persona_version_number,
        "persona_style_only": submitted.persona_style_only,
        "source_refs": [ref.model_dump() for ref in source_refs],
        "epistemic_status": epistemic_status,
        "epistemic_reason_codes": epistemic_reason_codes,
        "disclosures": disclosures,
        "llm_provider": submitted.llm_provider,
        "llm_model": submitted.llm_model,
        "tts_provider": submitted.tts_provider,
        "tts_model": actual_voice_resource_id,
        "actual_voice_profile_id": actual_voice_profile_id,
        "actual_voice_profile_version": actual_voice_profile_version,
        "actual_voice_resource_id": actual_voice_resource_id,
        "actual_voice_provider_expires_at": actual_voice_provider_expires_at,
        "actual_voice_speaker_sha256": actual_voice_speaker_sha256,
    }
    if legacy_access is not None:
        canonical.update(
            {
                "actor_account_id": str(session["user_id"]),
                "resource_owner_account_id": legacy_access.resource_owner_account_id,
                "legacy_actor_role": legacy_access.actor_role,
                "legacy_grantee_account_id": legacy_access.grantee_account_id,
                "legacy_grant_id": legacy_access.grant_id,
                "legacy_grant_snapshot_sha256": legacy_access.grant_snapshot_sha256,
                "legacy_scope_sha256": legacy_access.scope_sha256,
                "legacy_shell_id": legacy_access.shell_id,
                "legacy_voice_allowed": legacy_access.voice_allowed,
                "legacy_expires_at": legacy_access.expires_at.isoformat(),
            }
        )
    return canonical


def _governance(request: Request) -> AccountDataGovernance:
    return cast(AccountDataGovernance, request.app.state.account_data_governance)


def _ensure_account_writable(request: Request, account_id: str) -> None:
    if _store(request).is_account_unavailable(user_id=account_id):
        raise HTTPException(status_code=409, detail="account deletion is in progress")


def _require_registered(request: Request, user: AuthenticatedUser) -> None:
    if _store(request).get_account(user_id=user.user_id) is None:
        raise HTTPException(status_code=403, detail="register an account before raw voice archive")


@asynccontextmanager
async def _account_write(request: Request, account_id: str) -> AsyncIterator[None]:
    gate = cast(AccountOperationGate, request.app.state.account_operations)
    try:
        async with gate.write(account_id):
            _ensure_account_writable(request, account_id)
            yield
    except AccountDeletingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _wake_compiler(request: Request) -> None:
    worker = getattr(request.app.state, "memory_compiler_worker", None)
    if isinstance(worker, MemoryCompilerWorker):
        worker.wake()


def _consent_payload(consent: RawVoiceConsent) -> dict[str, Any]:
    return {
        "consent_grant_id": consent.consent_grant_id,
        "policy_version": consent.policy_version,
        "retention_policy": consent.retention_policy,
        "granted_at": consent.granted_at.isoformat(),
        "revoked_at": consent.revoked_at.isoformat() if consent.revoked_at else None,
    }


def _decode_owner_wav(encoded: str) -> bytes:
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="audio_base64 must be valid base64") from exc
    if not audio or len(audio) > MAX_RAW_VOICE_WAV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"raw voice WAV must not exceed {MAX_RAW_VOICE_WAV_BYTES} bytes",
        )
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            frame_count = reader.getnframes()
            frame_bytes = reader.readframes(frame_count)
            valid = (
                reader.getnchannels() == 1
                and reader.getsampwidth() == 2
                and reader.getframerate() == 16_000
                and reader.getcomptype() == "NONE"
                and frame_count > 0
                and len(frame_bytes) == frame_count * 2
            )
    except (EOFError, wave.Error) as exc:
        raise HTTPException(status_code=422, detail="raw voice must be a valid WAV") from exc
    if not valid:
        raise HTTPException(
            status_code=422,
            detail="raw voice WAV must be mono PCM16 at 16000 Hz",
        )
    return audio


async def _put_archive_object(
    store: ObjectStore,
    *,
    account_id: str,
    data: bytes,
) -> ObjectRef:
    task = asyncio.create_task(
        store.put(
            account_id=account_id,
            purpose="raw-voice-archive",
            data=data,
            media_type="audio/wav",
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        reference = await asyncio.shield(task)
        await asyncio.shield(store.delete(reference))
        raise


async def _delete_object_safely(store: ObjectStore, reference: ObjectRef) -> None:
    try:
        await asyncio.shield(store.delete(reference))
    except Exception:
        logger.exception("raw voice object compensation failed object_key=%s", reference.object_key)
        raise


async def _observe_persona(
    request: Request,
    engine: PersonaEnginePort,
    *,
    account_id: str,
    source_event_id: str,
    speech_duration_ms: int | None,
    pause_ratio: float | None,
    quality_score: float | None,
) -> None:
    try:
        async with _account_write(request, account_id):
            allowed = await engine.learning_allowed(account_id=account_id)
            await engine.observe(
                PersonaEvidence(
                    account_id=account_id,
                    source_event_id=source_event_id,
                    learning_allowed=allowed,
                    speech_duration_ms=speech_duration_ms,
                    pause_ratio=pause_ratio,
                    quality_score=quality_score,
                )
            )
    except Exception:
        logger.exception("persona observation failed source_event_id=%s", source_event_id)


def _bounded_metric(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    metric = float(value)
    if not math.isfinite(metric) or not minimum <= metric <= maximum:
        return None
    return metric


def _persona_metrics(
    payload: Mapping[str, Any],
) -> tuple[int | None, float | None, float | None] | None:
    speech_ms = _bounded_metric(payload, "speech_ms", minimum=1, maximum=600_000)
    pause_ratio = _bounded_metric(payload, "pause_ratio", minimum=0, maximum=1)
    quality_score = _bounded_metric(payload, "quality_score", minimum=0, maximum=1)
    if any(
        key in payload and metric is None
        for key, metric in (
            ("speech_ms", speech_ms),
            ("pause_ratio", pause_ratio),
            ("quality_score", quality_score),
        )
    ):
        return None
    return (
        int(speech_ms) if speech_ms is not None else None,
        pause_ratio,
        quality_score,
    )


def _schedule_persona_observation(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    event: EvidenceEvent,
    duplicate: bool,
    allow_uncertain_candidate: bool = False,
) -> None:
    if duplicate or event.event_type != "speech.utterance_finalized":
        return
    if event.payload.get("persona_eligible") is not True:
        return
    if event.speaker_class == "uncertain":
        if (
            not allow_uncertain_candidate
            or _store(request).get_account(user_id=event.account_id) is None
            or trusted_uncertain_profile(event.payload) is None
        ):
            return
    elif event.speaker_class != "owner":
        return
    engine = cast(PersonaEnginePort, request.app.state.persona_engine)
    metrics = _persona_metrics(event.payload)
    if metrics is None:
        return
    speech_duration_ms, pause_ratio, quality_score = metrics
    background_tasks.add_task(
        _observe_persona,
        request,
        engine,
        account_id=event.account_id,
        source_event_id=event.event_id,
        speech_duration_ms=speech_duration_ms,
        pause_ratio=pause_ratio,
        quality_score=quality_score,
    )


def _schedule_low_sensitivity_persona_observation(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    event: EvidenceEvent,
    duplicate: bool,
) -> None:
    """Keep shadow candidates separate from owner-history learning."""

    if (
        event.speaker_class != "uncertain"
        or event.payload.get("speaker_reason_code") != "shadow_owner_candidate"
        or event.payload.get("history_eligible") is not False
        or event.payload.get("owner_projection_eligible") is not False
    ):
        return
    _schedule_persona_observation(
        request,
        background_tasks,
        event=event,
        duplicate=duplicate,
        allow_uncertain_candidate=True,
    )


def _require_internal_token(
    request: Request,
    capability: Literal["archive_write", "memory_read"],
    token: str | None,
) -> None:
    settings = cast(ControlSettings, request.app.state.settings)
    expected = settings.internal_token(capability)
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal capability token required")


def _require_archive_write_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    _require_internal_token(request, "archive_write", token)


async def _append_legacy_shell_event(
    request: Request,
    *,
    body: SessionEvidenceEventCreate,
    access: LegacyAccessSnapshot,
    tool_epoch: int | None,
    response_provenance: Mapping[str, Any] | None,
) -> JSONResponse:
    shell_role: Literal["grantee", "digital_self"] | None = None
    if access.actor_role == "grantee" and body.event_type == "speech.utterance_finalized":
        if body.speaker_class == "owner":
            shell_role = "grantee"
    elif access.actor_role == "grantee" and body.event_type == "assistant.playout_stopped":
        shell_role = "digital_self"
    if shell_role is None:
        return JSONResponse(
            status_code=200,
            content={"event_id": body.event_id, "shell_turn_id": None, "isolated": True},
        )
    text = body.payload.get("text")
    if (
        not isinstance(text, str)
        or not text.strip()
        or body.turn_id is None
        or body.generation_id is None
        or tool_epoch is None
        or access.shell_id is None
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "legacy_shell_fence_invalid"},
        )
    idempotency_key = "archive:" + hashlib.sha256(body.event_id.encode()).hexdigest()
    try:
        turn = await _legacy_registry(request).append_shell_turn(
            actor_account_id=access.grantee_account_id,
            shell_id=access.shell_id,
            actor_role=shell_role,
            actual_heard_text=text.strip(),
            fence=LegacyFence(
                session_id=body.session_id,
                turn_id=str(body.turn_id),
                generation_id=str(body.generation_id),
                tool_epoch=tool_epoch,
            ),
            idempotency_key=idempotency_key,
            now=body.occurred_at.astimezone(UTC),
        )
    except (LegacyAccessDeniedError, LegacyNotFoundError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "legacy_access_unavailable"},
        ) from exc
    except LegacyIdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "legacy_shell_idempotency_conflict"},
        ) from exc
    if shell_role == "digital_self":
        disclosures = (
            response_provenance.get("disclosures") if response_provenance is not None else None
        )
        if not isinstance(disclosures, list):
            disclosures = []
        try:
            registry = _legacy_registry(request)
            fence = LegacyFence(
                session_id=body.session_id,
                turn_id=str(body.turn_id),
                generation_id=str(body.generation_id),
                tool_epoch=tool_epoch,
            )
            now = body.occurred_at.astimezone(UTC)
            actual_voice_profile_id = (
                response_provenance.get("actual_voice_profile_id")
                if response_provenance is not None
                else None
            )
            if isinstance(actual_voice_profile_id, str) and actual_voice_profile_id:
                await registry.append_runtime_audit(
                    actor_account_id=access.grantee_account_id,
                    grant_id=access.grant_id,
                    action="select_voice",
                    decision="allowed",
                    reason="voice_selected",
                    fence=fence,
                    target=LegacyAuditTarget("voice_profile", actual_voice_profile_id),
                    now=now,
                )
            if "privacy_refusal" in disclosures:
                await registry.append_runtime_audit(
                    actor_account_id=access.grantee_account_id,
                    grant_id=access.grant_id,
                    action="refuse",
                    decision="denied",
                    reason="privacy_refusal",
                    fence=fence,
                    target=None,
                    now=now,
                )
            elif "unknown" in disclosures:
                await registry.append_runtime_audit(
                    actor_account_id=access.grantee_account_id,
                    grant_id=access.grant_id,
                    action="refuse",
                    decision="denied",
                    reason="unknown_refusal",
                    fence=fence,
                    target=None,
                    now=now,
                )
            else:
                await registry.append_runtime_audit(
                    actor_account_id=access.grantee_account_id,
                    grant_id=access.grant_id,
                    action="plan_answer",
                    decision="allowed",
                    reason="answer_planned",
                    fence=fence,
                    target=None,
                    now=now,
                )
        except (LegacyAccessDeniedError, LegacyNotFoundError) as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "legacy_access_unavailable"},
            ) from exc
    return JSONResponse(
        status_code=201,
        content={
            "event_id": body.event_id,
            "shell_turn_id": turn.shell_turn_id,
            "isolated": True,
        },
    )


def _canonical_turn_eligibility(event: EvidenceEvent) -> tuple[bool, bool] | None:
    interaction = event.payload.get("interaction")
    if not isinstance(interaction, Mapping):
        return None
    history = event.payload.get("history_eligible")
    owner_projection = event.payload.get("owner_projection_eligible")
    if not isinstance(history, bool) or not isinstance(owner_projection, bool):
        return None
    if (
        interaction.get("history_eligible") is not history
        or interaction.get("owner_projection_eligible") is not owner_projection
    ):
        return None
    nested_history = interaction.get("history_eligible")
    nested_owner_projection = interaction.get("owner_projection_eligible")
    if not isinstance(nested_history, bool) or not isinstance(nested_owner_projection, bool):
        return None
    return nested_history, nested_owner_projection


def _require_memory_read_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    _require_internal_token(request, "memory_read", token)


@router.post("/events")
async def append_event(
    body: EvidenceEventCreate,
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    if body.event_type in SESSION_BOUND_EVENT_TYPES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_bound_event_required",
                "event_type": body.event_type,
            },
        )
    if body.event_type in SERVER_OWNED_EVENT_TYPES:
        raise HTTPException(
            status_code=409,
            detail={"code": "server_owned_event_required", "event_type": body.event_type},
        )
    event = EvidenceEvent(**body.model_dump())
    try:
        async with _account_write(request, body.account_id):
            result = await _archive(request).record(event)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _wake_compiler(request)
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={
            "event_id": result.event_id,
            "outbox_id": result.outbox_id,
            "recorded_at": result.recorded_at.isoformat(),
            "duplicate": result.duplicate,
        },
    )


@router.post("/session-events")
async def append_session_event(
    body: SessionEvidenceEventCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    session = require_active_voice_session(request, body.session_id)
    legacy_access = (
        await _resolve_legacy_access(request, session=session, now=datetime.now(UTC))
        if str(session["interaction_mode"]) == "legacy"
        else None
    )
    archive = _archive(request)
    account_id = str(session["user_id"])
    values = body.model_dump()
    tool_epoch = values.pop("tool_epoch")
    values["account_id"] = account_id
    payload = dict(values["payload"])
    reason_code = payload.get("speaker_reason_code") or payload.get("reason_code")
    if not isinstance(reason_code, str):
        reason_code = None
    assistant_event = body.speaker_class == "assistant"
    parent_eligibility: tuple[bool, bool] | None = None
    parent: EvidenceEvent | None = None
    if assistant_event and legacy_access is not None:
        assert body.turn_id is not None and body.generation_id is not None
        parent = EvidenceEvent(
            event_id=f"legacy-parent:{body.event_id}",
            account_id=legacy_access.resource_owner_account_id,
            event_type="speech.utterance_finalized",
            occurred_at=body.occurred_at,
            speaker_class="owner",
            source="legacy.shell",
            payload={},
            session_id=body.session_id,
            turn_id=body.turn_id,
            generation_id=body.generation_id,
        )
        parent_eligibility = (False, False)
    elif assistant_event:
        assert body.turn_id is not None and body.generation_id is not None
        try:
            parent = await archive.turn_event(
                account_id=account_id,
                session_id=body.session_id,
                turn_id=body.turn_id,
                generation_id=body.generation_id,
                event_type="speech.utterance_finalized",
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "ambiguous_parent_turn"},
            ) from exc
        if parent is None:
            raise HTTPException(
                status_code=425,
                detail={"code": "parent_turn_not_recorded"},
            )
        parent_eligibility = _canonical_turn_eligibility(parent)
        if parent_eligibility is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "parent_turn_not_canonical"},
            )
    elif legacy_access is None and (
        body.event_type == "speech.utterance_finalized"
        and body.turn_id is not None
        and body.generation_id is not None
    ):
        try:
            existing_turn = await archive.turn_event(
                account_id=account_id,
                session_id=body.session_id,
                turn_id=body.turn_id,
                generation_id=body.generation_id,
                event_type=body.event_type,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "ambiguous_parent_turn"},
            ) from exc
        if existing_turn is not None and existing_turn.event_id != body.event_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "turn_event_conflict"},
            )
    raw_response_provenance = payload.get("response_provenance")
    canonical_response_provenance: dict[str, Any] | None = None
    if raw_response_provenance is not None:
        if not assistant_event or parent is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "response_provenance_invalid"},
            )
        canonical_response_provenance = await _canonical_response_provenance(
            request,
            raw=raw_response_provenance,
            session=session,
            parent=parent,
            tool_epoch=cast(int, tool_epoch),
            legacy_access=legacy_access,
        )
    elif assistant_event and legacy_access is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "response_provenance_invalid"},
        )
    policy_speaker = cast(
        SpeakerClass,
        "guest" if assistant_event else body.speaker_class,
    )
    trusted_interaction = ModePolicy.trusted_context(
        FrozenMode.from_session(session),
        speaker_class=policy_speaker,
        reason_code=reason_code,
        history_eligible=(parent_eligibility[0] if parent_eligibility is not None else None),
        owner_projection_eligible=(
            parent_eligibility[1] if parent_eligibility is not None else None
        ),
    )
    prompt_kind = payload.get("prompt_kind")
    if prompt_kind not in {"spontaneous", "open", "structured", "leading"}:
        prompt_kind = "spontaneous"
    for key in SERVER_INTERACTION_PAYLOAD_KEYS:
        payload.pop(key, None)
    payload.update(
        {
            "interaction_mode": trusted_interaction["interaction_mode"],
            "mode_policy_version": trusted_interaction["mode_policy_version"],
            "simulated_output": trusted_interaction["simulated_output"],
            "history_eligible": trusted_interaction["history_eligible"],
            "owner_projection_eligible": trusted_interaction["owner_projection_eligible"],
            "interaction": trusted_interaction,
            "prompt_kind": prompt_kind,
        }
    )
    if tool_epoch is not None:
        payload["tool_epoch"] = tool_epoch
    if canonical_response_provenance is not None:
        payload["response_provenance"] = canonical_response_provenance
    if (
        body.event_type == "speech.utterance_finalized"
        and body.speaker_class == "owner"
        and body.turn_id is not None
        and body.generation_id is not None
        and tool_epoch is not None
        and trusted_interaction["interaction_mode"] == "companion"
        and trusted_interaction["simulated_output"] is False
        and trusted_interaction["owner_projection_eligible"] is True
        and explicit_remember_content(payload.get("text")) is not None
    ):
        payload["memory_write_intent"] = dict(EXPLICIT_MEMORY_INTENT)
    if legacy_access is not None:
        return await _append_legacy_shell_event(
            request,
            body=body,
            access=legacy_access,
            tool_epoch=tool_epoch,
            response_provenance=canonical_response_provenance,
        )
    learning_task_id = session.get("learning_task_id")

    async def record_event() -> tuple[EvidenceEvent, Any]:
        values["payload"] = payload
        event = EvidenceEvent(**values)
        try:
            async with _account_write(request, event.account_id):
                result = await archive.record(event)
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return event, result

    if isinstance(learning_task_id, str) and learning_task_id:
        lock = cast(asyncio.Lock, request.app.state.growth_task_lock)
        async with lock:
            task = await request.app.state.growth_reader.task(
                account_id=account_id,
                task_id=learning_task_id,
            )
            if task is not None and task.kind == "natural_chat" and task.status == "active":
                payload.update(
                    {
                        "learning_task_id": learning_task_id,
                        "learning_task_kind": "natural_chat",
                    }
                )
            event, result = await record_event()
    else:
        event, result = await record_event()
    _wake_compiler(request)
    if trusted_interaction["capabilities"]["learning"]:
        _schedule_persona_observation(
            request,
            background_tasks,
            event=event,
            duplicate=result.duplicate,
        )
    elif trusted_interaction["capabilities"]["persona_low_sensitivity"]:
        _schedule_low_sensitivity_persona_observation(
            request,
            background_tasks,
            event=event,
            duplicate=result.duplicate,
        )
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={
            "event_id": result.event_id,
            "outbox_id": result.outbox_id,
            "recorded_at": result.recorded_at.isoformat(),
            "duplicate": result.duplicate,
        },
    )


@router.get("/raw-voice-consent")
async def raw_voice_consent(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    consent = await _archive(request).active_raw_voice_consent(account_id=user.user_id)
    return {"consent": _consent_payload(consent) if consent is not None else None}


@router.post("/raw-voice-consent", status_code=201)
async def grant_raw_voice_consent(
    body: RawVoiceConsentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _require_registered(request, user)
    async with _account_write(request, user.user_id):
        consent = await _archive(request).grant_raw_voice_consent(
            account_id=user.user_id,
            policy_version=body.policy_version,
            retention_policy=body.retention_policy,
            granted_at=datetime.now(UTC),
        )
    return _consent_payload(consent)


@router.delete("/raw-voice-consent")
async def revoke_raw_voice_consent(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _require_registered(request, user)
    archive = _archive(request)
    store = _archive_objects(request)
    async with _account_write(request, user.user_id):
        try:
            revocation = await archive.revoke_raw_voice_consent(
                account_id=user.user_id,
                revoked_at=datetime.now(UTC),
            )
        except RawVoiceConsentRequiredError as exc:
            raise HTTPException(status_code=404, detail="raw voice consent not found") from exc
        try:
            for reference in revocation.references:
                await store.delete(reference)
            await archive.purge_raw_voice_blobs(
                account_id=user.user_id,
                object_keys=tuple(reference.object_key for reference in revocation.references),
            )
        except Exception as exc:
            logger.exception("raw voice revocation cleanup failed")
            raise HTTPException(
                status_code=503,
                detail="raw voice deletion is incomplete; retry revocation",
            ) from exc
    return _consent_payload(revocation.consent)


@router.get("/session-raw-voice-consent")
async def session_raw_voice_consent(
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, session_id)
    consent = await _archive(request).active_raw_voice_consent(account_id=str(session["user_id"]))
    if consent is None:
        return {"allowed": False}
    return {
        "allowed": True,
        "consent_grant_id": consent.consent_grant_id,
        "policy_version": consent.policy_version,
        "retention_policy": consent.retention_policy,
    }


@router.post("/session-raw-audio")
async def append_session_raw_audio(
    body: SessionRawAudioCreate,
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    session = require_active_voice_session(request, body.session_id)
    if body.speaker_class != "owner":
        raise HTTPException(status_code=422, detail="raw voice archive is restricted to the owner")
    account_id = str(session["user_id"])
    archive = _archive(request)
    store = _archive_objects(request)
    active = await archive.active_raw_voice_consent(account_id=account_id)
    if active is None:
        raise HTTPException(status_code=410, detail="raw voice consent is no longer active")
    if (
        active.consent_grant_id != body.consent_grant_id
        or active.retention_policy != body.retention_policy
    ):
        raise HTTPException(status_code=403, detail="raw voice consent grant does not match")
    audio = _decode_owner_wav(body.audio_base64)
    event = await archive.event(account_id=account_id, event_id=body.event_id)
    if event is None:
        raise HTTPException(
            status_code=425,
            detail={"code": "parent_turn_not_recorded"},
        )
    if (
        event.session_id != body.session_id
        or event.turn_id != body.turn_id
        or event.generation_id != body.generation_id
        or event.event_type != "speech.utterance_finalized"
        or event.speaker_class != "owner"
        or event.consent_grant_id != body.consent_grant_id
        or _canonical_turn_eligibility(event) != (True, True)
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "raw_audio_parent_mismatch"},
        )
    reference: ObjectRef | None = None
    try:
        async with _account_write(request, account_id):
            reference = await _put_archive_object(
                store,
                account_id=account_id,
                data=audio,
            )
            result = await archive.record_with_blob(
                event,
                reference,
                retention_policy=body.retention_policy,
            )
            if result.blob_duplicate is True:
                await _delete_object_safely(store, reference)
    except RawVoiceConsentRequiredError as exc:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise HTTPException(
            status_code=410, detail="raw voice consent is no longer active"
        ) from exc
    except IdempotencyConflictError as exc:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except asyncio.CancelledError:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise
    except Exception:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={
            "event_id": result.event_id,
            "outbox_id": result.outbox_id,
            "recorded_at": result.recorded_at.isoformat(),
            "duplicate": result.duplicate,
            "blob_archived": True,
        },
    )


@router.post("/session-context")
async def session_memory_context(
    body: SessionMemoryContextCreate,
    request: Request,
    _: Annotated[None, Depends(_require_memory_read_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    trusted_interaction = ModePolicy.trusted_context(
        FrozenMode.from_session(session),
        speaker_class=body.speaker_class,
    )
    if not trusted_interaction["capabilities"]["private_memory"]:
        return {"items": []}
    settings = cast(ControlSettings, request.app.state.settings)
    recall = RecallPlanner.plan(
        query=body.topic,
        now=current_local_time(settings.memoria_timezone),
        people=await _catalog(request).people(account_id=str(session["user_id"]), limit=100),
    )
    result = await _catalog(request).context(
        MemorySearchQuery(
            account_id=str(session["user_id"]),
            speaker_class=body.speaker_class,
            text=recall.text,
            entity_ids=recall.entity_ids,
            occurred_after=recall.occurred_after,
            occurred_before=recall.occurred_before,
            include_candidates=False,
            limit=body.limit,
        )
    )
    return {
        "items": [
            {
                "kind": item.kind,
                "title": item.title,
                "snippet": item.snippet,
                "category": item.category,
                "domain_category": item.domain_category,
                "memory_kind": item.memory_kind,
                "entity_ids": list(item.entity_ids),
                "status": item.status,
                "source_event_id": item.source_event_id,
                "source_event_ids": list(item.source_event_ids),
                "occurred_at": item.occurred_at.isoformat(),
                "valid_from": item.valid_from.isoformat() if item.valid_from else None,
                "valid_to": item.valid_to.isoformat() if item.valid_to else None,
                "observed_at": item.observed_at.isoformat() if item.observed_at else None,
                "stability": item.stability,
                "salience": item.salience,
                "sensitivity": item.sensitivity,
                "conflict_state": item.conflict_state,
                "score": item.score,
            }
            for item in result.items
        ]
    }


@router.get("/timeline")
async def timeline(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    bundle = await _archive(request).context(
        ContextQuery(account_id=user.user_id, speaker_class="owner", limit=limit)
    )
    return {
        "items": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "speaker_class": event.speaker_class,
                "source": event.source,
                "payload": dict(event.payload),
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "generation_id": event.generation_id,
            }
            for event in bundle.evidence
        ]
    }


def _search_item(item: Any) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "title": item.title,
        "snippet": item.snippet,
        "category": item.category,
        "domain_category": item.domain_category,
        "memory_kind": item.memory_kind,
        "entity_ids": list(item.entity_ids),
        "status": item.status,
        "source_event_id": item.source_event_id,
        "source_event_ids": list(item.source_event_ids),
        "occurred_at": item.occurred_at.isoformat(),
        "valid_from": item.valid_from.isoformat() if item.valid_from else None,
        "valid_to": item.valid_to.isoformat() if item.valid_to else None,
        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
        "stability": item.stability,
        "salience": item.salience,
        "sensitivity": item.sensitivity,
        "conflict_state": item.conflict_state,
        "score": item.score,
    }


@router.get("/search")
async def search_memories(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    q: Annotated[str, Query(max_length=500)] = "",
    kind: Annotated[list[str] | None, Query()] = None,
    memory_kind: Annotated[list[MemoryKind] | None, Query()] = None,
    domain_category: Annotated[list[DomainCategory] | None, Query()] = None,
    category: Annotated[list[MemoryCategory] | None, Query()] = None,
    entity_id: Annotated[list[str] | None, Query()] = None,
    valid_at: datetime | None = None,
    sensitivity: Annotated[list[MemorySensitivity] | None, Query()] = None,
    conflict_state: Annotated[list[ConflictState] | None, Query()] = None,
    include_candidates: bool = True,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    if category and domain_category:
        raise HTTPException(
            status_code=422,
            detail="use domain_category or legacy category, not both",
        )
    try:
        result = await _catalog(request).search(
            MemorySearchQuery(
                account_id=user.user_id,
                speaker_class="owner",
                text=q,
                kinds=tuple(kind or ()),
                memory_kinds=tuple(memory_kind or ()),
                domain_categories=tuple(domain_category or category or ()),
                entity_ids=tuple(entity_id or ()),
                valid_at=valid_at,
                sensitivities=tuple(sensitivity or ()),
                conflict_states=tuple(conflict_state or ()),
                include_candidates=include_candidates,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
                limit=limit,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"items": [_search_item(item) for item in result.items]}


@router.get("/life-timeline")
async def life_timeline(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    items = await _catalog(request).timeline(account_id=user.user_id, limit=limit)
    return {
        "items": [
            {
                "timeline_id": item.timeline_id,
                "title": item.title,
                "category": item.category,
                "domain_category": item.domain_category,
                "status": item.status,
                "event_start": item.event_start.isoformat(),
                "event_end": item.event_end.isoformat() if item.event_end else None,
                "time_precision": item.time_precision,
                "source_event_id": item.source_event_id,
                "source_event_ids": list(item.source_event_ids),
                "episode_id": item.episode_id,
            }
            for item in items
        ]
    }


@router.get("/people")
async def people(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    items = await _catalog(request).people(account_id=user.user_id, limit=limit)
    return {
        "items": [
            {
                "person_id": item.person_id,
                "display_name": item.display_name,
                "relationship_to_owner": item.relationship_to_owner,
                "aliases": list(item.aliases),
                "status": item.status,
                "source_event_id": item.source_event_id,
            }
            for item in items
        ]
    }


@router.get("/review-queue")
async def review_queue(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    items = await _catalog(request).review_queue(account_id=user.user_id)
    return {
        "items": [
            {
                "item_id": item.item_id,
                "kind": item.kind,
                "category": item.category,
                "domain_category": item.domain_category,
                "memory_kind": item.memory_kind,
                "value": item.value,
                "status": item.status,
                "reason": item.reason,
                "source_event_id": item.source_event_id,
                "conflict_state": item.conflict_state,
            }
            for item in items
        ]
    }


class MemoryReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["confirm", "dispute", "retract", "correct"]
    corrected_value: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def enforce_correction_value(self) -> MemoryReviewBody:
        if self.action == "correct" and self.corrected_value is None:
            raise ValueError("corrected_value is required for correction")
        if self.action != "correct" and self.corrected_value is not None:
            raise ValueError("corrected_value is only valid for correction")
        return self


@router.post("/memories/{claim_id}/review")
async def review_memory(
    claim_id: str,
    body: MemoryReviewBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        reviewed = await _catalog(request).review(
            MemoryClaimReview(
                account_id=user.user_id,
                claim_id=claim_id,
                action=body.action,
                corrected_value=body.corrected_value,
            )
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="memory claim not found") from exc
    return {
        "claim_id": reviewed.claim_id,
        "status": reviewed.status,
        "value": reviewed.value,
        "review_event_id": reviewed.review_event_id,
    }


class ArchiveExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=8, max_length=128)


class ArchiveDeletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, min_length=8, max_length=128)
    wechat_login_code: str | None = Field(default=None, min_length=1, max_length=256)
    confirmation: Literal["永久删除我的全部数据"]

    @model_validator(mode="after")
    def require_one_step_up_method(self) -> ArchiveDeletionBody:
        if (self.password is None) == (self.wechat_login_code is None):
            raise ValueError("provide exactly one account verification method")
        return self


def _verify_sensitive_action(
    request: Request,
    user: AuthenticatedUser,
    password: str,
) -> None:
    account = _store(request).get_account(user_id=user.user_id)
    if account is None:
        raise HTTPException(status_code=409, detail="请先注册账户再执行该操作")
    if not verify_password(password, str(account["password_hash"])):
        raise HTTPException(status_code=403, detail="密码验证失败")


async def _verify_deletion_action(
    request: Request,
    user: AuthenticatedUser,
    body: ArchiveDeletionBody,
) -> None:
    if body.password is not None:
        _verify_sensitive_action(request, user, body.password)
        return
    assert body.wechat_login_code is not None
    try:
        session = await code_to_session(
            cast(ControlSettings, request.app.state.settings),
            body.wechat_login_code,
        )
    except WechatAuthError as exc:
        status_code = 503 if exc.code == "wechat_credentials_missing" else 502
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code},
        ) from exc
    account_id = _store(request).external_identity_user(
        provider="wechat_openid",
        subject_hash=openid_hash(session.openid),
    )
    if account_id != user.user_id:
        raise HTTPException(status_code=403, detail="微信身份验证失败")


def _account_audit_hash(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


@router.post("/exports")
async def export_archive(
    body: ArchiveExportBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    _verify_sensitive_action(request, user, body.password)
    exported = await _governance(request).export_account(user.user_id)
    logger.info(
        "archive export completed account_hash=%s manifest=%s",
        _account_audit_hash(user.user_id),
        exported["manifest_sha256"],
    )
    return JSONResponse(
        content=exported,
        headers={
            "Content-Disposition": ('attachment; filename="memoria-account-export.json"'),
            "Cache-Control": "no-store",
        },
    )


@router.post("/deletion-requests")
async def delete_archive(
    body: ArchiveDeletionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    await _verify_deletion_action(request, user, body)
    try:
        result = await _governance(request).delete_account(user.user_id)
    except AccountDeletionIncompleteError as exc:
        logger.warning(
            "account deletion incomplete account_hash=%s reason=%s",
            _account_audit_hash(user.user_id),
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="外部资产删除未完成，请稍后重试") from exc
    logger.info(
        "account deletion completed account_hash=%s request_id=%s",
        _account_audit_hash(user.user_id),
        result["request_id"],
    )
    return result
