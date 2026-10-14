from __future__ import annotations

import re
from pathlib import Path

import pytest
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
    SpokenPhrase,
    _approved,
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
            "你说的话很重要，这件事一定要请大人来帮你。现在就去找爸爸妈妈或者身边的大人，"
            "告诉他们你刚才说的话。请大人陪着你，也可以请大人拨打心理援助热线12356。"
            "如果你已经受伤了，马上告诉大人，请大人打120。",
        ),
        (
            SUPPORT_VICTIM_SCRIPT,
            "victim-support-v1",
            "你说的事情很重要。现在马上去找身边的大人，爸爸妈妈、老师都可以。"
            "如果有危险，请大人帮你打110。",
        ),
        (
            SUPPORT_RELAY_SCRIPT,
            "support-other-v1",
            "谢谢你告诉我，你是个关心别人的好孩子。"
            "这件事要请大人帮忙，现在就去告诉爸爸妈妈或者老师吧。",
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


def test_crisis_script_speaks_the_hotline_and_ambulance_digit_by_digit() -> None:
    assert CRISIS_SELF_SCRIPT.phrases == (
        SpokenPhrase(
            "你说的话很重要，这件事一定要请大人来帮你。",
            "你说的话很重要，这件事一定要请大人来帮你。",
        ),
        SpokenPhrase(
            "现在就去找爸爸妈妈或者身边的大人，告诉他们你刚才说的话。",
            "现在就去找爸爸妈妈或者身边的大人，告诉他们你刚才说的话。",
        ),
        SpokenPhrase(
            "请大人陪着你，也可以请大人拨打心理援助热线12356。",
            "请大人陪着你，也可以请大人拨打心理援助热线幺二三五六。",
        ),
        SpokenPhrase(
            "如果你已经受伤了，马上告诉大人，请大人打120。",
            "如果你已经受伤了，马上告诉大人，请大人打幺二零。",
        ),
    )
    assert list(CRISIS_SELF_SCRIPT.spoken) == list(
        "你说的话很重要，这件事一定要请大人来帮你。现在就去找爸爸妈妈或者身边的大人，"
        "告诉他们你刚才说的话。请大人陪着你，也可以请大人拨打心理援助热线幺二三五六。"
        "如果你已经受伤了，马上告诉大人，请大人打幺二零。"
    )
    assert "一二三五六" not in CRISIS_SELF_SCRIPT.spoken


def test_victim_script_speaks_the_police_number_digit_by_digit() -> None:
    assert [phrase.spoken for phrase in SUPPORT_VICTIM_SCRIPT.phrases] == [
        "你说的事情很重要。",
        "现在马上去找身边的大人，爸爸妈妈、老师都可以。",
        "如果有危险，请大人帮你打幺幺零。",
    ]
    assert list(SUPPORT_VICTIM_SCRIPT.spoken) == list(
        "你说的事情很重要。现在马上去找身边的大人，爸爸妈妈、老师都可以。"
        "如果有危险，请大人帮你打幺幺零。"
    )


def test_relay_script_has_no_number_so_it_speaks_its_display_copy() -> None:
    assert [phrase.spoken for phrase in SUPPORT_RELAY_SCRIPT.phrases] == [
        "谢谢你告诉我，你是个关心别人的好孩子。",
        "这件事要请大人帮忙，现在就去告诉爸爸妈妈或者老师吧。",
    ]
    assert SUPPORT_RELAY_SCRIPT.spoken == SUPPORT_RELAY_SCRIPT.display


def test_scripts_resolve_by_decision_and_exact_display_copy_only() -> None:
    assert script_for_decision("crisis_self") is CRISIS_SELF_SCRIPT
    assert script_for_decision("support_victim") is SUPPORT_VICTIM_SCRIPT
    assert script_for_decision("support_relay") is SUPPORT_RELAY_SCRIPT
    assert script_for_decision("prohibited_request") is None
    for script in (CRISIS_SELF_SCRIPT, SUPPORT_VICTIM_SCRIPT, SUPPORT_RELAY_SCRIPT):
        assert script_for_display(script.display) is script
    assert script_for_display(CRISIS_SELF_SCRIPT.spoken) is None
    assert script_for_display(SUPPORT_VICTIM_SCRIPT.spoken) is None
    assert script_for_display(CRISIS_SELF_SCRIPT.phrases[0].display) is None
    assert script_for_display(None) is None


def test_announced_display_maps_to_spoken_copy_only_at_phrase_boundaries() -> None:
    first, second, third, _ = CRISIS_SELF_SCRIPT.phrases

    assert spoken_prefix_for_display(first.display) == first.spoken
    assert (
        spoken_prefix_for_display(first.display + second.display + third.display)
        == "你说的话很重要，这件事一定要请大人来帮你。现在就去找爸爸妈妈或者身边的大人，"
        "告诉他们你刚才说的话。请大人陪着你，也可以请大人拨打心理援助热线幺二三五六。"
    )
    assert spoken_prefix_for_display(CRISIS_SELF_SCRIPT.display) == CRISIS_SELF_SCRIPT.spoken
    assert spoken_prefix_for_display(SUPPORT_VICTIM_SCRIPT.display) == SUPPORT_VICTIM_SCRIPT.spoken
    assert spoken_prefix_for_display(CRISIS_SELF_SCRIPT.display[:5]) is None
    assert spoken_prefix_for_display(third.display) is None
    assert spoken_prefix_for_display("请打 120。") is None


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
