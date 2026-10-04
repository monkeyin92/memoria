from datetime import UTC, datetime

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import ExtractedClaim, MemoryExtraction
from services.archive.memory_write_policy import (
    EXPLICIT_MEMORY_INTENT,
    MemoryWritePolicy,
    filter_extraction_for_subject,
)


def _event(
    text: str,
    *,
    prompt_kind: str = "spontaneous",
    intent: bool = True,
    owner_projection_eligible: bool = True,
) -> EvidenceEvent:
    payload: dict[str, object] = {
        "text": text,
        "interaction_mode": "companion",
        "prompt_kind": prompt_kind,
        "owner_projection_eligible": owner_projection_eligible,
        "tool_epoch": 0,
    }
    if intent:
        payload["memory_write_intent"] = dict(EXPLICIT_MEMORY_INTENT)
    return EvidenceEvent(
        event_id="write-policy-event",
        account_id="account",
        subject_id="child",
        session_id="session",
        turn_id=1,
        generation_id=1,
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 10, 3, tzinfo=UTC),
        speaker_class="owner",
        source="test",
        payload=payload,
    )


def _extraction(value: str = "我最喜欢蓝色", *, confidence: float = 0.9) -> MemoryExtraction:
    return MemoryExtraction(
        claims=(
            ExtractedClaim(
                domain_category="daily_life",
                subject_key="self",
                predicate="preference",
                value=value,
                confidence=confidence,
            ),
        ),
        extractor_version="test",
    )


def _decide(event: EvidenceEvent, extraction: MemoryExtraction, *, category: str = "minor"):
    return MemoryWritePolicy().decide(event, extraction, subject_category=category)


@pytest.mark.parametrize("prompt_kind", ["spontaneous", "open", "structured", "leading"])
def test_an_explicit_request_is_confirmed_whatever_the_robots_last_line_was(
    prompt_kind: str,
) -> None:
    # 2026-10-02 round 10: the robot ends nearly every line with a small question, so from the
    # second turn on the child's request scored 0.9 x 0.7 = 0.63 and stayed a candidate.
    decision = _decide(_event("帮我记住我最喜欢蓝色。", prompt_kind=prompt_kind), _extraction())

    assert decision.confirmed is True
    assert decision.reason == "explicit-memory-low-risk"
    assert decision.content == "我最喜欢蓝色。"


@pytest.mark.parametrize("prompt_kind", ["spontaneous", "open", "structured", "leading"])
def test_the_extractors_own_confidence_still_has_to_reach_the_bar(prompt_kind: str) -> None:
    decision = _decide(
        _event("帮我记住我最喜欢蓝色。", prompt_kind=prompt_kind), _extraction(confidence=0.85)
    )

    assert decision.confirmed is False
    assert decision.reason == "insufficient_confidence"


def test_a_greeting_echo_ahead_of_the_command_does_not_keep_the_request_a_candidate() -> None:
    event = _event("晚上好，你在？ 帮我记住，我最喜欢蓝色。", prompt_kind="open")

    decision = _decide(event, _extraction())

    assert decision.confirmed is True
    assert decision.content == "我最喜欢蓝色。"


@pytest.mark.parametrize(
    "text",
    [
        # 2026-10-03 round 12, first sentence after wake: a stray 7-character final was merged into the turn.
        "帮我记住我最喜欢绿色。 你告诉我哪个？",
        "帮我记住我最喜欢绿色。你记住了吗？",
        "晚上好，你在？ 帮我记住，我最喜欢绿色。 你记住了吗？",
        # 2026-10-04 round 13: the ASR joined the question to the command with a comma.
        "帮我记住我最喜欢绿色，你记住了吗？",
        "晚上好，你在？ 帮我记住，我最喜欢绿色，你记住了吗？",
    ],
)
def test_a_stray_sentence_after_the_command_does_not_keep_the_request_a_candidate(
    text: str,
) -> None:
    decision = _decide(_event(text, prompt_kind="open"), _extraction("我最喜欢绿色"))

    assert decision.confirmed is True
    assert decision.content == "我最喜欢绿色。"


@pytest.mark.parametrize(
    "text",
    [
        # A sentence that takes the request back, or a second subject, is not a stray remark.
        "帮我记住我最喜欢绿色。算了不用了。",
        "帮我记住我最喜欢绿色。不对，是蓝色。",
        "帮我记住我最喜欢绿色。好。嗯。啊。",
        # The same holds when the ASR joined the sentence to the command with a comma.
        "帮我记住我最喜欢绿色，算了吧？",
        "帮我记住我最喜欢绿色，不对吗？",
        # A clause that names another value is no remark either.
        "帮我记住我最喜欢绿色，还是红色？",
    ],
)
def test_a_sentence_that_takes_the_request_back_keeps_it_a_candidate(text: str) -> None:
    decision = _decide(_event(text), _extraction("我最喜欢绿色"))

    assert decision.confirmed is False


@pytest.mark.parametrize(
    "text",
    [
        "帮我记住我最喜欢绿色。我爸爸叫小明。",
        "帮我记住我最喜欢绿色，你记住我爸爸了吗？",
    ],
)
def test_a_stray_family_sentence_after_the_command_still_drops_the_childs_claim(text: str) -> None:
    event = _event(text)

    kept = filter_extraction_for_subject(
        event, _extraction("我最喜欢绿色"), subject_category="minor"
    )

    assert kept.claims == ()


@pytest.mark.parametrize(
    ("event", "extraction", "reason"),
    [
        (_event("帮我记住我最喜欢蓝色。", intent=False), _extraction(), "explicit_intent_missing"),
        (
            _event("帮我记住我最喜欢蓝色。", owner_projection_eligible=False),
            _extraction(),
            "untrusted_owner_fence",
        ),
        (_event("我最喜欢蓝色。"), _extraction(), "invalid_explicit_command"),
        (
            _event("帮我记住我喜欢恐龙。"),
            _extraction("我喜欢恐龙"),
            "auto_confirm_not_allowlisted",
        ),
        (
            _event("晚上好，你在？ 帮我记住我喜欢恐龙。"),
            _extraction("我喜欢恐龙"),
            "invalid_explicit_command",
        ),
        (
            _event("帮我记住我爸爸喜欢蓝色。"),
            _extraction("我爸爸喜欢蓝色"),
            "minor_long_term_boundary",
        ),
        (
            _event("帮我记住我最喜欢蓝色。"),
            MemoryExtraction(extractor_version="test"),
            "single_claim_required",
        ),
    ],
)
def test_the_other_gates_are_unchanged(
    event: EvidenceEvent, extraction: MemoryExtraction, reason: str
) -> None:
    decision = _decide(event, extraction)

    assert decision.confirmed is False
    assert decision.reason == reason


def test_a_minor_sentence_with_a_hard_context_is_dropped_before_the_policy_sees_it() -> None:
    event = _event("我今天被欺负了。帮我记住我最喜欢蓝色。")

    kept = filter_extraction_for_subject(event, _extraction(), subject_category="minor")

    assert kept.claims == ()


def test_a_glued_greeting_still_leaves_the_claim_to_the_minor_filter() -> None:
    event = _event("晚上好，你在？ 帮我记住，我最喜欢蓝色。", prompt_kind="open")

    kept = filter_extraction_for_subject(event, _extraction(), subject_category="minor")

    assert [claim.value for claim in kept.claims] == ["我最喜欢蓝色"]
