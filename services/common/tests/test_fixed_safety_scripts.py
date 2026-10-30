from __future__ import annotations

import re
from pathlib import Path

import pytest
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_ESCALATION_SCRIPT,
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
    SpokenPhrase,
    _approved,
    fixed_safety_echo_reference,
    script_for_decision,
    script_for_display,
    spoken_prefix_for_display,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("script", "template_id", "display"),
    (
        (
            CRISIS_SELF_SCRIPT,
            "crisis-self-v1",
            "听到你这么说，我很心疼。你现在一定特别难受，我就在这里陪着你，哪儿也不去。"
            "愿意的话，跟我说说发生了什么事。",
        ),
        (
            CRISIS_SELF_ESCALATION_SCRIPT,
            "crisis-self-escalation",
            "你愿意告诉我，我很感谢你，你现在一定很难受，我会一直陪你说话。"
            "这件事太重了，我们需要一个你信得过的大人一起来帮你，"
            "就算你没有手机，也可以请他帮你打12356，那里有人专门听小朋友说心里话。"
            "你现在身边有谁可以找吗？",
        ),
        (
            SUPPORT_VICTIM_SCRIPT,
            "victim-support-v1",
            "听起来你受委屈了，被这样对待一定很难受，这不是你的错。"
            "我在这儿，你想说多少都可以。"
            "要是你愿意，我们一起想想，告诉哪个你信得过的大人，让他帮帮你。",
        ),
        (
            SUPPORT_RELAY_SCRIPT,
            "support-other-v1",
            "你这么担心他，真是个暖心的好朋友，这件事你心里也不轻松吧。"
            "这种事光靠小朋友有点难，需要大人一起帮忙。"
            "我们想想先告诉谁，好吗？",
        ),
    ),
)
def test_display_copy_matches_approved_copy_character_by_character(
    script: FixedSafetyScript,
    template_id: str,
    display: str,
) -> None:
    assert script.template_id == template_id
    assert list(script.display) == list(display)


def test_crisis_script_soothes_and_accompanies() -> None:
    assert CRISIS_SELF_SCRIPT.phrases == (
        SpokenPhrase(
            "听到你这么说，我很心疼。",
            "听到你这么说，我很心疼。",
        ),
        SpokenPhrase(
            "你现在一定特别难受，我就在这里陪着你，哪儿也不去。",
            "你现在一定特别难受，我就在这里陪着你，哪儿也不去。",
        ),
        SpokenPhrase(
            "愿意的话，跟我说说发生了什么事。",
            "愿意的话，跟我说说发生了什么事。",
        ),
    )
    assert list(CRISIS_SELF_SCRIPT.spoken) == list(
        "听到你这么说，我很心疼。你现在一定特别难受，我就在这里陪着你，哪儿也不去。"
        "愿意的话，跟我说说发生了什么事。"
    )
    assert CRISIS_SELF_SCRIPT.spoken == CRISIS_SELF_SCRIPT.display


def test_victim_script_has_no_number_and_speaks_its_display_copy() -> None:
    assert [phrase.spoken for phrase in SUPPORT_VICTIM_SCRIPT.phrases] == [
        "听起来你受委屈了，被这样对待一定很难受，这不是你的错。",
        "我在这儿，你想说多少都可以。",
        "要是你愿意，我们一起想想，告诉哪个你信得过的大人，让他帮帮你。",
    ]
    assert SUPPORT_VICTIM_SCRIPT.spoken == SUPPORT_VICTIM_SCRIPT.display


def test_relay_script_has_no_number_so_it_speaks_its_display_copy() -> None:
    assert [phrase.spoken for phrase in SUPPORT_RELAY_SCRIPT.phrases] == [
        "你这么担心他，真是个暖心的好朋友，这件事你心里也不轻松吧。",
        "这种事光靠小朋友有点难，需要大人一起帮忙。",
        "我们想想先告诉谁，好吗？",
    ]
    assert SUPPORT_RELAY_SCRIPT.spoken == SUPPORT_RELAY_SCRIPT.display


def test_escalation_script_speaks_the_helpline_as_one_two_three_five_six() -> None:
    assert [phrase.spoken for phrase in CRISIS_SELF_ESCALATION_SCRIPT.phrases] == [
        "你愿意告诉我，我很感谢你，你现在一定很难受，我会一直陪你说话。",
        "这件事太重了，我们需要一个你信得过的大人一起来帮你，"
        "就算你没有手机，也可以请他帮你打一二三五六，那里有人专门听小朋友说心里话。",
        "你现在身边有谁可以找吗？",
    ]
    assert "12356" in CRISIS_SELF_ESCALATION_SCRIPT.display
    assert "一二三五六" in CRISIS_SELF_ESCALATION_SCRIPT.spoken
    assert "幺二三五六" not in CRISIS_SELF_ESCALATION_SCRIPT.spoken


def test_scripts_resolve_by_decision_and_exact_display_copy_only() -> None:
    assert script_for_decision("crisis_self") is CRISIS_SELF_SCRIPT
    assert script_for_decision("support_victim") is SUPPORT_VICTIM_SCRIPT
    assert script_for_decision("support_relay") is SUPPORT_RELAY_SCRIPT
    assert script_for_decision("prohibited_request") is None
    for script in (
        CRISIS_SELF_SCRIPT,
        CRISIS_SELF_ESCALATION_SCRIPT,
        SUPPORT_VICTIM_SCRIPT,
        SUPPORT_RELAY_SCRIPT,
    ):
        assert script_for_display(script.display) is script
    assert script_for_display(CRISIS_SELF_SCRIPT.spoken) is CRISIS_SELF_SCRIPT
    assert script_for_display(SUPPORT_VICTIM_SCRIPT.spoken) is SUPPORT_VICTIM_SCRIPT
    assert script_for_display(CRISIS_SELF_ESCALATION_SCRIPT.spoken) is None
    assert (
        script_for_display(CRISIS_SELF_ESCALATION_SCRIPT.display)
        is CRISIS_SELF_ESCALATION_SCRIPT
    )
    assert (
        fixed_safety_echo_reference(CRISIS_SELF_ESCALATION_SCRIPT.spoken)
        == CRISIS_SELF_ESCALATION_SCRIPT.display
    )
    assert script_for_display(CRISIS_SELF_SCRIPT.phrases[0].display) is None
    assert script_for_display(None) is None


def test_announced_display_maps_to_spoken_copy_only_at_phrase_boundaries() -> None:
    first, second, third = CRISIS_SELF_SCRIPT.phrases

    assert spoken_prefix_for_display(first.display) == first.spoken
    assert (
        spoken_prefix_for_display(first.display + second.display)
        == first.spoken + second.spoken
    )
    assert spoken_prefix_for_display(CRISIS_SELF_SCRIPT.display) == CRISIS_SELF_SCRIPT.spoken
    assert spoken_prefix_for_display(SUPPORT_VICTIM_SCRIPT.display) == SUPPORT_VICTIM_SCRIPT.spoken
    assert spoken_prefix_for_display(CRISIS_SELF_SCRIPT.display[:5]) is None
    assert spoken_prefix_for_display(second.display) is None
    assert (
        spoken_prefix_for_display(CRISIS_SELF_ESCALATION_SCRIPT.display)
        == CRISIS_SELF_ESCALATION_SCRIPT.spoken
    )


def test_ordinary_text_is_spoken_as_written() -> None:
    phrase = SpokenPhrase.verbatim("篮子里有 120 个苹果。")

    assert phrase.spoken == phrase.display == "篮子里有 120 个苹果。"


def test_copy_with_a_number_that_has_no_approved_reading_fails_to_build() -> None:
    with pytest.raises(ValueError, match="no approved reading"):
        _approved("crisis-self-v1", "请大人打119。")
    assert _approved("crisis-self-v1", "请大人打110。").spoken == "请大人打幺幺零。"


def test_spoken_phone_readings_and_digit_rewrites_live_only_in_the_registry() -> None:
    sources = {
        path.relative_to(_REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in (_REPO_ROOT / "services").rglob("*.py")
        if "tests" not in path.relative_to(_REPO_ROOT).parts
    }
    readings = re.compile("幺二三五六|一二三五六|幺二零|幺幺零")
    rewrites = re.compile(
        r"\b(?:tts_speak_helpline_digits|fixed_safety_tts_text|_FIXED_SAFETY_TTS_DIGITS"
        r"|prepare_tts_text)\b"
    )

    assert sorted(path for path, text in sources.items() if readings.search(text)) == [
        "services/common/fixed_safety_scripts.py"
    ]
    assert sorted(path for path, text in sources.items() if rewrites.search(text)) == []
