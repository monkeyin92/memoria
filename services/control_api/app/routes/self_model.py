"""Account-scoped review APIs for cognitive, decision, and relationship material."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from services.archive.domain import EvidenceEvent, IdempotencyConflictError, LifeArchivePort
from services.control_api.app.account_gate import AccountDeletingError, AccountOperationGate
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
    verify_password,
)
from services.self_model.domain import (
    CognitiveClaim,
    CognitiveClaimType,
    DecisionCase,
    DecisionKind,
    InvalidSelfModelTransitionError,
    ItemStatus,
    RelationshipReferenceError,
    SelfModelIdempotencyConflictError,
    SelfModelItem,
    SelfModelItemKind,
    SelfModelNotFoundError,
    SelfModelRegistryPort,
    SelfModelSource,
    SelfModelVersionConflictError,
    SourceInput,
    SourceRelation,
    UntrustedSelfModelSourceError,
)
from services.self_model.policy import HIGH_SENSITIVITY_CLAIM_TYPES, activation_decision

router = APIRouter(prefix="/v1/self-model", tags=["self-model"])


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _registry(request: Request) -> SelfModelRegistryPort:
    return cast(SelfModelRegistryPort, request.app.state.self_model_registry)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _registered(request: Request, user: AuthenticatedUser) -> dict[str, Any]:
    if _store(request).is_account_unavailable(user_id=user.user_id):
        raise _error(status.HTTP_409_CONFLICT, "account_deletion_in_progress")
    account = _store(request).get_account(user_id=user.user_id)
    if account is None:
        raise _error(status.HTTP_403_FORBIDDEN, "account_not_registered")
    return account


@asynccontextmanager
async def _account_write(request: Request, account_id: str) -> AsyncIterator[None]:
    gate = cast(AccountOperationGate, request.app.state.account_operations)
    try:
        async with gate.write(account_id):
            yield
    except AccountDeletingError as exc:
        raise _error(status.HTTP_409_CONFLICT, "account_deletion_in_progress") from exc


async def require_self_model_writable(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> AsyncIterator[AuthenticatedUser]:
    async with _account_write(request, user.user_id):
        yield user


def _raise_registry_error(exc: Exception) -> NoReturn:
    if isinstance(exc, SelfModelNotFoundError):
        raise _error(status.HTTP_404_NOT_FOUND, "self_model_item_not_found") from exc
    if isinstance(exc, SelfModelVersionConflictError):
        raise _error(status.HTTP_409_CONFLICT, "self_model_version_conflict") from exc
    if isinstance(exc, SelfModelIdempotencyConflictError):
        raise _error(status.HTTP_409_CONFLICT, "idempotency_conflict") from exc
    if isinstance(exc, InvalidSelfModelTransitionError):
        raise _error(status.HTTP_409_CONFLICT, "invalid_transition") from exc
    if isinstance(exc, UntrustedSelfModelSourceError):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "untrusted_owner_source") from exc
    if isinstance(exc, RelationshipReferenceError):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "relationship_reference_invalid") from exc
    if isinstance(exc, ValueError):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_self_model_input") from exc
    raise exc


def _require_step_up(account: dict[str, Any], password: str | None) -> None:
    if password is None or not verify_password(password, str(account["password_hash"])):
        raise _error(status.HTTP_403_FORBIDDEN, "step_up_failed")


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _event_excerpt(event: EvidenceEvent) -> str:
    text = str(event.payload.get("text") or "").strip()
    if not text:
        parts = [
            f"选择：{value}"
            for value in (event.payload.get("chosen_option"),)
            if isinstance(value, str) and value.strip()
        ]
        for label, value in (
            ("结果", event.payload.get("outcome")),
            ("反思", event.payload.get("reflection")),
        ):
            if isinstance(value, str) and value.strip():
                parts.append(f"{label}：{value}")
        text = "；".join(parts)
    return " ".join(text.split())[:500]


async def _source_excerpts(
    request: Request,
    *,
    account_id: str,
    items: tuple[SelfModelItem, ...],
) -> dict[str, str]:
    event_ids = tuple(
        dict.fromkeys(
            source.source_event_id
            for item in items
            for source in item.sources
        )
    )
    if not event_ids:
        return {}
    events = await asyncio.gather(
        *(
            _archive(request).event(account_id=account_id, event_id=event_id)
            for event_id in event_ids
        )
    )
    return {
        event_id: _event_excerpt(event)
        for event_id, event in zip(event_ids, events, strict=True)
        if event is not None
    }


def _source_payload(
    source: SelfModelSource,
    excerpts: dict[str, str],
) -> dict[str, Any]:
    return {
        "source_event_id": source.source_event_id,
        "relation": source.relation,
        "adopted": source.adopted,
        "negative": source.negative,
        "speaker_class": source.speaker_class,
        "occurred_at": source.occurred_at.isoformat(),
        "excerpt": excerpts.get(source.source_event_id, ""),
    }


def _base_item_payload(
    item: SelfModelItem,
    excerpts: dict[str, str],
) -> dict[str, Any]:
    decision = activation_decision(item)
    return {
        "account_id": item.account_id,
        "status": item.status,
        "sharing_scope": item.sharing_scope,
        "unresolved_conflict": item.unresolved_conflict,
        "sources": [_source_payload(source, excerpts) for source in item.sources],
        "owner_reviewed_at": (
            item.owner_reviewed_at.isoformat() if item.owner_reviewed_at is not None else None
        ),
        "step_up_verified": item.step_up_verified,
        "effective": decision.effective,
        "effective_reasons": list(decision.reasons),
        "created_at": item.created_at.isoformat(),
    }


def _item_payload(
    item: SelfModelItem,
    excerpts: dict[str, str],
) -> dict[str, Any]:
    payload = _base_item_payload(item, excerpts)
    if isinstance(item, CognitiveClaim):
        payload.update(
            {
                "kind": "cognitive_claim",
                "claim_id": item.claim_id,
                "claim_type": item.claim_type,
                "statement": item.statement,
                "context": item.context,
                "confidence": item.confidence,
                "version": item.version,
                "updated_at": item.updated_at.isoformat(),
            }
        )
        return payload
    if isinstance(item, DecisionCase):
        payload.update(
            {
                "kind": "decision_case",
                "case_id": item.case_id,
                "decision_kind": item.kind,
                "context": item.context,
                "options": list(item.options),
                "constraints": list(item.constraints),
                "chosen_option": item.chosen_option,
                "rejected_options": list(item.rejected_options),
                "outcome": item.outcome,
                "reflection": item.reflection,
                "still_endorsed": item.still_endorsed,
                "version": item.version,
                "updated_at": item.updated_at.isoformat(),
            }
        )
        return payload
    payload.update(
        {
            "kind": "relationship_profile",
            "profile_id": item.profile_id,
            "version_number": item.version_number,
            "person_id": item.person_id,
            "relationship_id": item.relationship_id,
            "salutation": item.salutation,
            "tone": item.tone,
            "advice_style": item.advice_style,
            "boundaries": list(item.boundaries),
        }
    )
    return payload


async def _item_response(
    request: Request,
    item: SelfModelItem,
) -> dict[str, Any]:
    excerpts = await _source_excerpts(
        request,
        account_id=item.account_id,
        items=(item,),
    )
    return _item_payload(item, excerpts)


class SourceBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source_event_id: str = Field(min_length=1, max_length=128)
    relation: SourceRelation = "support"
    adopted: bool = True
    negative: bool = False


def _source_inputs(sources: list[SourceBody]) -> tuple[SourceInput, ...]:
    return tuple(
        SourceInput(
            source_event_id=source.source_event_id,
            relation=source.relation,
            adopted=source.adopted,
            negative=source.negative,
        )
        for source in sources
    )


class CognitiveClaimCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=256)
    claim_type: CognitiveClaimType
    statement: str = Field(min_length=1, max_length=4000)
    context: str = Field(default="", max_length=4000)
    confidence: float = Field(ge=0, le=1)
    sharing_scope: str = Field(default="private", min_length=1, max_length=128)
    unresolved_conflict: bool = False
    sources: list[SourceBody] = Field(default_factory=list, max_length=32)


class DecisionCaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=256)
    kind: DecisionKind
    context: str = Field(min_length=1, max_length=4000)
    options: list[str] = Field(min_length=1, max_length=32)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    chosen_option: str = Field(min_length=1, max_length=1000)
    rejected_options: list[str] = Field(default_factory=list, max_length=32)
    outcome: str = Field(default="", max_length=4000)
    reflection: str = Field(default="", max_length=4000)
    still_endorsed: bool = True
    sharing_scope: str = Field(default="private", min_length=1, max_length=128)
    unresolved_conflict: bool = False
    sources: list[SourceBody] = Field(default_factory=list, max_length=32)


class RelationshipProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=1, max_length=256)
    person_id: str = Field(min_length=1, max_length=128)
    relationship_id: str = Field(min_length=1, max_length=128)
    salutation: str = Field(min_length=1, max_length=256)
    tone: str = Field(min_length=1, max_length=1000)
    advice_style: str = Field(min_length=1, max_length=1000)
    boundaries: list[str] = Field(default_factory=list, max_length=64)
    sharing_scope: str = Field(default="private", min_length=1, max_length=128)
    unresolved_conflict: bool = False
    sources: list[SourceBody] = Field(default_factory=list, max_length=32)


class AddSourceBody(SourceBody):
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)


class CounterexampleBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    expected_version: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)


class ItemReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["confirmed", "disputed", "retracted", "superseded"]
    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    password: str | None = Field(default=None, min_length=8, max_length=128)


class RelationshipReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["approved", "revoked"]
    expected_status: Literal["candidate", "approved"]
    idempotency_key: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class RelationshipRevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    salutation: str = Field(min_length=1, max_length=256)
    tone: str = Field(min_length=1, max_length=1000)
    advice_style: str = Field(min_length=1, max_length=1000)
    boundaries: list[str] = Field(default_factory=list, max_length=64)
    sharing_scope: str = Field(min_length=1, max_length=128)
    unresolved_conflict: bool = False
    password: str = Field(min_length=8, max_length=128)


@router.get("")
async def list_self_model(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _registered(request, user)
    registry = _registry(request)
    try:
        claims = await registry.cognitive_claims(account_id=user.user_id)
        decisions = await registry.decision_cases(account_id=user.user_id)
        relationships = await registry.relationship_profiles(account_id=user.user_id)
    except Exception as exc:
        _raise_registry_error(exc)
    all_items: tuple[SelfModelItem, ...] = (*claims, *decisions, *relationships)
    excerpts = await _source_excerpts(
        request,
        account_id=user.user_id,
        items=all_items,
    )
    return {
        "claims": [_item_payload(item, excerpts) for item in claims],
        "decision_cases": [_item_payload(item, excerpts) for item in decisions],
        "relationship_profiles": [
            _item_payload(item, excerpts) for item in relationships
        ],
    }


@router.post("/claims", status_code=status.HTTP_201_CREATED)
async def create_cognitive_claim(
    body: CognitiveClaimCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    _registered(request, user)
    registry = _registry(request)
    try:
        item: SelfModelItem = await registry.create_cognitive_claim(
            account_id=user.user_id,
            claim_type=body.claim_type,
            statement=body.statement,
            context=body.context,
            confidence=body.confidence,
            sharing_scope=body.sharing_scope,
            unresolved_conflict=body.unresolved_conflict,
            idempotency_key=body.idempotency_key,
            sources=_source_inputs(body.sources),
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)


@router.post("/decision-cases", status_code=status.HTTP_201_CREATED)
async def create_decision_case(
    body: DecisionCaseCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    _registered(request, user)
    registry = _registry(request)
    try:
        item: SelfModelItem = await registry.create_decision_case(
            account_id=user.user_id,
            kind=body.kind,
            context=body.context,
            options=tuple(body.options),
            constraints=tuple(body.constraints),
            chosen_option=body.chosen_option,
            rejected_options=tuple(body.rejected_options),
            outcome=body.outcome,
            reflection=body.reflection,
            still_endorsed=body.still_endorsed,
            sharing_scope=body.sharing_scope,
            unresolved_conflict=body.unresolved_conflict,
            idempotency_key=body.idempotency_key,
            sources=_source_inputs(body.sources),
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)


@router.post("/relationship-profiles", status_code=status.HTTP_201_CREATED)
async def create_relationship_profile(
    body: RelationshipProfileCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    _registered(request, user)
    registry = _registry(request)
    try:
        item: SelfModelItem = await registry.create_relationship_profile(
            account_id=user.user_id,
            person_id=body.person_id,
            relationship_id=body.relationship_id,
            salutation=body.salutation,
            tone=body.tone,
            advice_style=body.advice_style,
            boundaries=tuple(body.boundaries),
            sharing_scope=body.sharing_scope,
            unresolved_conflict=body.unresolved_conflict,
            idempotency_key=body.idempotency_key,
            sources=_source_inputs(body.sources),
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)


@router.post("/items/{item_kind}/{item_id}/sources")
async def add_source(
    item_kind: SelfModelItemKind,
    item_id: str,
    body: AddSourceBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    _registered(request, user)
    try:
        item = await _registry(request).add_source(
            account_id=user.user_id,
            item_kind=item_kind,
            item_id=item_id,
            source_event_id=body.source_event_id,
            relation=body.relation,
            adopted=body.adopted,
            negative=body.negative,
            expected_version=body.expected_version,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)


@router.post("/claims/{claim_id}/counterexamples")
async def add_cognitive_claim_counterexample(
    claim_id: str,
    body: CounterexampleBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    _registered(request, user)
    registry = _registry(request)
    try:
        current = await registry.get_cognitive_claim(
            account_id=user.user_id,
            claim_id=claim_id,
        )
        if any(
            source.source_event_id == body.event_id
            and source.relation == "counterexample"
            and not source.negative
            for source in current.sources
        ):
            return await _item_response(request, current)
        if current.version != body.expected_version:
            raise SelfModelVersionConflictError(claim_id)
    except Exception as exc:
        _raise_registry_error(exc)

    event = EvidenceEvent(
        event_id=body.event_id,
        account_id=user.user_id,
        event_type="owner.action_recorded",
        occurred_at=datetime.now(UTC),
        speaker_class="owner",
        source="user.self_model_counterexample",
        payload={
            "action_type": "self_model_counterexample",
            "target_kind": "cognitive_claim",
            "target_id": claim_id,
            "text": body.text,
            "owner_projection_eligible": True,
            "simulated_output": False,
        },
    )
    try:
        result = await request.app.state.life_archive.record(event)
    except IdempotencyConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "idempotency_conflict") from exc
    worker = getattr(request.app.state, "memory_compiler_worker", None)
    if worker is not None and not result.duplicate:
        worker.wake()

    for _ in range(3):
        try:
            current = await registry.get_cognitive_claim(
                account_id=user.user_id,
                claim_id=claim_id,
            )
            if any(
                source.source_event_id == body.event_id
                and source.relation == "counterexample"
                and not source.negative
                for source in current.sources
            ):
                return await _item_response(request, current)
            item = await registry.add_source(
                account_id=user.user_id,
                item_kind="cognitive_claim",
                item_id=claim_id,
                source_event_id=body.event_id,
                relation="counterexample",
                adopted=False,
                negative=False,
                expected_version=current.version,
                idempotency_key=(
                    f"self-model-counterexample:{body.event_id}:v{current.version}"
                ),
            )
            return await _item_response(request, item)
        except SelfModelVersionConflictError:
            continue
        except Exception as exc:
            _raise_registry_error(exc)
    raise _error(status.HTTP_409_CONFLICT, "self_model_version_conflict")


@router.post("/claims/{claim_id}/review")
async def review_cognitive_claim(
    claim_id: str,
    body: ItemReviewBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    account = _registered(request, user)
    registry = _registry(request)
    try:
        current = await registry.get_cognitive_claim(
            account_id=user.user_id, claim_id=claim_id
        )
        step_up = (
            body.status == "confirmed"
            and current.claim_type in HIGH_SENSITIVITY_CLAIM_TYPES
        )
        if step_up:
            _require_step_up(account, body.password)
        item = await registry.review_cognitive_claim(
            account_id=user.user_id,
            claim_id=claim_id,
            status=cast(ItemStatus, body.status),
            expected_version=body.expected_version,
            step_up_verified=step_up,
            idempotency_key=body.idempotency_key,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)


@router.post("/decision-cases/{case_id}/review")
async def review_decision_case(
    case_id: str,
    body: ItemReviewBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    _registered(request, user)
    try:
        item = await _registry(request).review_decision_case(
            account_id=user.user_id,
            case_id=case_id,
            status=cast(ItemStatus, body.status),
            expected_version=body.expected_version,
            step_up_verified=False,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)


@router.post("/relationship-profiles/{profile_id}/versions/{version_number}/review")
async def review_relationship_profile(
    profile_id: str,
    version_number: int,
    body: RelationshipReviewBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    account = _registered(request, user)
    _require_step_up(account, body.password)
    try:
        item = await _registry(request).review_relationship_profile(
            account_id=user.user_id,
            profile_id=profile_id,
            version_number=version_number,
            status=body.status,
            expected_status=body.expected_status,
            step_up_verified=True,
            idempotency_key=body.idempotency_key,
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)


@router.post("/relationship-profiles/{profile_id}/revisions", status_code=201)
async def revise_relationship_profile(
    profile_id: str,
    body: RelationshipRevisionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_self_model_writable)],
) -> dict[str, Any]:
    account = _registered(request, user)
    _require_step_up(account, body.password)
    try:
        item = await _registry(request).revise_relationship_profile(
            account_id=user.user_id,
            profile_id=profile_id,
            expected_version=body.expected_version,
            salutation=body.salutation,
            tone=body.tone,
            advice_style=body.advice_style,
            boundaries=tuple(body.boundaries),
            idempotency_key=body.idempotency_key,
            sharing_scope=body.sharing_scope,
            unresolved_conflict=body.unresolved_conflict,
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return await _item_response(request, item)
