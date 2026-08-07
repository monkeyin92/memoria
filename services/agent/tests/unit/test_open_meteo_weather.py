from __future__ import annotations

import httpx
import pytest
from services.agent.src.providers.open_meteo_weather import OpenMeteoWeather


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
