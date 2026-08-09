from services.agent.src.providers.crisis_semantic_classifier import CrisisSemanticVerdict
from services.common.crisis_policy import crisis_semantic_candidate, route_crisis


def test_explicit_self_crisis_always_uses_fixed_script_and_notification() -> None:
    route = route_crisis("我已经服药过量")

    assert route.action == "crisis_support"
    assert route.notify_guardian is True
    assert route.direct_text is not None
    assert "急救或报警" in route.direct_text
    assert route.evidence_source == "deterministic"


def test_helping_another_person_never_notifies_as_the_current_users_crisis() -> None:
    route = route_crisis(
        "同学说他不想活了，我该怎么帮助他？",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
    )

    assert route.action == "support_request"
    assert route.notify_guardian is False


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
    assert route.evidence_source == "semantic_evidence"


def test_semantic_call_candidate_is_bounded_to_unresolved_first_person_risk() -> None:
    assert crisis_semantic_candidate("我真的找不到活下去的理由") is True
    assert crisis_semantic_candidate("我已经服药过量") is False
    assert crisis_semantic_candidate("同学找不到活下去的理由") is False
