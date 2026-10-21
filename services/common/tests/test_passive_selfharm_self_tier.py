"""Passive self-harm wording belongs to the self tier, not relay or none."""

from __future__ import annotations

import logging

import pytest
from services.common.companion_response_safety import (
    companion_safety_decision,
    fixed_companion_reply,
)
from services.common.crisis_policy import (
    fixed_safety_reply_log_fields,
    log_fixed_safety_reply,
    route_crisis,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT

_PASSIVE_SELF_HARM = (
    "我觉得活着一点意思都没有，大家没有我会更好",
    "活着没意思",
    "我不想活了",
    "我要是消失就好了",
    "我消失就好了",
    "我想死",
    "要是我没有生出来就好了",
    "我想睡着了就不醒过来",
    "没有人需要我，我不在也没关系",
)

_NOT_CRISIS = (
    "这个游戏一点意思都没有",
    "没有我你们也能玩",
    "笑死我了",
    "我累死了，不想写作业",
    "游戏里我又死了",
    "我的小鱼死了，我好难过",
    "我不在家没关系",
    "明天我不在学校也没事",
)


@pytest.mark.parametrize("query", _PASSIVE_SELF_HARM)
def test_passive_self_harm_is_the_self_tier(query: str) -> None:
    assert companion_safety_decision(query) == "crisis_self"
    route = route_crisis(query)

    assert route.action == "crisis_support"
    assert route.notify_guardian is True
    assert route.attempt_push is True
    assert route.evidence_source == "deterministic"
    assert route.script is CRISIS_SELF_SCRIPT
    assert route.script.template_id == "crisis-self-v1"
    assert fixed_companion_reply(query=query, is_companion=True) == CRISIS_SELF_SCRIPT.display


@pytest.mark.parametrize("query", _NOT_CRISIS)
def test_ordinary_death_and_meaninglessness_stay_out_of_the_self_tier(query: str) -> None:
    assert companion_safety_decision(query) != "crisis_self"
    route = route_crisis(query)

    assert route.script != CRISIS_SELF_SCRIPT
    assert route.script is None or route.script.template_id != "crisis-self-v1"


def test_passive_self_harm_log_records_only_the_tier_and_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = "我要是消失就好了"
    route = route_crisis(query)
    logger = logging.getLogger("test.passive_self_harm_reply")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_fixed_safety_reply(logger, route)

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    fields = fixed_safety_reply_log_fields(route)
    assert fields is not None
    assert fields.keys() == {"reply_template_id", "at"}
    assert fields["reply_template_id"] == "crisis-self-v1"
    assert query not in message
    for phrase in CRISIS_SELF_SCRIPT.phrases:
        assert phrase.display not in message
        assert phrase.spoken not in message
