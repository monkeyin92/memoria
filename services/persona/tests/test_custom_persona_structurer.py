"""Strict-boundary tests for the custom-persona structurer (T-B acceptance ②⑥)."""

from __future__ import annotations

import json

import httpx
import pytest
from services.persona.custom_persona_fields import StructuredPersona
from services.persona.custom_persona_structurer import (
    PersonaStructuringError,
    QwenCustomPersonaStructurer,
)


def _response(content: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(content)}}]},
    )


def _structured() -> dict[str, object]:
    return {
        "style_description": "温和的陪伴者",
        "warmth": "warm",
        "directness": "gentle",
        "response_length": "brief",
        "question_frequency": "rare",
        "interview_depth": "light",
        "welcome_text": "嗨，我在。",
        "conversation_instruction": "说话短一点，像朋友。",
        "voice_instruction": "轻松自然",
        "default_voice_emotion": "neutral",
        "default_voice_rate": 1.0,
    }


def _structurer(content: dict[str, object]) -> QwenCustomPersonaStructurer:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-key"
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["temperature"] == 0
        return _response(content)

    return QwenCustomPersonaStructurer(
        api_key="test-key",
        base_url="https://dashscope.test/v1",
        model="qwen-flash",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_structure_returns_the_controlled_fields_only() -> None:
    structurer = _structurer(_structured())
    structured = await structurer.structure("想要一个说话短一点、像朋友一样的伙伴")

    assert isinstance(structured, StructuredPersona)
    assert structured.warmth == "warm"
    assert structured.conversation_instruction == "说话短一点，像朋友。"
    assert structurer.version == "qwen-persona-structuring:qwen-flash:v1"


@pytest.mark.asyncio
async def test_out_of_domain_enum_is_rejected_never_clamped() -> None:
    structurer = _structurer({**_structured(), "warmth": "甜"})
    with pytest.raises(PersonaStructuringError) as excinfo:
        await structurer.structure("温柔到发甜")

    assert excinfo.value.field == "warmth"


@pytest.mark.asyncio
async def test_free_text_slot_is_forbidden() -> None:
    """⑥: no unsanctioned free-text key may survive into the structured body."""

    structurer = _structurer({**_structured(), "free_text": "用户原话"})
    with pytest.raises(PersonaStructuringError) as excinfo:
        await structurer.structure("用户原话")

    assert excinfo.value.field == "free_text"


@pytest.mark.asyncio
async def test_out_of_range_rate_is_rejected() -> None:
    structurer = _structurer({**_structured(), "default_voice_rate": 1.5})
    with pytest.raises(PersonaStructuringError) as excinfo:
        await structurer.structure("语速快一点")

    assert excinfo.value.field == "default_voice_rate"


@pytest.mark.asyncio
async def test_blank_free_text_is_rejected_before_any_request() -> None:
    structurer = _structurer(_structured())
    with pytest.raises(PersonaStructuringError):
        await structurer.structure("   ")
