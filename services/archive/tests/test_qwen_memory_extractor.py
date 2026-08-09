from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.qwen_memory_extractor import (
    FallbackMemoryExtractor,
    QwenMemoryExtractor,
)


def _event(text: str) -> EvidenceEvent:
    return EvidenceEvent(
        event_id="qwen-memory-001",
        account_id="account-qwen-memory",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        speaker_class="owner",
        source="test",
        payload={"text": text},
    )


@pytest.mark.asyncio
async def test_qwen_extractor_accepts_only_strict_traceable_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-key"
        assert payload["response_format"] == {"type": "json_object"}
        assert "不得补充" in payload["messages"][1]["content"]
        assert "study_progress" in payload["messages"][1]["content"]
        assert "不能从一次行为推断" in payload["messages"][1]["content"]
        extraction = {
            "claims": [
                {
                    "category": "life_story",
                    "subject_key": "mother:李梅",
                    "predicate": "age",
                    "value": "60",
                    "confidence": 0.91,
                    "sensitive_domain": "personal",
                }
            ],
            "people": [
                {
                    "display_name": "李梅",
                    "relationship_to_owner": "mother",
                    "canonical_key": "mother:李梅",
                    "aliases": ["妈妈", "母亲"],
                }
            ],
            "relationships": [
                {"person_key": "mother:李梅", "relationship_type": "mother"}
            ],
            "timeline": [
                {
                    "title": "妈妈李梅今年60岁",
                    "category": "life_story",
                    "event_start": None,
                    "event_end": None,
                    "time_precision": "conversation_time",
                }
            ],
            "knowledge": [],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(extraction)}}]},
        )

    extractor = QwenMemoryExtractor(
        api_key="test-key",
        base_url="https://dashscope.test/v1",
        model="qwen-test",
        transport=httpx.MockTransport(handler),
    )

    result = await extractor.extract(_event("我妈妈叫李梅，今年60岁。"))

    assert result.extractor_version == "qwen-json:qwen-test:v2"
    assert result.claims[0].subject_key == result.people[0].canonical_key
    assert result.people[0].aliases == ("李梅", "妈妈", "母亲")
    assert result.timeline[0].event_start == datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_invalid_qwen_entity_reference_falls_back_to_local_rules() -> None:
    invalid = {
        "claims": [],
        "people": [],
        "relationships": [{"person_key": "mother:不存在", "relationship_type": "mother"}],
        "timeline": [],
        "knowledge": [],
    }

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(invalid)}}]},
        )

    extractor = FallbackMemoryExtractor(
        QwenMemoryExtractor(
            api_key="test-key",
            base_url="https://dashscope.test/v1",
            model="qwen-test",
            transport=httpx.MockTransport(handler),
        ),
        RuleBasedMemoryExtractor(),
    )

    result = await extractor.extract(_event("我们家的家训是说到做到。"))

    assert result.extractor_version == "rules-zh-v2"
    assert result.claims[0].category == "family_principle"
