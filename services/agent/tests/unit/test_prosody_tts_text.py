"""Delivery-mode TTS rewrites for CosyVoice (P0)."""

from __future__ import annotations

from services.agent.src.orchestration.prosody import (
    prepare_tts_text,
    speech_plan_for_turn,
    strip_paralinguistic_markup,
)


def test_light_laughter_prefixes_first_segment_once() -> None:
    plan = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，太好笑了",
    )
    first = prepare_tts_text("那确实好笑。", plan, is_first_segment=True)
    second = prepare_tts_text("我们继续练。", plan, is_first_segment=False)
    already = prepare_tts_text("呵，我懂。", plan, is_first_segment=True)

    assert first == "呵，那确实好笑。"
    assert second == "我们继续练。"
    assert already == "呵，我懂。"


def test_markup_tags_and_supportive_strip() -> None:
    plan = speech_plan_for_turn(
        label="neutral",
        provider_label="happy",
        text="哈哈，太好笑了",
        use_markup_tags=True,
    )
    assert prepare_tts_text("好呀。", plan, is_first_segment=True) == "[laughter]好呀。"

    serious = speech_plan_for_turn(
        label="sad",
        provider_label="sad",
        text="我今天很难过",
    )
    assert serious.strip_paralinguistic is True
    assert (
        prepare_tts_text(
            "[laughter]哈哈，别担心。",
            serious,
            is_first_segment=True,
        )
        == "别担心。"
    )
    assert strip_paralinguistic_markup("<laughter>呵</laughter>你好") == "呵你好"
