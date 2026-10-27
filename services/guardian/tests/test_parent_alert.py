from __future__ import annotations

import dataclasses
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from zoneinfo import ZoneInfo

import pytest
from services.common.fixed_safety_scripts import FixedSafetyTemplateId
from services.common.llm_failover import PRODUCT_FORBIDDEN_COPY
from services.guardian.parent_alert import (
    PARENT_ALERT_COPY,
    ParentAlertCopy,
    alert_time_label,
    interrupted_parent_detail,
    parse_alert_template,
    playback_left_unfinished,
)

ROOT = Path(__file__).resolve().parents[3]
SHANGHAI = ZoneInfo("Asia/Shanghai")
ROBOT_SAID = "机器人已提醒孩子去找身边的大人"
ROBOT_SAID_CRISIS = "机器人已安抚孩子情绪并在旁陪伴"


def _strings(copy: ParentAlertCopy) -> list[str]:
    return [str(value) for value in dataclasses.astuple(copy)]


def test_every_fixed_safety_script_has_the_approved_parent_copy() -> None:
    assert set(PARENT_ALERT_COPY) == set(get_args(FixedSafetyTemplateId))
    assert PARENT_ALERT_COPY["crisis-self-v1"] == ParentAlertCopy(
        category_label="情绪和安全",
        summary="孩子说到了和自己情绪、安全有关的内容",
        robot_said=ROBOT_SAID_CRISIS,
        parent_action="找个安静的时间陪孩子聊聊，先听孩子说，不急着讲道理。如需帮助可拨打心理援助热线12356",
        interrupted_detail="这次的安抚话术孩子没有听完，如需帮助可拨打心理援助热线12356",
    )
    assert PARENT_ALERT_COPY["victim-support-v1"] == ParentAlertCopy(
        category_label="人身安全",
        summary="孩子说到了自己可能受到伤害",
        robot_said=ROBOT_SAID,
        parent_action="尽快当面陪孩子聊聊，先让孩子安心，不反复追问细节。如有危险请拨打110",
        interrupted_detail="这次的提醒话术孩子没有听完，如有危险请拨打110",
    )
    assert PARENT_ALERT_COPY["support-other-v1"] == ParentAlertCopy(
        category_label="孩子提到的安全话题",
        summary="孩子提到了别人的安全，或在问怎么预防",
        robot_said=ROBOT_SAID,
        parent_action="可以找时间跟孩子聊聊这个话题，听听孩子是怎么想的",
    )


def test_summaries_fit_one_line_and_parent_copy_avoids_banned_words() -> None:
    for template_id, copy in PARENT_ALERT_COPY.items():
        assert len(copy.summary) <= 40, template_id
        for text in _strings(copy):
            assert not [word for word in PRODUCT_FORBIDDEN_COPY if word in text], (template_id, text)


def test_hotline_numbers_appear_only_in_their_own_tier_action() -> None:
    hits = {
        (template_id, field.name, number)
        for template_id, copy in PARENT_ALERT_COPY.items()
        for field in dataclasses.fields(copy)
        for number in ("12356", "110")
        if isinstance(getattr(copy, field.name), str) and number in getattr(copy, field.name)
    }
    assert hits == {
        ("crisis-self-v1", "parent_action", "12356"),
        ("crisis-self-v1", "interrupted_detail", "12356"),
        ("victim-support-v1", "parent_action", "110"),
        ("victim-support-v1", "interrupted_detail", "110"),
    }


def test_banned_words_match_the_miniprogram_copy_check() -> None:
    script = (ROOT / "apps/miniprogram/scripts/check-forbidden-ui-copy.js").read_text(encoding="utf-8")
    match = re.search(r"const FORBIDDEN = (\[[^\]]*\]);", script)
    assert match is not None
    assert set(json.loads(match.group(1))) == PRODUCT_FORBIDDEN_COPY


@pytest.mark.parametrize("template_id", get_args(FixedSafetyTemplateId))
def test_a_known_script_id_is_its_own_tier(template_id: str) -> None:
    assert parse_alert_template(template_id) == template_id


@pytest.mark.parametrize(
    "stored",
    ("legacy-unspecified", "crisis-support-reply", "crisis-transfer-draft-v2", ""),
)
def test_an_unknown_script_id_stays_unknown_and_logs_only_the_id(
    stored: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="services.guardian.parent_alert"):
        assert parse_alert_template(stored) is None
    assert [record.args for record in caplog.records if record.levelno >= logging.ERROR] == [
        (stored,)
    ]
    assert "12356" not in caplog.text
    assert "110" not in caplog.text
    assert "不想活" not in caplog.text
    assert "打我" not in caplog.text


def test_an_unfinished_playback_uses_the_tier_sentence() -> None:
    assert playback_left_unfinished(terminals=("preempted",), playback_ended=False) is True
    assert playback_left_unfinished(terminals=("no_audio",), playback_ended=False) is True
    assert playback_left_unfinished(terminals=(), playback_ended=False) is False
    assert playback_left_unfinished(terminals=("playback_ended",), playback_ended=True) is False
    assert playback_left_unfinished(terminals=("preempted",), playback_ended=True) is False
    assert PARENT_ALERT_COPY["support-other-v1"].interrupted_detail is None
    assert (
        PARENT_ALERT_COPY["crisis-self-v1"].interrupted_detail
        == "这次的安抚话术孩子没有听完，如需帮助可拨打心理援助热线12356"
    )
    assert (
        PARENT_ALERT_COPY["victim-support-v1"].interrupted_detail
        == "这次的提醒话术孩子没有听完，如有危险请拨打110"
    )


def test_victim_action_is_the_final_sentence_and_does_not_describe_an_incident() -> None:
    victim = PARENT_ALERT_COPY["victim-support-v1"]
    assert (
        victim.parent_action
        == "尽快当面陪孩子聊聊，先让孩子安心，不反复追问细节。如有危险请拨打110"
    )
    assert "发生了什么" not in victim.parent_action
    assert victim.summary == "孩子说到了自己可能受到伤害"
    for copy in PARENT_ALERT_COPY.values():
        blob = " ".join(_strings(copy))
        assert "打我" not in blob
        assert "不想活" not in blob


def test_interrupted_detail_uses_the_tier_table_only_when_the_flag_is_set() -> None:
    unfinished_self = "这次的安抚话术孩子没有听完，如需帮助可拨打心理援助热线12356"
    unfinished_victim = "这次的提醒话术孩子没有听完，如有危险请拨打110"
    assert (
        interrupted_parent_detail("victim-support-v1", script_interrupted=False) is None
    )
    assert interrupted_parent_detail("crisis-self-v1", script_interrupted=False) is None
    assert (
        interrupted_parent_detail("victim-support-v1", script_interrupted=True)
        == unfinished_victim
    )
    assert (
        interrupted_parent_detail("crisis-self-v1", script_interrupted=True) == unfinished_self
    )
    assert interrupted_parent_detail("support-other-v1", script_interrupted=True) is None
    assert interrupted_parent_detail("support-other-v1", script_interrupted=False) is None
    assert interrupted_parent_detail("legacy-unspecified", script_interrupted=True) is None
    assert interrupted_parent_detail("", script_interrupted=True) is None
    assert interrupted_parent_detail(None, script_interrupted=True) is None
    heard = (
        PARENT_ALERT_COPY["victim-support-v1"].parent_action,
        unfinished_victim,
        PARENT_ALERT_COPY["crisis-self-v1"].parent_action,
        unfinished_self,
        PARENT_ALERT_COPY["support-other-v1"].parent_action,
    )
    for sentence in heard:
        assert "我不会做题。我不想活了" not in sentence
        assert "停" not in sentence
        assert "自杀" not in sentence


def test_alert_time_is_month_day_and_clock_in_the_server_zone() -> None:
    assert alert_time_label(datetime(2026, 10, 16, 11, 42, tzinfo=UTC), SHANGHAI) == "10月16日 19:42"
    assert alert_time_label(datetime(2026, 10, 4, 23, 5, tzinfo=UTC), SHANGHAI) == "10月5日 07:05"
    assert "今天" not in alert_time_label(datetime.now(UTC), SHANGHAI)
