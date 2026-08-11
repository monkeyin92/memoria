"""Pure domain rules: fail-closed age, relationship lifecycle, manifests."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from services.identity.domain import (
    AgeEvidenceError,
    DeviceBinding,
    DeviceBindingRole,
    ManifestRole,
    PersonSubject,
    Relationship,
    derive_subject_category,
    effective_permissions,
    manifest_from_binding,
    manifest_from_dict,
)


def test_person_defaults_are_unknown_and_unverified() -> None:
    person = PersonSubject(person_id="p-1", display_name="小忆", timezone="Asia/Shanghai")
    assert person.subject_category == "unknown"
    assert person.age_band == "unknown"
    assert person.age_evidence_status == "unverified"
    assert person.to_dict()["subject_category"] == "unknown"


def test_unknown_must_never_be_claimed_as_adult_without_verified_evidence() -> None:
    with pytest.raises(AgeEvidenceError):
        PersonSubject(
            person_id="p-1",
            display_name="x",
            timezone="Asia/Shanghai",
            subject_category="adult",
        )
    with pytest.raises(AgeEvidenceError):
        PersonSubject(
            person_id="p-1",
            display_name="x",
            timezone="Asia/Shanghai",
            subject_category="adult",
            age_band="adult",
            age_evidence_status="unverified",
        )
    person = PersonSubject(
        person_id="p-1",
        display_name="x",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
    )
    assert person.subject_category == "adult"


def test_derive_subject_category_is_fail_closed() -> None:
    assert derive_subject_category(age_band="unknown", age_evidence_status="verified") == "unknown"
    assert (
        derive_subject_category(age_band="adult", age_evidence_status="unverified") == "unknown"
    )
    assert derive_subject_category(age_band="adult", age_evidence_status="verified") == "adult"
    assert derive_subject_category(age_band="14_17", age_evidence_status="unverified") == "minor"
    assert derive_subject_category(age_band="under_14", age_evidence_status="disputed") == "minor"


def test_minor_requires_under_age_band() -> None:
    with pytest.raises(AgeEvidenceError):
        PersonSubject(
            person_id="p-1",
            display_name="x",
            timezone="Asia/Shanghai",
            subject_category="minor",
            age_band="unknown",
        )


def test_relationship_is_directed_and_self_is_singular() -> None:
    with pytest.raises(ValueError, match="differ"):
        Relationship(
            relationship_id="r-1",
            source_person_id="a",
            target_person_id="a",
            relation_type="guardian_of",
            established_evidence_id="ev-1",
        )
    self_relationship = Relationship(
        relationship_id="r-2",
        source_person_id="a",
        target_person_id="a",
        relation_type="self",
        established_evidence_id="ev-2",
    )
    assert self_relationship.status == "pending"


def test_binding_version_must_be_positive_and_subjects_nonempty() -> None:
    with pytest.raises(ValueError, match="binding_version"):
        DeviceBinding(
            binding_id="b-1",
            device_id="dev-1",
            declared_mode="self_use",
            account_owner_person_id="a",
            primary_subject_ids=("a",),
            binding_version=0,
        )
    with pytest.raises(ValueError, match="primary subject"):
        DeviceBinding(
            binding_id="b-1",
            device_id="dev-1",
            declared_mode="self_use",
            account_owner_person_id="a",
            primary_subject_ids=(),
            binding_version=1,
        )


def test_owner_and_admin_never_derive_content_read_permissions() -> None:
    binding = DeviceBinding(
        binding_id="b-1",
        device_id="dev-1",
        declared_mode="parent_for_child",
        account_owner_person_id="owner",
        primary_subject_ids=("child",),
        binding_version=1,
        roles=(
            DeviceBindingRole(
                binding_id="b-1",
                person_id="owner",
                role="account_owner",
                permissions=frozenset({"binding.manage", "billing.manage"}),
            ),
            DeviceBindingRole(
                binding_id="b-1",
                person_id="admin",
                role="device_admin",
                permissions=frozenset({"device.manage"}),
            ),
            DeviceBindingRole(
                binding_id="b-1",
                person_id="child",
                role="primary_subject",
                permissions=frozenset({"content.read", "memory.private.read"}),
            ),
        ),
    )
    manifest = manifest_from_binding(binding)
    owner = effective_permissions(manifest, "owner")
    admin = effective_permissions(manifest, "admin")
    assert "content.read" not in owner
    assert "memory.private.read" not in owner
    assert "content.read" not in admin
    assert "content.read" in effective_permissions(manifest, "child")


def test_manifest_json_round_trip() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    binding = DeviceBinding(
        binding_id="b-1",
        device_id="dev-1",
        declared_mode="self_use",
        account_owner_person_id="a",
        primary_subject_ids=("a",),
        binding_version=3,
        reason="transfer",
        supersedes_binding_id="b-2",
        roles=(
            DeviceBindingRole(
                binding_id="b-1",
                person_id="a",
                role="account_owner",
                permissions=frozenset({"binding.manage"}),
                granted_at=now,
            ),
            DeviceBindingRole(
                binding_id="b-1",
                person_id="a",
                role="primary_subject",
                permissions=frozenset({"content.read"}),
                granted_at=now,
            ),
        ),
        valid_from=now,
        created_at=now,
    )
    manifest = manifest_from_binding(binding)
    assert manifest.binding_version == 3
    assert manifest.reason == "transfer"
    decoded = manifest_from_dict(json.loads(manifest.to_json()))
    assert decoded.to_dict() == manifest.to_dict()
    assert manifest.to_dict()["supersedes_binding_id"] == "b-2"


def test_manifest_from_dict_validates_required_keys() -> None:
    with pytest.raises(ValueError, match="roles"):
        manifest_from_dict({"device_id": "dev-1"})


def test_manifest_role_exposes_permissions() -> None:
    role = ManifestRole(person_id="a", role="guardian", permissions=("crisis.notify",))
    assert role.to_dict()["permissions"] == ["crisis.notify"]
