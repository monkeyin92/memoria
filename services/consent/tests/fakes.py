"""Explicit test adapters for external evidence and pre-existing offer fixtures."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from services.consent.authority import (
    ConsentAuthority,
    ConsentOperationResult,
    EvidenceResolverPort,
    SubjectProof,
)
from services.consent.evidence import (
    ActorKind,
    BindingEvidence,
    ConsentOffer,
    RelationshipEvidence,
)
from services.consent.store import ConsentStorePort


class PassthroughEvidenceResolver(EvidenceResolverPort):
    """Marks test fixture payloads as trusted; never use outside tests."""

    def resolve_subject(self, candidate: SubjectProof) -> SubjectProof:
        return candidate

    def resolve_binding(self, candidate: BindingEvidence, subject_id: str) -> BindingEvidence:
        del subject_id
        return candidate

    def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]:
        del actor_id, subject_id, binding_id
        return candidates


class PersistingFixtureAuthority(ConsentAuthority):
    """Persists legacy offer fixtures before exercising the production grant path."""

    def __init__(
        self,
        store: ConsentStorePort,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(store, resolver=PassthroughEvidenceResolver(), now=now)

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
        if isinstance(offer, ConsentOffer):
            self.create_offer(
                offer_id=offer.offer_id,
                version=offer.version,
                status=offer.status,
                issuer=offer.issuer,
                supersedes_offer_id=offer.supersedes_offer_id,
                capability=offer.capability,
                subject_id=offer.subject_id,
                actor_id=offer.actor_id,
                resource_owner_id=offer.resource_owner_id,
                purpose=offer.purpose,
                params=offer.params,
                valid_from=offer.valid_from,
                valid_until=offer.valid_until,
                policy_version=offer.policy_version,
                created_at=offer.created_at,
            )
        return super().grant(
            offer,
            expected_version=expected_version,
            subject=subject,
            binding=binding,
            relationships=relationships,
            actor_kind=actor_kind,
            idempotency_key=idempotency_key,
            now=now,
        )


__all__ = ["PassthroughEvidenceResolver", "PersistingFixtureAuthority"]
