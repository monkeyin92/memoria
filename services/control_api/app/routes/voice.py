"""Designed-voice session resolution only.

Voice cloning and provider sample storage were removed (product decision
2026-10-13). Personal clone fields on a frozen session fall through to the
designed companion voice or the generic fallback.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.common.companions import (
    DEFAULT_COMPANION_ID,
    DESIGNED_VOICE_MODEL,
    designed_voice_profile,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.mode_policy import FrozenMode
from services.control_api.app.security import require_active_voice_session

router = APIRouter(prefix="/v1/voices", tags=["voices"])


def _require_internal_token(
    request: Request,
    x_memoria_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).internal_token(
        "voice_resolution"
    )
    if not expected or not x_memoria_internal_token or x_memoria_internal_token != expected:
        raise HTTPException(status_code=401, detail="internal token required")


class SessionResolutionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)


def _designed_payload(profile_id: str) -> dict[str, Any]:
    return {
        "mode": "designed",
        "profile_id": profile_id,
        "provider": "volcengine_doubao",
        "voice_kind": "designed",
        "model": DESIGNED_VOICE_MODEL,
        "resource_id": DESIGNED_VOICE_MODEL,
        "voice_id": None,
        "speaker_sha256": None,
    }


def _fallback_payload() -> dict[str, Any]:
    return {
        "mode": "fallback",
        "profile_id": None,
        "provider": None,
        "voice_kind": None,
        "model": None,
        "resource_id": None,
        "voice_id": None,
        "speaker_sha256": None,
    }


@router.post("/session-resolution")
async def session_resolution(
    body: SessionResolutionCreate,
    request: Request,
    _: Annotated[None, Depends(_require_internal_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    frozen = FrozenMode.from_session(session)
    if frozen.interaction_mode == "companion":
        designed_profile = designed_voice_profile(
            frozen.companion_style_id or DEFAULT_COMPANION_ID
        )
        if designed_profile is not None:
            return _designed_payload(designed_profile)
    if frozen.interaction_mode in {"self_preview", "legacy"}:
        if (
            frozen.fallback_voice_profile_id is not None
            and frozen.fallback_voice_provider == "volcengine_doubao"
            and frozen.fallback_voice_model == DESIGNED_VOICE_MODEL
            and frozen.fallback_voice_resource_id == DESIGNED_VOICE_MODEL
        ):
            return _designed_payload(frozen.fallback_voice_profile_id)
    return _fallback_payload()
