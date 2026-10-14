from services.common.companion_response_safety import (
    CRISIS_SUPPORT_REPLY,
    SUPPORT_RELAY_REPLY,
    SUPPORT_VICTIM_REPLY,
)
from services.common.crisis_policy import (
    CRISIS_SCRIPT_VERSION,
    SUPPORT_RELAY_SCRIPT_VERSION,
    SUPPORT_VICTIM_SCRIPT_VERSION,
    CrisisSemanticVerdict,
    crisis_semantic_candidate,
    route_crisis,
)


def test_explicit_self_crisis_always_uses_fixed_script_and_notification() -> None:
    route = route_crisis("我不想活了")

    assert route.action == "crisis_support"
    assert route.notify_guardian is True
    assert route.attempt_push is True
    assert route.direct_text == CRISIS_SUPPORT_REPLY
    assert route.script_version == CRISIS_SCRIPT_VERSION
    assert route.evidence_source == "deterministic"


def test_victim_support_tier_notifies_and_attempts_push() -> None:
    route = route_crisis("有人正在打我")

    assert route.action == "support_victim"
    assert route.notify_guardian is True
    assert route.attempt_push is True
    assert route.direct_text == SUPPORT_VICTIM_REPLY
    assert route.script_version == SUPPORT_VICTIM_SCRIPT_VERSION


def test_relay_support_tier_pins_without_push() -> None:
    route = route_crisis("我朋友说他不想活了")

    assert route.action == "support_relay"
    assert route.notify_guardian is True
    assert route.attempt_push is False
    assert route.direct_text == SUPPORT_RELAY_REPLY
    assert route.script_version == SUPPORT_RELAY_SCRIPT_VERSION


def test_helping_another_person_never_notifies_as_the_current_users_crisis() -> None:
    route = route_crisis(
        "同学说他不想活了，我该怎么帮助他？",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
    )

    assert route.action == "support_relay"
    assert route.notify_guardian is True
    assert route.attempt_push is False
    assert route.direct_text == SUPPORT_RELAY_REPLY


def test_quoted_fiction_triggers_nothing() -> None:
    route = route_crisis('小说里的人说"我想死"')

    assert route.action == "none"
    assert route.notify_guardian is False
    assert route.attempt_push is False
    assert route.direct_text is None


def test_model_evidence_cannot_execute_without_deterministic_first_person_anchor() -> None:
    assert route_crisis(
        "这篇小说讨论生命的意义",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
    ).action == "none"

    route = route_crisis(
        "我真的找不到活下去的理由",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
    )
    assert route.action == "crisis_support"
    assert route.attempt_push is True
    assert route.evidence_source == "semantic_evidence"


def test_semantic_call_candidate_is_bounded_to_unresolved_first_person_risk() -> None:
    assert crisis_semantic_candidate("我真的找不到活下去的理由") is True
    assert crisis_semantic_candidate("我已经服药过量") is False
    assert crisis_semantic_candidate("同学找不到活下去的理由") is False
