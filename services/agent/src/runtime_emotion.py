"""Emotion-observation runtime mixin: acoustic segments and speech planning.

``DuplexRuntimeEmotionMixin`` is composed into ``DuplexRuntime``; shared state
stays owned by the runtime dataclass and is only declared here for type
checking.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.emotion import (
    EmotionObservation,
    EmotionSmoother,
    aggregate_acoustic_segments,
)
from services.agent.src.orchestration.prosody import SpeechPlan, speech_plan_for_turn

if TYPE_CHECKING:
    from services.agent.src.mode_policy_client import ModePolicy
    from services.agent.src.orchestration.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class DuplexRuntimeEmotionMixin:
    """Aggregate acoustic emotion evidence and freeze one plan per turn."""

    if TYPE_CHECKING:
        # Shared state owned by the DuplexRuntime dataclass.
        session_id: str
        tts: Any | None
        emotion_smoother: EmotionSmoother
        speech_plan: SpeechPlan
        orchestrator: Orchestrator
        use_paralinguistic_tags: bool
        _speaker_class: str
        _emotion_segments_by_turn: dict[int, list[tuple[str, str]]]
        _speech_plans_by_fence: dict[GenerationFence, SpeechPlan]
        _tts_references_by_fence: dict[GenerationFence, tuple[str, ...]]

        # Core runtime members consumed by this mixin.
        @property
        def fence(self) -> GenerationFence: ...

        @property
        def mode_policy(self) -> ModePolicy: ...

        def apply_speech_plan_to_tts(self, fence: GenerationFence) -> None: ...

        def _publish(
            self,
            event: dict[str, Any],
            *,
            fence: GenerationFence | None = None,
        ) -> asyncio.Task[Any] | None: ...

    def observe_acoustic_emotion(
        self,
        provider_label: str,
        *,
        text: str = "",
        turn_id: int | None = None,
    ) -> None:
        current_turn_id = self.fence.turn_id
        if turn_id is None or turn_id <= current_turn_id or turn_id > current_turn_id + 1:
            return
        segments = self._emotion_segments_by_turn.setdefault(turn_id, [])
        if len(segments) < 8:
            segments.append((provider_label, text[:512]))
        self._emotion_segments_by_turn = {
            key: value
            for key, value in self._emotion_segments_by_turn.items()
            if key > current_turn_id
        }

    def _publish_emotion_observation(
        self,
        observation: EmotionObservation,
        *,
        turn_id: int,
        fence: GenerationFence,
    ) -> None:
        logger.info(
            "emotion_observation label=%s provider_label=%s evidence=%s turn_id=%s",
            observation.label,
            observation.provider_label,
            ",".join(observation.evidence),
            turn_id,
        )
        target_generation_id = fence.generation_id + int(turn_id > fence.turn_id)
        self._publish(
            {
                "type": "emotion_observation",
                "session_id": self.session_id,
                "label": observation.label,
                "provider_label": observation.provider_label,
                "provider_confidence": None,
                "evidence": list(observation.evidence),
                "persist": False,
                "turn_id": turn_id,
                "generation_id": target_generation_id,
                "expires_after_ms": self.emotion_smoother.ttl_ms,
                "at": datetime.now(UTC).isoformat(),
            },
            fence=fence,
        )

    def _apply_speech_plan(
        self,
        user_text: str,
        *,
        turn_id: int,
        fence: GenerationFence,
    ) -> SpeechPlan:
        segments = self._emotion_segments_by_turn.pop(turn_id, [])
        self._emotion_segments_by_turn = {
            key: value for key, value in self._emotion_segments_by_turn.items() if key > turn_id
        }
        acoustic = None
        if segments:
            provider_label, acoustic_text = aggregate_acoustic_segments(segments)
            acoustic = self.emotion_smoother.observe_acoustic(
                provider_label,
                text=acoustic_text,
                turn_id=turn_id,
            )
        observation = self.emotion_smoother.observe_text(user_text, acoustic=acoustic)
        self._publish_emotion_observation(observation, turn_id=turn_id, fence=fence)
        self.speech_plan = speech_plan_for_turn(
            label=observation.label,
            provider_label=observation.provider_label,
            text=user_text,
            evidence=observation.evidence,
            use_markup_tags=self.use_paralinguistic_tags,
            companion_id=self.mode_policy.companion_style_id,
        )
        self._speech_plans_by_fence[fence] = self.speech_plan
        while len(self._speech_plans_by_fence) > 16:
            self._speech_plans_by_fence.pop(next(iter(self._speech_plans_by_fence)))
        apply_plan = getattr(self.tts, "apply_speech_plan", None)
        if callable(apply_plan):
            speaker_scope: Literal["owner", "public"] = (
                "owner" if self._speaker_class == "owner" else "public"
            )
            reference_contexts = self.orchestrator.context.tts_reference_context(
                current_user_final=user_text,
                speaker_scope=speaker_scope,
            )
            self._tts_references_by_fence[fence] = reference_contexts
            while len(self._tts_references_by_fence) > 16:
                self._tts_references_by_fence.pop(next(iter(self._tts_references_by_fence)))
            self.apply_speech_plan_to_tts(fence)
        logger.info(
            "speech_plan_selected emotion=%s dialect=%s tone=%s rate=%.2f pitch=%s "
            "delivery=%s tts_prefix=%s strip=%s turn_id=%s",
            self.speech_plan.voice_emotion,
            self.speech_plan.dialect,
            self.speech_plan.tone,
            self.speech_plan.rate,
            self.speech_plan.pitch,
            self.speech_plan.delivery_mode,
            self.speech_plan.tts_prefix or "-",
            self.speech_plan.strip_paralinguistic,
            turn_id,
        )
        return self.speech_plan
