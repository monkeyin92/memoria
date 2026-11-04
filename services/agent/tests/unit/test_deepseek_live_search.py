from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from services.agent.src.providers.deepseek_live_search import (
    DeepSeekLiveSearch,
    DeepSeekLiveSearchConfig,
)

_RESULTS = {
    "results": [
        {
            "title": "上海天气预报",
            "date": "2026-10-31T00:00:00Z",
            "summary": "今天晴转多云，17到24度。",
            "url": "https://weather.example/sh",
        }
    ]
}
_ANSWER = {"choices": [{"message": {"content": "上海今天晴转多云，十七到二十四度。"}}]}


def _resolver(handler: object) -> tuple[DeepSeekLiveSearch, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    resolver = DeepSeekLiveSearch(
        DeepSeekLiveSearchConfig(
            api_key="test-key",
            base_url="https://deepseek.example",
            model="deepseek-flash",
        ),
        client=client,
        now=lambda: datetime(2026, 10, 31, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    return resolver, client


@pytest.mark.asyncio
async def test_searches_then_answers_from_the_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = _RESULTS if request.url.path == "/v1/tools/search" else _ANSWER
        return httpx.Response(200, json=body)

    resolver, client = _resolver(handler)
    assert await resolver.resolve(query="上海今天天气怎么样") == "上海今天晴转多云，十七到二十四度。"

    assert [request.url.path for request in requests] == ["/v1/tools/search", "/chat/completions"]
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(requests[0].content) == {"query": "上海今天天气怎么样", "count": 5}
    chat = json.loads(requests[1].content)
    assert chat["model"] == "deepseek-flash"
    assert chat["stream"] is False
    assert chat["thinking"] == {"type": "disabled"}
    assert "2026年10月31日09点05分" in chat["messages"][0]["content"]
    assert "（2026-10-31）今天晴转多云，17到24度。" in chat["messages"][1]["content"]
    assert "weather.example" not in chat["messages"][1]["content"]
    await client.aclose()


@pytest.mark.asyncio
async def test_redacts_the_query_before_it_leaves() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body = _RESULTS if request.url.path == "/v1/tools/search" else _ANSWER
        return httpx.Response(200, json=body)

    resolver, client = _resolver(handler)
    await resolver.resolve(query="我的手机13812345678，明天南京天气怎么样")

    assert sent[0]["query"] == "我的手机[手机号]，明天南京天气怎么样"
    assert "13812345678" not in json.dumps(sent, ensure_ascii=False)
    await client.aclose()


@pytest.mark.asyncio
async def test_no_results_returns_none_without_calling_the_model() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json={"results": []})

    resolver, client = _resolver(handler)
    assert await resolver.resolve(query="火星今天下雨吗") is None
    assert paths == ["/v1/tools/search"]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_search_errors_fail_closed(status: int) -> None:
    resolver, client = _resolver(lambda request: httpx.Response(status, json={}))
    assert await resolver.resolve(query="今天有什么新闻") is None
    await client.aclose()


@pytest.mark.asyncio
async def test_a_v1_base_url_still_reaches_the_search_endpoint() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        body = _RESULTS if request.url.path == "/v1/tools/search" else _ANSWER
        return httpx.Response(200, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = DeepSeekLiveSearch(
        DeepSeekLiveSearchConfig(
            api_key="k", base_url="https://deepseek.example/v1/", model="deepseek-flash"
        ),
        client=client,
    )
    await resolver.resolve(query="今天有什么新闻")
    assert urls == [
        "https://deepseek.example/v1/tools/search",
        "https://deepseek.example/v1/chat/completions",
    ]
    await client.aclose()


@pytest.mark.asyncio
async def test_long_query_is_cut_to_the_search_limit() -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body = _RESULTS if request.url.path == "/v1/tools/search" else _ANSWER
        return httpx.Response(200, json=body)

    resolver, client = _resolver(handler)
    await resolver.resolve(query="天气" * 100)
    assert len(str(sent[0]["query"])) == 128
    await client.aclose()


_STALE_SHENZHOU = {
    "results": [
        {
            "title": "神舟飞船动态",
            "date": "2025-05-12T00:00:00Z",
            "summary": "神舟飞船近日完成对接。",
            "url": "https://news.example/shenzhou-may",
        }
    ]
}
_STALE_AS_FRESH_ANSWER = {
    "choices": [{"message": {"content": "最近神舟飞船完成了对接。"}}]
}


@pytest.mark.asyncio
async def test_stale_only_results_do_not_answer_a_recency_query() -> None:
    """Repro for 神舟最近消息: May-dated hits must not become a spoken「最近」answer."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/tools/search":
            return httpx.Response(200, json=_STALE_SHENZHOU)
        return httpx.Response(200, json=_STALE_AS_FRESH_ANSWER)

    resolver, client = _resolver(handler)
    assert await resolver.resolve(query="神舟最近消息") is None
    assert paths == ["/v1/tools/search"]
    await client.aclose()


@pytest.mark.asyncio
async def test_fresh_hits_still_answer_a_recency_query() -> None:
    fresh = {
        "results": [
            {
                "title": "神舟飞船动态",
                "date": "2026-10-28T00:00:00Z",
                "summary": "神舟飞船今天完成对接。",
                "url": "https://news.example/shenzhou-now",
            }
        ]
    }
    answer = {"choices": [{"message": {"content": "神舟飞船今天完成了对接。"}}]}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200, json=fresh if request.url.path == "/v1/tools/search" else answer
        )

    resolver, client = _resolver(handler)
    assert await resolver.resolve(query="神舟最近消息") == "神舟飞船今天完成了对接。"
    assert paths == ["/v1/tools/search", "/chat/completions"]
    await client.aclose()


@pytest.mark.asyncio
async def test_non_recency_queries_still_label_old_dates_for_the_model() -> None:
    paths: list[str] = []
    chat_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/chat/completions":
            chat_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": "那是二零二五年五月十二日的消息，神舟飞船完成了对接。"
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json=_STALE_SHENZHOU)

    resolver, client = _resolver(handler)
    spoken = await resolver.resolve(query="神舟飞船什么时候对接的")
    assert spoken is not None
    assert "五月" in spoken or "2025" in spoken or "二零二五" in spoken
    system = str(chat_bodies[0]["messages"][0]["content"])  # type: ignore[index]
    user = str(chat_bodies[0]["messages"][1]["content"])  # type: ignore[index]
    assert "不要说成最近" in system
    assert "（2025-05-12）" in user
    await client.aclose()
