"""Evidence seam helpers: effective consent subsets and active relationships."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.policy.evidence import (
    CANONICAL_RELATION_TYPES,
    active_relationship_for,
    binding_fence_ok,
    effective_consents_for,
    is_delegate_for,
    is_emergency_contact_for,
    is_guardian_of,
)
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_relationship,
)

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)


def test_effective_consents_filters_by_subject_binding_and_capability() -> None:
    matching = make_consent(
        consent_id="c-match",
        subject_id="person-adult",
        capability="voice_clone_use",
        now=NOW,
    )
    wrong_subject = make_consent(
        consent_id="c-subject",
        subject_id="person-other",
        capability="voice_clone_use",
        now=NOW,
    )
    wrong_binding = make_consent(
        consent_id="c-binding",
        subject_id="person-adult",
        binding_id="binding-other",
        capability="voice_clone_use",
        now=NOW,
    )
    wrong_capability = make_consent(
        consent_id="c-capability",
        subject_id="person-adult",
        capability="payment",
        now=NOW,
    )
    consents = (matching, wrong_subject, wrong_binding, wrong_capability)

    result = effective_consents_for(
        "person-adult",
        "binding-1",
        1,
        "voice_clone_use",
        NOW,
        consents,
    )

    assert result == (matching,)


@pytest.mark.parametrize(
    "status",
    ["revoked", "expired", "disputed", "superseded"],
)
def test_effective_consents_exclude_non_active_statuses(status: str) -> None:
    consent = make_consent(status=status, now=NOW)  # type: ignore[arg-type]
    result = effective_consents_for(
        "person-adult",
        "binding-1",
        1,
        "voice_clone_use",
        NOW,
        (consent,),
    )
    assert result == ()


def test_effective_consents_exclude_outside_validity_window() -> None:
    before = make_consent(now=NOW, valid_until=NOW - timedelta(minutes=1))
    after = make_consent(now=NOW, valid_from=NOW + timedelta(minutes=1))
    result = effective_consents_for(
        "person-adult",
        "binding-1",
        1,
        "voice_clone_use",
        NOW,
        (before, after),
    )
    assert result == ()


def test_active_relationship_matches_directed_type_and_target() -> None:
    guardian = make_relationship(
        relationship_id="r-guardian",
        relation_type="guardian_of",
        source_person_id="person-parent",
        target_person_id="person-child",
        now=NOW,
    )
    other_child = make_relationship(
        relationship_id="r-other",
        relation_type="guardian_of",
        source_person_id="person-parent",
        target_person_id="person-other-child",
        now=NOW,
    )
    emergency = make_relationship(
        relationship_id="r-emergency",
        relation_type="emergency_contact_for",
        source_person_id="person-contact",
        target_person_id="person-child",
        now=NOW,
    )

    result = active_relationship_for(
        "guardian_of", "person-child", NOW, (guardian, other_child, emergency)
    )

    assert result == (guardian,)


@pytest.mark.parametrize(
    "status",
    ["pending", "suspended", "revoked", "expired", "disputed"],
)
def test_active_relationship_excludes_non_active_statuses(status: str) -> None:
    relationship = make_relationship(status=status, now=NOW)  # type: ignore[arg-type]
    result = active_relationship_for(
        "guardian_of", "person-child", NOW, (relationship,)
    )
    assert result == ()


def test_directed_relationship_projection_never_authorizes_reverse_edges() -> None:
    guardian = make_relationship(
        relation_type="guardian_of",
        source_person_id="guardian",
        target_person_id="child",
        now=NOW,
    )
    ward = make_relationship(
        relation_type="ward_of",
        source_person_id="child",
        target_person_id="guardian",
        now=NOW,
    )
    child = make_relationship(
        relation_type="child_of",
        source_person_id="child",
        target_person_id="parent",
        now=NOW,
    )
    emergency = make_relationship(
        relation_type="emergency_contact_for",
        source_person_id="contact",
        target_person_id="adult",
        now=NOW,
    )
    delegate = make_relationship(
        relation_type="delegate_for",
        source_person_id="delegate",
        target_person_id="senior",
        now=NOW,
    )

    assert is_guardian_of(guardian, "guardian", "child")
    assert not is_guardian_of(ward, "guardian", "child")
    assert not is_guardian_of(child, "parent", "child")
    assert is_emergency_contact_for(emergency, "contact", "adult")
    assert is_delegate_for(delegate, "delegate", "senior")


def test_identity_relationship_can_be_projected_to_policy_directional_evidence() -> None:
    from services.identity.domain import ALL_RELATION_TYPES, Relationship

    assert CANONICAL_RELATION_TYPES == ALL_RELATION_TYPES

    identity_relationship = Relationship(
        relationship_id="identity-guardian",
        source_person_id="guardian",
        target_person_id="child",
        relation_type="guardian_of",
        status="active",
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(hours=1),
        established_evidence_id="identity-evidence",
        confirmed_by_source_at=NOW - timedelta(minutes=1),
        confirmed_by_target_at=NOW - timedelta(minutes=1),
    )
    adapter = make_relationship(
        relationship_id=identity_relationship.relationship_id,
        relation_type=identity_relationship.relation_type,
        source_person_id=identity_relationship.source_person_id,
        target_person_id=identity_relationship.target_person_id,
        status=identity_relationship.status,
        valid_from=identity_relationship.valid_from,
        valid_until=identity_relationship.valid_until,
        now=NOW,
    )

    assert is_guardian_of(adapter, "guardian", "child")


def test_binding_fence_requires_matching_active_binding() -> None:
    active = make_binding(now=NOW)
    assert binding_fence_ok(
        "binding-1", 1, NOW, active
    )
    assert not binding_fence_ok(
        "binding-1", 2, NOW, active
    )
    assert not binding_fence_ok(
        "binding-other", 1, NOW, active
    )
    revoked = make_binding(status="revoked", now=NOW)
    assert not binding_fence_ok("binding-1", 1, NOW, revoked)
    expired = make_binding(valid_until=NOW - timedelta(minutes=1), now=NOW)
    assert not binding_fence_ok("binding-1", 1, NOW, expired)
    assert not binding_fence_ok("binding-1", 1, NOW, None)
