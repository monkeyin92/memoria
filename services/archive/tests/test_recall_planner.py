from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from services.archive.memory_domain import PersonItem
from services.archive.recall_planner import RecallPlanner

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 8, 7, 15, 30, tzinfo=_SHANGHAI)


@pytest.mark.parametrize(
    ("query", "expected_start", "expected_end"),
    (
        (
            "昨天聊了什么？",
            datetime(2026, 8, 6, tzinfo=_SHANGHAI),
            datetime(2026, 8, 7, tzinfo=_SHANGHAI),
        ),
        (
            "前天聊了什么？",
            datetime(2026, 8, 5, tzinfo=_SHANGHAI),
            datetime(2026, 8, 6, tzinfo=_SHANGHAI),
        ),
        (
            "三天前聊了什么？",
            datetime(2026, 8, 4, tzinfo=_SHANGHAI),
            datetime(2026, 8, 5, tzinfo=_SHANGHAI),
        ),
        ("最近3天聊了什么？", datetime(2026, 8, 5, tzinfo=_SHANGHAI), _NOW),
        (
            "上周聊了什么？",
            datetime(2026, 7, 27, tzinfo=_SHANGHAI),
            datetime(2026, 8, 3, tzinfo=_SHANGHAI),
        ),
        ("本周聊了什么？", datetime(2026, 8, 3, tzinfo=_SHANGHAI), _NOW),
        (
            "上个月聊了什么？",
            datetime(2026, 7, 1, tzinfo=_SHANGHAI),
            datetime(2026, 8, 1, tzinfo=_SHANGHAI),
        ),
        ("本月聊了什么？", datetime(2026, 8, 1, tzinfo=_SHANGHAI), _NOW),
    ),
)
def test_relative_time_is_planned_as_a_local_calendar_window(
    query: str,
    expected_start: datetime,
    expected_end: datetime,
) -> None:
    plan = RecallPlanner.plan(query=query, now=_NOW)

    assert plan.occurred_after is not None
    assert plan.occurred_before is not None
    assert plan.occurred_after.astimezone(_SHANGHAI) == expected_start
    if expected_end == _NOW:
        assert plan.occurred_before.astimezone(_SHANGHAI) == expected_end
    else:
        assert (
            plan.occurred_before.astimezone(_SHANGHAI) + timedelta(microseconds=1) == expected_end
        )


def test_time_filter_removes_recall_scaffolding_but_keeps_the_topic() -> None:
    assert RecallPlanner.plan(query="昨天我们聊了什么？", now=_NOW).text == ""
    assert RecallPlanner.plan(query="昨天聊到的南京旅行", now=_NOW).text == "聊到的南京旅行"


def test_only_unambiguous_confirmed_person_aliases_become_entity_filters() -> None:
    people = (
        PersonItem(
            person_id="15c1ea15-8465-4cdf-92a8-90ac860c6aac",
            display_name="李梅",
            relationship_to_owner="mother",
            aliases=("妈妈", "母亲", "李梅"),
            status="confirmed",
            source_event_id="owner-mother",
        ),
        PersonItem(
            person_id="7af3f21e-85c8-4a8f-bc90-34363435310f",
            display_name="王强",
            relationship_to_owner="friend",
            aliases=("朋友", "王强"),
            status="confirmed",
            source_event_id="owner-friend",
        ),
        PersonItem(
            person_id="8858b9c0-6a5c-4b80-a02d-4769cc97cc94",
            display_name="小周",
            relationship_to_owner="friend",
            aliases=("朋友", "小周"),
            status="confirmed",
            source_event_id="owner-friend-2",
        ),
        PersonItem(
            person_id="860d5c64-8981-4703-8bca-2356cabbbbd1",
            display_name="老张",
            relationship_to_owner="father",
            aliases=("爸爸", "老张"),
            status="candidate",
            source_event_id="candidate-father",
        ),
    )

    mother = RecallPlanner.plan(query="前天妈妈提到的旅行", now=_NOW, people=people)
    ambiguous_friend = RecallPlanner.plan(query="朋友最近怎么样", now=_NOW, people=people)
    candidate = RecallPlanner.plan(query="爸爸前天说了什么", now=_NOW, people=people)

    assert mother.entity_ids == ("15c1ea15-8465-4cdf-92a8-90ac860c6aac",)
    assert mother.text == "提到的旅行"
    assert ambiguous_friend.entity_ids == ()
    assert candidate.entity_ids == ()


def test_ambiguous_or_invalid_time_language_does_not_guess_a_window() -> None:
    plan = RecallPlanner.plan(query="昨天还是前天聊的？", now=_NOW)
    assert plan.occurred_after is None
    assert plan.occurred_before is None

    with pytest.raises(ValueError, match="timezone-aware"):
        RecallPlanner.plan(query="昨天", now=datetime(2026, 8, 7, 15, 30))
