"""Single owner of the runtime's floor state (who holds the conversation floor).

Three sources answer "is the assistant speaking / who holds the floor" and
they may legitimately disagree for a moment:

- ``ConversationState`` of the orchestrator's ``DuplexStateMachine`` (the
  authoritative turn/generation machine, owned by the orchestrator);
- ``InteractionPhase``, the observable UI/log overlay;
- the runtime's playback latch (``assistant_speaking``), which e.g. also
  covers filler audio that plays without a ``ConversationState`` transition.

The enums are deliberately NOT merged.  ``VoiceFloorState`` owns the phase and
every floor scalar of ``DuplexRuntime`` behind one mutation API
(``set_phase`` / ``update``); ``FloorSnapshot`` is the typed, read-only view
consumers use instead of comparing state names.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal

from services.agent.src.orchestration.state_machine import ConversationState, InteractionPhase


class _Keep(Enum):
    KEEP = "keep"


KEEP: Final = _Keep.KEEP
"""Sentinel for ``VoiceFloorState.update``: leave this field unchanged."""

_Kept = Literal[_Keep.KEEP]

_INTERRUPTIBLE_STATES = frozenset(
    {ConversationState.SPEAKING, ConversationState.INTERRUPTION_PENDING}
)
_ASSISTANT_FLOOR_PHASES = frozenset(
    {
        InteractionPhase.THINKING_SILENT,
        InteractionPhase.SPEAKING,
        InteractionPhase.BACKCHANNEL,
        InteractionPhase.INTERRUPTED,
    }
)
_OPEN_PHASES = frozenset({InteractionPhase.CONNECTING, InteractionPhase.LISTENING})
_USER_FLOOR_PHASES = frozenset({InteractionPhase.USER_SPEAKING, InteractionPhase.INTERRUPTED})


@dataclass(frozen=True, slots=True)
class FloorSnapshot:
    """Read-only floor view taken at one instant (never hold across awaits)."""

    conversation_state: ConversationState
    phase: InteractionPhase
    assistant_speaking: bool
    """The runtime playback latch: assistant audio is (believed) audible."""
    fresh_user_speech: bool

    @property
    def interruptible(self) -> bool:
        """The state machine has an assistant reply a stop can interrupt."""

        return self.conversation_state in _INTERRUPTIBLE_STATES

    @property
    def listening_or_connecting(self) -> bool:
        """The observable floor is open (no reply planned, audible or held)."""

        return self.phase in _OPEN_PHASES

    @property
    def assistant_holds_floor(self) -> bool:
        """The assistant speaks, plans, backchannels or just yielded mid-reply."""

        return self.assistant_speaking or self.phase in _ASSISTANT_FLOOR_PHASES

    @property
    def output_floor_allows_assistant(self) -> bool:
        """No fresh user speech and the phase does not hand the floor to the user."""

        return not self.fresh_user_speech and self.phase not in _USER_FLOOR_PHASES

    @property
    def divergent(self) -> bool:
        """The three floor sources disagree on whether the assistant speaks."""

        state_speaking = self.conversation_state in _INTERRUPTIBLE_STATES
        phase_speaking = self.phase is InteractionPhase.SPEAKING
        return not (state_speaking == phase_speaking == self.assistant_speaking)


class VoiceFloorState:
    """Mutable floor facts of one ``DuplexRuntime``; one writer per field."""

    __slots__ = (
        "_assistant_speaking",
        "_capture_blocked",
        "_fresh_user_speech",
        "_last_playback_completed_ns",
        "_pending_assistant_text",
        "_phase",
        "_played_assistant_text",
    )

    def __init__(self) -> None:
        self._phase = InteractionPhase.CONNECTING
        self._assistant_speaking = False
        self._fresh_user_speech = False
        self._pending_assistant_text = ""
        self._played_assistant_text = ""
        self._capture_blocked = False
        self._last_playback_completed_ns: int | None = None

    @property
    def phase(self) -> InteractionPhase:
        return self._phase

    @property
    def assistant_speaking(self) -> bool:
        return self._assistant_speaking

    @property
    def fresh_user_speech(self) -> bool:
        return self._fresh_user_speech

    @property
    def pending_assistant_text(self) -> str:
        return self._pending_assistant_text

    @property
    def played_assistant_text(self) -> str:
        return self._played_assistant_text

    @property
    def capture_blocked(self) -> bool:
        return self._capture_blocked

    @property
    def last_playback_completed_ns(self) -> int | None:
        return self._last_playback_completed_ns

    @property
    def echo_reference_text(self) -> str:
        """Assistant text an echo guard compares user input against."""

        return self._pending_assistant_text or self._played_assistant_text

    def set_phase(self, phase: InteractionPhase) -> InteractionPhase | None:
        """The only phase writer; returns the previous phase when it changed."""

        if phase is self._phase:
            return None
        previous = self._phase
        self._phase = phase
        return previous

    def update(
        self,
        *,
        assistant_speaking: bool | _Kept = KEEP,
        fresh_user_speech: bool | _Kept = KEEP,
        pending_assistant_text: str | _Kept = KEEP,
        played_assistant_text: str | _Kept = KEEP,
        capture_blocked: bool | _Kept = KEEP,
        last_playback_completed_ns: int | None | _Kept = KEEP,
    ) -> None:
        """The only writer of the floor scalars; ``KEEP`` leaves a field as is."""

        if assistant_speaking is not KEEP:
            self._assistant_speaking = assistant_speaking
        if fresh_user_speech is not KEEP:
            self._fresh_user_speech = fresh_user_speech
        if pending_assistant_text is not KEEP:
            self._pending_assistant_text = pending_assistant_text
        if played_assistant_text is not KEEP:
            self._played_assistant_text = played_assistant_text
        if capture_blocked is not KEEP:
            self._capture_blocked = capture_blocked
        if last_playback_completed_ns is not KEEP:
            self._last_playback_completed_ns = last_playback_completed_ns

    def snapshot(self, conversation_state: ConversationState) -> FloorSnapshot:
        return FloorSnapshot(
            conversation_state=conversation_state,
            phase=self._phase,
            assistant_speaking=self._assistant_speaking,
            fresh_user_speech=self._fresh_user_speech,
        )


__all__ = ["KEEP", "FloorSnapshot", "VoiceFloorState"]
