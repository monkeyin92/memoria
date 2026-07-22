from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.persona.domain import PersonaEvidence
from services.persona.engine import PersonaEngine
from services.persona.qwen_extractor import (
    FallbackPersonaExtractor,
    QwenPersonaExtractor,
)
from services.persona.rules import RuleBasedPersonaExtractor


def _response(content: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content)}}]},
    )


def _structured_traits() -> dict[str, object]:
    return {
        "traits": [
            {
                "category": "narrative_style",
                "normalized_key": "chronological_with_reflection",
                "description": "讲述经历时通常按时间顺序展开，最后补充自己的反思",
                "context": "讲述工作经历",
                "counterexample": "紧急汇报时会先说结论",
            },
            {
                "category": "decision_habit",
                "normalized_key": "facts_then_cooling_off",
                "description": "重大决定前先核对事实，再留一晚冷静期",
                "context": "涉及长期承诺的重大决定",
                "counterexample": "紧急安全风险出现时会立即行动",
            },
            {
                "category": "value_priority",
                "normalized_key": "promise_over_convenience",
                "description": "答应他人的事情优先于一时方便",
                "context": "已经作出明确承诺时",
                "counterexample": "承诺会伤害家人安全时会重新协商",
            },
        ]
    }


@pytest.mark.asyncio
async def test_qwen_persona_extractor_requires_context_and_counterexamples() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-key"
        assert payload["response_format"] == {"type": "json_object"}
        assert "不得补充" in payload["messages"][1]["content"]
        return _response(_structured_traits())

    extractor = QwenPersonaExtractor(
        api_key="test-key",
        base_url="https://dashscope.test/v1",
        model="qwen-test",
        transport=httpx.MockTransport(handler),
    )
    evidence = PersonaEvidence(
        account_id="persona-account",
        source_event_id="persona-qwen-001",
        learning_allowed=True,
        scene="conversation",
    )

    traits = await extractor.extract(
        "讲工作经历时，我通常按时间说，最后补反思；但紧急汇报会先说结论。"
        "涉及长期承诺时，重大决定前先核对事实，再留一晚；出现紧急安全风险会立即行动。"
        "已经明确答应别人的事，我会优先做到；如果会伤害家人安全，就重新协商。",
        evidence,
    )

    assert extractor.version == "qwen-persona-json:qwen-test:v2"
    semantic = [
        trait
        for trait in traits
        if trait.category in {"narrative_style", "decision_habit", "value_priority"}
    ]
    assert [trait.category for trait in semantic] == [
        "narrative_style",
        "decision_habit",
        "value_priority",
    ]
    assert all(trait.context != evidence.scene for trait in semantic)
    assert all(trait.counterexample for trait in semantic)
    assert any(trait.category == "sentence_length" for trait in traits)


@pytest.mark.asyncio
async def test_qwen_persona_traits_round_trip_through_engine(tmp_path: Path) -> None:
    path = tmp_path / "persona-qwen.sqlite3"
    archive = LifeArchive.sqlite(path)
    await archive.record(
        EvidenceEvent(
            event_id="persona-qwen-001",
            account_id="persona-account",
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 16, 0, tzinfo=UTC),
            speaker_class="owner",
            source="test",
            payload={
                "text": "讲工作经历时，我通常按时间说，最后补反思；但紧急汇报会先说结论。"
                "涉及长期承诺时，重大决定前先核对事实，再留一晚；出现紧急安全风险会立即行动。"
                "已经明确答应别人的事，我会优先做到；如果会伤害家人安全，就重新协商。",
                "persona_eligible": True,
            },
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return _response(_structured_traits())

    engine = PersonaEngine.sqlite(
        path,
        extractor=QwenPersonaExtractor(
            api_key="test-key",
            base_url="https://dashscope.test/v1",
            model="qwen-test",
            transport=httpx.MockTransport(handler),
        ),
    )
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )
    observed = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="persona-qwen-001",
            learning_allowed=True,
        )
    )
    traits = await engine.traits(account_id="persona-account")

    assert observed.accepted is True
    assert {trait.category for trait in traits} >= {
        "narrative_style",
        "decision_habit",
        "value_priority",
    }
    decision = next(trait for trait in traits if trait.category == "decision_habit")
    assert decision.context == "涉及长期承诺的重大决定"
    assert decision.counterexample == "紧急安全风险出现时会立即行动"


@pytest.mark.asyncio
async def test_evidenced_emphasis_and_emotional_expression_round_trip_through_engine(
    tmp_path: Path,
) -> None:
    path = tmp_path / "persona-speech-style.sqlite3"
    archive = LifeArchive.sqlite(path)
    await archive.record(
        EvidenceEvent(
            event_id="persona-speech-style-001",
            account_id="persona-account",
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 7, 19, 16, 5, tzinfo=UTC),
            speaker_class="owner",
            source="test",
            payload={
                "text": "讲重要事情时我会重读结论；安慰家人时语气会更柔和、停顿更多。",
                "persona_eligible": True,
            },
        )
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return _response(
            {
                "traits": [
                    {
                        "category": "emphasis_style",
                        "normalized_key": "stress_key_conclusion",
                        "description": "讲重要事情时会重读关键结论",
                        "context": "表达重要结论",
                        "counterexample": "",
                    },
                    {
                        "category": "emotional_expression",
                        "normalized_key": "gentle_when_comforting_family",
                        "description": "安慰家人时语气更柔和并保留更多停顿",
                        "context": "安慰家人",
                        "counterexample": "",
                    },
                ]
            }
        )

    engine = PersonaEngine.sqlite(
        path,
        extractor=QwenPersonaExtractor(
            api_key="test-key",
            base_url="https://dashscope.test/v1",
            model="qwen-test",
            transport=httpx.MockTransport(handler),
        ),
    )
    await engine.grant_consent(
        account_id="persona-account",
        policy_version="persona-learning-v1",
    )
    observed = await engine.observe(
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="persona-speech-style-001",
            learning_allowed=True,
            scene="family_conversation",
        )
    )
    traits = await engine.traits(account_id="persona-account")

    assert observed.accepted is True
    assert {trait.category for trait in traits} >= {
        "emphasis_style",
        "emotional_expression",
    }


@pytest.mark.asyncio
async def test_invalid_qwen_persona_output_falls_back_to_conservative_rules() -> None:
    invalid = {
        "traits": [
            {
                "category": "decision_habit",
                "normalized_key": "missing-boundaries",
                "description": "总是很理性",
                "context": "",
                "counterexample": "",
            }
        ]
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return _response(invalid)

    extractor = FallbackPersonaExtractor(
        QwenPersonaExtractor(
            api_key="test-key",
            base_url="https://dashscope.test/v1",
            model="qwen-test",
            transport=httpx.MockTransport(handler),
        ),
        RuleBasedPersonaExtractor(),
    )
    evidence = PersonaEvidence(
        account_id="persona-account",
        source_event_id="persona-qwen-invalid",
        learning_allowed=True,
        scene="major_decision",
    )

    traits = await extractor.extract(
        "做重大决定时，我习惯先列事实，再睡一晚。",
        evidence,
    )

    assert any(trait.category == "decision_habit" for trait in traits)
    assert all(trait.description != "总是很理性" for trait in traits)
