"""Small public weather adapter used when a search-model key is unavailable."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import TypeGuard
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_WEATHER_MARKERS = ("天气", "天气预报")
_LOCATION_STOPWORDS = (
    "今天",
    "明天",
    "后天",
    "现在",
    "当前",
    "外面",
    "当地",
    "最近",
    "请问",
    "问一下",
    "问问",
    "帮我",
    "帮我查一下",
    "查一下",
    "查一查",
    "查询",
    "查查",
    "看看",
    "告诉我",
    "想知道",
    "的",
)
_WEATHER_DAY_OFFSETS = (
    ("后天", 2),
    ("明天", 1),
    ("今天", 0),
)
_DAY_LABELS = ("今天", "明天", "后天")
# Open-Meteo documents forecast_days as an integer in 0-16, so a longer range
# cannot be served and is refused instead of being answered for today.
_MAX_FORECAST_DAYS = 16
_RANGE_MARKERS = ("未来", "接下来", "接下去", "后面", "往后", "今后")
_RANGE_MARKER_RE = re.compile("|".join(_RANGE_MARKERS))
_RANGE_RE = re.compile(
    rf"(?:{'|'.join(_RANGE_MARKERS)})(?:这|那)?"
    r"(?P<count>[0-9]{1,2}|[一二两三四五六七八九十]{1,3})?"
    r"(?P<vague>几|数|多少|好些|一些|好多)?"
    r"(?P<unit>天|个星期|个礼拜|个月|星期|礼拜|周|月)"
)
# A continuation such as "未来三到五天" asks for a span, not for one window.
_RANGE_CONTINUATION_RE = re.compile(
    r"^(?:到|至|~|～|-|—|或者|还是|、|和)"
    r"(?:[0-9]{1,2}|[一二两三四五六七八九十]{1,3})?(?:天|周|月|星期|礼拜)"
)
# A range may only start today, so any other day word makes the start ambiguous.
_NON_TODAY_MARKERS = ("大后天", "后天", "明天")
_DAY_START_RE = re.compile(r"(?:从)?(?:今天|明天|后天|大后天|现在|当前)(?:起|开始)")
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
# Canonical counts only: "3", "三", "十", "十一".."十九". Malformed spelling such
# as "十十" is a broken range expression, not a day count.
_DAY_COUNT_RE = re.compile(r"[0-9]{1,2}|[一二两三四五六七八九十]|十[一二三四五六七八九]")
_WMO_CONDITIONS = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "有雾",
    48: "有雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "较强毛毛雨",
    56: "冻毛毛雨",
    57: "较强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "较强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "较强阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "较强阵雪",
    95: "雷雨",
    96: "雷雨并伴有冰雹",
    99: "雷雨并伴有较强冰雹",
}


def is_weather_query(query: str) -> bool:
    compact = re.sub(r"[\s，,。！？!?；;：:、]", "", query or "").lower()
    return bool(compact) and any(marker in compact for marker in _WEATHER_MARKERS)


@dataclass(frozen=True, slots=True)
class _WeatherWindow:
    """Days to answer for; the historical single-day reply is days == 1."""

    start_offset: int = 0
    days: int = 1

    @property
    def header(self) -> str:
        prefix = "未来" if self.start_offset == 0 else f"从{_day_label(self.start_offset)}起"
        return f"{prefix}{_cn_days(self.days)}天"


def _cn_days(count: int) -> str:
    """Render a day count as the Chinese numeral used by the range headline."""

    if count < len(_CN_DIGITS):
        return _CN_DIGITS[count]
    tens, ones = divmod(count, 10)
    prefix = "十" if tens == 1 else f"{_CN_DIGITS[tens]}十"
    return f"{prefix}{_CN_DIGITS[ones]}" if ones else prefix


def _day_count(token: str | None) -> int | None:
    """Parse a canonical arabic or Chinese day count; None for anything else."""

    if not token or _DAY_COUNT_RE.fullmatch(token) is None:
        return None
    if token.isdigit():
        return int(token)
    if token.startswith("十"):
        return 10 + (_CN_NUMERALS[token[1]] if len(token) > 1 else 0)
    return _CN_NUMERALS[token]


def _day_label(offset: int) -> str:
    return _DAY_LABELS[offset] if 0 <= offset < len(_DAY_LABELS) else f"第{_cn_days(offset + 1)}天"


def _weather_window(query: str) -> _WeatherWindow | None:
    """Resolve the requested day window, or None when a range cannot be honored."""

    compact = re.sub(r"\s", "", query or "")
    marker_count = len(_RANGE_MARKER_RE.findall(compact))
    if marker_count == 0:
        for marker, offset in _WEATHER_DAY_OFFSETS:
            if marker in compact:
                return _WeatherWindow(start_offset=offset)
        return _WeatherWindow()
    if marker_count > 1:
        # Every range expression counts, parseable or not: "未来三天南京天气
        # 未来100天" states two windows that cannot be reduced to one answer.
        return None
    match = _RANGE_RE.search(compact)
    if match is None:
        # Range intent with no parseable span ("未来100天", "未来三到五天").
        return None
    if match["vague"] is not None or match["unit"] != "天":
        return None
    days = _day_count(match["count"])
    if days is None or not 1 <= days <= _MAX_FORECAST_DAYS:
        return None
    if _RANGE_CONTINUATION_RE.match(compact[match.end() :]) is not None:
        return None
    if any(marker in compact for marker in _NON_TODAY_MARKERS):
        # "明天未来三天" starts tomorrow, so a window from today would be wrong.
        return None
    return _WeatherWindow(days=days)


def _wants_current_conditions(query: str) -> bool:
    compact = re.sub(r"\s", "", query or "")
    return "现在" in compact or "当前" in compact


def _location_candidates(query: str) -> tuple[str, ...]:
    """Return bounded suffixes so leading conversational filler cannot poison geocoding."""

    compact = re.sub(r"[\s，,。！？!?；;：:、]", "", query or "")
    # Only text before "天气" is read as a city. A trailing clause such as
    # "未来三天天气南京怎么样" intentionally yields no candidates: geocoding a
    # question fragment would resolve unrelated places.
    before_weather = compact.split("天气", 1)[0]
    # Drop a recognized range phrase ("未来三天南京天气") and a start phrase
    # ("从今天起未来三天南京天气"). Only the matched spans are removed: stripping
    # bare digits or day words would damage names such as 四川 or 三里屯.
    before_weather = _DAY_START_RE.sub("", _RANGE_RE.sub("", before_weather))
    for stopword in _LOCATION_STOPWORDS:
        before_weather = before_weather.replace(stopword, "")
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff·-]", "", before_weather)
    if len(clean) < 2:
        return ()
    candidates: list[str] = []
    # Try the complete administrative name first, then progressively shorter
    # suffixes (e.g. ``江苏省南京市`` -> ``南京市`` -> ``南京``).
    for start in range(0, max(1, len(clean) - 1)):
        candidate = clean[start:]
        if len(candidate) >= 2 and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


@dataclass(frozen=True, slots=True)
class OpenMeteoWeatherConfig:
    geocoding_url: str = "https://geocoding-api.open-meteo.com/v1/search"
    forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    timeout_s: float = 8.0
    slow_query_alert_ms: int = 5000

    def __post_init__(self) -> None:
        for name, value in (
            ("geocoding_url", self.geocoding_url),
            ("forecast_url", self.forecast_url),
        ):
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"{name} must be an HTTP(S) URL")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("weather timeout must be finite and positive")
        if self.slow_query_alert_ms <= 0:
            raise ValueError("weather slow-query threshold must be positive")


class OpenMeteoWeather:
    """Resolve a city weather query with public, keyless Open-Meteo APIs."""

    model = "open-meteo"

    def __init__(
        self,
        config: OpenMeteoWeatherConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or OpenMeteoWeatherConfig()
        self._client = client or httpx.AsyncClient(timeout=self.config.timeout_s)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def resolve(self, *, query: str) -> str | None:
        if not is_weather_query(query):
            return None
        window = _weather_window(query)
        if window is None:
            # An unsupported, indefinite or out-of-range window must never be
            # answered for today; fall through to the existing caller fallback.
            logger.info("open meteo weather range unsupported model=%s", self.model)
            return None
        candidates = _location_candidates(query)
        if not candidates:
            return None
        include_current = window.start_offset == 0 and _wants_current_conditions(query)
        started = time.monotonic()
        try:
            location = await self._geocode(candidates)
            if location is None:
                return None
            forecast = await self._forecast(
                location, window=window, include_current=include_current
            )
            result = self._format(
                location, forecast, window=window, include_current=include_current
            )
            elapsed_ms = round((time.monotonic() - started) * 1000)
            if elapsed_ms > self.config.slow_query_alert_ms:
                logger.warning(
                    "slow weather lookup detected model=%s elapsed_ms=%s",
                    self.model,
                    elapsed_ms,
                )
            logger.info(
                "open meteo weather lookup completed model=%s start_offset=%s days=%s elapsed_ms=%s",
                self.model,
                window.start_offset,
                window.days,
                elapsed_ms,
            )
            return result
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, TypeError, ValueError, KeyError, IndexError, OverflowError):
            logger.warning("open meteo weather lookup failed reason=provider_error")
            return None

    async def _geocode(self, candidates: tuple[str, ...]) -> dict[str, object] | None:
        for candidate in candidates:
            response = await self._client.get(
                self.config.geocoding_url,
                params={
                    "name": candidate,
                    "count": 1,
                    "language": "zh",
                    "format": "json",
                },
                timeout=self.config.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            first = results[0] if isinstance(results, list) and results else None
            if isinstance(first, dict):
                latitude = first.get("latitude")
                longitude = first.get("longitude")
                if self._finite_number(latitude) and self._finite_number(longitude):
                    return first
        return None

    async def _forecast(
        self,
        location: dict[str, object],
        *,
        window: _WeatherWindow,
        include_current: bool,
    ) -> dict[str, object]:
        latitude = location.get("latitude")
        longitude = location.get("longitude")
        if not self._finite_number(latitude) or not self._finite_number(longitude):
            raise ValueError("weather location is missing coordinates")
        params: dict[str, str | int | float] = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": (
                "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            ),
            "forecast_days": max(1, window.start_offset + window.days),
            "timezone": "auto",
        }
        if include_current:
            params["current"] = (
                "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation"
            )
        response = await self._client.get(
            self.config.forecast_url,
            params=params,
            timeout=self.config.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("weather response must be an object")
        return payload

    @classmethod
    def _format(
        cls,
        location: dict[str, object],
        forecast: dict[str, object],
        *,
        window: _WeatherWindow,
        include_current: bool,
    ) -> str:
        current = forecast.get("current")
        daily = forecast.get("daily")
        if not isinstance(daily, dict):
            raise ValueError("weather response is missing daily data")
        start = window.start_offset
        stop = start + window.days
        max_values = daily.get("temperature_2m_max")
        min_values = daily.get("temperature_2m_min")
        probability_values = daily.get("precipitation_probability_max")
        codes = daily.get("weather_code")
        if (
            not isinstance(max_values, list)
            or len(max_values) < stop
            or not isinstance(min_values, list)
            or len(min_values) < stop
            or not isinstance(codes, list)
            or len(codes) < stop
            or not isinstance(probability_values, list)
            or len(probability_values) < stop
        ):
            # A missing or truncated column cannot truthfully cover the window, so
            # no partial range is reported.
            raise ValueError("weather response does not cover the requested days")
        clauses: list[str] = []
        # The historical single-day current reply names the condition once, for
        # "现在"; every other reply names the condition of each covered day.
        with_condition = not include_current or window.days > 1
        for offset in range(start, stop):
            clause = cls._daily_clause(
                code=codes[offset],
                maximum=max_values[offset],
                minimum=min_values[offset],
                probability=probability_values[offset],
                with_condition=with_condition,
            )
            clauses.append(f"{_day_label(offset)}{clause}")
        name = str(location.get("name") or location.get("admin2") or "该城市")
        detail = clauses[0] if window.days == 1 else f"{window.header}：{'；'.join(clauses)}"
        if not include_current:
            return f"{name}{detail}。"
        if not isinstance(current, dict):
            raise ValueError("weather response is missing current data")
        temperature = cls._number(current.get("temperature_2m"))
        if temperature is None:
            raise ValueError("weather response contains invalid current values")
        condition = (
            cls._condition(current.get("weather_code"))
            or cls._condition(codes[start])
            or "天气情况待确认"
        )
        return f"{name}现在{condition}，{temperature}度；{detail}。"

    @classmethod
    def _daily_clause(
        cls,
        *,
        code: object,
        maximum: object,
        minimum: object,
        probability: object,
        with_condition: bool = True,
    ) -> str:
        maximum_text = cls._number(maximum)
        minimum_text = cls._number(minimum)
        probability_text = cls._number(probability)
        if maximum_text is None or minimum_text is None or probability_text is None:
            raise ValueError("weather response contains invalid daily values")
        condition = f"{cls._condition(code) or '天气情况待确认'}，" if with_condition else ""
        return f"{condition}{minimum_text}到{maximum_text}度，降水概率{probability_text}%"

    @classmethod
    def _condition(cls, code: object) -> str | None:
        """Map a WMO weather code, or None when the provider sent no usable code."""

        if isinstance(code, bool) or not isinstance(code, (int, float)):
            return None
        if not cls._finite_number(code):
            raise ValueError("weather response contains a non-finite weather code")
        return _WMO_CONDITIONS.get(int(code), "天气情况待确认")

    @staticmethod
    def _finite_number(value: object) -> TypeGuard[int | float]:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    @classmethod
    def _number(cls, value: object) -> str | None:
        if not cls._finite_number(value):
            return None
        number = float(value)
        return str(int(round(number))) if number.is_integer() else f"{number:.1f}"


__all__ = ["OpenMeteoWeather", "OpenMeteoWeatherConfig", "is_weather_query"]
