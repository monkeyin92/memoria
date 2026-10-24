"""Deterministic clock replies and bounded instructions for live information."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from services.common.companion_response_safety import companion_safety_decision

ASK_WEATHER_CITY_REPLY = "你想查哪个城市呀？"
# Product copy weather-fallback-v1, 2026-10-16. Tests pin this string.
WEATHER_LOOKUP_UNAVAILABLE_REPLY = "我现在还查不到天气，可以请爸爸妈妈帮你看看窗外或者手机哦。"
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_STRIP = re.compile(r"[\s，,。！？!?；;：:、]")
_SAFE_REPLY = re.compile(
    r"^(?:今天是\d{4}年\d{1,2}月\d{1,2}日，星期[一二三四五六日]。|"
    r"现在是北京时间\d{1,2}点\d{2}分。|"
    r"现在是\d{4}年\d{1,2}月\d{1,2}日，星期[一二三四五六日]，北京时间\d{1,2}点\d{2}分。|"
    + re.escape(ASK_WEATHER_CITY_REPLY)
    + r"|"
    + re.escape(WEATHER_LOOKUP_UNAVAILABLE_REPLY)
    + r")$"
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
_WEATHER_TOPIC_MARKERS = (
    "天气",
    "下雨",
    "下雪",
    "降雨",
    "降雪",
    "气温",
    "温度",
    "冷不冷",
    "热不热",
    "穿外套",
    "带伞",
    "刮风",
    "风力",
    "紫外线",
    "雾霾",
    "寒潮",
    "台风",
    "冰雹",
    "霜冻",
)
# Body complaints reuse weather words ("温度", "冷", "热") but are never a
# weather question.  Crisis support and this check run before the fallback.
_BODY_DISCOMFORT_MARKERS = (
    "发烧",
    "发热",
    "体温",
    "温度好高",
    "不舒服",
    "难受",
    "生病",
    "咳嗽",
    "头晕",
    "头疼",
    "头痛",
    "肚子疼",
    "肚子痛",
    "想吐",
    "恶心",
    "拉肚子",
    "没力气",
    "乏力",
    "浑身",
    "发抖",
    "打冷战",
)
_BODY_DISCOMFORT_SELF = re.compile(
    r"我(?:现在|今天|感觉|觉得|有点|有些|好|很|特别|超级|浑身|全身)*(?:冷|热|疼|痛)"
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
REALTIME_UNAVAILABLE_REPLY = "我暂时没查到实时结果，稍后再试一次。"
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
    "不知道",
    "不清楚",
    "不确定",
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
        marker in compact for marker in ("现在几点", "几点了", "当前时间", "现在时间", "现在是几点")
    )


def is_fuzzy_weekday_query(query: str) -> bool:
    compact = query.strip().replace(" ", "").replace("　", "")
    if any(
        marker in compact
        for marker in ("星期几", "周几", "礼拜几", "今天几号", "今天日期", "几月几号")
    ):
        return True
    return "几" in compact and any(
        marker in compact for marker in ("星期", "周几", "礼拜", "星", "周")
    )


def mentions_weather(query: str) -> bool:
    return "天气" in _normalized(query)


def mentions_body_discomfort(query: str) -> bool:
    """Whether the turn describes the speaker's body rather than the weather."""

    compact = _normalized(query)
    return bool(compact) and (
        any(marker in compact for marker in _BODY_DISCOMFORT_MARKERS)
        or _BODY_DISCOMFORT_SELF.search(compact) is not None
    )


def asks_about_weather(query: str) -> bool:
    """Whether the turn is a weather question, not only a literal 天气 mention."""

    compact = _normalized(query)
    if not compact or companion_safety_decision(query) != "none":
        return False
    if mentions_body_discomfort(query):
        return False
    return any(marker in compact for marker in _WEATHER_TOPIC_MARKERS)


def weekday_lookup_query(query: str) -> str:
    """Keep a weather mention intact when a fuzzy weekday shares the turn."""

    if is_fuzzy_weekday_query(query) and not mentions_weather(query):
        return "今天星期几"
    return query


def weather_lookup_configured(settings: object) -> bool:
    """True when Qwen live search or a configured domestic weather source can answer."""

    if bool(getattr(settings, "live_search_qwen_enabled", False)):
        return True
    provider = str(getattr(settings, "weather_lookup_provider", "off") or "off")
    if provider != "domestic":
        return False
    raw_key = getattr(settings, "qweather_api_key", "")
    if hasattr(raw_key, "get_secret_value"):
        key = str(raw_key.get_secret_value())
    else:
        key = str(raw_key or "")
    host = str(getattr(settings, "qweather_api_host", "") or "")
    return bool(key.strip() and host.strip())


def fixed_realtime_reply(
    *,
    query: str,
    now: datetime,
    weather_lookup_available: bool = True,
    city_followup: bool = False,
) -> str | None:
    """Answer clock facts locally.

    Ask for a city only when some weather source can use the answer.
    With no source, a weather question and the following city report use the
    fixed unavailable line.
    """

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
    if not weather_lookup_available and (asks_about_weather(query) or city_followup):
        # Crisis support and body discomfort outrank the weather fallback: a
        # child saying "我发烧了温度好高" or "我好冷" must not hear it.
        if companion_safety_decision(query) != "none" or mentions_body_discomfort(query):
            return None
        return WEATHER_LOOKUP_UNAVAILABLE_REPLY
    if compact in _WEATHER_WITHOUT_LOCATION:
        return ASK_WEATHER_CITY_REPLY
    return None


def realtime_unavailable_reply(query: str, *, now: datetime | None = None) -> str:
    """The product fallback for a public live lookup that produced no answer."""

    fixed = fixed_realtime_reply(
        query=query,
        now=now or current_local_time(),
        weather_lookup_available=False,
    )
    return fixed or REALTIME_UNAVAILABLE_REPLY


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
    return (
        bool(compact)
        and companion_safety_decision(query) == "none"
        and any(marker in compact for marker in _LIVE_MARKERS)
    )


def is_realtime_followup_nudge(query: str) -> bool:
    """Recognize short prompts that may resume a same-scope live request."""

    return _normalized(query) in _REALTIME_FOLLOWUP_NUDGES


def is_incomplete_realtime_reply(reply: str, *, query: str = "") -> bool:
    """Recognize a bridge, generic nudge, or explicit search failure as non-final."""

    if is_safe_realtime_reply(reply):
        return False
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
