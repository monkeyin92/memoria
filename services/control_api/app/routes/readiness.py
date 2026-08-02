"""Readiness stays closed until the current process receives fresh smoke evidence."""

from __future__ import annotations

import hmac
from asyncio import to_thread
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Literal, cast
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.archive.domain import ContextQuery, LifeArchivePort
from services.archive.memory_domain import MemoryCatalogPort
from services.archive.object_store import ObjectStore
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.persona.domain import PersonaEnginePort
from services.speaker.domain import SpeakerAuthorityPort
from services.voice_profile.domain import VoiceProfilePort

router = APIRouter(tags=["health"])
AGENT_HEARTBEAT_MAX_AGE_S = 45


class TTSSmokeChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["doubao"]
    audio: bool
    word_timestamps: bool


class SmokeChecks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    livekit: bool
    funasr: bool
    llm: bool
    llm_provider: Literal["qwen", "bailian_deepseek", "deepseek"]
    release_tag: str = Field(min_length=1, max_length=200)
    tts: TTSSmokeChecks

    @model_validator(mode="after")
    def require_all_passed(self) -> SmokeChecks:
        if not all(
            (
                self.livekit,
                self.funasr,
                self.llm,
                self.tts.audio,
                self.tts.word_timestamps,
            )
        ):
            raise ValueError("all readiness smokes must pass")
        return self


class AgentHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    release_tag: str = Field(min_length=1, max_length=200)
    boot_id: UUID
    worker_ready: bool
    livekit_ready: bool
    last_loop_at: datetime

    @field_validator("last_loop_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("agent heartbeat timestamp must include timezone")
        return value.astimezone(UTC)


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


def _valid_configuration(settings: ControlSettings) -> bool:
    try:
        settings.validate_production()
    except ValueError:
        return False
    return True


def _smoke_state(request: Request, settings: ControlSettings) -> str:
    try:
        evidence = _store(request).get_readiness(
            release_tag=settings.memoria_release_tag,
            llm_provider=settings.llm_provider,
        )
    except Exception:
        return "unavailable"
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


def _component(request: Request, name: str) -> object:
    component = getattr(request.app.state, name, None)
    if component is None:
        raise RuntimeError(f"readiness component is missing: {name}")
    return component


async def _probe_object_store(store: ObjectStore) -> object:
    # ponytail: use the existing public seam; add a cached native health probe if polling gets hot.
    payload = b"memoria-readiness"
    reference = await store.put(
        account_id="memoria-readiness-probe",
        purpose="readiness",
        data=payload,
        media_type="application/octet-stream",
    )
    try:
        if await store.get(reference) != payload:
            raise RuntimeError("object store readiness payload mismatch")
    finally:
        await store.delete(reference)
    return None


async def _probe_speaker_model(
    settings: ControlSettings,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    endpoint = settings.speaker_embedding_url.strip()
    if not endpoint:
        return "skipped" if settings.environment != "production" else "unavailable"
    expected_model = settings.speaker_embedding_model.strip()
    if not expected_model:
        return "unavailable"
    try:
        health_url = httpx.URL(endpoint).copy_with(
            path="/health/ready",
            query=None,
            fragment=None,
        )
    except (TypeError, ValueError):
        return "unavailable"

    owned_client = client is None
    runtime_client = client or httpx.AsyncClient(timeout=settings.speaker_embedding_timeout_s)
    try:
        response = await runtime_client.get(
            health_url,
            timeout=settings.speaker_embedding_timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, TypeError, ValueError):
        return "unavailable"
    finally:
        if owned_client:
            await runtime_client.aclose()
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "ready"
        or payload.get("model_version") != expected_model
    ):
        return "unavailable"
    return "ready"


async def _core_checks(
    request: Request,
    settings: ControlSettings,
) -> dict[str, str]:
    async def control_database() -> object:
        return await to_thread(
            _store(request).get_readiness,
            release_tag=settings.memoria_release_tag,
            llm_provider=settings.llm_provider,
        )

    async def memory_archive() -> object:
        archive = cast(LifeArchivePort, _component(request, "life_archive"))
        return await archive.context(
            ContextQuery(
                account_id="memoria-readiness-probe",
                speaker_class="guest",
                limit=1,
            )
        )

    async def memory_catalog() -> object:
        catalog = cast(MemoryCatalogPort, _component(request, "memory_catalog"))
        return await catalog.timeline(account_id="memoria-readiness-probe", limit=1)

    async def persona() -> object:
        engine = cast(PersonaEnginePort, _component(request, "persona_engine"))
        return await engine.traits(account_id="memoria-readiness-probe")

    async def speaker_authority() -> object:
        authority = cast(SpeakerAuthorityPort, _component(request, "speaker_authority"))
        return await authority.profiles("memoria-readiness-probe")

    async def voice_profile() -> object:
        profiles = cast(VoiceProfilePort, _component(request, "voice_profile_manager"))
        return await profiles.profiles(account_id="memoria-readiness-probe")

    async def archive_object_store() -> object:
        store = cast(ObjectStore, _component(request, "archive_object_store"))
        return await _probe_object_store(store)

    async def voice_object_store() -> object:
        store = cast(ObjectStore, _component(request, "voice_object_store"))
        return await _probe_object_store(store)

    probes: tuple[tuple[str, Callable[[], Awaitable[object]]], ...] = (
        ("control_database", control_database),
        ("memory_archive", memory_archive),
        ("memory_catalog", memory_catalog),
        ("persona", persona),
        ("speaker_authority", speaker_authority),
        ("voice_profile", voice_profile),
        ("archive_object_store", archive_object_store),
        ("voice_object_store", voice_object_store),
    )
    checks: dict[str, str] = {}
    for name, probe in probes:
        try:
            await probe()
        except Exception:
            checks[name] = "unavailable"
        else:
            checks[name] = "ready"
    try:
        checks["speaker_model"] = await _probe_speaker_model(settings)
    except Exception:
        checks["speaker_model"] = "unavailable"
    return checks


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


def _require_agent_token(request: Request, supplied: str | None) -> None:
    expected = _settings(request).internal_token("agent_heartbeat")
    if not expected or supplied is None or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid agent heartbeat authentication")


def _agent_state(request: Request, settings: ControlSettings) -> dict[str, object]:
    if settings.environment != "production":
        return {"status": "skipped"}
    heartbeat = cast(AgentHeartbeat | None, getattr(request.app.state, "agent_heartbeat", None))
    if heartbeat is None:
        return {"status": "missing"}
    state: dict[str, object] = {
        "release_tag": heartbeat.release_tag,
        "boot_id": str(heartbeat.boot_id),
        "worker_ready": heartbeat.worker_ready,
        "livekit_ready": heartbeat.livekit_ready,
        "last_loop_at": heartbeat.last_loop_at.isoformat(),
    }
    age_s = (datetime.now(UTC) - heartbeat.last_loop_at).total_seconds()
    if heartbeat.release_tag != settings.memoria_release_tag:
        status = "release_mismatch"
    elif age_s < 0:
        status = "invalid"
    elif age_s > AGENT_HEARTBEAT_MAX_AGE_S:
        status = "stale"
    elif not heartbeat.worker_ready or not heartbeat.livekit_ready:
        status = "starting"
    else:
        status = "ready"
    return {"status": status, **state}


@router.post("/internal/readiness/agent-heartbeat")
def record_agent_heartbeat(
    body: AgentHeartbeat,
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> dict[str, object]:
    _require_agent_token(request, token)
    settings = _settings(request)
    if body.release_tag != settings.memoria_release_tag:
        raise HTTPException(status_code=409, detail="agent release tag does not match config")
    request.app.state.agent_heartbeat = body
    return {
        "status": "recorded",
        "release_tag": body.release_tag,
        "boot_id": str(body.boot_id),
        "last_loop_at": body.last_loop_at.isoformat(),
        "expires_in": AGENT_HEARTBEAT_MAX_AGE_S,
    }


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
    if not _valid_configuration(settings):
        raise HTTPException(status_code=503, detail="invalid production configuration")
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
async def health_ready(request: Request) -> JSONResponse:
    settings = _settings(request)
    config_ready = _valid_configuration(settings)
    core_checks = await _core_checks(request, settings)
    core_ready = all(status in {"ready", "skipped"} for status in core_checks.values())
    if settings.offline_mock:
        return JSONResponse(
            status_code=200 if config_ready and core_ready else 503,
            content={
                "status": "ready" if config_ready and core_ready else "not_ready",
                "mode": "offline_mock",
                "checks": {
                    "config": config_ready,
                    "core": core_checks,
                    "agent": {"status": "skipped"},
                    "livekit": "skipped",
                    "funasr": "skipped",
                    "llm": {"provider": settings.llm_provider, "status": "skipped"},
                    "tts": {"provider": "doubao", "status": "skipped"},
                },
            },
        )

    missing = _missing_config(settings)
    smoke_state = _smoke_state(request, settings)
    agent_state = _agent_state(request, settings)
    agent_ready = agent_state["status"] in {"ready", "skipped"}
    if not config_ready or missing or smoke_state != "passed" or not core_ready or not agent_ready:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "missing": missing,
                "smokes": smoke_state,
                "release_tag": settings.memoria_release_tag,
                "llm_provider": settings.llm_provider,
                "checks": {
                    "config": config_ready and not missing,
                    "core": core_checks,
                    "agent": agent_state,
                },
            },
        )
    return JSONResponse(
        {
            "status": "ready",
            "release_tag": settings.memoria_release_tag,
            "checks": {
                "config": True,
                "core": core_checks,
                "agent": agent_state,
                "livekit": True,
                "funasr": True,
                "llm": {"provider": settings.llm_provider, "passed": True},
                "tts": {
                    "provider": "doubao",
                    "audio": True,
                    "word_timestamps": True,
                },
            },
        }
    )
