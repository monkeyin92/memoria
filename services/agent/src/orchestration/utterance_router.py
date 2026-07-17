"""Utterance-level intent router for the cascade duplex control plane.

Single ordered rule table for enroll / interrupt / stop / chat so control
paths do not fight via ad-hoc ifs across ``accept_user_turn`` and barge-in.

Leaf matchers (phrase lists, normalize) stay in ``interruption_guard``;
this module owns *priority* and the resulting side-effect policy flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.agent.src.orchestration.interruption_guard import (
    interrupt_ack_phrase,
    is_explicit_interrupt,
    is_interrupt_command_only,
    normalize_short,
)
from services.agent.src.orchestration.speaker_verify import SpeakerGateState


class UtteranceIntent(StrEnum):
    """Control-plane intent for one user utterance."""

    # Voiceprint collection — never becomes a chat turn.
    ENROLL = "enroll"
    # Pure stop/wait command (等等 / 停一下 / 别说了) — ack only, no LLM.
    INTERRUPT_COMMAND = "interrupt_command"
    # Explicit interrupt wording plus real content (等一下我想问…) — interrupt then chat.
    INTERRUPT_THEN_CHAT = "interrupt_then_chat"
    # Normal conversational turn.
    CHAT = "chat"
    # Empty / whitespace-only ASR.
    EMPTY = "empty"


@dataclass(frozen=True)
class UtteranceRoute:
    intent: UtteranceIntent
    reason: str
    """Stable machine reason (also used as accept_user_turn reject reason when suppressed)."""

    enter_chat: bool
    """Whether this text should become an LLM user turn."""

    should_interrupt: bool
    """Whether mid-reply barge-in should confirm interruption (not recover)."""

    speaker_gate_override: bool
    """When True, short/too_short speaker scores must not block owner stop phrases."""

    ack_phrase: str | None
    """Fixed TTS ack when suppressing or yielding (semantic 嗯你说 / 好的)."""

    normalized_text: str


def _as_speaker_state(
    speaker_state: SpeakerGateState | str | None,
) -> SpeakerGateState | None:
    if speaker_state is None:
        return None
    if isinstance(speaker_state, SpeakerGateState):
        return speaker_state
    try:
        return SpeakerGateState(str(speaker_state))
    except ValueError:
        return None


def route_utterance(
    text: str,
    *,
    speaker_state: SpeakerGateState | str | None = None,
) -> UtteranceRoute:
    """Classify one utterance. First matching rule wins (see tests for the table).

    Priority (high → low):
      1. speaker PENDING → enroll (blocks all chat, including stop phrases)
      2. empty text → empty
      3. interrupt-command-only → interrupt_command (no chat, yield/stop ack)
      4. explicit interrupt + content → interrupt_then_chat
      5. default → chat
    """
    normalized = normalize_short(text)
    state = _as_speaker_state(speaker_state)

    # 1) Enrollment speech is never chat — even if ASR looked like「停一下」.
    if state is SpeakerGateState.PENDING:
        return UtteranceRoute(
            intent=UtteranceIntent.ENROLL,
            reason="speaker_enrolling",
            enter_chat=False,
            should_interrupt=False,
            speaker_gate_override=False,
            ack_phrase=None,
            normalized_text=normalized,
        )

    # 2) Empty
    if not normalized:
        return UtteranceRoute(
            intent=UtteranceIntent.EMPTY,
            reason="empty",
            enter_chat=False,
            should_interrupt=False,
            speaker_gate_override=False,
            ack_phrase=None,
            normalized_text=normalized,
        )

    # 3) Pure control phrases — do not let LLM answer「怎么了？」
    if is_interrupt_command_only(text):
        return UtteranceRoute(
            intent=UtteranceIntent.INTERRUPT_COMMAND,
            reason="interrupt_command_only",
            enter_chat=False,
            should_interrupt=True,
            speaker_gate_override=True,
            ack_phrase=interrupt_ack_phrase(text),
            normalized_text=normalized,
        )

    # 4) Interrupt wording with real content → barge-in then chat
    if is_explicit_interrupt(text):
        return UtteranceRoute(
            intent=UtteranceIntent.INTERRUPT_THEN_CHAT,
            reason="interrupt_then_chat",
            enter_chat=True,
            should_interrupt=True,
            speaker_gate_override=True,
            ack_phrase=None,  # content becomes the next turn; no pure-control ack
            normalized_text=normalized,
        )

    # 5) Normal chat
    return UtteranceRoute(
        intent=UtteranceIntent.CHAT,
        reason="chat",
        enter_chat=True,
        should_interrupt=False,
        speaker_gate_override=False,
        ack_phrase=None,
        normalized_text=normalized,
    )
