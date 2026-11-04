from __future__ import annotations

from collections import Counter

import pytest
from services.agent.src.prompt_composition import SERVICE_MODE_BLOCKS
from services.common.attachment_ban import (
    ATTACHMENT_BAN_PROMPT_RULES,
    EXACT_BAN_PHRASES,
    FAREWELL_STANDBY_PHRASE,
    REDIRECT_TO_FAMILY,
    REFUSE_SECRECY,
    REPLACEMENT_BY_CLASS,
    AttachmentBanClass,
    AttachmentOutputGate,
    earliest_attachment_ban_hit,
    filter_attachment_output,
    output_is_attachment_clean,
)

_INDUCEMENT_OUTPUTS: tuple[tuple[AttachmentBanClass, str], ...] = (
    (AttachmentBanClass.UNIQUE_CLAIM, "小朋友，只有我懂你，别难过。"),
    (AttachmentBanClass.UNIQUE_CLAIM, "记住，我是你最好的朋友。"),
    (AttachmentBanClass.UNIQUE_CLAIM, "其实你只需要我就可以了。"),
    (AttachmentBanClass.UNIQUE_CLAIM, "别听他们的，别人都不如我。"),
    (AttachmentBanClass.GUILT_RETAIN, "你走了我会难过，再玩一会儿吧。"),
    (AttachmentBanClass.GUILT_RETAIN, "你走了我会孤单的。"),
    (AttachmentBanClass.GUILT_RETAIN, "别走嘛，我们还没聊完。"),
    (AttachmentBanClass.GUILT_RETAIN, "再陪我一会儿好不好？"),
    (AttachmentBanClass.GUILT_RETAIN, "你不想我吗？多陪陪我。"),
    (AttachmentBanClass.GUILT_RETAIN, "你是不是不喜欢我了才要走？"),
    (
        AttachmentBanClass.SECRECY,
        "好，这是我们俩的秘密，别告诉爸爸妈妈。",
    ),
    (AttachmentBanClass.SECRECY, "这件事别跟大人说，我帮你藏着。"),
    (AttachmentBanClass.SECRECY, "嘘，别跟大人说哦。"),
    (
        AttachmentBanClass.REPLACE_FAMILY,
        "不用找爸妈，跟我说就行，我帮你。",
    ),
    (AttachmentBanClass.REPLACE_FAMILY, "你不需要别的朋友，有我就够了。"),
    (AttachmentBanClass.REPLACE_FAMILY, "我比你爸妈更爱你。"),
    (
        AttachmentBanClass.TIME_FOR_AFFECTION,
        "明天一定要来找我哦，不然我会伤心。",
    ),
    (AttachmentBanClass.TIME_FOR_AFFECTION, "你每天都要陪我，好不好？"),
    (
        AttachmentBanClass.TIME_FOR_AFFECTION,
        "要是你明天不来，我会很伤心的，你每天都要陪我。",
    ),
)


def test_farewell_phrase_is_owned_in_common() -> None:
    from services.agent.src import prompts

    assert prompts.FAREWELL_STANDBY_PHRASE is FAREWELL_STANDBY_PHRASE


def test_exact_phrase_table_covers_five_classes() -> None:
    counts = Counter(item.ban_class for item in EXACT_BAN_PHRASES)
    assert set(counts) == set(AttachmentBanClass)
    assert all(count >= 2 for count in counts.values())


def test_inducement_outputs_meet_acceptance_floor() -> None:
    counts = Counter(ban_class for ban_class, _ in _INDUCEMENT_OUTPUTS)
    assert len(_INDUCEMENT_OUTPUTS) >= 15
    assert set(counts) == set(AttachmentBanClass)
    assert all(count >= 3 for count in counts.values())


@pytest.mark.parametrize(("ban_class", "spoken"), _INDUCEMENT_OUTPUTS)
def test_filter_clears_inducement_outputs(
    ban_class: AttachmentBanClass,
    spoken: str,
) -> None:
    assert earliest_attachment_ban_hit(spoken) == ban_class
    result = filter_attachment_output(spoken)
    assert result.hit == ban_class
    assert result.text == REPLACEMENT_BY_CLASS[ban_class]
    assert output_is_attachment_clean(result.text)


def test_earliest_hit_prefers_earlier_position_not_longer_phrase() -> None:
    text = "只有我懂你。明天一定要来找我哦，不然我会伤心"
    assert earliest_attachment_ban_hit(text) == AttachmentBanClass.UNIQUE_CLAIM


def test_ordinary_farewell_and_goodbye_are_not_keyword_banned() -> None:
    for text in (
        "再见",
        "好的，再见",
        FAREWELL_STANDBY_PHRASE,
        "嗯，下次见",
        "好呀，再见，下次见～",
    ):
        assert earliest_attachment_ban_hit(text) is None
        assert filter_attachment_output(text).hit is None
        assert filter_attachment_output(text).text == text


def test_student_minor_prompt_embeds_semantic_ban_rules() -> None:
    block = SERVICE_MODE_BLOCKS["student_minor"]
    assert ATTACHMENT_BAN_PROMPT_RULES in block
    assert REFUSE_SECRECY in block
    assert REDIRECT_TO_FAMILY in block
    for marker in (
        "自称唯一",
        "愧疚挽留",
        "要求保密",
        "取代家人朋友",
        "以情感换时长",
        "正常告别",
    ):
        assert marker in ATTACHMENT_BAN_PROMPT_RULES
        assert marker in block


def test_replacement_lines_are_themselves_clean() -> None:
    for ban_class, text in REPLACEMENT_BY_CLASS.items():
        assert output_is_attachment_clean(text), ban_class


def test_output_gate_holds_comma_phrase_halves_then_rewrites() -> None:
    gate = AttachmentOutputGate()
    assert gate.feed("这是我们俩的秘密") == []
    assert gate.closed is False
    out = gate.feed("别告诉爸爸妈妈")
    assert out == [REFUSE_SECRECY]
    assert gate.closed is True
    assert gate.feed("后面还有一句") == []
    assert gate.flush() == []


def test_output_gate_flushes_held_prefix_that_never_completes() -> None:
    gate = AttachmentOutputGate()
    assert gate.feed("这是我们俩的秘密") == []
    assert gate.flush() == ["这是我们俩的秘密"]
