"""Duplex Orchestrator conversation state machine (ch.10)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from services.agent.src.contracts.errors import IllegalStateTransition
from services.agent.src.contracts.events import StateTransitionEvent
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.observability.metrics import MetricsRegistry


class ConversationState(StrEnum):
    CONNECTING = "connecting"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    EOT_PENDING = "eot_pending"
    # Quiet planning after turn commit, before first audible TTS frame.
    THINKING = "thinking"
    SPEAKING = "speaking"
    INTERRUPTION_PENDING = "interruption_pending"
    TOOL_WAITING = "tool_waiting"
    RECOVERING = "recovering"
    CLOSED = "closed"


class InteractionPhase(StrEnum):
    """Observable duplex phase for UI/logs (P0 naturalness).

    This is a publish/log overlay on top of ConversationState. BACKCHANNEL is
    only for short listener cues and never enters chat history. THINKING_SILENT
    is the quiet window after turn commit until first playback audio.
    """

    CONNECTING = "connecting"
    LISTENING = "listening"
    USER_SPEAKING = "user_speaking"
    BACKCHANNEL = "backchannel"
    THINKING_SILENT = "thinking_silent"
    SPEAKING = "speaking"
    INTERRUPTED = "interrupted"
    TOOL_WAITING = "tool_waiting"
    RECOVERING = "recovering"
    CLOSED = "closed"


class TransitionEvent(StrEnum):
    PREWARM_OK = "prewarm_ok"
    VAD_START = "vad_start"
    VAD_PAUSE_INCOMPLETE = "vad_pause_incomplete"
    USER_RESUME = "user_resume"
    TURN_END = "turn_end"
    FIRST_PHRASE_READY = "first_phrase_ready"
    USER_SPEAKS_DURING_THINK = "user_speaks_during_think"
    USER_VOICE_WHILE_SPEAKING = "user_voice_while_speaking"
    BACKCHANNEL_OR_NOISE = "backchannel_or_noise"
    REAL_INTERRUPT = "real_interrupt"
    STOP_RESPONSE = "stop_response"
    PLAYBACK_DONE = "playback_done"
    PLAYBACK_DONE_TOOLS_ACTIVE = "playback_done_tools_active"
    TOOL_RESULT_VALID = "tool_result_valid"
    # A fenced, non-user-initiated audible source is about to take the floor.
    # This is intentionally separate from TURN_END: no user text or context
    # mutation occurs when an already-authorized output work is resumed.
    OUTPUT_READY = "output_ready"
    USER_CHANGED_TOOL_CONDITIONS = "user_changed_tool_conditions"
    RECOVERABLE_ERROR = "recoverable_error"
    RECOVERED = "recovered"
    SESSION_END = "session_end"


# Legal transitions: (from_state, event) -> to_state
_TRANSITIONS: dict[tuple[ConversationState, TransitionEvent], ConversationState] = {
    (ConversationState.CONNECTING, TransitionEvent.PREWARM_OK): ConversationState.LISTENING,
    (ConversationState.LISTENING, TransitionEvent.VAD_START): ConversationState.USER_SPEAKING,
    (
        ConversationState.USER_SPEAKING,
        TransitionEvent.VAD_PAUSE_INCOMPLETE,
    ): ConversationState.EOT_PENDING,
    (ConversationState.EOT_PENDING, TransitionEvent.USER_RESUME): ConversationState.USER_SPEAKING,
    (ConversationState.EOT_PENDING, TransitionEvent.TURN_END): ConversationState.THINKING,
    (ConversationState.THINKING, TransitionEvent.FIRST_PHRASE_READY): ConversationState.SPEAKING,
    (
        ConversationState.THINKING,
        TransitionEvent.USER_SPEAKS_DURING_THINK,
    ): ConversationState.USER_SPEAKING,
    (
        ConversationState.SPEAKING,
        TransitionEvent.USER_VOICE_WHILE_SPEAKING,
    ): ConversationState.INTERRUPTION_PENDING,
    (
        ConversationState.INTERRUPTION_PENDING,
        TransitionEvent.BACKCHANNEL_OR_NOISE,
    ): ConversationState.SPEAKING,
    (
        ConversationState.INTERRUPTION_PENDING,
        TransitionEvent.REAL_INTERRUPT,
    ): ConversationState.USER_SPEAKING,
    (ConversationState.THINKING, TransitionEvent.STOP_RESPONSE): ConversationState.LISTENING,
    (ConversationState.SPEAKING, TransitionEvent.STOP_RESPONSE): ConversationState.LISTENING,
    (
        ConversationState.INTERRUPTION_PENDING,
        TransitionEvent.STOP_RESPONSE,
    ): ConversationState.LISTENING,
    (ConversationState.TOOL_WAITING, TransitionEvent.STOP_RESPONSE): ConversationState.LISTENING,
    (ConversationState.SPEAKING, TransitionEvent.PLAYBACK_DONE): ConversationState.LISTENING,
    (
        ConversationState.SPEAKING,
        TransitionEvent.PLAYBACK_DONE_TOOLS_ACTIVE,
    ): ConversationState.TOOL_WAITING,
    (ConversationState.TOOL_WAITING, TransitionEvent.TOOL_RESULT_VALID): ConversationState.THINKING,
    (ConversationState.LISTENING, TransitionEvent.OUTPUT_READY): ConversationState.THINKING,
    (ConversationState.TOOL_WAITING, TransitionEvent.OUTPUT_READY): ConversationState.THINKING,
    (
        ConversationState.TOOL_WAITING,
        TransitionEvent.USER_CHANGED_TOOL_CONDITIONS,
    ): ConversationState.USER_SPEAKING,
    (ConversationState.RECOVERING, TransitionEvent.RECOVERED): ConversationState.LISTENING,
}

# From any non-CLOSED state
_GLOBAL_NON_CLOSED: dict[TransitionEvent, ConversationState] = {
    TransitionEvent.RECOVERABLE_ERROR: ConversationState.RECOVERING,
    TransitionEvent.SESSION_END: ConversationState.CLOSED,
}


@dataclass
class TransitionResult:
    from_state: ConversationState
    to_state: ConversationState
    event: TransitionEvent
    fence: GenerationFence
    cause: str


@dataclass
class DuplexStateMachine:
    """Serial state machine; all transitions under apply()."""

    session_id: str
    fence: GenerationFence
    state: ConversationState = ConversationState.CONNECTING
    metrics: MetricsRegistry = field(default_factory=MetricsRegistry)
    history: list[StateTransitionEvent] = field(default_factory=list)
    on_transition: Callable[[TransitionResult], None] | None = None

    def apply(
        self,
        event: TransitionEvent,
        *,
        cause: str = "",
        new_fence: GenerationFence | None = None,
    ) -> TransitionResult:
        if self.state is ConversationState.CLOSED and event is not TransitionEvent.SESSION_END:
            raise IllegalStateTransition(self.state.value, event.value)

        next_state = self._resolve(event)
        if next_state is None:
            raise IllegalStateTransition(self.state.value, event.value)

        if new_fence is not None:
            self.fence = new_fence

        from_state = self.state
        self.state = next_state
        mono = time.monotonic_ns()
        record = StateTransitionEvent(
            session_id=self.session_id,
            from_state=from_state.value,
            to_state=next_state.value,
            event=event.value,
            turn_id=self.fence.turn_id,
            generation_id=self.fence.generation_id,
            tool_epoch=self.fence.tool_epoch,
            monotonic_ns=mono,
            cause=cause,
        )
        self.history.append(record)
        self.metrics.inc_state_transition(from_state.value, next_state.value, event.value)
        result = TransitionResult(
            from_state=from_state,
            to_state=next_state,
            event=event,
            fence=self.fence,
            cause=cause,
        )
        if self.on_transition is not None:
            self.on_transition(result)
        return result

    def _resolve(self, event: TransitionEvent) -> ConversationState | None:
        if self.state is not ConversationState.CLOSED and event in _GLOBAL_NON_CLOSED:
            return _GLOBAL_NON_CLOSED[event]
        return _TRANSITIONS.get((self.state, event))

    def can_transition(self, event: TransitionEvent) -> bool:
        if self.state is ConversationState.CLOSED and event is not TransitionEvent.SESSION_END:
            return False
        return self._resolve(event) is not None


def all_legal_transitions() -> list[tuple[ConversationState, TransitionEvent, ConversationState]]:
    rows: list[tuple[ConversationState, TransitionEvent, ConversationState]] = []
    for (frm, ev), to in _TRANSITIONS.items():
        rows.append((frm, ev, to))
    for st in ConversationState:
        if st is ConversationState.CLOSED:
            continue
        for ev, to in _GLOBAL_NON_CLOSED.items():
            rows.append((st, ev, to))
    return rows
