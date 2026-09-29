"""Legacy grant validation, canonical digests and shell preference rules."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from services.digital_self.compiler import canonical_manifest_bytes
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfVersion,
    ManifestEntry,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RelationshipProfileManifestEntry,
)
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyAuditDecision,
    LegacyAuditReason,
    LegacyAuditTarget,
    LegacyFence,
    LegacyGrant,
    LegacyManifestItemRef,
    LegacyRelationshipSnapshot,
    LegacyRuntimeAuditAction,
    RegisteredGranteeSnapshot,
)
from services.self_model.domain import RelationshipProfile

_PREFERENCE_VALUES = {
    "preferred_response_length": {"brief", "balanced", "detailed"},
    "question_frequency": {"rare", "occasional"},
}
_DEFAULT_PREFERENCES = (
    ("preferred_response_length", "balanced"),
    ("question_frequency", "occasional"),
)


def _runtime_audit_event_id(
    *,
    actor_account_id: str,
    grant_id: str,
    action: LegacyRuntimeAuditAction,
    decision: LegacyAuditDecision,
    reason: LegacyAuditReason,
    fence: LegacyFence | None,
    target: LegacyAuditTarget | None,
) -> str:
    _required(actor_account_id, "actor_account_id")
    _required(grant_id, "grant_id")
    valid = {
        ("read_source", "allowed", "source_read"),
        ("plan_answer", "allowed", "answer_planned"),
        ("select_voice", "allowed", "voice_selected"),
        ("refuse", "denied", "privacy_refusal"),
        ("refuse", "denied", "unknown_refusal"),
    }
    if (action, decision, reason) not in valid:
        raise ValueError("invalid Legacy runtime audit action, decision, and reason")
    if action != "select_voice" and fence is None:
        raise ValueError("Legacy runtime audit fence is required")
    if fence is not None:
        _validate_fence(fence)
    if action in {"read_source", "select_voice"}:
        if target is None:
            raise ValueError("Legacy runtime audit target is required")
        _required(target.target_id, "target_id", 128)
        if action == "read_source" and target.kind == "voice_profile":
            raise ValueError("Legacy source audit target must be a manifest item")
        if action == "select_voice" and target.kind != "voice_profile":
            raise ValueError("Legacy voice audit target must be a voice profile")
    elif target is not None:
        raise ValueError("Legacy runtime audit target is not allowed")
    canonical = {
        "actor_account_id": actor_account_id,
        "grant_id": grant_id,
        "action": action,
        "decision": decision,
        "reason": reason,
        "fence": (
            {
                "session_id": fence.session_id,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
            }
            if fence is not None
            else None
        ),
        "target": (
            {"kind": target.kind, "target_id": target.target_id}
            if target is not None
            else None
        ),
    }
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "memoria:legacy-runtime-audit:" + _json(canonical)))


def _validate_fence(fence: LegacyFence) -> None:
    _required(fence.session_id, "session_id", 128)
    _required(fence.turn_id, "turn_id", 128)
    _required(fence.generation_id, "generation_id", 128)
    if fence.tool_epoch < 0:
        raise ValueError("tool_epoch must be non-negative")


def _validate_runtime_audit_status(
    grant: LegacyGrant, *, actor_account_id: str, now: datetime
) -> None:
    status = grant.status_at(now)
    if status in {"expired", "revoked"} or (
        actor_account_id == grant.grantee_account_id and status != "active"
    ):
        raise LegacyAccessDeniedError("legacy grant is not available")


def _validate_issue(
    *,
    owner_account_id: str,
    grantee: RegisteredGranteeSnapshot,
    version: DigitalSelfVersion,
    relationship_profile: RelationshipProfile,
    allowed_items: tuple[LegacyManifestItemRef, ...],
    visibility: object,
    expires_at: datetime,
    now: datetime,
) -> None:
    _required(owner_account_id, "owner_account_id")
    _required(grantee.account_id, "grantee account_id")
    if owner_account_id == grantee.account_id:
        raise ValueError("Legacy owner and grantee must differ")
    if grantee.registered_at > now:
        raise ValueError("grantee registration snapshot is in the future")
    if version.account_id != owner_account_id or version.status != "frozen":
        raise ValueError("Legacy requires the owner's frozen Digital Self version")
    if hashlib.sha256(canonical_manifest_bytes(version.manifest)).hexdigest() != version.manifest_sha256:
        raise ValueError("Legacy frozen manifest digest does not match its snapshot")
    if relationship_profile.account_id != owner_account_id or relationship_profile.status != "approved":
        raise ValueError("Legacy requires the owner's approved relationship profile")
    if relationship_profile.sharing_scope not in {"family", "public"}:
        raise ValueError("Legacy relationship scope must not be private or unknown")
    if relationship_profile.sharing_scope == "family" and visibility == "public":
        raise ValueError("Legacy grant visibility exceeds relationship scope")
    if visibility not in {"family", "public"}:
        raise ValueError("Legacy visibility must be family or public")
    if expires_at <= now:
        raise ValueError("Legacy grant expiry must be in the future")
    if not allowed_items:
        raise ValueError("Legacy grant scope cannot be empty")
    selected = set(allowed_items)
    if len(selected) != len(allowed_items):
        raise ValueError("Legacy scope must contain unique canonical frozen manifest items")
    for item in selected:
        matching_items = [
            entry for entry in version.manifest.entries if _entry_ref(entry) == item
        ]
        if len(matching_items) != 1:
            raise ValueError("Legacy scope must contain unique canonical frozen manifest items")
        if not _scope_allows_visibility(_entry_scope(matching_items[0]), visibility):
            raise ValueError("Legacy scope contains private, unknown, or over-visible items")
    relation_ref = LegacyManifestItemRef(
        kind="relationship_profile", item_id=relationship_profile.profile_id
    )
    matching_entries = [
        entry
        for entry in version.manifest.entries
        if _entry_ref(entry) == relation_ref
        and isinstance(entry, RelationshipProfileManifestEntry)
        and entry.version_number == relationship_profile.version_number
    ]
    if len(matching_entries) != 1 or not _relationship_matches_manifest(
        relationship_profile, matching_entries[0]
    ):
        raise ValueError("approved relationship snapshot is not exact in frozen manifest")


def _entry_ref(entry: ManifestEntry) -> LegacyManifestItemRef:
    if isinstance(entry, MemoryClaimManifestEntry):
        return LegacyManifestItemRef("memory_claim", entry.claim_id)
    if isinstance(entry, PersonaTraitManifestEntry):
        return LegacyManifestItemRef("persona_trait", entry.trait_id)
    if isinstance(entry, CognitiveClaimManifestEntry):
        return LegacyManifestItemRef("cognitive_claim", entry.claim_id)
    if isinstance(entry, DecisionCaseManifestEntry):
        return LegacyManifestItemRef("decision_case", entry.case_id)
    return LegacyManifestItemRef("relationship_profile", entry.profile_id)


def _entry_scope(entry: ManifestEntry) -> str | None:
    if isinstance(entry, MemoryClaimManifestEntry):
        return entry.sensitive_domain
    if isinstance(entry, PersonaTraitManifestEntry):
        return None
    return entry.sharing_scope


def _scope_allows_visibility(scope: str | None, visibility: object) -> bool:
    return scope == "public" or (scope == "family" and visibility == "family")


def _relationship_matches_manifest(
    profile: RelationshipProfile, entry: RelationshipProfileManifestEntry
) -> bool:
    return (
        profile.profile_id,
        profile.version_number,
        profile.person_id,
        profile.relationship_id,
        profile.salutation,
        profile.tone,
        profile.advice_style,
        profile.sharing_scope,
        profile.boundaries,
    ) == (
        entry.profile_id,
        entry.version_number,
        entry.person_id,
        entry.relationship_id,
        entry.salutation,
        entry.tone,
        entry.advice_style,
        entry.sharing_scope,
        entry.boundaries,
    )


def _canonical_items(
    items: tuple[LegacyManifestItemRef, ...],
) -> tuple[LegacyManifestItemRef, ...]:
    return tuple(sorted(items, key=lambda item: (item.kind, item.item_id)))


def _item_dict(item: LegacyManifestItemRef) -> dict[str, str]:
    return {"kind": item.kind, "item_id": item.item_id}


def _validate_turn(text: str, fence: LegacyFence) -> None:
    _required(text, "actual_heard_text", 20_000)
    _required(fence.session_id, "session_id", 256)
    _required(fence.turn_id, "turn_id", 256)
    _required(fence.generation_id, "generation_id", 256)
    if fence.tool_epoch < 0:
        raise ValueError("tool_epoch must be non-negative")


def _validate_preferences(
    preferences: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    if len(preferences) > len(_PREFERENCE_VALUES):
        raise ValueError("too many Legacy shell preferences")
    canonical = tuple(sorted(preferences))
    if len({key for key, _ in canonical}) != len(canonical):
        raise ValueError("duplicate Legacy shell preference")
    if {key for key, _ in canonical} != set(_PREFERENCE_VALUES):
        raise ValueError("Legacy shell preferences must include every supported key")
    for key, value in canonical:
        if key not in _PREFERENCE_VALUES:
            raise ValueError("unknown Legacy shell preference")
        if value not in _PREFERENCE_VALUES[key]:
            raise ValueError("unknown Legacy shell preference value")
    return canonical


def _grant_digest(
    *,
    grant_id: str,
    owner_account_id: str,
    grantee_account_id: str,
    version_id: str,
    version_number: int,
    manifest_sha256: str,
    relationship_profile_id: str,
    relationship_profile_version: int,
    relationship: LegacyRelationshipSnapshot,
    scope_sha256: str,
    voice_allowed: bool,
    expires_at: datetime,
    activated_at: datetime | None,
    revoked_at: datetime | None,
    revision: int,
    created_at: datetime,
) -> str:
    return _digest(
        {
            "grant_id": grant_id,
            "owner_account_id": owner_account_id,
            "grantee_account_id": grantee_account_id,
            "version_id": version_id,
            "version_number": version_number,
            "manifest_sha256": manifest_sha256,
            "relationship_profile_id": relationship_profile_id,
            "relationship_profile_version": relationship_profile_version,
            "relationship": {
                "relationship_id": relationship.relationship_id,
                "salutation": relationship.salutation,
                "tone": relationship.tone,
                "advice_style": relationship.advice_style,
                "sharing_scope": relationship.sharing_scope,
                "boundaries": list(relationship.boundaries),
            },
            "scope_sha256": scope_sha256,
            "voice_allowed": voice_allowed,
            "expires_at": expires_at.isoformat(),
            "activated_at": activated_at.isoformat() if activated_at else None,
            "revoked_at": revoked_at.isoformat() if revoked_at else None,
            "revision": revision,
            "created_at": created_at.isoformat(),
        }
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _required(value: str, name: str, maximum: int = 256) -> None:
    if not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is required and must be at most {maximum} characters")
