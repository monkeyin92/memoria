"""ConsentAuthority: authoritative consent lifecycle with fail-closed rules.

Rules (CONTRACT "权威同意规则" 1-8):

1. Unknown subjects (missing id / unknown category / missing age evidence)
   fail closed: no grant is ever created.
2. Adult-sensitive capabilities are self-grant only (actor == subject ==
   resource owner) and require verified adult evidence plus an active binding.
3. Guardians may grant only the child whitelist to minors, backed by
   authoritative active relationship + binding evidence.  ``voice_profile_create``
   is voice-print *enrollment* only (NOT cloning): the offer must declare
   ``extras[("scope", "voice_recognition")]``; cloning belongs to the adult-only
   ``voice_clone_use`` path.
4. family_admin is never a data subject proxy: it cannot grant (or revoke) any
   consent.
5. Binding/subject/relationship changes invalidate old snapshots: grants verify
   active binding + relationship evidence, and snapshots are bound to
   ``binding_id + binding_version`` (new versions, never overwrites).
6. Revoke / dispute / expire append new versions (the old row is never
   modified); superseded chains are explicitly recorded.
7. Idempotency: same key + same canonical content replays the stored result;
   same key + different content raises ``ConsentConflictError``.
8. Every change writes consent rows + snapshot + audit + outbox in one
   transaction.

The authority never trusts caller-claimed roles: ``actor_kind`` is either
derived from authoritative evidence (identity equality or guardian relationship
evidence) or verified against it before use.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeEvidenceStatusValue,
    PurposeValue,
    SubjectCategoryValue,
)

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
    AsyncConsentStorePort,
    AsyncConsentUnitOfWork,
    ConsentConflictError,
    ConsentConsistencyError,
    ConsentNotFoundError,
    ConsentStorePort,
    ConsentUnitOfWork,
    IdempotencyRecord,
)

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


class ConsentDeniedError(PermissionError):
    """A consent rule rejected the operation (fail closed)."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SubjectProof:
    """Structured subject payload; resolver verification supplies authority."""

    subject_id: str
    subject_category: SubjectCategoryValue
    age_evidence_status: AgeEvidenceStatusValue

    def __post_init__(self) -> None:
        if not self.subject_id.strip() or len(self.subject_id) > 128:
            raise ValueError("subject_id must be a bounded non-empty string")
        if self.subject_category not in {"unknown", "minor", "adult"}:
            raise ValueError(f"unknown subject_category {self.subject_category!r}")
        if self.age_evidence_status not in {"unverified", "verified", "disputed"}:
            raise ValueError(f"unknown age_evidence_status {self.age_evidence_status!r}")


class EvidenceResolverPort(Protocol):
    """Identity adapter that resolves authoritative evidence by stable identity."""

    def resolve_subject(self, candidate: SubjectProof) -> SubjectProof: ...

    def resolve_binding(self, candidate: BindingEvidence, subject_id: str) -> BindingEvidence: ...

    def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]: ...


class AsyncEvidenceResolverPort(Protocol):
    """Asynchronous Identity adapter for authoritative evidence resolution."""

    async def resolve_subject(self, candidate: SubjectProof) -> SubjectProof: ...

    async def resolve_binding(
        self, candidate: BindingEvidence, subject_id: str
    ) -> BindingEvidence: ...

    async def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]: ...


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
    if subject.age_evidence_status != "verified":
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
    if kind not in {"subject", "guardian"}:
        raise ConsentDeniedError("actor_kind_not_authorized", f"actor kind {kind!r} cannot grant")

    if kind == "subject":
        if not (offer.actor_id == offer.subject_id == offer.resource_owner_id):
            raise ConsentDeniedError(
                "subject_claim_mismatch",
                "subject grant requires actor == subject == resource owner",
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
    else:
        if kind != "subject":
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
    if actor_kind not in {"subject", "guardian"}:
        raise ConsentDeniedError(
            "actor_kind_not_authorized", f"actor kind {actor_kind!r} cannot act"
        )
    if actor_kind == "subject":
        if actor_id != evidence.subject_id:
            raise ConsentDeniedError("revoke_subject_only", "only the subject may revoke")
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


class ConsentAuthority:
    """Synchronous consent authority over a :class:`ConsentStorePort`."""

    def __init__(
        self,
        store: ConsentStorePort,
        *,
        resolver: EvidenceResolverPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._now = now or (lambda: datetime.now(UTC))

    # -- offer ----------------------------------------------------------------
    def create_offer(
        self,
        *,
        capability: str,
        subject_id: str,
        actor_id: str,
        resource_owner_id: str,
        purpose: PurposeValue,
        params: object,
        valid_from: datetime,
        valid_until: datetime,
        policy_version: str,
        offer_id: str | None = None,
        created_at: datetime | None = None,
        version: int = 1,
        status: str = "active",
        issuer: str = "consent_authority",
        supersedes_offer_id: str | None = None,
    ) -> ConsentOffer:
        from services.consent.evidence import ConsentParams

        if not isinstance(params, ConsentParams):
            raise ValueError("params must be a ConsentParams instance")
        resolved_offer_id = offer_id or str(uuid.uuid4())
        uow = self._store.transaction()
        try:
            uow.lock_offer_head(resolved_offer_id, actor_id, subject_id)
            existing = uow.offer_by_id(resolved_offer_id, version)
            issued_at = created_at or (existing.created_at if existing is not None else self._now())
            offer = ConsentOffer(
                offer_id=resolved_offer_id,
                version=version,
                status=status,  # type: ignore[arg-type]
                capability=capability,  # type: ignore[arg-type]
                subject_id=subject_id,
                actor_id=actor_id,
                resource_owner_id=resource_owner_id,
                purpose=purpose,
                params=params,
                valid_from=_utc(valid_from, "valid_from"),
                valid_until=_utc(valid_until, "valid_until"),
                created_at=_utc(issued_at, "created_at"),
                issuer=issuer,
                policy_version=policy_version,
                supersedes_offer_id=supersedes_offer_id,
            )
            if existing is not None:
                if existing.canonical_hash != offer.canonical_hash:
                    raise ConsentConflictError(
                        "offer id/version reused with different canonical content"
                    )
                uow.commit()
                return existing
            latest = uow.latest_offer(offer.offer_id)
            expected_previous = offer.version - 1
            if latest is None and offer.version != 1:
                raise ConsentConflictError("offer version must start at 1")
            if latest is not None and latest.version != expected_previous:
                raise ConsentConflictError("offer version must be monotonically incremented")
            if latest is not None and offer.supersedes_offer_id != latest.offer_id:
                raise ConsentConflictError("offer version must reference the superseded offer")

            uow.append_offer(offer)
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memoria:consent-offer:{offer.offer_id}:{offer.version}:{offer.canonical_hash}",
                )
            )
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_offer",
                aggregate_id=offer.offer_id,
                version=offer.version,
                payload=_outbox_payload(
                    ("actor_id", offer.actor_id),
                    ("binding_id", "__consent_offer__"),
                    ("capability", offer.capability),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("status", offer.status),
                    ("subject_id", offer.subject_id),
                ),
                created_at=offer.created_at,
                idempotency_key=None,
            )
            uow.append_outbox(event)
            uow.append_audit(
                AuditEntry(
                    audit_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"memoria:consent-offer-audit:{offer.offer_id}:{offer.version}:{offer.canonical_hash}",
                        )
                    ),
                    event_id=event_id,
                    action="offer_create",
                    actor_id=offer.actor_id,
                    subject_id=offer.subject_id,
                    consent_id=None,
                    snapshot_id=None,
                    payload=_outbox_payload(
                        ("offer_hash", offer.canonical_hash),
                        ("offer_id", offer.offer_id),
                        ("offer_version", str(offer.version)),
                    ),
                    created_at=offer.created_at,
                )
            )
            uow.commit()
            return offer
        except BaseException:
            uow.rollback()
            raise

    # -- grant ----------------------------------------------------------------
    def grant(
        self,
        offer: ConsentOffer | str,
        *,
        expected_version: int | None = None,
        subject: SubjectProof,
        binding: BindingEvidence,
        relationships: tuple[RelationshipEvidence, ...] = (),
        actor_kind: ActorKind | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        grant_at = _utc(now or self._now(), "now")
        offer_id = offer.offer_id if isinstance(offer, ConsentOffer) else offer
        requested_version = (
            expected_version
            if expected_version is not None
            else (offer.version if isinstance(offer, ConsentOffer) else None)
        )
        uow = self._store.transaction()
        try:
            stored_offer = uow.latest_offer(offer_id)
            if stored_offer is None:
                raise ConsentDeniedError("offer_not_found", "offer is not stored by the authority")
            if requested_version is None or stored_offer.version != requested_version:
                raise ConsentConflictError("offer version does not match latest version")
            offer = stored_offer
            if offer.status != "active":
                raise ConsentDeniedError("offer_inactive", "offer status is not active")
            if grant_at >= offer.valid_until:
                raise ConsentDeniedError("offer_expired", "the offer window has expired")
            if grant_at < offer.valid_from:
                raise ConsentDeniedError("offer_not_yet_valid", "the offer window has not opened")
            if self._resolver is None:
                raise ConsentDeniedError(
                    "evidence_unresolved",
                    "evidence_unresolved: an authoritative evidence resolver is required",
                )
            try:
                resolved_subject = self._resolver.resolve_subject(subject)
                resolved_binding = self._resolver.resolve_binding(binding, offer.subject_id)
                resolved_relationships = self._resolver.resolve_relationships(
                    relationships,
                    offer.actor_id,
                    offer.subject_id,
                    resolved_binding.binding_id,
                )
            except Exception as exc:
                raise ConsentDeniedError(
                    "evidence_unresolved",
                    "evidence_unresolved: authoritative evidence could not be resolved",
                ) from exc
            if resolved_subject.subject_id != offer.subject_id:
                raise ConsentDeniedError("subject_mismatch", "offer subject does not match proof")
            subject = resolved_subject
            binding = resolved_binding
            relationships = resolved_relationships
            content_hash = _grant_content_hash(
                offer=offer,
                subject=subject,
                actor_kind=actor_kind,
                binding=binding,
                relationships=relationships,
            )
            if idempotency_key is not None:
                existing = uow.get_idempotency(idempotency_key)
                if existing is not None:
                    result = _replay(uow, existing, content_hash)
                    uow.commit()
                    return result
            verified_kind = verify_grant(
                offer=offer,
                subject=subject,
                actor_kind=actor_kind,
                binding=binding,
                relationships=relationships,
                now=grant_at,
            )

            current_head = uow.lock_consent_head(
                offer.actor_id,
                offer.actor_id,
                subject.subject_id,
                binding.binding_id,
                binding.version,
                offer.capability,
                offer.purpose,
            )
            superseded_chain = current_head if current_head is not None else None
            uow.lock_snapshot_head(
                offer.actor_id,
                subject.subject_id,
                binding.binding_id,
                binding.version,
            )

            consent_id = str(uuid.uuid4())
            snapshot_id = str(uuid.uuid4())
            evidence_id = str(uuid.uuid4())
            new_chain = ConsentEvidence(
                consent_id=consent_id,
                version=(current_head.version + 2 if current_head is not None else 1),
                snapshot_id=snapshot_id,
                status="active",
                subject_id=offer.subject_id,
                resource_owner_id=offer.resource_owner_id,
                actor_id=offer.actor_id,
                actor_kind=verified_kind,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                binding_version=binding.version,
                capability=offer.capability,
                purpose=offer.purpose,
                policy_version=offer.policy_version,
                evidence_id=evidence_id,
                offer_id=offer.offer_id,
                offer_version=offer.version,
                offer_hash=offer.canonical_hash,
                idempotency_key=idempotency_key,
                params=offer.params,
                valid_from=offer.valid_from,
                valid_until=offer.valid_until,
                supersedes_consent_id=(
                    superseded_chain.consent_id if superseded_chain is not None else None
                ),
                superseded_by_consent_id=None,
                canonical_hash="",
            )
            if superseded_chain is not None:
                superseded_row = _superseded_version(superseded_chain, snapshot_id, consent_id)
                uow.append_consent(superseded_row)
            uow.append_consent(new_chain)

            snapshot = _build_snapshot(
                uow,
                subject_id=offer.subject_id,
                binding=binding,
                relationships=relationships,
                policy_version=offer.policy_version,
                created_at=grant_at,
                snapshot_id=snapshot_id,
            )
            uow.append_snapshot(snapshot)

            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_grant",
                aggregate_id=consent_id,
                version=new_chain.version,
                payload=_outbox_payload(
                    ("actor_id", offer.actor_id),
                    ("binding_id", binding.binding_id),
                    ("binding_version", str(binding.version)),
                    ("capability", offer.capability),
                    ("consent_id", consent_id),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("policy_version", offer.policy_version),
                    ("status", "active"),
                    ("subject_id", offer.subject_id),
                    ("valid_until", offer.valid_until.isoformat()),
                ),
                created_at=grant_at,
                idempotency_key=idempotency_key,
            )
            uow.append_outbox(event)

            audit = AuditEntry(
                audit_id=str(uuid.uuid4()),
                event_id=event_id,
                action="grant",
                actor_id=offer.actor_id,
                subject_id=offer.subject_id,
                consent_id=consent_id,
                snapshot_id=snapshot_id,
                payload=_outbox_payload(
                    ("capability", offer.capability),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("policy_version", offer.policy_version),
                ),
                created_at=grant_at,
            )
            uow.append_audit(audit)

            if idempotency_key is not None:
                uow.save_idempotency(
                    IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        content_hash=content_hash,
                        consent_id=consent_id,
                        version=new_chain.version,
                        snapshot_id=snapshot_id,
                        event_id=event_id,
                        audit_id=audit.audit_id,
                        subject_id=offer.subject_id,
                        actor_id=offer.actor_id,
                        created_at=grant_at,
                    )
                )
            uow.commit()
            return ConsentOperationResult(
                evidence=new_chain,
                snapshot=snapshot,
                outbox_event=event,
                audit_entry=audit,
            )
        except BaseException:
            uow.rollback()
            raise

    # -- revoke / dispute -----------------------------------------------------
    def _terminate(
        self,
        *,
        consent_id: str,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...],
        reason: str,
        operation: Literal["revoke", "dispute"],
        expected_version: int | None,
        idempotency_key: str | None,
        now: datetime | None,
    ) -> ConsentOperationResult:
        acted_at = _utc(now or self._now(), "now")
        content_hash = _termination_content_hash(
            operation=operation,
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            reason=reason,
            relationships=relationships,
        )
        uow = self._store.transaction()
        try:
            if idempotency_key is not None:
                existing = uow.get_idempotency(idempotency_key)
                if existing is not None:
                    result = _replay(uow, existing, content_hash)
                    uow.commit()
                    return result
            latest = uow.latest_consent(consent_id)
            if latest is None:
                raise ConsentNotFoundError(f"consent {consent_id!r} not found")
            current_head = uow.lock_consent_head(
                actor_id,
                latest.actor_id,
                latest.subject_id,
                latest.binding_id,
                latest.binding_version,
                latest.capability,
                latest.purpose,
            )
            if current_head is None or current_head.consent_id != latest.consent_id:
                raise ConsentConflictError("consent is no longer the current authority head")
            latest = current_head
            uow.lock_snapshot_head(
                actor_id,
                latest.subject_id,
                latest.binding_id,
                latest.binding_version,
            )
            if expected_version is not None and latest.version != expected_version:
                raise ConsentConflictError(
                    f"consent {consent_id!r} is at version {latest.version}, not {expected_version}"
                )
            if latest.status != "active":
                raise ConsentConflictError(
                    f"consent {consent_id!r} is {latest.status}; "
                    "only active consents can be revoked or disputed"
                )
            verify_revocation(
                evidence=latest,
                actor_id=actor_id,
                actor_kind=actor_kind,
                relationships=relationships,
                now=acted_at,
            )

            status: str = "revoked" if operation == "revoke" else "disputed"
            snapshot_id = str(uuid.uuid4())
            evidence_id = str(uuid.uuid4())
            next_row = ConsentEvidence(
                consent_id=latest.consent_id,
                version=latest.version + 1,
                snapshot_id=snapshot_id,
                status=status,  # type: ignore[arg-type]
                subject_id=latest.subject_id,
                resource_owner_id=latest.resource_owner_id,
                actor_id=latest.actor_id,
                actor_kind=latest.actor_kind,
                device_id=latest.device_id,
                binding_id=latest.binding_id,
                binding_version=latest.binding_version,
                capability=latest.capability,
                purpose=latest.purpose,
                policy_version=latest.policy_version,
                evidence_id=evidence_id,
                offer_id=latest.offer_id,
                offer_version=latest.offer_version,
                offer_hash=latest.offer_hash,
                idempotency_key=idempotency_key,
                params=latest.params,
                valid_from=latest.valid_from,
                valid_until=latest.valid_until,
                supersedes_consent_id=None,
                superseded_by_consent_id=None,
                canonical_hash="",
            )
            uow.append_consent(next_row)

            previous = uow.latest_snapshot(
                latest.subject_id, latest.binding_id, latest.binding_version
            )
            if previous is None:
                raise ConsentConsistencyError(
                    f"no snapshot for consent {consent_id!r}; refusing to continue"
                )
            snapshot = _build_snapshot(
                uow,
                subject_id=latest.subject_id,
                binding=previous.binding,
                relationships=previous.relationships,
                policy_version=previous.policy_version,
                created_at=acted_at,
                snapshot_id=snapshot_id,
            )
            uow.append_snapshot(snapshot)

            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type=("consent_revoke" if operation == "revoke" else "consent_dispute"),
                aggregate_id=consent_id,
                version=next_row.version,
                payload=_outbox_payload(
                    ("actor_id", actor_id),
                    ("binding_id", latest.binding_id),
                    ("consent_id", consent_id),
                    ("reason", reason),
                    ("status", status),
                    ("subject_id", latest.subject_id),
                    ("version", str(next_row.version)),
                ),
                created_at=acted_at,
                idempotency_key=idempotency_key,
            )
            uow.append_outbox(event)

            audit = AuditEntry(
                audit_id=str(uuid.uuid4()),
                event_id=event_id,
                action=operation,
                actor_id=actor_id,
                subject_id=latest.subject_id,
                consent_id=consent_id,
                snapshot_id=snapshot_id,
                payload=_outbox_payload(("reason", reason)),
                created_at=acted_at,
            )
            uow.append_audit(audit)

            if idempotency_key is not None:
                uow.save_idempotency(
                    IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        content_hash=content_hash,
                        consent_id=consent_id,
                        version=next_row.version,
                        snapshot_id=snapshot_id,
                        event_id=event_id,
                        audit_id=audit.audit_id,
                        subject_id=latest.subject_id,
                        actor_id=actor_id,
                        created_at=acted_at,
                    )
                )
            uow.commit()
            return ConsentOperationResult(
                evidence=next_row,
                snapshot=snapshot,
                outbox_event=event,
                audit_entry=audit,
            )
        except BaseException:
            uow.rollback()
            raise

    def revoke(
        self,
        consent_id: str,
        *,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...] = (),
        reason: str = "",
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        return self._terminate(
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            relationships=relationships,
            reason=reason,
            operation="revoke",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
        )

    def dispute(
        self,
        consent_id: str,
        *,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...] = (),
        reason: str = "",
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        return self._terminate(
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            relationships=relationships,
            reason=reason,
            operation="dispute",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
        )

    # -- expire ---------------------------------------------------------------
    def expire_due(self, now: datetime | None = None) -> tuple[ConsentOperationResult, ...]:
        """Expire every active chain whose window has passed (one transaction)."""
        acted_at = _utc(now or self._now(), "now")
        uow = self._store.transaction()
        results: list[ConsentOperationResult] = []
        try:
            for chain in uow.all_active_chains():
                if chain.valid_until > acted_at:
                    continue
                current_head = uow.lock_consent_head(
                    "system",
                    chain.actor_id,
                    chain.subject_id,
                    chain.binding_id,
                    chain.binding_version,
                    chain.capability,
                    chain.purpose,
                )
                if current_head is None or current_head.canonical_hash != chain.canonical_hash:
                    continue
                chain = current_head
                uow.lock_snapshot_head(
                    "system",
                    chain.subject_id,
                    chain.binding_id,
                    chain.binding_version,
                )
                previous = uow.latest_snapshot(
                    chain.subject_id, chain.binding_id, chain.binding_version
                )
                if previous is None:
                    raise ConsentConsistencyError(
                        f"no snapshot for consent {chain.consent_id!r}; refusing to expire"
                    )
                snapshot_id = str(uuid.uuid4())
                evidence_id = str(uuid.uuid4())
                expired_row = ConsentEvidence(
                    consent_id=chain.consent_id,
                    version=chain.version + 1,
                    snapshot_id=snapshot_id,
                    status="expired",
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
                    evidence_id=evidence_id,
                    offer_id=chain.offer_id,
                    offer_version=chain.offer_version,
                    offer_hash=chain.offer_hash,
                    idempotency_key=None,
                    params=chain.params,
                    valid_from=chain.valid_from,
                    valid_until=chain.valid_until,
                    supersedes_consent_id=None,
                    superseded_by_consent_id=None,
                    canonical_hash="",
                )
                uow.append_consent(expired_row)
                snapshot = _build_snapshot(
                    uow,
                    subject_id=chain.subject_id,
                    binding=previous.binding,
                    relationships=previous.relationships,
                    policy_version=previous.policy_version,
                    created_at=acted_at,
                    snapshot_id=snapshot_id,
                )
                uow.append_snapshot(snapshot)
                event_id = str(uuid.uuid4())
                event = ConsentOutboxEvent(
                    event_id=event_id,
                    aggregate_type="consent_expire",
                    aggregate_id=chain.consent_id,
                    version=expired_row.version,
                    payload=_outbox_payload(
                        ("actor_id", "system"),
                        ("binding_id", chain.binding_id),
                        ("consent_id", chain.consent_id),
                        ("status", "expired"),
                        ("subject_id", chain.subject_id),
                        ("version", str(expired_row.version)),
                    ),
                    created_at=acted_at,
                    idempotency_key=None,
                )
                uow.append_outbox(event)
                audit = AuditEntry(
                    audit_id=str(uuid.uuid4()),
                    event_id=event_id,
                    action="expire",
                    actor_id="system",
                    subject_id=chain.subject_id,
                    consent_id=chain.consent_id,
                    snapshot_id=snapshot_id,
                    payload=_outbox_payload(("reason", "due")),
                    created_at=acted_at,
                )
                uow.append_audit(audit)
                results.append(
                    ConsentOperationResult(
                        evidence=expired_row,
                        snapshot=snapshot,
                        outbox_event=event,
                        audit_entry=audit,
                    )
                )
            uow.commit()
            return tuple(results)
        except BaseException:
            uow.rollback()
            raise

    # -- snapshot -------------------------------------------------------------
    def snapshot(
        self,
        subject_id: str,
        *,
        binding: BindingEvidence,
        relationships: tuple[RelationshipEvidence, ...] = (),
        policy_version: str | None = None,
        now: datetime | None = None,
    ) -> ConsentSnapshot:
        """Rebuild/refresh the current snapshot; no-op when nothing changed."""
        acted_at = _utc(now or self._now(), "now")
        uow = self._store.transaction()
        try:
            uow.lock_snapshot_head("system", subject_id, binding.binding_id, binding.version)
            previous = uow.latest_snapshot(subject_id, binding.binding_id, binding.version)
            resolved_policy = policy_version or (
                previous.policy_version if previous is not None else ""
            )
            if not resolved_policy:
                raise ValueError("policy_version is required for the first snapshot")
            candidate = _build_snapshot(
                uow,
                subject_id=subject_id,
                binding=binding,
                relationships=relationships,
                policy_version=resolved_policy,
                created_at=acted_at,
                snapshot_id=str(uuid.uuid4()),
            )
            if previous is not None and _snapshot_unchanged(previous, candidate):
                uow.commit()
                return previous
            snapshot = _with_new_identity(
                candidate,
                snapshot_id=str(uuid.uuid4()),
                version=(previous.version + 1 if previous is not None else 1),
                created_at=acted_at,
            )
            uow.append_snapshot(snapshot)
            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_snapshot",
                aggregate_id=snapshot.snapshot_id,
                version=snapshot.version,
                payload=_outbox_payload(
                    ("actor_id", "system"),
                    ("binding_id", binding.binding_id),
                    ("binding_version", str(binding.version)),
                    ("snapshot_id", snapshot.snapshot_id),
                    ("subject_id", subject_id),
                ),
                created_at=acted_at,
                idempotency_key=None,
            )
            uow.append_outbox(event)
            uow.append_audit(
                AuditEntry(
                    audit_id=str(uuid.uuid4()),
                    event_id=event_id,
                    action="snapshot",
                    actor_id="system",
                    subject_id=subject_id,
                    consent_id=None,
                    snapshot_id=snapshot.snapshot_id,
                    payload=_outbox_payload(("version", str(snapshot.version))),
                    created_at=acted_at,
                )
            )
            uow.commit()
            return snapshot
        except BaseException:
            uow.rollback()
            raise


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


class AsyncConsentAuthority:
    """Asynchronous consent authority over an :class:`AsyncConsentStorePort`."""

    def __init__(
        self,
        store: AsyncConsentStorePort,
        *,
        resolver: AsyncEvidenceResolverPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._resolver = resolver
        self._now = now or (lambda: datetime.now(UTC))

    async def create_offer(
        self,
        *,
        capability: str,
        subject_id: str,
        actor_id: str,
        resource_owner_id: str,
        purpose: PurposeValue,
        params: object,
        valid_from: datetime,
        valid_until: datetime,
        policy_version: str,
        offer_id: str | None = None,
        created_at: datetime | None = None,
        version: int = 1,
        status: str = "active",
        issuer: str = "consent_authority",
        supersedes_offer_id: str | None = None,
    ) -> ConsentOffer:
        from services.consent.evidence import ConsentParams

        if not isinstance(params, ConsentParams):
            raise ValueError("params must be a ConsentParams instance")
        resolved_offer_id = offer_id or str(uuid.uuid4())
        uow = await self._store.transaction()
        try:
            await uow.lock_offer_head(resolved_offer_id, actor_id, subject_id)
            existing = await uow.offer_by_id(resolved_offer_id, version)
            issued_at = created_at or (existing.created_at if existing is not None else self._now())
            offer = ConsentOffer(
                offer_id=resolved_offer_id,
                version=version,
                status=status,  # type: ignore[arg-type]
                capability=capability,  # type: ignore[arg-type]
                subject_id=subject_id,
                actor_id=actor_id,
                resource_owner_id=resource_owner_id,
                purpose=purpose,
                params=params,
                valid_from=_utc(valid_from, "valid_from"),
                valid_until=_utc(valid_until, "valid_until"),
                created_at=_utc(issued_at, "created_at"),
                issuer=issuer,
                policy_version=policy_version,
                supersedes_offer_id=supersedes_offer_id,
            )
            if existing is not None:
                if existing.canonical_hash != offer.canonical_hash:
                    raise ConsentConflictError(
                        "offer id/version reused with different canonical content"
                    )
                await uow.commit()
                return existing
            latest = await uow.latest_offer(offer.offer_id)
            if latest is None and offer.version != 1:
                raise ConsentConflictError("offer version must start at 1")
            if latest is not None and latest.version != offer.version - 1:
                raise ConsentConflictError("offer version must be monotonically incremented")
            if latest is not None and offer.supersedes_offer_id != latest.offer_id:
                raise ConsentConflictError("offer version must reference the superseded offer")
            await uow.append_offer(offer)
            event_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memoria:consent-offer:{offer.offer_id}:{offer.version}:{offer.canonical_hash}",
                )
            )
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_offer",
                aggregate_id=offer.offer_id,
                version=offer.version,
                payload=_outbox_payload(
                    ("actor_id", offer.actor_id),
                    ("binding_id", "__consent_offer__"),
                    ("capability", offer.capability),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("status", offer.status),
                    ("subject_id", offer.subject_id),
                ),
                created_at=offer.created_at,
            )
            await uow.append_outbox(event)
            await uow.append_audit(
                AuditEntry(
                    audit_id=str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"memoria:consent-offer-audit:{offer.offer_id}:{offer.version}:{offer.canonical_hash}",
                        )
                    ),
                    event_id=event_id,
                    action="offer_create",
                    actor_id=offer.actor_id,
                    subject_id=offer.subject_id,
                    consent_id=None,
                    snapshot_id=None,
                    payload=_outbox_payload(
                        ("offer_hash", offer.canonical_hash),
                        ("offer_id", offer.offer_id),
                        ("offer_version", str(offer.version)),
                    ),
                    created_at=offer.created_at,
                )
            )
            await uow.commit()
            return offer
        except BaseException:
            await uow.rollback()
            raise

    async def grant(
        self,
        offer: ConsentOffer | str,
        *,
        expected_version: int | None = None,
        subject: SubjectProof,
        binding: BindingEvidence,
        relationships: tuple[RelationshipEvidence, ...] = (),
        actor_kind: ActorKind | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        grant_at = _utc(now or self._now(), "now")
        offer_id = offer.offer_id if isinstance(offer, ConsentOffer) else offer
        requested_version = (
            expected_version
            if expected_version is not None
            else (offer.version if isinstance(offer, ConsentOffer) else None)
        )
        uow = await self._store.transaction()
        try:
            stored_offer = await uow.latest_offer(offer_id)
            if stored_offer is None:
                raise ConsentDeniedError("offer_not_found", "offer is not stored by the authority")
            if requested_version is None or stored_offer.version != requested_version:
                raise ConsentConflictError("offer version does not match latest version")
            offer = stored_offer
            if offer.status != "active":
                raise ConsentDeniedError("offer_inactive", "offer status is not active")
            if grant_at >= offer.valid_until:
                raise ConsentDeniedError("offer_expired", "the offer window has expired")
            if grant_at < offer.valid_from:
                raise ConsentDeniedError("offer_not_yet_valid", "the offer window has not opened")
            if self._resolver is None:
                raise ConsentDeniedError(
                    "evidence_unresolved",
                    "evidence_unresolved: an authoritative evidence resolver is required",
                )
            try:
                resolved_subject = await self._resolver.resolve_subject(subject)
                resolved_binding = await self._resolver.resolve_binding(binding, offer.subject_id)
                resolved_relationships = await self._resolver.resolve_relationships(
                    relationships,
                    offer.actor_id,
                    offer.subject_id,
                    resolved_binding.binding_id,
                )
            except Exception as exc:
                raise ConsentDeniedError(
                    "evidence_unresolved",
                    "evidence_unresolved: authoritative evidence could not be resolved",
                ) from exc
            if resolved_subject.subject_id != offer.subject_id:
                raise ConsentDeniedError("subject_mismatch", "offer subject does not match proof")
            subject = resolved_subject
            binding = resolved_binding
            relationships = resolved_relationships
            content_hash = _grant_content_hash(
                offer=offer,
                subject=subject,
                actor_kind=actor_kind,
                binding=binding,
                relationships=relationships,
            )
            if idempotency_key is not None:
                existing = await uow.get_idempotency(idempotency_key)
                if existing is not None:
                    result = await _async_replay(uow, existing, content_hash)
                    await uow.commit()
                    return result
            verified_kind = verify_grant(
                offer=offer,
                subject=subject,
                actor_kind=actor_kind,
                binding=binding,
                relationships=relationships,
                now=grant_at,
            )

            current_head = await uow.lock_consent_head(
                offer.actor_id,
                offer.actor_id,
                subject.subject_id,
                binding.binding_id,
                binding.version,
                offer.capability,
                offer.purpose,
            )
            superseded_chain = current_head if current_head is not None else None
            await uow.lock_snapshot_head(
                offer.actor_id,
                subject.subject_id,
                binding.binding_id,
                binding.version,
            )

            consent_id = str(uuid.uuid4())
            snapshot_id = str(uuid.uuid4())
            evidence_id = str(uuid.uuid4())
            new_chain = ConsentEvidence(
                consent_id=consent_id,
                version=(current_head.version + 2 if current_head is not None else 1),
                snapshot_id=snapshot_id,
                status="active",
                subject_id=offer.subject_id,
                resource_owner_id=offer.resource_owner_id,
                actor_id=offer.actor_id,
                actor_kind=verified_kind,
                device_id=binding.device_id,
                binding_id=binding.binding_id,
                binding_version=binding.version,
                capability=offer.capability,
                purpose=offer.purpose,
                policy_version=offer.policy_version,
                evidence_id=evidence_id,
                offer_id=offer.offer_id,
                offer_version=offer.version,
                offer_hash=offer.canonical_hash,
                idempotency_key=idempotency_key,
                params=offer.params,
                valid_from=offer.valid_from,
                valid_until=offer.valid_until,
                supersedes_consent_id=(
                    superseded_chain.consent_id if superseded_chain is not None else None
                ),
                superseded_by_consent_id=None,
                canonical_hash="",
            )
            if superseded_chain is not None:
                await uow.append_consent(
                    _superseded_version(superseded_chain, snapshot_id, consent_id)
                )
            await uow.append_consent(new_chain)

            snapshot = await _async_build_snapshot(
                uow,
                subject_id=offer.subject_id,
                binding=binding,
                relationships=relationships,
                policy_version=offer.policy_version,
                created_at=grant_at,
                snapshot_id=snapshot_id,
            )
            await uow.append_snapshot(snapshot)

            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_grant",
                aggregate_id=consent_id,
                version=new_chain.version,
                payload=_outbox_payload(
                    ("actor_id", offer.actor_id),
                    ("binding_id", binding.binding_id),
                    ("binding_version", str(binding.version)),
                    ("capability", offer.capability),
                    ("consent_id", consent_id),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("policy_version", offer.policy_version),
                    ("status", "active"),
                    ("subject_id", offer.subject_id),
                    ("valid_until", offer.valid_until.isoformat()),
                ),
                created_at=grant_at,
                idempotency_key=idempotency_key,
            )
            await uow.append_outbox(event)
            audit = AuditEntry(
                audit_id=str(uuid.uuid4()),
                event_id=event_id,
                action="grant",
                actor_id=offer.actor_id,
                subject_id=offer.subject_id,
                consent_id=consent_id,
                snapshot_id=snapshot_id,
                payload=_outbox_payload(
                    ("capability", offer.capability),
                    ("offer_hash", offer.canonical_hash),
                    ("offer_id", offer.offer_id),
                    ("offer_version", str(offer.version)),
                    ("policy_version", offer.policy_version),
                ),
                created_at=grant_at,
            )
            await uow.append_audit(audit)
            if idempotency_key is not None:
                await uow.save_idempotency(
                    IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        content_hash=content_hash,
                        consent_id=consent_id,
                        version=new_chain.version,
                        snapshot_id=snapshot_id,
                        event_id=event_id,
                        audit_id=audit.audit_id,
                        subject_id=offer.subject_id,
                        actor_id=offer.actor_id,
                        created_at=grant_at,
                    )
                )
            await uow.commit()
            return ConsentOperationResult(
                evidence=new_chain,
                snapshot=snapshot,
                outbox_event=event,
                audit_entry=audit,
            )
        except BaseException:
            await uow.rollback()
            raise

    async def _terminate(
        self,
        *,
        consent_id: str,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...],
        reason: str,
        operation: Literal["revoke", "dispute"],
        expected_version: int | None,
        idempotency_key: str | None,
        now: datetime | None,
    ) -> ConsentOperationResult:
        acted_at = _utc(now or self._now(), "now")
        content_hash = _termination_content_hash(
            operation=operation,
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            reason=reason,
            relationships=relationships,
        )
        uow = await self._store.transaction()
        try:
            if idempotency_key is not None:
                existing = await uow.get_idempotency(idempotency_key)
                if existing is not None:
                    result = await _async_replay(uow, existing, content_hash)
                    await uow.commit()
                    return result
            latest = await uow.latest_consent(consent_id)
            if latest is None:
                raise ConsentNotFoundError(f"consent {consent_id!r} not found")
            current_head = await uow.lock_consent_head(
                actor_id,
                latest.actor_id,
                latest.subject_id,
                latest.binding_id,
                latest.binding_version,
                latest.capability,
                latest.purpose,
            )
            if current_head is None or current_head.consent_id != latest.consent_id:
                raise ConsentConflictError("consent is no longer the current authority head")
            latest = current_head
            await uow.lock_snapshot_head(
                actor_id,
                latest.subject_id,
                latest.binding_id,
                latest.binding_version,
            )
            if expected_version is not None and latest.version != expected_version:
                raise ConsentConflictError(
                    f"consent {consent_id!r} is at version {latest.version}, not {expected_version}"
                )
            if latest.status != "active":
                raise ConsentConflictError(
                    f"consent {consent_id!r} is {latest.status}; "
                    "only active consents can be revoked or disputed"
                )
            verify_revocation(
                evidence=latest,
                actor_id=actor_id,
                actor_kind=actor_kind,
                relationships=relationships,
                now=acted_at,
            )

            status: str = "revoked" if operation == "revoke" else "disputed"
            snapshot_id = str(uuid.uuid4())
            next_row = ConsentEvidence(
                consent_id=latest.consent_id,
                version=latest.version + 1,
                snapshot_id=snapshot_id,
                status=status,  # type: ignore[arg-type]
                subject_id=latest.subject_id,
                resource_owner_id=latest.resource_owner_id,
                actor_id=latest.actor_id,
                actor_kind=latest.actor_kind,
                device_id=latest.device_id,
                binding_id=latest.binding_id,
                binding_version=latest.binding_version,
                capability=latest.capability,
                purpose=latest.purpose,
                policy_version=latest.policy_version,
                evidence_id=str(uuid.uuid4()),
                offer_id=latest.offer_id,
                offer_version=latest.offer_version,
                offer_hash=latest.offer_hash,
                idempotency_key=idempotency_key,
                params=latest.params,
                valid_from=latest.valid_from,
                valid_until=latest.valid_until,
                supersedes_consent_id=None,
                superseded_by_consent_id=None,
                canonical_hash="",
            )
            await uow.append_consent(next_row)
            previous = await uow.latest_snapshot(
                latest.subject_id, latest.binding_id, latest.binding_version
            )
            if previous is None:
                raise ConsentConsistencyError(
                    f"no snapshot for consent {consent_id!r}; refusing to continue"
                )
            snapshot = await _async_build_snapshot(
                uow,
                subject_id=latest.subject_id,
                binding=previous.binding,
                relationships=previous.relationships,
                policy_version=previous.policy_version,
                created_at=acted_at,
                snapshot_id=snapshot_id,
            )
            await uow.append_snapshot(snapshot)

            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type=("consent_revoke" if operation == "revoke" else "consent_dispute"),
                aggregate_id=consent_id,
                version=next_row.version,
                payload=_outbox_payload(
                    ("actor_id", actor_id),
                    ("binding_id", latest.binding_id),
                    ("consent_id", consent_id),
                    ("reason", reason),
                    ("status", status),
                    ("subject_id", latest.subject_id),
                    ("version", str(next_row.version)),
                ),
                created_at=acted_at,
                idempotency_key=idempotency_key,
            )
            await uow.append_outbox(event)
            audit = AuditEntry(
                audit_id=str(uuid.uuid4()),
                event_id=event_id,
                action=operation,
                actor_id=actor_id,
                subject_id=latest.subject_id,
                consent_id=consent_id,
                snapshot_id=snapshot_id,
                payload=_outbox_payload(("reason", reason)),
                created_at=acted_at,
            )
            await uow.append_audit(audit)
            if idempotency_key is not None:
                await uow.save_idempotency(
                    IdempotencyRecord(
                        idempotency_key=idempotency_key,
                        content_hash=content_hash,
                        consent_id=consent_id,
                        version=next_row.version,
                        snapshot_id=snapshot_id,
                        event_id=event_id,
                        audit_id=audit.audit_id,
                        subject_id=latest.subject_id,
                        actor_id=actor_id,
                        created_at=acted_at,
                    )
                )
            await uow.commit()
            return ConsentOperationResult(
                evidence=next_row,
                snapshot=snapshot,
                outbox_event=event,
                audit_entry=audit,
            )
        except BaseException:
            await uow.rollback()
            raise

    async def revoke(
        self,
        consent_id: str,
        *,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...] = (),
        reason: str = "",
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        return await self._terminate(
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            relationships=relationships,
            reason=reason,
            operation="revoke",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
        )

    async def dispute(
        self,
        consent_id: str,
        *,
        actor_id: str,
        actor_kind: ActorKind,
        relationships: tuple[RelationshipEvidence, ...] = (),
        reason: str = "",
        expected_version: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> ConsentOperationResult:
        return await self._terminate(
            consent_id=consent_id,
            actor_id=actor_id,
            actor_kind=actor_kind,
            relationships=relationships,
            reason=reason,
            operation="dispute",
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
        )

    async def expire_due(self, now: datetime | None = None) -> tuple[ConsentOperationResult, ...]:
        acted_at = _utc(now or self._now(), "now")
        uow = await self._store.transaction()
        results: list[ConsentOperationResult] = []
        try:
            for chain in await uow.all_active_chains():
                if chain.valid_until > acted_at:
                    continue
                current_head = await uow.lock_consent_head(
                    "system",
                    chain.actor_id,
                    chain.subject_id,
                    chain.binding_id,
                    chain.binding_version,
                    chain.capability,
                    chain.purpose,
                )
                if current_head is None or current_head.canonical_hash != chain.canonical_hash:
                    continue
                chain = current_head
                await uow.lock_snapshot_head(
                    "system",
                    chain.subject_id,
                    chain.binding_id,
                    chain.binding_version,
                )
                previous = await uow.latest_snapshot(
                    chain.subject_id, chain.binding_id, chain.binding_version
                )
                if previous is None:
                    raise ConsentConsistencyError(
                        f"no snapshot for consent {chain.consent_id!r}; refusing to expire"
                    )
                snapshot_id = str(uuid.uuid4())
                expired_row = ConsentEvidence(
                    consent_id=chain.consent_id,
                    version=chain.version + 1,
                    snapshot_id=snapshot_id,
                    status="expired",
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
                    superseded_by_consent_id=None,
                    canonical_hash="",
                )
                await uow.append_consent(expired_row)
                snapshot = await _async_build_snapshot(
                    uow,
                    subject_id=chain.subject_id,
                    binding=previous.binding,
                    relationships=previous.relationships,
                    policy_version=previous.policy_version,
                    created_at=acted_at,
                    snapshot_id=snapshot_id,
                )
                await uow.append_snapshot(snapshot)
                event_id = str(uuid.uuid4())
                event = ConsentOutboxEvent(
                    event_id=event_id,
                    aggregate_type="consent_expire",
                    aggregate_id=chain.consent_id,
                    version=expired_row.version,
                    payload=_outbox_payload(
                        ("actor_id", "system"),
                        ("binding_id", chain.binding_id),
                        ("consent_id", chain.consent_id),
                        ("status", "expired"),
                        ("subject_id", chain.subject_id),
                        ("version", str(expired_row.version)),
                    ),
                    created_at=acted_at,
                    idempotency_key=None,
                )
                await uow.append_outbox(event)
                audit = AuditEntry(
                    audit_id=str(uuid.uuid4()),
                    event_id=event_id,
                    action="expire",
                    actor_id="system",
                    subject_id=chain.subject_id,
                    consent_id=chain.consent_id,
                    snapshot_id=snapshot_id,
                    payload=_outbox_payload(("reason", "due")),
                    created_at=acted_at,
                )
                await uow.append_audit(audit)
                results.append(
                    ConsentOperationResult(
                        evidence=expired_row,
                        snapshot=snapshot,
                        outbox_event=event,
                        audit_entry=audit,
                    )
                )
            await uow.commit()
            return tuple(results)
        except BaseException:
            await uow.rollback()
            raise

    async def snapshot(
        self,
        subject_id: str,
        *,
        binding: BindingEvidence,
        relationships: tuple[RelationshipEvidence, ...] = (),
        policy_version: str | None = None,
        now: datetime | None = None,
    ) -> ConsentSnapshot:
        acted_at = _utc(now or self._now(), "now")
        uow = await self._store.transaction()
        try:
            await uow.lock_snapshot_head(
                "system", subject_id, binding.binding_id, binding.version
            )
            previous = await uow.latest_snapshot(subject_id, binding.binding_id, binding.version)
            resolved_policy = policy_version or (
                previous.policy_version if previous is not None else ""
            )
            if not resolved_policy:
                raise ValueError("policy_version is required for the first snapshot")
            candidate = await _async_build_snapshot(
                uow,
                subject_id=subject_id,
                binding=binding,
                relationships=relationships,
                policy_version=resolved_policy,
                created_at=acted_at,
                snapshot_id=str(uuid.uuid4()),
            )
            if previous is not None and _snapshot_unchanged(previous, candidate):
                await uow.commit()
                return previous
            snapshot = _with_new_identity(
                candidate,
                snapshot_id=str(uuid.uuid4()),
                version=(previous.version + 1 if previous is not None else 1),
                created_at=acted_at,
            )
            await uow.append_snapshot(snapshot)
            event_id = str(uuid.uuid4())
            event = ConsentOutboxEvent(
                event_id=event_id,
                aggregate_type="consent_snapshot",
                aggregate_id=snapshot.snapshot_id,
                version=snapshot.version,
                payload=_outbox_payload(
                    ("actor_id", "system"),
                    ("binding_id", binding.binding_id),
                    ("binding_version", str(binding.version)),
                    ("snapshot_id", snapshot.snapshot_id),
                    ("subject_id", subject_id),
                ),
                created_at=acted_at,
                idempotency_key=None,
            )
            await uow.append_outbox(event)
            await uow.append_audit(
                AuditEntry(
                    audit_id=str(uuid.uuid4()),
                    event_id=event_id,
                    action="snapshot",
                    actor_id="system",
                    subject_id=subject_id,
                    consent_id=None,
                    snapshot_id=snapshot.snapshot_id,
                    payload=_outbox_payload(("version", str(snapshot.version))),
                    created_at=acted_at,
                )
            )
            await uow.commit()
            return snapshot
        except BaseException:
            await uow.rollback()
            raise


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


__all__ = [
    "AsyncEvidenceResolverPort",
    "AsyncConsentAuthority",
    "ConsentAuthority",
    "ConsentDeniedError",
    "ConsentOperationResult",
    "EvidenceResolverPort",
    "GUARDIAN_RELATION_TYPES",
    "GUARDIAN_WHITELIST",
    "NOT_GRANTABLE",
    "SENSITIVE_ADULT_CAPABILITIES",
    "SubjectProof",
    "verify_grant",
    "verify_revocation",
]
