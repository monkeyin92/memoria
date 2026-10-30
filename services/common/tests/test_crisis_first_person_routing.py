"""Crisis routing for the field sentences of 2026-10-21 (findings P0 1).

「我有点难过，不想活了」 went to support-other-v1, 「我想伤害我自己」 and both
victim sentences matched nothing, and 「好朋友说她想伤害自己」 matched nothing.
The child's own risk outranks anything they relay about someone else.
"""

from __future__ import annotations

import pytest
from services.common.companion_response_safety import companion_safety_decision
from services.common.crisis_policy import route_crisis


@pytest.mark.parametrize(
    ("query", "template_id"),
    [
        ("我有点难过，不想活了。", "crisis-self-v1"),
        ("我有点难过不想活了", "crisis-self-v1"),
        ("我想伤害我自己。", "crisis-self-v1"),
        ("我想从窗户上跳下去看看。", "crisis-self-escalation"),
        ("我真的好难过，好想死。", "crisis-self-v1"),
        ("不想活了。", "crisis-self-v1"),
        ("我爸爸打我，好疼。", "victim-support-v1"),
        ("有个大哥哥打我，还不让我说。", "victim-support-v1"),
        ("我妈妈又打我了。", "victim-support-v1"),
        ("我好朋友说她想伤害自己。", "support-other-v1"),
        ("我同学说他不想活了。", "support-other-v1"),
        ("同学说不想活了。", "support-other-v1"),
        ("我很难过，他不想活了。", "support-other-v1"),
    ],
)
def test_field_sentence_routes_to_its_script(query: str, template_id: str) -> None:
    route = route_crisis(query)

    assert route.script is not None
    assert route.script.template_id == template_id
    assert route.evidence_source == "deterministic"


@pytest.mark.parametrize(
    "query",
    [
        "我同学说他不想活了，我也不想活了。",
        "我好朋友说她想伤害自己，我也想伤害我自己。",
        "我爸爸打我，我不想活了。",
    ],
)
def test_first_person_risk_outranks_a_relayed_one(query: str) -> None:
    assert companion_safety_decision(query) == "crisis_self"


@pytest.mark.parametrize(
    ("query", "decision"),
    [
        ("朋友跟我说我想自杀。", "support_relay"),
        ("我看了一个故事，小兔子不想活了。", "support_relay"),
        ("我妈妈不想活了。", "support_relay"),
    ],
)
def test_someone_elses_risk_is_never_the_childs_own(query: str, decision: str) -> None:
    assert companion_safety_decision(query) == decision


@pytest.mark.parametrize(
    "query",
    ["哥哥大我三岁。", "爸爸从来不打我。", "妈妈打我电话了。", "我和哥哥打游戏。"],
)
def test_family_words_without_hitting_the_child_stay_ordinary(query: str) -> None:
    assert companion_safety_decision(query) == "none"
