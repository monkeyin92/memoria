"""Policy Engine V2 evidence seam (Agent B side).

``services/policy`` only consumes *verified evidence* through these structural
Protocol ports — caller-reported role/consent strings (``relationship_roles`` /
``consent_kinds`` on PolicyContext) are never treated as authoritative.  The
directed relationship shape follows the Identity authority. The generated
``packages/contracts`` relationship enum still needs to converge on this
Identity-owned vocabulary before cross-service wire integration is complete.
Tests otherwise use structural local adapters and fakes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    DeviceDeclaredModeValue,
)

type ConsentStatusValue = Literal[
    "active", "revoked", "expired", "disputed", "superseded"
]
type ConsentActorKindValue = Literal[
    "subject", "guardian", "family_admin", "service", "other"
]
type RelationshipStatusValue = Literal[
    "pending", "active", "suspended", "revoked", "expired", "disputed"
]
type BindingStatusValue = Literal["active", "superseded", "revoked", "expired"]

#: Complete directed relationship vocabulary owned by Identity authority.
CANONICAL_RELATION_TYPES: frozenset[str] = frozenset(
    {
        "self",
        "parent_of",
        "child_of",
        "guardian_of",
        "ward_of",
        "spouse_of",
        "sibling_of",
        "caregiver_of",
        "emergency_contact_for",
        "delegate_for",
        "beneficiary_of",
        "co_subject_of",
        "family_member_of",
    }
)


class ConsentParamsPort(Protocol):
    """Auditable parameters carried by a consent grant (CONTRACT ConsentParams)."""

    @property
    def max_session_seconds(self) -> int | None: ...

    @property
    def retention_ttl_seconds(self) -> int | None: ...

    @property
    def quiet_hours(self) -> tuple[str, str] | None: ...

    @property
    def extras(self) -> tuple[tuple[str, str], ...]: ...


class ConsentEvidencePort(Protocol):
    """Immutable, versioned consent evidence (CONTRACT ConsentEvidence)."""

    @property
    def consent_id(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def snapshot_id(self) -> str: ...

    @property
    def status(self) -> ConsentStatusValue: ...

    @property
    def subject_id(self) -> str: ...

    @property
    def resource_owner_id(self) -> str: ...

    @property
    def actor_id(self) -> str: ...

    @property
    def actor_kind(self) -> ConsentActorKindValue: ...

    @property
    def device_id(self) -> str | None: ...

    @property
    def binding_id(self) -> str: ...

    @property
    def binding_version(self) -> int: ...

    @property
    def capability(self) -> CapabilityValue: ...

    @property
    def purpose(self) -> str: ...

    @property
    def policy_version(self) -> str: ...

    @property
    def evidence_id(self) -> str: ...

    @property
    def offer_id(self) -> str: ...

    @property
    def idempotency_key(self) -> str | None: ...

    @property
    def params(self) -> ConsentParamsPort: ...

    @property
    def valid_from(self) -> datetime: ...

    @property
    def valid_until(self) -> datetime: ...

    @property
    def supersedes_consent_id(self) -> str | None: ...

    @property
    def superseded_by_consent_id(self) -> str | None: ...

    @property
    def canonical_hash(self) -> str: ...

    def is_effective_at(self, now: datetime) -> bool: ...

    def matches(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: CapabilityValue,
    ) -> bool: ...


class ConsentSnapshotEvidencePort(Protocol):
    """Current global consent snapshot head, separate from evidence source.

    ``ConsentEvidencePort.snapshot_id`` records the historical snapshot/source
    that emitted that evidence.  This port records the independently advancing
    global snapshot head locked at consumption time.  Receipts reference this
    identity/revision/hash, never the historical source id.
    """

    @property
    def snapshot_id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def canonical_hash(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def subject_id(self) -> str: ...

    @property
    def binding_id(self) -> str: ...

    @property
    def binding_version(self) -> int: ...

    @property
    def grant_refs(self) -> tuple[tuple[str, int, str], ...]: ...

    @property
    def valid_from(self) -> datetime: ...

    @property
    def valid_until(self) -> datetime: ...

    def is_current_at(self, now: datetime) -> bool: ...

    def contains(self, evidence: ConsentEvidencePort) -> bool: ...


class RelationshipEvidencePort(Protocol):
    """Versioned relationship snapshot (CONTRACT RelationshipEvidence)."""

    @property
    def relationship_id(self) -> str: ...

    @property
    def snapshot_id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def relation_type(self) -> str: ...

    @property
    def status(self) -> RelationshipStatusValue: ...

    @property
    def source_person_id(self) -> str: ...

    @property
    def target_person_id(self) -> str: ...

    @property
    def binding_id(self) -> str: ...

    @property
    def valid_from(self) -> datetime: ...

    @property
    def valid_until(self) -> datetime: ...

    @property
    def canonical_hash(self) -> str: ...

    def is_active_at(self, now: datetime) -> bool: ...


class BindingEvidencePort(Protocol):
    """Versioned device binding snapshot (CONTRACT BindingEvidence)."""

    @property
    def binding_id(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def device_id(self) -> str: ...

    @property
    def status(self) -> BindingStatusValue: ...

    @property
    def declared_mode(self) -> DeviceDeclaredModeValue: ...

    @property
    def valid_from(self) -> datetime: ...

    @property
    def valid_until(self) -> datetime: ...

    @property
    def canonical_hash(self) -> str: ...

    def is_active_at(self, now: datetime) -> bool: ...


class MembershipEvidencePort(Protocol):
    """Current family membership snapshot used by family action fences."""

    @property
    def snapshot_id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def canonical_hash(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def family_space_id(self) -> str: ...

    @property
    def family_owner_subject_id(self) -> str: ...

    @property
    def subject_ids(self) -> tuple[str, ...]: ...

    @property
    def binding_id(self) -> str: ...

    @property
    def binding_version(self) -> int: ...

    def is_active_at(self, now: datetime) -> bool: ...


class ApprovalEvidencePort(Protocol):
    """One immutable current confirm vote for a family proposal."""

    @property
    def snapshot_id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def canonical_hash(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def subject_id(self) -> str: ...

    @property
    def proposal_id(self) -> str: ...

    @property
    def proposal_revision(self) -> int: ...

    @property
    def decision(self) -> str: ...

    def is_active_at(self, now: datetime) -> bool: ...


class CaptureEvidencePort(Protocol):
    """Current capture evidence identity bound into a proposal hash."""

    @property
    def evidence_id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def canonical_hash(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def subject_id(self) -> str: ...

    @property
    def binding_id(self) -> str: ...

    @property
    def binding_version(self) -> int: ...

    def is_active_at(self, now: datetime) -> bool: ...


class ProposalEvidencePort(Protocol):
    """Current family proposal head locked by action authorization."""

    @property
    def proposal_id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def canonical_hash(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def family_space_id(self) -> str: ...

    @property
    def family_owner_subject_id(self) -> str: ...

    @property
    def required_approval_subject_ids(self) -> tuple[str, ...]: ...

    def is_active_at(self, now: datetime) -> bool: ...


def effective_consents_for(
    subject_id: str,
    binding_id: str,
    binding_version: int,
    capability: CapabilityValue,
    now: datetime,
    consents: tuple[ConsentEvidencePort, ...],
) -> tuple[ConsentEvidencePort, ...]:
    """Consent evidence effective at ``now`` and matching the subject/binding
    fence and capability.  Anything not ``active`` (revoked / expired /
    disputed / superseded) or outside its validity window is excluded."""
    return tuple(
        consent
        for consent in consents
        if consent.is_effective_at(now)
        and consent.matches(subject_id, binding_id, binding_version, capability)
    )


def active_relationship_for(
    relation_type: str,
    subject_id: str,
    now: datetime,
    relationships: tuple[RelationshipEvidencePort, ...],
) -> tuple[RelationshipEvidencePort, ...]:
    """Relationship evidence of ``relation_type`` for ``subject_id`` that is
    active at ``now``.  Pending / suspended / revoked / expired / disputed
    relationships never grant anything."""
    if relation_type not in CANONICAL_RELATION_TYPES:
        return ()
    return tuple(
        relationship
        for relationship in relationships
        if relationship.relation_type == relation_type
        and relationship.target_person_id == subject_id
        and relationship.is_active_at(now)
    )


def is_guardian_of(
    evidence: RelationshipEvidencePort,
    guardian_id: str,
    subject_id: str,
) -> bool:
    """Project only the authoritative guardian -> subject direction."""
    return (
        evidence.relation_type == "guardian_of"
        and evidence.source_person_id == guardian_id
        and evidence.target_person_id == subject_id
    )


def is_emergency_contact_for(
    evidence: RelationshipEvidencePort,
    contact_id: str,
    subject_id: str,
) -> bool:
    """Project only the authoritative emergency-contact -> subject direction."""
    return (
        evidence.relation_type == "emergency_contact_for"
        and evidence.source_person_id == contact_id
        and evidence.target_person_id == subject_id
    )


def is_delegate_for(
    evidence: RelationshipEvidencePort,
    delegate_id: str,
    subject_id: str,
) -> bool:
    """Project only the authoritative delegate -> subject direction."""
    return (
        evidence.relation_type == "delegate_for"
        and evidence.source_person_id == delegate_id
        and evidence.target_person_id == subject_id
    )


def binding_fence_ok(
    binding_id: str,
    binding_version: int,
    now: datetime,
    binding: BindingEvidencePort | None,
) -> bool:
    """The binding evidence matches the context fence and is active at ``now``."""
    return (
        binding is not None
        and binding.binding_id == binding_id
        and binding.version == binding_version
        and binding.is_active_at(now)
    )
