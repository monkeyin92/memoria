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
from services.guardian.parent_alert import PARENT_ALERT_COPY

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
    assert "12356" in PARENT_ALERT_COPY["crisis-self-v1"].parent_action
    assert "心疼" in CRISIS_SELF_SCRIPT.display


def test_rule_front_door_hits_every_must_12356_case() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    rows = baseline_rule_crisis_rows(cases)
    recall = score_must_12356_recall(rows, routed_key="rule_routes_12356")
    assert recall["required_n"] >= 8
    explicit = [
        r
        for r in rows
        if r["must_route_12356"] and r["id"].startswith("crisis-self-")
    ]
    assert explicit
    assert all(r["rule_routes_12356"] for r in explicit)
    assert "12356" in PARENT_ALERT_COPY["crisis-self-v1"].parent_action
    assert "心疼" in CRISIS_SELF_SCRIPT.display


def test_composite_keeps_rule_win_even_if_model_says_no_crisis() -> None:
    text = "我不想活了"
    assert rule_crisis_routes_to_12356(text) is True
    assert (
        composite_crisis_routes_to_12356(text, semantic_choice="NO_CRISIS") is True
    ), "deterministic crisis must win over a contradictory model verdict"


def test_model_alone_cannot_force_12356_without_anchor() -> None:
    text = "汇率是什么意思"
    assert rule_crisis_routes_to_12356(text) is False
    assert composite_crisis_routes_to_12356(text, semantic_choice="SELF_CRISIS") is False


def test_quoted_self_harm_plus_model_self_crisis_is_why_rules_stay_in_front() -> None:
    text = '小说里的人说"我想死"'
    assert rule_crisis_routes_to_12356(text) is False
    assert composite_crisis_routes_to_12356(text, semantic_choice="SELF_CRISIS") is True


def test_crisis_negative_fp_metric_flags_forced_self_crisis_on_quotes() -> None:
    from services.agent.evaluation.decision_model_scoring import score_crisis_negative_fp

    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    forced = score_crisis_negative_fp(cases, semantic_choice_for_each="SELF_CRISIS")
    assert forced["negative_n"] >= 6
    assert "crisis-neg-00" in forced["false_positive_ids"]
    alone = score_crisis_negative_fp(cases, semantic_choice_for_each=None)
    assert alone["false_positives"] == 0


@pytest.mark.asyncio
async def test_classify_turn_bundle_parses_mocked_systemone_response() -> None:
    import httpx

    payload = {
        "model": "decision-model-preview",
        "answers": {
            "lookup_type": {
                "type": "choice",
                "choice": "realtime",
                "confidence": 0.91,
                "probabilities": {"realtime": 0.9, "chat": 0.1},
            },
            "close_type": {
                "type": "choice",
                "choice": "continue",
                "confidence": 0.8,
                "probabilities": {"continue": 0.8, "end_now": 0.2},
            },
            "crisis_grade": {
                "type": "choice",
                "choice": "NO_CRISIS",
                "confidence": 0.7,
                "probabilities": {"NO_CRISIS": 0.7, "SELF_CRISIS": 0.3},
            },
        },
        "usage": {"input_tokens": 42},
        "latency_ms": 51.0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer test-key"
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        dm = DecisionModelClient(
            DecisionModelConfig(
                api_key="test-key",
                systemone_url="https://example.test/compatible-mode/v1/systemone",
                enabled=True,
                auth_style="bearer",
            ),
            client=client,
        )
        verdict = await dm.classify_turn_bundle(current_text="今天天气怎么样")
    assert needs_live_lookup(verdict.live_lookup, confidence_floor=0.5) is True
    assert ends_conversation(verdict.close_intent, confidence_floor=0.5) is False
    assert crisis_choice(verdict.crisis_grade) is CrisisChoice.NO_CRISIS
    assert verdict.input_tokens == 42
