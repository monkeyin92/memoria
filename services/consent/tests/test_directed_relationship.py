"""Directed Identity relationship evidence protocol and guardian projection."""

from __future__ import annotations

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
    ConsentParams,
    RelationshipEvidence,
    is_guardian_of,
    is_parent_of,
)
from services.consent.in_memory_store import InMemoryConsentStore
from services.identity.domain import Relationship

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)


def identity_relationship(
    relation_type: str,
    *,
    source: str,
    target: str,
    status: str = "active",
) -> Relationship:
    return Relationship(
        relationship_id=f"rel-{relation_type}",
        source_person_id=source,
        target_person_id=target,
        relation_type=relation_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        established_evidence_id="identity-evidence",
        confirmed_by_source_at=NOW - timedelta(days=1),
        confirmed_by_target_at=NOW - timedelta(days=1),
        created_at=NOW - timedelta(days=1),
        updated_at=NOW,
    )


def adapter_evidence(relationship: Relationship) -> RelationshipEvidence:
    """Test-only adapter at the future Identity -> Consent wire seam."""
    assert relationship.valid_until is not None
    return RelationshipEvidence(
        relationship_id=relationship.relationship_id,
        snapshot_id="identity-snapshot-v7",
        revision=7,
        relation_type=relationship.relation_type,
        source_person_id=relationship.source_person_id,
        target_person_id=relationship.target_person_id,
        status=relationship.status,
        binding_id="binding-1",
        valid_from=relationship.valid_from,
        valid_until=relationship.valid_until,
        canonical_hash="",
    )


def test_identity_guardian_and_parent_project_only_in_source_to_target_direction() -> None:
    guardian = adapter_evidence(
        identity_relationship("guardian_of", source="guardian", target="minor")
    )
    parent = adapter_evidence(identity_relationship("parent_of", source="guardian", target="minor"))

    assert is_guardian_of(guardian, guardian_id="guardian", subject_id="minor")
    assert is_parent_of(parent, parent_id="guardian", subject_id="minor")
    assert not is_guardian_of(guardian, guardian_id="minor", subject_id="guardian")
    assert not is_parent_of(parent, parent_id="minor", subject_id="guardian")


def test_inverse_identity_relations_never_project_to_guardian_authority() -> None:
    for relation_type in ("ward_of", "child_of"):
        inverse = adapter_evidence(
            identity_relationship(relation_type, source="minor", target="guardian")
        )
        assert not is_guardian_of(inverse, guardian_id="guardian", subject_id="minor")
        assert not is_parent_of(inverse, parent_id="guardian", subject_id="minor")


def test_relationship_wire_payload_uses_identity_directional_fields_only() -> None:
    evidence = adapter_evidence(
        identity_relationship("guardian_of", source="guardian", target="minor")
    )
    payload = evidence.to_canonical_dict()
    assert payload["source_person_id"] == "guardian"
    assert payload["target_person_id"] == "minor"
    assert "guardian_person_id" not in payload
    assert "subject_person_id" not in payload


class StaticResolver(EvidenceResolverPort):
    def __init__(self, relationships: tuple[RelationshipEvidence, ...]) -> None:
        self._relationships = relationships

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
        del candidates, actor_id, subject_id, binding_id
        return self._relationships


def grant_with_relationships(
    relationships: tuple[RelationshipEvidence, ...],
    *,
    supplied_relationships: tuple[RelationshipEvidence, ...] | None = None,
) -> object:
    authority = ConsentAuthority(InMemoryConsentStore(), resolver=StaticResolver(relationships))
    offer = authority.create_offer(
        offer_id="directed-offer",
        capability="chat",
        subject_id="minor",
        actor_id="guardian",
        resource_owner_id="minor",
        purpose="user_request",
        params=ConsentParams(max_session_seconds=60),
        valid_from=NOW - timedelta(hours=1),
        valid_until=NOW + timedelta(hours=1),
        created_at=NOW - timedelta(minutes=1),
        policy_version="policy-v2",
    )
    binding = BindingEvidence(
        binding_id="binding-1",
        version=1,
        device_id="device-1",
        status="active",
        declared_mode="parent_for_child",
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        canonical_hash="",
    )
    return authority.grant(
        offer.offer_id,
        expected_version=offer.version,
        actor_kind="guardian",
        subject=SubjectProof("minor", "minor", "verified"),
        binding=binding,
        relationships=supplied_relationships or relationships,
        now=NOW,
    )


@pytest.mark.parametrize("relation_type", ["guardian_of", "parent_of"])
def test_authority_accepts_only_forward_guardian_projection(relation_type: str) -> None:
    forward = adapter_evidence(
        identity_relationship(relation_type, source="guardian", target="minor")
    )
    result = grant_with_relationships((forward,))
    assert result.evidence.actor_kind == "guardian"  # type: ignore[attr-defined]


@pytest.mark.parametrize("relation_type", ["ward_of", "child_of"])
def test_authority_rejects_inverse_relation_types(relation_type: str) -> None:
    inverse = adapter_evidence(
        identity_relationship(relation_type, source="minor", target="guardian")
    )
    with pytest.raises(ConsentDeniedError) as caught:
        grant_with_relationships((inverse,))
    assert caught.value.reason == "relationship_not_active"


def test_authority_multi_relation_scan_matches_only_correct_direction() -> None:
    inverse = adapter_evidence(identity_relationship("ward_of", source="minor", target="guardian"))
    forward = adapter_evidence(
        identity_relationship("guardian_of", source="guardian", target="minor")
    )
    result = grant_with_relationships((inverse, forward))
    assert result.evidence.actor_id == "guardian"  # type: ignore[attr-defined]


def test_authoritative_revocation_or_revision_replacement_rejects_old_relation() -> None:
    supplied_old = adapter_evidence(
        identity_relationship("guardian_of", source="guardian", target="minor")
    )
    authoritative_new = RelationshipEvidence.from_canonical_dict(
        {
            **supplied_old.to_canonical_dict(),
            "revision": supplied_old.revision + 1,
            "status": "revoked",
            "canonical_hash": "",
        }
    )
    with pytest.raises(ConsentDeniedError) as caught:
        grant_with_relationships((authoritative_new,), supplied_relationships=(supplied_old,))
    assert caught.value.reason == "relationship_not_active"


def test_snapshot_direction_change_invalidates_same_revision_projection() -> None:
    authority = ConsentAuthority(InMemoryConsentStore())
    binding = BindingEvidence(
        binding_id="binding-1",
        version=1,
        device_id="device-1",
        status="active",
        declared_mode="parent_for_child",
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        canonical_hash="",
    )
    forward = adapter_evidence(
        identity_relationship("guardian_of", source="guardian", target="minor")
    )
    reversed_direction = RelationshipEvidence.from_canonical_dict(
        {
            **forward.to_canonical_dict(),
            "source_person_id": "minor",
            "target_person_id": "guardian",
            "canonical_hash": "",
        }
    )
    first = authority.snapshot(
        "minor",
        binding=binding,
        relationships=(forward,),
        policy_version="policy-v2",
        now=NOW,
    )
    second = authority.snapshot(
        "minor",
        binding=binding,
        relationships=(reversed_direction,),
        policy_version="policy-v2",
        now=NOW + timedelta(minutes=1),
    )
    assert second.snapshot_id != first.snapshot_id
    assert second.version == first.version + 1
