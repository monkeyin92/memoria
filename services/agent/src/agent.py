"""LiveKit AgentSession entrypoint with Duplex Orchestrator wiring (ch.11, ch.8–18)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from collections.abc import AsyncGenerator, AsyncIterable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from services.agent.src.config import load_turn_timing
from services.agent.src.context_assembler import (
    ContextAssembler,
    heard_only_chat_context,
)
from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import (
    DuplexRuntime,
    GenerationVoiceSnapshot,
    KeywordSpotterBinding,
)
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.utterance_router import InterruptSemanticVerdict
from services.agent.src.prompts import VOICE_SYSTEM_PROMPT
from services.agent.src.providers.doubao_voice_catalog import resolve_approved_voice
from services.agent.src.providers.interrupt_semantic_classifier import (
    InterruptSemanticClassifier,
    InterruptSemanticClassifierConfig,
)
from services.agent.src.response_planner_client import (
    CANONICAL_PLANNER_POLICY_VERSION,
    Disclosure,
    ResponsePlan,
    ResponsePlannerClient,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.src.voice_profile_client import VoiceProfileClient, VoiceRuntimeProfile
from services.common.companions import DESIGNED_VOICE_MODEL, companion_definition
from services.common.miniprogram_gateway_ticket import (
    MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    MINIPROGRAM_AEC_FAILED,
    MINIPROGRAM_AEC_FAILED_ACK,
    MINIPROGRAM_AEC_HEALTH_ACK_TOPIC,
    MINIPROGRAM_AEC_HEALTH_TOPIC,
    MINIPROGRAM_AGENT_DISPATCH_METADATA,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Voice replies stay shorter than chat, but 96/3 cut creative answers mid-stream.
MAX_VOICE_REPLY_SENTENCES = 8
MAX_VOICE_REPLY_CHARS = 320
# Longer budget when user asks for writing / plans / multi-step content.
MAX_VOICE_REPLY_CHARS_LONGFORM = 560
MAX_VOICE_REPLY_SENTENCES_LONGFORM = 12
_LONGFORM_HINTS = (
    "创作",
    "写一",
    "写个",
    "写段",
    "故事",
    "小说",
    "文案",
    "诗",
    "歌词",
    "详细",
    "完整",
    "长一点",
    "继续写",
    "大纲",
    "方案",
    "计划",
    "步骤",
)
_SENTENCE_ENDINGS = frozenset("。！？；!?")
TELEMETRY_TOPIC = "voice-agent.telemetry"
CASCADE_OPUS_MAX_BITRATE = 64_000
_LOCAL_SAFE_REFUSAL_INSTRUCTIONS = "禁止生成普通回答；仅返回固定安全拒答。"
_LOCAL_SAFE_REFUSAL_TEXT = "当前模式暂时无法安全生成回答。"
try:
    from livekit import agents, rtc
    from livekit.agents import Agent, AgentSession, StopResponse, llm, room_io
    from livekit.agents.types import TimedString
    from livekit.plugins import openai, silero

    _HAS_LIVEKIT = True
except ImportError:  # pragma: no cover
    _HAS_LIVEKIT = False
    agents = None  # type: ignore[assignment]
    rtc = None  # type: ignore[assignment]
    llm = None  # type: ignore[assignment]
    TimedString = str  # type: ignore[misc, assignment]


def build_cascade_audio_output_options() -> Any:
    """Keep 24 kHz TTS PCM while avoiding low-bitrate Opus metallic artifacts."""
    if not _HAS_LIVEKIT:
        raise RuntimeError("LiveKit is unavailable")
    publish_options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    publish_options.audio_encoding.max_bitrate = CASCADE_OPUS_MAX_BITRATE
    return room_io.AudioOutputOptions(
        sample_rate=24000,
        num_channels=1,
        track_publish_options=publish_options,
    )


def apply_miniprogram_session_audio_policy(
    session_kwargs: dict[str, Any],
    dispatch_metadata: object,
) -> bool:
    """Disable LiveKit's warmup only when this job has gateway AEC."""
    if not is_miniprogram_aec_session(dispatch_metadata):
        return False
    session_kwargs["aec_warmup_duration"] = None
    return True


def is_miniprogram_aec_session(dispatch_metadata: object) -> bool:
    return dispatch_metadata == MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA


def is_miniprogram_session(dispatch_metadata: object) -> bool:
    return isinstance(dispatch_metadata, str) and dispatch_metadata in {
        MINIPROGRAM_AGENT_DISPATCH_METADATA,
        MINIPROGRAM_AEC_AGENT_DISPATCH_METADATA,
    }


def build_keyword_spotter_pcm_observer(
    runtime: DuplexRuntime,
    spotter: Any,
) -> Callable[[bytes], None]:
    """Decode one fenced VAD epoch and accept only its complete result."""

    active_binding: KeywordSpotterBinding | None = None
    closed_binding: KeywordSpotterBinding | None = None

    def _observe(pcm: bytes) -> None:
        nonlocal active_binding, closed_binding
        binding = runtime.keyword_spotter_binding()
        if binding == closed_binding:
            return
        if binding != active_binding:
            spotter.reset()
            active_binding = binding
            closed_binding = None
        if binding is None:
            return
        spotter.feed_pcm(pcm)

    def _finalize(binding: KeywordSpotterBinding | None) -> None:
        nonlocal active_binding, closed_binding
        if binding is None or binding != active_binding:
            spotter.reset()
            active_binding = None
            closed_binding = None
            return
        keyword = spotter.finish_utterance()
        active_binding = None
        closed_binding = binding
        if keyword:
            runtime.observe_keyword_spotter_hit(keyword, binding=binding)

    runtime.set_keyword_spotter_finalizer(_finalize)
    return _observe


def _chunk_text(chunk: Any) -> str:
    """Extract plain text from LLM stream items for fence gating."""
    if isinstance(chunk, str):
        return chunk
    delta = getattr(chunk, "delta", None)
    if delta is not None:
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
    return ""


def _message_text(message: Any) -> str:
    content = getattr(message, "text_content", None)
    if callable(content):
        content = content()
    if isinstance(content, str):
        return content
    if isinstance(message, str):
        return message
    raw = getattr(message, "content", "")
    if isinstance(raw, list):
        return "\n".join(part for part in raw if isinstance(part, str))
    return str(raw or "")


def _frozen_designed_fallback(policy: ModePolicy | None) -> VoiceRuntimeProfile | None:
    if policy is None or policy.mode not in {"self_preview", "legacy"}:
        return None
    references = dict(policy.references)
    profile_id = references.get("fallback_voice_profile_id")
    model = references.get("fallback_voice_model")
    resource_id = references.get("fallback_voice_resource_id")
    provider = references.get("fallback_voice_provider")
    voice = (
        resolve_approved_voice(profile_id=profile_id, model=model)
        if isinstance(profile_id, str) and isinstance(model, str)
        else None
    )
    if (
        voice is None
        or not isinstance(profile_id, str)
        or not isinstance(model, str)
        or not isinstance(resource_id, str)
        or not isinstance(provider, str)
        or provider != "volcengine_doubao"
        or model != DESIGNED_VOICE_MODEL
        or resource_id != DESIGNED_VOICE_MODEL
    ):
        return None
    return VoiceRuntimeProfile(
        profile_id=profile_id,
        model=model,
        voice_id=voice,
        provider=provider,
        voice_kind="designed",
        resource_id=resource_id,
    )


def _apply_cached_voice_profile(
    *,
    tts_plugin: Any,
    client: VoiceProfileClient,
    session_id: str,
    mode: str | None = None,
    policy: ModePolicy | None = None,
) -> None:
    profile = client.cached(session_id=session_id)
    references = dict(policy.references) if policy is not None else {}
    selected_fallback = _frozen_designed_fallback(policy)
    if profile is None:
        profile = selected_fallback
    if profile is None:
        baseline = getattr(tts_plugin, "use_baseline_voice", None)
        if callable(baseline):
            baseline()
        return
    personal_matches = (
        profile.voice_kind == "personal"
        and profile.profile_id == references.get("voice_profile_id")
        and profile.provider == references.get("voice_provider")
        and profile.model == references.get("voice_model")
        and profile.resource_id == references.get("voice_resource_id")
        and profile.speaker_sha256 == references.get("voice_speaker_sha256")
    )
    designed_fallback_matches = (
        profile.voice_kind == "designed"
        and profile.profile_id == references.get("fallback_voice_profile_id")
        and profile.provider == references.get("fallback_voice_provider")
        and profile.model == references.get("fallback_voice_model")
        and profile.resource_id == references.get("fallback_voice_resource_id")
    )
    legacy_personal_allowed = references.get("legacy_voice_allowed") is True
    if (
        mode == "legacy"
        and not (designed_fallback_matches or (legacy_personal_allowed and personal_matches))
        and selected_fallback is not None
    ):
        profile = selected_fallback
        designed_fallback_matches = True
    if (
        (mode == "companion" and profile.voice_kind != "designed")
        or (mode == "self_preview" and not (personal_matches or designed_fallback_matches))
        or (
            mode == "legacy"
            and not (designed_fallback_matches or (legacy_personal_allowed and personal_matches))
        )
    ):
        baseline = getattr(tts_plugin, "use_baseline_voice", None)
        if callable(baseline):
            baseline()
        return
    apply_profile = getattr(tts_plugin, "apply_voice_profile", None)
    if callable(apply_profile):
        try:
            if profile.voice_kind == "personal":
                configure_fallback = getattr(
                    tts_plugin,
                    "configure_personal_fallback",
                    None,
                )
                clear_fallback = getattr(tts_plugin, "clear_personal_fallback", None)
                if selected_fallback is not None and callable(configure_fallback):
                    configure_fallback(
                        profile_id=selected_fallback.profile_id,
                        provider=selected_fallback.provider,
                        model=selected_fallback.model,
                        resource_id=selected_fallback.resource_id,
                        voice=selected_fallback.voice_id,
                    )
                elif callable(clear_fallback):
                    clear_fallback()
            apply_profile(
                model=profile.model,
                voice=profile.voice_id,
                profile_id=profile.profile_id,
                provider=profile.provider,
                voice_kind=profile.voice_kind,
                resource_id=profile.resource_id,
            )
        except TypeError:
            if profile.voice_kind == "designed":
                try:
                    apply_profile(model=profile.model, voice=profile.voice_id)
                    return
                except (TypeError, ValueError):
                    pass
            baseline = getattr(tts_plugin, "use_baseline_voice", None)
            if callable(baseline):
                baseline()
            logger.warning(
                "resolved voice profile rejected; restored baseline profile_id=%s",
                profile.profile_id,
            )
        except ValueError:
            baseline = getattr(tts_plugin, "use_baseline_voice", None)
            if callable(baseline):
                baseline()
            logger.warning(
                "resolved voice profile rejected; restored baseline profile_id=%s",
                profile.profile_id,
            )


def should_enable_legacy_speaker_verifier(settings: Any, *, offline: bool) -> bool:
    """Keep the old per-session enrollment separate from formal authority."""
    enabled = bool(getattr(settings, "speaker_verify_enabled", False))
    authority_enabled = bool(getattr(settings, "speaker_authority_enabled", False))
    if enabled and authority_enabled:
        logger.warning(
            "legacy speaker enrollment disabled because formal speaker authority is enabled"
        )
    return enabled and not authority_enabled and not offline


def _heard_only_chat_context(chat_ctx: Any, heard_assistant: list[str]) -> Any:
    return heard_only_chat_context(chat_ctx, heard_assistant)


class DuplexVoiceAgent(Agent if _HAS_LIVEKIT else object):  # type: ignore[misc]
    """Agent that gates LLM/TTS through GenerationFence and tracks active tasks."""

    def __init__(
        self,
        *,
        instructions: str,
        runtime: DuplexRuntime,
        # Kept as ignored compatibility keywords for older callers.  Realtime
        # never creates or reads the legacy persona/memory clients.
        persona_client: Any = None,
        memory_context_client: Any = None,
        voice_profile_client: VoiceProfileClient | None = None,
        response_planner_client: ResponsePlannerClient | None = None,
        llm_provider: str = "unknown",
        llm_model: str = "unknown",
        tts_provider: str = "unknown",
        tts_model: str = "unknown",
        actual_voice_profile_id: str | None = None,
    ) -> None:
        if _HAS_LIVEKIT:
            super().__init__(instructions=instructions)
        self._runtime = runtime
        _ = (persona_client, memory_context_client)
        self._voice_profile_client = voice_profile_client
        self._response_planner_client = response_planner_client
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._tts_provider = tts_provider
        self._tts_model = tts_model
        self._legacy_actual_voice_profile_id = actual_voice_profile_id
        self._context_assembler = ContextAssembler()
        self._llm_text_buf = ""
        self._response_plan_by_fence: dict[tuple[str, int, int, int], ResponsePlan] = {}
        alignment_setter = getattr(runtime.tts, "set_alignment_callback", None)
        if callable(alignment_setter):
            alignment_setter(self._observe_tts_alignment)
        fallback_setter = getattr(runtime.tts, "set_voice_fallback_callback", None)
        if callable(fallback_setter):
            fallback_setter(self._observe_tts_voice_fallback)

    @staticmethod
    def _response_plan_key(fence: GenerationFence) -> tuple[str, int, int, int]:
        return (fence.session_id, fence.turn_id, fence.generation_id, fence.tool_epoch)

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
        if (
            fallback is None
            or fallback.profile_id != plan.voice_target.profile_id
            or fallback.model != plan.voice_target.model
            or self._runtime.tts is None
        ):
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
        return self._bind_current_tts_voice(fence) and self._generation_voice_matches_target(
            self._runtime.generation_voice_for(fence), plan.voice_target
        )

    def _bind_response_plan_provenance(
        self,
        fence: GenerationFence,
        plan: ResponsePlan,
    ) -> bool:
        voice = self._runtime.generation_voice_for(fence)
        if self._runtime.tts is not None and voice is None:
            return False
        policy = self._runtime.mode_policy_for_fence(fence)
        if (
            policy.mode in {"self_preview", "legacy"}
            and voice is not None
            and not self._generation_voice_matches_target(voice, plan.voice_target)
        ):
            return False
        payload = plan.provenance.archive_payload(
            fence=fence,
            llm_provider=self._llm_provider,
            llm_model=self._llm_model,
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

    def _bind_current_tts_voice(self, fence: GenerationFence) -> bool:
        tts_plugin = self._runtime.tts
        if tts_plugin is None:
            return False
        profile_id = getattr(tts_plugin, "current_voice_profile_id", None)
        resource_id = getattr(tts_plugin, "current_model", None)
        speaker = getattr(tts_plugin, "current_voice", None)
        voice_kind = getattr(tts_plugin, "current_voice_kind", None)
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or not isinstance(resource_id, str)
            or not resource_id
            or not isinstance(speaker, str)
            or not speaker
            or not isinstance(voice_kind, str)
            or not voice_kind
        ):
            return False
        archive_profile_id = self._archive_voice_profile_id(
            fence,
            profile_id=profile_id,
            voice_kind=voice_kind,
        )
        return self._runtime.bind_generation_voice(
            fence,
            profile_id=archive_profile_id,
            resource_id=resource_id,
            speaker_sha256=hashlib.sha256(speaker.encode()).hexdigest(),
            voice_kind=cast(Literal["designed", "personal"], voice_kind),
        )

    def _archive_voice_profile_id(
        self,
        fence: GenerationFence,
        *,
        profile_id: str,
        voice_kind: str,
    ) -> str | None:
        if voice_kind == "personal":
            return profile_id
        mode = self._runtime.mode_policy_for_fence(fence).mode
        return profile_id if mode in {"companion", "self_preview", "legacy"} else None

    def _observe_tts_voice_fallback(
        self,
        fence: GenerationFence,
        profile_id: str,
        resource_id: str,
        speaker: str,
        voice_kind: str,
    ) -> None:
        speaker_sha256 = hashlib.sha256(speaker.encode()).hexdigest()
        archive_profile_id = self._archive_voice_profile_id(
            fence,
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

    @staticmethod
    def _is_local_safe_plan(plan: ResponsePlan) -> bool:
        return plan.provenance.planner_policy_version == "local-safe-fallback-v1"

    def _plan_matches_mode_policy(
        self,
        plan: ResponsePlan,
        policy: ModePolicy,
    ) -> bool:
        """Allow a plan only when it is bound to this fence's frozen policy."""

        provenance = plan.provenance
        references = dict(policy.references)
        if (
            policy.mode is None
            or provenance.interaction_mode != policy.mode
            or provenance.mode_policy_version != policy.policy_version
        ):
            return False
        if self._is_local_safe_plan(plan):
            companion_safe = policy.mode == "companion" and plan.direct_text is None
            refusal_safe = (
                policy.mode in {"self_preview", "legacy"}
                and plan.instructions == _LOCAL_SAFE_REFUSAL_INSTRUCTIONS
                and plan.direct_text == _LOCAL_SAFE_REFUSAL_TEXT
                and not plan.grounded_items
                and not plan.provenance.source_refs
                and plan.disclosures
                == (
                    ("digital_identity", "privacy_refusal", "unknown")
                    if policy.mode == "legacy"
                    else ("privacy_refusal", "unknown")
                )
                and plan.provenance.disclosures == plan.disclosures
            )
            relationship_version = provenance.relationship_profile_version
            relationship_safe = provenance.relationship_profile_id == references.get(
                "relationship_profile_id"
            ) and (
                str(relationship_version) if relationship_version is not None else None
            ) == references.get("relationship_profile_version")
            frozen_snapshot_safe = (
                policy.mode == "companion"
                and provenance.digital_self_version_id is None
                and provenance.manifest_sha256 is None
                and provenance.relationship_profile_id is None
                and provenance.relationship_profile_version is None
                and self._legacy_provenance_absent(provenance)
            ) or (
                policy.mode in {"self_preview", "legacy"}
                and provenance.digital_self_version_id == references.get("digital_self_version_id")
                and provenance.manifest_sha256 == references.get("manifest_sha256")
                and relationship_safe
                and (
                    self._legacy_provenance_absent(provenance)
                    if policy.mode == "self_preview"
                    else self._legacy_provenance_matches(provenance, references)
                )
            )
            return (
                (companion_safe or refusal_safe)
                and frozen_snapshot_safe
                and self._fallback_voice_target_matches(plan.voice_target, policy)
            )
        if provenance.planner_policy_version != CANONICAL_PLANNER_POLICY_VERSION:
            return False
        if policy.mode == "companion":
            companion = companion_definition(policy.companion_style_id)
            return (
                companion is not None
                and plan.voice_target.kind == "companion"
                and plan.voice_target.profile_id == companion.designed_voice_profile
                and plan.voice_target.model == DESIGNED_VOICE_MODEL
                and provenance.digital_self_version_id is None
                and provenance.manifest_sha256 is None
                and self._persona_snapshot_matches(provenance)
                and provenance.relationship_profile_id is None
                and provenance.relationship_profile_version is None
                and self._legacy_provenance_absent(provenance)
            )
        if policy.mode not in {"self_preview", "legacy"}:
            return False
        if (
            provenance.digital_self_version_id is None
            or provenance.manifest_sha256 is None
            or references.get("digital_self_version_id") != provenance.digital_self_version_id
            or references.get("manifest_sha256") != provenance.manifest_sha256
        ):
            return False
        if policy.mode == "self_preview" and not self._legacy_provenance_absent(provenance):
            return False
        if provenance.relationship_profile_id is None:
            if provenance.relationship_profile_version is not None:
                return False
        elif (
            provenance.relationship_profile_version is None
            or references.get("relationship_profile_id") != provenance.relationship_profile_id
            or references.get("relationship_profile_version")
            != str(provenance.relationship_profile_version)
        ):
            return False
        if policy.mode == "legacy":
            if (
                "digital_identity" not in plan.disclosures
                or "digital_identity" not in provenance.disclosures
                or provenance.speaker_class != "owner"
                or not self._legacy_provenance_matches(provenance, references)
            ):
                return False
        if plan.voice_target.kind == "fallback":
            return self._fallback_voice_target_matches(plan.voice_target, policy)
        return (
            plan.voice_target.kind == "approved_personal"
            and plan.voice_target.profile_id is not None
            and (policy.mode != "legacy" or references.get("legacy_voice_allowed") is True)
            and references.get("voice_profile_id") == plan.voice_target.profile_id
            and references.get("voice_model") == plan.voice_target.model
        )

    @staticmethod
    def _legacy_provenance_absent(provenance: ResponseProvenance) -> bool:
        return all(
            value is None
            for value in (
                provenance.actor_account_id,
                provenance.resource_owner_account_id,
                provenance.legacy_actor_role,
                provenance.legacy_grantee_account_id,
                provenance.legacy_grant_id,
                provenance.legacy_grant_snapshot_sha256,
                provenance.legacy_scope_sha256,
                provenance.legacy_shell_id,
                provenance.legacy_voice_allowed,
                provenance.legacy_expires_at,
            )
        )

    @staticmethod
    def _legacy_provenance_matches(
        provenance: ResponseProvenance,
        references: dict[str, str | bool | None],
    ) -> bool:
        return (
            provenance.actor_account_id == references.get("actor_account_id")
            and provenance.resource_owner_account_id == references.get("resource_owner_account_id")
            and provenance.legacy_actor_role == references.get("legacy_actor_role")
            and provenance.legacy_grantee_account_id == references.get("legacy_grantee_account_id")
            and provenance.legacy_grant_id == references.get("legacy_grant_id")
            and provenance.legacy_grant_snapshot_sha256
            == references.get("legacy_grant_snapshot_sha256")
            and provenance.legacy_scope_sha256 == references.get("legacy_scope_sha256")
            and provenance.legacy_shell_id == references.get("legacy_shell_id")
            and provenance.legacy_voice_allowed == references.get("legacy_voice_allowed")
            and provenance.legacy_expires_at == references.get("legacy_expires_at")
        )

    def _fallback_voice_target_matches(
        self,
        voice_target: ResponseVoiceTarget,
        policy: ModePolicy,
    ) -> bool:
        references = dict(policy.references)
        if policy.mode in {"self_preview", "legacy"}:
            return (
                voice_target.kind == "fallback"
                and voice_target.profile_id == references.get("fallback_voice_profile_id")
                and voice_target.model == references.get("fallback_voice_model")
            )
        return (
            voice_target.kind == "fallback"
            and voice_target.profile_id is None
            and voice_target.model == self._tts_model
        )

    @staticmethod
    def _persona_snapshot_matches(provenance: ResponseProvenance) -> bool:
        absent = provenance.persona_version_id is None and provenance.persona_version_number is None
        present = (
            provenance.persona_version_id is not None
            and provenance.persona_version_number is not None
        )
        if not absent and not present:
            return False
        if absent:
            return not provenance.persona_style_only
        if not provenance.persona_style_only:
            return provenance.speaker_class == "owner"
        return (
            provenance.speaker_class == "uncertain"
            and provenance.speaker_reason_code == "shadow_owner_candidate"
            and provenance.persona_version_id is not None
            and not provenance.source_refs
        )

    def _local_safe_plan(
        self,
        *,
        fence: GenerationFence,
        speaker: Any,
        reason: str,
    ) -> ResponsePlan:
        policy = self._runtime.mode_policy_for_fence(fence)
        mode = policy.mode or "legacy"
        raw_speaker_class = getattr(speaker, "classification", "uncertain")
        speaker_class = cast(
            Literal["owner", "guest", "uncertain"],
            raw_speaker_class
            if raw_speaker_class in {"owner", "guest", "uncertain"}
            else "uncertain",
        )
        speaker_reason = getattr(speaker, "reason_code", "speaker_unavailable")
        speaker_model = getattr(speaker, "model_version", "unknown")
        speaker_profile = getattr(speaker, "profile_id", None)
        speaker_template = getattr(speaker, "template_version", None)
        companion = mode == "companion"
        references = dict(policy.references)
        relationship_version_raw = references.get("relationship_profile_version")
        relationship_version = (
            int(relationship_version_raw)
            if isinstance(relationship_version_raw, str) and relationship_version_raw.isdigit()
            else None
        )
        refusal_disclosures: tuple[Disclosure, ...] = (
            ("digital_identity", "privacy_refusal", "unknown")
            if mode == "legacy"
            else ("privacy_refusal", "unknown")
        )
        return ResponsePlan(
            fence=fence,
            instructions=(
                "仅依据当前用户这一轮内容回答。不得读取、引用或推断历史对话、"
                "账户主人的私人记忆、人格、关系或工具结果；不确定时明确说明。"
                if companion
                else _LOCAL_SAFE_REFUSAL_INSTRUCTIONS
            ),
            direct_text=None if companion else _LOCAL_SAFE_REFUSAL_TEXT,
            epistemic_status="not_applicable",
            epistemic_reason_codes=("local_safe_fallback", reason),
            grounded_items=(),
            disclosures=("privacy_refusal", "unknown") if companion else refusal_disclosures,
            voice_target=ResponseVoiceTarget(
                kind="fallback",
                profile_id=(
                    None
                    if companion
                    else cast(str | None, references.get("fallback_voice_profile_id"))
                ),
                model=(
                    self._tts_model
                    if companion
                    else cast(str, references.get("fallback_voice_model"))
                ),
            ),
            provenance=ResponseProvenance(
                planner_policy_version="local-safe-fallback-v1",
                interaction_mode=mode,
                mode_policy_version=policy.policy_version or "unavailable",
                digital_self_version_id=(
                    None
                    if companion
                    else cast(str | None, references.get("digital_self_version_id"))
                ),
                manifest_sha256=(
                    None if companion else cast(str | None, references.get("manifest_sha256"))
                ),
                persona_version_id=None,
                persona_version_number=None,
                persona_style_only=False,
                relationship_profile_id=(
                    None
                    if companion
                    else cast(str | None, references.get("relationship_profile_id"))
                ),
                relationship_profile_version=(None if companion else relationship_version),
                speaker_class=speaker_class,
                speaker_reason_code=speaker_reason,
                speaker_profile_id=speaker_profile,
                speaker_model_version=speaker_model,
                speaker_template_version=speaker_template,
                source_refs=(),
                epistemic_status="not_applicable",
                epistemic_reason_codes=("local_safe_fallback", reason),
                disclosures=(("privacy_refusal", "unknown") if companion else refusal_disclosures),
                actor_account_id=(
                    cast(str | None, references.get("actor_account_id"))
                    if mode == "legacy"
                    else None
                ),
                resource_owner_account_id=(
                    cast(str | None, references.get("resource_owner_account_id"))
                    if mode == "legacy"
                    else None
                ),
                legacy_actor_role=(
                    cast(
                        Literal["owner_preview", "grantee"] | None,
                        references.get("legacy_actor_role"),
                    )
                    if mode == "legacy"
                    else None
                ),
                legacy_grantee_account_id=(
                    cast(str | None, references.get("legacy_grantee_account_id"))
                    if mode == "legacy"
                    else None
                ),
                legacy_grant_id=(
                    cast(str | None, references.get("legacy_grant_id"))
                    if mode == "legacy"
                    else None
                ),
                legacy_grant_snapshot_sha256=(
                    cast(str | None, references.get("legacy_grant_snapshot_sha256"))
                    if mode == "legacy"
                    else None
                ),
                legacy_scope_sha256=(
                    cast(str | None, references.get("legacy_scope_sha256"))
                    if mode == "legacy"
                    else None
                ),
                legacy_shell_id=(
                    cast(str | None, references.get("legacy_shell_id"))
                    if mode == "legacy"
                    else None
                ),
                legacy_voice_allowed=(
                    cast(bool | None, references.get("legacy_voice_allowed"))
                    if mode == "legacy"
                    else None
                ),
                legacy_expires_at=(
                    cast(str | None, references.get("legacy_expires_at"))
                    if mode == "legacy"
                    else None
                ),
            ),
        )

    def _cache_response_plan(self, plan: ResponsePlan) -> None:
        self._response_plan_by_fence[self._response_plan_key(plan.fence)] = plan
        while len(self._response_plan_by_fence) > 32:
            self._response_plan_by_fence.pop(next(iter(self._response_plan_by_fence)))

    def _observe_tts_alignment(
        self,
        fence: GenerationFence,
        utterance_id: str,
        status: str,
    ) -> None:
        if self._runtime.fence.matches(fence):
            self._runtime.heard_tracker.observe_alignment(fence, utterance_id, status)

    async def on_user_turn_completed(self, turn_ctx: Any, new_message: Any) -> None:
        raw_text = _message_text(new_message) if new_message is not None else ""
        canonical_text = self._runtime.consume_canonical_user_turn(raw_text)
        canonical_speech_epoch = self._runtime.consumed_canonical_speech_epoch
        if canonical_text is None:
            raise StopResponse()
        text = canonical_text
        if new_message is not None and text != raw_text.strip() and hasattr(new_message, "content"):
            new_message.content = [text]
        if text.strip():
            has_speech_metrics = hasattr(new_message, "metrics")
            metrics = getattr(new_message, "metrics", {}) or {}
            speech_anchored = (
                any(
                    metrics.get(name) is not None
                    for name in ("started_speaking_at", "stopped_speaking_at")
                )
                if has_speech_metrics
                else None
            )
            logger.info(
                "user_turn_endpoint_timing started_speaking_at=%s "
                "stopped_speaking_at=%s transcription_delay=%s "
                "end_of_turn_delay=%s",
                metrics.get("started_speaking_at"),
                metrics.get("stopped_speaking_at"),
                metrics.get("transcription_delay"),
                metrics.get("end_of_turn_delay"),
            )
            speaker, semantic_verdict = await asyncio.gather(
                self._runtime.await_speaker_classification(),
                self._runtime.resolve_interrupt_semantic(
                    text.strip(),
                    canonical_speech_epoch=canonical_speech_epoch,
                ),
            )
            logger.info(
                "speaker_authority classification=%s reason=%s model=%s "
                "template_version=%s session_id=%s",
                speaker.classification,
                speaker.reason_code,
                speaker.model_version,
                speaker.template_version,
                self._runtime.session_id,
            )
            accepted, reason = self._runtime.accept_user_turn(
                text.strip(),
                speech_anchored=speech_anchored,
                canonical_speech_epoch=canonical_speech_epoch,
                semantic_verdict=semantic_verdict,
            )
            if not accepted:
                logger.info(
                    "%s reason=%s",
                    (
                        "post_playback_input_ignored"
                        if reason
                        in {
                            "backchannel",
                            "assistant_echo",
                            "non_target_language",
                            "speaker_mismatch",
                            "interrupt_command_only",
                            "interrupt_replayed_previous_turn",
                            "interrupt_semantic_control_only",
                            "interrupt_semantic_unsure",
                            "stale_interrupt_semantic",
                            "target_non_owner",
                            "target_insufficient_speech",
                            "target_unconfirmed",
                        }
                        else "user_turn_ignored"
                    ),
                    reason,
                )
                raise StopResponse()
            policy = self._runtime.mode_policy
            if (
                self._voice_profile_client is not None
                and self._runtime.tts is not None
                and (policy.allows_voice_profile() or policy.mode == "legacy")
            ):
                await self._runtime.wait_for_voice_profile_refresh()
                _apply_cached_voice_profile(
                    tts_plugin=self._runtime.tts,
                    client=self._voice_profile_client,
                    session_id=self._runtime.session_id,
                    mode=policy.mode,
                    policy=policy,
                )
            fence = await self._runtime.on_turn_committed(text.strip())
            if self._runtime.tts is not None and not self._bind_current_tts_voice(fence):
                logger.error(
                    "generation voice binding rejected session_id=%s turn_id=%s generation_id=%s",
                    fence.session_id,
                    fence.turn_id,
                    fence.generation_id,
                )
                raise StopResponse()
            self._runtime.publish_transcript(
                speaker="user",
                text=text.strip(),
                final=True,
                fence=fence,
            )
            self._llm_text_buf = ""
            fetch_reason = "missing_response_planner_client"
            plan = None
            if self._response_planner_client is not None:
                try:
                    fetch = await self._response_planner_client.fetch(
                        session_id=self._runtime.session_id,
                        query=text.strip(),
                        fence=fence,
                        speaker_decision=speaker,
                    )
                    plan = fetch.plan
                    fetch_reason = fetch.reason
                except Exception:
                    logger.warning(
                        "response plan fetch failed closed session_id=%s turn_id=%s",
                        self._runtime.session_id,
                        fence.turn_id,
                        exc_info=True,
                    )
                    fetch_reason = "request_exception"
            if not fence.matches(self._runtime.fence):
                logger.info(
                    "stale response plan dropped session_id=%s turn_id=%s reason=runtime_fence",
                    self._runtime.session_id,
                    fence.turn_id,
                )
                raise StopResponse()
            if plan is not None and not plan.fence.matches(fence):
                logger.info(
                    "stale response plan dropped session_id=%s turn_id=%s reason=plan_fence",
                    self._runtime.session_id,
                    fence.turn_id,
                )
                raise StopResponse()
            if plan is None and fetch_reason == "fence_mismatch":
                logger.info(
                    "stale response plan dropped session_id=%s turn_id=%s reason=fetch_fence",
                    self._runtime.session_id,
                    fence.turn_id,
                )
                raise StopResponse()
            policy = self._runtime.mode_policy_for_fence(fence)
            if (
                plan is not None
                and self._runtime.mode_policy_enforced
                and not self._plan_matches_mode_policy(plan, policy)
            ):
                logger.warning(
                    "response plan dropped for policy mismatch session_id=%s turn_id=%s",
                    self._runtime.session_id,
                    fence.turn_id,
                )
                plan = None
                fetch_reason = "mode_policy_mismatch"
            if plan is None:
                plan = self._local_safe_plan(
                    fence=fence,
                    speaker=speaker,
                    reason=fetch_reason,
                )
            if policy.mode in {
                "self_preview",
                "legacy",
            } and not self._ensure_generation_voice_matches_plan(fence, plan, policy):
                logger.error(
                    "response plan voice bind failed closed mode=%s fallback=%s "
                    "session_id=%s turn_id=%s generation_id=%s",
                    policy.mode,
                    self._is_local_safe_plan(plan),
                    self._runtime.session_id,
                    fence.turn_id,
                    fence.generation_id,
                )
                raise StopResponse()
            self._cache_response_plan(plan)
            logger.info(
                "response_plan_cached reason=%s mode=%s direct_text=%s fallback=%s "
                "session_id=%s turn_id=%s",
                fetch_reason,
                plan.provenance.interaction_mode,
                plan.direct_text is not None,
                self._is_local_safe_plan(plan),
                self._runtime.session_id,
                fence.turn_id,
            )
            logger.info(
                "turn_committed turn_id=%s generation_id=%s tool_epoch=%s text_len=%s",
                fence.turn_id,
                fence.generation_id,
                fence.tool_epoch,
                len(text.strip()),
            )
        parent = getattr(Agent, "on_user_turn_completed", None) if _HAS_LIVEKIT else None
        if parent is not None:
            try:
                await parent(self, turn_ctx, new_message)
            except TypeError:
                return

    def llm_node(
        self,
        chat_ctx: Any,
        tools: list[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        return self._llm_node_impl(chat_ctx, tools, model_settings)

    async def _llm_node_impl(
        self,
        chat_ctx: Any,
        tools: list[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        """Gate every LLM token/chunk through GenerationFence; register cancel task."""
        if not _HAS_LIVEKIT:
            return
            yield  # pragma: no cover  # make this an async generator

        fence = self._runtime.fence
        policy = self._runtime.mode_policy_for_fence(fence)
        if self._runtime.mode_policy_enforced and not policy.allows_conversation(
            self._runtime.current_speaker_class
        ):
            logger.error(
                "llm request blocked by frozen interaction policy session_id=%s "
                "turn_id=%s generation_id=%s",
                self._runtime.session_id,
                fence.turn_id,
                fence.generation_id,
            )
            return
        resume_interrupted_reply = self._runtime.is_resume_generation(fence)
        if self._runtime.tts is not None:
            self._runtime.tts.bind_fence(fence)

        task = asyncio.current_task()
        self._runtime.orchestrator.set_active_llm_task(task)
        self._llm_text_buf = ""
        heard_assistant = [
            turn.content
            for turn in self._runtime.orchestrator.context.turns
            if turn.role == "assistant"
        ]
        response_plan = self._response_plan_by_fence.get(self._response_plan_key(fence))
        if response_plan is None or not response_plan.fence.matches(fence):
            logger.error(
                "llm request blocked without exact response plan session_id=%s "
                "turn_id=%s generation_id=%s",
                self._runtime.session_id,
                fence.turn_id,
                fence.generation_id,
            )
            return
        if self._runtime.mode_policy_enforced and not self._plan_matches_mode_policy(
            response_plan, policy
        ):
            logger.error(
                "llm request blocked by response plan policy mismatch session_id=%s "
                "turn_id=%s generation_id=%s",
                self._runtime.session_id,
                fence.turn_id,
                fence.generation_id,
            )
            return
        if policy.mode in {
            "self_preview",
            "legacy",
        } and not self._ensure_generation_voice_matches_plan(fence, response_plan, policy):
            logger.error(
                "llm request blocked by response plan voice mismatch mode=%s fallback=%s "
                "session_id=%s turn_id=%s generation_id=%s",
                policy.mode,
                self._is_local_safe_plan(response_plan),
                self._runtime.session_id,
                fence.turn_id,
                fence.generation_id,
            )
            return
        provenance_bound = self._bind_response_plan_provenance(fence, response_plan)
        if not provenance_bound:
            logger.error(
                "response provenance bind failed closed mode=%s fallback=%s "
                "session_id=%s turn_id=%s generation_id=%s",
                policy.mode,
                self._is_local_safe_plan(response_plan),
                self._runtime.session_id,
                fence.turn_id,
                fence.generation_id,
            )
            return
        speaker_class = response_plan.provenance.speaker_class
        safe_chat_ctx = self._context_assembler.assemble(
            chat_ctx=chat_ctx,
            heard_assistant=heard_assistant,
            speaker_class=speaker_class,
            response_plan=response_plan,
            resume_interrupted_reply=resume_interrupted_reply,
            force_current_user_only=self._is_local_safe_plan(response_plan),
        )
        segmenter = self._runtime.orchestrator.segmenter
        if segmenter is None:
            raise RuntimeError("PhraseSegmenter is not configured")
        segmenter.reset(fence)
        reply_chars = 0
        reply_sentences = 0
        reply_budget_exhausted = False
        first_content_marked = False
        first_phrase_marked = False
        user_turns = [
            turn.content
            for turn in self._runtime.orchestrator.context.turns
            if turn.role == "user" and turn.content
        ]
        last_user = (
            user_turns[-2]
            if resume_interrupted_reply and len(user_turns) >= 2
            else user_turns[-1]
            if user_turns
            else ""
        )
        longform = any(hint in last_user for hint in _LONGFORM_HINTS) or (
            self._runtime.speech_plan.delivery_mode in {"deliberative", "supportive"}
        )
        max_chars = MAX_VOICE_REPLY_CHARS_LONGFORM if longform else MAX_VOICE_REPLY_CHARS
        max_sentences = (
            MAX_VOICE_REPLY_SENTENCES_LONGFORM if longform else MAX_VOICE_REPLY_SENTENCES
        )

        def _accept_segment(text: str) -> str | None:
            nonlocal reply_chars, reply_sentences, reply_budget_exhausted
            remaining_chars = max_chars - reply_chars
            remaining_sentences = max_sentences - reply_sentences
            if remaining_chars <= 0 or remaining_sentences <= 0:
                reply_budget_exhausted = True
                return None
            accepted_chars = 0
            accepted_sentences = 0
            accepted: list[str] = []
            for char in text:
                if char.isalnum():
                    if accepted_chars >= remaining_chars:
                        break
                    accepted_chars += 1
                accepted.append(char)
                if char in _SENTENCE_ENDINGS:
                    accepted_sentences += 1
                    if accepted_sentences >= remaining_sentences:
                        break
            fitted = "".join(accepted).strip()
            if not fitted:
                reply_budget_exhausted = True
                return None
            if fitted != text and fitted[-1] not in _SENTENCE_ENDINGS:
                fitted += "。"
            chars = sum(1 for ch in fitted if ch.isalnum())
            reply_chars += chars
            reply_sentences += sum(ch in _SENTENCE_ENDINGS for ch in fitted)
            if fitted != text or reply_chars >= max_chars or reply_sentences >= max_sentences:
                reply_budget_exhausted = True
            return fitted

        stream: Any = None
        try:
            self._runtime.mark_audio_event("llm_request_started")
            if response_plan.direct_text is not None:
                self._runtime.mark_audio_event("llm_first_content_token")
                gated = self._runtime.gate_llm_token(fence, response_plan.direct_text)
                if gated is not None:
                    self._llm_text_buf += gated
                    for segment in segmenter.push_token(gated):
                        accepted_segment = _accept_segment(segment.text)
                        if accepted_segment is None:
                            break
                        if not first_phrase_marked:
                            self._runtime.mark_audio_event(
                                "first_phrase_ready",
                                detail={
                                    "stream_speak_while_think": True,
                                    "generation_id": fence.generation_id,
                                },
                            )
                            first_phrase_marked = True
                        yield accepted_segment
                    if fence.matches(self._runtime.fence) and not reply_budget_exhausted:
                        for segment in segmenter.flush(end_of_stream=True):
                            accepted_segment = _accept_segment(segment.text)
                            if accepted_segment is None:
                                break
                            if not first_phrase_marked:
                                self._runtime.mark_audio_event(
                                    "first_phrase_ready",
                                    detail={
                                        "stream_speak_while_think": True,
                                        "generation_id": fence.generation_id,
                                    },
                                )
                                first_phrase_marked = True
                            yield accepted_segment
                return
            safe_tools = (
                tools
                if policy.allows_tools(speaker_class)
                and not self._is_local_safe_plan(response_plan)
                else []
            )
            stream = Agent.default.llm_node(self, safe_chat_ctx, safe_tools, model_settings)
            # default may return async gen or coroutine of async gen
            if asyncio.iscoroutine(stream):
                stream = await stream
            assert stream is not None
            async for chunk in stream:
                if task is not None and task.cancelled():
                    break
                if self._runtime.orchestrator.tts_cancel_event().is_set():
                    break
                text = _chunk_text(chunk)
                if text:
                    if not first_content_marked:
                        self._runtime.mark_audio_event("llm_first_content_token")
                        first_content_marked = True
                    gated = self._runtime.gate_llm_token(fence, text)
                    if gated is None:
                        # Stale generation — stop yielding into TTS pipeline.
                        logger.info(
                            "stale llm token dropped generation_id=%s",
                            fence.generation_id,
                        )
                        break
                    self._llm_text_buf += gated
                    for segment in segmenter.push_token(gated):
                        accepted_segment = _accept_segment(segment.text)
                        if accepted_segment is None:
                            break
                        if not first_phrase_marked:
                            # P1-6: first audible phrase while LLM stream still open.
                            self._runtime.mark_audio_event(
                                "first_phrase_ready",
                                detail={
                                    "stream_speak_while_think": True,
                                    "generation_id": fence.generation_id,
                                },
                            )
                            first_phrase_marked = True
                        yield accepted_segment
                    if reply_budget_exhausted:
                        logger.info(
                            "voice_reply_budget_reached chars=%s sentences=%s",
                            reply_chars,
                            reply_sentences,
                        )
                        break
                    if not isinstance(chunk, str):
                        delta = getattr(chunk, "delta", None)
                        if delta is not None and hasattr(chunk, "model_copy"):
                            chunk_without_text: Any = chunk
                            yield chunk_without_text.model_copy(
                                update={"delta": delta.model_copy(update={"content": None})}
                            )
                    continue
                yield chunk
            if fence.matches(self._runtime.fence) and not reply_budget_exhausted:
                for segment in segmenter.flush(end_of_stream=True):
                    accepted_segment = _accept_segment(segment.text)
                    if accepted_segment is None:
                        break
                    if not first_phrase_marked:
                        self._runtime.mark_audio_event(
                            "first_phrase_ready",
                            detail={
                                "stream_speak_while_think": True,
                                "generation_id": fence.generation_id,
                            },
                        )
                        first_phrase_marked = True
                    yield accepted_segment
        finally:
            if reply_budget_exhausted and stream is not None:
                close = getattr(stream, "aclose", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await close()
            self._runtime.orchestrator.clear_active_llm_task(task)

    def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        return self._tts_node_impl(text, model_settings)

    async def _tts_node_impl(
        self,
        text: AsyncIterable[str],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        """Gate every audio frame through GenerationFence; register cancel task."""
        if not _HAS_LIVEKIT:
            return
            yield  # pragma: no cover

        from services.agent.src.orchestration.prosody import prepare_tts_text

        fence = self._runtime.fence
        self._runtime.heard_tracker.expect_utterance(fence)
        if self._runtime.tts is not None:
            self._runtime.tts.bind_fence(fence)

        task = asyncio.current_task()
        self._runtime.orchestrator.set_active_tts_task(task)
        spoken_parts: list[str] = []
        first_audio_marked = False

        async def _track_text() -> AsyncGenerator[str, None]:
            first_segment = True
            async for part in text:
                rewritten = prepare_tts_text(
                    part,
                    self._runtime.speech_plan,
                    is_first_segment=first_segment,
                    use_markup_tags=self._runtime.use_paralinguistic_tags,
                )
                first_segment = False
                if not rewritten:
                    continue
                spoken_parts.append(rewritten)
                self._runtime.update_pending_assistant_text("".join(spoken_parts))
                yield rewritten

        try:
            self._runtime.mark_audio_event("tts_task_started")
            stream = Agent.default.tts_node(self, _track_text(), model_settings)
            if asyncio.iscoroutine(stream):
                stream = await stream
            assert stream is not None
            async for frame in stream:
                if task is not None and task.cancelled():
                    break
                if self._runtime.orchestrator.tts_cancel_event().is_set():
                    break
                pcm = bytes(getattr(frame, "data", b"") or b"")
                if pcm:
                    if not first_audio_marked:
                        self._runtime.mark_audio_event(
                            "tts_first_audio_received",
                            detail={"pcm_bytes": len(pcm)},
                        )
                        first_audio_marked = True
                    gated = self._runtime.gate_tts_audio(fence, pcm)
                    if gated is None:
                        logger.info(
                            "stale tts audio dropped generation_id=%s",
                            fence.generation_id,
                        )
                        break
                yield frame
        finally:
            self._runtime.orchestrator.clear_active_tts_task(task)

    def transcription_node(
        self,
        text: AsyncIterable[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        return self._transcription_node_impl(text, model_settings)

    async def _transcription_node_impl(
        self,
        text: AsyncIterable[Any],
        model_settings: Any,
    ) -> AsyncGenerator[Any, None]:
        """Capture timed transcript words into HeardTextTracker."""
        if not _HAS_LIVEKIT:
            return
            yield  # pragma: no cover

        words: list[TimedWord] = []
        async for delta in Agent.default.transcription_node(self, text, model_settings):
            if isinstance(delta, TimedString) or (
                hasattr(delta, "start_time") and hasattr(delta, "end_time")
            ):
                try:
                    start = float(getattr(delta, "start_time", 0.0) or 0.0)
                    end = float(getattr(delta, "end_time", start) or start)
                    words.append(
                        TimedWord(
                            text=str(delta),
                            begin_ms=int(start * 1000),
                            end_ms=int(end * 1000),
                        )
                    )
                except Exception:
                    pass
            if not self._runtime.heard_tracker.alignment_degraded:
                yield delta
        if words:
            self._runtime.orchestrator.heard_tracker.add_words(words)


def prewarm(proc: Any) -> None:
    if not _HAS_LIVEKIT:
        return
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.05,
        min_silence_duration=0.30,
        prefix_padding_duration=0.30,
        force_cpu=True,
    )


def build_turn_handling_options(
    profile: str,
    *,
    interruptions_enabled: bool = True,
) -> Any:
    """Build TurnHandlingOptions; raises on API mismatch (no silent swallow)."""
    from livekit.agents import TurnHandlingOptions, inference

    config = build_turn_handling_config(
        profile,
        interruptions_enabled=interruptions_enabled,
    )
    turn_detector_version = cast(Literal["v1", "v1-mini"], config["turn_detection"]["version"])
    return TurnHandlingOptions(
        turn_detection=inference.TurnDetector(version=turn_detector_version),
        endpointing=config["endpointing"],
        interruption=config["interruption"],
        preemptive_generation=config["preemptive_generation"],
    )


def build_session_kwargs(
    *,
    vad: Any,
    stt: Any,
    llm: Any,
    tts: Any,
    profile: str,
    offline: bool,
    interruptions_enabled: bool = True,
) -> dict[str, Any]:
    _ = offline
    session_kwargs: dict[str, Any] = {
        "vad": vad,
        "llm": llm,
        "stt": stt,
        "tts": tts,
    }
    try:
        session_kwargs["turn_handling"] = build_turn_handling_options(
            profile,
            interruptions_enabled=interruptions_enabled,
        )
    except Exception as exc:
        logger.error(
            "TurnHandlingOptions construction failed; using config fallback: %s",
            exc,
            exc_info=True,
        )
        session_kwargs["turn_handling_config"] = build_turn_handling_config(
            profile,
            interruptions_enabled=interruptions_enabled,
        )
        if os.getenv("ENVIRONMENT", "development") == "production":
            raise
    return session_kwargs


async def entrypoint(ctx: Any) -> None:
    """Production LiveKit entry. Wires FunASR/Doubao TTS/LLM + DuplexRuntime."""
    if not _HAS_LIVEKIT:
        raise RuntimeError("livekit-agents not installed")

    dispatch_metadata = getattr(getattr(ctx, "job", None), "metadata", "")
    await ctx.connect()

    from services.agent.src.config import AgentSettings
    from services.agent.src.providers.doubao_tts import DoubaoTTS
    from services.agent.src.providers.funasr_stt import FunASRSTT
    from services.agent.src.providers.qwen_emotion_asr import (
        QwenEmotionConfig,
        QwenEmotionSidecar,
    )
    from services.agent.src.providers.vosk_kws import (
        VoskKeywordSpotter,
        VoskKeywordSpotterConfig,
    )

    stt_plugin = FunASRSTT.from_env()
    tts_plugin = DoubaoTTS.from_env()
    try:
        await tts_plugin.pool.warm()
    except Exception as exc:
        logger.warning("Doubao TTS pool warm failed (will open on demand): %s", exc)

    import httpx

    runtime_settings = AgentSettings()

    llm_plugin = openai.LLM(
        model=runtime_settings.llm_fast_model,
        api_key=runtime_settings.llm_api_key,
        base_url=runtime_settings.llm_base_url,
        temperature=float(os.getenv("DEEPSEEK_FAST_TEMPERATURE", "0.45")),
        tool_choice="auto",
        max_retries=0,
        timeout=httpx.Timeout(connect=3.0, read=12.0, write=5.0, pool=3.0),
        extra_body={
            "thinking": {"type": "disabled"},
            "max_tokens": int(os.getenv("DEEPSEEK_FAST_MAX_TOKENS", "240")),
        },
    )

    profile = os.getenv("DEPLOYMENT_PROFILE", "livekit_cloud")
    offline = os.getenv("OFFLINE_MOCK", "false").lower() == "true"
    miniprogram_session = is_miniprogram_session(dispatch_metadata)
    miniprogram_aec_session = is_miniprogram_aec_session(dispatch_metadata)

    room_name = str(ctx.room.name)
    runtime_session_id = (
        room_name.removeprefix("voice-") if room_name.startswith("voice-") else room_name
    )
    from services.agent.src.orchestration.speaker_verify import SpeakerVerifier

    speaker_verifier = SpeakerVerifier(
        enabled=should_enable_legacy_speaker_verifier(runtime_settings, offline=offline),
        enroll_speech_ms=runtime_settings.speaker_enroll_speech_ms,
        enroll_timeout_ms=runtime_settings.speaker_enroll_timeout_ms,
        accept_threshold=runtime_settings.speaker_accept_threshold,
        min_verify_speech_ms=runtime_settings.speaker_min_verify_speech_ms,
    )
    cue_playback = runtime_settings.listener_cue_playback
    cues_on = runtime_settings.listener_cues_enabled
    if cues_on and cue_playback == "background" and not runtime_settings.listener_cue_aec_validated:
        cue_playback = "main_track"
    runtime = DuplexRuntime.create(
        session_id=runtime_session_id,
        tts=tts_plugin,
        input_guard_enabled=profile == "cn_self_hosted" or miniprogram_aec_session,
        trusted_aec_playback_control=miniprogram_aec_session,
        barge_in_enabled=not miniprogram_session,
        listener_cues_enabled=cues_on,
        use_paralinguistic_tags=False,
        speaker_verifier=speaker_verifier,
    )
    interrupt_semantic_classifier: InterruptSemanticClassifier | None = None
    if (
        miniprogram_aec_session
        and runtime.barge_in_enabled
        and runtime_settings.interrupt_semantic_enabled
        and not offline
    ):
        interrupt_semantic_classifier = InterruptSemanticClassifier(
            InterruptSemanticClassifierConfig(
                api_key=runtime_settings.dashscope_api_key,
                base_url=runtime_settings.dashscope_compatible_base_url,
                model=runtime_settings.interrupt_semantic_model,
                timeout_s=runtime_settings.interrupt_semantic_timeout_s,
            )
        )

        async def _resolve_interrupt_semantic(
            final_text: str,
            sticky_text: str,
            assistant_text: str,
        ) -> InterruptSemanticVerdict:
            assert interrupt_semantic_classifier is not None
            return await interrupt_semantic_classifier.classify(
                final_text=final_text,
                sticky_text=sticky_text,
                assistant_text=assistant_text,
            )

        runtime.set_interrupt_semantic_resolver(_resolve_interrupt_semantic)
    mode_policy_client = None
    interaction_policy_token = runtime_settings.internal_token("interaction_policy")
    if interaction_policy_token and not offline:
        from services.agent.src.mode_policy_client import (
            ModePolicyClient,
            ModePolicyClientConfig,
        )

        mode_policy_client = ModePolicyClient(
            ModePolicyClientConfig(
                endpoint=runtime_settings.interaction_policy_url,
                internal_token=interaction_policy_token,
                timeout_s=runtime_settings.interaction_policy_timeout_s,
            )
        )
        policy = await mode_policy_client.fetch(session_id=runtime_session_id)
        runtime.set_mode_policy(policy)
        if not policy.available:
            logger.error(
                "interaction policy unavailable; session is fail-closed session_id=%s reason=%s",
                runtime_session_id,
                policy.unavailable_reason,
            )
    else:
        runtime.set_mode_policy(
            ModePolicy.unavailable(
                "missing_interaction_policy_token"
                if not interaction_policy_token
                else "offline_mock"
            )
        )
        logger.error(
            "interaction policy unavailable; session is fail-closed session_id=%s",
            runtime_session_id,
        )
    if not runtime.mode_policy.allows_conversation():
        if mode_policy_client is not None:
            await mode_policy_client.aclose()
        raise RuntimeError(
            "interaction policy does not authorize a companion conversation; refusing session start"
        )
    if runtime_settings.speaker_authority_enabled and not offline:
        from services.agent.src.speaker_authority_client import (
            SpeakerAuthorityClient,
            SpeakerAuthorityClientConfig,
        )

        speaker_authority = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint=runtime_settings.speaker_authority_url,
                internal_token=runtime_settings.speaker_internal_token.get_secret_value(),
                timeout_s=runtime_settings.speaker_authority_timeout_s,
            )
        )

        async def _classify_speaker(pcm: bytes, sample_rate: int) -> Any:
            try:
                return await speaker_authority.classify(
                    session_id=runtime_session_id,
                    pcm=pcm,
                    sample_rate=sample_rate,
                )
            finally:
                runtime.set_reject_non_owner_voice(speaker_authority.reject_non_owner_voice)

        runtime.set_speaker_classifier(
            _classify_speaker,
            sample_rate=runtime_settings.funasr_sample_rate,
            timeout_s=runtime_settings.speaker_authority_timeout_s,
        )
        runtime.set_target_speaker_focus(True)
    archive_sink = None
    archive_token = runtime_settings.internal_token("archive_write")
    archive_spool_key = runtime_settings.archive_spool_key.get_secret_value()
    if runtime_settings.archive_sink_enabled and archive_token and archive_spool_key:
        from services.agent.src.archive_sink import ArchiveSink, ArchiveSinkConfig

        archive_sink = ArchiveSink(
            ArchiveSinkConfig(
                endpoint=runtime_settings.archive_session_events_url,
                internal_token=archive_token,
                spool_path=Path(runtime_settings.archive_spool_path),
                spool_key=archive_spool_key,
                spool_max_bytes=runtime_settings.archive_spool_max_bytes,
            )
        )

        async def _publish_evidence(event: dict[str, Any]) -> None:
            delivered = await archive_sink.publish(event)
            if not delivered:
                logger.warning(
                    "archive event queued in encrypted spool event_id=%s", event["event_id"]
                )

        async def _publish_owner_turn(
            event: dict[str, Any],
            pcm: bytes,
            sample_rate: int,
        ) -> None:
            delivered = await archive_sink.publish_owner_turn(
                event,
                pcm=pcm,
                sample_rate=sample_rate,
            )
            if not delivered:
                logger.warning(
                    "owner archive turn queued in shared encrypted spool event_id=%s",
                    event["event_id"],
                )

        async def _replay_archive_spool() -> None:
            try:
                replayed = await archive_sink.replay()
                if replayed:
                    logger.info("archive spool replayed events=%s", replayed)
            except Exception:
                logger.error("archive spool replay failed", exc_info=True)

        runtime.set_evidence_publisher(_publish_evidence)
        runtime.set_owner_turn_publisher(_publish_owner_turn)
        runtime._spawn(_replay_archive_spool(), name="archive-spool-replay")
    elif runtime_settings.archive_sink_enabled:
        logger.warning("archive sink is disabled because token or spool key is not configured")
    response_planner_client = None
    response_plan_token = runtime_settings.internal_token("response_plan")
    if response_plan_token and not offline:
        from services.agent.src.response_planner_client import ResponsePlannerClientConfig

        response_planner_client = ResponsePlannerClient(
            ResponsePlannerClientConfig(
                endpoint=runtime_settings.response_plan_url,
                internal_token=response_plan_token,
                timeout_s=runtime_settings.response_plan_timeout_s,
            )
        )
    elif not offline:
        logger.warning("response plan is disabled because token is unavailable")
    voice_profile_client = None
    voice_token = runtime_settings.internal_token("voice_resolution")
    if (
        runtime_settings.voice_profile_enabled
        and voice_token
        and (runtime.mode_policy.allows_voice_profile() or runtime.mode_policy.mode == "legacy")
        and not offline
    ):
        from services.agent.src.voice_profile_client import VoiceProfileClientConfig

        voice_profile_client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint=runtime_settings.voice_profile_url,
                internal_token=voice_token,
                timeout_s=runtime_settings.voice_profile_timeout_s,
            )
        )
        runtime.set_voice_profile_refresher(
            lambda: voice_profile_client.refresh(session_id=runtime_session_id)
        )
        await voice_profile_client.refresh(session_id=runtime_session_id)
        _apply_cached_voice_profile(
            tts_plugin=tts_plugin,
            client=voice_profile_client,
            session_id=runtime_session_id,
            mode=runtime.mode_policy.mode,
            policy=runtime.mode_policy,
        )
    elif runtime_settings.voice_profile_enabled and not offline:
        logger.warning("voice profile is disabled because policy authority or token is unavailable")
    runtime.cue_scheduler.min_speech_ms = runtime_settings.listener_cue_min_speech_ms
    runtime.cue_scheduler.pause_ms = runtime_settings.listener_cue_pause_ms
    runtime.cue_scheduler.cooldown_ms = runtime_settings.listener_cue_cooldown_ms
    runtime.cue_scheduler.max_per_turn = runtime_settings.listener_cue_max_per_turn
    # Main-track cues do not need dual-track AEC; background mode still gates on it.
    runtime.set_listener_cue_aec_healthy(
        cue_playback == "main_track" or runtime_settings.listener_cue_aec_validated
    )
    if hasattr(tts_plugin, "set_trace_callback"):
        tts_plugin.set_trace_callback(
            lambda name, status, detail: runtime.mark_audio_event(
                name,
                status=status,
                detail=detail,
            )
        )
    runtime.mark_audio_event("agent_runtime_created")
    emotion_sidecar: QwenEmotionSidecar | None = None
    pcm_observers: list[Any] = [runtime.feed_speaker_pcm]
    if runtime_settings.qwen_emotion_enabled and hasattr(stt_plugin, "set_pcm_observer"):
        emotion_sidecar = QwenEmotionSidecar(
            QwenEmotionConfig.from_env(),
            on_observation=lambda result: runtime.observe_acoustic_emotion(
                result.label,
                text=result.text,
                turn_id=result.turn_id,
            ),
        )
        runtime.set_emotion_turn_observer(emotion_sidecar.start_turn)
        if emotion_sidecar.start():
            pcm_observers.append(emotion_sidecar.feed_pcm)
            runtime.mark_audio_event("emotion_sidecar_ready")
        else:
            emotion_sidecar = None
            runtime.mark_audio_event("emotion_sidecar_ready", status="error")
    keyword_spotter: VoskKeywordSpotter | None = None
    if (
        miniprogram_aec_session
        and runtime.barge_in_enabled
        and runtime_settings.miniprogram_kws_enabled
        and hasattr(stt_plugin, "set_pcm_observer")
    ):
        keyword_spotter = VoskKeywordSpotter.try_create(
            VoskKeywordSpotterConfig.from_settings(runtime_settings)
        )
        if keyword_spotter is not None:
            pcm_observers.append(build_keyword_spotter_pcm_observer(runtime, keyword_spotter))
            runtime.mark_audio_event("keyword_spotter_ready")
        else:
            runtime.mark_audio_event("keyword_spotter_ready", status="error")
    if hasattr(stt_plugin, "set_pcm_observer"):

        def _fanout_pcm(pcm: bytes) -> None:
            for observer in pcm_observers:
                with contextlib.suppress(Exception):
                    observer(pcm)

        stt_plugin.set_pcm_observer(_fanout_pcm)
    await runtime.orchestrator.ready()
    ctx.proc.userdata["duplex_runtime"] = runtime
    ctx.proc.userdata["generation_fence"] = runtime.fence
    ctx.proc.userdata["orchestrator"] = runtime.orchestrator

    session_kwargs = build_session_kwargs(
        vad=ctx.proc.userdata.get("vad"),
        stt=stt_plugin,
        llm=llm_plugin,
        tts=tts_plugin,
        profile=profile,
        offline=offline,
        interruptions_enabled=not miniprogram_session,
    )
    session_kwargs.pop("turn_handling_config", None)
    if apply_miniprogram_session_audio_policy(session_kwargs, dispatch_metadata):
        logger.info(
            "mini_program_session_audio_policy aec_warmup_duration=disabled session_id=%s",
            runtime_session_id,
        )

    session = AgentSession(**session_kwargs)
    runtime.set_user_turn_clearer(session.clear_user_turn)

    async def _publish_ui_event(event: dict[str, Any]) -> None:
        await ctx.room.local_participant.publish_data(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            reliable=True,
            topic="voice-agent.ui",
        )

    runtime.set_event_publisher(_publish_ui_event)
    runtime.attach_session_events(session)

    original_interrupt = session.interrupt

    def _interrupt_wrapped(*args: Any, **kwargs: Any) -> Any:
        async def _stop_livekit() -> str | None:
            await original_interrupt(*args, **kwargs)
            return None

        return asyncio.create_task(
            runtime.on_real_interrupt(
                cause="session.interrupt",
                stop_playback=_stop_livekit,
            ),
            name="duplex-session-interrupt",
        )

    session.interrupt = _interrupt_wrapped  # type: ignore[method-assign]

    async def _target_speaker_interrupt() -> None:
        async def _stop_livekit() -> str | None:
            await original_interrupt()
            return None

        await runtime.on_real_interrupt(
            cause="target_speaker_confirmed",
            stop_playback=_stop_livekit,
        )

    runtime.set_target_speaker_interrupt(_target_speaker_interrupt)
    runtime.set_result_speaker(
        lambda text: session.say(text, allow_interruptions=True, add_to_chat_ctx=True)
    )

    async def _play_pcm_via_room_track(pcm: bytes, *, sample_rate: int) -> None:
        """Push PCM through a temporary LocalAudioTrack (main session.say is mute post-barge-in).

        Only used while assistant playout is already stopped — unpublish when done.
        """
        from livekit import rtc

        if not pcm or len(pcm) < 4:
            return
        source = rtc.AudioSource(sample_rate, 1)
        track = rtc.LocalAudioTrack.create_audio_track("memoria-ack", source)
        pub = await ctx.room.local_participant.publish_track(track)
        try:
            frame_ms = 20
            samples = sample_rate * frame_ms // 1000
            bytes_per = samples * 2
            # Pad to whole frames
            if len(pcm) % 2:
                pcm = pcm[:-1]
            pad = (-len(pcm)) % bytes_per
            if pad:
                pcm = pcm + b"\x00" * pad
            for offset in range(0, len(pcm), bytes_per):
                chunk = pcm[offset : offset + bytes_per]
                frame = rtc.AudioFrame(
                    data=chunk,
                    sample_rate=sample_rate,
                    num_channels=1,
                    samples_per_channel=samples,
                )
                await source.capture_frame(frame)
                await asyncio.sleep(frame_ms / 1000)
            await asyncio.sleep(0.05)
        finally:
            with contextlib.suppress(Exception):
                await ctx.room.local_participant.unpublish_track(pub.sid)

    async def _say_control_ack(text: str) -> None:
        """Short fixed ack after interrupt — prefer RTC PCM path (session.say is silent).

        Prod 20260717-195800: session.say after barge-in got first_pcm but never
        playback_started; wait_for_playout returned in ~200ms with no audible audio.
        """
        phrase = (text or "").strip() or "嗯，你说。"
        await asyncio.sleep(0.12)
        sample_rate = int(runtime_settings.doubao_tts_sample_rate or 24000)
        played = False
        if hasattr(tts_plugin, "synthesize_stream_text"):
            try:
                if hasattr(tts_plugin, "apply_speech_plan"):
                    tts_plugin.apply_speech_plan(emotion="neutral", rate=1.0)
                if hasattr(tts_plugin, "bind_fence"):
                    tts_plugin.bind_fence(runtime.fence)
                result = await tts_plugin.synthesize_stream_text(
                    [phrase],
                    fence=runtime.fence,
                )
                if result.pcm and not result.discarded:
                    await _play_pcm_via_room_track(
                        result.pcm,
                        sample_rate=sample_rate,
                    )
                    played = True
                    runtime.mark_audio_event(
                        "control_ack_played",
                        detail={
                            "ack_len": len(phrase),
                            "path": "room_track_pcm",
                            "pcm_bytes": len(result.pcm),
                        },
                    )
            except Exception:
                logger.warning("control ack PCM path failed", exc_info=True)
        if played:
            return
        # Last resort: session.say (often silent after barge-in).
        if hasattr(tts_plugin, "apply_speech_plan"):
            tts_plugin.apply_speech_plan(emotion="neutral", rate=1.0)
        handle = session.say(
            phrase,
            allow_interruptions=False,
            add_to_chat_ctx=False,
        )
        wait = getattr(handle, "wait_for_playout", None)
        if callable(wait):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(wait(), timeout=2.5)
        await asyncio.sleep(0.45)
        runtime.mark_audio_event(
            "control_ack_played",
            detail={"ack_len": len(phrase), "path": "session_say_fallback"},
        )

    async def _interrupt_yield_say(phrase: str) -> None:
        """Semantic interrupt ack:「嗯，你说。」vs「好的。」"""
        text = (phrase or "").strip() or "嗯，你说。"
        if text in {"嗯，你说", "嗯你说"}:
            text = "嗯，你说。"
        elif text in {"好的", "好"}:
            text = "好的。"
        await _say_control_ack(text)

    runtime.set_interrupt_yield(_interrupt_yield_say)

    async def _false_interrupt_recover() -> None:
        """Nearby noise stopped playout; acknowledge without starting a new generation."""
        await _say_control_ack("我继续。")

    runtime.set_false_interrupt_recover(_false_interrupt_recover)

    def _on_control_packet(packet: Any) -> None:
        topic = getattr(packet, "topic", None)
        if topic == MINIPROGRAM_AEC_HEALTH_TOPIC:
            if not miniprogram_aec_session or getattr(packet, "participant", None) is None:
                return
            try:
                event = json.loads(bytes(packet.data).decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                return
            failure_id = event.get("failure_id") if isinstance(event, dict) else None
            if (
                not isinstance(event, dict)
                or event.get("type") != MINIPROGRAM_AEC_FAILED
                or event.get("session_id") != runtime.session_id
                or not isinstance(failure_id, str)
                or not 1 <= len(failure_id) <= 64
            ):
                return
            runtime.revoke_trusted_aec_playback_control(cause="gateway_apm_failed")

            async def _ack_aec_failure() -> None:
                await ctx.room.local_participant.publish_data(
                    json.dumps(
                        {
                            "type": MINIPROGRAM_AEC_FAILED_ACK,
                            "session_id": runtime.session_id,
                            "failure_id": failure_id,
                        },
                        separators=(",", ":"),
                    ),
                    reliable=True,
                    topic=MINIPROGRAM_AEC_HEALTH_ACK_TOPIC,
                )

            runtime._spawn(_ack_aec_failure(), name="miniprogram-aec-failed-ack")
            return
        if topic == TELEMETRY_TOPIC:
            if getattr(packet, "participant", None) is None:
                return
            try:
                telemetry = json.loads(bytes(packet.data).decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                return
            if isinstance(telemetry, dict):
                runtime.observe_client_audio_trace(telemetry)
            return
        if topic != "voice-agent.control":
            return
        # Only the LiveKit server sends packets without a participant identity.
        if getattr(packet, "participant", None) is not None:
            return
        try:
            event = json.loads(bytes(packet.data).decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return
        event_type = event.get("type")
        if (
            event_type not in {"stop_response", "rtc_recovered"}
            or event.get("session_id") != runtime.session_id
        ):
            return

        async def _apply_control() -> None:
            async def _stop_livekit() -> str | None:
                with contextlib.suppress(RuntimeError):
                    await original_interrupt(force=True)
                return None

            await runtime.on_real_interrupt(
                cause=(
                    "rtc_recovered"
                    if event_type == "rtc_recovered"
                    else str(event.get("reason") or "user_button")
                ),
                stop_playback=_stop_livekit,
                create_user_turn=False,
                force_generation_bump=event_type == "rtc_recovered",
            )
            if event_type == "rtc_recovered":
                runtime.publish_assistant_state("listening")

        runtime._spawn(_apply_control(), name=f"duplex-{event_type.replace('_', '-')}")

    ctx.room.on("data_received", _on_control_packet)

    agent = DuplexVoiceAgent(
        instructions=VOICE_SYSTEM_PROMPT,
        runtime=runtime,
        voice_profile_client=voice_profile_client,
        response_planner_client=response_planner_client,
        llm_provider=runtime_settings.llm_provider,
        llm_model=runtime_settings.llm_fast_model,
        tts_provider="volcengine_doubao",
        tts_model=runtime_settings.doubao_tts_resource_id,
    )

    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                sample_rate=24000,
                num_channels=1,
                frame_size_ms=50,
                auto_gain_control=True,
                pre_connect_audio=True,
            ),
            audio_output=build_cascade_audio_output_options(),
            text_output=room_io.TextOutputOptions(
                sync_transcription=True,
                json_format=True,
            ),
        ),
    )
    if session.output.audio is None:
        raise RuntimeError("LiveKit audio output was not attached")
    runtime.attach_playback_events(session.output.audio)
    runtime.mark_audio_event("audio_output_attached")

    async def _shutdown_runtime() -> None:
        ctx.room.off("data_received", _on_control_packet)
        shutdown_errors: list[Exception] = []

        async def _close_component(label: str, operation: Any) -> None:
            try:
                await operation
            except Exception as exc:
                logger.error("shutdown component failed component=%s", label, exc_info=True)
                shutdown_errors.append(exc)

        if hasattr(stt_plugin, "set_pcm_observer"):
            stt_plugin.set_pcm_observer(None)
        if emotion_sidecar is not None:
            await _close_component("emotion_sidecar", emotion_sidecar.aclose())
        await _close_component("runtime", runtime.close())
        if interrupt_semantic_classifier is not None:
            await _close_component(
                "interrupt_semantic_classifier",
                interrupt_semantic_classifier.aclose(),
            )
        await _close_component("tts", tts_plugin.aclose())
        if response_planner_client is not None:
            await _close_component("response_planner_client", response_planner_client.aclose())
        if voice_profile_client is not None:
            await _close_component("voice_profile_client", voice_profile_client.close())
        if mode_policy_client is not None:
            await _close_component("mode_policy_client", mode_policy_client.aclose())
        if archive_sink is not None:
            await _close_component("archive_sink", archive_sink.close())
        if shutdown_errors:
            raise RuntimeError("one or more Agent shutdown components failed") from shutdown_errors[
                0
            ]

    ctx.add_shutdown_callback(_shutdown_runtime)

    ready_publish = runtime.publish_assistant_state("ready")
    if ready_publish is None:
        raise RuntimeError("Agent UI publisher was not configured")
    await ready_publish

    async def _say_fixed(text: str, *, interruptible: bool = False) -> None:
        """One TTS stream of fixed text to avoid multi-phrase voice glitches."""
        if hasattr(tts_plugin, "apply_speech_plan"):
            tts_plugin.apply_speech_plan(emotion="neutral", rate=1.0)
        handle = session.say(
            text,
            allow_interruptions=interruptible,
            add_to_chat_ctx=False,
        )
        wait = getattr(handle, "wait_for_playout", None)
        if callable(wait):
            await wait()
        else:
            for _ in range(150):
                if not runtime._was_speaking:
                    break
                await asyncio.sleep(0.1)
        # Fixed say may not clear speaking via conversation_item path.
        runtime._was_speaking = False
        await asyncio.sleep(0.2)

    if runtime.speaker_verifier.enabled:
        # Fixed single-stream prompt (not generate_reply) so TTS does not
        # split into multiple phrases that sound like a second voice / speed-up.
        runtime.publish_assistant_state("speaker_enroll")
        runtime.mark_audio_event("speaker_enroll_prompt_started")
        await _say_fixed(
            "请用正常音量连续说大约四秒，可以说：我是主人，请记住我的声音。",
            interruptible=False,
        )
        # Only start PCM enrollment after the prompt has fully finished playing.
        runtime.begin_speaker_enrollment()
        enroll_deadline = asyncio.get_running_loop().time() + (
            runtime_settings.speaker_enroll_timeout_ms / 1000.0
        )
        while asyncio.get_running_loop().time() < enroll_deadline:
            result = runtime.poll_speaker_enrollment()
            if result is not None:
                break
            # Faster poll once speech is flowing so welcome starts sooner.
            progress = runtime.speaker_verifier.enrollment_progress()
            speech_ms = int(progress.get("speech_ms") or 0)
            await asyncio.sleep(0.1 if speech_ms > 400 else 0.2)
        else:
            # Wall-clock end: leave legacy spectral guard unavailable, never owner.
            # (elapsed_ms only advances when feed_pcm runs).
            runtime.poll_speaker_enrollment(force=True)
        # Hard safety: never leave PENDING or all chat turns stay blocked.
        if runtime.speaker_verifier.state.value == "pending":
            runtime.poll_speaker_enrollment(force=True)
        runtime.publish_assistant_state("listening")
        runtime.mark_audio_event("welcome_generation_started")
        if runtime.speaker_verifier.state.value == "enrolled":
            await _say_fixed(
                "好的，已经记住你的声音了。想聊什么都可以直接说。",
                interruptible=False,
            )
        else:
            # Fail-open: do not announce "跳过声纹" — it felt like a random extra
            # sentence after the model had already answered enroll speech.
            await _say_fixed(
                "好的，想聊什么都可以直接说。",
                interruptible=False,
            )
    else:
        runtime.mark_audio_event("welcome_generation_started")
        await _say_fixed(
            "嗨，我在呢。想聊什么就直接说吧。",
            interruptible=True,
        )


def build_turn_handling_config(
    profile: str = "livekit_cloud",
    *,
    interruptions_enabled: bool = True,
) -> dict[str, Any]:
    """Pure config dict for tests without LiveKit types."""
    self_hosted = profile == "cn_self_hosted"
    endpointing_min_delay, endpointing_max_delay, false_interruption_timeout = load_turn_timing(
        profile
    )
    turn_version = "v1-mini" if self_hosted else "v1"
    env_version = os.getenv("LIVEKIT_TURN_DETECTOR_VERSION")
    if not self_hosted and env_version in ("v1", "v1-mini"):
        turn_version = env_version
    # P1-5: Adaptive Interruption needs LiveKit Cloud agent-gateway. Self-hosted
    # fails with "failed to connect to LiveKit Adaptive Interruption" and falls
    # back to VAD after noisy retries — default VAD on cn_self_hosted.
    adaptive_default = "false" if self_hosted else "true"
    interruption_mode = (
        "adaptive"
        if os.getenv("LIVEKIT_ADAPTIVE_INTERRUPTION", adaptive_default).lower() == "true"
        else "vad"
    )
    # P1-6: LiveKit preemptive starts LLM before EOU — keep default off on self-hosted
    # (historically caused stuck thinking). Streaming phrase→TTS is the safe path.
    preemptive_default = "false" if self_hosted else "false"
    preemptive_enabled = os.getenv("PREEMPTIVE_GENERATION", preemptive_default).lower() == "true"
    preemptive_tts = os.getenv("PREEMPTIVE_TTS", "false").lower() == "true"
    return {
        "turn_detection": {"version": turn_version},
        "endpointing": {
            "mode": "dynamic",
            # Prod sample (20260717): end_of_turn_delay stuck ~2.0s (= max_delay).
            # Floor 0.90 keeps false-EOU risk low while shaving ~0.4–1.1s off the
            # user-stop → first-audio gap vs the previous 1.30/2.00 defaults.
            "min_delay": endpointing_min_delay,
            "max_delay": endpointing_max_delay,
            "alpha": float(os.getenv("ENDPOINTING_ALPHA", "0.85")),
        },
        "interruption": {
            "enabled": interruptions_enabled,
            "mode": interruption_mode,
            # Slightly longer on self-hosted: short noise/echo was cancelling
            # mid-reply creative TTS (user hears "突然不说了").
            "min_duration": float(
                os.getenv("INTERRUPTION_MIN_DURATION_S", "0.55" if self_hosted else "0.25")
            ),
            "min_words": 0,
            "discard_audio_if_uninterruptible": True,
            "false_interruption_timeout": false_interruption_timeout,
            "resume_false_interruption": True,
            "backchannel_boundary": (0.50, 1.80),
        },
        "preemptive_generation": {
            "enabled": preemptive_enabled,
            "preemptive_tts": preemptive_tts and preemptive_enabled,
            "max_speech_duration": float(os.getenv("PREEMPTIVE_MAX_SPEECH_DURATION_S", "10.0")),
            "max_retries": int(os.getenv("PREEMPTIVE_MAX_RETRIES", "2")),
        },
        # Product flag: fence-gated stream first phrase while LLM continues (not LiveKit preemptive).
        "stream_speak_while_think": True,
    }


def create_runtime_for_tests(tts: Any | None = None) -> DuplexRuntime:
    """Test helper: construct wired runtime without LiveKit room."""
    return DuplexRuntime.create(tts=tts)
