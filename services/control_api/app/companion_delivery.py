"""Freeze the companion a device session should speak and sound like."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC
from typing import Any

from fastapi import HTTPException

from services.common.companions import DESIGNED_VOICE_MODEL, CompanionDefinition
from services.common.custom_persona import parse_custom_persona
from services.control_api.app.account_gate import require_capability_for_account_id
from services.control_api.app.mode_policy import FrozenMode, ModePolicy
from services.tutor.domain import SessionFocus
from services.voice_profile.domain import VoiceProfilePort, VoiceResolution

logger = logging.getLogger(__name__)


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


async def freeze_companion_delivery(
    *,
    companion: CompanionDefinition,
    session_focus: SessionFocus,
    bio: object,
    account_id: str,
    store: Any,
    voice_manager: VoiceProfilePort | None,
) -> FrozenMode:
    """Catalog companion, plus a personal clone when custom persona is active."""

    frozen = ModePolicy.freeze_companion(companion, session_focus=session_focus)
    if voice_manager is None or not parse_custom_persona(bio).active:
        return frozen
    try:
        require_capability_for_account_id(account_id, "voice_clone", store=store)
    except HTTPException:
        return frozen
    try:
        resolution = await voice_manager.resolve(account_id=account_id)
    except Exception:
        logger.exception("companion clone resolve failed account_id=%s", account_id)
        return frozen
    attached = _attach_personal_clone(frozen, companion=companion, resolution=resolution)
    return attached if attached is not None else frozen


def _attach_personal_clone(
    frozen: FrozenMode,
    *,
    companion: CompanionDefinition,
    resolution: VoiceResolution,
) -> FrozenMode | None:
    if not _clone_resolution_bindable(resolution) or resolution.voice_id is None:
        return None
    expires_at = (
        resolution.provider_expires_at.isoformat()
        if resolution.provider_expires_at is not None
        else None
    )
    from dataclasses import replace

    return replace(
        frozen,
        voice_profile_id=resolution.profile_id,
        voice_profile_version=resolution.version_number,
        voice_provider=resolution.provider,
        voice_model=resolution.model,
        voice_resource_id=resolution.resource_id,
        voice_provider_expires_at=expires_at,
        voice_speaker_sha256=hashlib.sha256(resolution.voice_id.encode("utf-8")).hexdigest(),
        fallback_voice_profile_id=companion.designed_voice_profile,
        fallback_voice_provider="volcengine_doubao",
        fallback_voice_model=DESIGNED_VOICE_MODEL,
        fallback_voice_resource_id=DESIGNED_VOICE_MODEL,
    )


def _clone_resolution_bindable(resolution: VoiceResolution) -> bool:
    if (
        resolution.mode != "active"
        or resolution.voice_kind != "personal"
        or not resolution.profile_id
        or resolution.version_number is None
        or resolution.version_number < 1
        or not resolution.voice_id
        or not resolution.model
        or not resolution.resource_id
        or not resolution.provider
    ):
        return False
    if resolution.provider == "volcengine_doubao":
        return (
            resolution.model == "seed-icl-2.0"
            and resolution.resource_id == "seed-icl-2.0"
            and resolution.provider_expires_at is not None
            and resolution.provider_expires_at.tzinfo is not None
            and resolution.provider_expires_at.utcoffset() == UTC.utcoffset(None)
        )
    return bool(
        resolution.provider == "alibaba_model_studio"
        and resolution.model.startswith("cosyvoice-v3.5-")
        and resolution.resource_id == resolution.model
    )
