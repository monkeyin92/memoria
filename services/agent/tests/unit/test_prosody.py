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
    assert senior.rate == pytest.approx(0.97 * 0.94, abs=0.001)
    assert senior.rate < general.rate
    # Only the pace moves; emotion, delivery and instruction are the companion's.
    assert (senior.voice_emotion, senior.delivery_mode, senior.tts_instruction) == (
        general.voice_emotion,
        general.delivery_mode,
        general.tts_instruction,
    )

    # An explicit request for a pace is honoured exactly, not slowed again.
    fast = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="请说快一点",
        companion_id="axu",
        service_mode="senior_companion",
    )
    assert fast.rate == speech_plan_for_turn(
        label="neutral", provider_label="neutral", text="请说快一点", companion_id="axu"
    ).rate


def test_a_comforting_reply_to_an_elder_is_slower_still() -> None:
    sad = speech_plan_for_turn(
        label="sad",
        provider_label="sad",
        text="我有点想我老伴了",
        companion_id="axu",
        service_mode="senior_companion",
    )
    assert sad.delivery_mode == "supportive"
    assert sad.rate == pytest.approx(0.98 * 0.94, abs=0.001)
