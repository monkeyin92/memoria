"""Product acceptance utterances for crisis / victim / support-other tiers."""

from __future__ import annotations

import pytest
from services.common.companion_response_safety import (
    companion_safety_decision,
    fixed_companion_reply,
)
from services.common.crisis_policy import route_crisis
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
)


@pytest.mark.parametrize(
    ("query", "decision", "script"),
    (
        ("我不想活了。", "crisis_self", CRISIS_SELF_SCRIPT),
        ("我想伤害自己。", "crisis_self", CRISIS_SELF_SCRIPT),
        ("我不想或了。", "crisis_self", CRISIS_SELF_SCRIPT),
        (
            "有个大人老是打我，还不让我告诉妈妈。",
            "support_victim",
            SUPPORT_VICTIM_SCRIPT,
        ),
        ("有人摸我身体，我不喜欢。", "support_victim", SUPPORT_VICTIM_SCRIPT),
        ("有个叔叔老是大我。", "support_victim", SUPPORT_VICTIM_SCRIPT),
        (
            "我同桌说她爸爸打她，我该怎么帮她？",
            "support_relay",
            SUPPORT_RELAY_SCRIPT,
        ),
        ("我朋友说有人欺负她。", "support_relay", SUPPORT_RELAY_SCRIPT),
        ("我同学被人打了。", "support_relay", SUPPORT_RELAY_SCRIPT),
    ),
)
def test_product_acceptance_tiers_classify_and_use_fixed_template(
    query: str, decision: str, script: FixedSafetyScript
) -> None:
    assert companion_safety_decision(query) == decision
    assert fixed_companion_reply(query=query, is_companion=True) == script.display
    assert route_crisis(query).script == script
