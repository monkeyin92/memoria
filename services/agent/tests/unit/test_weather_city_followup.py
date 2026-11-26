from __future__ import annotations

import httpx
import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.domestic_weather import DomesticWeather, QWeatherConfig
from services.agent.src.reply_pipeline import ReplyPipeline
from services.common.realtime_information import (
    ASK_WEATHER_CITY_REPLY,
    WEATHER_LOOKUP_UNAVAILABLE_REPLY,
)


class _Search:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def resolve(self, *, query: str) -> str:
        self.queries.append(query)
        return f"查到了：{query}"


class _NoSources:
    weather = None
    web = None

    async def resolve(self, *, query: str) -> str:
        raise AssertionError(f"no source may be called for {query}")


async def _never_live(_text: str) -> bool:
    return False


def _session(name: str, resolver: object) -> tuple[DuplexRuntime, ReplyPipeline]:
    runtime = DuplexRuntime.create(session_id=name)
    # Production's semantic classifier sees only the current text and says no
    # to 「那明天呢」; the weather thread must not depend on it.
    runtime.set_live_lookup_semantic_resolver(_never_live)
    pipeline = ReplyPipeline(instructions="test", runtime=runtime, realtime_search_resolver=resolver)
    return runtime, pipeline


async def _say(runtime: DuplexRuntime, pipeline: ReplyPipeline, text: str) -> str | None:
    fence = await runtime.on_turn_committed(text)
    # Production's prepare_turn marks the turn's context frozen before the delegated answer is used.
    pipeline._mark_context_ready(fence)
    return await pipeline.resolve_media_delegation(text, fence)


def _names(query: str, *words: str) -> bool:
    return all(word in query for word in words)


@pytest.mark.asyncio
async def test_cityless_weather_asks_for_the_city_then_follow_ups_inherit_it() -> None:
    search = _Search()
    runtime, pipeline = _session("weather-city-followup", search)

    assert await _say(runtime, pipeline, "今天天气怎么样？") == ASK_WEATHER_CITY_REPLY
    assert search.queries == []

    assert await runtime.resolve_lookup_owns_reply("杭州"), "the city answer routes as a live lookup"
    answer = await _say(runtime, pipeline, "杭州")
    assert len(search.queries) == 1
    assert _names(search.queries[0], "杭州", "今天", "天气")
    assert answer == f"查到了：{search.queries[0]}"

    assert await runtime.resolve_lookup_owns_reply("那明天呢？")
    answer = await _say(runtime, pipeline, "那明天呢？")
    assert len(search.queries) == 2
    assert _names(search.queries[1], "杭州", "明天", "天气")
    assert answer == f"查到了：{search.queries[1]}"

    await _say(runtime, pipeline, "那上海呢")
    assert len(search.queries) == 3
    assert _names(search.queries[2], "上海", "明天", "天气")
    await runtime.close()


@pytest.mark.asyncio
async def test_a_named_city_is_searched_and_remembered() -> None:
    search = _Search()
    runtime, pipeline = _session("weather-named-city", search)

    await _say(runtime, pipeline, "上海今天天气怎么样")
    assert len(search.queries) == 1
    assert _names(search.queries[0], "上海")

    await _say(runtime, pipeline, "后天呢")
    assert _names(search.queries[1], "上海", "后天", "天气")

    await _say(runtime, pipeline, "我们来讲个故事吧")
    assert await _say(runtime, pipeline, "今天天气怎么样") != ASK_WEATHER_CITY_REPLY
    assert len(search.queries) == 3
    assert _names(search.queries[2], "上海", "今天", "天气")
    await runtime.close()


@pytest.mark.asyncio
async def test_day_follow_up_before_a_city_is_inherited_when_the_city_arrives() -> None:
    search = _Search()
    runtime, pipeline = _session("weather-day-then-city", search)

    assert await _say(runtime, pipeline, "今天天气怎么样？") == ASK_WEATHER_CITY_REPLY
    assert await _say(runtime, pipeline, "那明天呢？") == ASK_WEATHER_CITY_REPLY
    assert search.queries == []

    answer = await _say(runtime, pipeline, "杭州")
    assert len(search.queries) == 1
    assert _names(search.queries[0], "杭州", "明天", "天气")
    assert answer == f"查到了：{search.queries[0]}"
    await runtime.close()


@pytest.mark.asyncio
async def test_a_non_weather_question_is_unaffected() -> None:
    search = _Search()
    runtime, pipeline = _session("weather-non-weather", search)

    await _say(runtime, pipeline, "杭州明天天气怎么样")
    assert await _say(runtime, pipeline, "你最喜欢什么颜色？") is None
    assert not await runtime.resolve_lookup_owns_reply("那你喜欢小狗吗")
    assert len(search.queries) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_a_follow_up_without_any_source_speaks_the_weather_fallback() -> None:
    runtime, pipeline = _session("weather-no-source", _NoSources())

    assert await _say(runtime, pipeline, "今天天气怎么样") == WEATHER_LOOKUP_UNAVAILABLE_REPLY
    assert await _say(runtime, pipeline, "杭州") == WEATHER_LOOKUP_UNAVAILABLE_REPLY
    assert await _say(runtime, pipeline, "那明天呢") == WEATHER_LOOKUP_UNAVAILABLE_REPLY
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("day", "offset"), [("明天", 1), ("后天", 2), ("大后天", 3)])
async def test_followup_uses_the_requested_day_in_the_actual_weather_reply(
    day: str, offset: int,
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/geo/v2/city/lookup":
            return httpx.Response(200, json={"code": "200", "location": [{"name": "杭州", "adm1": "浙江省", "id": "101210101", "country": "中国"}]})
        return httpx.Response(200, json={"code": "200", "daily": [{"textDay": "晴", "tempMin": str(i), "tempMax": str(i + 10)} for i in range(7)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        weather = DomesticWeather(QWeatherConfig(api_key="test-key", api_host="abcd.re.qweatherapi.com"), client=client)
        runtime, pipeline = _session("weather-followup-offset", weather)
        try:
            assert await _say(runtime, pipeline, "杭州今天天气") == "杭州今天晴，0到10度。"
            assert await _say(runtime, pipeline, f"那{day}呢") == f"杭州{day}晴，{offset}到{offset + 10}度。"
            assert requests[-1].url.path == f"/v7/weather/{'7d' if offset == 3 else '3d'}"
        finally:
            await runtime.close()
