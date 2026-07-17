"""Rule-table tests for the utterance control-plane router."""

from __future__ import annotations

import pytest
from services.agent.src.orchestration.speaker_verify import SpeakerGateState
from services.agent.src.orchestration.utterance_router import (
    UtteranceIntent,
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
            "停一下",
            SpeakerGateState.OPEN,
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
            SpeakerGateState.OPEN,
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


def test_enrolled_chat_not_blocked_as_enroll() -> None:
    route = route_utterance("我是主人", speaker_state=SpeakerGateState.ENROLLED)
    assert route.intent is UtteranceIntent.CHAT
    assert route.enter_chat is True
