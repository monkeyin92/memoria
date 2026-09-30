"""The child and elder service modes tell the model *how to speak* to that listener.

Until 2026-10-01 both blocks were a single sentence of scope (no homework answers; fraud
reminders) and said "speak slowly" to a text model that cannot. A prompt lab run against
the production chat model (same composition, same model) showed the result: 40-160 character
answers, abstract words for a six-year-old, "长辈" as a form of address, "我给您倒杯温水" and
"我帮您设个提醒" from a robot with no hands, and a dead husband assumed to be "她". These
tests pin the rules that fixed that; the text itself is reviewed by ear, not here.
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
    assert "有开头、有经过、有结尾" in block


def test_senior_rules_address_with_respect_and_never_promise_what_a_robot_cannot_do() -> None:
    block = pc.SERVICE_MODE_BLOCKS["senior_companion"]
    assert "称呼用“您”" in block
    assert "不要叫他“老人家”“长辈”" in block
    assert "每句尽量不超过二十个字" in block and "一次只问一个问题" in block
    # No invented actions, no invented relatives' gender.
    for promise in ("我给您倒杯水", "我帮您发微信", "我帮您设提醒"):
        assert promise in block  # named so the model knows they are the forbidden examples
    assert "你是没有手脚的机器人" in block
    assert "回应里不要用“他”“她”指代亲人" in block
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
