from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from services.agent.src.config import AgentSettings
from services.agent.src.providers.domestic_weather import (
    DomesticWeather,
    build_domestic_weather,
)
from services.agent.src.providers.handlers import build_realtime_search_resolver
from services.agent.src.providers.weather_places import City

_QWEN_ANSWER = {"choices": [{"message": {"content": "南京明天多云。"}}]}


class _Sent:
    def __init__(self) -> None:
        self.hosts: list[str] = []
        self.qwen_queries: list[str] = []


@pytest.fixture(autouse=True)
def clean_weather_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("WEATHER_LOOKUP_PROVIDER", "QWEATHER_API_KEY", "QWEATHER_API_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> _Sent:
    recorded = _Sent()

    async def send(
        self: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any
    ) -> httpx.Response:
        recorded.hosts.append(request.url.host)
        if request.url.host.endswith("qweatherapi.com"):
            return httpx.Response(200, json=_qweather_body(request), request=request)
        payload = json.loads(request.content)
        recorded.qwen_queries.append(str(payload["messages"][1]["content"]))
        return httpx.Response(200, json=_QWEN_ANSWER, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return recorded


def _domestic(**overrides: str) -> AgentSettings:
    values: dict[str, str] = {
        "DASHSCOPE_API_KEY": "k",
        "LIVE_SEARCH_QWEN_ENABLED": "true",
        "WEATHER_LOOKUP_PROVIDER": "domestic",
        "QWEATHER_API_KEY": "test-key",
        "QWEATHER_API_HOST": "abcd.re.qweatherapi.com",
    }
    values.update(overrides)
    return AgentSettings(_env_file=None, **values)


def _qweather_body(request: httpx.Request) -> dict[str, object]:
    if request.url.path == "/geo/v2/city/lookup":
        name = str(request.url.params["location"])
        return {
            "code": "200",
            "location": [
                {
                    "name": name,
                    "id": "101190101",
                    "adm1": City[name].province,
                    "country": "中国",
                }
            ],
        }
    if request.url.path == "/v7/weather/now":
        return {"code": "200", "now": {"text": "晴", "temp": "22"}}
    return {
        "code": "200",
        "daily": [{"textDay": "多云", "tempMin": "15", "tempMax": "24"} for _ in range(7)],
    }


async def test_default_settings_do_not_call_qwen_realtime_search(sent: _Sent) -> None:
    resolver = build_realtime_search_resolver(
        settings=AgentSettings(_env_file=None, DASHSCOPE_API_KEY="k")
    )

    reply = await resolver.resolve(query="南京明天天气怎么样")
    await resolver.aclose()

    assert resolver.weather is None
    assert resolver.qwen is None
    assert resolver.model == ""
    assert reply is None
    assert sent.hosts == []
    assert sent.qwen_queries == []


async def test_live_search_flag_sends_weather_questions_only_to_dashscope(sent: _Sent) -> None:
    resolver = build_realtime_search_resolver(
        settings=AgentSettings(
            _env_file=None,
            DASHSCOPE_API_KEY="k",
            LIVE_SEARCH_QWEN_ENABLED=True,
        )
    )

    reply = await resolver.resolve(query="南京明天天气怎么样")
    await resolver.aclose()

    assert resolver.weather is None
    assert resolver.model == "qwen-plus"
    assert reply == "南京明天多云。"
    assert sent.hosts == ["dashscope.aliyuncs.com"]
    assert sent.qwen_queries == ["南京明天天气怎么样"]


async def test_clarify_does_not_call_qwen(sent: _Sent) -> None:
    resolver = build_realtime_search_resolver(settings=_domestic())

    reply = await resolver.resolve(query="东京天气怎么样")
    await resolver.aclose()

    assert reply == "你想查哪个城市呀？"
    assert sent.hosts == []
    assert sent.qwen_queries == []


async def test_weather_answer_does_not_call_qwen(sent: _Sent) -> None:
    resolver = build_realtime_search_resolver(settings=_domestic())

    reply = await resolver.resolve(query="南京明天天气怎么样")
    await resolver.aclose()

    assert isinstance(resolver.weather, DomesticWeather)
    assert resolver.model == "qweather+qwen-plus"
    assert reply == "南京明天多云，15到24度。"
    assert sent.hosts == ["abcd.re.qweatherapi.com", "abcd.re.qweatherapi.com"]
    assert sent.qwen_queries == []


async def test_unsupported_window_reaches_qwen_with_the_original_query(sent: _Sent) -> None:
    resolver = build_realtime_search_resolver(settings=_domestic())

    reply = await resolver.resolve(query="南京未来十天天气")
    await resolver.aclose()

    assert reply == "南京明天多云。"
    assert sent.hosts == ["dashscope.aliyuncs.com"]
    assert sent.qwen_queries == ["南京未来十天天气"]


async def test_domestic_without_a_host_builds_no_weather_client() -> None:
    resolver = build_realtime_search_resolver(
        settings=_domestic(QWEATHER_API_HOST="", QWEATHER_API_KEY="k")
    )

    assert resolver.weather is None
    await resolver.aclose()


def test_build_domestic_weather_defaults_missing_settings_to_off() -> None:
    assert build_domestic_weather(SimpleNamespace()) is None
