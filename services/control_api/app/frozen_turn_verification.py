"""Authorize a frozen interaction turn and append its legacy audit.

A turn is served from a session that freezes the interaction mode, the
digital-self version and, for a legacy grant, the shell, scope and relationship
it was granted on. This module decides whether the current live state still
matches that frozen claim, and records the reads the grant covers. The
interaction router keeps the endpoints and passes the request through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import HTTPException, Request

from services.common.companions import (
    COMPANION_STYLE_VERSION,
    DESIGNED_VOICE_MODEL,
    companion_definition,
)
from services.control_api.app.account_gate import require_capability_for_account_id
from services.control_api.app.database import MemoryStore
from services.control_api.app.mode_policy import FrozenMode
from services.control_api.app.response_plan_contract import ResponsePlanRequest
from services.control_api.app.security import require_active_voice_session
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
from services.digital_self.preview import SelfPreviewRegistryPort
from services.digital_self.response_planner import ResponsePlan
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAccessSnapshot,
    LegacyAuditTarget,
    LegacyFence,
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

__all__ = ["append_legacy_plan_audit", "resolve_frozen_turn"]



def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _registry(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


def _legacy_registry(request: Request) -> LegacyRegistryPort:
    return cast(LegacyRegistryPort, request.app.state.legacy_registry)


def _self_model_registry(request: Request) -> SelfModelRegistryPort:
    return cast(SelfModelRegistryPort, request.app.state.self_model_registry)


def _response_plan_unavailable() -> HTTPException:
    return HTTPException(status_code=409, detail={"code": "response_plan_unavailable"})


def _relationship_in_version(
    version: DigitalSelfVersion, profile_id: str
) -> RelationshipProfileManifestEntry | None:
    matches = [
        entry
        for entry in version.manifest.entries
        if isinstance(entry, RelationshipProfileManifestEntry) and entry.profile_id == profile_id
    ]
    return matches[0] if len(matches) == 1 else None


def _legacy_access_matches_session(
    frozen: FrozenMode,
    access: LegacyAccessSnapshot,
) -> bool:
    return (
        frozen.actor_account_id == access.resource_owner_account_id
        if access.actor_role == "owner_preview"
        else frozen.actor_account_id == access.grantee_account_id
    ) and (
        frozen.resource_owner_account_id == access.resource_owner_account_id
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


def _legacy_relationship_matches(
    grant: LegacyGrant,
    live: RelationshipProfile,
    manifest: RelationshipProfileManifestEntry,
) -> bool:
    snapshot = grant.relationship
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
    return (
        live.account_id == grant.owner_account_id
        and live.status == "approved"
        and live.step_up_verified
        and not live.unresolved_conflict
        and expected
        == (
            live.profile_id,
            live.version_number,
            live.relationship_id,
            live.salutation,
            live.tone,
            live.advice_style,
            live.sharing_scope,
            live.boundaries,
        )
        == (
            manifest.profile_id,
            manifest.version_number,
            manifest.relationship_id,
            manifest.salutation,
            manifest.tone,
            manifest.advice_style,
            manifest.sharing_scope,
            manifest.boundaries,
        )
    )


def _legacy_manifest_refs(version: DigitalSelfVersion) -> frozenset[LegacyManifestItemRef]:
    refs: set[LegacyManifestItemRef] = set()
    for entry in version.manifest.entries:
        if hasattr(entry, "claim_id") and isinstance(entry, MemoryClaimManifestEntry):
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


def _frozen_fallback_voice_matches(frozen: FrozenMode) -> bool:
    return (
        frozen.fallback_voice_profile_id is not None
        and frozen.fallback_voice_provider == "volcengine_doubao"
        and frozen.fallback_voice_model == DESIGNED_VOICE_MODEL
        and frozen.fallback_voice_resource_id == DESIGNED_VOICE_MODEL
    )


async def resolve_frozen_turn(
    request: Request, session_id: str
) -> tuple[
    FrozenMode,
    str,
    DigitalSelfVersion | None,
    RelationshipProfileManifestEntry | None,
    LegacyAccessSnapshot | None,
]:
    session = require_active_voice_session(request, session_id)
    frozen = FrozenMode.from_session(session)
    actor_account_id = str(session["user_id"])
    if frozen.interaction_mode == "companion":
        definition = companion_definition(frozen.companion_style_id)
        if (
            definition is None
            or frozen.companion_style_version != COMPANION_STYLE_VERSION
            or any(
                value is not None
                for value in (
                    frozen.digital_self_version_id,
                    frozen.relationship_profile_id,
                    frozen.legacy_grant_id,
                    frozen.voice_profile_id,
                    frozen.voice_profile_version,
                    frozen.voice_provider,
                    frozen.voice_model,
                    frozen.voice_resource_id,
                    frozen.voice_provider_expires_at,
                    frozen.voice_speaker_sha256,
                    frozen.fallback_voice_profile_id,
                    frozen.fallback_voice_provider,
                    frozen.fallback_voice_model,
                    frozen.fallback_voice_resource_id,
                )
            )
        ):
            raise _response_plan_unavailable()
        return frozen, actor_account_id, None, None, None
    if frozen.interaction_mode == "archive" or not frozen.digital_self_version_id:
        raise _response_plan_unavailable()
    if frozen.interaction_mode == "self_preview":
        require_capability_for_account_id(
            actor_account_id,
            "self_preview",
            store=_store(request),
        )
    elif frozen.interaction_mode == "legacy":
        require_capability_for_account_id(
            actor_account_id,
            "legacy_grant" if frozen.legacy_actor_role == "owner_preview" else "legacy_receive",
            store=_store(request),
        )
    legacy_access: LegacyAccessSnapshot | None = None
    resource_owner_account_id = actor_account_id
    legacy_grant: LegacyGrant | None = None
    if frozen.interaction_mode == "legacy":
        if (
            frozen.legacy_actor_role not in {"owner_preview", "grantee"}
            or frozen.legacy_grant_id is None
        ):
            raise _response_plan_unavailable()
        purpose: Literal["owner_preview", "grantee_session"] = (
            "owner_preview" if frozen.legacy_actor_role == "owner_preview" else "grantee_session"
        )
        try:
            legacy_access = await _legacy_registry(request).resolve_access(
                actor_account_id=actor_account_id,
                grant_id=frozen.legacy_grant_id,
                purpose=purpose,
                now=datetime.now(UTC),
            )
            legacy_grant = await _legacy_registry(request).get_grant(
                actor_account_id=actor_account_id,
                grant_id=frozen.legacy_grant_id,
            )
        except (LegacyAccessDeniedError, LegacyNotFoundError) as exc:
            raise _response_plan_unavailable() from exc
        if (
            not _legacy_access_matches_session(frozen, legacy_access)
            or legacy_grant.grant_snapshot_sha256 != legacy_access.grant_snapshot_sha256
            or legacy_grant.scope_sha256 != legacy_access.scope_sha256
            or legacy_grant.allowed_items != legacy_access.allowed_items
            or _store(request).is_account_unavailable(
                user_id=legacy_access.resource_owner_account_id
            )
            or _store(request).is_account_unavailable(user_id=legacy_access.grantee_account_id)
        ):
            raise _response_plan_unavailable()
        resource_owner_account_id = legacy_access.resource_owner_account_id
        require_capability_for_account_id(
            resource_owner_account_id,
            "legacy_grant",
            store=_store(request),
        )
    try:
        version = await _registry(request).get(
            account_id=resource_owner_account_id,
            version_id=frozen.digital_self_version_id,
        )
    except VersionNotFoundError as exc:
        raise _response_plan_unavailable() from exc
    if version.account_id != resource_owner_account_id:
        raise _response_plan_unavailable()
    if frozen.interaction_mode == "self_preview":
        if (
            version.status not in {"approved", "frozen"}
            or frozen.legacy_grant_id is not None
            or frozen.relationship_profile_id is not None
            or frozen.companion_style_id is not None
            or frozen.manifest_sha256 != version.manifest_sha256
            or frozen.preview_grant_id is None
            or frozen.perspective not in {"owner", "child", "friend"}
            or not _frozen_fallback_voice_matches(frozen)
            or await cast(
                SelfPreviewRegistryPort, request.app.state.self_preview_registry
            ).version_stale(
                account_id=actor_account_id,
                version_id=version.version_id,
                manifest_sha256=version.manifest_sha256,
            )
        ):
            raise _response_plan_unavailable()
    elif frozen.interaction_mode == "legacy":
        assert legacy_access is not None and legacy_grant is not None
        if (
            version.status != "frozen"
            or version.version_number != legacy_access.version_number
            or version.manifest_sha256 != legacy_access.manifest_sha256
            or not frozen.relationship_profile_id
            or not frozen.legacy_grant_id
            or not set(legacy_access.allowed_items) <= _legacy_manifest_refs(version)
        ):
            raise _response_plan_unavailable()
    else:
        raise _response_plan_unavailable()

    relationship = (
        _relationship_in_version(version, frozen.relationship_profile_id)
        if frozen.relationship_profile_id
        else None
    )
    if frozen.relationship_profile_id and relationship is None:
        raise _response_plan_unavailable()
    if frozen.interaction_mode == "legacy":
        assert legacy_access is not None and legacy_grant is not None and relationship is not None
        try:
            live_relationship = await _self_model_registry(request).get_relationship_profile(
                account_id=resource_owner_account_id,
                profile_id=legacy_access.relationship_profile_id,
                version_number=legacy_access.relationship_profile_version,
            )
        except SelfModelNotFoundError as exc:
            raise _response_plan_unavailable() from exc
        if not _legacy_relationship_matches(
            legacy_grant,
            live_relationship,
            relationship,
        ):
            raise _response_plan_unavailable()
    return frozen, actor_account_id, version, relationship, legacy_access


async def append_legacy_plan_audit(
    request: Request,
    *,
    body: ResponsePlanRequest,
    access: LegacyAccessSnapshot,
    plan: ResponsePlan,
) -> None:
    fence = LegacyFence(
        session_id=body.fence.session_id,
        turn_id=str(body.fence.turn_id),
        generation_id=str(body.fence.generation_id),
        tool_epoch=body.fence.tool_epoch,
    )
    registry = _legacy_registry(request)
    now = datetime.now(UTC)
    actor_account_id = (
        access.resource_owner_account_id
        if access.actor_role == "owner_preview"
        else access.grantee_account_id
    )
    try:
        for ref in plan.provenance.source_refs[:16]:
            await registry.append_runtime_audit(
                actor_account_id=actor_account_id,
                grant_id=access.grant_id,
                action="read_source",
                decision="allowed",
                reason="source_read",
                fence=fence,
                target=LegacyAuditTarget(ref.entry_type, ref.entry_id),
                now=now,
            )
        refusal_kind = plan.disclosure_decision.kind
        if refusal_kind == "privacy":
            await registry.append_runtime_audit(
                actor_account_id=actor_account_id,
                grant_id=access.grant_id,
                action="refuse",
                decision="denied",
                reason="privacy_refusal",
                fence=fence,
                target=None,
                now=now,
            )
        elif refusal_kind == "unknown":
            await registry.append_runtime_audit(
                actor_account_id=actor_account_id,
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
                actor_account_id=actor_account_id,
                grant_id=access.grant_id,
                action="plan_answer",
                decision="allowed",
                reason="answer_planned",
                fence=fence,
                target=None,
                now=now,
            )
    except (LegacyAccessDeniedError, LegacyNotFoundError) as exc:
        raise _response_plan_unavailable() from exc
