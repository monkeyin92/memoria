"""Auto-extract archive births stay candidate and below confidence 1.0."""

from __future__ import annotations

from services.common.memory_decision_contract import decide_auto_extract
from services.guardian.preference_extract import DEFAULT_AUTO_CONFIDENCE, RULE_VERSION
from services.memory_scope.child_layers import archive_birth_stamp


def test_parent_archive_birth_stays_confirmed() -> None:
    assert archive_birth_stamp(None) == ("confirmed", 1.0, "child_archive_fact")


def test_auto_extract_archive_birth_is_candidate_below_one() -> None:
    decision = decide_auto_extract(
        subject_key="child-1",
        predicate="preference",
        object_text="喜欢画画",
        polarity="positive",
        rule_version=RULE_VERSION,
        lifecycle="pending_confirm",
        source_session_id="sess-1",
        confidence=DEFAULT_AUTO_CONFIDENCE,
        record_id="charch:auto-1",
    )
    status, confidence, reason = archive_birth_stamp(decision)
    assert decision.fact.provenance == "auto_extract"
    assert decision.lifecycle == "pending_confirm"
    assert decision.record_id == "charch:auto-1"
    assert status == "candidate"
    assert confidence == DEFAULT_AUTO_CONFIDENCE
    assert confidence < 1.0
    assert reason == "child_archive_auto_extract"


def test_missing_auto_confidence_stays_below_one() -> None:
    decision = decide_auto_extract(
        subject_key="child-1",
        predicate="preference",
        object_text="喜欢画画",
        polarity="positive",
        rule_version=RULE_VERSION,
        lifecycle="pending_confirm",
        confidence=None,
        record_id="charch:auto-2",
    )
    status, confidence, _reason = archive_birth_stamp(decision)
    assert status == "candidate"
    assert confidence == DEFAULT_AUTO_CONFIDENCE
    assert confidence < 1.0
