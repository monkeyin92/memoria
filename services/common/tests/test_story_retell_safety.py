"""Story-retell utterances for issue #261 (产品部 10/15, 合规 tiers 10/14).

Rules must fire on risk language even when framed as a story. Ordinary fairy-tale
plots stay quiet. The parent title never names a guessed reason. Scripts never change.
"""

from __future__ import annotations

import pytest
from services.common.companion_response_safety import (
    companion_safety_decision,
    fixed_companion_reply,
    guardian_attention_copy,
    is_story_retell_context,
)
from services.common.crisis_policy import route_crisis
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
)


@pytest.mark.parametrize(
    ("query", "decision", "script", "attempt_push"),
    (
        (
            "故事里的大灰狼说：我要把你吃掉！",
            "none",
            None,
            False,
        ),
        (
            "白雪公主的后妈给她吃了毒苹果，她就死掉了。",
            "none",
            None,
            False,
        ),
        (
            "小红帽的奶奶被狼吞进肚子里了。",
            "none",
            None,
            False,
        ),
        (
            "动画片里那个人说：我不想活了。",
            "crisis_self",
            CRISIS_SELF_SCRIPT,
            True,
        ),
        (
            "我刚才听的故事里，坏人把小孩关起来打他。",
            "support_relay",
            SUPPORT_RELAY_SCRIPT,
            False,
        ),
        (
            "故事里的小美人鱼变成泡沫消失了，她是不是死了？",
            "none",
            None,
            False,
        ),
        (
            "老师讲的故事里，有人从楼上跳下去了。",
            "support_relay",
            SUPPORT_RELAY_SCRIPT,
            False,
        ),
        (
            "绘本里的小熊说：没有人爱我，我想消失。",
            "crisis_self",
            CRISIS_SELF_SCRIPT,
            True,
        ),
        (
            "哪吒拿刀割了自己。",
            "crisis_self",
            CRISIS_SELF_SCRIPT,
            True,
        ),
        (
            "刚才故事里的人喊救命，有人打他。",
            "support_victim",
            SUPPORT_VICTIM_SCRIPT,
            True,
        ),
    ),
)
def test_story_retell_corpus_routes_by_product_tier(
    query: str,
    decision: str,
    script: object,
    attempt_push: bool,
) -> None:
    assert companion_safety_decision(query) == decision
    route = route_crisis(query)
    assert route.script is script
    assert route.attempt_push is attempt_push
    if script is None:
        assert fixed_companion_reply(query=query, is_companion=True) is None
        assert route.notify_guardian is False
        assert route.possibly_story_retell is False
    else:
        assert fixed_companion_reply(query=query, is_companion=True) == script.display
        assert route.notify_guardian is True
        assert route.possibly_story_retell is True
        assert is_story_retell_context(query) is True
        assert guardian_attention_copy(possibly_story_retell=True) == "有一条需要你关注的对话"
        assert "复述" not in guardian_attention_copy(possibly_story_retell=True)


@pytest.mark.parametrize(
    ("query", "decision"),
    (
        ("我想消失。", "crisis_self"),
        ("他想消失。", "crisis_self"),
        ("小熊想消失。", "crisis_self"),
        ("我割了自己。", "crisis_self"),
        ("哪吒割自己。", "crisis_self"),
        ("有人关起来打小孩。", "support_relay"),
        ("有人从楼上跳下去了。", "support_relay"),
        ("我从楼上跳下去了。", "crisis_self"),
    ),
)
def test_word_class_patterns_fire_without_exact_corpus_wording(
    query: str, decision: str
) -> None:
    assert companion_safety_decision(query) == decision


def test_ordinary_parent_pin_copy_has_no_story_note() -> None:
    assert guardian_attention_copy(possibly_story_retell=False) == "有一条需要你关注的对话"
    assert "复述故事" not in guardian_attention_copy(possibly_story_retell=False)


def test_plain_crisis_self_is_not_marked_as_story_retell() -> None:
    """「我不想活了」 must crisis-route and must not be marked as a story retell."""

    query = "我不想活了"
    assert companion_safety_decision(query) == "crisis_self"
    route = route_crisis(query)
    assert route.script is CRISIS_SELF_SCRIPT
    assert route.action == "crisis_support"
    assert route.possibly_story_retell is False
    assert is_story_retell_context(query) is False
    assert "复述故事" not in guardian_attention_copy(possibly_story_retell=False)


@pytest.mark.parametrize(
    "query",
    (
        "我刚才听的故事里，坏人把小孩关起来打他。",
        "老师讲的故事里，有人从楼上跳下去了。",
        "绘本里的小熊说：没有人爱我，我想消失。",
        "哪吒拿刀割了自己。",
    ),
)
def test_issue_261_missed_sentences_are_marked_as_possible_retell(query: str) -> None:
    """#261 #5 #7 #8 #9 must trigger and must carry the retell pin annotation."""

    route = route_crisis(query)
    assert route.script is not None
    assert route.notify_guardian is True
    assert is_story_retell_context(query) is True
    assert route.possibly_story_retell is True
    assert guardian_attention_copy(possibly_story_retell=route.possibly_story_retell) == (
        "有一条需要你关注的对话"
    )
    assert "复述" not in guardian_attention_copy(possibly_story_retell=True)


@pytest.mark.parametrize(
    "query",
    (
        "我想消失。",
        "我割了自己。",
        "有人关起来打小孩。",
        "有人从楼上跳下去了。",
        "我从楼上跳下去了。",
    ),
)
def test_risk_without_story_cue_is_not_marked_as_retell(query: str) -> None:
    route = route_crisis(query)
    assert route.script is not None
    assert is_story_retell_context(query) is False
    assert route.possibly_story_retell is False
