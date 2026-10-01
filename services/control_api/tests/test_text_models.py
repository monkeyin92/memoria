from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import pytest
from services.agent.src.providers.crisis_semantic_classifier import CrisisSemanticVerdict
from services.archive.domain import EvidenceEvent
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.control_api.app.config import ControlSettings
from services.control_api.app.memory_components import build_memory_extractor
from services.control_api.app.text_models import (
    build_crisis_semantic_classifier,
    build_persona_extractor,
    build_persona_structurer,
)
from services.persona.custom_persona_structurer import PersonaStructuringError
from services.persona.domain import PersonaEvidence
from services.persona.rules import RuleBasedPersonaExtractor

Provider = Literal["qwen", "deepseek"]

_REAL_ASYNC_CLIENT = httpx.AsyncClient

# Calls in order: crisis evidence, memory extraction, persona extraction, persona structuring.
# Only the extractors send the DashScope workspace header; DeepSeek never gets it.
_EXPECTED: dict[Provider, dict[str, Any]] = {
    "qwen": {
        "url": "https://dashscope.example.invalid/v1/chat/completions",
        "key": "dashscope-test-key",
        "models": ["qwen-crisis", "qwen-extract", "qwen-extract", "qwen-structure"],
        "workspaces": [None, "ws-test", "ws-test", "ws-test"],
        "switch": {"enable_thinking": False},
        "absent": "thinking",
    },
    "deepseek": {
        "url": "https://deepseek.example.invalid/chat/completions",
        "key": "deepseek-test-key",
        "models": ["deepseek-flash"] * 4,
        "workspaces": [None] * 4,
        "switch": {"thinking": {"type": "disabled"}},
        "absent": "enable_thinking",
    },
}


def _settings(provider: Provider, *, deepseek_key: str = "deepseek-test-key") -> ControlSettings:
    return ControlSettings(
        _env_file=None,
        OFFLINE_MOCK=False,
        LLM_PROVIDER=provider,
        DASHSCOPE_API_KEY="dashscope-test-key",
        DASHSCOPE_BASE_URL="https://dashscope.example.invalid/v1",
        DASHSCOPE_WORKSPACE_ID="ws-test",
        DEEPSEEK_API_KEY=deepseek_key,
        DEEPSEEK_BASE_URL="https://deepseek.example.invalid",
        DEEPSEEK_FAST_MODEL="deepseek-flash",
        CRISIS_SEMANTIC_ENABLED=True,
        CRISIS_SEMANTIC_MODEL="qwen-crisis",
        MEMORIA_MEMORY_EXTRACTION_MODEL="qwen-extract",
        MEMORIA_PERSONA_STRUCTURING_MODEL="qwen-structure",
    )


def _record_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Every client the builders create answers 503, so each call falls back after one try."""

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(503)

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


def _event() -> EvidenceEvent:
    return EvidenceEvent(
        event_id="text-model-001",
        account_id="account-text-model",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        speaker_class="owner",
        source="test",
        payload={"text": "我女儿下个月要去上海读大学。"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["qwen", "deepseek"])
async def test_every_control_text_call_follows_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: Provider,
) -> None:
    seen = _record_requests(monkeypatch)
    settings = _settings(provider)
    crisis = build_crisis_semantic_classifier(settings)
    structurer = build_persona_structurer(settings)
    assert crisis is not None and structurer is not None

    assert await crisis.classify(current_text="我不想活了") is CrisisSemanticVerdict.UNSURE
    await crisis.aclose()
    await build_memory_extractor(settings).extract(_event())
    await build_persona_extractor(settings).extract(
        "我做重大决定前总要先核对事实。",
        PersonaEvidence(
            account_id="persona-account",
            source_event_id="persona-text-model-001",
            learning_allowed=True,
        ),
    )
    with pytest.raises(PersonaStructuringError):
        await structurer.structure("温柔、耐心，说话慢一点的奶奶")

    expected = _EXPECTED[provider]
    bodies = [json.loads(request.content) for request in seen]
    assert [str(request.url) for request in seen] == [expected["url"]] * 4
    assert {request.headers["Authorization"] for request in seen} == {f"Bearer {expected['key']}"}
    assert [request.headers.get("X-DashScope-WorkSpace") for request in seen] == expected[
        "workspaces"
    ]
    assert [body["model"] for body in bodies] == expected["models"]
    for body in bodies:
        assert {key: body.get(key) for key in expected["switch"]} == expected["switch"]
        assert expected["absent"] not in body


def test_official_deepseek_without_its_key_never_falls_back_to_dashscope() -> None:
    settings = _settings("deepseek", deepseek_key="")

    assert build_crisis_semantic_classifier(settings) is None
    assert build_persona_structurer(settings) is None
    assert isinstance(build_persona_extractor(settings), RuleBasedPersonaExtractor)
    assert isinstance(build_memory_extractor(settings), RuleBasedMemoryExtractor)
