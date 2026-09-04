from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from services.agent.src.live_lookup_router import (
    keyword_requires_live_media_lookup,
    live_lookup_needed,
    resolve_live_lookup_needed,
)
from services.agent.src.providers.live_lookup_semantic_classifier import (
    LiveLookupSemanticClassifier,
    LiveLookupSemanticClassifierConfig,
    LiveLookupSemanticVerdict,
    parse_live_lookup_semantic_verdict,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NEEDS_LIVE_LOOKUP", LiveLookupSemanticVerdict.NEEDS_LIVE_LOOKUP),
        (" NO_LIVE_LOOKUP\n", LiveLookupSemanticVerdict.NO_LIVE_LOOKUP),
        ("UNSURE", LiveLookupSemanticVerdict.UNSURE),
        ('{"verdict":"NEEDS_LIVE_LOOKUP"}', LiveLookupSemanticVerdict.UNSURE),
        ("NEEDS_LIVE_LOOKUP because...", LiveLookupSemanticVerdict.UNSURE),
    ],
)
def test_live_lookup_semantic_output_is_one_strict_enum(
    raw: str,
    expected: LiveLookupSemanticVerdict,
) -> None:
    assert parse_live_lookup_semantic_verdict(raw) is expected


def test_keyword_requires_live_media_lookup_matches_train_and_weather() -> None:
    assert keyword_requires_live_media_lookup("查询明天从南京到上海最快的动车")
    assert keyword_requires_live_media_lookup("今天南京天气怎么样")
    assert not keyword_requires_live_media_lookup("讲个笑话")
    assert not keyword_requires_live_media_lookup("今天星期几")
    assert not keyword_requires_live_media_lookup("现在几点了")


@pytest.mark.asyncio
async def test_weekday_clock_fact_does_not_start_live_lookup() -> None:
    cache: dict[str, bool] = {"今天星期几": True}

    async def resolver(_: str) -> bool:
        raise AssertionError("clock-fact must not call the semantic lookup classifier")

    assert not await resolve_live_lookup_needed(
        "今天星期几",
        cache=cache,
        semantic_resolver=resolver,
    )
    assert not live_lookup_needed("今天星期几", cache=cache)
    assert cache["今天星期几"] is False
    assert not await resolve_live_lookup_needed(
        "现在几点了",
        cache={},
        semantic_resolver=resolver,
    )


@pytest.mark.asyncio
async def test_resolve_live_lookup_needed_uses_semantic_fallback() -> None:
    cache: dict[str, bool] = {}
    calls: list[str] = []

    async def resolver(query: str) -> bool:
        calls.append(query)
        return True

    assert not keyword_requires_live_media_lookup("明天从南京去上海，哪种交通方式最快")
    assert await resolve_live_lookup_needed(
        "明天从南京去上海，哪种交通方式最快",
        cache=cache,
        semantic_resolver=resolver,
    )
    assert calls == ["明天从南京去上海，哪种交通方式最快"]
    assert cache
    assert live_lookup_needed("明天从南京去上海，哪种交通方式最快", cache=cache)


@pytest.mark.asyncio
async def test_resolve_live_lookup_needed_skips_classifier_on_keyword_hit() -> None:
    cache: dict[str, bool] = {}

    async def resolver(_: str) -> bool:
        raise AssertionError("keyword hit should not call semantic resolver")

    assert await resolve_live_lookup_needed(
        "今天南京天气怎么样",
        cache=cache,
        semantic_resolver=resolver,
    )


@pytest.mark.asyncio
async def test_classifier_timeout_fails_closed_to_unsure() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "NEEDS_LIVE_LOOKUP"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = LiveLookupSemanticClassifier(
        LiveLookupSemanticClassifierConfig(
            api_key="test-secret",
            base_url="https://provider.test/v1",
            timeout_s=0.01,
        ),
        client=client,
    )
    assert (
        await classifier.classify(current_text="模糊语句")
        is LiveLookupSemanticVerdict.UNSURE
    )
    await client.aclose()


@pytest.mark.asyncio
async def test_classifier_sends_only_current_bounded_text() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "UNSURE"}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    classifier = LiveLookupSemanticClassifier(
        LiveLookupSemanticClassifierConfig(
            api_key="test-secret",
            base_url="https://provider.test/v1",
        ),
        client=client,
    )
    verdict = await classifier.classify(current_text="旧内容" * 400 + "当前一句")
    await client.aclose()

    assert verdict is LiveLookupSemanticVerdict.UNSURE
    serialized = json.dumps(captured["body"], ensure_ascii=False)
    assert "当前一句" in serialized
    assert "旧内容" * 200 not in serialized
