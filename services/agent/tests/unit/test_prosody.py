from __future__ import annotations

import pytest
from services.agent.src.orchestration.prosody import (
    ProsodyController,
    ProsodyFeatures,
    SpeakingStyle,
    speech_plan_for_turn,
)


def test_explicit_slow_down() -> None:
    c = ProsodyController()
    st = c.update(ProsodyFeatures(explicit_request="说慢一点"))
    assert st.style is SpeakingStyle.CALM
    assert 0.90 <= st.rate <= 1.10
    assert st.rate < 1.0
    assert c.instruction_for_cosyvoice() == "你正在进行闲聊互动，你说话的情感是neutral。"


def test_low_confidence_neutral() -> None:
    c = ProsodyController()
    st = c.update(ProsodyFeatures(rms_dbfs=-10, speech_rate_cps=7.0))
    # excited conf 0.65 -> neutral
    assert st.style is SpeakingStyle.NEUTRAL


def test_no_sensitive_labels_in_state() -> None:
    c = ProsodyController()
    c.update(ProsodyFeatures(explicit_request="慢一点"))
    dumped = str(c.state)
    assert "焦虑" not in dumped
    assert "抑郁" not in dumped


def test_companion_delivery_changes_neutral_voice_without_overriding_user_request() -> None:
    bright = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="今天想随便聊聊",
        companion_id="taoxi",
    )
    steady = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="今天想随便聊聊",
        companion_id="axu",
    )
    explicit = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="请说快一点",
        companion_id="xuanmo",
    )

    assert bright.voice_emotion == "happy"
    assert bright.rate == 1.05
    assert "青春" in bright.tts_instruction
    assert steady.voice_emotion == "neutral"
    assert steady.rate == 0.97
    assert "沉稳" in steady.tts_instruction
    assert explicit.rate == 1.10


def test_the_elder_mode_really_slows_the_voice_unless_the_user_asked_for_a_pace() -> None:
    kwargs = dict(label="neutral", provider_label="neutral", text="今天想随便聊聊", companion_id="axu")
    general = speech_plan_for_turn(**kwargs)
    senior = speech_plan_for_turn(**kwargs, service_mode="senior_companion")
    student = speech_plan_for_turn(**kwargs, service_mode="student_minor")
    assert general.rate == 0.97 and student.rate == general.rate
    assert senior.rate == pytest.approx(0.97 * 0.90, abs=0.001)
    assert senior.rate < general.rate
    # The pace moves; emotion and delivery are the companion's own.
    assert (senior.voice_emotion, senior.delivery_mode) == (general.voice_emotion, general.delivery_mode)

    # An explicit request for a pace is honoured exactly, not slowed again, and gets no hint.
    fast = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="请说快一点",
        companion_id="axu",
        service_mode="senior_companion",
    )
    plain_fast = speech_plan_for_turn(
        label="neutral", provider_label="neutral", text="请说快一点", companion_id="axu"
    )
    assert fast.rate == plain_fast.rate
    assert fast.tts_instruction == plain_fast.tts_instruction


def test_a_comforting_reply_to_an_elder_is_slower_still() -> None:
    sad = speech_plan_for_turn(
        label="sad",
        provider_label="sad",
        text="我有点想我老伴了",
        companion_id="axu",
        service_mode="senior_companion",
    )
    assert sad.delivery_mode == "supportive"
    assert sad.rate == pytest.approx(0.98 * 0.90, abs=0.001)


def test_each_listener_gets_a_voice_of_their_own_not_only_a_pace() -> None:
    kwargs = dict(label="neutral", provider_label="neutral", text="今天想随便聊聊", companion_id="axu")
    adult = speech_plan_for_turn(**kwargs)
    child = speech_plan_for_turn(**kwargs, service_mode="student_minor")
    elder = speech_plan_for_turn(**kwargs, service_mode="senior_companion")
    # The companion's own instruction stays first; the hint for the listener follows it.
    assert child.tts_instruction.startswith(adult.tts_instruction.rstrip("。"))
    assert elder.tts_instruction.startswith(adult.tts_instruction.rstrip("。"))
    assert "对孩子说话" in child.tts_instruction and "对长辈说话" not in child.tts_instruction
    assert "对长辈说话" in elder.tts_instruction and "对孩子说话" not in elder.tts_instruction
    assert "对孩子说话" not in adult.tts_instruction and "对长辈说话" not in adult.tts_instruction
    assert len(child.tts_instruction) <= 240 and len(elder.tts_instruction) <= 240
    # Other service modes are not touched.
    for mode in (None, "adult_companion", "family_shared", "unknown_safe"):
        assert speech_plan_for_turn(**kwargs, service_mode=mode).tts_instruction == adult.tts_instruction


def test_comforting_turns_use_the_gentle_form_of_the_listeners_hint() -> None:
    kwargs = dict(label="sad", provider_label="sad", text="我今天有点难过", companion_id="axu")
    child = speech_plan_for_turn(**kwargs, service_mode="student_minor")
    elder = speech_plan_for_turn(**kwargs, service_mode="senior_companion")
    assert child.delivery_mode == elder.delivery_mode == "supportive"
    assert "放柔放轻" in child.tts_instruction and "带一点笑意" not in child.tts_instruction
    assert "温和低缓" in elder.tts_instruction and "节奏放缓" not in elder.tts_instruction


def test_a_request_for_a_style_beats_the_listener_hint() -> None:
    for mode in ("student_minor", "senior_companion"):
        plan = speech_plan_for_turn(
            label="neutral",
            provider_label="neutral",
            text="请用四川话说",
            companion_id="axu",
            service_mode=mode,
        )
        assert plan.dialect == "sichuan"
        assert "对孩子说话" not in plan.tts_instruction
        assert "对长辈说话" not in plan.tts_instruction


@pytest.mark.parametrize(
    "text",
    [
        "我有点想我老伴了",
        "我一个人在家，有点孤单",
        "我最近晚上睡不着",
        "我中考压力好大",
        "今天同学不跟我玩",
        "我在学校被人笑话了，还有人欺负我",
        "我儿子好久没理我，也不理我的电话",
        "我想念我妈妈",
    ],
)
def test_words_children_and_elders_use_for_hard_moments_get_the_gentle_delivery(text: str) -> None:
    plan = speech_plan_for_turn(label="neutral", provider_label="neutral", text=text, companion_id="axu")
    assert plan.delivery_mode == "supportive"


@pytest.mark.parametrize("text", ["今天想随便聊聊", "我画了一幅画", "给我讲个故事吧", "天为什么是蓝色的"])
def test_ordinary_talk_keeps_the_everyday_delivery(text: str) -> None:
    plan = speech_plan_for_turn(label="neutral", provider_label="neutral", text=text, companion_id="axu")
    assert plan.delivery_mode != "supportive"
