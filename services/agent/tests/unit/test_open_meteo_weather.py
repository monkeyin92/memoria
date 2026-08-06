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
                    "weather_code": 2,
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

    result = await resolver.resolve(query="今天南京天气怎么样")

    assert result == (
        "南京现在局部多云，气温31.2摄氏度，体感34.6摄氏度，风速约9.4公里每小时。"
        "今天最高34.1摄氏度，最低26摄氏度，降水概率35%。"
    )
    assert requests[0].url.params["name"] == "南京"
    assert requests[1].url.params["timezone"] == "auto"
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
