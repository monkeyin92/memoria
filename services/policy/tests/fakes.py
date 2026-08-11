"""Local fake implementations of the policy evidence ports (Agent B tests only).

Consent fakes match the Consent authority port; directed relationship fakes
match the Identity authority port. They never import either implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    DeviceDeclaredModeValue,
    PolicyActionResourceFence,
    RelationshipStatusValue,
)
from services.policy.context import (
    PolicyContext,
    canonical_runtime_purpose_for_capability,
)
from services.policy.evidence import (
    ApprovalEvidencePort,
    BindingEvidencePort,
    CaptureEvidencePort,
    ConsentEvidencePort,
    ConsentParamsPort,
    ConsentSnapshotEvidencePort,
    ConsentStatusValue,
    MembershipEvidencePort,
    ProposalEvidencePort,
    RelationshipEvidencePort,
)

HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class FakeConsentParams:
    max_session_seconds: int | None = None
    retention_ttl_seconds: int | None = None
    quiet_hours: tuple[str, str] | None = None
    extras: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class FakeConsentEvidence:
    consent_id: str
    version: int
    snapshot_id: str
    status: ConsentStatusValue
    subject_id: str
    resource_owner_id: str
    actor_id: str
    actor_kind: str
    device_id: str | None
    binding_id: str
    binding_version: int
    capability: CapabilityValue
    purpose: str
    policy_version: str
    evidence_id: str
    offer_id: str
    idempotency_key: str | None
    params: ConsentParamsPort
    valid_from: datetime
    valid_until: datetime
    supersedes_consent_id: str | None
    superseded_by_consent_id: str | None
    canonical_hash: str

    def is_effective_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until

    def matches(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: CapabilityValue,
    ) -> bool:
        return (
            self.subject_id == subject_id
            and self.binding_id == binding_id
            and self.binding_version == binding_version
            and self.capability == capability
        )


@dataclass(frozen=True, slots=True)
class FakeConsentSnapshotEvidence:
    snapshot_id: str
    revision: int
    canonical_hash: str
    status: str
    subject_id: str
    binding_id: str
    binding_version: int
    grant_refs: tuple[tuple[str, int, str], ...]
    valid_from: datetime
    valid_until: datetime

    def is_current_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until

    def contains(self, evidence: ConsentEvidencePort) -> bool:
        return (
            evidence.consent_id,
            evidence.version,
            evidence.canonical_hash,
        ) in self.grant_refs


@dataclass(frozen=True, slots=True)
class FakeRelationshipEvidence:
    relationship_id: str
    snapshot_id: str
    revision: int
    relation_type: str
    status: RelationshipStatusValue
    source_person_id: str
    target_person_id: str
    binding_id: str
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until


@dataclass(frozen=True, slots=True)
class FakeBindingEvidence:
    binding_id: str
    version: int
    device_id: str
    status: str
    declared_mode: DeviceDeclaredModeValue
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until


@dataclass(frozen=True, slots=True)
class FakeMembershipEvidence:
    snapshot_id: str = "membership-1"
    revision: int = 1
    canonical_hash: str = "d" * 64
    status: str = "active"
    family_space_id: str = "family-1"
    family_owner_subject_id: str = "owner-1"
    subject_ids: tuple[str, ...] = ("subject-a", "subject-b")
    binding_id: str = "binding-1"
    binding_version: int = 1

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active"


@dataclass(frozen=True, slots=True)
class FakeApprovalEvidence:
    snapshot_id: str
    revision: int
    canonical_hash: str
    subject_id: str
    proposal_id: str = "proposal-1"
    proposal_revision: int = 1
    decision: str = "confirm"
    status: str = "active"

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active"


@dataclass(frozen=True, slots=True)
class FakeCaptureEvidence:
    evidence_id: str = "capture-1"
    revision: int = 1
    canonical_hash: str = "e" * 64
    subject_id: str = "subject-a"
    binding_id: str = "binding-1"
    binding_version: int = 1
    status: str = "active"

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active"


@dataclass(frozen=True, slots=True)
class FakeProposalEvidence:
    proposal_id: str = "proposal-1"
    revision: int = 1
    canonical_hash: str = "f" * 64
    status: str = "pending"
    family_space_id: str = "family-1"
    family_owner_subject_id: str = "owner-1"
    required_approval_subject_ids: tuple[str, ...] = ("subject-a", "subject-b")

    def is_active_at(self, now: datetime) -> bool:
        return self.status in {"pending", "approvals_complete"}


def make_consent(
    *,
    consent_id: str = "consent-1",
    version: int = 1,
    snapshot_id: str = "snap-1",
    status: ConsentStatusValue = "active",
    subject_id: str = "person-adult",
    resource_owner_id: str | None = None,
    actor_id: str = "person-adult",
    actor_kind: str = "subject",
    device_id: str | None = "device-1",
    binding_id: str = "binding-1",
    binding_version: int = 1,
    capability: CapabilityValue = "voice_clone_use",
    purpose: str = "voice_clone",
    policy_version: str = "multi-subject-v2",
    evidence_id: str = "evidence-1",
    offer_id: str = "offer-1",
    idempotency_key: str | None = "idem-1",
    params: ConsentParamsPort | None = None,
    now: datetime | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    supersedes_consent_id: str | None = None,
    superseded_by_consent_id: str | None = None,
    canonical_hash: str = "a" * 64,
    data_classification: str | None = None,
) -> FakeConsentEvidence:
    moment = now or datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    if data_classification is not None:
        params = FakeConsentParams(
            extras=(("data_classification", data_classification),)
        )
    return FakeConsentEvidence(
        consent_id=consent_id,
        version=version,
        snapshot_id=snapshot_id,
        status=status,
        subject_id=subject_id,
        resource_owner_id=resource_owner_id or subject_id,
        actor_id=actor_id,
        actor_kind=actor_kind,
        device_id=device_id,
        binding_id=binding_id,
        binding_version=binding_version,
        capability=capability,
        purpose=purpose,
        policy_version=policy_version,
        evidence_id=evidence_id,
        offer_id=offer_id,
        idempotency_key=idempotency_key,
        params=params or FakeConsentParams(),
        valid_from=valid_from or (moment - HOUR),
        valid_until=valid_until or (moment + HOUR),
        supersedes_consent_id=supersedes_consent_id,
        superseded_by_consent_id=superseded_by_consent_id,
        canonical_hash=canonical_hash,
    )


def make_consent_snapshot_ref(
    *,
    snapshot_id: str = "current-consent-snapshot-1",
    revision: int = 1,
    canonical_hash: str = "8" * 64,
    status: str = "active",
    subject_id: str | None = None,
    binding_id: str | None = None,
    binding_version: int | None = None,
    consents: tuple[ConsentEvidencePort, ...],
    now: datetime | None = None,
) -> FakeConsentSnapshotEvidence:
    if not consents:
        raise ValueError("consents must not be empty")
    moment = now or datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    first = consents[0]
    return FakeConsentSnapshotEvidence(
        snapshot_id=snapshot_id,
        revision=revision,
        canonical_hash=canonical_hash,
        status=status,
        subject_id=subject_id or first.subject_id,
        binding_id=binding_id or first.binding_id,
        binding_version=(
            binding_version if binding_version is not None else first.binding_version
        ),
        grant_refs=tuple(
            sorted(
                (item.consent_id, item.version, item.canonical_hash)
                for item in consents
            )
        ),
        valid_from=moment - HOUR,
        valid_until=moment + HOUR,
    )


def make_relationship(
    *,
    relationship_id: str = "rel-1",
    snapshot_id: str = "rel-snap-1",
    revision: int = 1,
    relation_type: str = "guardian_of",
    status: RelationshipStatusValue = "active",
    source_person_id: str = "person-parent",
    target_person_id: str = "person-child",
    binding_id: str = "binding-1",
    now: datetime | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    canonical_hash: str = "b" * 64,
) -> FakeRelationshipEvidence:
    moment = now or datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    return FakeRelationshipEvidence(
        relationship_id=relationship_id,
        snapshot_id=snapshot_id,
        revision=revision,
        relation_type=relation_type,
        status=status,
        source_person_id=source_person_id,
        target_person_id=target_person_id,
        binding_id=binding_id,
        valid_from=valid_from or (moment - HOUR),
        valid_until=valid_until or (moment + HOUR),
        canonical_hash=canonical_hash,
    )


def make_binding(
    *,
    binding_id: str = "binding-1",
    version: int = 1,
    device_id: str = "device-1",
    status: str = "active",
    declared_mode: DeviceDeclaredModeValue = "self_use",
    now: datetime | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    canonical_hash: str = "c" * 64,
) -> FakeBindingEvidence:
    moment = now or datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    return FakeBindingEvidence(
        binding_id=binding_id,
        version=version,
        device_id=device_id,
        status=status,
        declared_mode=declared_mode,
        valid_from=valid_from or (moment - HOUR),
        valid_until=valid_until or (moment + HOUR),
        canonical_hash=canonical_hash,
    )


def make_context(
    *,
    actor_id: str = "person-adult",
    subject_id: str = "person-adult",
    resource_owner_id: str = "person-adult",
    device_id: str = "device-1",
    capability: CapabilityValue = "chat",
    purpose: str | None = None,
    declared_device_mode: DeviceDeclaredModeValue = "self_use",
    current_session_mode: str = "adult_companion",
    relationship_roles: frozenset[str] = frozenset({"self"}),
    subject_category: str = "adult",
    age_band: str = "adult",
    speaker_state: str = "confirmed",
    speaker_confidence: float | None = 0.99,
    consent_kinds: frozenset[str] = frozenset(),
    device_trust: str = "trusted",
    safety_state: str = "normal",
    jurisdiction: str = "CN",
    data_classification: str = "private",
    binding_id: str = "binding-1",
    binding_version: int = 1,
    session_id: str = "session-1",
    session_epoch: int = 1,
    runtime_profile_id: str = "profile-1",
    subject_revision: int = 0,
    evaluated_at: datetime | None = None,
    idempotency_key: str | None = None,
    consent_evidence: tuple[ConsentEvidencePort, ...] = (),
    consent_snapshot_evidence: tuple[ConsentSnapshotEvidencePort, ...] | None = None,
    relationship_evidence: tuple[RelationshipEvidencePort, ...] = (),
    binding_evidence: BindingEvidencePort | None = None,
    generation_id: int = 0,
    turn_id: int = 0,
    tool_epoch: int = 0,
    action_resource_fence: PolicyActionResourceFence | None = None,
    membership_evidence: MembershipEvidencePort | None = None,
    approval_evidence: tuple[ApprovalEvidencePort, ...] = (),
    capture_evidence: tuple[CaptureEvidencePort, ...] = (),
    proposal_evidence: ProposalEvidencePort | None = None,
) -> PolicyContext:
    effective_purpose = (
        canonical_runtime_purpose_for_capability(capability)
        if purpose is None
        else purpose
    )
    effective_consent_snapshots = consent_snapshot_evidence
    if (
        effective_consent_snapshots is None
        and consent_evidence
        and all(
            hasattr(item, field)
            for item in consent_evidence
            for field in ("snapshot_id", "version", "canonical_hash")
        )
    ):
        if (
            action_resource_fence is not None
            and action_resource_fence.consent_snapshot_id is not None
            and action_resource_fence.consent_snapshot_revision is not None
            and action_resource_fence.consent_snapshot_hash is not None
        ):
            effective_consent_snapshots = (
                make_consent_snapshot_ref(
                    snapshot_id=action_resource_fence.consent_snapshot_id,
                    revision=action_resource_fence.consent_snapshot_revision,
                    canonical_hash=action_resource_fence.consent_snapshot_hash,
                    consents=consent_evidence,
                    now=evaluated_at,
                ),
            )
        else:
            effective_consent_snapshots = (
                make_consent_snapshot_ref(
                    snapshot_id=consent_evidence[0].snapshot_id,
                    revision=max(item.version for item in consent_evidence),
                    canonical_hash=consent_evidence[0].canonical_hash,
                    consents=consent_evidence,
                    now=evaluated_at,
                ),
            )
    return PolicyContext(
        actor_id=actor_id,
        subject_id=subject_id,
        resource_owner_id=resource_owner_id,
        device_id=device_id,
        capability=capability,
        purpose=effective_purpose,
        declared_device_mode=declared_device_mode,
        current_session_mode=current_session_mode,  # type: ignore[arg-type]
        relationship_roles=relationship_roles,
        subject_category=subject_category,  # type: ignore[arg-type]
        age_band=age_band,  # type: ignore[arg-type]
        speaker_state=speaker_state,  # type: ignore[arg-type]
        speaker_confidence=speaker_confidence,
        consent_kinds=consent_kinds,
        device_trust=device_trust,  # type: ignore[arg-type]
        safety_state=safety_state,  # type: ignore[arg-type]
        jurisdiction=jurisdiction,
        data_classification=data_classification,  # type: ignore[arg-type]
        binding_id=binding_id,
        binding_version=binding_version,
        session_id=session_id,
        session_epoch=session_epoch,
        runtime_profile_id=runtime_profile_id,
        subject_revision=subject_revision,
        evaluated_at=evaluated_at or datetime(2026, 8, 9, 8, 0, tzinfo=UTC),
        idempotency_key=idempotency_key,
        consent_evidence=consent_evidence,
        consent_snapshot_evidence=effective_consent_snapshots or (),
        relationship_evidence=relationship_evidence,
        binding_evidence=binding_evidence,
        generation_id=generation_id,
        turn_id=turn_id,
        tool_epoch=tool_epoch,
        action_resource_fence=action_resource_fence,
        membership_evidence=membership_evidence,
        approval_evidence=approval_evidence,
        capture_evidence=capture_evidence,
        proposal_evidence=proposal_evidence,
    )


def minor_governance_evidence(
    *,
    now: datetime | None = None,
    subject_id: str = "person-child",
    guardian_id: str = "person-parent",
    snapshot_id: str = "snap-child-1",
    binding_id: str = "binding-1",
    binding_version: int = 1,
) -> tuple[FakeRelationshipEvidence, FakeConsentEvidence, FakeBindingEvidence]:
    """Build (guardian relationship, generic minor consent, active binding)."""
    moment = now or datetime(2026, 8, 9, 8, 0, tzinfo=UTC)
    relationship = make_relationship(
        relationship_id="rel-guardian-1",
        snapshot_id="rel-snap-guardian-1",
        relation_type="guardian_of",
        source_person_id=guardian_id,
        target_person_id=subject_id,
        now=moment,
    )
    consent = make_consent(
        consent_id="consent-minor-1",
        snapshot_id=snapshot_id,
        subject_id=subject_id,
        resource_owner_id=subject_id,
        actor_id=guardian_id,
        actor_kind="guardian",
        binding_id=binding_id,
        binding_version=binding_version,
        capability="chat",
        purpose="user_request",
        now=moment,
    )
    binding = make_binding(now=moment)
    return relationship, consent, binding
