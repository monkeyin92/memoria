"""Voice-target and TTS-observation mixin for the reply pipeline.

``ReplyVoiceMixin`` keeps the generation-voice matching, response-plan
provenance binding and TTS observation callbacks.  The runtime, response-plan
cache and model/TTS client handles stay owned by ``ReplyPipeline`` and are
declared here for type checking only.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Literal, cast

from services.agent.src import generation_output_policy as output_policy
from services.agent.src.agent_voice_profile import (
    _frozen_designed_fallback,
    bind_generation_tts_voice,
)
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponseVoiceTarget,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from services.agent.src.duplex_runtime import DuplexRuntime, GenerationVoiceSnapshot


class ReplyVoiceMixin:
    """Keep the reply voice in step with its response plan and observe TTS."""

    if TYPE_CHECKING:
        _runtime: DuplexRuntime
        _llm_model: str
        _llm_provider: str
        _tts_provider: str


    @staticmethod
    def _generation_voice_matches_target(
        voice: GenerationVoiceSnapshot | None,
        target: ResponseVoiceTarget,
    ) -> bool:
        expected_kind = "personal" if target.kind == "approved_personal" else "designed"
        return bool(
            voice is not None
            and voice.voice_kind == expected_kind
            and voice.profile_id == target.profile_id
            and voice.resource_id == target.model
        )

    def _ensure_generation_voice_matches_plan(
        self,
        fence: GenerationFence,
        plan: ResponsePlan,
        policy: ModePolicy,
    ) -> bool:
        voice = self._runtime.generation_voice_for(fence)
        if self._generation_voice_matches_target(voice, plan.voice_target):
            return True
        if policy.mode not in {"self_preview", "legacy"} or plan.voice_target.kind != "fallback":
            return False
        fallback = _frozen_designed_fallback(policy)
        if fallback is None or fallback.profile_id != plan.voice_target.profile_id or fallback.model != plan.voice_target.model or self._runtime.tts is None:
            return False
        apply_profile = getattr(self._runtime.tts, "apply_voice_profile", None)
        if not callable(apply_profile):
            return False
        try:
            apply_profile(
                model=fallback.model,
                voice=fallback.voice_id,
                profile_id=fallback.profile_id,
                provider=fallback.provider,
                voice_kind=fallback.voice_kind,
                resource_id=fallback.resource_id,
            )
        except (TypeError, ValueError):
            return False
        self._runtime.apply_speech_plan_to_tts(fence)
        return bind_generation_tts_voice(self._runtime, fence) and self._generation_voice_matches_target(
            self._runtime.generation_voice_for(fence), plan.voice_target
        )

    def _bind_response_plan_provenance(
        self,
        fence: GenerationFence,
        plan: ResponsePlan,
        *,
        llm_model: str | None = None,
    ) -> bool:
        # Plan-level provenance is bound before synthesis. The selected TTS
        # provider appends each spoken sentence's actual engine to the same
        # generation record through ``_observe_tts_sentence``.
        voice = self._runtime.generation_voice_for(fence)
        text_only = self._runtime.input_modality_for_fence(fence) == "text"
        if self._runtime.tts is not None and voice is None and not text_only:
            return False
        policy = self._runtime.mode_policy_for_fence(fence)
        if (
            output_policy.generation_voice_must_match_plan(policy)
            and voice is not None
            and not self._generation_voice_matches_target(voice, plan.voice_target)
        ):
            return False
        payload = plan.provenance.archive_payload(
            fence=fence,
            llm_provider=self._llm_provider,
            llm_model=llm_model or self._llm_model,
            tts_provider=self._tts_provider if voice is not None else None,
            tts_model=voice.resource_id if voice is not None else None,
            actual_voice_profile_id=voice.profile_id if voice is not None else None,
        )
        if voice is not None:
            payload.update(
                {
                    "actual_voice_resource_id": voice.resource_id,
                    "actual_voice_speaker_sha256": voice.speaker_sha256,
                }
            )
            if voice.voice_kind == "personal":
                references = dict(self._runtime.mode_policy_for_fence(fence).references)
                version = references.get("voice_profile_version")
                payload.update(
                    {
                        "actual_voice_profile_version": (
                            int(version) if isinstance(version, str) and version.isdigit() else None
                        ),
                        "actual_voice_provider_expires_at": references.get(
                            "voice_provider_expires_at"
                        ),
                    }
                )
        return self._runtime.bind_response_provenance(fence, payload)

    def _observe_tts_voice_fallback(
        self,
        fence: GenerationFence,
        profile_id: str,
        resource_id: str,
        speaker: str,
        voice_kind: str,
    ) -> None:
        speaker_sha256 = hashlib.sha256(speaker.encode()).hexdigest()
        archive_profile_id = output_policy.generation_voice_profile_id(
            self._runtime.mode_policy_for_fence(fence),
            profile_id=profile_id,
            voice_kind=voice_kind,
        )
        if not self._runtime.bind_generation_voice(
            fence,
            profile_id=archive_profile_id,
            resource_id=resource_id,
            speaker_sha256=speaker_sha256,
            voice_kind=cast(Literal["designed", "personal"], voice_kind),
        ):
            logger.error(
                "voice fallback snapshot rejected session_id=%s turn_id=%s generation_id=%s",
                fence.session_id,
                fence.turn_id,
                fence.generation_id,
            )
            return
        self._runtime.mark_audio_event(
            "voice_generation_fallback",
            status="error",
            detail={
                "voice_profile_id": archive_profile_id,
                "resource_id": resource_id,
                "speaker_sha256": speaker_sha256,
            },
            fence=fence,
        )

    def _observe_tts_sentence(
        self,
        fence: GenerationFence,
        index: int,
        engine: str,
        model: str,
        voice: str,
        switched: bool,
    ) -> None:
        recorded = self._runtime.record_tts_sentence_provenance(
            fence,
            index=index,
            engine=engine,
            model=model,
            voice=voice,
            switched=switched,
        )
        if not recorded:
            logger.warning(
                "tts sentence provenance rejected session_id=%s turn_id=%s "
                "generation_id=%s index=%s",
                fence.session_id,
                fence.turn_id,
                fence.generation_id,
                index,
            )

    def _observe_tts_alignment(
        self,
        fence: GenerationFence,
        utterance_id: str,
        status: str,
    ) -> None:
        if self._runtime.fence.matches(fence):
            self._runtime.heard_tracker.observe_alignment(fence, utterance_id, status)
