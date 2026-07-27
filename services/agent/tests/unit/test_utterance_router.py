"""Rule-table tests for the utterance control-plane router."""

from __future__ import annotations

import pytest
from services.agent.src.orchestration.speaker_verify import SpeakerGateState
from services.agent.src.orchestration.utterance_router import (
    InterruptSemanticVerdict,
    UtteranceIntent,
    route_speaker_gate,
    route_target_speaker,
    route_utterance,
)


@pytest.mark.parametrize(
    ("text", "speaker_state", "intent", "enter_chat", "should_interrupt", "override", "reason"),
    [
        # 1) enroll wins over everything
        (
            "我是主人请记住我的声音",
            SpeakerGateState.PENDING,
            UtteranceIntent.ENROLL,
            False,
            False,
            False,
            "speaker_enrolling",
        ),
        (
            "停一下",
            SpeakerGateState.PENDING,
            UtteranceIntent.ENROLL,
            False,
            False,
            False,
            "speaker_enrolling",
        ),
        (
            "等等",
            "pending",
            UtteranceIntent.ENROLL,
            False,
            False,
            False,
            "speaker_enrolling",
        ),
        # 2) empty
        ("", None, UtteranceIntent.EMPTY, False, False, False, "empty"),
        ("   ", None, UtteranceIntent.EMPTY, False, False, False, "empty"),
        ("。！？", None, UtteranceIntent.EMPTY, False, False, False, "empty"),
        # 3) pure interrupt commands
        (
            "等等",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.INTERRUPT_COMMAND,
            False,
            True,
            True,
            "interrupt_command_only",
        ),
        (
            "嗯，等等，等等。",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.INTERRUPT_COMMAND,
            False,
            True,
            True,
            "interrupt_command_only",
        ),
        (
            "等下。",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.INTERRUPT_COMMAND,
            False,
            True,
            True,
            "interrupt_command_only",
        ),
        (
            "停一下",
            SpeakerGateState.UNAVAILABLE,
            UtteranceIntent.INTERRUPT_COMMAND,
            False,
            True,
            True,
            "interrupt_command_only",
        ),
        (
            "别说了",
            None,
            UtteranceIntent.INTERRUPT_COMMAND,
            False,
            True,
            True,
            "interrupt_command_only",
        ),
        (
            "暂停",
            None,
            UtteranceIntent.INTERRUPT_COMMAND,
            False,
            True,
            True,
            "interrupt_command_only",
        ),
        # 4) interrupt wording + content → chat after interrupt
        (
            "等一下我想问下周三",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.INTERRUPT_THEN_CHAT,
            True,
            True,
            True,
            "interrupt_then_chat",
        ),
        (
            "等下我想问下周三",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.CHAT,
            True,
            False,
            False,
            "chat",
        ),
        (
            "不是这个意思",
            None,
            UtteranceIntent.INTERRUPT_THEN_CHAT,
            True,
            True,
            True,
            "interrupt_then_chat",
        ),
        # 5) normal chat
        (
            "今天天气怎么样",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.CHAT,
            True,
            False,
            False,
            "chat",
        ),
        (
            "我等下再说",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.CHAT,
            True,
            False,
            False,
            "chat",
        ),
        (
            "等下我",
            SpeakerGateState.ENROLLED,
            UtteranceIntent.CHAT,
            True,
            False,
            False,
            "chat",
        ),
        (
            "讲个故事",
            SpeakerGateState.DISABLED,
            UtteranceIntent.CHAT,
            True,
            False,
            False,
            "chat",
        ),
        (
            "你好",
            SpeakerGateState.UNAVAILABLE,
            UtteranceIntent.CHAT,
            True,
            False,
            False,
            "chat",
        ),
    ],
)
def test_route_table(
    text: str,
    speaker_state: SpeakerGateState | str | None,
    intent: UtteranceIntent,
    enter_chat: bool,
    should_interrupt: bool,
    override: bool,
    reason: str,
) -> None:
    route = route_utterance(text, speaker_state=speaker_state)
    assert route.intent is intent
    assert route.enter_chat is enter_chat
    assert route.should_interrupt is should_interrupt
    assert route.speaker_gate_override is override
    assert route.reason == reason


def test_interrupt_command_ack_phrases() -> None:
    yield_route = route_utterance("停一下", speaker_state=SpeakerGateState.ENROLLED)
    assert yield_route.ack_phrase == "嗯，你说。"

    wait_route = route_utterance("等等", speaker_state=SpeakerGateState.ENROLLED)
    assert wait_route.ack_phrase == "嗯，你说。"

    stop_route = route_utterance("别说了", speaker_state=SpeakerGateState.ENROLLED)
    assert stop_route.ack_phrase == "好的。"

    pause_route = route_utterance("暂停")
    assert pause_route.ack_phrase == "好的。"


def test_interrupt_then_chat_has_no_control_ack() -> None:
    route = route_utterance("等一下我想问个事")
    assert route.intent is UtteranceIntent.INTERRUPT_THEN_CHAT
    assert route.ack_phrase is None
    assert route.enter_chat is True


def test_sticky_interrupt_replay_is_control_not_a_second_chat_turn() -> None:
    sticky = route_utterance("停一下，你叫什么名字？")

    route = route_utterance(
        "你叫什么名字。",
        sticky_interrupt_route=sticky,
        previous_committed_text_normalized="你叫什么名字",
    )

    assert route.intent is UtteranceIntent.INTERRUPT_REPLAY
    assert route.reason == "interrupt_replayed_previous_turn"
    assert route.enter_chat is False
    assert route.should_interrupt is True
    assert route.ack_phrase == "嗯，你说。"


def test_sticky_replay_wins_when_previous_turn_starts_with_interrupt_wording() -> None:
    sticky = route_utterance("停一下，不是这个意思，我重新说")

    route = route_utterance(
        "不是这个意思，我重新说",
        sticky_interrupt_route=sticky,
        previous_committed_text_normalized="不是这个意思，我重新说",
    )

    assert route.intent is UtteranceIntent.INTERRUPT_REPLAY
    assert route.enter_chat is False


def test_repeated_previous_turn_without_sticky_interrupt_remains_chat() -> None:
    route = route_utterance(
        "你叫什么名字？",
        previous_committed_text_normalized="你叫什么名字",
    )

    assert route.intent is UtteranceIntent.CHAT
    assert route.enter_chat is True


def test_sticky_interrupt_keeps_different_final_as_interrupt_then_chat() -> None:
    sticky = route_utterance("停一下，你叫什么名字？")

    route = route_utterance(
        "你今天过得怎么样？",
        sticky_interrupt_route=sticky,
        previous_committed_text_normalized="你叫什么名字",
    )

    assert route.intent is UtteranceIntent.INTERRUPT_THEN_CHAT
    assert route.enter_chat is True
    assert route.normalized_text == "你今天过得怎么样"


def test_semantic_control_only_collapses_polluted_sticky_final_to_control() -> None:
    sticky = route_utterance("停一下，你叫什么名字？")

    route = route_utterance(
        "份停听一下能是据提供的数据和指示来协助。",
        sticky_interrupt_route=sticky,
        semantic_verdict=InterruptSemanticVerdict.CONTROL_ONLY,
    )

    assert route.intent is UtteranceIntent.INTERRUPT_COMMAND
    assert route.reason == "interrupt_semantic_control_only"
    assert route.enter_chat is False
    assert route.should_interrupt is True
    assert route.ack_phrase == "嗯，你说。"


def test_semantic_user_content_preserves_sticky_chat() -> None:
    sticky = route_utterance("停一下，你叫什么名字？")

    route = route_utterance(
        "停一下，你叫什么名字？",
        sticky_interrupt_route=sticky,
        semantic_verdict=InterruptSemanticVerdict.HAS_USER_CONTENT,
    )

    assert route.intent is UtteranceIntent.INTERRUPT_THEN_CHAT
    assert route.reason == "interrupt_then_chat"
    assert route.enter_chat is True


def test_semantic_unsure_fails_closed_and_asks_for_repetition() -> None:
    sticky = route_utterance("停一下，你叫什么名字？")

    route = route_utterance(
        "停听一下",
        sticky_interrupt_route=sticky,
        semantic_verdict=InterruptSemanticVerdict.UNSURE,
    )

    assert route.intent is UtteranceIntent.INTERRUPT_COMMAND
    assert route.reason == "interrupt_semantic_unsure"
    assert route.enter_chat is False
    assert route.should_interrupt is True
    assert route.ack_phrase == "刚才没听清，你再说一遍。"


def test_sticky_interrupt_does_not_override_enrollment_or_new_pure_control() -> None:
    sticky = route_utterance("停一下，你叫什么名字？")

    enroll = route_utterance(
        "你叫什么名字？",
        speaker_state=SpeakerGateState.PENDING,
        sticky_interrupt_route=sticky,
        previous_committed_text_normalized="你叫什么名字",
    )
    stop = route_utterance(
        "别说了",
        sticky_interrupt_route=sticky,
        previous_committed_text_normalized="你叫什么名字",
    )

    assert enroll.intent is UtteranceIntent.ENROLL
    assert stop.intent is UtteranceIntent.INTERRUPT_COMMAND
    assert stop.ack_phrase == "好的。"


def test_sticky_pure_interrupt_survives_empty_final_revision() -> None:
    sticky = route_utterance("停一下")

    route = route_utterance(
        "",
        sticky_interrupt_route=sticky,
        previous_committed_text_normalized="你叫什么名字",
    )

    assert route.intent is UtteranceIntent.INTERRUPT_COMMAND
    assert route.reason == "interrupt_command_only"


def test_paused_reply_resume_trace_routes_back_to_the_interrupted_answer() -> None:
    """Regression from production: TTS ack + pause + resume may share one ASR final."""

    route = route_utterance(
        "好的，好的。 等一下。 继续。",
        resumable_reply=True,
    )

    assert route.intent is UtteranceIntent.RESUME
    assert route.reason == "resume_interrupted_reply"
    assert route.enter_chat is True
    assert route.should_interrupt is False


def test_enrolled_chat_not_blocked_as_enroll() -> None:
    route = route_utterance("我是主人", speaker_state=SpeakerGateState.ENROLLED)
    assert route.intent is UtteranceIntent.CHAT
    assert route.enter_chat is True


def test_route_has_no_duplicate_speaker_authority_policy() -> None:
    route = route_utterance("打开我的人生故事")

    assert not hasattr(route, "permissions")


@pytest.mark.parametrize(
    ("score_reason", "allowed", "reason"),
    [
        ("mismatch", True, "guest_mismatch"),
        ("too_short", False, "too_short"),
        ("embed_failed", False, "embed_failed"),
    ],
)
def test_legacy_speaker_gate_routes_human_mismatch_as_guest(
    score_reason: str,
    allowed: bool,
    reason: str,
) -> None:
    route = route_speaker_gate(score_reason=score_reason)

    assert route.allow_input is allowed
    assert route.reason == reason


@pytest.mark.parametrize(
    (
        "classification",
        "reason_code",
        "profile_id",
        "pcm_duration_ms",
        "context",
        "allowed",
        "reason",
    ),
    [
        (
            "uncertain",
            "shadow_owner_candidate",
            "shadow-1",
            800,
            "conversation",
            True,
            "target_owner",
        ),
        ("guest", "owner_mismatch", "active-1", 800, "conversation", False, "target_non_owner"),
        (
            "uncertain",
            "shadow_guest_candidate",
            "shadow-1",
            800,
            "conversation",
            False,
            "target_non_owner",
        ),
        (
            "uncertain",
            "shadow_owner_candidate",
            "shadow-1",
            500,
            "interrupt",
            False,
            "target_insufficient_speech",
        ),
        (
            "uncertain",
            "ambiguous_score",
            "active-1",
            800,
            "conversation",
            True,
            "target_unconfirmed",
        ),
        (
            "uncertain",
            "shadow_ambiguous_candidate",
            "shadow-1",
            800,
            "conversation",
            True,
            "target_unconfirmed",
        ),
        (
            "uncertain",
            "ambiguous_score",
            "active-1",
            800,
            "interrupt",
            False,
            "target_unconfirmed",
        ),
        (
            "uncertain",
            "shadow_guest_candidate",
            "shadow-1",
            800,
            "interrupt",
            False,
            "target_non_owner",
        ),
        (
            "uncertain",
            "model_unavailable",
            "shadow-1",
            800,
            "interrupt",
            True,
            "target_unavailable",
        ),
        (
            "uncertain",
            "no_active_profile",
            None,
            800,
            "interrupt",
            True,
            "target_profile_absent",
        ),
    ],
)
def test_target_speaker_focus_is_separate_from_authority_permissions(
    classification: str,
    reason_code: str,
    profile_id: str | None,
    pcm_duration_ms: int,
    context: str,
    allowed: bool,
    reason: str,
) -> None:
    route = route_target_speaker(
        classification=classification,
        reason_code=reason_code,
        profile_id=profile_id,
        pcm_duration_ms=pcm_duration_ms,
        context=context,  # type: ignore[arg-type]
    )

    assert route.allow_input is allowed
    assert route.reason == reason


def test_strict_explicit_wait_cannot_be_used_by_non_owner() -> None:
    shadow_command = route_target_speaker(
        classification="uncertain",
        reason_code="shadow_guest_candidate",
        profile_id="shadow-1",
        pcm_duration_ms=320,
        context="interrupt",
        explicit_interrupt=True,
    )
    formal_guest_command = route_target_speaker(
        classification="guest",
        reason_code="owner_mismatch",
        profile_id="formal-guest-1",
        pcm_duration_ms=1200,
        context="interrupt",
        explicit_interrupt=True,
    )
    shadow_ambiguous_command = route_target_speaker(
        classification="uncertain",
        reason_code="shadow_ambiguous_candidate",
        profile_id="shadow-1",
        pcm_duration_ms=320,
        context="interrupt",
        explicit_interrupt=True,
    )

    assert shadow_command.allow_input is False
    assert shadow_command.reason == "target_non_owner"
    assert formal_guest_command.allow_input is False
    assert formal_guest_command.reason == "target_non_owner"
    assert shadow_ambiguous_command.allow_input is True
    assert shadow_ambiguous_command.reason == "target_explicit_control"


@pytest.mark.parametrize(
    ("classification", "reason_code", "context", "explicit_interrupt", "reason"),
    [
        ("guest", "owner_mismatch", "conversation", False, "target_guest_allowed"),
        ("guest", "owner_mismatch", "interrupt", True, "target_guest_allowed"),
        ("uncertain", "shadow_guest_candidate", "conversation", False, "target_guest_allowed"),
        ("uncertain", "shadow_guest_candidate", "interrupt", True, "target_guest_allowed"),
        ("uncertain", "shadow_ambiguous_candidate", "conversation", False, "target_unconfirmed"),
        ("uncertain", "shadow_ambiguous_candidate", "interrupt", True, "target_explicit_control"),
    ],
)
def test_permissive_policy_allows_guest_and_ambiguous_conversation_and_interrupt(
    classification: str,
    reason_code: str,
    context: str,
    explicit_interrupt: bool,
    reason: str,
) -> None:
    route = route_target_speaker(
        classification=classification,
        reason_code=reason_code,
        profile_id="profile-1",
        pcm_duration_ms=800,
        context=context,  # type: ignore[arg-type]
        explicit_interrupt=explicit_interrupt,
        reject_non_owner_voice=False,
    )

    assert route.allow_input is True
    assert route.reason == reason
