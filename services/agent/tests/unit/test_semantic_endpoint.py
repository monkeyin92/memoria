from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.config import AgentSettings
from services.agent.src.conversation_close_wiring import build_close_intent_semantic_classifier
from services.agent.src.live_lookup_wiring import build_live_lookup_semantic_classifier
from services.agent.src.providers.close_intent_semantic_classifier import (
    CloseIntentSemanticClassifierConfig,
)
from services.agent.src.providers.live_lookup_semantic_classifier import (
    LiveLookupSemanticClassifier,
    LiveLookupSemanticClassifierConfig,
    LiveLookupSemanticVerdict,
)
from services.common.llm_thinking import ThinkingMode


def _provider_env(monkeypatch: pytest.MonkeyPatch, provider: str, *, deepseek_key: str) -> None:
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-test-key")
    monkeypatch.setenv("DASHSCOPE_COMPATIBLE_BASE_URL", "https://dashscope.example.invalid/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", deepseek_key)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example.invalid")
    monkeypatch.setenv("DEEPSEEK_FAST_MODEL", "deepseek-flash")
    monkeypatch.setenv("LIVE_LOOKUP_SEMANTIC_MODEL", "qwen-live")
    monkeypatch.setenv("CONVERSATION_CLOSE_SEMANTIC_MODEL", "qwen-close")


def _endpoint(
    config: LiveLookupSemanticClassifierConfig | CloseIntentSemanticClassifierConfig,
) -> tuple[str, str, str, ThinkingMode]:
    return (config.api_key, config.base_url, config.model, config.thinking_mode)


def test_official_deepseek_moves_every_turn_classifier_to_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider_env(monkeypatch, "deepseek", deepseek_key="deepseek-test-key")
    settings = AgentSettings()

    live = build_live_lookup_semantic_classifier(settings)
    close = build_close_intent_semantic_classifier(settings)

    assert live is not None and close is not None
    expected = ("deepseek-test-key", "https://deepseek.example.invalid", "deepseek-flash", "deepseek")
    assert _endpoint(live._config) == _endpoint(close._config) == expected


@pytest.mark.parametrize("provider", ["qwen", "bailian_deepseek"])
def test_dashscope_providers_keep_each_classifier_on_its_own_qwen_model(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    _provider_env(monkeypatch, provider, deepseek_key="deepseek-test-key")
    settings = AgentSettings()

    live = build_live_lookup_semantic_classifier(settings)
    close = build_close_intent_semantic_classifier(settings)

    assert live is not None and close is not None
    base = ("dashscope-test-key", "https://dashscope.example.invalid/v1")
    assert _endpoint(live._config) == (*base, "qwen-live", "dashscope")
    assert _endpoint(close._config) == (*base, "qwen-close", "dashscope")


def test_official_deepseek_without_its_key_has_no_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The DashScope key is set, but a DeepSeek model name must never be sent to DashScope.
    _provider_env(monkeypatch, "deepseek", deepseek_key="")
    settings = AgentSettings()

    assert build_live_lookup_semantic_classifier(settings) is None
    assert build_close_intent_semantic_classifier(settings) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("thinking_mode", "sent", "absent"),
    [
        ("deepseek", {"thinking": {"type": "disabled"}}, "enable_thinking"),
        ("dashscope", {"enable_thinking": False}, "thinking"),
    ],
)
async def test_classifier_sends_the_providers_own_thinking_switch(
    thinking_mode: ThinkingMode,
    sent: dict[str, object],
    absent: str,
) -> None:
    # DeepSeek ignores enable_thinking and would spend the 16 tokens on reasoning.
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "NEEDS_LIVE_LOOKUP"}}]}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = LiveLookupSemanticClassifier(
        LiveLookupSemanticClassifierConfig(
            api_key="test-key",
            base_url="https://llm.example.invalid",
            model="test-model",
            thinking_mode=thinking_mode,
        ),
        client=client,
    )

    verdict = await classifier.classify(current_text="明天会下雨吗？")
    await client.aclose()

    assert verdict is LiveLookupSemanticVerdict.NEEDS_LIVE_LOOKUP
    assert {key: captured[key] for key in sent} == sent
    assert absent not in captured
