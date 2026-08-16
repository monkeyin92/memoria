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
    is_backchannel,
    is_completion_ack_only,
    is_explicit_interrupt,
    is_interrupt_command_only,
    is_resume_command_only,
    normalize_short,
)
from services.agent.src.orchestration.speaker_verify import SpeakerGateState
from services.tutor.domain import SessionFocus


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
    # Tutor-focus learning semantics; all remain ordinary LLM turns.
    REQUEST_HINT = "request_hint"
    REQUEST_REPEAT = "request_repeat"
    PACE_CONTROL = "pace_control"
    GIVE_UP = "give_up"
    # Empty / whitespace-only ASR.
    EMPTY = "empty"


class InterruptSemanticVerdict(StrEnum):
    """Narrow evidence returned by the ambiguous-interrupt classifier."""

    CONTROL_ONLY = "CONTROL_ONLY"
    HAS_USER_CONTENT = "HAS_USER_CONTENT"
    UNSURE = "UNSURE"


@dataclass(frozen=True)
class PlaybackUtteranceRoute:
    """Router-owned text semantics for an interruption candidate.

    Acoustic policy may decide whether a candidate is trustworthy, but it
    must not grow a second phrase table.  This projection keeps stop,
    interrupt-with-content, empty and backchannel classification behind the
    same Router boundary used by committed user turns.
    """

    utterance: UtteranceRoute
    backchannel: bool


def route_playback_utterance(
    text: str,
    *,
    duration_ms: int,
    speaker_state: SpeakerGateState | str | None = None,
) -> PlaybackUtteranceRoute:
    """Project playback-time text without assigning acoustic authority."""

    utterance = route_utterance(text, speaker_state=speaker_state)
    return PlaybackUtteranceRoute(
        utterance=utterance,
        backchannel=(
            utterance.intent is UtteranceIntent.CHAT
            and is_backchannel(text, duration_ms=duration_ms)
        ),
    )


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
    formal_non_owner = classification == "guest" or reason_code == "owner_mismatch"
    shadow_non_owner = reason_code == "shadow_guest_candidate"
    if (
        context == "interrupt"
        and explicit_interrupt
        and shadow_non_owner
        and not formal_non_owner
    ):
        # A shadow score from short, playback-contaminated audio is not formal
        # identity evidence. Pure stop remains control-only and reversible.
        return TargetSpeakerRoute(allow_input=True, reason="target_explicit_control")
    non_owner = formal_non_owner or shadow_non_owner
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


_TUTOR_RULES: tuple[tuple[UtteranceIntent, str, tuple[str, ...]], ...] = (
    (
        UtteranceIntent.GIVE_UP,
        "tutor_give_up",
        ("我放弃", "不想学了", "不想做了", "学不下去", "做不下去", "这题算了"),
    ),
    (
        UtteranceIntent.REQUEST_REPEAT,
        "tutor_request_repeat",
        ("再讲一遍", "再说一遍", "重新讲", "没听懂", "没有听懂", "没听清"),
    ),
    (
        UtteranceIntent.PACE_CONTROL,
        "tutor_pace_control",
        ("慢一点", "慢点", "说慢些", "讲慢些", "太快了"),
    ),
    (
        UtteranceIntent.REQUEST_HINT,
        "tutor_request_hint",
        ("我不会", "不会做", "提示一下", "给点提示", "没思路", "没有思路", "卡住了"),
    ),
)


def _tutor_route(
    text: str,
    normalized: str,
    session_focus: SessionFocus | None,
) -> UtteranceRoute | None:
    if session_focus not in {"tutor_english", "tutor_homework"}:
        return None
    compact = "".join(
        character
        for character in text
        if not character.isspace() and character not in "，,。！？!?；;：:"
    )
    for intent, reason, phrases in _TUTOR_RULES:
        if any(phrase in compact for phrase in phrases):
            return UtteranceRoute(
                intent=intent,
                reason=reason,
                enter_chat=True,
                should_interrupt=False,
                speaker_gate_override=False,
                ack_phrase=None,
                normalized_text=normalized,
            )
    return None


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
    semantic_verdict: InterruptSemanticVerdict | None = None,
    session_focus: SessionFocus | None = None,
) -> UtteranceRoute:
    """Classify one utterance. First matching rule wins (see tests for the table).

    Priority (high → low):
      1. speaker PENDING → enroll (blocks all chat, including stop phrases)
      2. resume command while a reply is paused → resume
      3. interrupt-command-only → interrupt_command (no chat, yield/stop ack)
      4. sticky interrupt exact replay → interrupt_replay
      5. semantic control-only evidence → interrupt_command
      6. explicit interrupt + content → interrupt_then_chat
      7. remaining sticky interrupt → monotonic prior route
      8. empty text → empty
      9. frozen tutor-focus rule table → tutor learning intent
      10. default → chat
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

    # 3) Pure control phrases — do not let LLM answer「怎么了？」.  A
    # completion acknowledgement such as「好了，知道了」also ends playback.
    if is_interrupt_command_only(text) or is_completion_ack_only(text):
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

    # 5) A small model may only provide evidence for an already-ambiguous
    # sticky interrupt. The Router remains the sole owner of side effects.
    if (
        sticky_interrupt_route is not None
        and sticky_interrupt_route.intent is UtteranceIntent.INTERRUPT_THEN_CHAT
        and semantic_verdict
        in {
            InterruptSemanticVerdict.CONTROL_ONLY,
            InterruptSemanticVerdict.UNSURE,
        }
    ):
        unsure = semantic_verdict is InterruptSemanticVerdict.UNSURE
        return UtteranceRoute(
            intent=UtteranceIntent.INTERRUPT_COMMAND,
            reason=(
                "interrupt_semantic_unsure"
                if unsure
                else "interrupt_semantic_control_only"
            ),
            enter_chat=False,
            should_interrupt=True,
            speaker_gate_override=True,
            ack_phrase=(
                "刚才没听清，你再说一遍。"
                if unsure
                else "嗯，你说。"
            ),
            normalized_text=normalized,
        )

    # 6) Interrupt wording with real content → barge-in then chat
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

    # 7) Preserve any remaining accepted interrupt across ASR revisions.
    if sticky_interrupt_route is not None and sticky_interrupt_route.should_interrupt:
        return replace(sticky_interrupt_route, normalized_text=normalized)

    # 8) Empty
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

    # 9) Tutor semantics are focus-scoped and never execute side effects here.
    tutor_route = _tutor_route(text, normalized, session_focus)
    if tutor_route is not None:
        return tutor_route

    # 10) Normal chat
    return UtteranceRoute(
        intent=UtteranceIntent.CHAT,
        reason="chat",
        enter_chat=True,
        should_interrupt=False,
        speaker_gate_override=False,
        ack_phrase=None,
        normalized_text=normalized,
    )
