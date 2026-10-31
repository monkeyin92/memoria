"""The city and day a session's weather questions are about.

A child asks 「今天天气怎么样」 with no place, answers 「杭州」, then asks
「那明天呢」. Each turn alone is not a searchable weather question; the thread
carries the city and day from one committed turn to the next.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from services.agent.src.providers.weather_places import (
    City,
    find_city,
    is_city_name_report,
    names_no_place,
)
from services.common.companion_response_safety import companion_safety_decision
from services.common.realtime_information import (
    _normalized,
    asks_about_weather,
    mentions_body_discomfort,
    mentions_weather,
)

_DAY_WORDS = ("大后天", "后天", "明天", "今晚", "今天", "现在")
_DEFAULT_DAY = "今天"
_FOLLOWUP_HEAD = re.compile(r"^(?:那么|那|还有)")
_FOLLOWUP_TAIL = re.compile(r"(?:的?天气)?(?:怎么样|咋样|如何)?[的呢呀啊吧嘛哦]*$")


@dataclass(frozen=True, slots=True)
class AskCity:
    pass


@dataclass(frozen=True, slots=True)
class WeatherSearch:
    query: str


WeatherTurn = AskCity | WeatherSearch


@dataclass(frozen=True, slots=True)
class _Read:
    key: str
    turn: WeatherTurn | None


@dataclass(frozen=True, slots=True)
class WeatherThread:
    """``followup_day`` is set only while the previous committed turn was about weather."""

    city: City | None = None
    followup_day: str | None = None
    last: _Read | None = None

    def read(self, text: str) -> WeatherTurn | None:
        """How ``text`` reads as the next turn, or as the turn just committed."""

        if self.last is not None and self.last.key == _normalized(text):
            return self.last.turn
        return _plan(self, text)[0]

    def after(self, text: str) -> WeatherThread:
        turn, city, day = _plan(self, text)
        return WeatherThread(city=city, followup_day=day, last=_Read(_normalized(text), turn))


def _day_in(compact: str) -> str | None:
    return next((word for word in _DAY_WORDS if word in compact), None)


def _search(city: City, day: str) -> WeatherSearch:
    return WeatherSearch(f"{city.standard_name}{day}天气")


def _followup(compact: str) -> tuple[str | None, City | None] | None:
    body = _FOLLOWUP_TAIL.sub("", compact, count=1)
    for candidate in (body, _FOLLOWUP_HEAD.sub("", body, count=1)):
        day = next(
            (w for w in _DAY_WORDS if candidate.startswith(w) or candidate.endswith(w)),
            None,
        )
        rest = candidate
        if day is not None:
            rest = rest.removeprefix(day) if rest.startswith(day) else rest.removesuffix(day)
        if not rest:
            if day is not None:
                return day, None
            continue
        if is_city_name_report(rest):
            return day, find_city(rest)
    return None


def _plan(
    thread: WeatherThread, text: str
) -> tuple[WeatherTurn | None, City | None, str | None]:
    compact = _normalized(text)
    if (
        not compact
        or companion_safety_decision(text) != "none"
        or mentions_body_discomfort(text)
    ):
        return None, thread.city, None
    if mentions_weather(text) and asks_about_weather(text):
        day = _day_in(compact) or _DEFAULT_DAY
        named = find_city(text)
        if named is not None:
            return WeatherSearch(text.strip()), named, day
        if not names_no_place(text):
            # A place outside the allowlist keeps the plain search path.
            return None, thread.city, None
        if thread.city is None:
            return AskCity(), None, day
        return _search(thread.city, day), thread.city, day
    if thread.followup_day is None:
        return None, thread.city, None
    parsed = _followup(compact)
    if parsed is None:
        return None, thread.city, None
    day = parsed[0] or thread.followup_day
    city = parsed[1] or thread.city
    if city is None:
        return AskCity(), None, day
    return _search(city, day), city, day


__all__ = ["AskCity", "WeatherSearch", "WeatherThread", "WeatherTurn"]
