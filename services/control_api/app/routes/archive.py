"""Authenticated archive ledger and timeline APIs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import logging
import math
import wave
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.archive.compiler_worker import MemoryCompilerWorker
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    LifeArchivePort,
    RawVoiceConsent,
    RawVoiceConsentRequiredError,
)
from services.archive.memory_domain import (
    MemoryCatalogPort,
    MemoryCategory,
    MemoryClaimReview,
    MemorySearchQuery,
)
from services.archive.object_store import ObjectRef, ObjectStore
from services.control_api.app.account_gate import (
    AccountDeletingError,
    AccountOperationGate,
    require_writable_account,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
    verify_password,
)
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionIncompleteError,
)
from services.persona.domain import PersonaEnginePort, PersonaEvidence

router = APIRouter(prefix="/v1/archive", tags=["archive"])
logger = logging.getLogger(__name__)
MAX_RAW_VOICE_WAV_BYTES = 2 * 1024 * 1024
MAX_RAW_VOICE_BASE64_CHARS = ((MAX_RAW_VOICE_WAV_BYTES + 2) // 3) * 4


class EvidenceEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=96)
    occurred_at: datetime
    speaker_class: Literal["owner", "guest", "uncertain", "assistant", "system"]
    source: str = Field(min_length=1, max_length=96)
    payload: dict[str, Any]
    session_id: str | None = Field(default=None, max_length=128)
    turn_id: int | None = Field(default=None, ge=0)
    generation_id: int | None = Field(default=None, ge=0)
    speaker_identity_id: str | None = Field(default=None, max_length=128)
    consent_grant_id: str | None = Field(default=None, max_length=128)
    schema_version: int = Field(default=1, ge=1, le=100)
    supersedes_event_id: str | None = Field(default=None, max_length=128)

    @field_validator("payload")
    @classmethod
    def limit_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return _limit_payload(payload)


class SessionEvidenceEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    event_type: Literal[
        "speech.utterance_finalized",
        "speaker.classified",
        "assistant.playout_progressed",
        "assistant.playout_stopped",
    ]
    occurred_at: datetime
    speaker_class: Literal["owner", "guest", "uncertain", "assistant"]
    source: str = Field(min_length=1, max_length=96)
    payload: dict[str, Any]
    turn_id: int | None = Field(default=None, ge=0)
    generation_id: int | None = Field(default=None, ge=0)
    speaker_identity_id: str | None = Field(default=None, max_length=128)
    consent_grant_id: str | None = Field(default=None, max_length=128)
    schema_version: int = Field(default=1, ge=1, le=100)
    supersedes_event_id: str | None = Field(default=None, max_length=128)

    @field_validator("payload")
    @classmethod
    def limit_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        return _limit_payload(payload)

    @model_validator(mode="after")
    def enforce_playout_evidence(self) -> SessionEvidenceEventCreate:
        if self.event_type.startswith("assistant."):
            if self.speaker_class != "assistant" or self.payload.get("actual_heard") is not True:
                raise ValueError("assistant archive events require actual-heard evidence")
        elif self.speaker_class == "assistant":
            raise ValueError("assistant speaker_class is only valid for assistant events")
        return self


class RawVoiceConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    policy_version: str = Field(
        default="raw-voice-archive-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    retention_policy: Literal["account_lifetime"] = "account_lifetime"


class SessionRawAudioCreate(SessionEvidenceEventCreate):
    event_type: Literal["speech.utterance_finalized"]
    speaker_class: Literal["owner", "guest", "uncertain"]
    audio_base64: str = Field(min_length=1, max_length=MAX_RAW_VOICE_BASE64_CHARS)
    media_type: Literal["audio/wav"] = "audio/wav"
    retention_policy: Literal["account_lifetime"] = "account_lifetime"


class SessionMemoryContextCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=128)
    speaker_class: Literal["owner", "guest", "uncertain"]
    topic: str = Field(default="", max_length=1000)
    limit: int = Field(default=8, ge=1, le=20)


def _limit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    import json

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > 64 * 1024:
        raise ValueError("payload must not exceed 64 KiB")
    return payload


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _archive_objects(request: Request) -> ObjectStore:
    return cast(ObjectStore, request.app.state.archive_object_store)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _catalog(request: Request) -> MemoryCatalogPort:
    return cast(MemoryCatalogPort, request.app.state.memory_catalog)


def _governance(request: Request) -> AccountDataGovernance:
    return cast(AccountDataGovernance, request.app.state.account_data_governance)


def _ensure_account_writable(request: Request, account_id: str) -> None:
    if _store(request).is_account_unavailable(user_id=account_id):
        raise HTTPException(status_code=409, detail="account deletion is in progress")


def _require_registered(request: Request, user: AuthenticatedUser) -> None:
    if _store(request).get_account(user_id=user.user_id) is None:
        raise HTTPException(status_code=403, detail="register an account before raw voice archive")


@asynccontextmanager
async def _account_write(request: Request, account_id: str) -> AsyncIterator[None]:
    gate = cast(AccountOperationGate, request.app.state.account_operations)
    try:
        async with gate.write(account_id):
            _ensure_account_writable(request, account_id)
            yield
    except AccountDeletingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _wake_compiler(request: Request) -> None:
    worker = getattr(request.app.state, "memory_compiler_worker", None)
    if isinstance(worker, MemoryCompilerWorker):
        worker.wake()


def _consent_payload(consent: RawVoiceConsent) -> dict[str, Any]:
    return {
        "consent_grant_id": consent.consent_grant_id,
        "policy_version": consent.policy_version,
        "retention_policy": consent.retention_policy,
        "granted_at": consent.granted_at.isoformat(),
        "revoked_at": consent.revoked_at.isoformat() if consent.revoked_at else None,
    }


def _decode_owner_wav(encoded: str) -> bytes:
    try:
        audio = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="audio_base64 must be valid base64") from exc
    if not audio or len(audio) > MAX_RAW_VOICE_WAV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"raw voice WAV must not exceed {MAX_RAW_VOICE_WAV_BYTES} bytes",
        )
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            frame_count = reader.getnframes()
            frame_bytes = reader.readframes(frame_count)
            valid = (
                reader.getnchannels() == 1
                and reader.getsampwidth() == 2
                and reader.getframerate() == 16_000
                and reader.getcomptype() == "NONE"
                and frame_count > 0
                and len(frame_bytes) == frame_count * 2
            )
    except (EOFError, wave.Error) as exc:
        raise HTTPException(status_code=422, detail="raw voice must be a valid WAV") from exc
    if not valid:
        raise HTTPException(
            status_code=422,
            detail="raw voice WAV must be mono PCM16 at 16000 Hz",
        )
    return audio


async def _put_archive_object(
    store: ObjectStore,
    *,
    account_id: str,
    data: bytes,
) -> ObjectRef:
    task = asyncio.create_task(
        store.put(
            account_id=account_id,
            purpose="raw-voice-archive",
            data=data,
            media_type="audio/wav",
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        reference = await asyncio.shield(task)
        await asyncio.shield(store.delete(reference))
        raise


async def _delete_object_safely(store: ObjectStore, reference: ObjectRef) -> None:
    try:
        await asyncio.shield(store.delete(reference))
    except Exception:
        logger.exception("raw voice object compensation failed object_key=%s", reference.object_key)
        raise


async def _observe_persona(
    request: Request,
    engine: PersonaEnginePort,
    *,
    account_id: str,
    source_event_id: str,
    speech_duration_ms: int | None,
    pause_ratio: float | None,
    quality_score: float | None,
) -> None:
    try:
        async with _account_write(request, account_id):
            allowed = await engine.learning_allowed(account_id=account_id)
            await engine.observe(
                PersonaEvidence(
                    account_id=account_id,
                    source_event_id=source_event_id,
                    learning_allowed=allowed,
                    speech_duration_ms=speech_duration_ms,
                    pause_ratio=pause_ratio,
                    quality_score=quality_score,
                )
            )
    except Exception:
        logger.exception("persona observation failed source_event_id=%s", source_event_id)


def _bounded_metric(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    metric = float(value)
    if not math.isfinite(metric) or not minimum <= metric <= maximum:
        return None
    return metric


def _persona_metrics(
    payload: Mapping[str, Any],
) -> tuple[int | None, float | None, float | None] | None:
    speech_ms = _bounded_metric(payload, "speech_ms", minimum=1, maximum=600_000)
    pause_ratio = _bounded_metric(payload, "pause_ratio", minimum=0, maximum=1)
    quality_score = _bounded_metric(payload, "quality_score", minimum=0, maximum=1)
    if any(
        key in payload and metric is None
        for key, metric in (
            ("speech_ms", speech_ms),
            ("pause_ratio", pause_ratio),
            ("quality_score", quality_score),
        )
    ):
        return None
    return (
        int(speech_ms) if speech_ms is not None else None,
        pause_ratio,
        quality_score,
    )


def _schedule_persona_observation(
    request: Request,
    background_tasks: BackgroundTasks,
    *,
    event: EvidenceEvent,
    duplicate: bool,
) -> None:
    if (
        duplicate
        or event.event_type != "speech.utterance_finalized"
        or event.speaker_class != "owner"
    ):
        return
    engine = cast(PersonaEnginePort, request.app.state.persona_engine)
    metrics = _persona_metrics(event.payload)
    if metrics is None:
        return
    speech_duration_ms, pause_ratio, quality_score = metrics
    background_tasks.add_task(
        _observe_persona,
        request,
        engine,
        account_id=event.account_id,
        source_event_id=event.event_id,
        speech_duration_ms=speech_duration_ms,
        pause_ratio=pause_ratio,
        quality_score=quality_score,
    )


def _require_internal_token(
    request: Request,
    capability: Literal["archive_write", "memory_read"],
    token: str | None,
) -> None:
    settings = cast(ControlSettings, request.app.state.settings)
    expected = settings.internal_token(capability)
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="valid internal capability token required")


def _require_archive_write_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    _require_internal_token(request, "archive_write", token)


def _require_memory_read_token(
    request: Request,
    token: Annotated[str | None, Header(alias="X-Memoria-Internal-Token")] = None,
) -> None:
    _require_internal_token(request, "memory_read", token)


@router.post("/events")
async def append_event(
    body: EvidenceEventCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    event = EvidenceEvent(**body.model_dump())
    try:
        async with _account_write(request, body.account_id):
            result = await _archive(request).record(event)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _wake_compiler(request)
    _schedule_persona_observation(
        request,
        background_tasks,
        event=event,
        duplicate=result.duplicate,
    )
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={
            "event_id": result.event_id,
            "outbox_id": result.outbox_id,
            "recorded_at": result.recorded_at.isoformat(),
            "duplicate": result.duplicate,
        },
    )


@router.post("/session-events")
async def append_session_event(
    body: SessionEvidenceEventCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    session = require_active_voice_session(request, body.session_id)
    values = body.model_dump()
    values["account_id"] = str(session["user_id"])
    event = EvidenceEvent(**values)
    try:
        async with _account_write(request, event.account_id):
            result = await _archive(request).record(event)
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _wake_compiler(request)
    _schedule_persona_observation(
        request,
        background_tasks,
        event=event,
        duplicate=result.duplicate,
    )
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={
            "event_id": result.event_id,
            "outbox_id": result.outbox_id,
            "recorded_at": result.recorded_at.isoformat(),
            "duplicate": result.duplicate,
        },
    )


@router.get("/raw-voice-consent")
async def raw_voice_consent(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    consent = await _archive(request).active_raw_voice_consent(account_id=user.user_id)
    return {"consent": _consent_payload(consent) if consent is not None else None}


@router.post("/raw-voice-consent", status_code=201)
async def grant_raw_voice_consent(
    body: RawVoiceConsentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _require_registered(request, user)
    async with _account_write(request, user.user_id):
        consent = await _archive(request).grant_raw_voice_consent(
            account_id=user.user_id,
            policy_version=body.policy_version,
            retention_policy=body.retention_policy,
            granted_at=datetime.now(UTC),
        )
    return _consent_payload(consent)


@router.delete("/raw-voice-consent")
async def revoke_raw_voice_consent(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _require_registered(request, user)
    archive = _archive(request)
    store = _archive_objects(request)
    async with _account_write(request, user.user_id):
        try:
            revocation = await archive.revoke_raw_voice_consent(
                account_id=user.user_id,
                revoked_at=datetime.now(UTC),
            )
        except RawVoiceConsentRequiredError as exc:
            raise HTTPException(status_code=404, detail="raw voice consent not found") from exc
        try:
            for reference in revocation.references:
                await store.delete(reference)
            await archive.purge_raw_voice_blobs(
                account_id=user.user_id,
                object_keys=tuple(
                    reference.object_key for reference in revocation.references
                ),
            )
        except Exception as exc:
            logger.exception("raw voice revocation cleanup failed")
            raise HTTPException(
                status_code=503,
                detail="raw voice deletion is incomplete; retry revocation",
            ) from exc
    return _consent_payload(revocation.consent)


@router.get("/session-raw-voice-consent")
async def session_raw_voice_consent(
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, session_id)
    consent = await _archive(request).active_raw_voice_consent(
        account_id=str(session["user_id"])
    )
    if consent is None:
        return {"allowed": False}
    return {
        "allowed": True,
        "consent_grant_id": consent.consent_grant_id,
        "policy_version": consent.policy_version,
        "retention_policy": consent.retention_policy,
    }


@router.post("/session-raw-audio")
async def append_session_raw_audio(
    body: SessionRawAudioCreate,
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    session = require_active_voice_session(request, body.session_id)
    if body.speaker_class != "owner":
        raise HTTPException(status_code=422, detail="raw voice archive is restricted to the owner")
    account_id = str(session["user_id"])
    archive = _archive(request)
    store = _archive_objects(request)
    active = await archive.active_raw_voice_consent(account_id=account_id)
    if active is None:
        raise HTTPException(status_code=410, detail="raw voice consent is no longer active")
    if (
        active.consent_grant_id != body.consent_grant_id
        or active.retention_policy != body.retention_policy
    ):
        raise HTTPException(status_code=403, detail="raw voice consent grant does not match")
    audio = _decode_owner_wav(body.audio_base64)
    values = body.model_dump(
        exclude={"audio_base64", "media_type", "retention_policy"}
    )
    values["account_id"] = account_id
    event = EvidenceEvent(**values)
    reference: ObjectRef | None = None
    try:
        async with _account_write(request, account_id):
            reference = await _put_archive_object(
                store,
                account_id=account_id,
                data=audio,
            )
            result = await archive.record_with_blob(
                event,
                reference,
                retention_policy=body.retention_policy,
            )
            if result.blob_duplicate is True:
                await _delete_object_safely(store, reference)
    except RawVoiceConsentRequiredError as exc:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise HTTPException(status_code=410, detail="raw voice consent is no longer active") from exc
    except IdempotencyConflictError as exc:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except asyncio.CancelledError:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise
    except Exception:
        if reference is not None:
            await _delete_object_safely(store, reference)
        raise
    _wake_compiler(request)
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={
            "event_id": result.event_id,
            "outbox_id": result.outbox_id,
            "recorded_at": result.recorded_at.isoformat(),
            "duplicate": result.duplicate,
            "blob_archived": True,
        },
    )


@router.post("/session-context")
async def session_memory_context(
    body: SessionMemoryContextCreate,
    request: Request,
    _: Annotated[None, Depends(_require_memory_read_token)],
) -> dict[str, Any]:
    session = require_active_voice_session(request, body.session_id)
    result = await _catalog(request).context(
        MemorySearchQuery(
            account_id=str(session["user_id"]),
            speaker_class=body.speaker_class,
            text=body.topic,
            include_candidates=False,
            limit=body.limit,
        )
    )
    return {
        "items": [
            {
                "kind": item.kind,
                "title": item.title,
                "snippet": item.snippet,
                "category": item.category,
                "status": item.status,
                "source_event_id": item.source_event_id,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in result.items
        ]
    }


@router.get("/timeline")
async def timeline(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    bundle = await _archive(request).context(
        ContextQuery(account_id=user.user_id, speaker_class="owner", limit=limit)
    )
    return {
        "items": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "occurred_at": event.occurred_at.isoformat(),
                "speaker_class": event.speaker_class,
                "source": event.source,
                "payload": dict(event.payload),
                "session_id": event.session_id,
                "turn_id": event.turn_id,
                "generation_id": event.generation_id,
            }
            for event in bundle.evidence
        ]
    }


def _search_item(item: Any) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "title": item.title,
        "snippet": item.snippet,
        "category": item.category,
        "status": item.status,
        "source_event_id": item.source_event_id,
        "occurred_at": item.occurred_at.isoformat(),
        "score": item.score,
    }


@router.get("/search")
async def search_memories(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    q: Annotated[str, Query(max_length=500)] = "",
    kind: Annotated[list[str] | None, Query()] = None,
    category: Annotated[list[MemoryCategory] | None, Query()] = None,
    include_candidates: bool = True,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    result = await _catalog(request).search(
        MemorySearchQuery(
            account_id=user.user_id,
            speaker_class="owner",
            text=q,
            kinds=tuple(kind or ()),
            categories=tuple(category or ()),
            include_candidates=include_candidates,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            limit=limit,
        )
    )
    return {"items": [_search_item(item) for item in result.items]}


@router.get("/life-timeline")
async def life_timeline(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    items = await _catalog(request).timeline(account_id=user.user_id, limit=limit)
    return {
        "items": [
            {
                "timeline_id": item.timeline_id,
                "title": item.title,
                "category": item.category,
                "status": item.status,
                "event_start": item.event_start.isoformat(),
                "event_end": item.event_end.isoformat() if item.event_end else None,
                "time_precision": item.time_precision,
                "source_event_id": item.source_event_id,
                "episode_id": item.episode_id,
            }
            for item in items
        ]
    }


@router.get("/people")
async def people(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    items = await _catalog(request).people(account_id=user.user_id, limit=limit)
    return {
        "items": [
            {
                "person_id": item.person_id,
                "display_name": item.display_name,
                "relationship_to_owner": item.relationship_to_owner,
                "aliases": list(item.aliases),
                "status": item.status,
                "source_event_id": item.source_event_id,
            }
            for item in items
        ]
    }


@router.get("/review-queue")
async def review_queue(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    items = await _catalog(request).review_queue(account_id=user.user_id)
    return {
        "items": [
            {
                "item_id": item.item_id,
                "kind": item.kind,
                "category": item.category,
                "value": item.value,
                "status": item.status,
                "reason": item.reason,
                "source_event_id": item.source_event_id,
            }
            for item in items
        ]
    }


class MemoryReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["confirm", "dispute", "retract", "correct"]
    corrected_value: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def enforce_correction_value(self) -> MemoryReviewBody:
        if self.action == "correct" and self.corrected_value is None:
            raise ValueError("corrected_value is required for correction")
        if self.action != "correct" and self.corrected_value is not None:
            raise ValueError("corrected_value is only valid for correction")
        return self


@router.post("/memories/{claim_id}/review")
async def review_memory(
    claim_id: str,
    body: MemoryReviewBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    try:
        reviewed = await _catalog(request).review(
            MemoryClaimReview(
                account_id=user.user_id,
                claim_id=claim_id,
                action=body.action,
                corrected_value=body.corrected_value,
            )
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="memory claim not found") from exc
    return {
        "claim_id": reviewed.claim_id,
        "status": reviewed.status,
        "value": reviewed.value,
        "review_event_id": reviewed.review_event_id,
    }


class ArchiveExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=8, max_length=128)


class ArchiveDeletionBody(ArchiveExportBody):
    confirmation: Literal["永久删除我的全部数据"]


def _verify_sensitive_action(
    request: Request,
    user: AuthenticatedUser,
    password: str,
) -> None:
    account = _store(request).get_account(user_id=user.user_id)
    if account is None:
        raise HTTPException(status_code=409, detail="请先注册账户再执行该操作")
    if not verify_password(password, str(account["password_hash"])):
        raise HTTPException(status_code=403, detail="密码验证失败")


def _account_audit_hash(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


@router.post("/exports")
async def export_archive(
    body: ArchiveExportBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    _verify_sensitive_action(request, user, body.password)
    exported = await _governance(request).export_account(user.user_id)
    logger.info(
        "archive export completed account_hash=%s manifest=%s",
        _account_audit_hash(user.user_id),
        exported["manifest_sha256"],
    )
    return JSONResponse(
        content=exported,
        headers={
            "Content-Disposition": ('attachment; filename="memoria-account-export.json"'),
            "Cache-Control": "no-store",
        },
    )


@router.post("/deletion-requests")
async def delete_archive(
    body: ArchiveDeletionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _verify_sensitive_action(request, user, body.password)
    try:
        result = await _governance(request).delete_account(user.user_id)
    except AccountDeletionIncompleteError as exc:
        logger.warning(
            "account deletion incomplete account_hash=%s reason=%s",
            _account_audit_hash(user.user_id),
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="外部资产删除未完成，请稍后重试") from exc
    logger.info(
        "account deletion completed account_hash=%s request_id=%s",
        _account_audit_hash(user.user_id),
        result["request_id"],
    )
    return result
