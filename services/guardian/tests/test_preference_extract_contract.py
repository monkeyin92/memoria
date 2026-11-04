"""A02: auto-extract decisions never claim confirmed or confidence 1.0."""

from __future__ import annotations

import sys
import types

sys.modules.setdefault("services.guardian", types.ModuleType("services.guardian"))
sys.modules["services.guardian"].__path__ = ["services/guardian"]

from services.guardian.preference_extract import (
    DEFAULT_AUTO_CONFIDENCE,
    RULE_VERSION,
    extract_preference_decisions,
)
from services.guardian.session_recap import SessionTurn


def test_auto_extract_decisions_are_pending_or_eligible_never_confirmed() -> None:
    decisions = extract_preference_decisions(
        subject_key="child-1",
        turns=[SessionTurn(role="user", text="我喜欢画画")],
        source_session_id="sess-1",
    )
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.fact.provenance == "auto_extract"
    assert decision.fact.rule_version == RULE_VERSION
    assert decision.lifecycle in {"eligible_unwritten", "pending_confirm"}
    assert decision.lifecycle != "confirmed"
    assert decision.fact.confidence == DEFAULT_AUTO_CONFIDENCE
    assert decision.fact.confidence is not None
    assert decision.fact.confidence < 1.0


def test_write_archive_fact_rejects_confirmed_auto_birth() -> None:
    from services.common.memory_decision_contract import (
        CandidateFact,
        MemoryDecision,
        MemoryDecisionContractError,
    )

    fact = CandidateFact(
        subject_key="child-1",
        predicate="preference",
        object_text="喜欢画画",
        polarity="positive",
        provenance="auto_extract",
        rule_version=RULE_VERSION,
        confidence=0.75,
    )
    try:
        MemoryDecision(fact=fact, lifecycle="confirmed", record_id="rec-1")
        raised = False
    except MemoryDecisionContractError:
        raised = True
    assert raised
