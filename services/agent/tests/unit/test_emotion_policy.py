from __future__ import annotations

import pytest
from services.agent.src.orchestration.emotion import EmotionSmoother
from services.agent.src.orchestration.prosody import (
    mascot_expression_for_reply,
    speech_plan_for_emotion,
    speech_plan_for_turn,
)


def test_acoustic_emotion_requires_repetition_and_never_claims_provider_confidence() -> None:
    smoother = EmotionSmoother(ttl_ms=30_000)

    first = smoother.observe_acoustic(
        "sad", text="最近有点累", turn_id=1, now_ns=1_000_000_000
    )
    second = smoother.observe_acoustic(
        "sad", text="还是提不起精神", turn_id=2, now_ns=2_000_000_000
    )

    assert first.label == "neutral"
    assert second.label == "sad"
    assert second.provider_label == "sad"
    assert second.provider_confidence is None
    assert second.persist is False
    assert smoother.current(now_ns=32_000_000_001).label == "neutral"


def test_explicit_self_report_can_override_one_weak_acoustic_label() -> None:
    smoother = EmotionSmoother()

    observation = smoother.observe_acoustic(
        "neutral",
        text="我现在真的很开心",
        turn_id=1,
        now_ns=1,
    )

    assert observation.label == "happy"
    assert observation.evidence == ("explicit:self_report", "acoustic:qwen3-asr")


def test_one_transcribed_laugh_does_not_infer_happy_delivery() -> None:
    smoother = EmotionSmoother()

    observation = smoother.observe_acoustic(
        "neutral",
        text="哈哈，这个还挺有意思的",
        turn_id=1,
        now_ns=1,
    )

    assert observation.label == "neutral"
    assert observation.evidence == (
        "acoustic:qwen3-asr:laughter",
        "fallback:neutral",
    )


def test_distress_self_report_wins_over_a_laughter_marker() -> None:
    smoother = EmotionSmoother()

    observation = smoother.observe_acoustic(
        "happy",
        text="哈哈，其实我很难过",
        turn_id=1,
        now_ns=1,
    )

    assert observation.label == "sad"


@pytest.mark.parametrize(
    "text",
    (
        "呵呵，这客服把我的钱骗走了",
        "哈哈，我刚刚出车祸了",
        "他说‘哈哈’，但我很不舒服",
    ),
)
def test_laughter_text_never_bypasses_conservative_emotion_policy(text: str) -> None:
    smoother = EmotionSmoother()

    observation = smoother.observe_acoustic(
        "neutral",
        text=text,
        turn_id=1,
        now_ns=1,
    )

    assert observation.label == "neutral"


def test_duplicate_acoustic_callbacks_from_one_turn_count_once() -> None:
    smoother = EmotionSmoother()

    first = smoother.observe_acoustic("happy", turn_id=1, now_ns=1)
    duplicate = smoother.observe_acoustic("happy", turn_id=1, now_ns=2)

    assert first.label == "neutral"
    assert duplicate.label == "neutral"


def test_main_transcript_laughter_overrides_a_happy_acoustic_observation_to_neutral() -> None:
    smoother = EmotionSmoother()
    smoother.observe_acoustic("happy", turn_id=1, now_ns=1)
    acoustic = smoother.observe_acoustic("happy", turn_id=2, now_ns=2)

    observation = smoother.observe_text(
        "哈哈，我刚刚出车祸了",
        acoustic=acoustic,
        now_ns=3,
    )

    assert acoustic.label == "happy"
    assert observation.label == "neutral"


def test_explicit_self_report_ignores_quoted_or_reported_other_people() -> None:
    smoother = EmotionSmoother()

    quoted = smoother.observe_text("她说‘我很开心’，但我今天很累", now_ns=1)
    reported = smoother.observe_text("朋友说我很开心", now_ns=2)

    assert quoted.label == "neutral"
    assert reported.label == "neutral"


def test_speech_plan_only_uses_cosyvoice_supported_safe_output_emotions() -> None:
    sad = speech_plan_for_emotion("sad")
    happy = speech_plan_for_emotion("happy")
    angry = speech_plan_for_emotion("angry")

    # Non-happy emotions lock to neutral instruct + rate 1.0 for stable timbre/loudness.
    assert (sad.voice_emotion, sad.rate) == ("neutral", 1.0)
    assert (happy.voice_emotion, happy.rate) == ("happy", 1.0)
    assert angry.voice_emotion == "neutral"
    assert angry.rate == 1.0
    assert sad.instruction == "你正在进行闲聊互动，你说话的情感是neutral。"


def test_multi_step_request_gets_deliberative_delivery_without_affecting_direct_answer() -> None:
    deliberative = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="帮我安排一个十五分钟的英语口语训练",
    )
    direct = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="今天星期几",
    )

    assert deliberative.delivery_mode == "deliberative"
    # Rate is fixed at 1.0 for consistent CosyVoice loudness; style differs via LLM only.
    assert deliberative.rate == direct.rate == 1.0
    assert "四到十二个字" in deliberative.llm_instruction
    assert "以逗号结束" in deliberative.llm_instruction
    assert deliberative.tts_prefix == ""
    assert speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="帮我安排一个十五分钟的英语口语训练",
        use_markup_tags=True,
    ).tts_prefix == "[breath]"
    assert direct.delivery_mode == "direct"
    assert direct.llm_instruction == ""


def test_safe_acoustic_laughter_gets_one_warm_laugh_but_serious_context_never_does() -> None:
    light = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，我刚才把单词读错得太离谱了",
    )
    light_markup = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，我刚才把单词读错得太离谱了",
        use_markup_tags=True,
    )
    serious = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，其实我刚刚出车祸了",
    )

    assert light.delivery_mode == "light_laughter"
    assert light.voice_emotion == "happy"
    assert light.tts_prefix == "呵，"
    assert light_markup.tts_prefix == "[laughter]"
    assert "只轻笑一次" in light.llm_instruction
    assert serious.delivery_mode == "supportive"
    assert serious.voice_emotion == "neutral"
    assert serious.strip_paralinguistic is True
    assert "不要笑" in serious.llm_instruction

    serious_plan = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="帮我安排一下葬礼",
    )
    assert serious_plan.delivery_mode == "supportive"


def test_assistant_reply_expression_follows_safe_delivery_semantics() -> None:
    caring = speech_plan_for_turn(
        label="sad",
        provider_label="sad",
        text="我最近真的很难过。",
    )
    happy = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，我刚才把单词读错得太离谱了",
    )
    curious = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="帮我安排一个十五分钟的英语口语训练",
    )
    direct = speech_plan_for_turn(
        label="neutral",
        provider_label="neutral",
        text="今天星期几",
    )

    assert mascot_expression_for_reply(plan=caring, text="我会陪着你。") == "caring"
    assert mascot_expression_for_reply(plan=happy, text="太好了！") == "happy"
    assert mascot_expression_for_reply(plan=curious, text="可以，先从第一步开始。") == "curious"
    assert mascot_expression_for_reply(plan=direct, text="今天星期三。") == "neutral"


def test_transcribed_acoustic_laughter_can_drive_delivery_without_claiming_happy() -> None:
    smoother = EmotionSmoother()
    acoustic = smoother.observe_acoustic(
        "neutral",
        text="哈哈，我把单词读错得太离谱了",
        turn_id=1,
        now_ns=1,
    )
    observation = smoother.observe_text(
        "哈哈，我把单词读错得太离谱了",
        acoustic=acoustic,
        now_ns=2,
    )

    plan = speech_plan_for_turn(
        label=observation.label,
        provider_label=observation.provider_label,
        evidence=observation.evidence,
        text="哈哈，我把单词读错得太离谱了",
    )

    assert observation.label == "neutral"
    assert plan.delivery_mode == "light_laughter"
    assert plan.voice_emotion == "happy"
