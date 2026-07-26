"""Utterance-level intent router for the cascade duplex control plane.

Single ordered rule table for enroll / interrupt / stop / chat so control
paths do not fight via ad-hoc ifs across ``accept_user_turn`` and barge-in.

Leaf matchers (phrase lists, normalize) stay in ``interruption_guard``;
this module owns *priority* and the resulting side-effect policy flags.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from services.agent.src.orchestration.interruption_guard import (
    interrupt_ack_phrase,
    is_explicit_interrupt,
    is_interrupt_command_only,
    is_resume_command_only,
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
    # A sticky interrupt whose endpoint final only replays the prior user turn.
    INTERRUPT_REPLAY = "interrupt_replay"
    # Continue the answer that the user explicitly paused.
    RESUME = "resume"
    # Normal conversational turn.
    CHAT = "chat"
    # Empty / whitespace-only ASR.
    EMPTY = "empty"


@dataclass(frozen=True)
class SpeakerGateRoute:
    """Control-plane outcome for the legacy acoustic guard.

    The acoustic verifier can distinguish human speech from likely media/noise,
    but it is not identity authority. A clear mismatch therefore stays a
    conversational guest path instead of becoming a muted turn.
    """

    allow_input: bool
    reason: str


def route_speaker_gate(*, score_reason: str) -> SpeakerGateRoute:
    """Map an acoustic score outcome to the single user-input policy table."""

    if score_reason == "mismatch":
        return SpeakerGateRoute(allow_input=True, reason="guest_mismatch")
    return SpeakerGateRoute(allow_input=False, reason=score_reason)


@dataclass(frozen=True)
class TargetSpeakerRoute:
    """Input-focus outcome, separate from SpeakerAuthority permissions."""

    allow_input: bool
    reason: str


def route_target_speaker(
    *,
    classification: str,
    reason_code: str,
    profile_id: str | None,
    pcm_duration_ms: int,
    context: Literal["conversation", "interrupt"] = "conversation",
    explicit_interrupt: bool = False,
    reject_non_owner_voice: bool = True,
) -> TargetSpeakerRoute:
    """Decide whether the current voice may control this account's conversation.

    Shadow CAM++ candidates remain ``uncertain`` for authority. Strict focus
    rejects only a formal mismatch or clear shadow guest; ambiguous candidates
    use the normal unconfirmed conversation/interrupt rules. Disabling the
    interaction filter never upgrades memory, tool, or sensitive-action permissions.
    """

    if reason_code in {"no_active_profile", "authority_unconfigured"}:
        return TargetSpeakerRoute(allow_input=True, reason="target_profile_absent")
    non_owner = classification == "guest" or reason_code in {
        "owner_mismatch",
        "shadow_guest_candidate",
    }
    if non_owner:
        return TargetSpeakerRoute(
            allow_input=not reject_non_owner_voice,
            reason="target_non_owner" if reject_non_owner_voice else "target_guest_allowed",
        )
    if context == "conversation":
        # Shadow decisions are useful for permissions, not identity gating.
        if classification == "owner" or reason_code == "shadow_owner_candidate":
            return TargetSpeakerRoute(allow_input=True, reason="target_owner")
        return TargetSpeakerRoute(allow_input=True, reason="target_unconfirmed")
    if explicit_interrupt:
        # A short command such as「等一下」cannot reliably produce a calibrated
        # CAM++ match. Its intent has already passed the single utterance router;
        # let it yield the floor, but do not treat it as a conversational turn.
        return TargetSpeakerRoute(allow_input=True, reason="target_explicit_control")
    if pcm_duration_ms < 600:
        return TargetSpeakerRoute(allow_input=False, reason="target_insufficient_speech")
    if classification == "owner" or reason_code == "shadow_owner_candidate":
        return TargetSpeakerRoute(allow_input=True, reason="target_owner")
    if reason_code in {
        "model_timeout",
        "model_unavailable",
        "authority_timeout",
        "authority_unavailable",
        "template_unavailable",
        "profile_revoked",
    }:
        return TargetSpeakerRoute(allow_input=True, reason="target_unavailable")
    return TargetSpeakerRoute(allow_input=False, reason="target_unconfirmed")


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
    resumable_reply: bool = False,
    sticky_interrupt_route: UtteranceRoute | None = None,
    previous_committed_text_normalized: str = "",
) -> UtteranceRoute:
    """Classify one utterance. First matching rule wins (see tests for the table).

    Priority (high → low):
      1. speaker PENDING → enroll (blocks all chat, including stop phrases)
      2. resume command while a reply is paused → resume
      3. interrupt-command-only → interrupt_command (no chat, yield/stop ack)
      4. sticky interrupt exact replay → interrupt_replay
      5. explicit interrupt + content → interrupt_then_chat
      6. remaining sticky interrupt → monotonic prior route
      7. empty text → empty
      8. default → chat
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

    # 2) Resume is stateful:「继续」is ordinary chat unless this session owns
    # an explicitly paused reply. Ack echo and the pause phrase may be folded
    # into the same ASR final, so match the whole control-only sequence.
    if resumable_reply and is_resume_command_only(text):
        return UtteranceRoute(
            intent=UtteranceIntent.RESUME,
            reason="resume_interrupted_reply",
            enter_chat=True,
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

    # 4) Streaming ASR may first hear「停一下」and later endpoint only the
    # previous user question after playback contamination. Keep the interrupt
    # monotonic, but do not create a duplicate LLM turn for that replay.
    if (
        sticky_interrupt_route is not None
        and sticky_interrupt_route.intent is UtteranceIntent.INTERRUPT_THEN_CHAT
        and bool(previous_committed_text_normalized)
        and normalized == previous_committed_text_normalized
    ):
        return UtteranceRoute(
            intent=UtteranceIntent.INTERRUPT_REPLAY,
            reason="interrupt_replayed_previous_turn",
            enter_chat=False,
            should_interrupt=True,
            speaker_gate_override=True,
            ack_phrase="嗯，你说。",
            normalized_text=normalized,
        )

    # 5) Interrupt wording with real content → barge-in then chat
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

    # 6) Preserve any remaining accepted interrupt across ASR revisions.
    if sticky_interrupt_route is not None and sticky_interrupt_route.should_interrupt:
        return replace(sticky_interrupt_route, normalized_text=normalized)

    # 7) Empty
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

    # 8) Normal chat
    return UtteranceRoute(
        intent=UtteranceIntent.CHAT,
        reason="chat",
        enter_chat=True,
        should_interrupt=False,
        speaker_gate_override=False,
        ack_phrase=None,
        normalized_text=normalized,
    )
