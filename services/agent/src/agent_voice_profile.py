"""Resolve and apply the voice profile a fenced turn is allowed to speak with."""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Literal, cast

from services.agent.src.context_assembler import heard_only_chat_context
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.generation_output_policy import (
    frozen_companion_clone_permitted,
    generation_voice_profile_id,
    generation_voice_reject_reason,
)
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.providers.doubao_voice_catalog import resolve_approved_voice
from services.agent.src.voice_profile_client import VoiceProfileClient, VoiceRuntimeProfile
from services.common.companions import (
    DEFAULT_COMPANION_ID,
    DESIGNED_VOICE_MODEL,
    companion_definition,
    designed_voice_profile,
)

logger = logging.getLogger(__name__)


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
    selected_fallback = _frozen_designed_fallback(policy) or _policy_designed_voice_profile(policy)
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
        (mode == "companion" and profile.voice_kind != "designed" and not personal_matches)
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


def _heard_only_chat_context(chat_ctx: Any, heard_assistant: list[str]) -> Any:
    return heard_only_chat_context(chat_ctx, heard_assistant)


def _designed_runtime_profile(profile_id: str) -> VoiceRuntimeProfile | None:
    voice = resolve_approved_voice(profile_id=profile_id, model=DESIGNED_VOICE_MODEL)
    if voice is None:
        return None
    return VoiceRuntimeProfile(
        profile_id=profile_id,
        model=DESIGNED_VOICE_MODEL,
        voice_id=voice,
        provider="volcengine_doubao",
        voice_kind="designed",
        resource_id=DESIGNED_VOICE_MODEL,
    )


def _policy_designed_voice_profile(policy: ModePolicy | None) -> VoiceRuntimeProfile | None:
    if policy is None:
        return None
    if policy.mode == "companion":
        companion = companion_definition(policy.companion_style_id)
        profile_id = (
            companion.designed_voice_profile
            if companion is not None
            else designed_voice_profile(DEFAULT_COMPANION_ID)
        )
        return _designed_runtime_profile(profile_id) if isinstance(profile_id, str) else None
    return _frozen_designed_fallback(policy)


def _tts_voice_fields(tts_plugin: Any) -> tuple[str, str, str, str] | None:
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
        return None
    return profile_id, resource_id, speaker, voice_kind


def _tts_reject_reason(
    policy: ModePolicy,
    tts_plugin: Any,
    *,
    personal_voice_permitted: bool,
) -> str | None:
    fields = _tts_voice_fields(tts_plugin)
    if fields is None:
        return "tts_fields"
    profile_id, resource_id, speaker, voice_kind = fields
    if voice_kind not in {"designed", "personal"}:
        return "voice_kind"
    archive_profile_id = generation_voice_profile_id(
        policy,
        profile_id=profile_id,
        voice_kind=voice_kind,
    )
    return generation_voice_reject_reason(
        policy,
        personal_voice_permitted=personal_voice_permitted,
        profile_id=archive_profile_id,
        resource_id=resource_id,
        speaker_sha256=hashlib.sha256(speaker.encode()).hexdigest(),
        voice_kind=cast(Literal["designed", "personal"], voice_kind),
    )


def align_tts_voice_to_policy(
    tts_plugin: Any,
    policy: ModePolicy | None,
    *,
    personal_voice_permitted: bool = False,
) -> None:
    """Apply the mode-allowed designed voice when the current TTS identity cannot bind."""

    if policy is None:
        return
    if (
        _tts_reject_reason(
            policy,
            tts_plugin,
            personal_voice_permitted=personal_voice_permitted,
        )
        is None
    ):
        return
    designed = _policy_designed_voice_profile(policy)
    apply_profile = getattr(tts_plugin, "apply_voice_profile", None)
    if designed is not None and callable(apply_profile):
        try:
            apply_profile(
                model=designed.model,
                voice=designed.voice_id,
                profile_id=designed.profile_id,
                provider=designed.provider,
                voice_kind=designed.voice_kind,
                resource_id=designed.resource_id,
            )
            return
        except (TypeError, ValueError):
            pass
    if policy.mode in {"self_preview", "legacy"}:
        return
    baseline = getattr(tts_plugin, "use_baseline_voice", None)
    if callable(baseline):
        baseline()


def generation_tts_voice_can_bind(runtime: Any) -> bool:
    return bind_generation_tts_voice(runtime, runtime.fence, snapshot=False)


def bind_generation_tts_voice(
    runtime: Any,
    fence: GenerationFence,
    *,
    snapshot: bool = True,
) -> bool:
    tts_plugin = runtime.tts
    if tts_plugin is None:
        return False
    policy = runtime.mode_policy_for_fence(fence)
    personal_voice_permitted = runtime.profile_permits(
        fence, capability="voice_clone_use"
    ) or frozen_companion_clone_permitted(policy)
    align_tts_voice_to_policy(
        tts_plugin,
        policy,
        personal_voice_permitted=personal_voice_permitted,
    )
    fields = _tts_voice_fields(tts_plugin)
    fence_match = runtime.fence.matches(fence)
    reason: str | None
    profile_id: object
    resource_id: object
    voice_kind: object
    archive_profile_id: str | None
    if fields is None:
        reason = "tts_fields"
        profile_id = getattr(tts_plugin, "current_voice_profile_id", None)
        resource_id = getattr(tts_plugin, "current_model", None)
        voice_kind = getattr(tts_plugin, "current_voice_kind", None)
        archive_profile_id = None
    else:
        profile_id, resource_id, speaker, voice_kind = fields
        archive_profile_id = generation_voice_profile_id(
            policy,
            profile_id=profile_id,
            voice_kind=voice_kind,
        )
        speaker_sha256 = hashlib.sha256(speaker.encode()).hexdigest()
        if snapshot and not fence_match:
            reason = "fence_mismatch"
        elif voice_kind not in {"designed", "personal"}:
            reason = "voice_kind"
        else:
            reason = generation_voice_reject_reason(
                policy,
                personal_voice_permitted=personal_voice_permitted,
                profile_id=archive_profile_id,
                resource_id=resource_id,
                speaker_sha256=speaker_sha256,
                voice_kind=cast(Literal["designed", "personal"], voice_kind),
            )
            if reason is None and not snapshot:
                return True
            if reason is None and runtime.bind_generation_voice(
                fence,
                profile_id=archive_profile_id,
                resource_id=resource_id,
                speaker_sha256=speaker_sha256,
                voice_kind=cast(Literal["designed", "personal"], voice_kind),
            ):
                return True
            if reason is None:
                reason = "bind_rejected"
    logger.error(
        "generation voice binding rejected reason=%s mode=%s companion_style_id=%s "
        "tts_profile_id=%s archive_profile_id=%s resource_id=%s voice_kind=%s "
        "fence_match=%s session_id=%s turn_id=%s generation_id=%s",
        reason,
        policy.mode,
        policy.companion_style_id,
        profile_id,
        archive_profile_id,
        resource_id,
        voice_kind,
        fence_match,
        fence.session_id,
        fence.turn_id,
        fence.generation_id,
    )
    return False
