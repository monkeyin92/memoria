from __future__ import annotations

import json

import httpx
import pytest
from services.agent.src.providers.qwen_realtime_search import (
    QwenRealtimeSearch,
    QwenRealtimeSearchConfig,
)


@pytest.mark.asyncio
async def test_qwen_realtime_search_sends_only_the_public_query_with_forced_search() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "南京今天多云。"}}]},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = QwenRealtimeSearch(
        QwenRealtimeSearchConfig(
            api_key="test-key",
            base_url="https://qwen.example/v1",
            model="qwen-deep",
        ),
        client=client,
    )

    assert await resolver.resolve(query="今天南京天气怎么样") == "南京今天多云。"
    payload = json.loads(requests[0].content)
    assert str(requests[0].url) == "https://qwen.example/v1/chat/completions"
    assert payload["model"] == "qwen-deep"
    assert payload["enable_search"] is True
    assert payload["search_options"] == {"forced_search": True, "search_strategy": "turbo"}
    assert payload["max_tokens"] == 160
    assert "联网结果必须由你先整理后再回答用户" in payload["messages"][0]["content"]
    assert payload["messages"][1] == {"role": "user", "content": "今天南京天气怎么样"}
    await client.aclose()
