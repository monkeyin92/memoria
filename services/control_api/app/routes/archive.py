"""Authenticated archive ledger and timeline APIs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    ConflictState,
    DomainCategory,
    MemoryCatalogPort,
    MemoryCategory,
    MemoryClaimReview,
    MemoryKind,
    MemorySearchQuery,
    MemorySensitivity,
)
from services.archive.memory_write_policy import EXPLICIT_MEMORY_INTENT
from services.archive.object_store import ObjectStore
from services.common.explicit_memory import explicit_remember_content
from services.control_api.app.account_gate import (
    AccountDeletingError,
    AccountOperationGate,
    require_capability_for_account_id,
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.archive_event_conflicts import (
    canonicalize_response_provenance,
)
from services.control_api.app.archive_event_conflicts import (
    event_id_conflict as conflict_409,
)
from services.control_api.app.bound_subject import (
    DEVICE_BOUND_SUBJECT_UNVERIFIED,
    claims_device_bound_subject,
    runtime_profile_trusts_bound_subject,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.mode_policy import FrozenMode, ModePolicy, SpeakerClass
from services.control_api.app.persona_learning import (
    schedule_low_sensitivity_persona_observation,
    schedule_persona_observation,
)
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
    verify_password,
)
from services.control_api.app.session_evidence import (
    EvidenceEventCreate,
    SessionEvidenceEventCreate,
    append_legacy_shell_event,
    canonical_turn_eligibility,
    memory_capture_candidate,
    resolve_legacy_access,
)
from services.control_api.app.wechat_auth import WechatAuthError, code_to_session, openid_hash
from services.evolution.account_fence import AccountWriteBlockedError
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionIncompleteError,
)
from services.guardian.domain import GuardianStorePort
from services.guardian.retention import (
    apply_memory_retention_ceiling,
    conversation_turn_eligible,
    memory_retention_allowed,
    payload_text_allowed,
)
from services.identity.domain import IdentityNotFoundError
from services.identity.service import IdentityService

router = APIRouter(prefix="/v1/archive", tags=["archive"])
logger = logging.getLogger(__name__)
SESSION_BOUND_EVENT_TYPES = frozenset(
    {
        "speech.utterance_finalized",
        "speaker.classified",
        "assistant.playout_progressed",
        "assistant.playout_stopped",
    }
)
SERVER_OWNED_EVENT_TYPES = frozenset(
    {
        "owner.action_recorded",
        "learning.task_created",
        "learning.task_transitioned",
        "memory.claim_reviewed",
    }
)
SERVER_INTERACTION_PAYLOAD_KEYS = frozenset(
    {
        "interaction",
        "interaction_mode",
        "mode_policy_version",
        "simulated_output",
        "history_eligible",
        "owner_projection_eligible",
        "prompt_kind",
        "learning_task_id",
        "learning_task_kind",
        "memory_write_intent",
        "evolution_observation",
        "response_provenance",
        "tool_epoch",
        "subject_category",
        "memory_retention",
        "memory_capture_candidate_v1",
    }
)

AGENT_MEMORY_WRITE_FENCE_FIELDS = (
    "session_epoch",
    "device_id",
    "subject_revision",
    "active_subject_id",
    "runtime_profile_id",
    "actor_id",
    "binding_id",
    "binding_version",
    "memory_scope",
    "policy_receipt_id",
    "raw_audio_receipt_id",
    "training_receipt_id",
    "no_model_training",
    "event_sequence",
)


class RawVoiceConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    policy_version: str = Field(
        default="raw-voice-archive-v1",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    retention_policy: Literal["account_lifetime"] = "account_lifetime"


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
    except (AccountDeletingError, AccountWriteBlockedError) as exc:
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


def _require_internal_token(
    request: Request,
    capability: Literal["archive_write"],
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


@router.post("/events")
async def append_event(
    body: EvidenceEventCreate,
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    if body.event_type in SESSION_BOUND_EVENT_TYPES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_bound_event_required",
                "event_type": body.event_type,
            },
        )
    if body.event_type in SERVER_OWNED_EVENT_TYPES:
        raise HTTPException(
            status_code=409,
            detail={"code": "server_owned_event_required", "event_type": body.event_type},
        )
    event = EvidenceEvent(**body.model_dump())
    try:
        async with _account_write(request, body.account_id):
            result = await _archive(request).record(event)
    except IdempotencyConflictError as exc:
        raise conflict_409(exc) from exc
    _wake_compiler(request)
    return JSONResponse(
        status_code=200 if result.duplicate else 201,
        content={
            "event_id": result.event_id,
            "outbox_id": result.outbox_id,
            "recorded_at": result.recorded_at.isoformat(),
            "duplicate": result.duplicate,
        },
    )


async def _active_memory_consent(request: Request, *, subject_id: str) -> bool:
    """Whether the subject has an active guardian memory-retention consent."""
    return (
        await cast(GuardianStorePort, request.app.state.guardian_store).active_consent(
            minor_user_id=subject_id,
            consent_kind="memory_retention",
        )
        is not None
    )


async def _speaker_retention_inputs(
    request: Request,
    *,
    account_id: str,
    subject_id: str | None,
) -> tuple[str | None, bool]:
    """Retention inputs for the speaker subject (P2-03).

    Missing identity is never the account owner. Authority failures propagate
    as an unavailable response, never as a successful adult retention decision.
    """
    if subject_id is None:
        return None, False
    if subject_id == account_id:
        profile = _store(request).get_subject_profile(user_id=account_id)
        category = (profile or {}).get("subject_category")
        consent = (
            await _active_memory_consent(request, subject_id=account_id)
            if category == "minor"
            else False
        )
        return cast(str | None, category), consent
    identity = cast(IdentityService, request.app.state.identity_service)
    try:
        person = await identity.get_person(subject_id, actor_person_id=account_id)
    except IdentityNotFoundError:
        return None, False
    category = person.subject_category
    consent = (
        await _active_memory_consent(request, subject_id=subject_id)
        if category == "minor"
        else False
    )
    return category, consent


async def _require_subject_memory_read(request: Request, *, account_id: str) -> None:
    require_capability_for_account_id(
        account_id, "conversation_review", store=_store(request)
    )
    try:
        category, consent = await _speaker_retention_inputs(
            request, account_id=account_id, subject_id=account_id
        )
    except Exception as exc:
        logger.exception("archive subject retention authority unavailable")
        raise HTTPException(
            status_code=503, detail={"code": "subject_retention_authority_unavailable"}
        ) from exc
    if not memory_retention_allowed(subject_category=category, active_consent=consent):
        raise HTTPException(status_code=403, detail={"code": "memory_retention_consent_required"})


@router.post("/session-events")
async def append_session_event(
    body: SessionEvidenceEventCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    session = require_active_voice_session(request, body.session_id)
    legacy_access = (
        await resolve_legacy_access(request, session=session, now=datetime.now(UTC))
        if str(session["interaction_mode"]) == "legacy"
        else None
    )
    archive = _archive(request)
    account_id = str(session["user_id"])
    values = body.model_dump()
    tool_epoch = values.pop("tool_epoch")
    memory_write_fence = {
        field: values.pop(field) for field in AGENT_MEMORY_WRITE_FENCE_FIELDS
    }
    values["account_id"] = account_id
    # P2-03: the confirmed speaker subject is written onto the evidence row
    # itself. It used to be consumed only as a fence input, which left every
    # read exit unable to fence by subject or scope retention to the speaker.
    # An absent/blank claim stores None (unknown speaker), never the account.
    claimed_subject = memory_write_fence.get("active_subject_id")
    values["subject_id"] = (
        claimed_subject.strip()
        if isinstance(claimed_subject, str) and claimed_subject.strip()
        else None
    )
    payload = dict(values["payload"])
    reason_code = payload.get("speaker_reason_code") or payload.get("reason_code")
    if not isinstance(reason_code, str):
        reason_code = None
    assistant_event = body.speaker_class == "assistant"
    parent_eligibility: tuple[bool, bool] | None = None
    parent: EvidenceEvent | None = None
    if assistant_event and legacy_access is not None:
        assert body.turn_id is not None and body.generation_id is not None
        parent = EvidenceEvent(
            event_id=f"legacy-parent:{body.event_id}",
            account_id=legacy_access.resource_owner_account_id,
            event_type="speech.utterance_finalized",
            occurred_at=body.occurred_at,
            speaker_class="owner",
            source="legacy.shell",
            payload={},
            session_id=body.session_id,
            turn_id=body.turn_id,
            generation_id=body.generation_id,
        )
        parent_eligibility = (False, False)
    elif assistant_event:
        assert body.turn_id is not None and body.generation_id is not None
        try:
            parent = await archive.turn_event(
                account_id=account_id,
                session_id=body.session_id,
                turn_id=body.turn_id,
                generation_id=body.generation_id,
                event_type="speech.utterance_finalized",
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "ambiguous_parent_turn"},
            ) from exc
        if parent is None:
            raise HTTPException(
                status_code=425,
                detail={"code": "parent_turn_not_recorded"},
            )
        parent_eligibility = canonical_turn_eligibility(parent)
        if parent_eligibility is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "parent_turn_not_canonical"},
            )
    elif legacy_access is None and (
        body.event_type == "speech.utterance_finalized"
        and body.turn_id is not None
        and body.generation_id is not None
    ):
        try:
            existing_turn = await archive.turn_event(
                account_id=account_id,
                session_id=body.session_id,
                turn_id=body.turn_id,
                generation_id=body.generation_id,
                event_type=body.event_type,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "ambiguous_parent_turn"},
            ) from exc
        if existing_turn is not None and existing_turn.event_id != body.event_id:
            raise HTTPException(
                status_code=409,
                detail={"code": "turn_event_conflict"},
            )
    current_profile: dict[str, Any] | None = None
    if assistant_event and parent is not None:
        if values["subject_id"] is not None and values["subject_id"] != parent.subject_id:
            raise HTTPException(status_code=409, detail={"code": "parent_subject_mismatch"})
        values["subject_id"] = parent.subject_id
    elif values["subject_id"] is not None and getattr(
        request.app.state, "session_runtime_service", None
    ) is not None:
        # Claims identify a subject only after the existing runtime authority
        # accepts the same profile/epoch. Never let a stale turn name the adult.
        from services.control_api.app.persistent_runtime_profile import (
            current_persistent_runtime_profile,
        )

        profile = await current_persistent_runtime_profile(
            request, session, session_id=body.session_id
        )
        current_profile = profile
        if any(
            memory_write_fence[field] != profile.get(field)
            for field in (
                "active_subject_id", "runtime_profile_id", "session_epoch",
                "actor_id", "device_id", "binding_id", "binding_version", "subject_revision",
            )
        ):
            raise HTTPException(status_code=409, detail={"code": "archive_subject_fence_stale"})
    subject_deletion = getattr(request.app.state, "subject_deletion", None)
    if (
        values["subject_id"] is not None
        and subject_deletion is not None
        and subject_deletion.is_subject_deleting(
            account_id=account_id, subject_id=values["subject_id"]
        )
    ):
        # Their data is being erased: nothing new may land behind the sweep.
        raise HTTPException(status_code=409, detail={"code": "subject_deletion_in_progress"})
    if claims_device_bound_subject(
        body.speaker_class, reason_code
    ) and not runtime_profile_trusts_bound_subject(
        current_profile, subject_id=values["subject_id"]
    ):
        # Owner data authority from the binding needs the same fenced profile
        # that names the subject; without it the claim is a bare assertion.
        raise HTTPException(status_code=409, detail={"code": DEVICE_BOUND_SUBJECT_UNVERIFIED})
    try:
        subject_category, memory_consent_active = await _speaker_retention_inputs(
            request, account_id=account_id, subject_id=values["subject_id"]
        )
    except Exception as exc:
        logger.exception("archive subject retention authority unavailable")
        raise HTTPException(
            status_code=503, detail={"code": "subject_retention_authority_unavailable"}
        ) from exc
    retention_allowed = memory_retention_allowed(
        subject_category=subject_category, active_consent=memory_consent_active
    )
    raw_response_provenance = payload.get("response_provenance")
    canonical_response_provenance: dict[str, Any] | None = None
    if raw_response_provenance is not None:
        if not assistant_event or parent is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "response_provenance_invalid"},
            )
        canonical_response_provenance = await canonicalize_response_provenance(
            request,
            raw=raw_response_provenance,
            session=session,
            parent=parent,
            tool_epoch=cast(int, tool_epoch),
            legacy_access=legacy_access,
        )
    elif assistant_event and legacy_access is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "response_provenance_invalid"},
        )
    policy_speaker = cast(
        SpeakerClass,
        "guest" if assistant_event else body.speaker_class,
    )
    trusted_interaction = ModePolicy.trusted_context(
        FrozenMode.from_session(session),
        speaker_class=policy_speaker,
        reason_code=reason_code,
        history_eligible=(parent_eligibility[0] if parent_eligibility is not None else None),
        owner_projection_eligible=(
            parent_eligibility[1] if parent_eligibility is not None else None
        ),
    )
    trusted_interaction = apply_memory_retention_ceiling(
        trusted_interaction,
        allowed=retention_allowed,
    )
    # Scheme A: minor and unknown never persist verbatim turn text. Summaries
    # are written separately as dialogue_summary rows; this evidence row stays
    # metadata-only even when memory_retention consent is active.
    if subject_category in {"minor", "unknown"} or not retention_allowed:
        for key in ("text", "transcript", "user_text", "assistant_text"):
            payload.pop(key, None)
    prompt_kind = payload.get("prompt_kind")
    if prompt_kind not in {"spontaneous", "open", "structured", "leading"}:
        prompt_kind = "spontaneous"
    for key in SERVER_INTERACTION_PAYLOAD_KEYS:
        payload.pop(key, None)
    payload.update(
        {
            "interaction_mode": trusted_interaction["interaction_mode"],
            "mode_policy_version": trusted_interaction["mode_policy_version"],
            "simulated_output": trusted_interaction["simulated_output"],
            "history_eligible": trusted_interaction["history_eligible"],
            "owner_projection_eligible": trusted_interaction["owner_projection_eligible"],
            "interaction": trusted_interaction,
            "prompt_kind": prompt_kind,
            "memory_retention": trusted_interaction.get("memory_retention", "retained"),
            "subject_category": subject_category,
        }
    )
    if tool_epoch is not None:
        payload["tool_epoch"] = tool_epoch
    if canonical_response_provenance is not None:
        payload["response_provenance"] = canonical_response_provenance
    capture_candidate = memory_capture_candidate(
        body=body,
        claims=memory_write_fence,
        trusted_interaction=trusted_interaction,
        retention_allowed=retention_allowed,
    )
    if capture_candidate is not None:
        payload["memory_capture_candidate_v1"] = capture_candidate
    if (
        body.event_type == "speech.utterance_finalized"
        and body.speaker_class == "owner"
        and body.turn_id is not None
        and body.generation_id is not None
        and tool_epoch is not None
        and trusted_interaction["interaction_mode"] == "companion"
        and trusted_interaction["simulated_output"] is False
        and trusted_interaction["owner_projection_eligible"] is True
        and explicit_remember_content(payload.get("text")) is not None
    ):
        payload["memory_write_intent"] = dict(EXPLICIT_MEMORY_INTENT)
    if legacy_access is not None:
        return await append_legacy_shell_event(
            request,
            body=body,
            access=legacy_access,
            tool_epoch=tool_epoch,
            response_provenance=canonical_response_provenance,
        )
    learning_task_id = session.get("learning_task_id")

    async def record_event() -> tuple[EvidenceEvent, Any]:
        values["payload"] = payload
        event = EvidenceEvent(**values)
        try:
            async with _account_write(request, event.account_id):
                result = await archive.record(event)
        except IdempotencyConflictError as exc:
            raise conflict_409(exc) from exc
        return event, result

    if isinstance(learning_task_id, str) and learning_task_id:
        lock = cast(asyncio.Lock, request.app.state.growth_task_lock)
        async with lock:
            task = await request.app.state.growth_reader.task(
                account_id=account_id,
                task_id=learning_task_id,
            )
            if task is not None and task.kind == "natural_chat" and task.status == "active":
                payload.update(
                    {
                        "learning_task_id": learning_task_id,
                        "learning_task_kind": "natural_chat",
                    }
                )
            event, result = await record_event()
    else:
        event, result = await record_event()
    from services.control_api.app.subject_session_recall import (
        record_archive_owner_utterance_if_applicable,
    )

    await record_archive_owner_utterance_if_applicable(
        request,
        event_type=body.event_type,
        speaker_class=body.speaker_class,
        retention_allowed=retention_allowed,
        subject_id=event.subject_id,
        session_id=body.session_id,
        turn_id=body.turn_id,
        payload=payload,
        memory_write_fence=memory_write_fence,
    )
    # ``actual_heard`` is evidence that playout completed, not evidence that a
    # user task succeeded.  Offline trajectory replay loads this canonical pair
    # later and appends a LearningSignal only after independent evaluation.
    _wake_compiler(request)
    capabilities = trusted_interaction["capabilities"]
    schedule = (
        schedule_persona_observation
        if capabilities["learning"]
        else schedule_low_sensitivity_persona_observation
        if capabilities["persona_low_sensitivity"]
        else None
    )
    if schedule is not None:
        schedule(
            request,
            background_tasks,
            event=event,
            duplicate=result.duplicate,
            account_write=_account_write,
            # "Long-term memory" ticked at binding lets another subject's
            # persona learn (signed as memory_recall_private for exactly this
            # subject); a minor's persona learns style only (P0-04 D2).
            subject_learning_allowed=runtime_profile_trusts_bound_subject(
                current_profile, subject_id=event.subject_id
            ),
            style_only=subject_category == "minor",
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
    require_capability_for_subject(user, "raw_voice_archive", store=_store(request))
    consent = await _archive(request).active_raw_voice_consent(account_id=user.user_id)
    return {"consent": _consent_payload(consent) if consent is not None else None}


@router.post("/raw-voice-consent", status_code=201)
async def grant_raw_voice_consent(
    body: RawVoiceConsentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "raw_voice_archive", store=_store(request))
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
                object_keys=tuple(reference.object_key for reference in revocation.references),
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
    account_id = str(session["user_id"])
    profile = _store(request).get_subject_profile(user_id=account_id)
    subject_category = profile.get("subject_category") if profile else None
    if subject_category == "minor":
        # Approved: abolish child corpus recording. Historical consent rows may
        # remain until the offline cleanup script runs; the write path always
        # refuses.
        return {
            "allowed": False,
            "subject_category": "minor",
            "reason": "child_corpus_recording_abolished",
        }
    if subject_category != "adult":
        return {"allowed": False, "subject_category": subject_category}
    consent = await _archive(request).active_raw_voice_consent(account_id=account_id)
    if consent is None:
        return {"allowed": False}
    return {
        "allowed": True,
        "consent_grant_id": consent.consent_grant_id,
        "policy_version": consent.policy_version,
        "retention_policy": consent.retention_policy,
    }


@router.get("/timeline")
async def timeline(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    await _require_subject_memory_read(request, account_id=user.user_id)
    bundle = await _archive(request).context(
        ContextQuery(
            account_id=user.user_id, subject_id=user.user_id, speaker_class="owner", limit=limit
        )
    )
    return {
        "items": [
            {
                "event_id": event.event_id,
                "subject_id": event.subject_id,
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
            if payload_text_allowed(event.payload)
            and (event.session_id is None or event.payload.get("history_eligible") is True)
        ]
    }


def _actual_heard_item(event: EvidenceEvent) -> dict[str, Any] | None:
    payload = event.payload
    text = payload.get("text")
    if not (
        event.event_type == "assistant.playout_stopped"
        and event.speaker_class == "assistant"
        and payload.get("actual_heard") is True
        and payload.get("history_eligible") is True
        and payload.get("owner_projection_eligible") is True
        and payload_text_allowed(payload)
        and isinstance(text, str)
        and text.strip()
        and event.session_id is not None
        and event.turn_id is not None
        and event.generation_id is not None
    ):
        return None
    return {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at.isoformat(),
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "generation_id": event.generation_id,
        "text": text.strip(),
        # Exact DAC evidence must opt in explicitly. Missing or malformed
        # telemetry remains an approximate playback record.
        "approximate": payload.get("approximate") is not False,
    }


@router.get("/conversation-review")
async def conversation_review(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    actual_heard_limit: int = Query(default=20, ge=1, le=50),
    memory_limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    require_capability_for_subject(
        user,
        "conversation_review",
        store=_store(request),
    )
    await _require_subject_memory_read(request, account_id=user.user_id)
    bundle = await _archive(request).context(
        ContextQuery(
            account_id=user.user_id,
            subject_id=user.user_id,
            speaker_class="owner",
            limit=min(100, actual_heard_limit * 4),
        )
    )
    actual_heard = [
        item
        for event in bundle.evidence
        if (item := _actual_heard_item(event)) is not None
    ][:actual_heard_limit]
    catalog = _catalog(request)
    candidates = await catalog.review_queue(account_id=user.user_id, subject_id=user.user_id)
    confirmed = await catalog.search(
        MemorySearchQuery(
            account_id=user.user_id,
            subject_id=user.user_id,
            speaker_class="owner",
            kinds=("claim",),
            include_candidates=False,
            limit=memory_limit,
        )
    )
    return {
        "actual_heard": actual_heard,
        "memory_candidates": [
            {
                "claim_id": item.item_id,
                "value": item.value,
                "status": item.status,
                "reason": item.reason,
                "domain_category": item.domain_category,
                "memory_kind": item.memory_kind,
                "conflict_state": item.conflict_state,
            }
            for item in candidates[:memory_limit]
        ],
        "confirmed_memories": [
            {
                "memory_id": item.item_id,
                "kind": item.kind,
                "title": item.title,
                "snippet": item.snippet,
                "status": item.status,
                "domain_category": item.domain_category,
                "memory_kind": item.memory_kind,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in confirmed.items
        ],
    }


def _owner_turn_item(event: EvidenceEvent) -> dict[str, Any] | None:
    payload = event.payload
    text = payload.get("text")
    if not (
        event.event_type == "speech.utterance_finalized"
        and event.speaker_class == "owner"
        and payload.get("history_eligible") is True
        and payload.get("owner_projection_eligible") is True
        and payload_text_allowed(payload)
        and isinstance(text, str)
        and text.strip()
        and event.session_id is not None
        and event.turn_id is not None
        and event.generation_id is not None
    ):
        return None
    return {
        "event_id": event.event_id,
        "occurred_at": event.occurred_at.isoformat(),
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "generation_id": event.generation_id,
        "speaker_class": "owner",
        "text": text.strip(),
    }


@router.get("/conversation-history")
async def conversation_history(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    turn_limit: int = Query(default=20, ge=1, le=50),
) -> dict[str, Any]:
    """Read one session's paired turns: owner text plus actual-heard replies.

    Uses the authenticated subject's current retention permission and filters
    both subject and session before the storage limit. Owner turns must be
    history-eligible; assistant turns must carry actual-heard
    evidence. Anything else is omitted, never fabricated: a session with no
    eligible turns returns empty lists, not a generated summary.
    """
    require_capability_for_subject(
        user,
        "conversation_review",
        store=_store(request),
    )
    await _require_subject_memory_read(request, account_id=user.user_id)
    bundle = await _archive(request).context(
        ContextQuery(
            account_id=user.user_id,
            subject_id=user.user_id,
            speaker_class="owner",
            session_id=session_id,
            limit=100,
        )
    )
    scoped = [
        event for event in bundle.evidence if event.session_id == session_id
    ]
    owner_turns = [
        item
        for event in scoped
        if (item := _owner_turn_item(event)) is not None
    ]
    assistant_turns = [
        item
        for event in scoped
        if (item := _actual_heard_item(event)) is not None
    ]
    by_turn: dict[tuple[int, int], dict[str, Any]] = {}
    for item in owner_turns:
        by_turn.setdefault(
            (int(item["turn_id"]), int(item["generation_id"])),
            {"turn_id": item["turn_id"], "generation_id": item["generation_id"]},
        )["owner_text"] = item["text"]
    for item in assistant_turns:
        by_turn.setdefault(
            (int(item["turn_id"]), int(item["generation_id"])),
            {"turn_id": item["turn_id"], "generation_id": item["generation_id"]},
        )["assistant_text"] = item["text"]
    turns = [
        {
            "turn_id": key[0],
            "generation_id": key[1],
            "owner_text": entry.get("owner_text"),
            "assistant_text": entry.get("assistant_text"),
            "owner_event_id": next(
                (
                    item["event_id"]
                    for item in owner_turns
                    if int(item["turn_id"]) == key[0]
                    and int(item["generation_id"]) == key[1]
                ),
                None,
            ),
            "assistant_event_id": next(
                (
                    item["event_id"]
                    for item in assistant_turns
                    if int(item["turn_id"]) == key[0]
                    and int(item["generation_id"]) == key[1]
                ),
                None,
            ),
            "assistant_approximate": next(
                (
                    item["approximate"]
                    for item in assistant_turns
                    if int(item["turn_id"]) == key[0]
                    and int(item["generation_id"]) == key[1]
                ),
                None,
            ),
        }
        for key, entry in sorted(by_turn.items())
    ][:turn_limit]
    return {"session_id": session_id, "turns": turns}


@router.get("/conversation-sessions")
async def conversation_sessions(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=10, ge=1, le=20),
) -> dict[str, Any]:
    """List recent conversation sessions visible to the authenticated subject.

    This is deliberately a narrow read model for the family/demo client.  It
    does not accept an account or subject selector and applies the same
    conversation-review and retention gates as the paired history endpoint.
    Eligibility is checked before session aggregation so guest, ephemeral, and
    non-projectable events cannot make a session appear in the list.
    """
    require_capability_for_subject(
        user,
        "conversation_review",
        store=_store(request),
    )
    await _require_subject_memory_read(request, account_id=user.user_id)

    # Read a bounded newest-first event window. The subject fence is pushed
    # into the archive query, while eligibility is still checked before a
    # session is exposed. Newest-first matters here: a busy old session must
    # not consume the window and hide newer sessions.
    sessions: dict[str, dict[str, Any]] = {}
    occurred_before = datetime.now(UTC)
    occurred_after = datetime(1970, 1, 1, tzinfo=UTC)
    # Continue in bounded newest-first pages until enough distinct sessions
    # are found. This prevents one very busy session from hiding older recent
    # sessions behind a single 10,000-event window.
    while len(sessions) < limit and occurred_before >= occurred_after:
        events = await _archive(request).evidence_window(
            account_id=user.user_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            event_types=("speech.utterance_finalized", "assistant.playout_stopped"),
            limit=10_000,
            subject_id=user.user_id,
            newest_first=True,
        )
        if not events:
            break
        for event in events:
            if event.session_id is None or not conversation_turn_eligible(
                event_type=event.event_type,
                speaker_class=event.speaker_class,
                payload=event.payload,
            ):
                continue
            session = sessions.setdefault(
                event.session_id,
                {
                    "session_id": event.session_id,
                    "occurred_at": event.occurred_at,
                    "turns": set(),
                    "preview": "",
                },
            )
            if event.occurred_at > session["occurred_at"]:
                session["occurred_at"] = event.occurred_at
            if event.turn_id is not None and event.generation_id is not None:
                session["turns"].add((event.turn_id, event.generation_id))
            # Events arrive newest-first, so the first eligible utterance of a
            # session becomes its list preview. Only already-eligible text is
            # reused; nothing is generated or summarised server-side. Since
            # #282 no real turn carries text, so sessions routinely have none.
            if not session["preview"]:
                text = event.payload.get("text")
                if isinstance(text, str) and text.strip():
                    session["preview"] = text.strip()[:80]
        oldest = min(event.occurred_at for event in events)
        next_before = oldest - timedelta(microseconds=1)
        if next_before >= occurred_before:
            break
        occurred_before = next_before

    items = sorted(
        (
            {
                "session_id": item["session_id"],
                "occurred_at": item["occurred_at"].isoformat(),
                "turn_count": len(item["turns"]),
                "preview": item["preview"],
            }
            for item in sessions.values()
        ),
        key=lambda item: (item["occurred_at"], item["session_id"]),
        reverse=True,
    )[:limit]
    return {"items": items}


def _search_item(item: Any) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "kind": item.kind,
        "title": item.title,
        "snippet": item.snippet,
        "category": item.category,
        "domain_category": item.domain_category,
        "memory_kind": item.memory_kind,
        "entity_ids": list(item.entity_ids),
        "status": item.status,
        "source_event_id": item.source_event_id,
        "source_event_ids": list(item.source_event_ids),
        "occurred_at": item.occurred_at.isoformat(),
        "valid_from": item.valid_from.isoformat() if item.valid_from else None,
        "valid_to": item.valid_to.isoformat() if item.valid_to else None,
        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
        "stability": item.stability,
        "salience": item.salience,
        "sensitivity": item.sensitivity,
        "conflict_state": item.conflict_state,
        "score": item.score,
    }


@router.get("/search")
async def search_memories(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    q: Annotated[str, Query(max_length=500)] = "",
    kind: Annotated[list[str] | None, Query()] = None,
    memory_kind: Annotated[list[MemoryKind] | None, Query()] = None,
    domain_category: Annotated[list[DomainCategory] | None, Query()] = None,
    category: Annotated[list[MemoryCategory] | None, Query()] = None,
    entity_id: Annotated[list[str] | None, Query()] = None,
    valid_at: datetime | None = None,
    sensitivity: Annotated[list[MemorySensitivity] | None, Query()] = None,
    conflict_state: Annotated[list[ConflictState] | None, Query()] = None,
    include_candidates: bool = False,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    await _require_subject_memory_read(request, account_id=user.user_id)
    if category and domain_category:
        raise HTTPException(
            status_code=422,
            detail="use domain_category or legacy category, not both",
        )
    try:
        result = await _catalog(request).search(
            MemorySearchQuery(
                account_id=user.user_id,
                subject_id=user.user_id,
                speaker_class="owner",
                text=q,
                kinds=tuple(kind or ()),
                memory_kinds=tuple(memory_kind or ()),
                domain_categories=tuple(domain_category or category or ()),
                entity_ids=tuple(entity_id or ()),
                valid_at=valid_at,
                sensitivities=tuple(sensitivity or ()),
                conflict_states=tuple(conflict_state or ()),
                include_candidates=include_candidates,
                occurred_after=occurred_after,
                occurred_before=occurred_before,
                limit=limit,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"items": [_search_item(item) for item in result.items]}


@router.get("/life-timeline")
async def life_timeline(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    await _require_subject_memory_read(request, account_id=user.user_id)
    items = await _catalog(request).timeline(
        account_id=user.user_id, subject_id=user.user_id, limit=limit
    )
    return {
        "items": [
            {
                "timeline_id": item.timeline_id,
                "title": item.title,
                "category": item.category,
                "domain_category": item.domain_category,
                "status": item.status,
                "event_start": item.event_start.isoformat(),
                "event_end": item.event_end.isoformat() if item.event_end else None,
                "time_precision": item.time_precision,
                "source_event_id": item.source_event_id,
                "source_event_ids": list(item.source_event_ids),
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
    await _require_subject_memory_read(request, account_id=user.user_id)
    items = await _catalog(request).people(
        account_id=user.user_id, subject_id=user.user_id, limit=limit
    )
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
    await _require_subject_memory_read(request, account_id=user.user_id)
    items = await _catalog(request).review_queue(
        account_id=user.user_id, subject_id=user.user_id
    )
    return {
        "items": [
            {
                "item_id": item.item_id,
                "kind": item.kind,
                "category": item.category,
                "domain_category": item.domain_category,
                "memory_kind": item.memory_kind,
                "value": item.value,
                "status": item.status,
                "reason": item.reason,
                "source_event_id": item.source_event_id,
                "conflict_state": item.conflict_state,
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
    require_capability_for_subject(
        user,
        "conversation_review",
        store=_store(request),
    )
    await _require_subject_memory_read(request, account_id=user.user_id)
    try:
        reviewed = await _catalog(request).review(
            MemoryClaimReview(
                account_id=user.user_id,
                subject_id=user.user_id,
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
        "trace": {
            "claim_id": reviewed.claim_id,
            "action": body.action,
            "reviewed_at": datetime.now(UTC).isoformat(),
            "reviewer_account_id": user.user_id,
            "review_event_id": reviewed.review_event_id,
        },
    }


class ArchiveExportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=8, max_length=128)


class ArchiveDeletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, min_length=8, max_length=128)
    wechat_login_code: str | None = Field(default=None, min_length=1, max_length=256)
    confirmation: Literal["永久删除我的全部数据"]

    @model_validator(mode="after")
    def require_one_step_up_method(self) -> ArchiveDeletionBody:
        if (self.password is None) == (self.wechat_login_code is None):
            raise ValueError("provide exactly one account verification method")
        return self


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


async def _verify_deletion_action(
    request: Request,
    user: AuthenticatedUser,
    body: ArchiveDeletionBody,
) -> None:
    if body.password is not None:
        _verify_sensitive_action(request, user, body.password)
        return
    assert body.wechat_login_code is not None
    try:
        session = await code_to_session(
            cast(ControlSettings, request.app.state.settings),
            body.wechat_login_code,
        )
    except WechatAuthError as exc:
        status_code = 503 if exc.code == "wechat_credentials_missing" else 502
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code},
        ) from exc
    account_id = _store(request).external_identity_user(
        provider="wechat_openid",
        subject_hash=openid_hash(session.openid),
    )
    if account_id != user.user_id:
        raise HTTPException(status_code=403, detail="微信身份验证失败")


def _account_audit_hash(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:16]


def _deletion_status_response(deletion: Mapping[str, Any] | None) -> dict[str, Any]:
    """Expose a stable, account-bound deletion status without account ids."""
    if deletion is None:
        return {
            "request_id": None,
            "status": "not_requested",
            "phase": "not_requested",
            "retryable": False,
            "step": None,
            "started_at": None,
            "updated_at": None,
            "completed_at": None,
            "progress": {},
            "deleted_counts": {},
            "last_error": None,
        }

    status = str(deletion.get("status", "deleting"))
    if status not in {"deleting", "completed"}:
        status = "deleting"
    return {
        "request_id": str(deletion["request_id"]),
        "status": status,
        "phase": "completed" if status == "completed" else "in_progress",
        "retryable": status == "deleting",
        "step": str(deletion["step"]),
        "started_at": deletion.get("started_at"),
        "updated_at": deletion.get("updated_at"),
        "completed_at": deletion.get("completed_at"),
        "progress": dict(deletion.get("progress") or {}),
        "deleted_counts": dict(deletion.get("deleted_counts") or {}),
        "last_error": deletion.get("last_error"),
    }


@router.post("/exports")
async def export_archive(
    body: ArchiveExportBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    _verify_sensitive_action(request, user, body.password)
    await _require_subject_memory_read(request, account_id=user.user_id)
    try:
        exported = await _governance(request).export_account(
            user.user_id, subject_id=user.user_id
        )
    except AccountDeletingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@router.get("/deletion-requests")
async def get_archive_deletion(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    deletion = await asyncio.to_thread(
        _store(request).get_account_deletion,
        user_id=user.user_id,
    )
    return JSONResponse(
        content=_deletion_status_response(deletion),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/deletion-receipts")
async def list_archive_deletion_receipts(
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> JSONResponse:
    """Operator read of the deletion receipts (no session required).

    The owner-facing ``GET /v1/archive/deletion-requests`` cannot answer once a
    deletion started, because its first step terminates the account's sessions.
    This exit is the audited alternative: it needs the archive internal token
    and returns receipts without any account id.
    """

    try:
        receipts = await asyncio.to_thread(
            _store(request).list_account_deletion_receipts,
            status_filter=status_filter,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse(
        content={"items": list(receipts), "count": len(receipts)},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/deletion-receipts/{request_id}")
async def get_archive_deletion_receipt(
    request_id: str,
    request: Request,
    _: Annotated[None, Depends(_require_archive_write_token)],
) -> JSONResponse:
    """Operator read of one deletion receipt by request id."""

    receipt = await asyncio.to_thread(
        _store(request).get_account_deletion_receipt,
        request_id=request_id,
    )
    if receipt is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "deletion_receipt_not_found"},
        )
    return JSONResponse(
        content=receipt,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/deletion-requests")
async def delete_archive(
    body: ArchiveDeletionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    await _verify_deletion_action(request, user, body)
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
