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
_REALTIME_FOLLOWUP_NUDGES = frozenset(
    {
        "人呢",
        "你人呢",
        "还在吗",
        "在吗",
        "查到了吗",
        "查好了吗",
        "结果呢",
        "有结果了吗",
        "怎么样了",
        "继续",
        "你能帮我查吗",
        "你不能帮我查吗",
        "能帮我查吗",
        "帮我查一下",
    }
)
_DEFERRED_REPLY_MARKERS = (
    "查一下",
    "查一查",
    "查查看",
    "查了一下",
    "查询",
    "看看",
)
_WAITING_REPLY_MARKERS = ("稍等", "等一下", "等等", "一会儿", "稍后")
_FAILURE_REPLY_MARKERS = (
    "联网失败",
    "无法联网",
    "暂时无法",
    "暂时不能",
    "暂时不清楚",
    "不能查询",
    "无法查询",
    "不能直接查",
    "无法直接查",
    "没办法直接查",
    "不具备查询能力",
    "没有结果",
    "没查到",
    "查不到",
    "拿不到",
    "出错",
    "错误",
    "失败",
)
_GENERIC_REALTIME_REPLY = re.compile(
    r"^(?:(?:你是在问谁呀[呢吗嘛]?|我在(?:这儿|这里|呢)(?:陪着你)?[呢呀啊]?|"
    r"怎么了[呢吗嘛]?|有什么事[呢吗嘛]?))+[～…]*$"
)
_BRIDGE_FILLER = re.compile(r"^[嗯好可以我先需要让帮请一下吧哦呀呢～]+$")
_DEFERRED_PREFIX = re.compile(
    r"^\s*(?:嗯[，,]?|好[，,]?|可以[，,]?)?"
    r"(?:我(?:先|需要)?(?:查一下|查一查|查查看|看看)|(?:让我|我先)看看)"
    r"(?:吧|哦|呀)?[。！？!?]+\s*"
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


def requires_realtime_lookup(query: str) -> bool:
    """Whether this turn needs a fresh result rather than a local clock reply."""

    compact = _normalized(query)
    return bool(compact) and any(marker in compact for marker in _LIVE_MARKERS)


def is_realtime_followup_nudge(query: str) -> bool:
    """Recognize short prompts that may resume a same-scope live request."""

    return _normalized(query) in _REALTIME_FOLLOWUP_NUDGES


def is_incomplete_realtime_reply(reply: str, *, query: str = "") -> bool:
    """Recognize a bridge, generic nudge, or explicit search failure as non-final."""

    compact = _normalized(reply)
    if query:
        compact = compact.replace(_normalized(query), "")
    if not compact:
        return True
    if any(marker in compact for marker in _FAILURE_REPLY_MARKERS):
        return True
    if _GENERIC_REALTIME_REPLY.fullmatch(compact) is not None:
        return True
    remaining = compact
    matched_bridge_marker = False
    for marker in (*_DEFERRED_REPLY_MARKERS, *_WAITING_REPLY_MARKERS):
        if marker in remaining:
            matched_bridge_marker = True
            remaining = remaining.replace(marker, "")
    return matched_bridge_marker and (
        not remaining or _BRIDGE_FILLER.fullmatch(remaining) is not None
    )


def strip_realtime_bridge_prefix(reply: str) -> str:
    """Remove a standalone "I will check" sentence before a real answer."""

    return _DEFERRED_PREFIX.sub("", reply).strip()


def is_safe_realtime_reply(value: object) -> bool:
    return isinstance(value, str) and _SAFE_REPLY.fullmatch(value) is not None
