"""Validate the Agent's session-bound evidence envelope.

The archive router accepts per-session Agent evidence. Before any of that
reaches storage the envelope decides three things: whether a frozen legacy
session may write a shell turn and its audits, whether an event is eligible
for the canonical turn projection, and what bounded memory-capture candidate
the durable projector may see. Those rules are the archive write contract
rather than HTTP routing, so they live here and the router passes the request
through.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal, cast

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.archive.domain import EvidenceEvent
from services.control_api.app.mode_policy import FrozenMode, ModePolicy
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


class EvidenceEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    subject_id: str | None = Field(default=None, min_length=1, max_length=128)
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
    # Agent-owned MemoryWriteFence envelope.  These claims are accepted at
    # the transport boundary so the real Agent event is not rejected, but
    # they are never copied directly into archive evidence.  The route emits
    # a bounded server-owned projection candidate and the PostgreSQL Memory
    # projector independently revalidates Session + Policy authority.
    session_epoch: int | None = Field(default=None, ge=0)
    device_id: str | None = Field(default=None, max_length=128)
    subject_revision: int | None = Field(default=None, ge=0)
    active_subject_id: str | None = Field(default=None, max_length=128)
    runtime_profile_id: str | None = Field(default=None, max_length=128)
    actor_id: str | None = Field(default=None, max_length=128)
    binding_id: str | None = Field(default=None, max_length=128)
    binding_version: int | None = Field(default=None, ge=1)
    memory_scope: Literal[
        "unknown",
        "session_ephemeral",
        "personal_private",
        "guardian_summary",
        "family_shared",
        "legacy_archive",
    ] = "unknown"
    policy_receipt_id: str | None = Field(default=None, max_length=128)
    raw_audio_receipt_id: str | None = Field(default=None, max_length=128)
    training_receipt_id: str | None = Field(default=None, max_length=128)
    no_model_training: bool = True
    event_sequence: int | None = Field(default=None, ge=1)
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


def _limit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    import json

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError("payload must not exceed 64 KiB")
    return payload


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


async def resolve_legacy_access(
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


async def append_legacy_shell_event(
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


def canonical_turn_eligibility(event: EvidenceEvent) -> tuple[bool, bool] | None:
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


def memory_capture_candidate(
    *,
    body: SessionEvidenceEventCreate,
    claims: dict[str, Any],
    trusted_interaction: dict[str, Any],
    retention_allowed: bool,
) -> dict[str, object] | None:
    """Return only the bounded input for the durable authority projector.

    This is deliberately a *candidate*, not authority.  The action-executor
    PostgreSQL port checks the current Session profile and the exact Policy
    receipt before inserting ``memory_capture_evidence``.  Keeping the raw
    Agent claims out of the archive payload also prevents a nested caller
    field from becoming trusted by accident.
    """

    if not (
        body.event_type == "speech.utterance_finalized"
        and body.speaker_class == "owner"
        and body.turn_id is not None
        and body.generation_id is not None
        and body.tool_epoch is not None
        and retention_allowed
        and trusted_interaction.get("interaction_mode") == "companion"
        and trusted_interaction.get("simulated_output") is False
        and trusted_interaction.get("owner_projection_eligible") is True
    ):
        return None
    required_text = (
        "device_id",
        "active_subject_id",
        "runtime_profile_id",
        "actor_id",
        "binding_id",
    )
    if any(
        not isinstance(claims.get(field), str)
        or not cast(str, claims[field]).strip()
        for field in required_text
    ):
        return None
    if claims.get("memory_scope") not in {
        "personal_private",
        "guardian_summary",
        "family_shared",
    }:
        return None
    required_positive = ("session_epoch", "binding_version", "event_sequence")
    if any(
        not isinstance(claims.get(field), int)
        or isinstance(claims[field], bool)
        or cast(int, claims[field]) < 1
        for field in required_positive
    ):
        return None
    subject_revision = claims.get("subject_revision")
    if (
        not isinstance(subject_revision, int)
        or isinstance(subject_revision, bool)
        or subject_revision < 0
    ):
        return None
    return {
        "active_subject_id": cast(str, claims["active_subject_id"]),
        "actor_id": cast(str, claims["actor_id"]),
        "binding_id": cast(str, claims["binding_id"]),
        "binding_version": cast(int, claims["binding_version"]),
        "device_id": cast(str, claims["device_id"]),
        "event_sequence": cast(int, claims["event_sequence"]),
        "generation_id": body.generation_id,
        "memory_scope": cast(str, claims["memory_scope"]),
        "runtime_profile_id": cast(str, claims["runtime_profile_id"]),
        "session_id": body.session_id,
        "session_epoch": cast(int, claims["session_epoch"]),
        "subject_revision": subject_revision,
        "tool_epoch": body.tool_epoch,
        "turn_id": body.turn_id,
    }
