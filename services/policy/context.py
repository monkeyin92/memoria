"""PolicyContext: strictly validated decision context and canonical hash."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    Capability,
    CapabilityValue,
    DataClassification,
    DeviceDeclaredModeValue,
    DeviceTrust,
    PolicyActionResourceFence,
    Purpose,
    PurposeValue,
    SafetyState,
    ServiceModeValue,
    SpeakerStateValue,
    SubjectCategoryValue,
)

from services.policy.action_fence import (
    build_default_action_resource_fence,
    verify_action_resource_fence,
)
from services.policy.evidence import (
    CANONICAL_RELATION_TYPES,
    ApprovalEvidencePort,
    BindingEvidencePort,
    CaptureEvidencePort,
    ConsentEvidencePort,
    ConsentSnapshotEvidencePort,
    MembershipEvidencePort,
    ProposalEvidencePort,
    RelationshipEvidencePort,
)

#: Purpose is a restricted set (CONTRACT: "严格枚举或受限集合").  Values cover
#: the existing callers plus the canonical consent purposes.
PURPOSE_VALUES: Final[frozenset[str]] = frozenset(Purpose.values())

#: CONTRACT device_trust literal.
DEVICE_TRUST_VALUES: Final[frozenset[str]] = frozenset(DeviceTrust.values())

SAFETY_STATE_VALUES: Final[frozenset[str]] = frozenset(SafetyState.values())

DATA_CLASSIFICATION_VALUES: Final[frozenset[str]] = frozenset(
    DataClassification.values()
)

_MAX_ID_LENGTH: Final[int] = 256

_ACTION_PURPOSE_PAIRS: Final[dict[str, PurposeValue]] = {
    "memory_capture": "memory_capture",
    "memory_promotion": "memory_promotion",
    "family_shared_memory_proposal": "family_shared_memory_proposal",
    "family_shared_memory_approval": "family_shared_memory_approval",
    "family_shared_memory_promotion": "family_shared_memory_promotion",
}

RUNTIME_PURPOSE_BY_CAPABILITY: Final[dict[CapabilityValue, PurposeValue]] = {
    "chat": "user_request",
    "tutor": "user_request",
    "english_practice": "user_request",
    "memory_capture": "memory_capture",
    "memory_promotion": "memory_promotion",
    "family_shared_memory_proposal": "family_shared_memory_proposal",
    "family_shared_memory_approval": "family_shared_memory_approval",
    "family_shared_memory_promotion": "family_shared_memory_promotion",
    "memory_recall_private": "memory_recall",
    "guardian_summary_view": "guardian_summary",
    "voice_profile_create": "voice_profile",
    "voice_clone_use": "voice_clone",
    "digital_self_preview": "digital_self",
    "legacy_grant_create": "legacy",
    "payment": "payment",
    "raw_audio_retention": "raw_audio",
    "model_training_contribution": "model_training",
    "crisis_notification": "crisis_response",
    "device_ownership_transfer": "device_transfer",
}


class CapabilityScope(StrEnum):
    """Where a capability may be authorized and minted."""

    SESSION_LEVEL = "session_level"
    RESOURCE_SCOPED_ACTION = "resource_scoped_action"


_RESOURCE_SCOPED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
    }
)


def capability_scope_for(capability: object) -> CapabilityScope:
    """Return the canonical mint/consume scope; unknown capabilities fail closed."""
    if not isinstance(capability, str) or Capability.from_value(capability) is None:
        raise ValueError(f"capability has invalid value {capability!r}")
    if capability in _RESOURCE_SCOPED_ACTIONS:
        return CapabilityScope.RESOURCE_SCOPED_ACTION
    return CapabilityScope.SESSION_LEVEL


def is_resource_scoped_action(capability: object) -> bool:
    return capability_scope_for(capability) is CapabilityScope.RESOURCE_SCOPED_ACTION


def canonical_purpose_for_capability(
    capability: object,
    *,
    fallback_purpose: object | None = None,
) -> PurposeValue:
    """Resolve one generated canonical purpose without consumer-side maps.

    The five generated action pairs are immutable.  Other capabilities require
    an explicit generated fallback purpose and may not borrow an action-pair
    purpose.  This keeps profile-time callers from substituting
    ``runtime_profile_issue`` for resource-scoped actions.
    """
    if not isinstance(capability, str) or Capability.from_value(capability) is None:
        raise ValueError(f"capability has invalid value {capability!r}")
    paired = _ACTION_PURPOSE_PAIRS.get(capability)
    if paired is not None:
        if fallback_purpose is not None and fallback_purpose != paired:
            raise ValueError(
                "canonical action purpose mismatch: "
                f"capability {capability!r} requires {paired!r}"
            )
        return paired
    if fallback_purpose is None:
        raise ValueError("fallback_purpose is required for non-action capability")
    if (
        not isinstance(fallback_purpose, str)
        or Purpose.from_value(fallback_purpose) is None
    ):
        raise ValueError(f"purpose has invalid value {fallback_purpose!r}")
    if fallback_purpose in _ACTION_PURPOSE_PAIRS.values():
        raise ValueError("action purpose cannot be used by a different capability")
    return cast(PurposeValue, fallback_purpose)


def canonical_runtime_purpose_for_capability(
    capability: object,
) -> PurposeValue:
    """Resolve the one semantic purpose for a post-issuance capability."""
    if not isinstance(capability, str) or Capability.from_value(capability) is None:
        raise ValueError(f"capability has invalid value {capability!r}")
    purpose = RUNTIME_PURPOSE_BY_CAPABILITY.get(cast(CapabilityValue, capability))
    if purpose is None:
        raise ValueError(f"runtime purpose is not configured for {capability!r}")
    return purpose

_CONSENT_EVIDENCE_ATTRS: Final[tuple[str, ...]] = (
    "consent_id",
    "version",
    "snapshot_id",
    "status",
    "subject_id",
    "resource_owner_id",
    "actor_id",
    "actor_kind",
    "device_id",
    "binding_id",
    "binding_version",
    "capability",
    "purpose",
    "policy_version",
    "evidence_id",
    "offer_id",
    "idempotency_key",
    "params",
    "valid_from",
    "valid_until",
    "supersedes_consent_id",
    "superseded_by_consent_id",
    "canonical_hash",
    "is_effective_at",
    "matches",
)

_CONSENT_SNAPSHOT_EVIDENCE_ATTRS: Final[tuple[str, ...]] = (
    "snapshot_id",
    "revision",
    "canonical_hash",
    "status",
    "subject_id",
    "binding_id",
    "binding_version",
    "grant_refs",
    "valid_from",
    "valid_until",
    "is_current_at",
    "contains",
)

_RELATIONSHIP_EVIDENCE_ATTRS: Final[tuple[str, ...]] = (
    "relationship_id",
    "snapshot_id",
    "revision",
    "relation_type",
    "status",
    "source_person_id",
    "target_person_id",
    "binding_id",
    "valid_from",
    "valid_until",
    "canonical_hash",
    "is_active_at",
)

_BINDING_EVIDENCE_ATTRS: Final[tuple[str, ...]] = (
    "binding_id",
    "version",
    "device_id",
    "status",
    "declared_mode",
    "valid_from",
    "valid_until",
    "canonical_hash",
    "is_active_at",
)


def _validate_id(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if len(value) > _MAX_ID_LENGTH:
        raise ValueError(f"{name} is too long (max {_MAX_ID_LENGTH} characters)")
    return value


def _validate_counter(value: object, name: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, not bool/float")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _validate_aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be an aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be an aware datetime (UTC or offset)")
    return value


def _validate_enum(value: object, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} has invalid value {value!r}")
    return value


def _validate_evidence_tuple(
    value: object, name: str, attrs: tuple[str, ...]
) -> tuple[object, ...]:
    if value is None:
        return ()
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{name} must be a tuple of evidence ports")
    result = tuple(value)
    for element in result:
        if not all(hasattr(element, attr) for attr in attrs):
            raise ValueError(f"{name} contains an object missing evidence fields")
    return result


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Strictly validated decision context (public class name preserved).

    Construction raises ``ValueError`` on: bool epoch/version values, naive
    datetimes, empty/overlong IDs, out-of-range speaker confidence, invalid
    capability / contract enums / purpose / device_trust / safety_state /
    data_classification, and evidence tuples containing non-port objects.
    ``idempotency_key`` is optional; when present it is a non-empty bounded id
    consumed by PolicyEngine receipt-id derivation.
    ``relationship_roles`` / ``consent_kinds`` are preserved as compatibility
    fields but are never authoritative for sensitive capability decisions.
    """

    actor_id: str
    subject_id: str | None
    resource_owner_id: str | None
    device_id: str
    capability: CapabilityValue
    purpose: PurposeValue
    declared_device_mode: DeviceDeclaredModeValue
    current_session_mode: ServiceModeValue
    subject_category: SubjectCategoryValue
    age_band: AgeBandValue
    speaker_state: SpeakerStateValue
    speaker_confidence: float | None
    device_trust: str
    safety_state: str
    jurisdiction: str
    data_classification: str
    binding_id: str
    binding_version: int
    session_id: str
    session_epoch: int
    runtime_profile_id: str
    subject_revision: int
    evaluated_at: datetime
    idempotency_key: str | None = None
    relationship_roles: frozenset[str] = frozenset()
    consent_kinds: frozenset[str] = frozenset()
    consent_evidence: tuple[ConsentEvidencePort, ...] = ()
    consent_snapshot_evidence: tuple[ConsentSnapshotEvidencePort, ...] = ()
    relationship_evidence: tuple[RelationshipEvidencePort, ...] = ()
    binding_evidence: BindingEvidencePort | None = None
    generation_id: int = 0
    turn_id: int = 0
    tool_epoch: int = 0
    action_resource_fence: PolicyActionResourceFence | None = None
    membership_evidence: MembershipEvidencePort | None = None
    approval_evidence: tuple[ApprovalEvidencePort, ...] = ()
    capture_evidence: tuple[CaptureEvidencePort, ...] = ()
    proposal_evidence: ProposalEvidencePort | None = None

    def __post_init__(self) -> None:
        _validate_id(self.actor_id, "actor_id")
        if self.subject_id is not None:
            _validate_id(self.subject_id, "subject_id")
        if self.resource_owner_id is not None:
            _validate_id(self.resource_owner_id, "resource_owner_id")
        _validate_id(self.device_id, "device_id")
        if not isinstance(self.capability, str) or Capability.from_value(
            self.capability
        ) is None:
            raise ValueError(f"capability has invalid value {self.capability!r}")
        _validate_enum(self.purpose, "purpose", PURPOSE_VALUES)
        resolved_purpose = canonical_purpose_for_capability(
            self.capability,
            fallback_purpose=self.purpose,
        )
        if self.purpose != resolved_purpose:
            raise ValueError("capability and purpose are not the canonical action pair")
        if is_resource_scoped_action(self.capability) and self.action_resource_fence is None:
            raise ValueError(
                "resource-scoped action requires an explicit canonical action_resource_fence"
            )
        _validate_enum(
            self.declared_device_mode,
            "declared_device_mode",
            frozenset({"parent_for_child", "self_use", "child_for_parent", "family_shared"}),
        )
        _validate_enum(
            self.current_session_mode,
            "current_session_mode",
            frozenset(
                {
                    "student_minor",
                    "adult_companion",
                    "senior_companion",
                    "family_shared",
                    "adult_archive",
                    "self_preview",
                    "legacy_access",
                    "unknown_safe",
                }
            ),
        )
        _validate_enum(
            self.subject_category,
            "subject_category",
            frozenset({"unknown", "minor", "adult"}),
        )
        _validate_enum(
            self.age_band,
            "age_band",
            frozenset({"unknown", "under_14", "14_17", "adult"}),
        )
        _validate_enum(
            self.speaker_state,
            "speaker_state",
            frozenset({"unknown", "unconfirmed", "confirmed"}),
        )
        if not isinstance(self.relationship_roles, frozenset) or any(
            not isinstance(role, str) for role in self.relationship_roles
        ):
            raise ValueError("relationship_roles must be a frozenset of strings")
        if not isinstance(self.consent_kinds, frozenset) or any(
            not isinstance(kind, str) for kind in self.consent_kinds
        ):
            raise ValueError("consent_kinds must be a frozenset of strings")
        if self.speaker_confidence is not None:
            if isinstance(self.speaker_confidence, bool) or not isinstance(
                self.speaker_confidence, (int, float)
            ):
                raise ValueError("speaker_confidence must be a float in [0, 1]")
            if not 0.0 <= float(self.speaker_confidence) <= 1.0:
                raise ValueError("speaker_confidence must be within [0, 1]")
        _validate_enum(self.device_trust, "device_trust", DEVICE_TRUST_VALUES)
        _validate_enum(self.safety_state, "safety_state", SAFETY_STATE_VALUES)
        _validate_enum(
            self.data_classification,
            "data_classification",
            DATA_CLASSIFICATION_VALUES,
        )
        _validate_id(self.jurisdiction, "jurisdiction")
        _validate_id(self.binding_id, "binding_id")
        object.__setattr__(
            self,
            "binding_version",
            _validate_counter(self.binding_version, "binding_version", minimum=1),
        )
        _validate_id(self.session_id, "session_id")
        object.__setattr__(
            self,
            "session_epoch",
            _validate_counter(self.session_epoch, "session_epoch", minimum=1),
        )
        _validate_id(self.runtime_profile_id, "runtime_profile_id")
        object.__setattr__(
            self,
            "subject_revision",
            _validate_counter(self.subject_revision, "subject_revision", minimum=0),
        )
        _validate_aware_datetime(self.evaluated_at, "evaluated_at")
        for name in ("generation_id", "turn_id", "tool_epoch"):
            _validate_counter(getattr(self, name), name, minimum=0)
        if self.idempotency_key is not None:
            _validate_id(self.idempotency_key, "idempotency_key")
        object.__setattr__(
            self,
            "consent_evidence",
            _validate_evidence_tuple(
                self.consent_evidence,
                "consent_evidence",
                _CONSENT_EVIDENCE_ATTRS,
            ),
        )
        object.__setattr__(
            self,
            "relationship_evidence",
            _validate_evidence_tuple(
                self.relationship_evidence,
                "relationship_evidence",
                _RELATIONSHIP_EVIDENCE_ATTRS,
            ),
        )
        object.__setattr__(
            self,
            "consent_snapshot_evidence",
            _validate_evidence_tuple(
                self.consent_snapshot_evidence,
                "consent_snapshot_evidence",
                _CONSENT_SNAPSHOT_EVIDENCE_ATTRS,
            ),
        )
        for snapshot in self.consent_snapshot_evidence:
            _validate_counter(snapshot.revision, "consent snapshot revision", minimum=1)
            _validate_counter(
                snapshot.binding_version,
                "consent snapshot binding_version",
                minimum=1,
            )
        for evidence in self.relationship_evidence:
            if evidence.relation_type not in CANONICAL_RELATION_TYPES:
                raise ValueError(
                    "relationship_evidence relation_type is not Identity canonical"
                )
            _validate_id(evidence.source_person_id, "relationship source_person_id")
            _validate_id(evidence.target_person_id, "relationship target_person_id")
            _validate_counter(
                evidence.revision,
                "relationship revision",
                minimum=1,
            )
        if self.binding_evidence is not None and not all(
            hasattr(self.binding_evidence, attr)
            for attr in _BINDING_EVIDENCE_ATTRS
        ):
            raise ValueError(
                "binding_evidence must be a BindingEvidencePort object"
            )
        if self.action_resource_fence is not None:
            fence = self.action_resource_fence
            if not verify_action_resource_fence(fence):
                raise ValueError("action_resource_fence hashes are invalid")
            if (fence.capability.value, fence.purpose.value) != (
                self.capability,
                self.purpose,
            ):
                raise ValueError("action_resource_fence capability/purpose mismatch")
            if (fence.generation_id, fence.turn_id, fence.tool_epoch) != (
                self.generation_id,
                self.turn_id,
                self.tool_epoch,
            ):
                raise ValueError("action_resource_fence generation/turn/tool mismatch")
            if not fence.issued_at <= self.evaluated_at < fence.valid_until:
                raise ValueError("action_resource_fence is outside its validity window")
        if self.membership_evidence is not None and not all(
            hasattr(self.membership_evidence, attr)
            for attr in (
                "snapshot_id",
                "revision",
                "canonical_hash",
                "status",
                "family_space_id",
                "family_owner_subject_id",
                "subject_ids",
                "binding_id",
                "binding_version",
                "is_active_at",
            )
        ):
            raise ValueError("membership_evidence is not a membership evidence port")
        object.__setattr__(
            self,
            "approval_evidence",
            _validate_evidence_tuple(
                self.approval_evidence,
                "approval_evidence",
                (
                    "snapshot_id",
                    "revision",
                    "canonical_hash",
                    "status",
                    "subject_id",
                    "proposal_id",
                    "proposal_revision",
                    "decision",
                    "is_active_at",
                ),
            ),
        )
        object.__setattr__(
            self,
            "capture_evidence",
            _validate_evidence_tuple(
                self.capture_evidence,
                "capture_evidence",
                (
                    "evidence_id",
                    "revision",
                    "canonical_hash",
                    "status",
                    "subject_id",
                    "binding_id",
                    "binding_version",
                    "is_active_at",
                ),
            ),
        )
        if self.proposal_evidence is not None and not all(
            hasattr(self.proposal_evidence, attr)
            for attr in (
                "proposal_id",
                "revision",
                "canonical_hash",
                "status",
                "family_space_id",
                "family_owner_subject_id",
                "required_approval_subject_ids",
                "is_active_at",
            )
        ):
            raise ValueError("proposal_evidence is not a proposal evidence port")


#: Canonical top-level field order for the context hash (CONTRACT hash list).
CANONICAL_FIELD_ORDER: Final[tuple[str, ...]] = (
    "actor_id",
    "subject_id",
    "resource_owner_id",
    "device_id",
    "capability",
    "purpose",
    "declared_device_mode",
    "current_session_mode",
    "subject_category",
    "age_band",
    "speaker_state",
    "speaker_confidence",
    "device_trust",
    "safety_state",
    "jurisdiction",
    "data_classification",
    "consent_evidence",
    "consent_snapshot_evidence",
    "relationship_evidence",
    "binding_evidence",
    "binding_id",
    "binding_version",
    "session_id",
    "session_epoch",
    "runtime_profile_id",
    "subject_revision",
    "evaluated_at",
    "generation_id",
    "turn_id",
    "tool_epoch",
    "action_resource_fence",
    "membership_evidence",
    "approval_evidence",
    "capture_evidence",
    "proposal_evidence",
)


def effective_action_resource_fence(context: PolicyContext) -> PolicyActionResourceFence:
    """Return the explicit generated fence or a deterministic compatibility fence."""
    if context.action_resource_fence is not None:
        return context.action_resource_fence
    current_snapshot = (
        context.consent_snapshot_evidence[0]
        if len(context.consent_snapshot_evidence) == 1
        else None
    )
    return build_default_action_resource_fence(
        capability=context.capability,
        purpose=context.purpose,
        actor_id=context.actor_id,
        subject_id=context.subject_id,
        resource_owner_id=context.resource_owner_id,
        device_id=context.device_id,
        binding_id=context.binding_id,
        binding_version=context.binding_version,
        session_id=context.session_id,
        session_epoch=context.session_epoch,
        generation_id=context.generation_id,
        turn_id=context.turn_id,
        tool_epoch=context.tool_epoch,
        evaluated_at=context.evaluated_at,
        consent_snapshot_id=(
            current_snapshot.snapshot_id if current_snapshot is not None else None
        ),
        consent_snapshot_revision=(
            current_snapshot.revision if current_snapshot is not None else None
        ),
        consent_snapshot_hash=(
            current_snapshot.canonical_hash if current_snapshot is not None else None
        ),
    )


def context_hash(context: PolicyContext) -> str:
    """Canonical sha256 of every authoritative decision input and evidence.

    Only caller-compatibility/transport fields ``relationship_roles``,
    ``consent_kinds`` and ``idempotency_key`` are excluded. They are not
    authoritative policy evidence and cannot alter an authorization result.
    """
    consent_entries = [
        {
            "consent_id": consent.consent_id,
            "snapshot_id": consent.snapshot_id,
            "revision": consent.version,
            "status": consent.status,
            "subject_id": consent.subject_id,
            "resource_owner_id": consent.resource_owner_id,
            "actor_id": consent.actor_id,
            "actor_kind": consent.actor_kind,
            "device_id": consent.device_id,
            "binding_id": consent.binding_id,
            "binding_version": consent.binding_version,
            "capability": consent.capability,
            "purpose": consent.purpose,
            "policy_version": consent.policy_version,
            "evidence_id": consent.evidence_id,
            "offer_id": consent.offer_id,
            "idempotency_key": consent.idempotency_key,
            "params": {
                "max_session_seconds": consent.params.max_session_seconds,
                "retention_ttl_seconds": consent.params.retention_ttl_seconds,
                "quiet_hours": consent.params.quiet_hours,
                "extras": consent.params.extras,
            },
            "valid_from": consent.valid_from.isoformat(),
            "valid_until": consent.valid_until.isoformat(),
            "supersedes_consent_id": consent.supersedes_consent_id,
            "superseded_by_consent_id": consent.superseded_by_consent_id,
            "canonical_hash": consent.canonical_hash,
        }
        for consent in sorted(
            context.consent_evidence,
            key=lambda evidence: (
                evidence.snapshot_id,
                evidence.version,
                evidence.consent_id,
                evidence.canonical_hash,
            ),
        )
    ]
    consent_snapshot_entries = [
        {
            "snapshot_id": item.snapshot_id,
            "revision": item.revision,
            "canonical_hash": item.canonical_hash,
            "status": item.status,
            "subject_id": item.subject_id,
            "binding_id": item.binding_id,
            "binding_version": item.binding_version,
            "grant_refs": sorted(item.grant_refs),
            "valid_from": item.valid_from.isoformat(),
            "valid_until": item.valid_until.isoformat(),
        }
        for item in sorted(
            context.consent_snapshot_evidence,
            key=lambda value: (value.snapshot_id, value.revision, value.canonical_hash),
        )
    ]
    relationship_entries = [
        {
            "relationship_id": relationship.relationship_id,
            "snapshot_id": relationship.snapshot_id,
            "revision": relationship.revision,
            "relation_type": relationship.relation_type,
            "status": relationship.status,
            "source_person_id": relationship.source_person_id,
            "target_person_id": relationship.target_person_id,
            "binding_id": relationship.binding_id,
            "valid_from": relationship.valid_from.isoformat(),
            "valid_until": relationship.valid_until.isoformat(),
            "canonical_hash": relationship.canonical_hash,
        }
        for relationship in sorted(
            context.relationship_evidence,
            key=lambda evidence: (
                evidence.snapshot_id,
                evidence.revision,
                evidence.relationship_id,
                evidence.canonical_hash,
            ),
        )
    ]
    binding_entry = (
        None
        if context.binding_evidence is None
        else {
            "binding_id": context.binding_evidence.binding_id,
            "version": context.binding_evidence.version,
            "device_id": context.binding_evidence.device_id,
            "status": context.binding_evidence.status,
            "declared_mode": context.binding_evidence.declared_mode,
            "valid_from": context.binding_evidence.valid_from.isoformat(),
            "valid_until": context.binding_evidence.valid_until.isoformat(),
            "canonical_hash": context.binding_evidence.canonical_hash,
        }
    )
    action_fence = effective_action_resource_fence(context)
    membership_entry = (
        None
        if context.membership_evidence is None
        else {
            "snapshot_id": context.membership_evidence.snapshot_id,
            "revision": context.membership_evidence.revision,
            "canonical_hash": context.membership_evidence.canonical_hash,
            "status": context.membership_evidence.status,
            "family_space_id": context.membership_evidence.family_space_id,
            "family_owner_subject_id": context.membership_evidence.family_owner_subject_id,
            "subject_ids": sorted(context.membership_evidence.subject_ids),
            "binding_id": context.membership_evidence.binding_id,
            "binding_version": context.membership_evidence.binding_version,
        }
    )
    approval_entries = [
        {
            "snapshot_id": item.snapshot_id,
            "revision": item.revision,
            "canonical_hash": item.canonical_hash,
            "status": item.status,
            "subject_id": item.subject_id,
            "proposal_id": item.proposal_id,
            "proposal_revision": item.proposal_revision,
            "decision": item.decision,
        }
        for item in sorted(context.approval_evidence, key=lambda value: value.subject_id)
    ]
    capture_entries = [
        {
            "evidence_id": item.evidence_id,
            "revision": item.revision,
            "canonical_hash": item.canonical_hash,
            "status": item.status,
            "subject_id": item.subject_id,
            "binding_id": item.binding_id,
            "binding_version": item.binding_version,
        }
        for item in sorted(context.capture_evidence, key=lambda value: value.evidence_id)
    ]
    proposal_entry = (
        None
        if context.proposal_evidence is None
        else {
            "proposal_id": context.proposal_evidence.proposal_id,
            "revision": context.proposal_evidence.revision,
            "canonical_hash": context.proposal_evidence.canonical_hash,
            "status": context.proposal_evidence.status,
            "family_space_id": context.proposal_evidence.family_space_id,
            "family_owner_subject_id": context.proposal_evidence.family_owner_subject_id,
            "required_approval_subject_ids": sorted(
                context.proposal_evidence.required_approval_subject_ids
            ),
        }
    )
    payload = {
        "actor_id": context.actor_id,
        "subject_id": context.subject_id,
        "resource_owner_id": context.resource_owner_id,
        "device_id": context.device_id,
        "capability": context.capability,
        "purpose": context.purpose,
        "declared_device_mode": context.declared_device_mode,
        "current_session_mode": context.current_session_mode,
        "subject_category": context.subject_category,
        "age_band": context.age_band,
        "speaker_state": context.speaker_state,
        "speaker_confidence": context.speaker_confidence,
        "device_trust": context.device_trust,
        "safety_state": context.safety_state,
        "jurisdiction": context.jurisdiction,
        "data_classification": context.data_classification,
        "consent_evidence": consent_entries,
        "consent_snapshot_evidence": consent_snapshot_entries,
        "relationship_evidence": relationship_entries,
        "binding_evidence": binding_entry,
        "binding_id": context.binding_id,
        "binding_version": context.binding_version,
        "session_id": context.session_id,
        "session_epoch": context.session_epoch,
        "runtime_profile_id": context.runtime_profile_id,
        "subject_revision": context.subject_revision,
        "evaluated_at": context.evaluated_at.isoformat(),
        "generation_id": context.generation_id,
        "turn_id": context.turn_id,
        "tool_epoch": context.tool_epoch,
        "action_resource_fence": action_fence.model_dump(mode="json"),
        "membership_evidence": membership_entry,
        "approval_evidence": approval_entries,
        "capture_evidence": capture_entries,
        "proposal_evidence": proposal_entry,
    }
    ordered = {key: payload[key] for key in CANONICAL_FIELD_ORDER}
    encoded = json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
