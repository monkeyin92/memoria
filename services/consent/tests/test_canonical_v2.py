"""Consent must consume canonical object-v2 vocabulary without shadow enums."""

from __future__ import annotations

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    ALL_CAPABILITY_VALUES,
    ALL_PURPOSE_VALUES,
    ALL_RELATIONSHIP_TYPE_VALUES,
    OBJECT_INVARIANTS,
)
from services.consent.evidence import (
    ALL_RELATION_TYPE_VALUES,
    ALLOWED_PURPOSES,
)
from services.consent.tests.test_evidence import base_evidence, base_offer


def _canonical_memory_pairs() -> dict[str, str]:
    rule = next(
        rule
        for rule in OBJECT_INVARIANTS["PolicyReceiptV2"]
        if rule.get("kind") == "value_pairs"
    )
    pairs = rule["pairs"]
    assert isinstance(pairs, dict)
    return {str(capability): str(purpose) for capability, purpose in pairs.items()}


def test_vocabularies_are_derived_from_generated_canonical_v2() -> None:
    assert ALLOWED_PURPOSES == frozenset(item.value for item in ALL_PURPOSE_VALUES)
    assert ALL_RELATION_TYPE_VALUES == frozenset(
        item.value for item in ALL_RELATIONSHIP_TYPE_VALUES
    )
    assert {item.value for item in ALL_CAPABILITY_VALUES} >= set(_canonical_memory_pairs())


@pytest.mark.parametrize(
    ("capability", "purpose"),
    sorted(_canonical_memory_pairs().items()),
)
def test_canonical_memory_capability_purpose_pairs_are_accepted(
    capability: str,
    purpose: str,
) -> None:
    assert base_offer(capability=capability, purpose=purpose).purpose == purpose
    assert base_evidence(capability=capability, purpose=purpose).purpose == purpose


@pytest.mark.parametrize(
    ("capability", "purpose"),
    [
        ("memory_capture", "memory_promotion"),
        ("memory_promotion", "memory_capture"),
        ("family_shared_memory_proposal", "family_shared_memory_approval"),
        ("family_shared_memory_approval", "family_shared_memory_promotion"),
        ("family_shared_memory_promotion", "family_shared_memory_proposal"),
        ("chat", "memory_capture"),
    ],
)
def test_canonical_memory_capability_purpose_mismatch_fails_closed(
    capability: str,
    purpose: str,
) -> None:
    with pytest.raises(ValueError, match="capability/purpose"):
        base_offer(capability=capability, purpose=purpose)
    with pytest.raises(ValueError, match="capability/purpose"):
        base_evidence(capability=capability, purpose=purpose)


def test_unknown_purpose_fails_closed_for_offer_and_evidence() -> None:
    with pytest.raises(ValueError, match="unknown consent purpose"):
        base_offer(purpose="future_unknown_purpose")
    with pytest.raises(ValueError, match="unknown consent purpose"):
        base_evidence(purpose="future_unknown_purpose")
