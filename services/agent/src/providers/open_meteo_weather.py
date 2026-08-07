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
    ("后天", 2, "后天"),
    ("明天", 1, "明天"),
    ("今天", 0, "今天"),
)
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


def _weather_day(query: str) -> tuple[int, str]:
    compact = re.sub(r"\s", "", query or "")
    for marker, offset, label in _WEATHER_DAY_OFFSETS:
        if marker in compact:
            return offset, label
    return 0, "今天"


def _wants_current_conditions(query: str, *, day_offset: int) -> bool:
    if day_offset != 0:
        return False
    compact = re.sub(r"\s", "", query or "")
    return "现在" in compact or "当前" in compact


def _location_candidates(query: str) -> tuple[str, ...]:
    """Return bounded suffixes so leading conversational filler cannot poison geocoding."""

    compact = re.sub(r"[\s，,。！？!?；;：:、]", "", query or "")
    before_weather = compact.split("天气", 1)[0]
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
        candidates = _location_candidates(query)
        if not candidates:
            return None
        day_offset, day_label = _weather_day(query)
        include_current = _wants_current_conditions(query, day_offset=day_offset)
        started = time.monotonic()
        try:
            location = await self._geocode(candidates)
            if location is None:
                return None
            forecast = await self._forecast(
                location, day_offset=day_offset, include_current=include_current
            )
            result = self._format(
                location,
                forecast,
                day_offset=day_offset,
                day_label=day_label,
                include_current=include_current,
            )
            logger.info(
                "open meteo weather lookup completed model=%s day_offset=%s elapsed_ms=%s",
                self.model,
                day_offset,
                round((time.monotonic() - started) * 1000),
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
        day_offset: int,
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
            "forecast_days": max(1, day_offset + 1),
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
        day_offset: int,
        day_label: str,
        include_current: bool,
    ) -> str:
        current = forecast.get("current")
        daily = forecast.get("daily")
        if not isinstance(daily, dict):
            raise ValueError("weather response is missing daily data")
        max_values = daily.get("temperature_2m_max")
        min_values = daily.get("temperature_2m_min")
        probability_values = daily.get("precipitation_probability_max")
        codes = daily.get("weather_code")
        if (
            not isinstance(max_values, list)
            or len(max_values) <= day_offset
            or not isinstance(min_values, list)
            or len(min_values) <= day_offset
            or not isinstance(codes, list)
            or len(codes) <= day_offset
        ):
            raise ValueError("weather response is missing daily temperatures")
        if not isinstance(probability_values, list) or len(probability_values) <= day_offset:
            raise ValueError("weather response is missing precipitation probability")
        maximum = cls._number(max_values[day_offset])
        minimum = cls._number(min_values[day_offset])
        probability = cls._number(probability_values[day_offset])
        if maximum is None or minimum is None or probability is None:
            raise ValueError("weather response contains invalid daily values")
        code = codes[day_offset]
        condition = (
            _WMO_CONDITIONS.get(int(code), "天气情况待确认")
            if isinstance(code, (int, float)) and not isinstance(code, bool)
            else "天气情况待确认"
        )
        name = str(location.get("name") or location.get("admin2") or "该城市")
        if not include_current:
            return f"{name}{day_label}{condition}，{minimum}到{maximum}度，降水概率{probability}%。"
        if not isinstance(current, dict):
            raise ValueError("weather response is missing current data")
        temperature = cls._number(current.get("temperature_2m"))
        if temperature is None:
            raise ValueError("weather response contains invalid current values")
        current_code = current.get("weather_code")
        if isinstance(current_code, (int, float)) and not isinstance(current_code, bool):
            condition = _WMO_CONDITIONS.get(int(current_code), "天气情况待确认")
        return (
            f"{name}现在{condition}，{temperature}度；今天{minimum}到{maximum}度，"
            f"降水概率{probability}%。"
        )

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
