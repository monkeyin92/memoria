"""Freeze the companion a device session should speak and sound like."""

from __future__ import annotations

from typing import Any

from services.common.companions import CompanionDefinition
from services.control_api.app.mode_policy import FrozenMode, ModePolicy
from services.control_api.app.session_companion import custom_persona_id_or_none
from services.tutor.domain import SessionFocus


def voice_session_delivery_fields(frozen: FrozenMode) -> dict[str, Any]:
    return {
        "companion_style_id": frozen.companion_style_id,
        "companion_style_version": frozen.companion_style_version,
        "voice_profile_id": frozen.voice_profile_id,
        "voice_profile_version": frozen.voice_profile_version,
        "voice_provider": frozen.voice_provider,
        "voice_model": frozen.voice_model,
        "voice_resource_id": frozen.voice_resource_id,
        "voice_provider_expires_at": frozen.voice_provider_expires_at,
        "voice_speaker_sha256": frozen.voice_speaker_sha256,
        "fallback_voice_profile_id": frozen.fallback_voice_profile_id,
        "fallback_voice_provider": frozen.fallback_voice_provider,
        "fallback_voice_model": frozen.fallback_voice_model,
        "fallback_voice_resource_id": frozen.fallback_voice_resource_id,
    }


def custom_persona_id_for(companion: CompanionDefinition) -> str | None:
    """The persona id when this companion is a custom persona, else ``None``."""
    return custom_persona_id_or_none(companion.companion_id)


async def freeze_companion_delivery(
    *,
    companion: CompanionDefinition,
    session_focus: SessionFocus,
    account_id: str,
    store: Any,
    voice_manager: Any = None,
) -> FrozenMode:
    """Catalog companion with its designed voice only.

    ``account_id``, ``store``, and ``voice_manager`` remain for call-site
    compatibility. Personal voice clones are no longer attached.
    """
    del account_id, store, voice_manager
    return ModePolicy.freeze_companion(companion, session_focus=session_focus)
