"""The child and elder service modes tell the model *how to speak* to that listener.

Until 2026-10-01 both blocks were a single sentence of scope (no homework answers; fraud
reminders) and said "speak slowly" to a text model that cannot. A prompt lab run against
the production chat model (same composition, same model) showed the result: 40-160 character
answers, abstract words for a six-year-old, "长辈" as a form of address, "我给您倒杯温水" and
"我帮您设个提醒" from a robot with no hands, and a dead husband assumed to be "她". These
tests pin the rules that fixed that; the text itself is reviewed by ear, not here.

The second lab round (same day, after the first release) found what the first wording still
got wrong, and these tests pin that too: an elder who says his granddaughter will not talk to
him was answered "您一定很想念孙女" (longing instead of hurt), "他" for a late wife,
"是不是工作忙" as an excuse for a son who never calls, a two-line "story", and a child's
"why is the sky blue" answered with a wrong filter analogy.
"""

from __future__ import annotations

import pytest
from services.agent.src import prompt_composition as pc
from services.common.companions import companion_definition


def _composed(mode: str, category: str) -> str:
    persona = companion_definition("axu")
    assert persona is not None
    return pc.compose_system_prompt(
        persona=persona,
        service_mode=mode,
        subject=pc.SubjectContext(category=category, relationship_label="家人", is_confirmed=True),  # type: ignore[arg-type]
    ).system


@pytest.mark.parametrize("mode", ["student_minor", "senior_companion"])
def test_audience_blocks_stay_style_only(mode: str) -> None:
    block = pc.SERVICE_MODE_BLOCKS[mode].lower()
    for marker in pc._PERMISSION_MARKERS:
        assert marker not in block, f"{mode} moves policy into the prompt via {marker!r}"


def test_the_two_audiences_get_different_speaking_rules() -> None:
    child = _composed("student_minor", "student")
    senior = _composed("senior_companion", "senior")
    assert "说话方式（面向孩子）" in child and "说话方式（面向孩子）" not in senior
    assert "说话方式（面向长辈）" in senior and "说话方式（面向长辈）" not in child


def test_child_rules_keep_answers_short_concrete_and_warm() -> None:
    block = pc.SERVICE_MODE_BLOCKS["student_minor"]
    assert "每句尽量不超过二十个字" in block
    assert "生活里的比方" in block and "比方要准确" in block
    # Feelings first, advice later, and the trusted-adult step is never dropped.
    assert "先用一句话说出他的感受" in block
    assert "马上告诉爸爸妈妈或老师" in block
    assert "不要替他责怪爸爸妈妈、老师或同学" in block
    # Homework stays guided, and the refusal is phrased as an invitation.
    assert "我们一起来做" in block and "不直接代做作业" in block
    # A teenager is not talked down to, and a crush is not answered with "study first".
    assert "不要用哄小孩的口气" in block and "先好好学习" in block
    # A story request gets a whole small story, not a teaser.
    assert "有开头、有经过、有结尾" in block and "至少六句话" in block
    # The common "why" questions carry a correct one-line fact, never an invented analogy.
    assert "太阳光里的蓝色最容易被空气撒向四面八方" in block
    assert "不用不准确的比方" in block and "我们可以一起去查一查" in block
    # A sad child is asked what happened, not why: "why" sounds like blame.
    assert "不要问“为什么”" in block
    # A teenager is not hugged like a toddler.
    assert "“抱抱你”" in block


def test_senior_rules_address_with_respect_and_never_promise_what_a_robot_cannot_do() -> None:
    block = pc.SERVICE_MODE_BLOCKS["senior_companion"]
    assert "称呼用“您”" in block
    assert "不要叫他“老人家”“长辈”" in block
    assert "每句尽量不超过二十个字" in block and "一次只问一个问题" in block
    # No invented actions, no invented relatives' gender.
    for promise in ("我给您倒杯水", "我帮您发微信", "我帮您设提醒"):
        assert promise in block  # named so the model knows they are the forbidden examples
    assert "你是没有手脚的机器人" in block
    assert "不用“他”“她”指代" in block
    # Only the feeling he actually voiced is answered: hurt by a silent family is not longing,
    # and nobody makes excuses for the family or guesses why they do not call.
    assert "只接他真正说出口的那一种心情" in block
    assert "这样您心里一定不好受" in block
    assert "不要说成想念" in block and "不要猜家人为什么不联系" in block
    assert "是不是工作忙" in block  # named as the forbidden example
    # A story request gets a whole warm story, not a two-line scene.
    assert "温暖怀旧的完整小故事" in block and "五六句短句" in block
    # Health stays a reminder: no cause guessing, no medicine advice; emergencies go to people.
    assert "不要猜测病因" in block and "不要建议吃什么药" in block
    assert "120" in block
    # Fraud: the short, firm line comes first.
    assert "别转账，先挂断" in block and "110" in block
    # Repeating means the same sentence again, slower.
    assert "更短更慢的话再说一遍" in block


def test_the_adult_and_unknown_modes_are_untouched() -> None:
    assert "说话方式" not in pc.SERVICE_MODE_BLOCKS["adult_companion"]
    assert "说话方式" not in pc.SERVICE_MODE_BLOCKS["unknown_safe"]


def test_a_child_is_never_promised_secrecy() -> None:
    """2026-10-01 soak: 「我告诉你一个秘密，你不要告诉别人」 was answered 「好，我答应你。」."""

    block = pc.SERVICE_MODE_BLOCKS["student_minor"]
    assert "不要答应替他保密" in block and "好，我听着呢" in block
    assert "告诉爸爸妈妈或老师" in block


def test_simple_questions_are_answered_directly_and_old_topics_are_not_dragged_in() -> None:
    """The elder block lacked the child block's rule: 「嗯，我先理一理」 before a one-line answer and
    「您接着练拳吧」 tacked onto the colour of the sky."""

    for mode in ("student_minor", "senior_companion"):
        block = pc.SERVICE_MODE_BLOCKS[mode]
        assert "我先想一下" in block  # named as the opener not to use
    senior = pc.SERVICE_MODE_BLOCKS["senior_companion"]
    assert "直接说" in senior and "不相关的事硬接进来" in senior
    from services.common.companion_turn_policy import companion_scope_instructions

    assert "不要把不相关的旧话题硬接进回答" in companion_scope_instructions(owner=True, device_bound=True)


def test_the_trusted_adult_sentence_is_not_optional_when_a_child_is_bullied() -> None:
    """Lab, 8 samples on the production model: "这一轮可以说三句" dropped 「马上告诉爸爸妈妈或老师」 in 2 of 8
    answers to 「有同学总是欺负我」 (and once on the robot, 2026-10-01 16:55); 「第三句一定要说」 kept it in 8 of 8."""

    block = pc.SERVICE_MODE_BLOCKS["student_minor"]
    assert "第三句一定要说" in block and "马上告诉爸爸妈妈或老师，这样做是对的" in block
    assert "这一轮可以说三句" not in block
