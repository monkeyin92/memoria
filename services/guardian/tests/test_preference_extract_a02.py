"""A02: subject / polarity / time extract hard-fail suite.

Wrong subject or wrong polarity fails the test. Misses are counted in the
report helper only; they never flip a case to pass.
"""

from __future__ import annotations

import sys
import types

import pytest

# Bypass services.guardian package __init__ side effects in lean envs.
sys.modules.setdefault("services.guardian", types.ModuleType("services.guardian"))
sys.modules["services.guardian"].__path__ = ["services/guardian"]

from services.guardian.session_recap import SessionTurn, extract_interest_facts  # noqa: E402
from services.guardian.tests.preference_extract_cases import (  # noqa: E402
    ALL_CASES,
    PreferenceExtractCase,
)


def _labels(utterance: str) -> list[str]:
    return [
        fact.text
        for fact in extract_interest_facts(
            "child-1",
            [SessionTurn(role="user", text=utterance)],
        )
    ]


def _hard_fail(case: PreferenceExtractCase, got: list[str]) -> str | None:
    got_set = set(got)
    missing = [label for label in case.must_include if label not in got_set]
    forbidden = [label for label in case.must_exclude if label in got_set]
    if case.exact and got_set != set(case.must_include):
        return (
            f"exact mismatch: want {sorted(case.must_include)} got {sorted(got_set)}; "
            f"missing={missing} forbidden_hits={forbidden}"
        )
    if forbidden:
        # Wrong subject or wrong polarity (or withdrawn still positive).
        return f"hard fail forbidden labels present: {forbidden} (got={got})"
    if missing:
        return f"miss (reported, not pass): missing={missing} (got={got})"
    return None


@pytest.mark.parametrize(
    "case",
    ALL_CASES,
    ids=[case.case_id for case in ALL_CASES],
)
def test_preference_extract_annotated_and_holdout(case: PreferenceExtractCase) -> None:
    got = _labels(case.utterance)
    failure = _hard_fail(case, got)
    if failure is None:
        return
    # Forbidden hits are hard fails. Exact/must_include misses also fail the
    # suite for annotated+holdout fixtures (product miss tolerance is separate;
    # these fixtures are the acceptance set, not a soft score).
    pytest.fail(f"{case.case_id}: {failure} — {case.note}")


def test_auto_extract_decisions_never_confirmed_or_certain() -> None:
    from services.guardian.preference_extract import (
        DEFAULT_AUTO_CONFIDENCE,
        RULE_VERSION,
        extract_preference_decisions,
    )

    decisions = extract_preference_decisions(
        subject_key="child-1",
        turns=[SessionTurn(role="user", text="我最喜欢恐龙")],
        source_session_id="sess-1",
    )
    assert [decision.fact.object_text for decision in decisions] == ["喜欢恐龙"]
    for decision in decisions:
        assert decision.fact.provenance == "auto_extract"
        assert decision.fact.rule_version == RULE_VERSION
        assert decision.lifecycle != "confirmed"
        assert decision.fact.confidence == DEFAULT_AUTO_CONFIDENCE
        assert decision.fact.confidence is not None
        assert decision.fact.confidence < 1.0


def test_original_three_a00_examples_no_wrong_subject_or_polarity() -> None:
    """A00 E1–E3 close criterion: no wrong-subject / wrong-polarity labels."""

    assert "喜欢恐龙" not in _labels("我喜欢画画，妈妈也喜欢恐龙")
    assert "喜欢画画" in _labels("我喜欢画画，妈妈也喜欢恐龙")
    assert "喜欢恐龙" not in _labels("我不喜欢恐龙，我喜欢小狗")
    assert "喜欢小狗" in _labels("我不喜欢恐龙，我喜欢小狗")
    assert "喜欢恐龙" not in _labels("我喜欢恐龙但现在不喜欢了")
    assert "喜欢恐龙" not in _labels("我喜欢恐龙，现在不喜欢了")
    assert "喜欢恐龙" not in _labels("以前我喜欢恐龙现在不喜欢")
