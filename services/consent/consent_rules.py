"""Fail-closed consent rules, rule vocabulary and pure lifecycle helpers.

``ConsentAuthority`` and ``AsyncConsentAuthority`` in
``services.consent.authority`` own transactions, stores and ordering; the
functions here decide rules 1-7 over already-resolved evidence and build the
append-only snapshot and idempotency-replay values.  The whitelists and the
rule rejection live here too, so this module never imports the authorities
that apply the rules.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from services.consent.events import AuditEntry, ConsentOutboxEvent
from services.consent.evidence import (
    ActorKind,
    BindingEvidence,
    ConsentEvidence,
    ConsentOffer,
    ConsentSnapshot,
    RelationshipEvidence,
    canonical_json,
    compute_canonical_hash,
    is_guardian_of,
    is_parent_of,
)
from services.consent.store import (
    AsyncConsentUnitOfWork,
    ConsentConflictError,
    ConsentConsistencyError,
    ConsentUnitOfWork,
    IdempotencyRecord,
)

if TYPE_CHECKING:
    from services.consent.authority import SubjectProof


#: Sensitive adult capabilities: self-grant only (rule 2).
SENSITIVE_ADULT_CAPABILITIES: frozenset[str] = frozenset(
    {
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "payment",
        "raw_audio_retention",
        "model_training_contribution",
        "device_ownership_transfer",
    }
)

#: Guardian whitelist for minors (rule 3). voice_profile_create is voice-print
#: enrollment only, never cloning (cloning is voice_clone_use, adult-only).
GUARDIAN_WHITELIST: frozenset[str] = frozenset(
    {
        "chat",
        "tutor",
        "english_practice",
        "voice_profile_create",
        "memory_capture",
        "memory_recall_private",
        "guardian_summary_view",
    }
)

#: Capabilities with no standing-grant path through the authority (fail closed).
NOT_GRANTABLE: frozenset[str] = frozenset({"crisis_notification"})

#: Relationship types that authorize guardian grants (rule 3).
GUARDIAN_RELATION_TYPES: frozenset[str] = frozenset({"guardian_of", "parent_of"})

#: What an adult child may consent to on behalf of the elderly parent a device
#: serves: that parent's own memory on that device, and nothing sensitive.
DELEGATE_WHITELIST: frozenset[str] = frozenset({"memory_capture", "memory_recall_private"})


class ConsentDeniedError(PermissionError):
    """A consent rule rejected the operation (fail closed)."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ConsentOperationResult:
    """Everything written by one authority operation."""

    evidence: ConsentEvidence
    snapshot: ConsentSnapshot
    outbox_event: ConsentOutboxEvent
    audit_entry: AuditEntry


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _guardian_relationship(
    *,
    actor_id: str,
    subject_id: str,
    binding: BindingEvidence,
    relationships: tuple[RelationshipEvidence, ...],
    now: datetime,
) -> RelationshipEvidence | None:
    for relationship in relationships:
        if (
            relationship.relation_type in GUARDIAN_RELATION_TYPES
            and relationship.is_active_at(now)
            and (
                is_guardian_of(relationship, guardian_id=actor_id, subject_id=subject_id)
                or is_parent_of(relationship, parent_id=actor_id, subject_id=subject_id)
            )
            and relationship.binding_id == binding.binding_id
        ):
            return relationship
    return None


def _delegate_relationship(
    *,
    actor_id: str,
    subject_id: str,
    binding: BindingEvidence,
    relationships: tuple[RelationshipEvidence, ...],
    now: datetime,
) -> RelationshipEvidence | None:
    for relationship in relationships:
        if (
            relationship.relation_type == "delegate_for"
            and relationship.is_active_at(now)
            and relationship.source_person_id == actor_id
            and relationship.target_person_id == subject_id
            and relationship.binding_id == binding.binding_id
        ):
            return relationship
    return None


def verify_grant(
    *,
    offer: ConsentOffer,
    subject: SubjectProof,
    actor_kind: ActorKind | None,
    binding: BindingEvidence,
    relationships: tuple[RelationshipEvidence, ...],
    now: datetime,
) -> ActorKind:
    """Evaluate rules 1-4 over payloads already verified by a resolver."""
    if subject.subject_category == "unknown":
        raise ConsentDeniedError("unknown_subject", "subject category is unknown")
    guardian_declared_minor = (
        subject.subject_category == "minor"
        and subject.age_evidence_status == "unverified"
        and actor_kind == "guardian"
        and offer.capability in GUARDIAN_WHITELIST
    )
    if subject.age_evidence_status != "verified" and not guardian_declared_minor:
        # A child's age is what the guardian declared: nobody can verify it on
        # the child's behalf, and a declared minor only ever gets the stricter
        # minor rules. Every other grant still needs verified age evidence.
        raise ConsentDeniedError(
            "age_evidence_unverified", "verified age evidence is required for any grant"
        )
    if offer.capability in NOT_GRANTABLE:
        raise ConsentDeniedError(
            "capability_not_grantable", f"{offer.capability} has no grant path"
        )

    kind = actor_kind
    if kind is None:
        if offer.actor_id == offer.subject_id == offer.resource_owner_id:
            kind = "subject"
        elif (
            _guardian_relationship(
                actor_id=offer.actor_id,
                subject_id=offer.subject_id,
                binding=binding,
                relationships=relationships,
                now=now,
            )
            is not None
        ):
            kind = "guardian"
        else:
            raise ConsentDeniedError(
                "actor_kind_unverifiable", "cannot derive actor kind from authoritative evidence"
            )

    if kind == "family_admin":
        raise ConsentDeniedError(
            "family_admin_cannot_grant", "family_admin is not a data subject proxy"
        )
    if kind not in {"subject", "guardian", "delegate"}:
        raise ConsentDeniedError("actor_kind_not_authorized", f"actor kind {kind!r} cannot grant")

    if kind == "subject":
        if not (offer.actor_id == offer.subject_id == offer.resource_owner_id):
            raise ConsentDeniedError(
                "subject_claim_mismatch",
                "subject grant requires actor == subject == resource owner",
            )
    elif kind == "delegate":
        # An adult child consenting for the elderly parent a device serves.
        # Recorded as a delegate grant, never as the parent's own consent.
        if offer.resource_owner_id != offer.subject_id:
            raise ConsentDeniedError(
                "resource_owner_mismatch", "delegate grants require resource_owner == subject"
            )
        if subject.subject_category != "adult":
            raise ConsentDeniedError(
                "delegate_adult_only", "delegates may only grant for an adult subject"
            )
        if offer.capability not in DELEGATE_WHITELIST:
            raise ConsentDeniedError(
                "delegate_whitelist_only", f"{offer.capability} is outside the delegate whitelist"
            )
        if (
            _delegate_relationship(
                actor_id=offer.actor_id,
                subject_id=offer.subject_id,
                binding=binding,
                relationships=relationships,
                now=now,
            )
            is None
        ):
            raise ConsentDeniedError(
                "relationship_not_active",
                "an active matching delegate relationship is required",
            )
    else:
        if offer.resource_owner_id != offer.subject_id:
            raise ConsentDeniedError(
                "resource_owner_mismatch", "guardian grants require resource_owner == subject"
            )
        if (
            _guardian_relationship(
                actor_id=offer.actor_id,
                subject_id=offer.subject_id,
                binding=binding,
                relationships=relationships,
                now=now,
            )
            is None
        ):
            raise ConsentDeniedError(
                "relationship_not_active",
                "an active matching guardian relationship is required",
            )

    if not binding.is_active_at(now):
        raise ConsentDeniedError("binding_not_active", "an active binding is required")

    if offer.capability in SENSITIVE_ADULT_CAPABILITIES:
        if subject.subject_category != "adult":
            raise ConsentDeniedError(
                "sensitive_requires_verified_adult",
                "adult-sensitive capabilities require a verified adult subject",
            )
        if kind != "subject":
            raise ConsentDeniedError(
                "sensitive_self_grant_only", "adult-sensitive capabilities are self-grant only"
            )
    if kind == "guardian" and subject.subject_category != "minor":
        raise ConsentDeniedError(
            "guardian_minor_only", "guardians may only grant consents to minors"
        )
    if subject.subject_category == "minor":
        if kind != "guardian":
            raise ConsentDeniedError(
                "minor_requires_guardian_grant", "minor consents require a guardian grant"
            )
        if offer.capability not in GUARDIAN_WHITELIST:
            raise ConsentDeniedError(
                "guardian_whitelist_only", f"{offer.capability} is outside the guardian whitelist"
            )
    elif kind not in {"subject", "delegate"}:
        raise ConsentDeniedError("adult_self_grant_only", "adult consents are self-grant only")

    if offer.capability == "voice_profile_create":
        # voice_profile_create = voice-print enrollment (NOT cloning).
        extras = dict(offer.params.extras)
        if extras.get("scope") != "voice_recognition":
            raise ConsentDeniedError(
                "voice_profile_create_requires_recognition_scope",
                "voice_profile_create is enrollment-only; cloning must use voice_clone_use",
            )
    return kind


def verify_revocation(
    *,
    evidence: ConsentEvidence,
    actor_id: str,
    actor_kind: ActorKind,
    relationships: tuple[RelationshipEvidence, ...],
    now: datetime,
) -> None:
    """Rule 4 + revocation authorization: subject owns consents; guardians scope."""
    if actor_kind == "family_admin":
        raise ConsentDeniedError(
            "family_admin_cannot_grant", "family_admin cannot operate consents"
        )
    if actor_kind not in {"subject", "guardian", "delegate"}:
        raise ConsentDeniedError(
            "actor_kind_not_authorized", f"actor kind {actor_kind!r} cannot act"
        )
    if actor_kind == "subject":
        if actor_id != evidence.subject_id:
            raise ConsentDeniedError("revoke_subject_only", "only the subject may revoke")
        return
    if actor_kind == "delegate":
        # A delegate can always withdraw what they granted, even after the
        # device is unbound; they can never touch the subject's own consents.
        if evidence.actor_kind != "delegate" or evidence.actor_id != actor_id:
            raise ConsentDeniedError(
                "delegate_revoke_scope", "delegates may only revoke consents they granted"
            )
        return
    if evidence.actor_kind != "guardian":
        raise ConsentDeniedError(
            "guardian_revoke_scope",
            "guardians may only revoke consents they granted to minors",
        )
    binding = BindingEvidence(
        binding_id=evidence.binding_id,
        version=evidence.binding_version,
        device_id=evidence.device_id or "",
        status="active",
        declared_mode="self_use",
        valid_from=evidence.valid_from,
        valid_until=evidence.valid_until,
        canonical_hash="",
    )
    if (
        _guardian_relationship(
            actor_id=actor_id,
            subject_id=evidence.subject_id,
            binding=binding,
            relationships=relationships,
            now=now,
        )
        is None
    ):
        raise ConsentDeniedError(
            "relationship_not_active", "an active matching guardian relationship is required"
        )


def _content_hash(*parts: object) -> str:
    return compute_canonical_hash([canonical_json(part) for part in parts])


def _grant_content_hash(
    *,
    offer: ConsentOffer,
    subject: SubjectProof,
    actor_kind: ActorKind | None,
    binding: BindingEvidence,
    relationships: tuple[RelationshipEvidence, ...],
) -> str:
    return _content_hash(
        "grant",
        actor_kind,
        offer.canonical_content(),
        {
            "subject_id": subject.subject_id,
            "subject_category": subject.subject_category,
            "age_evidence_status": subject.age_evidence_status,
        },
        binding.to_canonical_dict(),
        [relationship.to_canonical_dict() for relationship in relationships],
    )


def _termination_content_hash(
    *,
    operation: str,
    consent_id: str,
    actor_id: str,
    actor_kind: ActorKind,
    reason: str,
    relationships: tuple[RelationshipEvidence, ...],
) -> str:
    return _content_hash(
        operation,
        consent_id,
        actor_id,
        actor_kind,
        reason,
        [relationship.to_canonical_dict() for relationship in relationships],
    )


def _outbox_payload(*pairs: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(pairs))


def _superseded_version(
    chain: ConsentEvidence, snapshot_id: str, superseded_by_consent_id: str
) -> ConsentEvidence:
    """Append-only supersession row for a chain replaced by a new grant."""
    return ConsentEvidence(
        consent_id=chain.consent_id,
        version=chain.version + 1,
        snapshot_id=snapshot_id,
        status="superseded",
        subject_id=chain.subject_id,
        resource_owner_id=chain.resource_owner_id,
        actor_id=chain.actor_id,
        actor_kind=chain.actor_kind,
        device_id=chain.device_id,
        binding_id=chain.binding_id,
        binding_version=chain.binding_version,
        capability=chain.capability,
        purpose=chain.purpose,
        policy_version=chain.policy_version,
        evidence_id=str(uuid.uuid4()),
        offer_id=chain.offer_id,
        offer_version=chain.offer_version,
        offer_hash=chain.offer_hash,
        idempotency_key=None,
        params=chain.params,
        valid_from=chain.valid_from,
        valid_until=chain.valid_until,
        supersedes_consent_id=None,
        superseded_by_consent_id=superseded_by_consent_id,
        canonical_hash="",
    )


def _build_snapshot(
    uow: ConsentUnitOfWork,
    *,
    subject_id: str,
    binding: BindingEvidence,
    relationships: tuple[RelationshipEvidence, ...],
    policy_version: str,
    created_at: datetime,
    snapshot_id: str,
) -> ConsentSnapshot:
    previous = uow.latest_snapshot(subject_id, binding.binding_id, binding.version)
    grants = uow.active_chains(subject_id, binding.binding_id, binding.version)
    return ConsentSnapshot(
        snapshot_id=snapshot_id,
        version=(previous.version + 1 if previous is not None else 1),
        subject_id=subject_id,
        binding_id=binding.binding_id,
        binding_version=binding.version,
        policy_version=policy_version,
        created_at=created_at,
        grants=grants,
        relationships=relationships,
        binding=binding,
        canonical_hash="",
    )


def _with_new_identity(
    snapshot: ConsentSnapshot,
    *,
    snapshot_id: str,
    version: int,
    created_at: datetime,
) -> ConsentSnapshot:
    return ConsentSnapshot(
        snapshot_id=snapshot_id,
        version=version,
        subject_id=snapshot.subject_id,
        binding_id=snapshot.binding_id,
        binding_version=snapshot.binding_version,
        policy_version=snapshot.policy_version,
        created_at=created_at,
        grants=snapshot.grants,
        relationships=snapshot.relationships,
        binding=snapshot.binding,
        canonical_hash="",
    )


def _snapshot_unchanged(previous: ConsentSnapshot, candidate: ConsentSnapshot) -> bool:
    if (
        previous.binding_version != candidate.binding_version
        or previous.policy_version != candidate.policy_version
        or previous.binding != candidate.binding
        or len(previous.grants) != len(candidate.grants)
        or len(previous.relationships) != len(candidate.relationships)
    ):
        return False
    if sorted((g.consent_id, g.version) for g in previous.grants) != sorted(
        (g.consent_id, g.version) for g in candidate.grants
    ):
        return False
    if sorted(
        (r.relationship_id, r.revision, r.canonical_hash) for r in previous.relationships
    ) != sorted((r.relationship_id, r.revision, r.canonical_hash) for r in candidate.relationships):
        return False
    return True


def _replay(
    uow: ConsentUnitOfWork,
    record: IdempotencyRecord,
    content_hash: str,
) -> ConsentOperationResult:
    """Rule 7: replay returns the stored result; different content conflicts."""
    if record.content_hash != content_hash:
        raise ConsentConflictError("idempotency key reused with different content")
    if record.consent_id is None or record.version is None:
        raise ConsentConsistencyError("idempotency record lacks consent identity")
    evidence = uow.get_consent(record.consent_id, record.version)
    snapshot = uow.snapshot_by_id(record.snapshot_id) if record.snapshot_id is not None else None
    event = uow.outbox_by_id(record.event_id)
    audit = uow.audit_by_id(record.audit_id) if record.audit_id is not None else None
    if evidence is None or snapshot is None or event is None or audit is None:
        raise ConsentConsistencyError("idempotency record points at missing rows")
    return ConsentOperationResult(
        evidence=evidence,
        snapshot=snapshot,
        outbox_event=event,
        audit_entry=audit,
    )


async def _async_build_snapshot(
    uow: AsyncConsentUnitOfWork,
    *,
    subject_id: str,
    binding: BindingEvidence,
    relationships: tuple[RelationshipEvidence, ...],
    policy_version: str,
    created_at: datetime,
    snapshot_id: str,
) -> ConsentSnapshot:
    previous = await uow.latest_snapshot(subject_id, binding.binding_id, binding.version)
    grants = await uow.active_chains(subject_id, binding.binding_id, binding.version)
    return ConsentSnapshot(
        snapshot_id=snapshot_id,
        version=(previous.version + 1 if previous is not None else 1),
        subject_id=subject_id,
        binding_id=binding.binding_id,
        binding_version=binding.version,
        policy_version=policy_version,
        created_at=created_at,
        grants=grants,
        relationships=relationships,
        binding=binding,
        canonical_hash="",
    )


async def _async_replay(
    uow: AsyncConsentUnitOfWork,
    record: IdempotencyRecord,
    content_hash: str,
) -> ConsentOperationResult:
    if record.content_hash != content_hash:
        raise ConsentConflictError("idempotency key reused with different content")
    if record.consent_id is None or record.version is None:
        raise ConsentConsistencyError("idempotency record lacks consent identity")
    evidence = await uow.get_consent(record.consent_id, record.version)
    snapshot = (
        await uow.snapshot_by_id(record.snapshot_id) if record.snapshot_id is not None else None
    )
    event = await uow.outbox_by_id(record.event_id)
    audit = await uow.audit_by_id(record.audit_id) if record.audit_id is not None else None
    if evidence is None or snapshot is None or event is None or audit is None:
        raise ConsentConsistencyError("idempotency record points at missing rows")
    return ConsentOperationResult(
        evidence=evidence,
        snapshot=snapshot,
        outbox_event=event,
        audit_entry=audit,
    )
