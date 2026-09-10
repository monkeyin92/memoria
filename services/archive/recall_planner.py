"""Deterministic time and entity filters for owner-memory recall."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from services.archive.memory_domain import PersonItem

_NUMBER = r"(?:\d{1,3}|[一二三四五六七八九十两]{1,3})"
_DAYS_AGO = re.compile(rf"(?P<number>{_NUMBER})天前")
_RECENT_DAYS = re.compile(rf"(?:最近|近|过去)(?P<number>{_NUMBER})天")
_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_RECALL_SCAFFOLDING = (
    "我们聊了什么",
    "我们聊过什么",
    "聊了些什么",
    "聊了什么",
    "聊过什么",
    "说过什么",
    "提过什么",
    "还记得",
    "你记得",
    "内容是什么",
    "的内容",
    "的事情",
    "之前",
    "当时",
)
_ALIAS_MASKS = ("小朋友",)
_CHILD_QUERY_MARKERS = ("小朋友", "小孩子", "孩子")
_CHILD_RELATIONS = frozenset({"son", "daughter"})
_CHILD_LEXEMES = ("儿子", "女儿")
_DISTRESS_QUERY_MARKERS = ("难受", "不开心", "伤心", "委屈")
_DISTRESS_LEXEMES = ("难过",)
_MAX_QUERY_EXPANSIONS = 8


@dataclass(frozen=True, slots=True)
class RecallPlan:
    text: str
    entity_ids: tuple[str, ...] = ()
    occurred_after: datetime | None = None
    occurred_before: datetime | None = None


def _number(value: str) -> int | None:
    if value.isdigit():
        number = int(value)
    elif "十" in value:
        left, right = value.split("十", 1)
        tens = _DIGITS.get(left, 1) if left else 1
        ones = _DIGITS.get(right, 0) if right else 0
        number = tens * 10 + ones
    else:
        number = _DIGITS.get(value, 0)
    return number if 1 <= number <= 365 else None


def _day_start(value: datetime, days: int = 0) -> datetime:
    day = value.date() + timedelta(days=days)
    return datetime.combine(day, time.min, tzinfo=value.tzinfo)


def _month_start(value: datetime, months: int = 0) -> datetime:
    month_index = value.year * 12 + value.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    return datetime(year, zero_based_month + 1, 1, tzinfo=value.tzinfo)


def _utc_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    return start.astimezone(UTC), end.astimezone(UTC)


def _time_window(
    query: str, now: datetime
) -> tuple[datetime | None, datetime | None, tuple[str, ...]]:
    candidates: list[tuple[datetime, datetime, str]] = []
    for match in _DAYS_AGO.finditer(query):
        days = _number(match.group("number"))
        if days is not None:
            start = _day_start(now, -days)
            candidates.append(
                (
                    *_utc_window(start, _day_start(now, 1 - days) - timedelta(microseconds=1)),
                    match.group(0),
                )
            )
    for match in _RECENT_DAYS.finditer(query):
        days = _number(match.group("number"))
        if days is not None:
            candidates.append((*_utc_window(_day_start(now, 1 - days), now), match.group(0)))

    fixed: tuple[tuple[tuple[str, ...], datetime, datetime], ...] = (
        (("前天",), _day_start(now, -2), _day_start(now, -1) - timedelta(microseconds=1)),
        (("昨天",), _day_start(now, -1), _day_start(now) - timedelta(microseconds=1)),
        (("今天",), _day_start(now), now),
        (
            ("上周", "上星期", "上礼拜"),
            _day_start(now, -now.weekday() - 7),
            _day_start(now, -now.weekday()) - timedelta(microseconds=1),
        ),
        (("本周", "这周", "这星期"), _day_start(now, -now.weekday()), now),
        (("上月", "上个月"), _month_start(now, -1), _month_start(now) - timedelta(microseconds=1)),
        (("本月", "这个月"), _month_start(now), now),
    )
    for aliases, start, end in fixed:
        for alias in aliases:
            if alias in query:
                candidates.append((*_utc_window(start, end), alias))
                break
    distinct = {(start, end) for start, end, _ in candidates}
    if len(distinct) != 1:
        return None, None, ()
    start, end = next(iter(distinct))
    return start, end, tuple(item for _, _, item in candidates)


def _alias_haystack(query: str, alias: str) -> str:
    masked = query
    for compound in _ALIAS_MASKS:
        if alias != compound and alias in compound:
            masked = masked.replace(compound, "\u3000" * len(compound))
    return masked


def _alias_matches(query: str, alias: str) -> bool:
    haystack = _alias_haystack(query, alias)
    if alias.isascii() and alias.replace(" ", "").isalnum():
        return (
            re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", haystack, re.I)
            is not None
        )
    return alias in haystack


def _entities(query: str, people: Sequence[PersonItem]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    by_alias: dict[str, set[str]] = defaultdict(set)
    for person in people:
        if person.status != "confirmed":
            continue
        for raw_alias in (person.display_name, *person.aliases):
            alias = raw_alias.strip()
            if len(alias) >= 2:
                by_alias[alias].add(person.person_id)
    entity_ids: set[str] = set()
    matched_aliases: list[str] = []
    for alias in sorted(by_alias, key=len, reverse=True):
        ids = by_alias[alias]
        if len(ids) == 1 and _alias_matches(query, alias):
            entity_ids.update(ids)
            matched_aliases.append(alias)
    return tuple(sorted(entity_ids)), tuple(matched_aliases)


def _query_expansions(query: str, people: Sequence[PersonItem]) -> tuple[str, ...]:
    extra: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        token = value.strip()
        if len(token) < 2 or token in seen or token in query:
            return
        seen.add(token)
        extra.append(token)

    if any(marker in query for marker in _CHILD_QUERY_MARKERS):
        for lexeme in _CHILD_LEXEMES:
            add(lexeme)
        for person in people:
            if person.status != "confirmed" or person.relationship_to_owner not in _CHILD_RELATIONS:
                continue
            add(person.display_name)
            for alias in person.aliases:
                add(alias)
    if any(marker in query for marker in _DISTRESS_QUERY_MARKERS):
        for lexeme in _DISTRESS_LEXEMES:
            add(lexeme)
    return tuple(extra[:_MAX_QUERY_EXPANSIONS])


def _clean_query(query: str, controls: Sequence[str]) -> str:
    cleaned = query
    for value in sorted(set(controls), key=len, reverse=True):
        cleaned = re.sub(re.escape(value), " ", cleaned, flags=re.I)
    for phrase in _RECALL_SCAFFOLDING:
        cleaned = cleaned.replace(phrase, " ")
    cleaned = re.sub(r"[\s，。！？、,.!?：:；;]+", " ", cleaned).strip()
    return "" if cleaned in {"", "我们", "什么", "是什么", "吗", "呢"} else cleaned


class RecallPlanner:
    @staticmethod
    def plan(*, query: str, now: datetime, people: Sequence[PersonItem] = ()) -> RecallPlan:
        if now.tzinfo is None:
            raise ValueError("recall planning requires a timezone-aware clock")
        text = query.strip()
        occurred_after, occurred_before, time_controls = _time_window(text, now)
        entity_ids, entity_controls = _entities(text, people)
        controls = (*time_controls, *entity_controls)
        planned_text = _clean_query(text, controls) if controls else text
        expansions = _query_expansions(text, people)
        if expansions:
            planned_text = " ".join(part for part in (planned_text, *expansions) if part).strip()
        return RecallPlan(
            text=planned_text,
            entity_ids=entity_ids,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
        )
