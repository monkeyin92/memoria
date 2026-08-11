"""Server-authoritative consent offer and evidence resolver tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from services.consent.authority import (
    ConsentAuthority,
    ConsentDeniedError,
    EvidenceResolverPort,
    SubjectProof,
)
from services.consent.evidence import (
    BindingEvidence,
    ConsentOffer,
    ConsentParams,
    RelationshipEvidence,
)
from services.consent.in_memory_store import InMemoryConsentStore
from services.consent.store import ConsentConflictError

NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


class FakeResolver(EvidenceResolverPort):
    def resolve_subject(self, candidate: SubjectProof) -> SubjectProof:
        if candidate.subject_id != "minor":
            raise LookupError(candidate.subject_id)
        return SubjectProof("minor", "minor", "verified")

    def resolve_binding(self, candidate: BindingEvidence, subject_id: str) -> BindingEvidence:
        if (candidate.binding_id, candidate.version, subject_id) != (
            "binding",
            1,
            "minor",
        ):
            raise LookupError((candidate.binding_id, candidate.version, subject_id))
        return BindingEvidence(
            binding_id="binding",
            version=1,
            device_id="device",
            status="active",
            declared_mode="parent_for_child",
            valid_from=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=30),
            canonical_hash="",
        )

    def resolve_relationships(
        self,
        candidates: tuple[RelationshipEvidence, ...],
        actor_id: str,
        subject_id: str,
        binding_id: str,
    ) -> tuple[RelationshipEvidence, ...]:
        del candidates
        if (actor_id, subject_id, binding_id) != ("guardian", "minor", "binding"):
            return ()
        return (
            RelationshipEvidence(
                relationship_id="relationship",
                snapshot_id="relationship-snapshot",
                revision=1,
                relation_type="guardian_of",
                status="active",
                source_person_id="guardian",
                target_person_id="minor",
                binding_id="binding",
                valid_from=NOW - timedelta(days=1),
                valid_until=NOW + timedelta(days=30),
                canonical_hash="",
            ),
        )


def params() -> ConsentParams:
    return ConsentParams(max_session_seconds=600, retention_ttl_seconds=3600)


def create_offer(
    authority: ConsentAuthority,
    *,
    version: int = 1,
    purpose: str = "user_request",
    valid_until: datetime | None = None,
) -> ConsentOffer:
    return authority.create_offer(
        offer_id="offer-authoritative",
        version=version,
        supersedes_offer_id="offer-authoritative" if version > 1 else None,
        capability="chat",
        actor_id="guardian",
        subject_id="minor",
        resource_owner_id="minor",
        purpose=purpose,
        params=params(),
        valid_from=NOW - timedelta(hours=1),
        valid_until=valid_until or NOW + timedelta(hours=1),
        policy_version="policy-v2",
        created_at=NOW - timedelta(minutes=5),
    )


def grant(authority: ConsentAuthority, offer: ConsentOffer) -> object:
    return authority.grant(
        offer.offer_id,
        expected_version=offer.version,
        subject=SubjectProof("minor", "minor", "verified"),
        binding=FakeResolver().resolve_binding(
            BindingEvidence(
                binding_id="binding",
                version=1,
                device_id="device",
                status="active",
                declared_mode="parent_for_child",
                valid_from=NOW - timedelta(days=1),
                valid_until=NOW + timedelta(days=30),
                canonical_hash="",
            ),
            "minor",
        ),
        relationships=(),
        actor_kind="guardian",
        idempotency_key="grant-authoritative",
        now=NOW,
    )


def test_create_offer_persists_offer_audit_and_outbox_atomically() -> None:
    store = InMemoryConsentStore()
    authority = ConsentAuthority(store, resolver=FakeResolver())
    created = create_offer(authority)

    with store.transaction() as uow:
        assert uow.latest_offer(created.offer_id) == created
        offer_events = [
            event for event in store.outbox_events if event.aggregate_type == "consent_offer"
        ]
        assert len(offer_events) == 1
        assert uow.audit_by_id(store.audit_entries[0].audit_id) is not None


def test_create_offer_same_content_replays_and_different_content_conflicts() -> None:
    store = InMemoryConsentStore()
    authority = ConsentAuthority(store, resolver=FakeResolver())
    first = create_offer(authority)
    assert create_offer(authority) == first
    assert len(store.outbox_events) == 1
    with pytest.raises(ConsentConflictError):
        create_offer(authority, purpose="runtime_profile_issue")


def test_grant_requires_resolver_and_uses_stored_offer_not_tampered_object() -> None:
    store = InMemoryConsentStore()
    unresolved = ConsentAuthority(store)
    stored = create_offer(unresolved)
    with pytest.raises(ConsentDeniedError) as unresolved_error:
        grant(unresolved, stored)
    assert unresolved_error.value.reason == "evidence_unresolved"

    authority = ConsentAuthority(store, resolver=FakeResolver())
    tampered = replace(
        stored,
        actor_id="attacker",
        subject_id="attacker",
        resource_owner_id="attacker",
        capability="payment",
        valid_until=NOW - timedelta(seconds=1),
        canonical_hash="",
    )
    result = authority.grant(
        tampered,
        expected_version=stored.version,
        subject=SubjectProof("minor", "minor", "verified"),
        binding=FakeResolver().resolve_binding(
            BindingEvidence(
                binding_id="binding",
                version=1,
                device_id="device",
                status="active",
                declared_mode="parent_for_child",
                valid_from=NOW - timedelta(days=1),
                valid_until=NOW + timedelta(days=30),
                canonical_hash="",
            ),
            "minor",
        ),
        relationships=(),
        actor_kind="guardian",
        idempotency_key="tampered-object",
        now=NOW,
    )
    assert result.evidence.actor_id == "guardian"
    assert result.evidence.subject_id == "minor"
    assert result.evidence.capability == "chat"
    assert result.evidence.offer_version == stored.version
    assert result.evidence.offer_hash == stored.canonical_hash


@pytest.mark.parametrize(
    ("subject_id", "binding_id"),
    [("other", "binding"), ("minor", "other")],
)
def test_grant_rejects_unresolved_subject_or_binding(subject_id: str, binding_id: str) -> None:
    authority = ConsentAuthority(InMemoryConsentStore(), resolver=FakeResolver())
    stored = create_offer(authority)
    with pytest.raises(ConsentDeniedError) as unresolved_error:
        authority.grant(
            stored.offer_id,
            expected_version=1,
            subject=SubjectProof(subject_id, "minor", "verified"),
            binding=BindingEvidence(
                binding_id=binding_id,
                version=1,
                device_id="device",
                status="active",
                declared_mode="parent_for_child",
                valid_from=NOW - timedelta(days=1),
                valid_until=NOW + timedelta(days=1),
                canonical_hash="",
            ),
            actor_kind="guardian",
            now=NOW,
        )
    assert unresolved_error.value.reason == "evidence_unresolved"


def test_expired_offer_and_stale_expected_version_fail_closed() -> None:
    authority = ConsentAuthority(InMemoryConsentStore(), resolver=FakeResolver())
    expired = create_offer(authority, valid_until=NOW)
    with pytest.raises(ConsentDeniedError) as expired_error:
        grant(authority, expired)
    assert expired_error.value.reason == "offer_expired"

    authority = ConsentAuthority(InMemoryConsentStore(), resolver=FakeResolver())
    first = create_offer(authority)
    create_offer(authority, version=2, purpose="runtime_profile_issue")
    with pytest.raises(ConsentConflictError, match="offer version"):
        grant(authority, first)
