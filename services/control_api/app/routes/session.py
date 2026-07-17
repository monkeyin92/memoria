"""Session control routes (ch.20.2)."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    create_session_id,
    mint_participant_token,
    require_authenticated_user,
    require_matching_user,
)

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)
telemetry_logger = logging.getLogger("uvicorn.error")
CONTROL_TOPIC = "voice-agent.control"
QWEN_OMNI_FLASH_MODEL = "qwen3.5-omni-flash-realtime"
QWEN_OMNI_PLUS_MODEL = "qwen3.5-omni-plus-realtime"
# Backward-compatible alias used by older tests and docs.
QWEN_OMNI_MODEL = QWEN_OMNI_FLASH_MODEL
QWEN_OMNI_WORKSPACE_ID = "llm-qp8mf178biax7m6c"
OMNI_BACKENDS = frozenset({"qwen_omni", "qwen_omni_plus"})
OMNI_MODELS: dict[str, str] = {
    "qwen_omni": QWEN_OMNI_FLASH_MODEL,
    "qwen_omni_plus": QWEN_OMNI_PLUS_MODEL,
}
OMNI_MAX_SDP_BYTES = 64 * 1024
OMNI_MAX_SDP_EXCHANGES = 2
_WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")

_STOP_REQUESTS: list[dict[str, Any]] = []


class ClientInfo(BaseModel):
    platform: str = "web"
    timezone: str = "Asia/Shanghai"


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    voice_backend: Literal["cascade", "qwen_omni", "qwen_omni_plus"] = "cascade"
    locale: str = "zh-CN"
    client: ClientInfo = Field(default_factory=ClientInfo)


class CreateSessionResponse(BaseModel):
    session_id: str
    livekit_url: str
    room_name: str
    participant_token: str
    expires_in: int
    agent_name: str
    voice_backend: Literal["cascade"] = "cascade"
    config: dict[str, Any]


class CreateOmniSessionResponse(BaseModel):
    session_id: str
    voice_backend: Literal["qwen_omni", "qwen_omni_plus"] = "qwen_omni"
    sdp_exchange_path: str
    config: dict[str, Any]


class StopResponseBody(BaseModel):
    reason: str = "user_button"


class WebRTCMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jitter: float | None = Field(default=None, ge=0)
    packets_lost: int | None = Field(default=None, ge=0)
    packets_received: int | None = Field(default=None, ge=0)
    packets_discarded: int | None = Field(default=None, ge=0)
    bytes_received: int | None = Field(default=None, ge=0)
    nack_count: int | None = Field(default=None, ge=0)
    concealed_samples: int | None = Field(default=None, ge=0)
    silent_concealed_samples: int | None = Field(default=None, ge=0)
    total_samples_received: int | None = Field(default=None, ge=0)
    concealment_events: int | None = Field(default=None, ge=0)
    concealment_ratio: float | None = Field(default=None, ge=0, le=1)
    non_silent_concealment_ratio: float | None = Field(default=None, ge=0, le=1)
    jitter_buffer_delay: float | None = Field(default=None, ge=0)
    jitter_buffer_target_delay: float | None = Field(default=None, ge=0)
    jitter_buffer_minimum_delay: float | None = Field(default=None, ge=0)
    jitter_buffer_emitted_count: int | None = Field(default=None, ge=0)
    total_samples_duration: float | None = Field(default=None, ge=0)
    average_jitter_buffer_delay_ms: float | None = Field(default=None, ge=0)
    average_jitter_buffer_target_delay_ms: float | None = Field(default=None, ge=0)
    encoded_audio_bitrate_kbps: float | None = Field(default=None, ge=0)
    inserted_samples_for_deceleration: int | None = Field(default=None, ge=0)
    removed_samples_for_acceleration: int | None = Field(default=None, ge=0)


class OmniTelemetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal[
        "session_created",
        "room_connected",
        "agent_ready",
        "track_subscribed",
        "audio_attached",
        "play_resolved",
        "play_rejected",
        "playing",
        "first_playback",
        "media_error",
        "omni_session_created",
        "omni_welcome_armed",
        "omni_welcome_requested",
        "omni_welcome_suppressed",
        "omni_playout_buffer_configured",
        "omni_feedback_guard_started",
        "omni_feedback_suppressed",
        "omni_speech_started",
        "omni_speech_stopped",
        "omni_response_created",
        "omni_response_cancelled",
        "omni_cancelled_response_done",
        "omni_response_done",
        "webrtc_inbound_audio",
    ]
    elapsed_ms: int = Field(ge=0, le=24 * 60 * 60 * 1000)
    turn_id: int = Field(ge=0)
    generation_id: int = Field(ge=0)
    metrics: WebRTCMetrics | None = None


@router.post("", response_model=CreateSessionResponse | CreateOmniSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> CreateSessionResponse | CreateOmniSessionResponse:
    settings = request.app.state.settings
    user_id = require_matching_user(body.user_id, user) if body.user_id else user.user_id
    session_id = create_session_id()
    # The room name is also the Agent's trusted source for the public session id.
    room_name = f"voice-{session_id}"
    identity = f"user-{user_id}-{session_id[:8]}"
    store = cast(MemoryStore, request.app.state.memory_store)
    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if body.voice_backend in OMNI_BACKENDS:
        if not settings.dashscope_api_key.get_secret_value():
            raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
        model = OMNI_MODELS[body.voice_backend]
        _omni_signaling_url(settings, model=model)
        store.add_voice_session(
            session_id=session_id,
            user_id=user_id,
            room_name=room_name,
            voice_backend=body.voice_backend,
            created_at=created_at,
        )
        turn_detection = _omni_turn_detection(
            settings,
            voice_backend=body.voice_backend,
        )
        ab_profile = (
            f"{body.voice_backend}:silence={turn_detection['silence_duration_ms']}"
            f":th={turn_detection['threshold']}"
            f":pad={turn_detection['prefix_padding_ms']}"
        )
        # P1-8: fixed persona voice; optional clone id from DashScope voice product.
        clone_id = (getattr(settings, "qwen_omni_voice_clone_id", None) or "").strip()
        persona_voice = clone_id or (settings.qwen_omni_voice.strip() or "Cherry")
        persona_label = (
            getattr(settings, "qwen_omni_persona_label", None) or "Memoria 人设声"
        ).strip() or "Memoria 人设声"
        logger.info(
            "omni_session_ab_profile session_id=%s profile=%s model=%s voice=%s clone=%s",
            session_id,
            ab_profile,
            model,
            persona_voice,
            bool(clone_id),
        )
        return CreateOmniSessionResponse(
            session_id=session_id,
            voice_backend=body.voice_backend,  # type: ignore[arg-type]
            sdp_exchange_path=f"/v1/sessions/{session_id}/omni/sdp",
            config={
                "model": model,
                "voice": persona_voice,
                "persona": {
                    "label": persona_label,
                    "voice": persona_voice,
                    "cloned": bool(clone_id),
                },
                "turn_detection": turn_detection,
                # P0-3: flash vs plus A/B identity for client telemetry / greps.
                "ab_profile": ab_profile,
                "ab_scan": {
                    "backends": ["qwen_omni", "qwen_omni_plus"],
                    "silence_duration_ms_candidates": [500, 650, 800, 1000],
                    "threshold_candidates": [0.35, 0.45, 0.5, 0.55, 0.65],
                    "active": {
                        "voice_backend": body.voice_backend,
                        **turn_detection,
                    },
                },
            },
        )

    token, ttl = mint_participant_token(
        settings,
        room_name=room_name,
        identity=identity,
        agent_name=settings.livekit_agent_name,
    )
    store.add_voice_session(
        session_id=session_id,
        user_id=user_id,
        room_name=room_name,
        voice_backend=body.voice_backend,
        created_at=created_at,
    )
    return CreateSessionResponse(
        session_id=session_id,
        livekit_url=settings.livekit_url,
        room_name=room_name,
        participant_token=token,
        expires_in=ttl,
        agent_name=settings.livekit_agent_name,
        config={
            "locale": body.locale,
            "allow_text_fallback": True,
        },
    )


@router.post("/{session_id}/omni/sdp", response_class=Response)
async def exchange_omni_sdp(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> Response:
    """Exchange a browser WebRTC offer without exposing provider credentials."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    backend = str(rec["voice_backend"])
    if backend not in OMNI_BACKENDS:
        raise HTTPException(status_code=409, detail="session does not use Qwen Omni")

    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/sdp":
        raise HTTPException(status_code=415, detail="Content-Type must be application/sdp")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > OMNI_MAX_SDP_BYTES:
                raise HTTPException(status_code=413, detail="SDP offer is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
    offer_sdp = await request.body()
    if len(offer_sdp) > OMNI_MAX_SDP_BYTES:
        raise HTTPException(status_code=413, detail="SDP offer is too large")
    if not offer_sdp.lstrip().startswith(b"v=0"):
        raise HTTPException(status_code=422, detail="invalid SDP offer")

    if not store.reserve_omni_sdp_exchange(
        session_id=session_id,
        user_id=user.user_id,
        max_exchanges=OMNI_MAX_SDP_EXCHANGES,
    ):
        raise HTTPException(status_code=429, detail="SDP exchange limit reached")
    answer_sdp = await _exchange_omni_sdp(
        request.app.state.settings,
        offer_sdp,
        model=OMNI_MODELS[backend],
    )
    return Response(
        content=answer_sdp,
        media_type="application/sdp",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{session_id}/telemetry", status_code=204)
async def publish_omni_telemetry(
    session_id: str,
    body: OmniTelemetryBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> Response:
    """Accept a small, owner-bound timeline without audio, text, or credentials."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    if str(rec["voice_backend"]) not in OMNI_BACKENDS:
        raise HTTPException(status_code=409, detail="session does not use Qwen Omni")
    telemetry_logger.info(
        "omni_realtime_telemetry session_id=%s name=%s elapsed_ms=%s "
        "turn_id=%s generation_id=%s metrics=%s",
        session_id,
        body.name,
        body.elapsed_ms,
        body.turn_id,
        body.generation_id,
        body.metrics.model_dump(exclude_none=True) if body.metrics else {},
    )
    return Response(status_code=204)


def _omni_turn_detection(settings: Any, *, voice_backend: str) -> dict[str, Any]:
    """Build semantic_vad config; Plus may override silence/threshold for sweeps."""
    threshold = float(settings.qwen_omni_vad_threshold)
    silence_ms = int(settings.qwen_omni_silence_duration_ms)
    if voice_backend == "qwen_omni_plus":
        plus_threshold = getattr(settings, "qwen_omni_plus_vad_threshold", None)
        plus_silence = getattr(settings, "qwen_omni_plus_silence_duration_ms", None)
        if plus_threshold is not None:
            threshold = float(plus_threshold)
        if plus_silence is not None:
            silence_ms = int(plus_silence)
    return {
        "type": "semantic_vad",
        "threshold": threshold,
        "prefix_padding_ms": int(settings.qwen_omni_prefix_padding_ms),
        "silence_duration_ms": silence_ms,
    }


def _omni_signaling_url(settings: Any, *, model: str = QWEN_OMNI_FLASH_MODEL) -> str:
    workspace_id = settings.dashscope_workspace_id.strip() or QWEN_OMNI_WORKSPACE_ID
    if not _WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
    if model not in OMNI_MODELS.values():
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
    return (
        f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com"
        f"/api/v1/webrtc/realtime?model={model}"
    )


async def _exchange_omni_sdp(
    settings: Any,
    offer_sdp: bytes,
    *,
    model: str = QWEN_OMNI_FLASH_MODEL,
) -> bytes:
    api_key = settings.dashscope_api_key.get_secret_value()
    if not api_key:
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 服务端尚未配置")
    try:
        async with httpx.AsyncClient(
            timeout=settings.qwen_omni_sdp_timeout_s,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                _omni_signaling_url(settings, model=model),
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/sdp",
                },
                content=offer_sdp,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Qwen3.5-Omni 建连超时") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Qwen3.5-Omni 建连失败") from exc

    if response.status_code == 429:
        raise HTTPException(status_code=503, detail="Qwen3.5-Omni 暂时繁忙")
    if response.status_code < 200 or response.status_code >= 300:
        logger.warning("Qwen Omni SDP exchange failed with status %s", response.status_code)
        raise HTTPException(status_code=502, detail="Qwen3.5-Omni 建连失败")
    answer_sdp = response.content
    if (
        not answer_sdp.lstrip().startswith(b"v=0")
        or len(answer_sdp) > OMNI_MAX_SDP_BYTES
    ):
        raise HTTPException(status_code=502, detail="Qwen3.5-Omni 返回了无效连接响应")
    return answer_sdp


async def _send_room_control(settings: Any, *, room_name: str, event: dict[str, Any]) -> None:
    """Send a server-originated reliable packet to one LiveKit room."""
    from livekit import api

    async with api.LiveKitAPI(
        settings.livekit_url,
        settings.livekit_api_key,
        settings.livekit_api_secret,
    ) as livekit:
        await livekit.room.send_data(
            api.SendDataRequest(
                room=room_name,
                data=json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode(),
                kind=api.DataPacket.Kind.RELIABLE,
                topic=CONTROL_TOPIC,
            )
        )


@router.post("/{session_id}/stop-response")
async def stop_response(
    session_id: str,
    body: StopResponseBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Same atomic cancel semantics as voice barge-in; no new user turn."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    event = {
        "type": "stop_response",
        "session_id": session_id,
        "reason": body.reason,
        "action": "atomic_cancel",
        "create_user_turn": False,
    }
    settings = request.app.state.settings
    if not settings.offline_mock:
        try:
            await _send_room_control(settings, room_name=str(rec["room_name"]), event=event)
        except Exception as exc:
            logger.warning("failed to route stop-response to LiveKit room", exc_info=True)
            raise HTTPException(status_code=502, detail="stop-response routing failed") from exc
    _STOP_REQUESTS.append(event)
    return {"ok": True, **event}


@router.post("/{session_id}/rtc-recovered")
async def rtc_recovered(
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Fence off media/data queued before a successful client RTC reconnect."""
    store = cast(MemoryStore, request.app.state.memory_store)
    rec = store.get_voice_session(session_id=session_id, user_id=user.user_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="session not found")
    event = {
        "type": "rtc_recovered",
        "session_id": session_id,
        "action": "advance_generation",
        "create_user_turn": False,
    }
    settings = request.app.state.settings
    if not settings.offline_mock:
        try:
            await _send_room_control(settings, room_name=str(rec["room_name"]), event=event)
        except Exception as exc:
            logger.warning("failed to route rtc-recovered to LiveKit room", exc_info=True)
            raise HTTPException(status_code=502, detail="rtc recovery routing failed") from exc
    return {"ok": True, **event}


def get_stop_requests() -> list[dict[str, Any]]:
    return list(_STOP_REQUESTS)


def reset_session_state() -> None:
    _STOP_REQUESTS.clear()
