"""User-input authority runtime mixin: speaker gate and interrupt routing.

``DuplexRuntimeInputAuthorityMixin`` is composed into ``DuplexRuntime``; shared
state stays owned by the runtime dataclass and is only declared here for type
checking.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.interruption_guard import PlaybackInputGuard
from services.agent.src.orchestration.speaker_verify import SpeakerGateState, SpeakerVerifier
from services.agent.src.orchestration.utterance_router import (
    InterruptSemanticVerdict,
    UtteranceIntent,
    UtteranceRoute,
    route_speaker_gate,
    route_utterance,
)

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import ResumeSpeakerBinding
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.orchestration.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class DuplexRuntimeInputAuthorityMixin:
    """Authorize user input against the speaker gate, the playback guard and the session policy."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        input_guard: PlaybackInputGuard
        orchestrator: Orchestrator
        session_id: str
        speaker_verifier: SpeakerVerifier
        _speaker_epoch: int
        _sticky_interrupt_epoch: int | None
        _sticky_interrupt_route: UtteranceRoute | None
        _device_conversation_controls_enabled: bool
        _mode_policy: ModePolicy
        _last_committed_user_text_normalized: str
        _paused_reply_available: bool
        _paused_reply_binding: ResumeSpeakerBinding | None

        # Core runtime and sibling-mixin members consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        def conversation_close_needed(self, text: str) -> bool: ...

        def _current_resume_speaker_binding(self) -> ResumeSpeakerBinding | None: ...

        def mark_audio_event(
            self,
            name: str,
            *,
            status: str = "ok",
            detail: dict[str, Any] | None = None,
            mono_ns: int | None = None,
            fence: GenerationFence | None = None,
        ) -> None: ...

        def _publish(
            self,
            event: dict[str, Any],
            *,
            fence: GenerationFence | None = None,
        ) -> asyncio.Task[Any] | None: ...

    def _interrupt_candidate_text(self) -> str:
        return (getattr(self.input_guard, "candidate_text", None) or "").strip()

    def _route_candidate(
        self,
        text: str | None = None,
        *,
        semantic_verdict: InterruptSemanticVerdict | None = None,
    ) -> UtteranceRoute:
        sticky_route = (
            self._sticky_interrupt_route
            if self._sticky_interrupt_epoch == self._speaker_epoch
            else None
        )
        return route_utterance(
            text if text is not None else self._interrupt_candidate_text(),
            speaker_state=self.speaker_verifier.state,
            resumable_reply=(
                self._paused_reply_available
                and self._paused_reply_binding is not None
                and self._paused_reply_binding == self._current_resume_speaker_binding()
            ),
            sticky_interrupt_route=sticky_route,
            previous_committed_text_normalized=(
                self._last_committed_user_text_normalized
                if self.input_guard.candidate_during_playback
                else ""
            ),
            semantic_verdict=semantic_verdict,
            session_focus=self._mode_policy.session_focus,
            device_conversation=self._device_conversation_controls_enabled,
            conversation_close=self.conversation_close_needed(
                text if text is not None else self._interrupt_candidate_text()
            ),
        )

    def _is_explicit_owner_interrupt_cmd(self) -> bool:
        """True when ASR heard stop/yield phrases like「停一下」「等等」."""
        return self._route_candidate().speaker_gate_override

    def _speaker_allows_user_input(self, *, context: str) -> bool:
        if not self.speaker_verifier.enabled:
            return True
        if self.speaker_verifier.state in {
            SpeakerGateState.DISABLED,
            SpeakerGateState.UNAVAILABLE,
        }:
            # Conversation stays open, but formal SpeakerAuthority remains uncertain.
            return True
        # Enrollment speech must not become a normal user turn / interrupt.
        if self.speaker_verifier.state is SpeakerGateState.PENDING:
            if context in {"turn_commit", "interrupt", "barge_in_start"}:
                return False
            return True
        # Explicit stop phrases from the mic: always treat as real interrupt.
        # Short「停一下」often scores too_short and used to wrongly trigger「我继续」.
        if context in {"interrupt", "barge_in_start"} and (
            self._is_explicit_owner_interrupt_cmd()
            or self._route_candidate().intent is UtteranceIntent.END_SESSION):
            return True
        utt = self.speaker_verifier.score_latest_utterance()
        roll = self.speaker_verifier.score_pcm()  # rolling ~4s window
        score = utt
        if utt.reason == "too_short":
            # Prefer rolling window over "allow any short blip" — nearby noise was
            # cancelling TTS while owner was enrolled.
            score = roll
            if score.reason == "too_short":
                # barge_in_start too_short: WAIT for ASR. interrupt: never cancel.
                # turn_commit: fail-closed so tablet/TV fragments cannot enter chat.
                if context == "interrupt":
                    return False
                return True

        # Near-field / media-pollution gates (turn_commit): tablet/TV often rides
        # under a short owner phrase (「等一下」) and used to pass dual-window.
        if context == "turn_commit":
            reject_reason: str | None = None
            if self.speaker_verifier.is_far_field(score):
                reject_reason = "far_field"
            elif self.speaker_verifier.is_media_polluted(score):
                reject_reason = "media_polluted"
            elif score.reason == "far_field":
                reject_reason = "far_field"
            if reject_reason is not None:
                self.orchestrator.metrics.inc_guarded_user_input(f"speaker_{reject_reason}")
                logger.info(
                    "speaker_reject context=%s reason=%s utt=%.3f roll=%.3f "
                    "rms=%.4f ref_rms=%.4f near_ms=%s far_ms=%s speech_ms=%s "
                    "session_id=%s",
                    context,
                    reject_reason,
                    utt.score,
                    roll.score,
                    score.rms,
                    self.speaker_verifier.owner_ref_rms,
                    score.near_ms,
                    score.far_ms,
                    score.speech_ms,
                    self.session_id,
                )
                self.mark_audio_event(
                    "speaker_rejected",
                    status="ignored",
                    detail={
                        "context": context,
                        "reason": reject_reason,
                        "utt": round(utt.score, 4),
                        "roll": round(roll.score, 4),
                        "rms": round(score.rms, 5),
                        "ref_rms": round(self.speaker_verifier.owner_ref_rms, 5),
                        "near_ms": score.near_ms,
                        "far_ms": score.far_ms,
                        "speech_ms": score.speech_ms,
                    },
                )
                self._publish(
                    {
                        "type": "speaker_reject",
                        "session_id": self.session_id,
                        "context": context,
                        "reason": reject_reason,
                        "score": round(score.score, 4),
                        "utt": round(utt.score, 4),
                        "roll": round(roll.score, 4),
                        "speech_ms": score.speech_ms,
                        "at": datetime.now(UTC).isoformat(),
                    },
                    fence=self.fence,
                )
                return False

        thr = float(self.speaker_verifier.accept_threshold)
        # Continuous tablet/TV: long high-duty audio needs a higher match bar.
        media_like = self.speaker_verifier.looks_like_continuous_media(
            roll if roll.speech_ms >= utt.speech_ms else score
        )
        if media_like:
            thr = min(0.92, thr + 0.10)

        dual_ok = utt.reason not in {"too_short", "embed_failed"} and roll.reason not in {
            "too_short",
            "embed_failed",
        }
        # Dual-window consensus: never accept on a single mid-band window.
        # Owner usually scores high on both utterance and rolling PCM.
        best = max(utt.score, roll.score) if dual_ok else score.score
        worst = min(utt.score, roll.score) if dual_ok else score.score
        if context == "turn_commit":
            # Chat turns: both windows must clear thr (tablet video was getting in
            # via single-window score.accepted).
            hard_ok = dual_ok and worst >= thr
        else:
            # Barge-in / interrupt: slightly more lenient so owner can stop TTS.
            hard_ok = score.accepted or (dual_ok and worst >= thr)

        # Soft margin only for short owner turns near thr — not for continuous media.
        soft_floor = thr - 0.02
        soft_ok = (
            context == "turn_commit"
            and not hard_ok
            and dual_ok
            and not media_like
            and score.reason == "mismatch"
            and score.speech_ms >= max(700, int(self.speaker_verifier.min_verify_speech_ms))
            and best >= thr
            and worst >= soft_floor
        )
        if hard_ok or soft_ok:
            logger.info(
                "speaker_accept context=%s soft=%s utt=%.3f roll=%.3f thr=%.3f "
                "near_ms=%s far_ms=%s speech_ms=%s session_id=%s",
                context,
                soft_ok,
                utt.score,
                roll.score,
                thr,
                score.near_ms,
                score.far_ms,
                score.speech_ms,
                self.session_id,
            )
            if soft_ok:
                self.mark_audio_event(
                    "speaker_soft_accept",
                    detail={
                        "context": context,
                        "utt": round(utt.score, 4),
                        "roll": round(roll.score, 4),
                        "threshold": thr,
                        "soft_floor": soft_floor,
                        "speech_ms": score.speech_ms,
                        "near_ms": score.near_ms,
                        "far_ms": score.far_ms,
                    },
                )
            return True

        # A clear human voice mismatch is a guest, not noise. The shared router
        # keeps this conversational path separate from private-memory authority.
        gate_route = route_speaker_gate(score_reason=score.reason)
        if gate_route.allow_input:
            logger.info(
                "speaker_gate_allow reason=%s context=%s utt=%.3f roll=%.3f session_id=%s",
                gate_route.reason,
                context,
                utt.score,
                roll.score,
                self.session_id,
            )
            return True

        self.orchestrator.metrics.inc_guarded_user_input(f"speaker_{score.reason}")
        logger.info(
            "speaker_reject context=%s reason=%s utt=%.3f roll=%.3f "
            "speech_ms=%s thr=%.3f media_like=%s session_id=%s",
            context,
            score.reason,
            utt.score,
            roll.score,
            score.speech_ms,
            thr,
            media_like,
            self.session_id,
        )
        self.mark_audio_event(
            "speaker_rejected",
            status="ignored",
            detail={
                "context": context,
                "reason": score.reason,
                "utt": round(utt.score, 4),
                "roll": round(roll.score, 4),
                "speech_ms": score.speech_ms,
                "threshold": thr,
                "media_like": media_like,
            },
        )
        self._publish(
            {
                "type": "speaker_reject",
                "session_id": self.session_id,
                "context": context,
                "reason": score.reason,
                "score": round(score.score, 4),
                "utt": round(utt.score, 4),
                "roll": round(roll.score, 4),
                "speech_ms": score.speech_ms,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=self.fence,
        )
        return False
