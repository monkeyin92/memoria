from __future__ import annotations

import pytest

from services.common.memory_decision_contract import (
    CONTRACT_VERSION,
    PRODUCT_PROMISE_MATRIX,
    CandidateFact,
    MemoryDecision,
    MemoryDecisionContractError,
    decide_auto_extract,
    display_for,
    promise_matrix_by_id,
    recall_for,
    save_receipt,
)


def test_contract_version_is_stable() -> None:
    assert CONTRACT_VERSION == "memory-decision-v1"


def test_auto_extract_cannot_be_confirmed() -> None:
    with pytest.raises(MemoryDecisionContractError, match="confirmed"):
        decide_auto_extract(
            subject_key="self",
            predicate="preference",
            object_text="喜欢恐龙",
            polarity="positive",
            rule_version="session-recap-v1",
            lifecycle="confirmed",
            confidence=0.8,
            record_id="rec-1",
        )


def test_auto_extract_cannot_claim_accuracy_one() -> None:
    with pytest.raises(MemoryDecisionContractError, match="confidence/accuracy=1.0"):
        CandidateFact(
            subject_key="self",
            predicate="preference",
            object_text="喜欢画画",
            polarity="positive",
            provenance="auto_extract",
            rule_version="session-recap-v1",
            confidence=1.0,
        )


def test_constructing_confirmed_auto_extract_decision_fails() -> None:
    fact = CandidateFact(
        subject_key="self",
        predicate="preference",
        object_text="喜欢画画",
        polarity="positive",
        provenance="auto_extract",
        rule_version="session-recap-v1",
        confidence=0.7,
    )
    with pytest.raises(MemoryDecisionContractError, match="confirmed"):
        MemoryDecision(fact=fact, lifecycle="confirmed", record_id="rec-1")


def test_auto_extract_pending_confirm_is_allowed() -> None:
    decision = decide_auto_extract(
        subject_key="self",
        predicate="preference",
        object_text="喜欢画画",
        polarity="positive",
        rule_version="session-recap-v1",
        lifecycle="pending_confirm",
        confidence=0.7,
        record_id="rec-pending-1",
        fact_version=1,
    )
    assert decision.lifecycle == "pending_confirm"
    assert decision.fact.provenance == "auto_extract"
    assert display_for(decision.lifecycle) == "parent_review"
    assert recall_for(decision.lifecycle, provenance=decision.fact.provenance) == "after_confirm"


def test_async_accepted_receipt_must_not_claim_saved() -> None:
    decision = decide_auto_extract(
        subject_key="self",
        predicate="preference",
        object_text="喜欢蓝色",
        polarity="positive",
        rule_version="session-recap-v1",
        lifecycle="eligible_unwritten",
        confidence=0.6,
    )
    receipt = save_receipt(
        decision=decision,
        write_mode="async_accepted",
        reason="queued_for_persist",
    )
    assert receipt.may_claim_saved() is False
    assert receipt.may_claim_remembered_aloud() is False


def test_sync_persisted_confirmed_explicit_may_claim_remembered() -> None:
    fact = CandidateFact(
        subject_key="self",
        predicate="preference",
        object_text="我最喜欢蓝色",
        polarity="positive",
        provenance="explicit_remember",
        rule_version="explicit-memory-v2",
        confidence=0.95,
    )
    decision = MemoryDecision(
        fact=fact,
        lifecycle="confirmed",
        record_id="claim-blue-1",
        fact_version=1,
    )
    receipt = save_receipt(
        decision=decision,
        write_mode="sync_persisted",
        reason="explicit-memory-low-risk",
    )
    assert receipt.may_claim_saved() is True
    assert receipt.may_claim_remembered_aloud() is True


def test_promise_matrix_covers_dinosaur_and_blue() -> None:
    blue = promise_matrix_by_id("blue-closed-vocab")
    dino = promise_matrix_by_id("dinosaur-not-allowlisted")
    assert blue.asks is True
    assert blue.will_save is True
    assert blue.promise == "may_claim_remembered_after_sync"
    assert dino.asks is True
    assert dino.will_save is False
    assert dino.promise == "must_not_claim_remembered"
    assert {row.example_id for row in PRODUCT_PROMISE_MATRIX} >= {
        "blue-closed-vocab",
        "dinosaur-not-allowlisted",
        "auto-extract-preference",
        "async-not-saved",
    }


def test_auto_extract_matrix_row_never_promises_remembered() -> None:
    row = promise_matrix_by_id("auto-extract-preference")
    assert row.asks is False
    assert row.will_save is False
    assert row.promise == "must_not_claim_remembered"
    assert row.expected_lifecycle_if_written == "pending_confirm"
    assert row.recall == "after_confirm"
