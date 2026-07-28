"""Mini Program media-gateway session boundary."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from services.common.miniprogram_gateway_ticket import issue_gateway_ticket


class MiniProgramAudioFormat(BaseModel):
    sample_rate: Literal[24000] = 24000
    channels: Literal[1] = 1
    sample_format: Literal["s16le"] = "s16le"
    frame_ms: Literal[20] = 20


class MiniProgramPlayoutPolicy(BaseModel):
    post_playout_guard_ms: int = Field(default=150, ge=0, le=2_000)


class MiniProgramMediaGateway(BaseModel):
    websocket_url: str
    ticket: str
    expires_in: int
    protocol_version: Literal[1] = 1
    audio: MiniProgramAudioFormat = Field(default_factory=MiniProgramAudioFormat)
    playout: MiniProgramPlayoutPolicy = Field(default_factory=MiniProgramPlayoutPolicy)


class CreateMiniProgramSessionResponse(BaseModel):
    """A Mini Program session never receives a direct LiveKit participant token."""

    session_id: str
    voice_backend: Literal["cascade"] = "cascade"
    config: dict[str, Any]
    interaction: dict[str, Any]
    learning_task_id: str | None = None
    media_gateway: MiniProgramMediaGateway


def require_gateway_url(settings: Any) -> str:
    websocket_url = str(settings.miniprogram_media_gateway_url).strip()
    if not websocket_url:
        raise HTTPException(
            status_code=503,
            detail={"code": "miniprogram_media_gateway_unavailable"},
        )
    return websocket_url


def build_gateway_ticket(
    settings: Any,
    *,
    session_id: str,
    user_id: str,
    room_name: str,
    identity: str,
) -> MiniProgramMediaGateway:
    websocket_url = require_gateway_url(settings)
    ticket, ttl = issue_gateway_ticket(
        secret=settings.memoria_miniprogram_gateway_ticket_secret.get_secret_value(),
        session_id=session_id,
        user_id=user_id,
        room_name=room_name,
        identity=identity,
        agent_name=settings.livekit_agent_name,
        ttl_s=settings.miniprogram_gateway_ticket_ttl_s,
    )
    return MiniProgramMediaGateway(
        websocket_url=websocket_url,
        ticket=ticket,
        expires_in=ttl,
        playout=MiniProgramPlayoutPolicy(
            post_playout_guard_ms=settings.miniprogram_post_playout_guard_ms,
        ),
    )
