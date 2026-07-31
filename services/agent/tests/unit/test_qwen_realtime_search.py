from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from services.agent.src.providers.qwen_realtime_search import (
    QwenRealtimeSearch,
    QwenRealtimeSearchConfig,
)


def _config(*, timeout_s: float = 0.2) -> QwenRealtimeSearchConfig:
    return QwenRealtimeSearchConfig(
        api_key="qwen-test-key",
        base_url="https://dashscope.test/compatible-mode/v1/",
        model="qwen-plus",
        timeout_s=timeout_s,
    )


def test_default_timeout_allows_the_primary_llm_read_budget() -> None:
    assert (
        QwenRealtimeSearchConfig(
            api_key="qwen-test-key",
            base_url="https://dashscope.test/compatible-mode/v1",
            model="qwen-plus",
        ).timeout_s
        == 12.0
    )


@pytest.mark.asyncio
async def test_resolve_forces_search_with_only_the_current_public_query() -> None:
    observed: dict[str, object] = {}
    query = "今天南京的天气怎么样？"
    private_history = "我的银行卡密码是 never-send-this"

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["headers"] = dict(request.headers)
        observed["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "南京今天多云。"}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        resolver = QwenRealtimeSearch(_config(), client=http_client)
        result = await resolver.resolve(query=query)

    assert result == "南京今天多云。"
    assert observed["path"] == "/compatible-mode/v1/chat/completions"
    headers = observed["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer qwen-test-key"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["model"] == "qwen-plus"
    assert body["stream"] is False
    assert body["max_tokens"] == 240
    assert body["thinking"] == {"type": "disabled"}
    assert body["enable_search"] is True
    assert body["search_options"] == {
        "forced_search": True,
        "search_strategy": "turbo",
    }
    messages = body["messages"]
    assert isinstance(messages, list)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": query}
    assert private_history not in json.dumps(body, ensure_ascii=False)


@pytest.mark.asyncio
async def test_resolve_rejects_blank_or_oversized_query_without_request() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "unused"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        resolver = QwenRealtimeSearch(_config(), client=http_client)
        assert await resolver.resolve(query="  \n") is None
        assert await resolver.resolve(query="x" * 1001) is None

    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": "unavailable"}),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": ["not text"]}}]}),
    ],
    ids=("non-2xx", "not-openai-payload", "no-choice", "blank-content", "non-text-content"),
)
async def test_resolve_fails_closed_for_unusable_provider_responses(
    response: httpx.Response,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: response)
    ) as http_client:
        resolver = QwenRealtimeSearch(_config(), client=http_client)
        assert await resolver.resolve(query="今天南京天气怎么样？") is None


@pytest.mark.asyncio
async def test_resolve_fails_closed_when_request_times_out() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        resolver = QwenRealtimeSearch(_config(timeout_s=0.01), client=http_client)
        assert await resolver.resolve(query="今天南京天气怎么样？") is None
