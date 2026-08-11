"""Canonical device ownership transfer purpose contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.consent.authority import ConsentAuthority, SubjectProof
from services.consent.evidence import (
    DEVICE_TRANSFER_PURPOSE,
    BindingEvidence,
    ConsentParams,
)
from services.consent.in_memory_store import InMemoryConsentStore
from services.consent.tests.fakes import PassthroughEvidenceResolver
from services.identity.authority import TRANSFER_PURPOSE as IDENTITY_TRANSFER_PURPOSE

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def authority() -> ConsentAuthority:
    return ConsentAuthority(InMemoryConsentStore(), resolver=PassthroughEvidenceResolver())


def create_transfer_offer(consent: ConsentAuthority, *, purpose: str = "device_transfer") -> object:
    return consent.create_offer(
        offer_id="device-transfer-offer",
        capability="device_ownership_transfer",
        subject_id="adult",
        actor_id="adult",
        resource_owner_id="adult",
        purpose=purpose,
        params=ConsentParams(max_session_seconds=300),
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=10),
        created_at=NOW,
        policy_version="policy-v2",
    )


def test_device_transfer_offer_accepts_only_canonical_purpose() -> None:
    consent = authority()
    offer = create_transfer_offer(consent)
    assert offer.purpose == DEVICE_TRANSFER_PURPOSE  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="unknown consent purpose"):
        create_transfer_offer(authority(), purpose="device_ownership_transfer")


def test_device_transfer_offer_to_grant_preserves_canonical_purpose() -> None:
    consent = authority()
    offer = create_transfer_offer(consent)
    binding = BindingEvidence(
        binding_id="adult-binding",
        version=1,
        device_id="device-1",
        status="active",
        declared_mode="self_use",
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=1),
        canonical_hash="",
    )
    result = consent.grant(
        offer.offer_id,  # type: ignore[attr-defined]
        expected_version=offer.version,  # type: ignore[attr-defined]
        actor_kind="subject",
        subject=SubjectProof("adult", "adult", "verified"),
        binding=binding,
        now=NOW,
    )
    assert result.evidence.capability == "device_ownership_transfer"
    assert result.evidence.purpose == "device_transfer"


def test_identity_transfer_purpose_converged_on_canonical_value() -> None:
    # Identity's receipt seam now uses the consent/policy canonical purpose;
    # the cross-domain contract is one value and must stay aligned.
    assert DEVICE_TRANSFER_PURPOSE == "device_transfer"
    assert IDENTITY_TRANSFER_PURPOSE == DEVICE_TRANSFER_PURPOSE
