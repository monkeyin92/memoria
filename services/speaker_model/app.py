"""Authenticated HTTP boundary for CAM++ speaker embeddings."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal, cast

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from services.speaker_model.config import SpeakerModelSettings
from services.speaker_model.engine import CampPlusOnnxEngine, SpeakerModelEngine

_LOGGER = logging.getLogger(__name__)


class EmbeddingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    audio_base64: str = Field(min_length=4, max_length=6 * 1024 * 1024)
    encoding: str = Field(pattern="^pcm_s16le$")
    sample_rate: Literal[16_000]


class EmbeddingResponse(BaseModel):
    model_version: str
    embedding: list[float]
    speech_ms: int
    snr_db: float
    quality_score: float
    replay_risk: float
    synthetic_risk: float
    risk_assessment: Literal["verified", "unavailable"]


def _require_bearer(authorization: str | None, expected: str) -> None:
    if authorization is None:
        raise HTTPException(status_code=401, detail="valid bearer token required")
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(token, expected)
    ):
        raise HTTPException(status_code=401, detail="valid bearer token required")


def _decode_audio(value: str, *, max_bytes: int) -> bytes:
    try:
        pcm = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="audio_base64 is invalid") from exc
    if not pcm or len(pcm) > max_bytes or len(pcm) % 2:
        raise HTTPException(status_code=422, detail="PCM audio is empty, oversized or malformed")
    return pcm


def create_app(
    *,
    settings: SpeakerModelSettings | None = None,
    engine: SpeakerModelEngine | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.settings is None:
            app.state.settings = SpeakerModelSettings.from_env()
        runtime_settings = cast(SpeakerModelSettings, app.state.settings)
        if app.state.engine is None:
            app.state.engine = CampPlusOnnxEngine(
                runtime_settings.model_path,
                model_version=runtime_settings.model_version,
                sha256_path=runtime_settings.model_sha256_path,
            )
        app.state.inference_slots = asyncio.Semaphore(runtime_settings.max_concurrency)
        yield

    app = FastAPI(title="Memoria Speaker Model", version="1", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.inference_slots = asyncio.Semaphore(settings.max_concurrency if settings else 1)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        runtime_engine = cast(SpeakerModelEngine | None, request.app.state.engine)
        if runtime_engine is None:
            raise HTTPException(status_code=503, detail="speaker model is not loaded")
        return {"status": "ready", "model_version": runtime_engine.model_version}

    @app.post(
        "/v1/embeddings/speaker",
        response_model=EmbeddingResponse,
        status_code=status.HTTP_200_OK,
    )
    async def embedding(
        body: EmbeddingRequest,
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        runtime_settings = cast(SpeakerModelSettings | None, request.app.state.settings)
        runtime_engine = cast(SpeakerModelEngine | None, request.app.state.engine)
        if runtime_settings is None or runtime_engine is None:
            raise HTTPException(status_code=503, detail="speaker model is not ready")
        _require_bearer(authorization, runtime_settings.token)
        pcm = _decode_audio(body.audio_base64, max_bytes=runtime_settings.max_audio_bytes)
        if len(pcm) < body.sample_rate // 10 * 2:
            raise HTTPException(status_code=422, detail="PCM audio must contain at least 100 ms")
        slots = cast(asyncio.Semaphore, request.app.state.inference_slots)
        try:
            async with slots:
                result = await asyncio.to_thread(
                    runtime_engine.embed,
                    pcm,
                    sample_rate=body.sample_rate,
                )
        except Exception as exc:
            _LOGGER.exception("speaker model inference failed")
            raise HTTPException(
                status_code=503,
                detail="speaker model inference unavailable",
            ) from exc
        return {
            "model_version": runtime_engine.model_version,
            "embedding": result.vector,
            "speech_ms": result.speech_ms,
            "snr_db": result.snr_db,
            "quality_score": result.quality_score,
            "replay_risk": result.replay_risk,
            "synthetic_risk": result.synthetic_risk,
            "risk_assessment": result.risk_assessment,
        }

    return app


app = create_app()
