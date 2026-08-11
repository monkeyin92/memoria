"""PolicyContext strict construction validation and canonical context_hash."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from services.policy.context import context_hash
from services.policy.tests.fakes import (
    FakeConsentParams,
    make_binding,
    make_consent,
    make_context,
    make_relationship,
)

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)


@pytest.mark.parametrize("field", ["session_epoch", "binding_version"])
def test_bool_epoch_or_version_is_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="integer"):
        make_context(**{field: True})  # type: ignore[arg-type]


def test_bool_subject_revision_is_rejected() -> None:
    with pytest.raises(ValueError, match="integer"):
        make_context(subject_revision=True)  # type: ignore[arg-type]


def test_float_epoch_is_rejected() -> None:
    with pytest.raises(ValueError, match="integer"):
        make_context(session_epoch=1.5)  # type: ignore[arg-type]


def test_zero_epoch_and_version_are_rejected() -> None:
    with pytest.raises(ValueError):
        make_context(session_epoch=0)
    with pytest.raises(ValueError):
        make_context(binding_version=0)


def test_negative_subject_revision_is_rejected() -> None:
    with pytest.raises(ValueError):
        make_context(subject_revision=-1)


def test_naive_evaluated_at_is_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        make_context(evaluated_at=datetime(2026, 8, 9, 8, 0))


def test_aware_evaluated_at_is_accepted() -> None:
    context = make_context(evaluated_at=NOW)
    assert context.evaluated_at == NOW


@pytest.mark.parametrize(
    "field",
    ["actor_id", "subject_id", "resource_owner_id", "device_id", "binding_id"],
)
def test_empty_ids_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        make_context(**{field: ""})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["actor_id", "device_id", "session_id", "runtime_profile_id"],
)
def test_overlong_ids_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match="too long"):
        make_context(**{field: "x" * 300})  # type: ignore[arg-type]


def test_whitespace_only_ids_are_rejected() -> None:
    with pytest.raises(ValueError):
        make_context(actor_id="   ")


@pytest.mark.parametrize("bad_key", ["", "   ", "x" * 300])
def test_invalid_idempotency_key_is_rejected(bad_key: str) -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        make_context(idempotency_key=bad_key)


@pytest.mark.parametrize("confidence", [-0.1, 1.5])
def test_out_of_range_speaker_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        make_context(speaker_confidence=confidence)


def test_bool_speaker_confidence_is_rejected() -> None:
    with pytest.raises(ValueError, match="confidence"):
        make_context(speaker_confidence=True)  # type: ignore[arg-type]


def test_none_and_boundary_confidence_are_accepted() -> None:
    assert make_context(speaker_confidence=None).speaker_confidence is None
    assert make_context(speaker_confidence=0.0).speaker_confidence == 0.0
    assert make_context(speaker_confidence=1.0).speaker_confidence == 1.0


def test_invalid_capability_is_rejected() -> None:
    with pytest.raises(ValueError, match="capability"):
        make_context(capability="teleport")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("declared_device_mode", "teleport"),
        ("current_session_mode", "teleport"),
        ("subject_category", "teleport"),
        ("age_band", "teleport"),
        ("speaker_state", "teleport"),
    ],
)
def test_invalid_contract_enums_are_rejected(field: str, bad: str) -> None:
    with pytest.raises(ValueError):
        make_context(**{field: bad})  # type: ignore[arg-type]


@pytest.mark.parametrize("trust", ["", "unknown", "semi-trusted", "TRUSTED"])
def test_invalid_device_trust_is_rejected(trust: str) -> None:
    with pytest.raises(ValueError, match="device_trust"):
        make_context(device_trust=trust)


@pytest.mark.parametrize(
    "trust",
    ["trusted", "verified", "offline", "untrusted", "revoked"],
)
def test_valid_device_trust_values_are_accepted(trust: str) -> None:
    assert make_context(device_trust=trust).device_trust == trust


@pytest.mark.parametrize("purpose", ["", "teleport", "ALL_CAPS"])
def test_invalid_purpose_is_rejected(purpose: str) -> None:
    with pytest.raises(ValueError, match="purpose"):
        make_context(purpose=purpose)


def test_invalid_safety_state_is_rejected() -> None:
    with pytest.raises(ValueError, match="safety_state"):
        make_context(safety_state="panic")


def test_invalid_data_classification_is_rejected() -> None:
    with pytest.raises(ValueError, match="data_classification"):
        make_context(data_classification="secret")


def test_invalid_jurisdiction_is_rejected() -> None:
    with pytest.raises(ValueError, match="jurisdiction"):
        make_context(jurisdiction="")


def test_consent_evidence_rejects_non_port_objects() -> None:
    with pytest.raises(ValueError, match="consent_evidence"):
        make_context(consent_evidence=(object(),))  # type: ignore[arg-type]


def test_relationship_evidence_rejects_non_port_objects() -> None:
    with pytest.raises(ValueError, match="relationship_evidence"):
        make_context(relationship_evidence=(object(),))  # type: ignore[arg-type]


def test_relationship_evidence_rejects_non_identity_relation_type() -> None:
    with pytest.raises(ValueError, match="relation_type"):
        make_context(
            relationship_evidence=(
                make_relationship(relation_type="guardian", now=NOW),
            )
        )


def test_binding_evidence_rejects_non_port_objects() -> None:
    with pytest.raises(ValueError, match="binding_evidence"):
        make_context(binding_evidence=object())  # type: ignore[arg-type]


def test_list_evidence_is_normalized_to_tuple() -> None:
    consent = make_consent(capability="chat", purpose="user_request", now=NOW)
    context = make_context(
        consent_evidence=[consent]  # type: ignore[arg-type]
    )
    assert context.consent_evidence == (consent,)


def test_context_hash_is_sha256_hex() -> None:
    digest = context_hash(make_context(evaluated_at=NOW))
    assert len(digest) == 64
    int(digest, 16)


def test_context_hash_is_deterministic() -> None:
    context = make_context(evaluated_at=NOW)
    assert context_hash(context) == context_hash(context)


@pytest.mark.parametrize(
    "field",
    [
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
        "binding_id",
        "binding_version",
        "session_id",
        "session_epoch",
        "runtime_profile_id",
        "subject_revision",
    ],
)
def test_context_hash_changes_with_context_field(field: str) -> None:
    context = make_context(evaluated_at=NOW)
    digest = context_hash(context)
    replacement: dict[str, object] = {
        "actor_id": "person-other-actor",
        "subject_id": "person-other-subject",
        "resource_owner_id": "person-other-owner",
        "device_id": "device-other",
        "capability": "tutor",
        "purpose": "voice_clone",
        "declared_device_mode": "family_shared",
        "current_session_mode": "family_shared",
        "subject_category": "minor",
        "age_band": "14_17",
        "speaker_state": "unconfirmed",
        "speaker_confidence": 0.5,
        "device_trust": "verified",
        "safety_state": "concern",
        "jurisdiction": "SG",
        "data_classification": "biometric",
        "binding_id": "binding-other",
        "binding_version": 2,
        "session_id": "session-other",
        "session_epoch": 2,
        "runtime_profile_id": "profile-other",
        "subject_revision": 1,
    }
    other = replace(context, **{field: replacement[field]})  # type: ignore[arg-type]
    assert context_hash(other) != digest


def test_context_hash_changes_with_evaluated_at() -> None:
    digest = context_hash(make_context(evaluated_at=NOW))
    later = make_context(evaluated_at=NOW + timedelta(minutes=5))
    assert context_hash(later) != digest


def test_context_hash_covers_consent_evidence_fields() -> None:
    consent = make_consent(capability="voice_clone_use", purpose="user_request", now=NOW)
    context = make_context(
        capability="voice_clone_use",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    digest = context_hash(context)
    for mutate in (
        replace(consent, consent_id="consent-other"),
        replace(consent, snapshot_id="snap-other"),
        replace(consent, version=2),
        replace(consent, status="revoked"),
        replace(consent, subject_id="person-other"),
        replace(consent, resource_owner_id="person-other"),
        replace(consent, actor_id="person-other"),
        replace(consent, actor_kind="guardian"),
        replace(consent, device_id="device-other"),
        replace(consent, binding_id="binding-other"),
        replace(consent, binding_version=2),
        replace(consent, capability="payment"),
        replace(consent, purpose="voice_clone"),
        replace(consent, policy_version="policy-other"),
        replace(consent, evidence_id="evidence-other"),
        replace(consent, offer_id="offer-other"),
        replace(consent, idempotency_key="evidence-idempotency-other"),
        replace(
            consent,
            params=FakeConsentParams(max_session_seconds=1200),
        ),
        replace(consent, valid_from=NOW - timedelta(hours=2)),
        replace(consent, valid_until=NOW + timedelta(hours=9)),
        replace(consent, supersedes_consent_id="consent-old"),
        replace(consent, superseded_by_consent_id="consent-new"),
        replace(consent, canonical_hash="d" * 64),
    ):
        assert context_hash(replace(context, consent_evidence=(mutate,))) != digest


def test_context_hash_covers_relationship_evidence_fields() -> None:
    relationship = make_relationship(now=NOW)
    context = make_context(
        subject_id="person-child",
        relationship_evidence=(relationship,),
        evaluated_at=NOW,
    )
    digest = context_hash(context)
    for mutate in (
        replace(relationship, relationship_id="rel-other"),
        replace(relationship, snapshot_id="rel-snap-other"),
        replace(relationship, revision=2),
        replace(relationship, relation_type="emergency_contact_for"),
        replace(relationship, status="revoked"),
        replace(relationship, source_person_id="person-other-guardian"),
        replace(relationship, target_person_id="person-other-child"),
        replace(relationship, binding_id="binding-other"),
        replace(relationship, valid_from=NOW - timedelta(hours=2)),
        replace(relationship, valid_until=NOW + timedelta(hours=9)),
        replace(relationship, canonical_hash="e" * 64),
    ):
        assert context_hash(replace(context, relationship_evidence=(mutate,))) != digest


def test_context_hash_covers_binding_evidence_fields() -> None:
    binding = make_binding(now=NOW)
    context = make_context(binding_evidence=binding, evaluated_at=NOW)
    digest = context_hash(context)
    for mutate in (
        replace(binding, binding_id="binding-other"),
        replace(binding, version=2),
        replace(binding, device_id="device-other"),
        replace(binding, status="revoked"),
        replace(binding, declared_mode="family_shared"),
        replace(binding, valid_from=NOW - timedelta(hours=2)),
        replace(binding, valid_until=NOW + timedelta(hours=9)),
        replace(binding, canonical_hash="f" * 64),
    ):
        assert context_hash(replace(context, binding_evidence=mutate)) != digest


def test_context_hash_is_order_independent_for_evidence() -> None:
    first = make_consent(
        consent_id="c-1",
        snapshot_id="snap-a",
        capability="chat",
        purpose="user_request",
        now=NOW,
    )
    second = make_consent(
        consent_id="c-2",
        snapshot_id="snap-b",
        capability="memory_capture",
        purpose="memory_capture",
        now=NOW,
    )
    context = make_context(
        capability="memory_capture",
        purpose="memory_capture",
        consent_evidence=(first, second),
        evaluated_at=NOW,
    )
    assert context_hash(context) == context_hash(
        replace(context, consent_evidence=(second, first))
    )


def test_non_authoritative_compat_fields_do_not_change_context_hash() -> None:
    context = make_context(
        relationship_roles=frozenset({"guardian"}),
        consent_kinds=frozenset({"voice_clone"}),
        evaluated_at=NOW,
    )
    digest = context_hash(context)
    changed = replace(
        context,
        relationship_roles=frozenset({"device_admin"}),
        consent_kinds=frozenset({"memory_retention"}),
        idempotency_key="transport-retry-key",
    )
    assert context_hash(changed) == digest
