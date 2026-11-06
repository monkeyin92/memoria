"""Interrupt runtime mixin: real interrupts, resume binding and control restore.

``DuplexRuntimeInterruptMixin`` is composed into ``DuplexRuntime``; shared state
stays owned by the runtime dataclass and is only declared here for type
checking.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.orchestration.utterance_router import UtteranceIntent

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from typing import Literal

    from services.agent.src.duplex_runtime import ResumeSpeakerBinding
    from services.agent.src.generation_records import GenerationRecords
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.orchestration.orchestrator import Orchestrator
    from services.agent.src.orchestration.speaker_verify import SpeakerVerifier
    from services.agent.src.orchestration.utterance_router import (
        InterruptSemanticVerdict,
        TargetSpeakerRoute,
        UtteranceRoute,
    )
    from services.agent.src.voice_floor import VoiceFloorState
    from services.speaker.domain import SpeakerDecision

logger = logging.getLogger(__name__)


class DuplexRuntimeInterruptMixin:
    """Apply one real interrupt and restore listening or the paused reply binding."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        orchestrator: Orchestrator
        session_id: str
        speaker_verifier: SpeakerVerifier
        tts: Any | None
        _generation_records: GenerationRecords
        _mode_policy: ModePolicy
        _voice_floor: VoiceFloorState
        _speaker_decision: SpeakerDecision | None
        _speaker_classifier: Callable[[bytes, int], Awaitable[SpeakerDecision]] | None
        _speaker_epoch: int
        _speaker_collecting: bool
        _target_speaker_focus_enabled: bool
        _target_focus_epoch: int | None
        _target_focus_pending_epoch: int | None
        _resume_fence: GenerationFence | None
        _reply_speaker_binding: ResumeSpeakerBinding | None
        _paused_reply_available: bool
        _paused_reply_binding: ResumeSpeakerBinding | None
        _pending_semantic_pause_epoch: int | None
        _pending_semantic_pause_binding: ResumeSpeakerBinding | None
        _last_listen_restore_ns: int | None
        CONTROL_RESTORE_SPEECH_EPOCH_GRACE_MS: int

        # Core runtime and sibling-mixin members consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        @property
        def interaction_phase(self) -> InteractionPhase: ...

        def _clear_control_user_turn(
            self,
            *,
            cause: str,
            speech_epoch: int | None = None,
        ) -> None: ...

        def _interrupt_candidate_text(self) -> str: ...

        def _route_candidate(
            self,
            text: str | None = None,
            *,
            semantic_verdict: InterruptSemanticVerdict | None = None,
        ) -> UtteranceRoute: ...

        def _speaker_allows_user_input(self, *, context: str) -> bool: ...

        async def await_speaker_classification(self) -> SpeakerDecision: ...

        def _target_speaker_route(
            self,
            *,
            context: Literal["conversation", "interrupt"],
            explicit_interrupt: bool = False,
        ) -> TargetSpeakerRoute: ...

        def _reject_target_speaker(
            self,
            *,
            context: str,
            route: TargetSpeakerRoute,
        ) -> None: ...

        def mark_audio_event(
            self,
            name: str,
            *,
            status: str = "ok",
            detail: dict[str, Any] | None = None,
            mono_ns: int | None = None,
            fence: GenerationFence | None = None,
        ) -> None: ...

        def publish_assistant_audio(
            self,
            action: str,
            *,
            gain: float,
        ) -> asyncio.Task[Any] | None: ...

        def publish_assistant_state(
            self,
            state: str,
            *,
            phase: InteractionPhase | None = None,
        ) -> asyncio.Task[Any] | None: ...

        def set_interaction_phase(
            self,
            phase: InteractionPhase,
            *,
            cause: str = "",
            publish: bool = True,
        ) -> asyncio.Task[Any] | None: ...

    def is_resume_generation(self, fence: GenerationFence | None = None) -> bool:
        """Whether this generation resumes the answer explicitly paused by the user."""

        candidate = fence or self.fence
        return self._resume_fence is not None and self._resume_fence.matches(candidate)

    def _current_resume_speaker_binding(self) -> ResumeSpeakerBinding | None:
        """Return the verified identity allowed to recover this speaker's reply."""

        decision = self._speaker_decision
        if decision is None or not decision.profile_id:
            return None
        if decision.classification == "owner":
            authority = "owner"
        elif (
            decision.classification == "uncertain"
            and decision.reason_code == "shadow_owner_candidate"
        ):
            authority = "shadow_owner_candidate"
        else:
            return None
        return (
            decision.classification,
            decision.profile_id,
            decision.template_version,
            authority,
        )

    def _within_control_restore_grace(self) -> bool:
        if self._last_listen_restore_ns is None:
            return False
        age_ms = (time.monotonic_ns() - self._last_listen_restore_ns) // 1_000_000
        return age_ms <= self.CONTROL_RESTORE_SPEECH_EPOCH_GRACE_MS

    def _restore_listen_after_control(self, *, cause: str) -> None:
        """After stop/wait, unlock turn commit and publish listening.

        Production logs (2026-07-17): after「停一下」yield finished in ~6ms and
        subsequent user speech was dropped as missing_speech_epoch while
        min_words stayed at PLAYBACK_INPUT_BLOCK_MIN_WORDS (1000).
        """
        self.publish_assistant_audio("restore", gain=1.0)
        self._voice_floor.update(assistant_speaking=False)
        self._voice_floor.update(fresh_user_speech=True)
        self._last_listen_restore_ns = time.monotonic_ns()
        # Always force listening after control so UI/logs match.
        if self.interaction_phase is not InteractionPhase.LISTENING:
            self.set_interaction_phase(
                InteractionPhase.LISTENING,
                cause=f"restore_listen:{cause}",
            )
        self.mark_audio_event(
            "listen_restored_after_control",
            detail={"cause": cause},
        )

    async def _apply_real_interrupt(
        self,
        *,
        cause: str,
        stop_playback: Callable[[], Awaitable[str | None]] | None,
        create_user_turn: bool,
        synchronized_transcript: str | None,
        force_generation_bump: bool,
        candidate: str,
        barge_route: UtteranceRoute,
        mid_reply: bool,
    ) -> GenerationFence:
        owner_cmd = barge_route.speaker_gate_override
        control_only = barge_route.should_interrupt and not barge_route.enter_chat
        if owner_cmd and create_user_turn:
            logger.info(
                "explicit_interrupt_cmd text_len=%s intent=%s cause=%s session_id=%s",
                len(candidate),
                barge_route.intent,
                cause,
                self.session_id,
            )
            self.mark_audio_event(
                "explicit_interrupt_cmd",
                detail={
                    "text_len": len(candidate),
                    "cause": cause,
                    "intent": barge_route.intent,
                },
            )
        old_fence = self.fence
        new_fence = await self.orchestrator.confirm_interruption(
            cause=cause,
            stop_playback=stop_playback,
            create_user_turn=create_user_turn,
            synchronized_transcript=synchronized_transcript,
            force_generation_bump=force_generation_bump,
        )
        self._generation_records.bind_mode_policy(new_fence, self._mode_policy)
        self.publish_assistant_audio("restore", gain=1.0)
        if create_user_turn and control_only:
            self._clear_control_user_turn(cause=f"interrupt:{cause}")
        self._voice_floor.update(
            assistant_speaking=False, pending_assistant_text="", last_playback_completed_ns=None
        )
        if self.tts is not None:
            self.tts.bind_fence(new_fence)
        if not new_fence.matches(old_fence):
            if create_user_turn and mid_reply and control_only:
                self._paused_reply_binding = self._reply_speaker_binding
                self._paused_reply_available = self._paused_reply_binding is not None
                self._pending_semantic_pause_epoch = None
                self._pending_semantic_pause_binding = None
            elif (
                create_user_turn
                and mid_reply
                and barge_route.intent is UtteranceIntent.INTERRUPT_THEN_CHAT
            ):
                self._pending_semantic_pause_epoch = self._speaker_epoch
                self._pending_semantic_pause_binding = self._reply_speaker_binding
                self._paused_reply_available = False
                self._paused_reply_binding = None
            elif create_user_turn:
                self._paused_reply_available = False
                self._paused_reply_binding = None
                self._pending_semantic_pause_epoch = None
                self._pending_semantic_pause_binding = None
            self.publish_assistant_state("interrupted")
            self.set_interaction_phase(
                InteractionPhase.INTERRUPTED,
                cause=f"interrupt:{cause}",
            )
            # A committed close owns the projection and keeps the floor; a
            # mid-reply control-only stop leaves the floor as the interrupt set it.
            yielded_mid_reply = (
                create_user_turn
                and mid_reply
                and control_only
                and cause not in {"user_button", "stop_response", "rtc_recovered"}
            )
            if barge_route.intent is not UtteranceIntent.END_SESSION and not yielded_mid_reply:
                self._restore_listen_after_control(cause=f"interrupt_no_yield:{cause}")
        return new_fence

    async def on_real_interrupt(
        self,
        cause: str = "livekit_interruption",
        *,
        stop_playback: Callable[[], Awaitable[str | None]] | None = None,
        create_user_turn: bool = True,
        synchronized_transcript: str | None = None,
        force_generation_bump: bool = False,
        candidate_text: str | None = None,
        utterance_route: UtteranceRoute | None = None,
    ) -> GenerationFence:
        mid_reply = self._voice_floor.assistant_speaking
        candidate = (
            candidate_text.strip()
            if candidate_text is not None
            else self._interrupt_candidate_text()
        )
        # Barge-in uses the same router as turn-commit so speaker-reject recover
        # cannot fire on pure「停一下」while accept_user_turn treats it as control.
        barge_route = utterance_route or self._route_candidate(candidate)
        owner_cmd = barge_route.speaker_gate_override
        if (
            create_user_turn
            and self._target_speaker_focus_enabled
            and self._speaker_classifier is not None
        ):
            if owner_cmd and self._target_focus_epoch == self._speaker_epoch:
                # A router-confirmed「等一下 / 停一下」must release playback
                # before ASR endpointing finishes. This path is reached only
                # once this speech epoch's voice was already confirmed; an
                # unconfirmed VAD interrupt still waits for endpointed audio
                # below.
                target_route = self._target_speaker_route(
                    context="interrupt",
                    explicit_interrupt=barge_route.intent
                    in {UtteranceIntent.INTERRUPT_COMMAND, UtteranceIntent.END_SESSION})
            else:
                # LiveKit may request an interrupt on VAD start. Do not classify
                # the first few PCM frames: the final transcript handler will
                # decide against the complete endpointed utterance instead.
                if self._speaker_collecting:
                    self._target_focus_pending_epoch = self._speaker_epoch
                    self.mark_audio_event(
                        "target_speaker_waiting_for_endpoint",
                        detail={"cause": cause},
                    )
                    return self.fence
                try:
                    await self.await_speaker_classification()
                except asyncio.CancelledError:
                    return self.fence
                # Unclassified utterance: only a device farewell may pass.
                target_route = self._target_speaker_route(
                    context="interrupt",
                    explicit_interrupt=barge_route.intent is UtteranceIntent.END_SESSION)
            if self._target_focus_pending_epoch == self._speaker_epoch:
                self._target_focus_pending_epoch = None
            if not target_route.allow_input:
                self.speaker_verifier.mark_utterance_end()
                await self.orchestrator.dismiss_pending_interruption(
                    cause="target_speaker_reject_interrupt"
                )
                self._reject_target_speaker(context="interrupt", route=target_route)
                return self.fence
            self._target_focus_epoch = self._speaker_epoch
        if (
            create_user_turn
            and self.speaker_verifier.active
            and not self._speaker_allows_user_input(context="interrupt")
            and not owner_cmd
        ):
            # Nearby talker: do not bump fence — but LiveKit may already have
            # stopped audio, so recover instead of dead silence.
            # Never recover when user said「停一下」etc. — that is a real yield.
            self.speaker_verifier.mark_utterance_end()
            await self.orchestrator.dismiss_pending_interruption(cause="speaker_reject_interrupt")
            self.mark_audio_event(
                "interrupt_blocked_by_speaker",
                status="ignored",
                detail={
                    "cause": cause,
                    "mid_reply": mid_reply,
                    "candidate_len": len(candidate),
                    "intent": barge_route.intent,
                },
            )
            return self.fence

        return await self._apply_real_interrupt(
            cause=cause,
            stop_playback=stop_playback,
            create_user_turn=create_user_turn,
            synchronized_transcript=synchronized_transcript,
            force_generation_bump=force_generation_bump,
            candidate=candidate,
            barge_route=barge_route,
            mid_reply=mid_reply,
        )
