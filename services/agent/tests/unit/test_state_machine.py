"""All legal and illegal state transitions (ch.10)."""

from __future__ import annotations

import pytest
from services.agent.src.contracts.errors import IllegalStateTransition
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    DuplexStateMachine,
    InteractionPhase,
    TransitionEvent,
    all_legal_transitions,
)


def _sm(state: ConversationState | None = None) -> DuplexStateMachine:
    fence = GenerationFence("s", 0, 0, 0)
    sm = DuplexStateMachine(session_id="s", fence=fence)
    if state is not None:
        sm.state = state
    return sm


def test_all_legal_transitions_succeed() -> None:
    for frm, ev, to in all_legal_transitions():
        sm = _sm(frm)
        result = sm.apply(ev)
        assert result.to_state is to
        assert sm.state is to


def test_illegal_transition_raises() -> None:
    sm = _sm(ConversationState.LISTENING)
    with pytest.raises(IllegalStateTransition):
        sm.apply(TransitionEvent.FIRST_PHRASE_READY)


def test_closed_rejects_non_end() -> None:
    sm = _sm(ConversationState.CLOSED)
    with pytest.raises(IllegalStateTransition):
        sm.apply(TransitionEvent.VAD_START)


def test_happy_path_sequence() -> None:
    sm = _sm()
    sm.apply(TransitionEvent.PREWARM_OK)
    sm.apply(TransitionEvent.VAD_START)
    sm.apply(TransitionEvent.VAD_PAUSE_INCOMPLETE)
    sm.apply(TransitionEvent.TURN_END, new_fence=sm.fence.bump_turn())
    sm.apply(TransitionEvent.FIRST_PHRASE_READY)
    sm.apply(TransitionEvent.PLAYBACK_DONE)
    assert sm.state is ConversationState.LISTENING


def test_interrupt_path() -> None:
    sm = _sm(ConversationState.SPEAKING)
    sm.apply(TransitionEvent.USER_VOICE_WHILE_SPEAKING)
    sm.apply(TransitionEvent.REAL_INTERRUPT, new_fence=sm.fence.bump_generation())
    assert sm.state is ConversationState.USER_SPEAKING


def test_interaction_phase_covers_p0_observable_states() -> None:
    values = {phase.value for phase in InteractionPhase}
    assert {
        "backchannel",
        "thinking_silent",
        "speaking",
        "user_speaking",
        "listening",
    } <= values
