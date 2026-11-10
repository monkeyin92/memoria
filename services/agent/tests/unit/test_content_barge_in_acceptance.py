"""Questions heard over a playing reply, against the backchannel forms that must not stop it.

Field 2026-10-29 soak (t017/t018): the uplink carried a whole new question and a lone
「嗯。」 while a reply played; both were refused as ``not_stop_word`` and the reply played on.
"""

from __future__ import annotations

import pytest
from services.agent.src.voice_core.playback_text import (
    content_barge_in_wanted,
    playback_text_is_echo,
)

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


def test_playback_echo_with_trailing_backchannel_is_not_content_barge_in() -> None:
    reply = "从前有一座山山里有一座庙"

    assert not content_barge_in_wanted(
        "从前有一座山山里有一座庙，嗯。",
        playback_text=reply,
    )


def test_non_exact_playback_echo_with_trailing_backchannel_is_not_content_barge_in() -> None:
    reply = "明天早上我们一起去公园散步吧"

    assert not content_barge_in_wanted(
        "明天早上我们一起去公园散步啦，嗯。",
        playback_text=reply,
    )


def test_short_candidate_from_long_reply_is_echo() -> None:
    reply = "今天的故事里小兔子在森林里唱歌遇见了狐狸，还一起回家吃晚饭。故事最后大家都睡着了，森林恢复了安静的夜晚，月亮升起来了。"
    candidate = reply[:16]

    assert sum(character.isalnum() for character in candidate) == 16
    assert sum(character.isalnum() for character in reply) == 54
    assert playback_text_is_echo(candidate, reply) is True
    assert content_barge_in_wanted(candidate, playback_text=reply) is False


def test_short_candidate_unrelated_to_long_reply_is_content_barge_in() -> None:
    candidate = "小熊今天想去河边看星星呀回家睡觉"
    reply = "今天的故事里小兔子在森林里唱歌遇见了狐狸，还一起回家吃晚饭。故事最后大家都睡着了，森林恢复了安静的夜晚，月亮升起来了。"

    assert sum(character.isalnum() for character in candidate) == 16
    assert sum(character.isalnum() for character in reply) == 54
    assert playback_text_is_echo(candidate, reply) is False
    assert content_barge_in_wanted(candidate, playback_text=reply) is True


def test_near_copy_question_is_content_barge_in() -> None:
    candidate = "那我们明天还去公园吗"
    reply = "那我们明天还去公园吧"

    assert playback_text_is_echo(candidate, reply) is False
    assert content_barge_in_wanted(candidate, playback_text=reply) is True


def test_question_punctuation_survives_echo_guard() -> None:
    candidate = "那我们明天还去公园走走？"
    reply = "那我们明天还去公园散步吧"

    assert content_barge_in_wanted(candidate, playback_text=reply) is True


def test_real_question_after_backchannel_still_is_content_barge_in() -> None:
    assert content_barge_in_wanted(
        "嗯，等一下，我想问云是怎么变出来的？",
        playback_text="从前有一座山山里有一座庙",
    )


@pytest.mark.parametrize("text", ["不要停", "别停啊", "继续讲这个故事", "接着说"])
def test_a_request_to_keep_talking_never_stops_the_reply(text: str) -> None:
    assert not content_barge_in_wanted(text), text
