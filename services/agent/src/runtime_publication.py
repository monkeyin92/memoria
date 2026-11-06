"""Event-publication runtime mixin: UI events, transcripts, and evidence.

``DuplexRuntimePublicationMixin`` is composed into ``DuplexRuntime``; shared
state stays owned by the runtime dataclass and is only declared here for type
checking.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.event_identity import (
    archive_evidence,
    evidence_fingerprint,
    input_policy_for_state,
    join_ui_publishes,
    phase_for_published_state,
    preview_provenance,
    transcript_delta_event,
)
from services.agent.src.orchestration.prosody import mascot_expression_for_reply
from services.agent.src.orchestration.state_machine import InteractionPhase
from services.agent.src.output_provenance import (
    mode_policy_provenance,
    owner_acoustic_evidence,
    speaker_persona_provenance,
)
from services.common.evidence_policy import classify_prompt_kind
from services.common.redaction import redact_pii

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.src.generation_records import GenerationRecords
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.observability.tracing import LatencyTrace
    from services.agent.src.orchestration.orchestrator import Orchestrator
    from services.agent.src.orchestration.prosody import SpeechPlan
    from services.agent.src.orchestration.turn_revision import TurnRevisionTracker
    from services.agent.src.voice_floor import FloorSnapshot, VoiceFloorState
    from services.speaker.domain import SpeakerDecision

logger = logging.getLogger(__name__)


class DuplexRuntimePublicationMixin:
    """Publish UI events, interaction phase, transcripts, and durable evidence."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        orchestrator: Orchestrator
        session_id: str
        barge_in_enabled: bool
        capture_release_holdoff_s: float
        latency_trace: LatencyTrace
        _generation_records: GenerationRecords
        _voice_floor: VoiceFloorState
        _capture_release_task: asyncio.Task[Any] | None
        _input_policy_epoch: int
        _transcript_revisions: TurnRevisionTracker
        _next_user_prompt_kind: str
        _persona_evidence_eligible: bool
        _event_publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None
        _event_sequence: int
        _evidence_publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]] | None
        _owner_turn_publisher: (
            Callable[[dict[str, Any], bytes, int], Coroutine[Any, Any, None]] | None
        )
        _speaker_class: str
        _speaker_decision: SpeakerDecision | None
        _speaker_pcm: bytearray
        _speaker_sample_rate: int
        _mode_policy: ModePolicy
        _assistant_expression_fence: GenerationFence | None

        # Core runtime members consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        @property
        def interaction_phase(self) -> InteractionPhase: ...

        @property
        def floor(self) -> FloorSnapshot: ...

        def mode_policy_for_fence(self, fence: GenerationFence) -> ModePolicy: ...

        def response_provenance_for(
            self,
            fence: GenerationFence,
        ) -> dict[str, Any] | None: ...

        def profile_permits(
            self, fence: GenerationFence, *, capability: str | None = None
        ) -> bool: ...

        def speech_plan_for_fence(self, fence: GenerationFence) -> SpeechPlan: ...

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

    def set_event_publisher(
        self,
        publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._event_publisher = publisher

    def set_evidence_publisher(
        self,
        publisher: Callable[[dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        self._evidence_publisher = publisher

    def set_owner_turn_publisher(
        self,
        publisher: Callable[
            [dict[str, Any], bytes, int],
            Coroutine[Any, Any, None],
        ],
    ) -> None:
        self._owner_turn_publisher = publisher

    def set_interaction_phase(
        self,
        phase: InteractionPhase,
        *,
        cause: str = "",
        publish: bool = True,
    ) -> asyncio.Task[Any] | None:
        cause_s = cause or "unspecified"
        previous = self._write_phase(phase, cause=cause_s)
        if previous is None:
            return None
        self.orchestrator.metrics.inc_interaction_phase(
            previous.value,
            phase.value,
            cause_s,
        )
        # Audio-trace seam so production logs can grep phase without UI events.
        self.mark_audio_event(
            "interaction_phase",
            detail={
                "from": previous.value,
                "to": phase.value,
                "cause": cause_s[:80],
            },
        )
        if not publish:
            return None
        return self.publish_assistant_state(phase.value, phase=phase)

    def _write_phase(self, phase: InteractionPhase, *, cause: str) -> InteractionPhase | None:
        """The one phase writer: floor transition, phase log line, divergence metric."""

        previous = self._voice_floor.set_phase(phase)
        if previous is None:
            return None
        fence = self.fence
        logger.info(
            "interaction_phase from=%s to=%s cause=%s session_id=%s turn_id=%s generation_id=%s",
            previous.value,
            phase.value,
            cause,
            self.session_id,
            fence.turn_id,
            fence.generation_id,
        )
        floor = self.floor
        if floor.divergent:
            self.orchestrator.metrics.inc_voice_floor_divergence(
                floor.conversation_state.value, floor.phase.value, floor.assistant_speaking
            )
        return previous

    def publish_assistant_state(
        self,
        state: str,
        *,
        phase: InteractionPhase | None = None,
    ) -> asyncio.Task[Any] | None:
        fence = self.fence
        mapped_phase = phase or cast(InteractionPhase, phase_for_published_state(state))
        if mapped_phase is not None:
            self._write_phase(mapped_phase, cause=f"publish:{state}")
        state_task = self._publish(
            {
                "type": "assistant_state",
                "session_id": self.session_id,
                "state": state,
                "phase": self.interaction_phase.value,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )
        if self.barge_in_enabled:
            return state_task
        policy = input_policy_for_state(state)
        if policy is None:
            return state_task
        capture_allowed, reason = policy
        if capture_allowed:
            if self._voice_floor.capture_blocked and self.capture_release_holdoff_s > 0:
                self._schedule_capture_release(reason)
                return state_task
            self._voice_floor.update(capture_blocked=False)
            self._cancel_capture_release()
        else:
            self._cancel_capture_release()
            self._voice_floor.update(capture_blocked=True)
        policy_task = self.publish_input_policy(capture_allowed=capture_allowed, reason=reason)
        return join_ui_publishes(cast("DuplexRuntime", self), state_task, policy_task)

    def publish_input_policy(
        self,
        *,
        capture_allowed: bool,
        reason: str,
    ) -> asyncio.Task[Any] | None:
        if not reason:
            raise ValueError("input policy reason is required")
        self._input_policy_epoch += 1
        fence = self.fence
        return self._publish(
            {
                "type": "input_policy",
                "session_id": self.session_id,
                "capture_allowed": capture_allowed,
                "policy_epoch": self._input_policy_epoch,
                "reason": reason,
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )

    def _cancel_capture_release(self) -> None:
        task = self._capture_release_task
        self._capture_release_task = None
        if task is not None:
            task.cancel()

    def _schedule_capture_release(self, reason: str) -> None:
        self._cancel_capture_release()
        delay = self.capture_release_holdoff_s

        async def _release() -> None:
            await asyncio.sleep(delay)
            self._voice_floor.update(capture_blocked=False)
            self._capture_release_task = None
            self.publish_input_policy(capture_allowed=True, reason=reason)

        self._capture_release_task = self._spawn(
            _release(),
            name="capture-release-holdoff",
        )

    def publish_assistant_audio(
        self,
        action: str,
        *,
        gain: float,
    ) -> asyncio.Task[Any] | None:
        if action not in {"duck", "restore"}:
            raise ValueError(f"unsupported assistant audio action: {action}")
        fence = self.fence
        return self._publish(
            {
                "type": "assistant_audio",
                "session_id": self.session_id,
                "action": action,
                "gain": min(1.0, max(0.0, gain)),
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )

    def mark_audio_event(
        self,
        name: str,
        *,
        status: str = "ok",
        detail: dict[str, Any] | None = None,
        mono_ns: int | None = None,
        fence: GenerationFence | None = None,
    ) -> None:
        """Record and publish one server-side first-audio stage without blocking media."""
        if status == "ok":
            self.latency_trace.mark(name, mono_ns=mono_ns)
        event: dict[str, Any] = {
            "type": "audio_trace",
            "source": "agent",
            "session_id": self.session_id,
            "name": name,
            "status": status,
            "at": datetime.now(UTC).isoformat(),
        }
        if fence is not None:
            event.update(
                {
                    "turn_id": fence.turn_id,
                    "generation_id": fence.generation_id,
                    "tool_epoch": fence.tool_epoch,
                }
            )
        if detail:
            event["detail"] = detail
        logger.info(
            "agent_audio_trace name=%s status=%s turn_id=%s generation_id=%s",
            name,
            status,
            None if fence is None else fence.turn_id,
            None if fence is None else fence.generation_id,
        )
        self._publish(event, fence=fence)

    def publish_transcript(
        self,
        *,
        speaker: str,
        text: str,
        final: bool,
        heard: bool | None = None,
        text_delivered: bool = False,
        fence: GenerationFence | None = None,
        archive_fence: GenerationFence | None = None,
        turn_revision: int | None = None,
    ) -> bool:
        original_fence = fence
        fence = fence or self.fence
        archive_fence = archive_fence or fence
        revision = self._transcript_revisions.issue(
            speaker=speaker,
            fence=fence,
            requested=turn_revision,
            final=bool(final and (speaker == "user" or heard is True or text_delivered)),
        )
        if revision is None:
            return False
        disclosure: dict[str, object] | None = None
        if (
            speaker == "assistant"
            and final
            and heard is True
            and self.mode_policy_for_fence(archive_fence).mode == "self_preview"
        ):
            disclosure = preview_provenance(
                self.response_provenance_for(archive_fence), archive_fence
            )
        event = transcript_delta_event(
            session_id=self.session_id,
            speaker=speaker,
            text=text,
            final=final,
            fence=fence,
            revision=revision,
            history_eligible=bool(final and self._generation_records.history_eligible(fence)),
            heard=heard,
            text_delivered=text_delivered,
            preview=disclosure,
        )
        self._publish(event, fence=original_fence)
        if not final or not text.strip():
            return True
        archive_text = redact_pii(text.strip())
        if speaker == "user":
            event_type = "speech.utterance_finalized"
            speaker_class = self._speaker_class
            input_modality = self._generation_records.input_modality_for(archive_fence)
            payload: dict[str, Any] = {
                "text": archive_text,
                "persona_eligible": self._persona_evidence_eligible,
                "prompt_kind": self._next_user_prompt_kind,
            }
            if input_modality == "text":
                payload["input_modality"] = "text"
            self._next_user_prompt_kind = "spontaneous"
            payload.update(self._speaker_persona_provenance())
            payload.update(self._owner_acoustic_evidence())
        elif speaker == "assistant" and heard is True:
            event_type = "assistant.playout_stopped"
            speaker_class = "assistant"
            payload = {"text": archive_text, "actual_heard": True}
            response_provenance = self.response_provenance_for(archive_fence)
            if response_provenance is not None:
                payload["response_provenance"] = response_provenance
            self._next_user_prompt_kind = classify_prompt_kind(archive_text)
        else:
            return True
        payload.update(
            self._mode_policy_provenance(
                archive_fence,
                speaker_class,
                history_eligible=self._generation_records.history_eligible(archive_fence),
                owner_projection_eligible=self._generation_records.owner_projection_eligible(
                    archive_fence
                ),
            )
        )
        decision = self.orchestrator.runtime_profiles.persistence_decision(
            archive_fence, current_fence=self.fence
        )
        fingerprint = evidence_fingerprint(
            session_id=self.session_id,
            event_type=event_type,
            speaker=speaker,
            fence=archive_fence,
            text=archive_text,
        )
        # MemoryWriteFence: evidence carries device + subject revision and the
        # exact verified capability receipt so the archive can re-verify the
        # write against the signed RuntimeProfile.
        if not decision.allowed or original_fence is None:
            # P0-6: without a verified receipt (or caller fence) no archive.
            return True
        self._event_sequence += 1
        evidence = archive_evidence(
            event_type=event_type,
            session_id=self.session_id,
            speaker_class=speaker_class,
            payload=payload,
            source=(
                "generation_fence.actual_heard"
                if speaker == "assistant"
                else (
                    "authenticated_text_input"
                    if payload.get("input_modality") == "text"
                    else "funasr.authoritative_final"
                )
            ),
            fence=archive_fence,
            decision=decision,
            fingerprint=fingerprint,
            event_sequence=self._event_sequence,
        )
        if (
            speaker_class == "owner"
            and payload["owner_projection_eligible"] is True
            and decision.content_ceiling == "verbatim"
            and decision.raw_audio_allowed
            and self._owner_turn_publisher is not None
        ):
            self._spawn(
                self._owner_turn_publisher(
                    evidence,
                    bytes(self._speaker_pcm),
                    self._speaker_sample_rate,
                ),
                name="duplex-evidence-owner-turn",
                durable=True,
            )
        elif self._evidence_publisher is not None:
            self._spawn(
                self._evidence_publisher(evidence),
                name=f"duplex-evidence-{event_type.replace('.', '-')}",
                durable=True,
            )
        return True

    def _speaker_persona_provenance(self) -> dict[str, Any]:
        return speaker_persona_provenance(self._speaker_decision)

    def _mode_policy_provenance(
        self,
        fence: GenerationFence,
        speaker_class: str,
        *,
        history_eligible: bool | None = None,
        owner_projection_eligible: bool | None = None,
        reason_code: str | None = None,
    ) -> dict[str, Any]:
        policy = self.mode_policy_for_fence(fence)
        return mode_policy_provenance(
            policy,
            speaker_class,
            history_eligible=history_eligible,
            owner_projection_eligible=owner_projection_eligible,
            reason_code=reason_code,
        )

    def _owner_acoustic_evidence(self) -> dict[str, int | float]:
        return owner_acoustic_evidence(
            speaker_class=self._speaker_class,
            decision=self._speaker_decision,
            owner_projection_eligible=self._mode_policy.owner_projection_eligible("owner", reason_code=None if self._speaker_decision is None else self._speaker_decision.reason_code),
            profile_permits_memory_capture=self.profile_permits(
                self.fence, capability="memory_capture"
            ),
            pcm=self._speaker_pcm,
            sample_rate=self._speaker_sample_rate,
        )

    def _publish_assistant_expression(self, full_text: str) -> None:
        fence = self.fence
        if self._assistant_expression_fence == fence:
            return
        self._assistant_expression_fence = fence
        self._publish(
            {
                "type": "assistant_expression",
                "session_id": self.session_id,
                "expression": mascot_expression_for_reply(
                    plan=self.speech_plan_for_fence(fence),
                    text=full_text,
                ),
                "turn_id": fence.turn_id,
                "generation_id": fence.generation_id,
                "tool_epoch": fence.tool_epoch,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )
