"""Resolve and apply the voice profile a fenced turn is allowed to speak with."""

from __future__ import annotations

import logging
from typing import Any

from services.agent.src.context_assembler import heard_only_chat_context
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.providers.doubao_voice_catalog import resolve_approved_voice
from services.agent.src.voice_profile_client import VoiceProfileClient, VoiceRuntimeProfile
from services.common.companions import DESIGNED_VOICE_MODEL

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


def _heard_only_chat_context(chat_ctx: Any, heard_assistant: list[str]) -> Any:
    return heard_only_chat_context(chat_ctx, heard_assistant)
