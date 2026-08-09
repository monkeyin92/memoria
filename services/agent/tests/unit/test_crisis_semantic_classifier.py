from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from services.agent.src.providers.crisis_semantic_classifier import (
    CrisisSemanticClassifier,
    CrisisSemanticClassifierConfig,
    CrisisSemanticVerdict,
    parse_crisis_semantic_verdict,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SELF_CRISIS", CrisisSemanticVerdict.SELF_CRISIS),
        (" SUPPORT_FOR_OTHER\n", CrisisSemanticVerdict.SUPPORT_FOR_OTHER),
        ("NO_CRISIS", CrisisSemanticVerdict.NO_CRISIS),
        ('{"verdict":"SELF_CRISIS"}', CrisisSemanticVerdict.UNSURE),
        ("SELF_CRISIS because...", CrisisSemanticVerdict.UNSURE),
    ],
)
def test_crisis_semantic_output_is_one_strict_enum(
    raw: str,
    expected: CrisisSemanticVerdict,
) -> None:
    assert parse_crisis_semantic_verdict(raw) is expected


@pytest.mark.asyncio
async def test_classifier_sends_only_current_bounded_text() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "UNSURE"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = CrisisSemanticClassifier(
        CrisisSemanticClassifierConfig(
            api_key="test-secret",
            base_url="https://provider.test/v1",
        ),
        client=client,
    )
    verdict = await classifier.classify(current_text="旧内容" * 400 + "当前一句")
    await client.aclose()

    assert verdict is CrisisSemanticVerdict.UNSURE
    serialized = json.dumps(captured["body"], ensure_ascii=False)
    assert "当前一句" in serialized
    assert "旧内容" * 200 not in serialized
    assert "history" not in serialized.casefold()
    assert "memory" not in serialized.casefold()


@pytest.mark.asyncio
async def test_classifier_timeout_fails_closed_to_unsure() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"choices": [{"message": {"content": "SELF_CRISIS"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = CrisisSemanticClassifier(
        CrisisSemanticClassifierConfig(
            api_key="test-secret",
            base_url="https://provider.test/v1",
            timeout_s=0.01,
        ),
        client=client,
    )
    assert (
        await classifier.classify(current_text="模糊语句")
        is CrisisSemanticVerdict.UNSURE
    )
    await client.aclose()
