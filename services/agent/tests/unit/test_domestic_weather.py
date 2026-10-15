from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from services.agent.src.config import AgentSettings
from services.agent.src.providers.domestic_weather import (
    _OUTBOUND_FIELDS,
    DomesticWeather,
    QWeatherConfig,
    build_domestic_weather,
)
from services.agent.src.providers.weather_places import City
from services.common.realtime_information import ASK_WEATHER_CITY_REPLY

CONFIG = QWeatherConfig(api_key="test-key", api_host="abcd.re.qweatherapi.com")
ALLOWED_HEADER_NAMES = {
    "host",
    "accept",
    "accept-encoding",
    "connection",
    "user-agent",
    "x-qw-api-key",
}
_LOCATION_IDS = {
    "北京": "101010100",
    "南京": "101190101",
    "上海": "101020100",
    "广州": "101280101",
}


def _daily_rows(
    count: int = 7,
    *,
    condition: str = "多云",
    low: str = "15",
    high: str = "24",
) -> list[dict[str, str]]:
    return [{"textDay": condition, "tempMin": low, "tempMax": high} for _ in range(count)]


def _geo_row(
    name: str,
    *,
    adm1: str | None = None,
    location_id: str | None = None,
) -> dict[str, str]:
    return {
        "name": name,
        "id": location_id or _LOCATION_IDS.get(name, "101000000"),
        "adm1": City[name].province if adm1 is None else adm1,
        "country": "中国",
    }


def qweather_transport(
    requests: list[httpx.Request],
    *,
    geo_rows: list[dict[str, str]] | None = None,
    status: int = 200,
    code: str = "200",
    daily: list[dict[str, str]] | None = None,
    now: dict[str, str] | None = None,
    hang: bool = False,
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if hang:
            await asyncio.Event().wait()
        if status != 200:
            return httpx.Response(status)
        if request.url.path == "/geo/v2/city/lookup":
            name = str(request.url.params["location"])
            rows = geo_rows if geo_rows is not None else [_geo_row(name)]
            return httpx.Response(200, json={"code": code, "location": rows})
        if request.url.path == "/v7/weather/now":
            payload = now or {"text": "晴", "temp": "22"}
            return httpx.Response(200, json={"code": code, "now": payload})
        if request.url.path in {"/v7/weather/3d", "/v7/weather/7d"}:
            return httpx.Response(200, json={"code": code, "daily": daily or _daily_rows()})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _weather(
    requests: list[httpx.Request],
    *,
    geo_rows: list[dict[str, str]] | None = None,
    status: int = 200,
    code: str = "200",
    daily: list[dict[str, str]] | None = None,
    now: dict[str, str] | None = None,
    hang: bool = False,
) -> DomesticWeather:
    return DomesticWeather(
        CONFIG,
        client=httpx.AsyncClient(
            transport=qweather_transport(
                requests,
                geo_rows=geo_rows,
                status=status,
                code=code,
                daily=daily,
                now=now,
                hang=hang,
            )
        ),
    )


def _wire(requests: list[httpx.Request]) -> list[tuple[str, str, str, dict[str, str]]]:
    return [
        (request.method, request.url.host, request.url.path, dict(request.url.params))
        for request in requests
    ]


async def test_match_sends_only_the_canonical_city() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests)

    reply = await weather.resolve(query="我是小明，我家在海淀区，明天天气怎么样")

    assert reply == "北京明天多云，15到24度。"
    assert _wire(requests) == [
        (
            "GET",
            "abcd.re.qweatherapi.com",
            "/geo/v2/city/lookup",
            {"location": "北京", "range": "cn", "number": "10"},
        ),
        (
            "GET",
            "abcd.re.qweatherapi.com",
            "/v7/weather/3d",
            {"location": "101010100"},
        ),
    ]
    assert all(request.content == b"" for request in requests)
    assert all("test-key" not in str(request.url) for request in requests)
    assert all(
        "小明" not in str(request.url) and "海淀" not in str(request.url) for request in requests
    )
    for request in requests:
        assert {name.lower() for name in request.headers} <= ALLOWED_HEADER_NAMES
        assert request.headers["X-QW-Api-Key"] == "test-key"
        assert set(request.url.params) == set(_OUTBOUND_FIELDS[request.url.path])


async def test_now_adds_the_current_conditions_request() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(
        requests,
        daily=[{"textDay": "晴", "tempMin": "15", "tempMax": "24"}],
    )

    reply = await weather.resolve(query="我是小明，我家在海淀区，现在天气怎么样")

    assert reply == "北京现在晴，22度；今天晴，15到24度。"
    assert [request.url.path for request in requests] == [
        "/geo/v2/city/lookup",
        "/v7/weather/3d",
        "/v7/weather/now",
    ]
    assert "小明" not in str(requests[0].url) and "海淀" not in str(requests[0].url)


async def test_five_day_span_uses_the_seven_day_endpoint() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests)

    reply = await weather.resolve(query="未来五天南京天气")

    assert reply == (
        "南京未来五天：今天多云，15到24度；明天多云，15到24度；后天多云，15到24度；"
        "第四天多云，15到24度；第五天多云，15到24度。"
    )
    assert requests[1].url.path == "/v7/weather/7d"


async def test_seven_day_span_is_the_longest_spoken_range() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests)

    reply = await weather.resolve(query="未来七天上海天气")

    assert reply == (
        "上海未来七天：今天多云，15到24度；明天多云，15到24度；后天多云，15到24度；"
        "第四天多云，15到24度；第五天多云，15到24度；第六天多云，15到24度；"
        "第七天多云，15到24度。"
    )
    assert requests[1].url.path == "/v7/weather/7d"


@pytest.mark.parametrize(
    "query",
    [
        "东京天气怎么样",
        "今天天气怎么样",
        "北京和上海天气",
    ],
)
async def test_no_allowlist_match_asks_for_city_without_http(query: str) -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests)

    assert await weather.resolve(query=query) == ASK_WEATHER_CITY_REPLY
    assert requests == []


@pytest.mark.parametrize(
    "query",
    [
        "南京未来十天天气",
        "未来八天南京天气",
        "未来几天南京天气",
        "未来一周南京天气",
        "未来三到五天南京天气",
        "南京从明天起未来三天天气",
        "明天未来三天南京天气",
        "未来三天未来五天南京天气",
        "今天有什么新闻",
    ],
)
async def test_unsupported_or_non_weather_query_falls_through_without_http(query: str) -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests)

    assert await weather.resolve(query=query) is None
    assert requests == []


async def test_second_question_for_the_same_city_skips_geo() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests)

    first = await weather.resolve(query="南京今天天气")
    second = await weather.resolve(query="南京明天天气")

    assert first == "南京今天多云，15到24度。"
    assert second == "南京明天多云，15到24度。"
    assert [request.url.path for request in requests].count("/geo/v2/city/lookup") == 1


async def test_geo_row_from_the_wrong_province_does_not_fetch_a_forecast() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests, geo_rows=[_geo_row("南京", adm1="上海市")])

    assert await weather.resolve(query="南京明天天气") is None
    assert [request.url.path for request in requests] == ["/geo/v2/city/lookup"]


async def test_vendor_failure_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests, status=500)

    with caplog.at_level(logging.WARNING):
        assert await weather.resolve(query="我是小明南京天气") is None

    assert "小明" not in caplog.text
    assert "南京" not in caplog.text
    assert "reason=provider_error" in caplog.text


async def test_vendor_code_is_not_success() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests, code="401")

    assert await weather.resolve(query="南京天气") is None


async def test_truncated_daily_forecast_returns_none() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests, daily=_daily_rows(1))

    assert await weather.resolve(query="未来三天南京天气") is None
    assert requests[-1].url.path == "/v7/weather/3d"


async def test_deadline_returns_none_without_the_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    weather = DomesticWeather(
        QWeatherConfig(api_key="test-key", api_host="abcd.re.qweatherapi.com", timeout_s=0.05),
        client=httpx.AsyncClient(transport=qweather_transport(requests, hang=True)),
    )

    with caplog.at_level(logging.WARNING):
        assert await weather.resolve(query="南京天气怎么样") is None

    assert "reason=deadline_exceeded" in caplog.text
    assert "南京" not in caplog.text


async def test_caller_cancellation_propagates() -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests, hang=True)
    task = asyncio.create_task(weather.resolve(query="南京天气"))
    for _ in range(50):
        if requests:
            break
        await asyncio.sleep(0.01)
    assert len(requests) == 1
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_config_rejects_a_foreign_host_and_an_empty_key() -> None:
    with pytest.raises(ValueError, match="hostname"):
        QWeatherConfig(api_key="k", api_host="https://evil.example")
    with pytest.raises(ValueError, match="empty"):
        QWeatherConfig(api_key="  ", api_host="abcd.re.qweatherapi.com")


async def test_owned_client_ignores_the_process_proxy_environment() -> None:
    weather = DomesticWeather(CONFIG)

    client = weather._qweather._client
    assert client.trust_env is False
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert any(
        isinstance(item, logging.Filter) and type(item).__name__ == "_DropWeatherHostUrls"
        for item in logging.getLogger("httpx").filters
    )
    await weather.aclose()


async def test_factory_is_absent_until_domestic_credentials_exist() -> None:
    assert build_domestic_weather(AgentSettings(_env_file=None, DASHSCOPE_API_KEY="k")) is None
    assert (
        build_domestic_weather(
            AgentSettings(
                _env_file=None,
                WEATHER_LOOKUP_PROVIDER="domestic",
                QWEATHER_API_KEY="k",
            )
        )
        is None
    )
    weather = build_domestic_weather(
        AgentSettings(
            _env_file=None,
            WEATHER_LOOKUP_PROVIDER="domestic",
            QWEATHER_API_KEY="k",
            QWEATHER_API_HOST="abcd.re.qweatherapi.com",
        )
    )
    assert isinstance(weather, DomesticWeather)
    await weather.aclose()


async def test_success_log_records_the_window_and_not_the_utterance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    weather = _weather(requests)

    with caplog.at_level(logging.INFO):
        assert await weather.resolve(query="我是小明南京明天天气") == "南京明天多云，15到24度。"

    completed = [
        record.getMessage()
        for record in caplog.records
        if record.name == "services.agent.src.providers.domestic_weather"
        and record.getMessage().startswith("domestic weather lookup completed ")
    ]
    assert len(completed) == 1
    prefix, _, elapsed = completed[0].partition("elapsed_ms=")
    assert prefix == ("domestic weather lookup completed model=qweather start_offset=1 days=1 ")
    assert elapsed.isdigit()
    assert "小明" not in caplog.text
    assert "南京" not in caplog.text
