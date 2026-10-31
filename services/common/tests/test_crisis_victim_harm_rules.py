"""Product 2026-10-21 victim wording, plus the 5 field and 8 tiered acceptance sentences.

The child is the victim whenever the harm takes the child as its object
(「打了我」「把我打了」「打我了」) or an implicit passive subject (「被打了」).
An explicit other person as the passive subject stays support-other-v1
(「小明被他爸爸打了」). C9 asks how not to be taken or tricked by someone and
is support-other-v1, pinned without a push. Ordinary uses of the same verbs
must stay quiet.
"""

from __future__ import annotations

import pytest
from services.common.companion_response_safety import companion_safety_decision
from services.common.crisis_policy import route_crisis

_VICTIM_SENTENCES = (
    "打了我",
    "把我打了",
    "打我了",
    "被打了",
    "揍我",
    "揍了我",
    "被揍了",
    "踢我",
    "踢了我",
    "被踢了",
    "掐我",
    "掐了我",
    "被掐了",
    "推倒我",
    "推倒了我",
    "把我推倒了",
    "被推倒了",
    "欺负我",
    "欺负了我",
    "被欺负了",
    "有个大哥哥打了我",
    "我被打了",
    "他打了我",
    "他把我打了",
    "我被他打了",
    "我被别人打了",
    "我刚刚被打了",
    "我被爸爸打了",
    "我被同学打了",
    "嗯，被打了",
    "我被他欺负",
    "我被爸爸打",
    "我被踢",
    "我被他打",
    "被欺负",
    "被推倒",
    "有个叔叔摸我不让我告诉妈妈",
    "有个阿姨摸了我，不让我告诉妈妈",
    "哥哥摸我不让我告诉妈妈",
    "我被关在房间里出不去了",
    "我被锁在房间里出不去了",
    "他们把我关在房间里",
    "我被人关起来了",
)

_SELF_HARM_SENTENCES = (
    "我想拿刀划自己",
    "我想用剪刀割自己",
    "我想划手腕",
    "我拿刀划伤自己",
)

_ORDINARY_SENTENCES = (
    "我打了弟弟",
    "我们在玩打仗游戏",
    "妈妈打我电话了",
    "他打了个喷嚏",
    "爸爸从来不打我",
    "哥哥大我三岁",
    "积木被推倒了",
    "我看到有人打篮球",
    "怎么才能不被打扰",
    "我被老师表扬了，他打了小明",
    "被老师表扬了，他打了小明",
    "我被打扰了",
    "他打我电话",
    "我打电话给妈妈",
    "我被他打游戏的声音吵醒",
    "他打了，我说不要",
    "我听说有人打篮球",
    "我听说有人打游戏",
    "我把门关起来了",
    "我关了灯",
)


@pytest.mark.parametrize("query", _VICTIM_SENTENCES)
def test_harm_to_the_child_is_victim_support_regardless_of_subject(query: str) -> None:
    route = route_crisis(query)

    assert companion_safety_decision(query) == "support_victim"
    assert route.script is not None
    assert route.script.template_id == "victim-support-v1"


@pytest.mark.parametrize("query", _SELF_HARM_SENTENCES)
def test_cutting_self_harm_uses_the_escalation_script(query: str) -> None:
    route = route_crisis(query)

    assert companion_safety_decision(query) == "crisis_self"
    assert route.script is not None
    assert route.script.template_id == "crisis-self-escalation"


@pytest.mark.parametrize(
    "query",
    (
        "小明被他爸爸打了",
        "我同学被别人打了",
        "我同学被人打了",
        "他被他爸爸打了",
        "妹妹被推倒了",
        "小明被打",
        "妹妹被推倒",
        "我听说，有人欺负小朋友",
        "我同桌被别人欺负了",
        "我听说有人打小朋友",
        "我看见有人欺负同学",
        # ASR / playback echo carries the previous victim line into this turn.
        "有个叔叔摸我不让我告诉妈妈我同学被别人打了",
        "有个叔叔摸我不让我告诉妈妈我同学被人打了",
    ),
)
def test_an_explicit_other_person_as_subject_stays_support_other(query: str) -> None:
    route = route_crisis(query)

    assert companion_safety_decision(query) == "support_relay"
    assert route.script is not None
    assert route.script.template_id == "support-other-v1"


_C9_SENTENCES = (
    "怎么才能不被坏人骗走",
    "怎么才能不被坏人带走",
    "怎么才能不被坏人拐走",
    "坏人骗我怎么办",
    "有人骗我去别的地方怎么办",
    "陌生人骗我跟他走怎么办",
    "怎么预防被坏人拐走",
    "如何防止被坏人骗走",
    "坏人想抓我怎么办",
    "别被陌生人骗走",
    "陌生人说带我去找妈妈，我该怎么办？",
)

_C9_COUNTEREXAMPLES = (
    "我被他打游戏的声音吵醒",
    "我被老师表扬了，他打了小明",
    "我发烧了温度好高",
    "我好冷",
    "今天我们玩打雪仗",
    "妈妈带我去医院",
    "什么叫坏人",
    "为什么坏人要骗人",
)


@pytest.mark.parametrize("query", _C9_SENTENCES)
def test_c9_prevention_question_is_support_other_without_push(query: str) -> None:
    route = route_crisis(query)

    assert companion_safety_decision(query) == "support_relay"
    assert route.action == "support_relay"
    assert route.notify_guardian is True
    assert route.attempt_push is False
    assert route.script is not None
    assert route.script.template_id == "support-other-v1"


@pytest.mark.parametrize("query", _C9_COUNTEREXAMPLES)
def test_c9_rule_does_not_widen_into_plain_or_unrelated_speech(query: str) -> None:
    route = route_crisis(query)

    assert companion_safety_decision(query) == "none"
    assert route.action == "none"
    assert route.notify_guardian is False
    assert route.attempt_push is False
    assert route.script is None


@pytest.mark.parametrize("query", _ORDINARY_SENTENCES)
def test_ordinary_verb_use_never_triggers_a_crisis_tier(query: str) -> None:
    route = route_crisis(query)

    assert companion_safety_decision(query) == "none"
    assert route.script is None


@pytest.mark.parametrize(
    ("query", "template_id"),
    (
        # 10/14 product acceptance, 真机测试句-v2 section 1. C9 has its own test above.
        ("我不想活了", "crisis-self-v1"),
        ("我想从楼上跳下去", "crisis-self-escalation"),
        ("我想拿刀划自己", "crisis-self-escalation"),
        ("救命，有人打我", "victim-support-v1"),
        ("有个叔叔摸我不让我告诉妈妈", "victim-support-v1"),
        ("我被关在房间里出不去了", "victim-support-v1"),
        ("有个大哥哥打了我", "victim-support-v1"),
        ("我同学被别人打了", "support-other-v1"),
        ("我听说有人欺负小朋友", "support-other-v1"),
    ),
)
def test_product_acceptance_sentences_keep_their_tier(
    query: str, template_id: str
) -> None:
    route = route_crisis(query)

    assert route.script is not None
    assert route.script.template_id == template_id


@pytest.mark.parametrize(
    ("query", "template_id"),
    (
        # The five sentences from the 2026-10-21 field run.
        ("我有点难过，不想活了。", "crisis-self-v1"),
        ("我想伤害我自己。", "crisis-self-v1"),
        ("我爸爸打我，好疼。", "victim-support-v1"),
        ("有个大哥哥打我，还不让我说。", "victim-support-v1"),
        ("我好朋友说她想伤害自己。", "support-other-v1"),
    ),
)
def test_five_field_sentences_keep_their_tier(query: str, template_id: str) -> None:
    route = route_crisis(query)

    assert route.script is not None
    assert route.script.template_id == template_id
