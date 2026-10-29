"""停止词 40 句（真机测试句 v2 第三节）：正例 20 句立即静音，反例 20 句不是停止词。

Field 2026-10-21 bridge bf245ed1 / session 74cdaf88: a single 「停」 heard during a story was
finalized as ``not_stop_word`` or refused as ``endpoint_already_pinned``.  The forms FunASR
produces around it -- punctuation, the reply's acknowledgment behind it, the wake name in
front, a modal particle at the end -- must still be the stop, while a stop character inside
content (「停车」「公交车停了」) never is.  These pin the product list at the predicate, then
drive the device runtime: a positive final under a playing story stops it and leaves the
session listening; a negative form that is not content keeps the story playing, while one
that is a question or four characters long stops it by the content barge-in rule.
"""

from __future__ import annotations

import pytest
from services.agent.src.conversation_close_router import lexical_playback_control_only
from services.agent.src.orchestration.interruption_guard import (
    interrupt_ack_phrase,
    is_interrupt_command_only,
    is_stop_command_only,
)
from services.agent.tests.unit.media_session_support import _user_turn_texts
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _run_story,
    _StoryHarness,
)

POSITIVE: dict[str, str] = {
    "S1": "停",
    "S2": "停下",
    "S3": "停停停",
    "S4": "暂停",
    "S5": "别说了",
    "S6": "不要说了",
    "S7": "闭嘴",
    "S8": "嘘",
    "S9": "安静",
    "S10": "stop",
    "S11": "停一下",
    "S12": "先停",
    "S13": "别讲了",
    "S14": "不要讲了",
    "S15": "你别说了",
    "S16": "停下来",
    "S17": "不听了",
    "S18": "我不想听了",
    "S19": "好了别说了",
    "S20": "嘘嘘",
}

NEGATIVE: dict[str, str] = {
    "N1": "停车",
    "N2": "停电了",
    "N3": "我不想停",
    "N4": "不要停",
    "N5": "我想听故事",
    "N6": "等等",
    "N7": "够了",
    "N8": "好了知道了",
    "N9": "暂停键在哪里",
    "N10": "公交车停了",
    "N11": "雨停了吗",
    "N12": "停车场在哪",
    "N13": "你讲的停好听",
    "N14": "我说完了",
    "N15": "再讲一个",
    "N16": "安静的小兔子",
    "N17": "别走",
    "N18": "不要关灯",
    "N19": "我闭上眼睛了",
    "N20": "嘘是什么意思",
}

# 产品 v2 口径：正例全静音、反例全不静音。N6、N7、N8 是完成/等待确认：路由成控制轮并
# 固定回一句（N8 「好了知道了」与 v1 的「好了，知道了」同一句，按该口径），不按停止词静音。
# 产品原句为 N6「等等（回「嗯，你说。」）」N7「够了（回「好的。」）」，回话由句内注明。
_WAIT_AND_CLOSE_ACK = {"N6", "N7", "N8"}


@pytest.mark.parametrize("sentence_id", sorted(POSITIVE))
@pytest.mark.parametrize("suffix", ["", "。"])
def test_positive_stop_sentence_is_a_playback_control(sentence_id: str, suffix: str) -> None:
    text = POSITIVE[sentence_id] + suffix
    assert is_interrupt_command_only(text), text
    assert lexical_playback_control_only(text), text
    # A stop gets no reply that talks: in media voice a stop never produces a turn.
    expected_ack = "嗯，你说。" if sentence_id == "S12" else "好的。"  # as before 先停
    assert interrupt_ack_phrase(text) in {None, expected_ack}, text


@pytest.mark.parametrize("sentence_id", sorted(set(NEGATIVE) - _WAIT_AND_CLOSE_ACK))
@pytest.mark.parametrize("suffix", ["", "。"])
def test_negative_sentence_is_not_a_playback_control(sentence_id: str, suffix: str) -> None:
    text = NEGATIVE[sentence_id] + suffix
    assert not lexical_playback_control_only(text), text


# Field 2026-10-21 (bridge bf245ed1, session 74cdaf88): FunASR finalized the
# stop with punctuation, with her own acknowledgment behind it, with the wake
# name in front, and once as a run the filler list had to peel.  Each is still
# the stop word; the counterexamples below (car, power cut, negated) never are.
_MISHEARD_STOP_FORMS = (
    "停。",
    "停！",
    "嗯，停。",
    "停吧",
    "停啊",
    "停啦",
    "停下来吧",
    "停，好的。",
    "停好了",
    "先停好了",
    "茉莉，停",
    "茉莉停",
)
_NOT_A_STOP_FORMS = (
    "停车",
    "停电了",
    "公交车停了",
    "雨停了吗",
    "停车场在哪",
    "你讲的停好听",
    "停了",
    "挺好",
    "婷",
    "好的",
)


@pytest.mark.parametrize("text", _MISHEARD_STOP_FORMS)
def test_a_misheard_or_decorated_stop_still_stops(text: str) -> None:
    assert is_interrupt_command_only(text), text
    assert is_stop_command_only(text), text
    assert lexical_playback_control_only(text), text


@pytest.mark.parametrize("text", _NOT_A_STOP_FORMS)
def test_a_stop_character_inside_content_is_not_a_stop(text: str) -> None:
    assert not is_interrupt_command_only(text), text
    assert not is_stop_command_only(text), text
    assert not lexical_playback_control_only(text), text


@pytest.mark.parametrize("sentence_id", sorted(POSITIVE))
def test_positive_is_an_explicit_stop_word(sentence_id: str) -> None:
    assert is_stop_command_only(POSITIVE[sentence_id]), POSITIVE[sentence_id]


@pytest.mark.parametrize("sentence_id", sorted(NEGATIVE))
def test_negative_is_never_an_explicit_stop_word(sentence_id: str) -> None:
    """Including 等等/够了/好了: control turns that must not cut a crisis script."""

    assert not is_stop_command_only(NEGATIVE[sentence_id]), NEGATIVE[sentence_id]


@pytest.mark.parametrize("sentence_id", sorted(_WAIT_AND_CLOSE_ACK))
def test_wait_and_completion_ack_are_control_turns_not_the_playback_stop(
    sentence_id: str,
) -> None:
    """N6「等等」N7「够了」N8「好了知道了」: control turns, not the playback stop word.

    The Router keeps them out of chat and answers one fixed acknowledgment (v2 N6 「嗯，你说。」,
    N7/N8 「好的。」).  What stops the playing story is the media stop-word route at the commit,
    not these, so the two are pinned apart here.
    """

    text = NEGATIVE[sentence_id]
    assert is_interrupt_command_only(text)
    expected = "嗯，你说。" if sentence_id == "N6" else "好的。"
    assert interrupt_ack_phrase(text) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [POSITIVE[sid] for sid in ("S1", "S8", "S20", "S10")],
)
async def test_a_positive_stop_final_during_the_story_stops_it_at_once(text: str) -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.audio_frames(15, final_text=text)
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(story)

    await _run_story(_device_identity(f"v2-stop-{text}"), script)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [NEGATIVE[sid] for sid in ("N1", "N4")],
)
async def test_a_negative_final_during_the_story_keeps_playing(text: str) -> None:
    async def script(harness: _StoryHarness) -> None:
        story = await harness.start_story()
        await harness.audio_frames(15, final_text=text)
        await harness.assert_story_still_playing(story)

    await _run_story(_device_identity(f"v2-no-stop-{text}"), script)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [NEGATIVE[sid] for sid in ("N9", "N11", "N16", "N20")],
)
async def test_a_negative_final_that_is_content_stops_the_story(text: str) -> None:
    """Not a stop word, but the content barge-in rule endpoints and answers it."""

    async def script(harness: _StoryHarness) -> None:
        await harness.start_story()
        await harness.audio_frames(15, final_text=text)
        await harness.wait_for(lambda: harness.story.reply_calls == 2)
        assert len(harness.cancel_effects()) == 1
        assert _user_turn_texts(harness.context) == ["给我讲个故事", text]

    await _run_story(_device_identity(f"v2-content-{text}"), script)
