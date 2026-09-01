from __future__ import annotations

import asyncio

import httpx
import pytest
from services.agent.src.conversation_close_router import (
    conversation_close_needed,
    resolve_conversation_close_needed,
    rule_conversation_close_only,
)
from services.agent.src.providers.close_intent_semantic_classifier import (
    CloseIntentSemanticClassifier,
    CloseIntentSemanticClassifierConfig,
    CloseIntentSemanticVerdict,
    parse_close_intent_semantic_verdict,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("END_SESSION", CloseIntentSemanticVerdict.END_SESSION),
        (" CONTINUE\n", CloseIntentSemanticVerdict.CONTINUE),
        ("UNSURE", CloseIntentSemanticVerdict.UNSURE),
        ('{"verdict":"END_SESSION"}', CloseIntentSemanticVerdict.UNSURE),
        ("END_SESSION because...", CloseIntentSemanticVerdict.UNSURE),
    ],
)
def test_close_intent_semantic_output_is_one_strict_enum(
    raw: str,
    expected: CloseIntentSemanticVerdict,
) -> None:
    assert parse_close_intent_semantic_verdict(raw) is expected


def test_rule_conversation_close_only_matches_explicit_phrases() -> None:
    assert rule_conversation_close_only("退下吧")
    assert rule_conversation_close_only("知道了，再见")
    assert not rule_conversation_close_only("再见是什么意思")


@pytest.mark.asyncio
async def test_resolve_conversation_close_needed_uses_semantic_fallback() -> None:
    cache: dict[str, bool] = {}
    calls: list[str] = []

    async def resolver(text: str) -> bool:
        calls.append(text)
        return True

    phrase = "那先不聊了"
    assert not rule_conversation_close_only(phrase)
    assert await resolve_conversation_close_needed(
        phrase,
        cache=cache,
        semantic_resolver=resolver,
    )
    assert calls == [phrase]
    assert conversation_close_needed(phrase, cache=cache)


@pytest.mark.asyncio
async def test_resolve_conversation_close_needed_skips_classifier_on_rule_hit() -> None:
    cache: dict[str, bool] = {}

    async def resolver(_: str) -> bool:
        raise AssertionError("rule hit should not call semantic resolver")

    assert await resolve_conversation_close_needed(
        "行，拜拜",
        cache=cache,
        semantic_resolver=resolver,
    )


@pytest.mark.asyncio
async def test_classifier_timeout_fails_closed_to_unsure() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "END_SESSION"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = CloseIntentSemanticClassifier(
        CloseIntentSemanticClassifierConfig(
            api_key="test-secret",
            base_url="https://provider.test/v1",
            timeout_s=0.01,
        ),
        client=client,
    )
    assert (
        await classifier.classify(current_text="模糊语句")
        is CloseIntentSemanticVerdict.UNSURE
    )
    await client.aclose()
