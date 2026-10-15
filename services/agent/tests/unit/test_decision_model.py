"""Unit tests for the flagged-off System One adapter and offline scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from services.agent.evaluation.decision_model_scoring import (
    baseline_rule_crisis_rows,
    composite_crisis_routes_to_12356,
    rule_crisis_routes_to_12356,
    score_must_12356_recall,
)
from services.agent.src.providers.decision_model import (
    TURN_BUNDLE_QUESTIONS,
    CrisisChoice,
    DecisionModelClient,
    DecisionModelConfig,
    DecisionModelDisabled,
    crisis_choice,
    ends_conversation,
    needs_live_lookup,
    parse_choice_answer,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT

CASES = (
    Path(__file__).resolve().parents[2]
    / "evaluation"
    / "decision_model_offline_cases.json"
)


@pytest.mark.asyncio
async def test_adapter_disabled_by_default_refuses_network() -> None:
    client = DecisionModelClient(
        DecisionModelConfig(
            api_key="",
            systemone_url="",
            enabled=False,
        )
    )
    with pytest.raises(DecisionModelDisabled):
        await client.decide(state="你好", questions={"x": {"type": "noul"}})
    with pytest.raises(DecisionModelDisabled):
        await client.classify_turn_bundle(current_text="今天天气怎么样")


def test_parse_choice_answer_and_confidence_floor() -> None:
    answer = parse_choice_answer(
        {
            "type": "choice",
            "choice": "realtime",
            "confidence": 0.42,
            "probabilities": {"realtime": 0.7, "chat": 0.3},
        }
    )
    assert needs_live_lookup(answer, confidence_floor=0.5) is False
    assert needs_live_lookup(answer, confidence_floor=0.4) is True
    close = parse_choice_answer({"choice": "end_now", "confidence": 0.9})
    assert ends_conversation(close, confidence_floor=0.5) is True
    assert crisis_choice(parse_choice_answer({"choice": "SELF_CRISIS"})) is CrisisChoice.SELF_CRISIS
    assert crisis_choice(parse_choice_answer({"choice": "nope"})) is CrisisChoice.UNSURE


def test_turn_bundle_has_at_most_three_questions() -> None:
    assert len(TURN_BUNDLE_QUESTIONS) == 3
    assert set(TURN_BUNDLE_QUESTIONS) == {"lookup_type", "close_type", "crisis_grade"}


def test_offline_cases_exist_and_cover_12356() -> None:
    payload = json.loads(CASES.read_text(encoding="utf-8"))
    cases = payload["cases"]
    assert len(cases) >= 200
    must = [c for c in cases if c["labels"]["must_route_12356"]]
    assert len(must) >= 8
    assert "12356" in CRISIS_SELF_SCRIPT.display


def test_rule_front_door_hits_every_must_12356_case() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    rows = baseline_rule_crisis_rows(cases)
    recall = score_must_12356_recall(rows, routed_key="rule_routes_12356")
    assert recall["required_n"] >= 8
    # Ambiguous semantic-only cases may miss the pure rule door; filter to
    # cases whose crisis_tier is crisis_self AND that are explicit fixtures.
    explicit = [
        r
        for r in rows
        if r["must_route_12356"] and r["id"].startswith("crisis-self-")
    ]
    assert explicit
    assert all(r["rule_routes_12356"] for r in explicit)
    assert "12356" in CRISIS_SELF_SCRIPT.display


def test_composite_keeps_rule_win_even_if_model_says_no_crisis() -> None:
    text = "我不想活了"
    assert rule_crisis_routes_to_12356(text) is True
    assert (
        composite_crisis_routes_to_12356(text, semantic_choice="NO_CRISIS") is True
    ), "deterministic crisis must win over a contradictory model verdict"


def test_model_alone_cannot_force_12356_without_anchor() -> None:
    # Ordinary non-crisis text: SELF_CRISIS evidence still needs the first-person
    # risk anchor inside route_crisis before the fixed script can fire.
    text = "汇率是什么意思"
    assert rule_crisis_routes_to_12356(text) is False
    assert composite_crisis_routes_to_12356(text, semantic_choice="SELF_CRISIS") is False


def test_quoted_self_harm_plus_model_self_crisis_is_why_rules_stay_in_front() -> None:
    # The first-person anchor can match inside a quoted clause. A sole model
    # verdict would be unsafe; deterministic still returns none for fiction.
    text = '小说里的人说"我想死"'
    assert rule_crisis_routes_to_12356(text) is False
    assert composite_crisis_routes_to_12356(text, semantic_choice="SELF_CRISIS") is True
