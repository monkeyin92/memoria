"""Archive 409 conflicts whose reason code clients must be able to count.

Two families live here. A write that reuses an ``event_id`` for different
immutable content is a failed write, not an idempotent success. A response
whose submitted provenance disagrees with the frozen session, manifest or
Legacy snapshot is rejected before it reaches the ledger. Both answer 409 and
name their cause, so no client has to match prose to tell them apart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal, cast

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.archive.domain import (
    EvidenceEvent,
    IdempotencyConflictError,
    LifeArchivePort,
)
from services.common.companions import (
    DEFAULT_COMPANION_ID,
    DESIGNED_VOICE_MODEL,
    designed_voice_profile,
    designed_voice_speaker_sha256,
)
from services.common.tts_defaults import STEPFUN_ARCHIVE_MODELS
from services.control_api.app.config import ControlSettings
from services.control_api.app.mode_policy import FrozenMode
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
from services.evolution.receipt import verify_resolution_receipt
from services.evolution.store import EvolutionNotFoundError, EvolutionStore
from services.legacy.domain import LegacyAccessSnapshot

EVENT_ID_CONTENT_CONFLICT = "event_id_content_conflict"
_LOCAL_SAFE_PLANNER_POLICY_VERSION = "local-safe-fallback-v1"
_DOUBAO_TTS_PROVIDER = "volcengine_doubao"
_STEPFUN_TTS_PROVIDER = "stepfun"
_DOUBAO_PERSONAL_VOICE_MODEL = "seed-icl-2.0"


def _tts_matches_applied_voice(submitted: ResponseProvenanceCreate) -> bool:
    """Doubao pairs with its voice resource. StepFun pairs with the designed voice.

    Production speech is StepFun primary. The session still freezes the
    approved designed-voice contract, and the archive records which engine
    actually spoke that turn.
    """

    if submitted.tts_provider == _DOUBAO_TTS_PROVIDER:
        return submitted.tts_model == submitted.actual_voice_resource_id
    if submitted.tts_provider == _STEPFUN_TTS_PROVIDER:
        return (
            submitted.tts_model in STEPFUN_ARCHIVE_MODELS
            and submitted.actual_voice_resource_id == DESIGNED_VOICE_MODEL
        )
    return False


def event_id_conflict(_exc: IdempotencyConflictError) -> HTTPException:
    """409 naming an event_id whose stored content differs from this write.

    Idempotent duplicates never reach here: the archive returns them as a
    duplicate success.

    The detail used to be plain text, so no client could count this cause
    apart from the idempotent duplicate without matching prose.
    """

    return HTTPException(status_code=409, detail={"code": EVENT_ID_CONTENT_CONFLICT})


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _digital_self_registry(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


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


class EvolutionArtifactRefCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    candidate_id: str = Field(min_length=1, max_length=128)
    version: int = Field(ge=1)
    kind: Literal["prompt"]
    status: Literal["canary", "stable"]
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvolutionReceiptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version: Literal["evolution-resolution-v1"]
    issued_at: str = Field(min_length=1, max_length=64)
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^[0-9a-f]{64}$")


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
    evolution_contract_version: Literal["v1"] | None = None
    evolution_artifacts: list[EvolutionArtifactRefCreate] = Field(
        default_factory=list, max_length=8
    )
    evolution_receipt: EvolutionReceiptCreate | None = None
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
        if self.evolution_contract_version is None:
            if self.evolution_artifacts or self.evolution_receipt is not None:
                raise ValueError("evolution provenance requires a negotiated contract")
        elif bool(self.evolution_artifacts) == (self.evolution_receipt is None):
            raise ValueError("evolution provenance receipt does not match artifacts")
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
        if has_actual_voice and not _tts_matches_applied_voice(self):
            raise ValueError("actual voice snapshot does not match the tts runtime")
        return self


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
        designed_engine = submitted.tts_provider == _DOUBAO_TTS_PROVIDER or (
            submitted.tts_provider == _STEPFUN_TTS_PROVIDER
            and submitted.tts_model in STEPFUN_ARCHIVE_MODELS
        )
        valid = (
            expected_profile is not None
            and profile_id == expected_profile
            and designed_engine
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
        stepfun_designed = (
            frozen.fallback_voice_profile_id is not None
            and profile_id == frozen.fallback_voice_profile_id
            and submitted.tts_provider == _STEPFUN_TTS_PROVIDER
            and submitted.tts_model in STEPFUN_ARCHIVE_MODELS
            and resource_id == DESIGNED_VOICE_MODEL
            and frozen.fallback_voice_resource_id == DESIGNED_VOICE_MODEL
            and speaker_sha256 == designed_voice_speaker_sha256(frozen.fallback_voice_profile_id)
        )
        valid = personal or safe_baseline or stepfun_designed
    elif interaction_mode == "archive":
        valid = profile_id is None and resource_id == DESIGNED_VOICE_MODEL
    if not valid:
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_voice_mismatch"},
        )
    return profile_id, profile_version, resource_id, provider_expires_at, speaker_sha256


async def _canonical_evolution_artifacts(
    request: Request,
    *,
    submitted: list[EvolutionArtifactRefCreate],
    contract_version: Literal["v1"] | None,
    receipt: EvolutionReceiptCreate | None,
    account_id: str,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
    interaction_mode: str,
    parent: EvidenceEvent | None,
    local_safe_plan: bool,
) -> list[dict[str, Any]]:
    if not submitted:
        return []
    query = parent.payload.get("text") if parent is not None else None
    if (
        contract_version != "v1"
        or receipt is None
        or local_safe_plan
        or interaction_mode != "companion"
        or parent is None
        or parent.speaker_class not in {"owner", "guest", "uncertain"}
        or not isinstance(query, str)
        or not query.strip()
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_evolution_invalid"},
        )
    actual = [artifact.model_dump() for artifact in submitted]
    settings = cast(ControlSettings, request.app.state.settings)
    if not verify_resolution_receipt(
        settings.evolution_internal_token(),
        receipt.model_dump(),
        account_id=account_id,
        session_id=session_id,
        turn_id=turn_id,
        generation_id=generation_id,
        tool_epoch=tool_epoch,
        speaker_class=cast(Any, parent.speaker_class),
        query=query,
        artifacts=actual,
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_evolution_invalid"},
        )
    evolution_store = cast(EvolutionStore, request.app.state.evolution_store)
    try:
        candidates = await asyncio.gather(
            *(
                asyncio.to_thread(evolution_store.get_candidate, artifact.candidate_id)
                for artifact in submitted
            )
        )
    except EvolutionNotFoundError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "response_provenance_evolution_invalid"},
        ) from exc
    owner_projection_eligible = (
        parent.speaker_class == "owner"
        and parent.payload.get("history_eligible") is True
        and parent.payload.get("owner_projection_eligible") is True
    )
    for artifact, candidate in zip(submitted, candidates, strict=True):
        if (
            candidate.kind != "prompt"
            or candidate.version != artifact.version
            or candidate.artifact_hash != artifact.artifact_hash
            or (
                candidate.scope == "owner_private"
                and (not owner_projection_eligible or candidate.account_id != account_id)
            )
            or (candidate.scope == "global_redacted" and candidate.account_id is not None)
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "response_provenance_evolution_invalid"},
            )
    return actual


async def canonicalize_response_provenance(
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
        or (
            interaction_mode == "companion"
            and (
                parent is None or parent.subject_id is None or event.subject_id != parent.subject_id
            )
        )
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
    evolution_artifacts = await _canonical_evolution_artifacts(
        request,
        submitted=submitted.evolution_artifacts,
        contract_version=submitted.evolution_contract_version,
        receipt=submitted.evolution_receipt,
        account_id=account_id,
        session_id=str(session["session_id"]),
        turn_id=submitted.fence.turn_id,
        generation_id=submitted.fence.generation_id,
        tool_epoch=submitted.fence.tool_epoch,
        interaction_mode=interaction_mode,
        parent=parent,
        local_safe_plan=local_safe_plan,
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
        "evolution_contract_version": submitted.evolution_contract_version,
        "evolution_artifacts": evolution_artifacts,
        "evolution_receipt": (
            submitted.evolution_receipt.model_dump()
            if submitted.evolution_receipt is not None
            else None
        ),
        "llm_provider": submitted.llm_provider,
        "llm_model": submitted.llm_model,
        "tts_provider": submitted.tts_provider,
        "tts_model": (
            submitted.tts_model
            if submitted.tts_provider == _STEPFUN_TTS_PROVIDER
            else actual_voice_resource_id
        ),
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
