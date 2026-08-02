"""Strict, bounded semantic review for ambiguous interruption finals."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from services.agent.src.orchestration.utterance_router import InterruptSemanticVerdict
from services.agent.src.providers.interrupt_semantic_classifier import (
    InterruptSemanticClassifier,
    InterruptSemanticClassifierConfig,
    parse_interrupt_semantic_verdict,
)


def test_classifier_config_rejects_missing_provider_identity() -> None:
    with pytest.raises(ValueError, match="API key"):
        InterruptSemanticClassifierConfig(
            api_key="",
            base_url="https://dashscope.example/compatible-mode/v1",
        )
    with pytest.raises(ValueError, match="base URL"):
        InterruptSemanticClassifierConfig(
            api_key="secret-test-key",
            base_url="",
        )
    with pytest.raises(ValueError, match="model"):
        InterruptSemanticClassifierConfig(
            api_key="secret-test-key",
            base_url="https://dashscope.example/compatible-mode/v1",
            model="",
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CONTROL_ONLY", InterruptSemanticVerdict.CONTROL_ONLY),
        (" HAS_USER_CONTENT \n", InterruptSemanticVerdict.HAS_USER_CONTENT),
        ("UNSURE", InterruptSemanticVerdict.UNSURE),
        ("CONTROL_ONLY because echo", InterruptSemanticVerdict.UNSURE),
        ('{"verdict":"CONTROL_ONLY"}', InterruptSemanticVerdict.UNSURE),
        ("", InterruptSemanticVerdict.UNSURE),
    ],
)
def test_parse_interrupt_semantic_verdict_is_strict(
    raw: str,
    expected: InterruptSemanticVerdict,
) -> None:
    assert parse_interrupt_semantic_verdict(raw) is expected


@pytest.mark.asyncio
async def test_classifier_sends_only_bounded_turn_evidence() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "CONTROL_ONLY"}}]},
        )

    client = httpx.AsyncClient(
        base_url="https://dashscope.example/compatible-mode/v1",
        transport=httpx.MockTransport(handler),
    )
    classifier = InterruptSemanticClassifier(
        InterruptSemanticClassifierConfig(
            api_key="secret-test-key",
            base_url="https://dashscope.example/compatible-mode/v1",
            model="deepseek-v4-flash",
            timeout_s=0.6,
        ),
        client=client,
    )

    verdict = await classifier.classify(
        final_text="份停听一下能是据提供的数据和指示来协助。",
        sticky_text="停一下",
        assistant_text="根据提供的数据和指示来协助。",
    )
    await client.aclose()

    assert verdict is InterruptSemanticVerdict.CONTROL_ONLY
    assert captured["authorization"] == "Bearer secret-test-key"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "deepseek-v4-flash"
    assert body["temperature"] == 0
    assert body["enable_thinking"] is False
    serialized = json.dumps(body, ensure_ascii=False)
    assert "份停听一下能是据提供的数据和指示来协助。" in serialized
    assert "停一下" in serialized
    assert "根据提供的数据和指示来协助。" in serialized
    assert "history" not in serialized.casefold()
    assert "memory" not in serialized.casefold()
    assert "tool" not in serialized.casefold()


@pytest.mark.asyncio
async def test_classifier_timeout_fails_closed_to_unsure() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "HAS_USER_CONTENT"}}]},
        )

    client = httpx.AsyncClient(
        base_url="https://dashscope.example/compatible-mode/v1",
        transport=httpx.MockTransport(handler),
    )
    classifier = InterruptSemanticClassifier(
        InterruptSemanticClassifierConfig(
            api_key="secret-test-key",
            base_url="https://dashscope.example/compatible-mode/v1",
            timeout_s=0.01,
        ),
        client=client,
    )

    verdict = await classifier.classify(
        final_text="停听一下",
        sticky_text="停一下",
        assistant_text="这是助手尾音",
    )
    await client.aclose()

    assert verdict is InterruptSemanticVerdict.UNSURE


@pytest.mark.asyncio
async def test_classifier_invalid_provider_payload_is_unsure() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "maybe"}}]})

    client = httpx.AsyncClient(
        base_url="https://dashscope.example/compatible-mode/v1",
        transport=httpx.MockTransport(handler),
    )
    classifier = InterruptSemanticClassifier(
        InterruptSemanticClassifierConfig(
            api_key="secret-test-key",
            base_url="https://dashscope.example/compatible-mode/v1",
        ),
        client=client,
    )

    verdict = await classifier.classify(
        final_text="停听一下",
        sticky_text="停一下",
        assistant_text="这是助手尾音",
    )
    await client.aclose()

    assert verdict is InterruptSemanticVerdict.UNSURE
