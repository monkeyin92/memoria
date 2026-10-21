from datetime import datetime
from types import SimpleNamespace

from services.common.realtime_information import (
    REALTIME_UNAVAILABLE_REPLY,
    WEATHER_LOOKUP_UNAVAILABLE_REPLY,
    asks_about_weather,
    fixed_realtime_reply,
    is_incomplete_realtime_reply,
    is_safe_realtime_reply,
    realtime_unavailable_reply,
    weather_lookup_configured,
)

_SENTENCE = "我现在还查不到天气，可以请爸爸妈妈帮你看看窗外或者手机哦。"
_NOW = datetime.fromisoformat("2026-07-30T18:42:00+08:00")


def test_unavailable_weather_uses_the_product_sentence_on_both_turns() -> None:
    assert WEATHER_LOOKUP_UNAVAILABLE_REPLY == _SENTENCE
    first = fixed_realtime_reply(
        query="今天天气怎么样",
        now=_NOW,
        weather_lookup_available=False,
    )
    second = fixed_realtime_reply(
        query="杭州",
        now=_NOW,
        weather_lookup_available=False,
        city_followup=True,
    )

    assert first == _SENTENCE
    assert second == _SENTENCE
    assert is_safe_realtime_reply(_SENTENCE) is True
    assert is_incomplete_realtime_reply(_SENTENCE, query="今天天气怎么样") is False
    assert (
        fixed_realtime_reply(
            query="杭州",
            now=_NOW,
            weather_lookup_available=False,
        )
        is None
    )
    assert (
        fixed_realtime_reply(query="今天天气怎么样", now=_NOW) == "你想查哪个城市呀？"
    )


def test_weather_lookup_is_configured_only_with_a_real_source() -> None:
    assert weather_lookup_configured(SimpleNamespace()) is False
    assert (
        weather_lookup_configured(SimpleNamespace(live_search_qwen_enabled=True)) is True
    )
    assert (
        weather_lookup_configured(
            SimpleNamespace(
                live_search_qwen_enabled=False,
                weather_lookup_provider="domestic",
                qweather_api_key="k",
                qweather_api_host="abcd.re.qweatherapi.com",
            )
        )
        is True
    )
    assert (
        weather_lookup_configured(
            SimpleNamespace(
                weather_lookup_provider="domestic",
                qweather_api_key="",
                qweather_api_host="abcd.re.qweatherapi.com",
            )
        )
        is False
    )


def test_weather_fallback_covers_weather_questions_without_the_literal_word() -> None:
    for query in ("明天会下雨吗？", "外面冷不冷？", "今天要穿外套吗？", "这周末天气好吗？"):
        assert asks_about_weather(query) is True
        assert (
            fixed_realtime_reply(
                query=query,
                now=_NOW,
                weather_lookup_available=False,
            )
            == _SENTENCE
        )
        assert realtime_unavailable_reply(query, now=_NOW) == _SENTENCE


def test_non_weather_live_failure_keeps_the_generic_fallback() -> None:
    assert asks_about_weather("美元兑人民币汇率是多少？") is False
    assert realtime_unavailable_reply("美元兑人民币汇率是多少？", now=_NOW) == (
        REALTIME_UNAVAILABLE_REPLY
    )
