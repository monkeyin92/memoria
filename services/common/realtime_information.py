"""Deterministic clock replies and bounded instructions for live information."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_STRIP = re.compile(r"[\s，,。！？!?；;：:、]")
_SAFE_REPLY = re.compile(
    r"^(?:今天是\d{4}年\d{1,2}月\d{1,2}日，星期[一二三四五六日]。|"
    r"现在是北京时间\d{1,2}点\d{2}分。|"
    r"现在是\d{4}年\d{1,2}月\d{1,2}日，星期[一二三四五六日]，北京时间\d{1,2}点\d{2}分。|"
    r"你想查哪个城市的天气？)$"
)
_WEATHER_WITHOUT_LOCATION = frozenset(
    {
        "天气",
        "今天天气",
        "今天天气怎么样",
        "今天天气如何",
        "今天天气咋样",
        "现在天气",
        "现在天气怎么样",
        "当前天气",
        "外面天气怎么样",
    }
)
_LIVE_MARKERS = (
    "天气",
    "新闻",
    "行情",
    "股价",
    "汇率",
    "政策",
    "航班",
    "高铁",
    "火车",
    "路况",
    "实时",
    "最新",
)


def current_local_time(timezone_name: str = "Asia/Shanghai") -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def _normalized(query: str) -> str:
    return _STRIP.sub("", query).lower()


def _asks_for_date(query: str) -> bool:
    compact = _normalized(query)
    return any(
        marker in compact
        for marker in ("星期几", "周几", "礼拜几", "今天几号", "今天日期", "几月几号")
    )


def _asks_for_time(query: str) -> bool:
    compact = _normalized(query)
    return any(
        marker in compact
        for marker in ("现在几点", "几点了", "当前时间", "现在时间", "现在是几点")
    )


def fixed_realtime_reply(*, query: str, now: datetime) -> str | None:
    """Answer clock facts locally; ask for a city before searching weather."""

    compact = _normalized(query)
    asks_date = _asks_for_date(query)
    asks_time = _asks_for_time(query)
    has_live_topic = any(marker in compact for marker in _LIVE_MARKERS)
    weekday = _WEEKDAYS[now.weekday()]
    if asks_date and asks_time and not has_live_topic:
        return (
            f"现在是{now.year}年{now.month}月{now.day}日，{weekday}，"
            f"北京时间{now.hour}点{now.minute:02d}分。"
        )
    if asks_date and not has_live_topic:
        return f"今天是{now.year}年{now.month}月{now.day}日，{weekday}。"
    if asks_time and not has_live_topic:
        return f"现在是北京时间{now.hour}点{now.minute:02d}分。"
    if compact in _WEATHER_WITHOUT_LOCATION:
        return "你想查哪个城市的天气？"
    return None


def realtime_instruction(*, query: str, now: datetime) -> str | None:
    """Return authoritative per-turn context only when freshness matters."""

    compact = _normalized(query)
    if not (
        _asks_for_date(query)
        or _asks_for_time(query)
        or any(marker in compact for marker in _LIVE_MARKERS)
    ):
        return None
    weekday = _WEEKDAYS[now.weekday()]
    return (
        "【权威实时上下文】"
        f"当前本地时间是 {now:%Y-%m-%d %H:%M} Asia/Shanghai（{weekday}）。"
        "日期、星期和时间必须以此为准，不要自行推算。"
        "天气、新闻、行情、政策、交通等会变化的信息必须先联网查询再回答；"
        "没有明确城市、可靠结果或联网能力时先追问或只说不知道，绝不猜测。"
        "查询只能使用用户本轮公开提供的地点或主题，不得带入私人资料。"
    )


def is_safe_realtime_reply(value: object) -> bool:
    return isinstance(value, str) and _SAFE_REPLY.fullmatch(value) is not None
