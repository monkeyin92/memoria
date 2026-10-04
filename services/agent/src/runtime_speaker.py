"""Speaker-authority runtime mixin: classification and enrollment.

``DuplexSpeakerMixin`` is composed into ``DuplexRuntime``; shared state stays
owned by the runtime dataclass and is only declared here for type checking.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.event_identity import speaker_classification_evidence
from services.agent.src.orchestration.formal_speaker_enrollment import (
    FormalSpeakerEnrollment,
)
from services.agent.src.orchestration.interaction_plane import (
    InteractionDecision,
    InteractionSnapshot,
)
from services.agent.src.orchestration.interruption_guard import (
    PlaybackInputGuard,
    normalize_short,
)
from services.agent.src.orchestration.orchestrator import Orchestrator
from services.agent.src.orchestration.speaker_verify import (
    SpeakerGateState,
    SpeakerVerifier,
    speech_ms_from_pcm,
)
from services.agent.src.orchestration.speech_epoch_assembler import (
    SpeechEpochAssembler,
)
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.orchestration.utterance_router import (
    InterruptSemanticVerdict,
    TargetSpeakerRoute,
    UtteranceRoute,
    route_target_speaker,
)
from services.speaker.domain import (
    DEVICE_BOUND_SUBJECT_MODEL,
    DEVICE_BOUND_SUBJECT_REASON,
    SpeakerDecision,
    permissions_for_speaker,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.voice_floor import VoiceFloorState

logger = logging.getLogger(__name__)

PLAYBACK_INPUT_BLOCK_MIN_WORDS = 1000
POST_PLAYBACK_SPEAKER_UNTRUSTED_MS = 2_000
POST_PLAYBACK_SPEAKER_PREROLL_MS = 400
POST_PLAYBACK_FORMAL_GUEST_MIN_QUALITY = 0.85
# A farewell heard this soon after playback that the robot itself just said is
# its own tail, not the user: short phrases such as 再见 fall under the
# 4-character text-echo floor, and with no voiceprint nothing else rejects them.
POST_PLAYBACK_CLOSE_ECHO_GUARD_MS = 3_000
# What a device VAD start leaves in place of the previous utterance's decision until the turn commits.
CLASSIFICATION_PENDING = "classification_pending"


class DuplexSpeakerMixin:
    """Speaker classification and formal enrollment."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        session_id: str
        barge_in_enabled: bool
        input_guard: PlaybackInputGuard
        orchestrator: Orchestrator
        speaker_verifier: SpeakerVerifier
        _voice_floor: VoiceFloorState
        _speaker_class: str
        _speaker_decision: SpeakerDecision | None
        _speaker_classifier: Callable[[bytes, int], Awaitable[SpeakerDecision]] | None
        _speaker_sample_rate: int
        _speaker_classify_timeout_s: float
        _speaker_epoch: int
        _speaker_pcm: bytearray
        _speaker_collecting: bool
        _speaker_classification_task: asyncio.Task[Any] | None
        _speaker_pcm_gate_epoch: int | None
        _speaker_pcm_skip_bytes: int
        _speaker_post_playback_untrusted: bool
        _reject_non_owner_voice: bool
        _target_speaker_focus_enabled: bool
        _target_focus_epoch: int | None
        _target_focus_pending_epoch: int | None
        _device_conversation_controls_enabled: bool
        _sticky_interrupt_epoch: int | None
        _sticky_interrupt_route: UtteranceRoute | None
        _sticky_interrupt_text: str
        _interaction_decision_epoch: int | None
        _interaction_decision: InteractionDecision | None
        _interaction_decision_text: str
        _evidence_publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None
        _speech_epoch_assembler: SpeechEpochAssembler
        _enroll_fence: GenerationFence | None
        _formal_enrollment: FormalSpeakerEnrollment
        _formal_enrollment_sample_sink: Callable[[bytes, int], Coroutine[Any, Any, None]] | None
        _enroll_collecting: bool
        _enroll_started_mono: float
        _last_enroll_progress_speech_ms: int
        _mode_policy: ModePolicy

        # Core runtime methods consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        @property
        def current_speaker_decision(self) -> SpeakerDecision: ...

        def profile_permits(
            self, fence: GenerationFence, *, capability: str | None = None
        ) -> bool: ...

        def decide_interaction(
            self, snapshot: InteractionSnapshot
        ) -> InteractionDecision: ...

        def _route_candidate(
            self,
            text: str | None = None,
            *,
            semantic_verdict: InterruptSemanticVerdict | None = None,
        ) -> UtteranceRoute: ...

        def _spawn(
            self,
            coroutine: Coroutine[Any, Any, Any],
            *,
            name: str,
            durable: bool = False,
        ) -> asyncio.Task[Any]: ...

        def _publish(
            self,
            event: dict[str, Any],
            *,
            fence: GenerationFence | None = None,
        ) -> asyncio.Task[Any] | None: ...

        def publish_assistant_state(
            self,
            state: str,
            *,
            phase: InteractionPhase | None = None,
        ) -> asyncio.Task[Any] | None: ...

        def publish_assistant_audio(
            self,
            action: str,
            *,
            gain: float,
        ) -> asyncio.Task[Any] | None: ...

        def mark_audio_event(
            self,
            name: str,
            *,
            status: str = "ok",
            detail: dict[str, Any] | None = None,
            mono_ns: int | None = None,
            fence: GenerationFence | None = None,
        ) -> None: ...

        def _mode_policy_provenance(
            self,
            fence: GenerationFence,
            speaker_class: str,
            *,
            history_eligible: bool | None = None,
            owner_projection_eligible: bool | None = None,
            reason_code: str | None = None,
        ) -> dict[str, Any]: ...

    def feed_speaker_pcm(self, pcm: bytes, *, now_ns: int | None = None) -> None:
        if self._speaker_collecting and pcm:
            if getattr(self, "_speaker_pcm_gate_epoch", None) != self._speaker_epoch:
                self._speaker_pcm_gate_epoch = self._speaker_epoch
                untrusted = self._in_post_playback_speaker_window(now_ns)
                self._speaker_post_playback_untrusted = untrusted
                self._speaker_pcm_skip_bytes = (
                    self._speaker_sample_rate * 2 * POST_PLAYBACK_SPEAKER_PREROLL_MS // 1_000
                    if untrusted
                    else 0
                )
            skip = getattr(self, "_speaker_pcm_skip_bytes", 0)
            if skip:
                dropped = min(skip, len(pcm))
                pcm = pcm[dropped:]
                self._speaker_pcm_skip_bytes = skip - dropped
            if pcm:
                self._speaker_pcm.extend(pcm)
                if len(self._speaker_pcm) > 4 * 1024 * 1024:
                    del self._speaker_pcm[: len(self._speaker_pcm) - 4 * 1024 * 1024]
        # Never enroll assistant TTS that leaks into the mic during playback.
        # After begin_speaker_enrollment we clear the playback latch so user
        # enroll speech is always collected.
        if (
            self.speaker_verifier.state is SpeakerGateState.PENDING
            and self._voice_floor.assistant_speaking
            and not getattr(self, "_enroll_collecting", False)
        ):
            return
        self.speaker_verifier.feed_pcm(pcm)

    def on_user_voice_stopped(self) -> None:
        was_collecting = self._speaker_collecting
        if self._speaker_collecting:
            self.mark_audio_event("last_user_audio")
        self._speaker_collecting = False
        self.speaker_verifier.mark_utterance_end()
        if self.formal_speaker_enrollment_active:
            pcm = bytes(self._speaker_pcm)
            speech_ms = speech_ms_from_pcm(pcm, sample_rate=self._speaker_sample_rate)
            sample = self._formal_enrollment.add_endpoint(pcm)
            logger.info(
                "formal enrollment endpoint session=%s accepted=%s speech_ms=%s "
                "pcm_bytes=%s collecting=%s sample_count=%s",
                self.session_id,
                sample is not None,
                speech_ms,
                len(pcm),
                was_collecting,
                self._formal_enrollment.sample_count,
            )
            if sample is not None and self._formal_enrollment_sample_sink is not None:
                self._spawn(
                    self._formal_enrollment_sample_sink(sample, self._speaker_sample_rate),
                    name="formal-speaker-enrollment-sample",
                )
            self.publish_formal_speaker_enrollment_progress()
        else:
            self._start_speaker_classification()

    def _start_speaker_classification(self) -> None:
        if (
            self._speaker_classifier is None
            or not self._speaker_pcm
            or self._speaker_classification_task is not None
        ):
            return
        epoch = self._speaker_epoch
        pcm = bytes(self._speaker_pcm)
        self._speaker_classification_task = self._spawn(
            self._classify_speaker(epoch, pcm),
            name=f"speaker-authority-{epoch}",
        )

    def _reset_speaker_classification_task(self) -> None:
        previous = self._speaker_classification_task
        if previous is not None and not previous.done():
            previous.cancel()
        self._speaker_classification_task = None

    async def _classify_speaker(self, epoch: int, pcm: bytes) -> SpeakerDecision:
        assert self._speaker_classifier is not None
        try:
            decision = await asyncio.wait_for(
                self._speaker_classifier(pcm, self._speaker_sample_rate),
                timeout=self._speaker_classify_timeout_s,
            )
        except TimeoutError:
            decision = self._uncertain_speaker_decision("authority_timeout")
        except Exception:
            logger.warning("speaker authority unavailable", exc_info=True)
            decision = self._uncertain_speaker_decision("authority_unavailable")
        if epoch != self._speaker_epoch:
            return decision
        if self._should_keep_post_playback_guest_unconfirmed(decision):
            decision = replace(
                decision,
                classification="uncertain",
                reason_code="post_playback_untrusted",
                permissions=permissions_for_speaker("uncertain"),
            )
            logger.info(
                "post-playback speaker mismatch held as uncertain session=%s "
                "score=%s quality=%.3f",
                self.session_id,
                decision.score,
                decision.quality_score,
            )
        self._speaker_decision = decision
        self._speaker_class = decision.classification
        self._publish_speaker_decision(epoch, decision)
        return decision

    def _in_post_playback_speaker_window(self, now_ns: int | None) -> bool:
        completed_ns = self._voice_floor.last_playback_completed_ns
        if completed_ns is None:
            return False
        now = now_ns if now_ns is not None else time.monotonic_ns()
        elapsed_ms = (now - completed_ns) // 1_000_000
        return 0 <= elapsed_ms <= POST_PLAYBACK_SPEAKER_UNTRUSTED_MS

    def _should_keep_post_playback_guest_unconfirmed(self, decision: SpeakerDecision) -> bool:
        if not getattr(self, "_speaker_post_playback_untrusted", False):
            return False
        if decision.classification != "guest" and decision.reason_code != "owner_mismatch":
            return False
        return decision.quality_score < POST_PLAYBACK_FORMAL_GUEST_MIN_QUALITY

    def _close_phrase_is_playback_echo(
        self,
        text: str,
        *,
        now_ns: int | None = None,
    ) -> bool:
        if not self._device_conversation_controls_enabled:
            return False
        completed_ns = self._voice_floor.last_playback_completed_ns
        if completed_ns is None:
            return False
        elapsed_ms = ((now_ns or time.monotonic_ns()) - completed_ns) // 1_000_000
        if not 0 <= elapsed_ms <= POST_PLAYBACK_CLOSE_ECHO_GUARD_MS:
            return False
        phrase = normalize_short(text)
        return bool(phrase) and phrase in normalize_short(self._voice_floor.played_assistant_text)

    def _uncertain_speaker_decision(self, reason: str) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=None,
            quality_score=0.0,
            reason_code=reason,
            model_version="unavailable",
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("uncertain"),
        )

    def _target_speaker_route(
        self,
        *,
        context: Literal["conversation", "interrupt"],
        explicit_interrupt: bool = False,
    ) -> TargetSpeakerRoute:
        if not self._target_speaker_focus_enabled:
            return TargetSpeakerRoute(allow_input=True, reason="target_focus_disabled")
        decision = self._speaker_decision or self._uncertain_speaker_decision(
            "authority_unconfigured"
            if self._speaker_classifier is None
            else "classification_pending"
        )
        pcm_duration_ms = int(len(self._speaker_pcm) * 1000 / max(1, self._speaker_sample_rate * 2))
        return route_target_speaker(
            classification=decision.classification,
            reason_code=decision.reason_code,
            profile_id=decision.profile_id,
            pcm_duration_ms=pcm_duration_ms,
            context=context,
            explicit_interrupt=explicit_interrupt,
            reject_non_owner_voice=self._reject_non_owner_voice,
        )

    def _reject_target_speaker(
        self,
        *,
        context: str,
        route: TargetSpeakerRoute,
    ) -> None:
        self.orchestrator.metrics.inc_guarded_user_input(route.reason)
        self.mark_audio_event(
            "target_speaker_rejected",
            detail={
                "context": context,
                "reason": route.reason,
                "pcm_duration_ms": int(
                    len(self._speaker_pcm) * 1000 / max(1, self._speaker_sample_rate * 2)
                ),
            },
        )

    def _publish_speaker_decision(self, epoch: int, decision: SpeakerDecision) -> None:
        if self._evidence_publisher is None:
            return
        event = speaker_classification_evidence(
            session_id=self.session_id,
            epoch=epoch,
            decision=decision,
            turn_id=self.fence.turn_id + 1,
            generation_id=self.fence.generation_id,
            provenance=self._mode_policy_provenance(
                self.fence,
                decision.classification,
                reason_code=decision.reason_code,
            ),
        )
        self._spawn(
            self._evidence_publisher(event),
            name="duplex-evidence-speaker-classified",
            durable=True,
        )

    def _device_bound_subject_decision(self) -> SpeakerDecision | None:
        """Owner data authority for the one person this device is bound to.

        Only when no voiceprint runs on a device conversation: the signed
        Runtime Profile must confirm its active subject outside unknown_safe
        and grant ``memory_recall_private`` for the current fence. Control API
        re-checks the same profile before it honours the claim.
        """

        if self._speaker_classifier is not None or not self._device_conversation_controls_enabled:
            return None
        verified = self._mode_policy.runtime_profile
        if verified is None:
            return None
        profile = verified.profile
        if (
            profile.speaker_state != "confirmed"
            or profile.active_subject_id is None
            or profile.service_mode == "unknown_safe"
            or not self.profile_permits(self.fence, capability="memory_recall_private")
        ):
            return None
        return SpeakerDecision(
            classification="owner",
            score=None,
            quality_score=0.0,
            reason_code=DEVICE_BOUND_SUBJECT_REASON,
            model_version=DEVICE_BOUND_SUBJECT_MODEL,
            template_version=None,
            profile_id=None,
            permissions=permissions_for_speaker("owner"),
        )

    def prospective_speaker_decision(self) -> SpeakerDecision:
        """The speaker decision the next commit will hold, as far as the signed profile alone decides it.

        ``await_speaker_classification`` gives a device conversation without a voiceprint the bound person's
        decision from the profile and nothing else, but only when a turn commits.  Until then the runtime holds
        none (a new session) or the ``classification_pending`` placeholder (a device VAD start resets it), so
        work that starts while the end-of-speech grace runs, the bound person's memory (TODOLIST N-14 10), found
        no speaker to start for.  A decision the runtime already holds is returned as it is; with a voiceprint
        classifier the decision depends on the audio and nothing is predicted.
        """

        decision = self._speaker_decision
        if decision is not None and decision.reason_code != CLASSIFICATION_PENDING:
            return decision
        return self._device_bound_subject_decision() or self.current_speaker_decision

    async def await_speaker_classification(self) -> SpeakerDecision:
        self._start_speaker_classification()
        task = self._speaker_classification_task
        if task is None:
            bound = self._device_bound_subject_decision()
            if bound is not None:
                self._speaker_decision = bound
                self._speaker_class = "owner"
                return bound
            decision = self._uncertain_speaker_decision(
                "authority_unconfigured" if self._speaker_classifier is None else "no_audio"
            )
            self._speaker_decision = decision
            self._speaker_class = "uncertain"
            return decision
        result = await asyncio.shield(task)
        if not isinstance(result, SpeakerDecision):  # pragma: no cover - task contract guard
            return self._uncertain_speaker_decision("authority_invalid")
        return result

    def begin_speaker_enrollment(self) -> None:
        # Fixed session.say may leave the playback latch stuck True (playback_finished
        # non-interrupt path used to not clear it). Clear so enroll PCM is fed.
        self._voice_floor.update(assistant_speaking=False)
        self._enroll_collecting = True
        self._enroll_fence = self.fence
        self._enroll_started_mono = time.monotonic()
        self.speaker_verifier.begin_enrollment()
        self.publish_assistant_state("speaker_enroll")
        self.mark_audio_event("speaker_enroll_started")

    @property
    def formal_speaker_enrollment_active(self) -> bool:
        return self._formal_enrollment.active

    def set_formal_speaker_enrollment_sample_sink(
        self,
        sink: Callable[[bytes, int], Coroutine[Any, Any, None]] | None,
    ) -> None:
        self._formal_enrollment_sample_sink = sink

    def begin_formal_speaker_enrollment(self, *, target_samples: int = 4) -> None:
        if target_samples != self._formal_enrollment.target_samples:
            self._formal_enrollment = FormalSpeakerEnrollment(
                target_samples=target_samples,
                sample_rate=self._speaker_sample_rate,
            )
        self._voice_floor.update(assistant_speaking=False)
        self._formal_enrollment.begin()
        self.publish_assistant_state("speaker_enroll")
        self.mark_audio_event(
            "formal_speaker_enroll_started",
            detail={"target_samples": self._formal_enrollment.target_samples},
        )
        self.publish_formal_speaker_enrollment_progress()

    def end_formal_speaker_enrollment(self, *, reason: str = "completed") -> None:
        self._formal_enrollment.cancel()
        self._formal_enrollment_sample_sink = None
        self.mark_audio_event("formal_speaker_enroll_finished", detail={"reason": reason})

    def publish_formal_speaker_enrollment_progress(self) -> asyncio.Task[Any] | None:
        state = "complete" if self._formal_enrollment.complete else "collecting"
        return self._publish(
            {
                "type": "speaker_enroll_progress",
                "session_id": self.session_id,
                "state": state,
                "sample_index": self._formal_enrollment.sample_count,
                "sample_count": self._formal_enrollment.sample_count,
                "target_samples": self._formal_enrollment.target_samples,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=self.fence,
        )

    def publish_formal_speaker_enrollment_result(
        self,
        *,
        accepted: bool,
        reason: str,
        profile_id: str | None = None,
        status: str | None = None,
    ) -> asyncio.Task[Any] | None:
        return self._publish(
            {
                "type": "speaker_enroll_result",
                "session_id": self.session_id,
                "accepted": accepted,
                "reason": reason,
                "profile_id": profile_id,
                "status": status,
                "sample_count": self._formal_enrollment.sample_count,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=self.fence,
        )

    def poll_speaker_enrollment(self, *, force: bool = False) -> dict[str, object] | None:
        wall_ms: int | None = None
        started = getattr(self, "_enroll_started_mono", None)
        if started is not None:
            wall_ms = int((time.monotonic() - started) * 1000)
        result = self.speaker_verifier.try_finalize_enrollment(
            force=force,
            wall_elapsed_ms=wall_ms,
        )
        if result is None:
            # Avoid spamming LiveKit data channel every poll tick.
            progress = self.speaker_verifier.enrollment_progress()
            speech_ms = int(progress["speech_ms"])
            last = getattr(self, "_last_enroll_progress_speech_ms", -1)
            if speech_ms - last >= 500 or speech_ms == 0 and last < 0:
                self._last_enroll_progress_speech_ms = speech_ms
                self._publish(
                    {
                        "type": "speaker_enroll_progress",
                        "session_id": self.session_id,
                        "state": progress["state"],
                        "speech_ms": progress["speech_ms"],
                        "target_ms": progress["target_ms"],
                        "elapsed_ms": progress["elapsed_ms"],
                        "at": datetime.now(UTC).isoformat(),
                    },
                    fence=self._enroll_fence or self.fence,
                )
            return None
        payload = {
            "type": "speaker_enroll_result",
            "session_id": self.session_id,
            "accepted": result.accepted,
            "reason": result.reason,
            "score": round(result.score, 4),
            "speech_ms": result.speech_ms,
            "state": self.speaker_verifier.state.value,
            "at": datetime.now(UTC).isoformat(),
        }
        self._publish(payload, fence=self._enroll_fence or self.fence)
        self._enroll_collecting = False
        self._enroll_fence = None
        self.mark_audio_event(
            "speaker_enrolled" if result.reason == "enrolled" else "speaker_enroll_open",
            detail={"reason": result.reason, "speech_ms": result.speech_ms},
        )
        logger.info(
            "speaker_enroll result=%s state=%s speech_ms=%s session_id=%s",
            result.reason,
            self.speaker_verifier.state.value,
            result.speech_ms,
            self.session_id,
        )
        return payload
