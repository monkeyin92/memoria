"""Readiness stays closed until the current process receives fresh smoke evidence."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore

router = APIRouter(tags=["health"])


class SmokeChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    livekit: bool
    funasr: bool
    llm: bool
    llm_provider: Literal["qwen", "deepseek"]
    release_tag: str = Field(min_length=1, max_length=200)
    cosyvoice: bool

    @model_validator(mode="after")
    def require_all_passed(self) -> SmokeChecks:
        if not all((self.livekit, self.funasr, self.llm, self.cosyvoice)):
            raise ValueError("all readiness smokes must pass")
        return self


def _settings(request: Request) -> ControlSettings:
    return cast(ControlSettings, request.app.state.settings)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _missing_config(settings: ControlSettings) -> list[str]:
    missing: list[str] = []
    if not settings.livekit_api_key or not settings.livekit_api_secret:
        missing.append("LIVEKIT_CREDENTIALS")
    if not settings.dashscope_api_key.get_secret_value():
        missing.append("DASHSCOPE_API_KEY")
    if settings.llm_provider == "deepseek" and not settings.deepseek_api_key.get_secret_value():
        missing.append("DEEPSEEK_API_KEY")
    if not settings.memoria_release_tag.strip():
        missing.append("MEMORIA_RELEASE_TAG")
    return missing


def _smoke_state(request: Request, settings: ControlSettings) -> str:
    evidence = _store(request).get_readiness(
        release_tag=settings.memoria_release_tag,
        llm_provider=settings.llm_provider,
    )
    if evidence is None:
        return "not_run"
    try:
        marked_at = datetime.fromisoformat(str(evidence["marked_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return "invalid"
    if marked_at.tzinfo is None:
        return "invalid"
    age_s = (datetime.now(UTC) - marked_at.astimezone(UTC)).total_seconds()
    if age_s < 0:
        return "invalid"
    if age_s > settings.readiness_gate_ttl_s:
        return "expired"
    return "passed"


def _require_internal_secret(
    request: Request,
    authorization: str | None,
) -> None:
    scheme, separator, supplied = (authorization or "").partition(" ")
    expected = _settings(request).memoria_auth_secret.get_secret_value()
    valid = bool(separator and scheme.lower() == "bearer" and supplied)
    valid = valid and hmac.compare_digest(supplied.encode(), expected.encode())
    if not valid:
        raise HTTPException(status_code=401, detail="invalid internal authentication")


@router.post("/internal/readiness/smokes")
def mark_smokes_passed(
    body: SmokeChecks,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    _require_internal_secret(request, authorization)
    settings = _settings(request)
    if body.llm_provider != settings.llm_provider:
        raise HTTPException(status_code=409, detail="smoke LLM provider does not match config")
    if body.release_tag != settings.memoria_release_tag:
        raise HTTPException(status_code=409, detail="smoke release tag does not match config")
    missing = _missing_config(settings)
    if missing:
        raise HTTPException(status_code=503, detail={"missing": missing})
    _store(request).mark_readiness(
        release_tag=settings.memoria_release_tag,
        llm_provider=settings.llm_provider,
        marked_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    return {
        "status": "marked",
        "release_tag": settings.memoria_release_tag,
        "llm_provider": settings.llm_provider,
        "expires_in": settings.readiness_gate_ttl_s,
    }


@router.get("/health/ready")
def health_ready(request: Request) -> JSONResponse:
    settings = _settings(request)
    if settings.offline_mock:
        return JSONResponse(
            {
                "status": "ready",
                "mode": "offline_mock",
                "checks": {
                    "config": True,
                    "livekit": "skipped",
                    "funasr": "skipped",
                    "llm": {"provider": settings.llm_provider, "status": "skipped"},
                    "cosyvoice": "skipped",
                },
            }
        )

    missing = _missing_config(settings)
    smoke_state = _smoke_state(request, settings)
    if missing or smoke_state != "passed":
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "missing": missing,
                "smokes": smoke_state,
                "release_tag": settings.memoria_release_tag,
                "llm_provider": settings.llm_provider,
            },
        )
    return JSONResponse(
        {
            "status": "ready",
            "release_tag": settings.memoria_release_tag,
            "checks": {
                "config": True,
                "livekit": True,
                "funasr": True,
                "llm": {"provider": settings.llm_provider, "passed": True},
                "cosyvoice": True,
            },
        }
    )
