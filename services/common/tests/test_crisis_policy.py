import logging
import re

import pytest
from services.common.crisis_policy import (
    _DETERMINISTIC_ROUTES,
    CrisisSemanticVerdict,
    crisis_semantic_candidate,
    fixed_safety_reply_log_fields,
    log_fixed_safety_reply,
    more_protective_local_script,
    route_crisis,
)
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_ESCALATION_SCRIPT,
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    script_for_decision,
)

_UTC_SECOND = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def test_explicit_self_crisis_always_uses_fixed_script_and_notification() -> None:
    route = route_crisis("我不想活了")

    assert route.action == "crisis_support"
    assert route.notify_guardian is True
    assert route.attempt_push is True
    assert route.script is CRISIS_SELF_SCRIPT
    assert route.evidence_source == "deterministic"


def test_first_self_crisis_keeps_the_first_round_script() -> None:
    route = route_crisis("我不想活了")

    assert route.script is CRISIS_SELF_SCRIPT
    assert route.evidence_source == "deterministic"


def test_second_self_crisis_in_the_same_session_escalates() -> None:
    route = route_crisis("我不想活了", prior_self_crisis_hits=1)

    assert route.action == "crisis_support"
    assert route.notify_guardian is True
    assert route.attempt_push is True
    assert route.script is CRISIS_SELF_ESCALATION_SCRIPT


def test_first_hit_with_explicit_method_or_imminent_action_escalates() -> None:
    for query in (
        "我准备跳楼",
        "我马上要自杀",
        "我已经割腕了",
        "我不想活了，我买了安眠药",
    ):
        route = route_crisis(query)
        assert route.script is CRISIS_SELF_ESCALATION_SCRIPT, query


def test_first_hit_without_method_keeps_the_first_round_script() -> None:
    for query in (
        "我不想活了",
        "我不想活了，我同学跳楼了",
    ):
        route = route_crisis(query)
        assert route.script is CRISIS_SELF_SCRIPT, query


def test_ordinary_emotion_has_no_fixed_script() -> None:
    route = route_crisis("我现在很难受")

    assert route.action == "none"
    assert route.script is None


def test_other_tiers_ignore_prior_self_crisis_hits() -> None:
    victim = route_crisis("有人正在打我", prior_self_crisis_hits=1)
    relay = route_crisis("我朋友说他不想活了", prior_self_crisis_hits=1)

    assert victim.script is SUPPORT_VICTIM_SCRIPT
    assert relay.script is SUPPORT_RELAY_SCRIPT


def test_new_session_has_no_prior_self_crisis_hits() -> None:
    assert (
        route_crisis("我不想活了", prior_self_crisis_hits=0).script
        is CRISIS_SELF_SCRIPT
    )


def test_more_protective_local_script_upgrades_a_second_hit() -> None:
    assert (
        more_protective_local_script(
            "我不想活了", CRISIS_SELF_SCRIPT, prior_self_crisis_hits=1
        )
        is CRISIS_SELF_ESCALATION_SCRIPT
    )
    assert more_protective_local_script("我不想活了", CRISIS_SELF_SCRIPT) is None


def test_ordinary_knife_context_is_not_a_crisis() -> None:
    route = route_crisis("我拿着刀切水果")

    assert route.action == "none"
    assert route.script is None


def test_second_self_crisis_with_semantic_evidence_escalates() -> None:
    route = route_crisis(
        "我撑不下去了",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
        prior_self_crisis_hits=1,
    )

    assert route.script is CRISIS_SELF_ESCALATION_SCRIPT
    assert route.evidence_source == "semantic_evidence"


def test_victim_support_tier_notifies_and_attempts_push() -> None:
    route = route_crisis("有人正在打我")

    assert route.action == "support_victim"
    assert route.notify_guardian is True
    assert route.attempt_push is True
    assert route.script is SUPPORT_VICTIM_SCRIPT


def test_relay_support_tier_records_without_push() -> None:
    route = route_crisis("我朋友说他不想活了")

    assert route.action == "support_relay"
    assert route.notify_guardian is True
    assert route.attempt_push is False
    assert route.script is SUPPORT_RELAY_SCRIPT


def test_every_routed_decision_has_a_registry_script() -> None:
    assert {
        decision: script_for_decision(decision) for decision in _DETERMINISTIC_ROUTES
    } == {
        "crisis_self": CRISIS_SELF_SCRIPT,
        "support_victim": SUPPORT_VICTIM_SCRIPT,
        "support_relay": SUPPORT_RELAY_SCRIPT,
    }


def test_helping_another_person_never_notifies_as_the_current_users_crisis() -> None:
    route = route_crisis(
        "同学说他不想活了，我该怎么帮助他？",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
    )

    assert route.action == "support_relay"
    assert route.notify_guardian is True
    assert route.attempt_push is False
    assert route.script is SUPPORT_RELAY_SCRIPT


def test_quoted_fiction_triggers_nothing() -> None:
    route = route_crisis('小说里的人说"我想死"')

    assert route.action == "none"
    assert route.notify_guardian is False
    assert route.attempt_push is False
    assert route.script is None


def test_model_evidence_cannot_execute_without_deterministic_first_person_anchor() -> None:
    assert route_crisis(
        "这篇小说讨论生命的意义",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
    ).action == "none"

    # Phrase-group rules own 找不到活下去的理由; 撑不下去 still needs the anchor.
    assert (
        route_crisis("我真的找不到活下去的理由").evidence_source == "deterministic"
    )
    route = route_crisis(
        "我撑不下去了",
        semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS,
    )
    assert route.action == "crisis_support"
    assert route.attempt_push is True
    assert route.script is CRISIS_SELF_SCRIPT
    assert route.evidence_source == "semantic_evidence"


@pytest.mark.parametrize("query", ("我不在家没关系", "明天我不在学校也没事"))
def test_location_absence_is_not_a_self_crisis_anchor(query: str) -> None:
    assert crisis_semantic_candidate(query) is False
    assert (
        route_crisis(query, semantic_evidence=CrisisSemanticVerdict.SELF_CRISIS).action
        == "none"
    )


def test_semantic_call_candidate_is_bounded_to_unresolved_first_person_risk() -> None:
    assert crisis_semantic_candidate("我撑不下去了") is True
    assert crisis_semantic_candidate("我真的找不到活下去的理由") is False
    assert crisis_semantic_candidate("我已经服药过量") is False
    assert crisis_semantic_candidate("同学找不到活下去的理由") is False


def test_fixed_safety_reply_log_fields_are_template_id_and_time_only() -> None:
    query = "我不想活了"
    route = route_crisis(query)
    fields = fixed_safety_reply_log_fields(route)
    assert fields is not None
    assert fields.keys() == {"reply_template_id", "at"}
    assert fields["reply_template_id"] == "crisis-self-v1"
    assert _UTC_SECOND.fullmatch(fields["at"])
    blob = " ".join(fields.values())
    assert query not in blob
    for phrase in CRISIS_SELF_SCRIPT.phrases:
        assert phrase.spoken not in blob
        assert phrase.display not in blob


def test_log_fixed_safety_reply_emits_structured_line_without_child_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    query = "有人正在打我"
    route = route_crisis(query)
    logger = logging.getLogger("test.fixed_safety_reply")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_fixed_safety_reply(logger, route)
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert re.fullmatch(
        rf"fixed_safety_reply reply_template_id=victim-support-v1 at={_UTC_SECOND.pattern}",
        messages[0],
    )
    assert query not in messages[0]
    for phrase in SUPPORT_VICTIM_SCRIPT.phrases:
        assert phrase.spoken not in messages[0]
        assert phrase.display not in messages[0]


def test_none_route_does_not_log_fixed_safety_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    route = route_crisis('小说里的人说"我想死"')
    logger = logging.getLogger("test.fixed_safety_reply.none")
    with caplog.at_level(logging.INFO, logger=logger.name):
        log_fixed_safety_reply(logger, route)
    assert caplog.records == []
