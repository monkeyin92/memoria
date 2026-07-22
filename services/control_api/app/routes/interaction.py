"""Account and internal policy seams for the S2 interaction control plane."""

from __future__ import annotations

import hmac
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from services.common.companions import companion_definition
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.mode_policy import FrozenMode, InteractionMode, ModePolicy
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
)

router = APIRouter(prefix="/v1/interaction", tags=["interaction"])


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _require_policy_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    expected = cast(ControlSettings, request.app.state.settings).internal_token(
        "interaction_policy"
    )
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=401, detail="valid internal interaction policy token required"
        )


@router.get("/capabilities")
async def capabilities(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    profile = _store(request).get_profile(user_id=user.user_id, now="1970-01-01T00:00:00Z")
    companion = companion_definition(profile.get("companion_id"))
    return {
        "selected_companion_id": companion.companion_id if companion else None,
        "modes": {
            mode: ModePolicy.availability(cast(InteractionMode, mode)).payload()
            for mode in ("companion", "self_preview", "legacy", "archive")
        },
    }


class SessionPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    # Deprecated compatibility input. The session policy must not turn a
    # startup-time speaker guess into a permanent authorization decision.
    speaker_class: Literal["owner", "guest", "uncertain"] | None = None


@router.post("/session-policy")
async def session_policy(
    body: SessionPolicyRequest,
    request: Request,
    _: Annotated[None, Depends(_require_policy_token)],
) -> dict[str, Any]:
    return ModePolicy.session_context(
        FrozenMode.from_session(require_active_voice_session(request, body.session_id))
    )
