from __future__ import annotations

import pytest
from services.agent.src.duplex_runtime import DuplexRuntime
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
