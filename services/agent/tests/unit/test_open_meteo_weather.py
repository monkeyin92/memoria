from __future__ import annotations

import httpx
import pytest
from services.agent.src.providers.open_meteo_weather import (
    OpenMeteoWeather,
    _day_count,
    _WeatherWindow,
)


def _daily(
    *entries: tuple[int, float, float, int],
) -> dict[str, list[object]]:
    """Build a daily block from ``(weather_code, max, min, probability)`` rows."""

    return {
        "weather_code": [code for code, _, _, _ in entries],
        "temperature_2m_max": [maximum for _, maximum, _, _ in entries],
        "temperature_2m_min": [minimum for _, _, minimum, _ in entries],
        "precipitation_probability_max": [probability for _, _, _, probability in entries],
    }


def _transport(
    requests: list[httpx.Request],
    *,
    daily: dict[str, list[object]],
    name: str = "南京",
    current: dict[str, object] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": name,
                            "latitude": 32.06167,
                            "longitude": 118.77778,
                            "timezone": "Asia/Shanghai",
                        }
                    ]
                },
            )
        payload: dict[str, object] = {"daily": daily}
        if current is not None:
            payload["current"] = current
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _names(requests: list[httpx.Request]) -> list[str]:
    return [
        str(request.url.params["name"])
        for request in requests
        if request.url.host == "geocoding-api.open-meteo.com"
    ]


@pytest.mark.asyncio
async def test_open_meteo_weather_resolves_nanjing_without_a_provider_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "南京",
                            "latitude": 32.06167,
                            "longitude": 118.77778,
                            "timezone": "Asia/Shanghai",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 31.2,
                    "apparent_temperature": 34.6,
                    "weather_code": 61,
                    "wind_speed_10m": 9.4,
                    "precipitation": 0,
                },
                "daily": {
                    "weather_code": [2],
                    "temperature_2m_max": [34.1],
                    "temperature_2m_min": [26.0],
                    "precipitation_probability_max": [35],
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = OpenMeteoWeather(client=client)

    current_result = await resolver.resolve(query="现在南京天气怎么样")
    today_result = await resolver.resolve(query="今天南京天气怎么样")

    assert current_result == "南京现在小雨，31.2度；今天26到34.1度，降水概率35%。"
    assert today_result == "南京今天局部多云，26到34.1度，降水概率35%。"
    assert requests[0].url.params["name"] == "南京"
    assert requests[1].url.params["timezone"] == "auto"
    assert "current" in requests[1].url.params
    assert "current" not in requests[3].url.params
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_does_not_guess_a_missing_city() -> None:
    requests: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(500)
        )
    )
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query="今天天气怎么样") is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_uses_tomorrow_daily_slot_and_compact_copy() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "geocoding-api.open-meteo.com":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "name": "上海",
                            "latitude": 31.23,
                            "longitude": 121.47,
                            "timezone": "Asia/Shanghai",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "daily": {
                    "time": ["2026-08-07", "2026-08-08"],
                    "weather_code": [0, 61],
                    "temperature_2m_max": [35.0, 31.0],
                    "temperature_2m_min": [28.0, 26.0],
                    "precipitation_probability_max": [20, 65],
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    resolver = OpenMeteoWeather(client=client)

    result = await resolver.resolve(query="明天上海的天气怎么样")

    assert result == "上海明天小雨，26到31度，降水概率65%。"
    assert requests[1].url.params["forecast_days"] == "2"
    assert "current" not in requests[1].url.params
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_covers_a_leading_future_range() -> None:
    requests: list[httpx.Request] = []
    client = httpx.AsyncClient(
        transport=_transport(
            requests,
            daily=_daily((2, 34.1, 26.0, 35), (61, 31.0, 26.0, 65), (3, 30.0, 25.0, 40)),
        )
    )
    resolver = OpenMeteoWeather(client=client)

    result = await resolver.resolve(query="未来三天南京天气怎么样")

    assert result == (
        "南京未来三天：今天局部多云，26到34.1度，降水概率35%；"
        "明天小雨，26到31度，降水概率65%；后天阴，25到30度，降水概率40%。"
    )
    assert _names(requests) == ["南京"]
    assert requests[-2].url.host == "geocoding-api.open-meteo.com"
    assert requests[-1].url.params["forecast_days"] == "3"
    assert "current" not in requests[-1].url.params
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_covers_a_trailing_arabic_range() -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    result = await resolver.resolve(query="南京接下来3天天气")

    assert result == (
        "南京未来三天：今天晴，22到30度，降水概率10%；"
        "明天晴，23到31度，降水概率15%；后天晴，24到32度，降水概率20%。"
    )
    assert _names(requests) == ["南京"]
    assert requests[-1].url.params["forecast_days"] == "3"
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_keeps_the_range_when_current_is_requested() -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(
        transport=_transport(
            requests,
            daily=daily,
            current={"temperature_2m": 28.5, "weather_code": 2},
        )
    )
    resolver = OpenMeteoWeather(client=client)

    result = await resolver.resolve(query="现在南京未来三天天气")

    assert result == (
        "南京现在局部多云，28.5度；未来三天：今天晴，22到30度，降水概率10%；"
        "明天晴，23到31度，降水概率15%；后天晴，24到32度，降水概率20%。"
    )
    assert "current" in requests[-1].url.params
    assert requests[-1].url.params["forecast_days"] == "3"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "未来几天南京天气",
        "未来一周南京天气",
        "未来17天南京天气",
        "未来二十天南京天气",
    ],
)
async def test_open_meteo_weather_rejects_ranges_it_cannot_cover(query: str) -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query=query) is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_rejects_an_explicit_non_today_range_start() -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily, name="上海"))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query="上海从明天起未来三天天气") is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_does_not_guess_a_city_for_a_range() -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query="未来三天天气怎么样") is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_refuses_a_truncated_daily_range() -> None:
    requests: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=_transport(requests, daily=_daily((0, 30.0, 22.0, 10))))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query="未来三天南京天气") is None
    assert requests[-1].url.params["forecast_days"] == "3"
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_covers_the_provider_maximum_range() -> None:
    requests: list[httpx.Request] = []
    daily = _daily(*[(0, 30.0 + index, 20.0 + index, index) for index in range(16)])
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily, name="广州"))
    resolver = OpenMeteoWeather(client=client)

    result = await resolver.resolve(query="未来16天广州天气")

    assert result is not None
    assert result.startswith("广州未来十六天：今天晴，20到30度，降水概率0%；")
    assert result.endswith("；第十六天晴，35到45度，降水概率15%。")
    assert result.count("；") == 15
    assert requests[-1].url.params["forecast_days"] == "16"
    assert await resolver.resolve(query="未来17天广州天气") is None
    assert len(requests) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_keeps_numerals_inside_city_names() -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily, name="四川"))
    resolver = OpenMeteoWeather(client=client)

    result = await resolver.resolve(query="四川未来三天天气")

    assert result is not None and result.startswith("四川未来三天：")
    assert _names(requests) == ["四川"]
    assert requests[-1].url.params["forecast_days"] == "3"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "未来100天南京天气",
        "未来零天南京天气",
        "未来一百天南京天气",
        "未来半个月南京天气",
        "未来一个月南京天气",
        "未来三天到五天南京天气",
        "未来三到五天南京天气",
    ],
)
async def test_open_meteo_weather_refuses_unparsed_range_intent(query: str) -> None:
    """A range the adapter cannot fully parse never falls back to today."""

    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query=query) is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_refuses_a_city_after_the_weather_word() -> None:
    """A trailing city is refused instead of geocoding a question fragment."""

    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query="未来三天天气南京怎么样") is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["明天未来三天南京天气", "后天未来三天南京天气"])
async def test_open_meteo_weather_refuses_a_non_today_start_without_a_particle(query: str) -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query=query) is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["未来三天未来五天南京天气", "接下来三天未来五天南京天气"],
)
async def test_open_meteo_weather_refuses_conflicting_ranges(query: str) -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query=query) is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_cleans_a_today_start_phrase() -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    result = await resolver.resolve(query="从今天起未来三天南京天气")

    assert result is not None and result.startswith("南京未来三天：")
    assert _names(requests) == ["南京"]
    assert requests[-1].url.params["forecast_days"] == "3"
    await client.aclose()


@pytest.mark.asyncio
async def test_open_meteo_weather_treats_non_finite_daily_values_as_invalid() -> None:
    """Non-finite values are invalid data, not an int() overflow escape."""

    requests: list[httpx.Request] = []
    daily = {
        "weather_code": [float("inf"), float("inf"), float("inf")],
        "temperature_2m_max": [30.0, 31.0, 32.0],
        "temperature_2m_min": [20.0, 21.0, 22.0],
        "precipitation_probability_max": [10, 15, 20],
    }
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query="未来三天南京天气") is None
    with pytest.raises(ValueError):
        OpenMeteoWeather._format(
            {"name": "南京"},
            {"daily": daily},
            window=_WeatherWindow(),
            include_current=False,
        )
    with pytest.raises(ValueError):
        OpenMeteoWeather._format(
            {"name": "南京"},
            {"daily": {**daily, "weather_code": [0], "temperature_2m_max": [float("inf")]}},
            window=_WeatherWindow(),
            include_current=False,
        )
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "未来三天南京天气未来100天",
        "未来三天南京天气，未来几个月呢",
        "未来三天南京天气，接下来三天呢",
    ],
)
async def test_open_meteo_weather_counts_every_range_marker(query: str) -> None:
    """A second range marker is refused even when only one span is parseable."""

    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query=query) is None
    assert requests == []
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["未来十十南京天气", "未来二十十南京天气"])
async def test_open_meteo_weather_refuses_malformed_day_counts(query: str) -> None:
    requests: list[httpx.Request] = []
    daily = _daily((0, 30.0, 22.0, 10), (0, 31.0, 23.0, 15), (0, 32.0, 24.0, 20))
    client = httpx.AsyncClient(transport=_transport(requests, daily=daily))
    resolver = OpenMeteoWeather(client=client)

    assert await resolver.resolve(query=query) is None
    assert requests == []
    await client.aclose()


def test_open_meteo_weather_day_count_accepts_only_canonical_counts() -> None:
    assert _day_count("3") == 3
    assert _day_count("16") == 16
    assert _day_count("三") == 3
    assert _day_count("十") == 10
    assert _day_count("十一") == 11
    assert _day_count("十六") == 16
    assert _day_count("十十") is None
    assert _day_count("二十") is None
    assert _day_count("几") is None
    assert _day_count(None) is None
