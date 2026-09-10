"""Shared hardware-agnostic interruption policy (plan 8.5, PR-15).

One deterministic decision point for button hard stop, offline stop
keywords, natural barge-in, backchannel and echo/noise false positives.
Text intent is consumed through ``UtteranceRouter`` itself, so device playback
and H5 playback share one semantic rule table instead of each transport
growing its own conditionals.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from services.agent.src.orchestration.utterance_router import (
    UtteranceIntent,
    route_playback_utterance,
)
from services.agent.src.voice_core.interruption.evidence import (
    InterruptionEvidence,
    InterruptionSource,
)

SpeakerProfile = Literal["adult", "child"]


class InterruptionVerdict(StrEnum):
    HARD_STOP = "hard_stop"
    TRUE_INTERRUPT = "true_interrupt"
    BACKCHANNEL = "backchannel"
    FALSE_POSITIVE = "false_positive"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class InterruptionPolicyDecision:
    """Verdict plus the side-effect flags the shared plane consumes.

    The policy itself never executes a side effect; the interaction plane
    remains the single mapper from verdict to duck/cancel/restore actions.
    """

    verdict: InterruptionVerdict
    reason: str
    duck_output: bool
    cancel_generation: bool
    continue_output: bool
    backchannel: bool


@dataclass(slots=True)
class InterruptionPolicy:
    """Deterministic rules; thresholds are initial values for device calibration.

    Hard-stop sources (physical button, offline stop keyword) are never
    blocked by acoustic guards, by speaker identity, or by a safety reply.
    Semantic interruption may be refused for crisis fixed speech, but the
    physical mute and local stop word must always keep working.
    """

    adult_sustained_speech_ms: int = 400
    child_sustained_speech_ms: int = 650
    unverified_aec_sustained_speech_ms: int = 600
    backchannel_max_duration_ms: int = 900
    min_vad_probability: float = 0.45
    max_residual_echo_score: float = 0.65

    def __post_init__(self) -> None:
        for name, value in (
            ("adult_sustained_speech_ms", self.adult_sustained_speech_ms),
            ("child_sustained_speech_ms", self.child_sustained_speech_ms),
            ("unverified_aec_sustained_speech_ms", self.unverified_aec_sustained_speech_ms),
            ("backchannel_max_duration_ms", self.backchannel_max_duration_ms),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0.0 <= self.min_vad_probability <= 1.0:
            raise ValueError("min_vad_probability must be between 0 and 1")
        if not 0.0 <= self.max_residual_echo_score <= 1.0:
            raise ValueError("max_residual_echo_score must be between 0 and 1")

    def sustained_speech_ms(self, speaker_profile: SpeakerProfile) -> int:
        return (
            self.child_sustained_speech_ms
            if speaker_profile == "child"
            else self.adult_sustained_speech_ms
        )

    def evaluate(
        self,
        evidence: InterruptionEvidence,
        *,
        asr_text: str = "",
        local_hard_stop: bool = False,
        speaker_profile: SpeakerProfile = "adult",
        safety_reply: bool = False,
        device_conversation: bool = False,
    ) -> InterruptionPolicyDecision:
        """Evaluate one interruption candidate. First matching rule wins."""

        text = (asr_text or "").strip()
        semantic = route_playback_utterance(
            text,
            duration_ms=evidence.duration_ms,
            device_conversation=device_conversation,
        )
        # 1) A physical button is a hard stop for every profile and every
        #    reply kind: the device already muted locally and the core must
        #    follow, even while a crisis fixed reply is playing.
        if evidence.source is InterruptionSource.BUTTON:
            return self._decide(InterruptionVerdict.HARD_STOP, "physical_button_stop")
        # 2) An offline stop keyword already flushed the device.  The shared
        #    event's authenticated hard_stop bit says whether the local model
        #    classified this keyword as a stop; a wake word must not cancel.
        if evidence.source is InterruptionSource.LOCAL_KWS:
            if local_hard_stop:
                return self._decide(InterruptionVerdict.HARD_STOP, "local_stop_keyword")
            return self._decide(InterruptionVerdict.FALSE_POSITIVE, "local_keyword_not_stop")
        # No active playback means this is an ordinary user turn, not an
        # interruption candidate. Hard-stop sources above still close their
        # local gate idempotently even if Core has not started generation 1.
        if evidence.active_generation_id == 0:
            return self._decide(InterruptionVerdict.FALSE_POSITIVE, "no_active_generation")
        # 3) Explicit stop/interrupt wording wins over acoustic ambiguity,
        #    mirroring UtteranceRouter priority: stop-only, completion ack
        #    and interrupt-plus-content are all user intent.
        if semantic.utterance.intent is UtteranceIntent.INTERRUPT_COMMAND:
            return self._decide(InterruptionVerdict.TRUE_INTERRUPT, "explicit_stop_phrase")
        if semantic.utterance.intent is UtteranceIntent.END_SESSION:
            return self._decide(InterruptionVerdict.TRUE_INTERRUPT, "conversation_end_explicit")
        if semantic.utterance.intent is UtteranceIntent.INTERRUPT_THEN_CHAT:
            return self._decide(InterruptionVerdict.TRUE_INTERRUPT, "explicit_interrupt_phrase")
        # 4) Acoustic false-positive guards: verified AEC residual echo and
        #    weak VAD energy are far-end artefacts, not user speech.
        if evidence.aec_verified:
            residual = evidence.residual_echo_score
            if residual is not None and residual >= self.max_residual_echo_score:
                return self._decide(InterruptionVerdict.FALSE_POSITIVE, "residual_echo")
        if (
            evidence.vad_probability is not None
            and evidence.vad_probability < self.min_vad_probability
        ):
            return self._decide(InterruptionVerdict.FALSE_POSITIVE, "low_vad_probability")
        # 5) Short acknowledgements during playback stay backchannel.
        if semantic.backchannel:
            return self._decide(InterruptionVerdict.BACKCHANNEL, "backchannel")
        # 6) Safety/crisis fixed replies refuse semantic interruption; the
        #    physical mute and local stop word remain available above.
        if safety_reply:
            return self._decide(InterruptionVerdict.FALSE_POSITIVE, "safety_reply_protected")
        # 7) Unconfirmed bystander speech may stop the public answer, but
        #    the turn still passes speaker authority before any private
        #    context, history or memory access.
        if evidence.speaker_class in {"guest", "bystander"}:
            return self._decide(InterruptionVerdict.TRUE_INTERRUPT, "bystander_speech")
        # 8) A non-backchannel cloud transcript is semantic evidence of real
        #    user content. The Router above already removed control phrases;
        #    speaker authority still gates all private context after stopping.
        if evidence.source is InterruptionSource.CLOUD_ASR and text:
            return self._decide(InterruptionVerdict.TRUE_INTERRUPT, "cloud_asr_user_content")
        # 9) Sustained near-end speech is a true interrupt; otherwise the
        #    candidate stays open (duck, keep collecting).
        required_ms = self.sustained_speech_ms(speaker_profile)
        if not evidence.aec_verified:
            required_ms = max(required_ms, self.unverified_aec_sustained_speech_ms)
        if evidence.duration_ms >= required_ms:
            return self._decide(InterruptionVerdict.TRUE_INTERRUPT, "sustained_speech")
        return self._decide(InterruptionVerdict.UNCERTAIN, "barge_in_evidence_pending")

    @staticmethod
    def _decide(verdict: InterruptionVerdict, reason: str) -> InterruptionPolicyDecision:
        return InterruptionPolicyDecision(
            verdict=verdict,
            reason=reason,
            duck_output=verdict is InterruptionVerdict.UNCERTAIN,
            cancel_generation=verdict
            in {InterruptionVerdict.HARD_STOP, InterruptionVerdict.TRUE_INTERRUPT},
            continue_output=verdict
            in {InterruptionVerdict.BACKCHANNEL, InterruptionVerdict.FALSE_POSITIVE},
            backchannel=verdict is InterruptionVerdict.BACKCHANNEL,
        )


__all__ = [
    "InterruptionPolicy",
    "InterruptionPolicyDecision",
    "InterruptionVerdict",
    "SpeakerProfile",
]
