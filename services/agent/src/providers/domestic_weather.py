from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NewType, assert_never

import httpx

from services.agent.src.config import AgentSettings
from services.agent.src.providers.weather_places import City, find_city
from services.common.realtime_information import ASK_WEATHER_CITY_REPLY

logger = logging.getLogger(__name__)

_MAX_FORECAST_DAYS = 7
_SLOW_LOOKUP_MS = 5000
_WEATHER_MARKERS = ("天气", "天气预报")
_WEATHER_DAY_OFFSETS = (
    ("后天", 2),
    ("明天", 1),
    ("今天", 0),
)
_DAY_LABELS = ("今天", "明天", "后天")
_RANGE_MARKERS = ("未来", "接下来", "接下去", "后面", "往后", "今后")
_RANGE_MARKER_RE = re.compile("|".join(_RANGE_MARKERS))
_RANGE_RE = re.compile(
    rf"(?:{'|'.join(_RANGE_MARKERS)})(?:这|那)?"
    r"(?P<count>[0-9]{1,2}|[一二两三四五六七八九十]{1,3})?"
    r"(?P<vague>几|数|多少|好些|一些|好多)?"
    r"(?P<unit>天|个星期|个礼拜|个月|星期|礼拜|周|月)"
)
_RANGE_CONTINUATION_RE = re.compile(
    r"^(?:到|至|~|～|-|—|或者|还是|、|和)"
    r"(?:[0-9]{1,2}|[一二两三四五六七八九十]{1,3})?(?:天|周|月|星期|礼拜)"
)
_NON_TODAY_MARKERS = ("大后天", "后天", "明天")
_CN_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")
_CN_NUMERALS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_DAY_COUNT_RE = re.compile(r"[0-9]{1,2}|[一二两三四五六七八九十]|十[一二三四五六七八九]")
_HOST_RE = re.compile(r"(?:[a-z0-9-]+\.)+qweatherapi\.com\Z")
_LOCATION_ID_RE = re.compile(r"\d{9}")
_GEO_PATH = "/geo/v2/city/lookup"
_DAILY_3D = "/v7/weather/3d"
_DAILY_7D = "/v7/weather/7d"
_NOW_PATH = "/v7/weather/now"
_OUTBOUND_FIELDS: Mapping[str, tuple[str, ...]] = {
    _GEO_PATH: ("location", "range", "number"),
    _DAILY_3D: ("location",),
    _DAILY_7D: ("location",),
    _NOW_PATH: ("location",),
}
_API_KEY_HEADER = "X-QW-Api-Key"

LocationId = NewType("LocationId", str)


class _DropWeatherHostUrls(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "qweatherapi.com" not in record.getMessage()


def _quiet_transport_logs() -> None:
    httpx_logger = logging.getLogger("httpx")
    if httpx_logger.level < logging.WARNING:
        httpx_logger.setLevel(logging.WARNING)
    if not any(isinstance(item, _DropWeatherHostUrls) for item in httpx_logger.filters):
        httpx_logger.addFilter(_DropWeatherHostUrls())


def _owned_client(config: QWeatherConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=config.timeout_s, trust_env=False)


@dataclass(frozen=True, slots=True)
class QWeatherConfig:
    api_key: str
    api_host: str
    timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("QWeather API key is empty")
        if _HOST_RE.fullmatch(self.api_host) is None:
            raise ValueError("QWeather API host must be a bare *.qweatherapi.com hostname")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("QWeather timeout must be finite and positive")


@dataclass(frozen=True, slots=True)
class _ForecastWindow:
    start_offset: int = 0
    days: int = 1
    wants_now: bool = False

    def __post_init__(self) -> None:
        if self.start_offset < 0 or self.days < 1 or self.end > _MAX_FORECAST_DAYS:
            raise ValueError("forecast window is out of range")

    @property
    def end(self) -> int:
        return self.start_offset + self.days

    @property
    def header(self) -> str:
        prefix = "未来" if self.start_offset == 0 else f"从{_day_label(self.start_offset)}起"
        return f"{prefix}{_cn_days(self.days)}天"


@dataclass(frozen=True, slots=True)
class _Day:
    condition: str
    low_c: int
    high_c: int


@dataclass(frozen=True, slots=True)
class _Now:
    condition: str
    temp_c: int


@dataclass(frozen=True, slots=True)
class _Skip:
    pass


@dataclass(frozen=True, slots=True)
class _AskCity:
    pass


@dataclass(frozen=True, slots=True)
class _Ready:
    city: City
    window: _ForecastWindow


class _QWeatherError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _cn_days(count: int) -> str:
    if count < len(_CN_DIGITS):
        return _CN_DIGITS[count]
    tens, ones = divmod(count, 10)
    prefix = "十" if tens == 1 else f"{_CN_DIGITS[tens]}十"
    return f"{prefix}{_CN_DIGITS[ones]}" if ones else prefix


def _day_count(token: str | None) -> int | None:
    if not token or _DAY_COUNT_RE.fullmatch(token) is None:
        return None
    if token.isdigit():
        return int(token)
    if token.startswith("十"):
        return 10 + (_CN_NUMERALS[token[1]] if len(token) > 1 else 0)
    return _CN_NUMERALS[token]


def _day_label(offset: int) -> str:
    if 0 <= offset < len(_DAY_LABELS):
        return _DAY_LABELS[offset]
    return f"第{_cn_days(offset + 1)}天"


def _is_weather_query(query: str) -> bool:
    compact = re.sub(r"[\s，,。！？!?；;：:、]", "", query or "").lower()
    return bool(compact) and any(marker in compact for marker in _WEATHER_MARKERS)


def _wants_now(query: str) -> bool:
    compact = re.sub(r"\s", "", query or "")
    return "现在" in compact or "当前" in compact


def _forecast_window(query: str) -> _ForecastWindow | None:
    compact = re.sub(r"\s", "", query or "")
    wants_now = _wants_now(query)
    marker_count = len(_RANGE_MARKER_RE.findall(compact))
    if marker_count == 0:
        for marker, offset in _WEATHER_DAY_OFFSETS:
            if marker in compact:
                return _ForecastWindow(
                    start_offset=offset,
                    wants_now=wants_now and offset == 0,
                )
        return _ForecastWindow(wants_now=wants_now)
    if marker_count > 1:
        return None
    match = _RANGE_RE.search(compact)
    if match is None or match["vague"] is not None or match["unit"] != "天":
        return None
    days = _day_count(match["count"])
    if days is None or not 1 <= days <= _MAX_FORECAST_DAYS:
        return None
    if _RANGE_CONTINUATION_RE.match(compact[match.end() :]) is not None:
        return None
    if any(marker in compact for marker in _NON_TODAY_MARKERS):
        return None
    return _ForecastWindow(days=days, wants_now=wants_now)


def _classify(query: str) -> _Skip | _AskCity | _Ready:
    if not _is_weather_query(query):
        return _Skip()
    window = _forecast_window(query)
    if window is None:
        return _Skip()
    city = find_city(query)
    if city is None:
        return _AskCity()
    return _Ready(city=city, window=window)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _QWeatherError("bad_payload")
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise _QWeatherError("bad_payload")
        parsed[key] = item
    return parsed


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _QWeatherError("bad_payload")
    return value.strip()


def _integer(value: object) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise _QWeatherError("bad_temp")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    raise _QWeatherError("bad_temp")


def _format(
    city: City,
    window: _ForecastWindow,
    days: tuple[_Day, ...],
    now: _Now | None,
) -> str:
    selected = days[window.start_offset : window.end]
    if len(selected) != window.days:
        raise _QWeatherError("short_daily")
    name = city.standard_name

    def clause(offset: int, day: _Day) -> str:
        return f"{_day_label(offset)}{day.condition}，{day.low_c}到{day.high_c}度"

    if window.days == 1:
        day = selected[0]
        today = clause(window.start_offset, day)
        if now is None:
            return f"{name}{today}。"
        return f"{name}现在{now.condition}，{now.temp_c}度；{today}。"
    body = "；".join(clause(window.start_offset + index, day) for index, day in enumerate(selected))
    forecast = f"{window.header}：{body}"
    if now is None:
        return f"{name}{forecast}。"
    return f"{name}现在{now.condition}，{now.temp_c}度；{forecast}。"


class _QWeather:
    def __init__(self, config: QWeatherConfig, client: httpx.AsyncClient) -> None:
        self._config = config
        self._client = client
        self._location_ids: dict[City, LocationId] = {}

    async def daily(self, city: City, *, count: int) -> tuple[_Day, ...]:
        if not 1 <= count <= _MAX_FORECAST_DAYS:
            raise _QWeatherError("bad_count")
        location_id = await self._location_id(city)
        path = _DAILY_3D if count <= 3 else _DAILY_7D
        body = await self._get(path, {"location": location_id})
        rows = body.get("daily")
        if not isinstance(rows, list) or len(rows) < count:
            raise _QWeatherError("short_daily")
        days: list[_Day] = []
        for row in rows[:count]:
            parsed = _mapping(row)
            days.append(
                _Day(
                    condition=_text(parsed.get("textDay")),
                    low_c=_integer(parsed.get("tempMin")),
                    high_c=_integer(parsed.get("tempMax")),
                )
            )
        return tuple(days)

    async def now(self, city: City) -> _Now:
        location_id = await self._location_id(city)
        body = await self._get(_NOW_PATH, {"location": location_id})
        current = _mapping(body.get("now"))
        return _Now(condition=_text(current.get("text")), temp_c=_integer(current.get("temp")))

    async def _location_id(self, city: City) -> LocationId:
        cached = self._location_ids.get(city)
        if cached is not None:
            return cached
        body = await self._get(
            _GEO_PATH,
            {"location": city.standard_name, "range": "cn", "number": "10"},
        )
        rows = body.get("location")
        if not isinstance(rows, list):
            raise _QWeatherError("bad_payload")
        for row in rows:
            parsed = _mapping(row)
            if (
                parsed.get("country") != "中国"
                or parsed.get("name") != city.standard_name
                or parsed.get("adm1") != city.province
            ):
                continue
            raw_id = parsed.get("id")
            if not isinstance(raw_id, str) or _LOCATION_ID_RE.fullmatch(raw_id) is None:
                raise _QWeatherError("bad_id")
            location_id = LocationId(raw_id)
            self._location_ids[city] = location_id
            return location_id
        raise _QWeatherError("geo_unverified")

    async def _get(self, path: str, values: Mapping[str, str]) -> dict[str, object]:
        params = {field: values[field] for field in _OUTBOUND_FIELDS[path]}
        response = await self._client.get(
            f"https://{self._config.api_host}{path}",
            params=params,
            headers={_API_KEY_HEADER: self._config.api_key},
        )
        response.raise_for_status()
        body = _mapping(response.json())
        if body.get("code") != "200":
            raise _QWeatherError("bad_code")
        return body


class DomesticWeather:
    model = "qweather"

    def __init__(self, config: QWeatherConfig, *, client: httpx.AsyncClient | None = None) -> None:
        _quiet_transport_logs()
        owns_client = client is None
        self._qweather = _QWeather(config, client or _owned_client(config))
        self._owns_client = owns_client
        self._timeout_s = config.timeout_s

    async def resolve(self, *, query: str) -> str | None:
        decision = _classify(query)
        match decision:
            case _AskCity():
                return ASK_WEATHER_CITY_REPLY
            case _Skip():
                return None
            case _Ready() as ready:
                return await self._answer(ready)
            case _ as unreachable:
                assert_never(unreachable)

    async def _answer(self, ready: _Ready) -> str | None:
        started = time.monotonic()
        try:
            async with asyncio.timeout(self._timeout_s):
                days = await self._qweather.daily(ready.city, count=ready.window.end)
                now = await self._qweather.now(ready.city) if ready.window.wants_now else None
            spoken = _format(ready.city, ready.window, days, now)
            elapsed_ms = round((time.monotonic() - started) * 1000)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._failed("deadline_exceeded")
            return None
        except _QWeatherError as exc:
            self._failed(exc.reason)
            return None
        except (httpx.HTTPError, ValueError):
            self._failed("provider_error")
            return None
        if elapsed_ms > _SLOW_LOOKUP_MS:
            logger.warning(
                "slow weather lookup detected model=%s elapsed_ms=%s",
                self.model,
                elapsed_ms,
            )
        logger.info(
            "domestic weather lookup completed model=%s start_offset=%s days=%s elapsed_ms=%s",
            self.model,
            ready.window.start_offset,
            ready.window.days,
            elapsed_ms,
        )
        return spoken

    def _failed(self, reason: str) -> None:
        logger.warning("domestic weather lookup failed reason=%s", reason)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._qweather._client.aclose()


def build_domestic_weather(settings: AgentSettings) -> DomesticWeather | None:
    if settings.weather_lookup_provider == "off":
        return None
    key = settings.qweather_api_key.get_secret_value().strip()
    host = settings.qweather_api_host.strip()
    if not key or not host:
        logger.warning("weather lookup disabled reason=missing_credentials provider=domestic")
        return None
    return DomesticWeather(QWeatherConfig(api_key=key, api_host=host))


__all__ = ["DomesticWeather", "QWeatherConfig", "build_domestic_weather"]
