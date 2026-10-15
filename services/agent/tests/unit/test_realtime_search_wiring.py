from __future__ import annotations

from typing import Any

import httpx
import pytest
from services.agent.src.config import AgentSettings
from services.agent.src.providers.handlers import build_realtime_search_resolver

_QWEN_ANSWER = {"choices": [{"message": {"content": "南京明天多云。"}}]}


@pytest.fixture(autouse=True)
def clean_weather_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WEATHER_LOOKUP_PROVIDER", "QWEATHER_API_KEY", "QWEATHER_API_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sent_hosts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    hosts: list[str] = []

    async def send(
        self: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any
    ) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json=_QWEN_ANSWER, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return hosts


async def test_default_settings_send_weather_questions_only_to_dashscope(
    sent_hosts: list[str],
) -> None:
    resolver = build_realtime_search_resolver(
        settings=AgentSettings(_env_file=None, DASHSCOPE_API_KEY="k")
    )

    reply = await resolver.resolve(query="南京明天天气怎么样")
    await resolver.aclose()

    assert reply == "南京明天多云。"
    assert sent_hosts == ["dashscope.aliyuncs.com"]
