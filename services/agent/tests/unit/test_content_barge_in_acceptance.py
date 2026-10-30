"""Questions heard over a playing reply, against the backchannel forms that must not stop it.

Field 2026-10-29 soak (t017/t018): the uplink carried a whole new question and a lone
「嗯。」 while a reply played; both were refused as ``not_stop_word`` and the reply played on.
"""

from __future__ import annotations

import pytest
from services.agent.src.voice_core.media_session_playback_stop import content_barge_in_wanted

STOP_QUESTION = "等一下，我想问另一个问题，云是怎么变出来的？"


@pytest.mark.parametrize(
    "text",
    [
        STOP_QUESTION,
        "那我明天出门要带伞吗",
        "云是怎么变出来的",
        "我想问一个问题",
        "不要讲了",
        "为什么天是蓝的",
        "帮我讲个小恐龙的故事",
    ],
)
def test_words_that_ask_or_command_stop_the_reply(text: str) -> None:
    assert content_barge_in_wanted(text), text


@pytest.mark.parametrize(
    "text",
    [
        "嗯。",
        "啊",
        "哦，",
        "对",
        "好。",
        "对对对",
        "好的",
        "嗯嗯",
        "行的",
        "好啊。",
        "  嗯  。",
    ],
)
def test_a_backchannel_alone_never_stops_the_reply(text: str) -> None:
    assert not content_barge_in_wanted(text), text


@pytest.mark.parametrize("text", ["云", "好看", "嗯，好", "对呀好"])
def test_leftover_words_under_four_without_a_question_never_stop(text: str) -> None:
    assert not content_barge_in_wanted(text), text


def test_a_backchannel_prefix_still_leaves_the_question_behind_it() -> None:
    assert content_barge_in_wanted("嗯，云是怎么变出来的？")
    assert content_barge_in_wanted("好的，那我问问别的事")


@pytest.mark.parametrize("text", ["不要停", "别停啊", "继续讲这个故事", "接着说"])
def test_a_request_to_keep_talking_never_stops_the_reply(text: str) -> None:
    assert not content_barge_in_wanted(text), text
