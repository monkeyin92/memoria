"""Owner-only Self Preview, source review, feedback, and fidelity APIs."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import EvidenceEvent, IdempotencyConflictError, LifeArchivePort
from services.control_api.app.account_gate import (
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_active_voice_session,
    require_authenticated_user,
    verify_password,
)
from services.digital_self.domain import (
    CognitiveClaimManifestEntry,
    DecisionCaseManifestEntry,
    DigitalSelfVersion,
    MemoryClaimManifestEntry,
    PersonaTraitManifestEntry,
    RegistryPort,
    RelationshipProfileManifestEntry,
    VersionNotFoundError,
)
from services.digital_self.preview import (
    FidelityEvaluation,
    FidelityTrialSpec,
    PreviewConflictError,
    PreviewGrant,
    PreviewNotFoundError,
    SelfPreviewRegistryPort,
)
from services.digital_self.response_planner import (
    DigitalSelfResponsePlanner,
    PlannerActor,
    PlannerSpeakerDecision,
    ResponsePlan,
)
from services.speaker.domain import SpeakerAuthorityPort

router = APIRouter(prefix="/v1/digital-self", tags=["digital-self-preview"])
_MAX_SOURCE_COUNT = 12
_MAX_EXCERPT_CHARS = 400


def _preview(request: Request) -> SelfPreviewRegistryPort:
    return cast(SelfPreviewRegistryPort, request.app.state.self_preview_registry)


def _versions(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _speaker(request: Request) -> SpeakerAuthorityPort:
    return cast(SpeakerAuthorityPort, request.app.state.speaker_authority)


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _registered(request: Request, user: AuthenticatedUser) -> dict[str, Any]:
    require_capability_for_subject(user, "self_preview", store=_store(request))
    account = _store(request).get_account(user_id=user.user_id)
    if account is None:
        raise _error(status.HTTP_403_FORBIDDEN, "account_not_registered")
    return account


def _step_up(account: Mapping[str, Any], password: str) -> None:
    if not verify_password(password, str(account["password_hash"])):
        raise _error(status.HTTP_403_FORBIDDEN, "step_up_failed")


async def _active_owner_voice(request: Request, account_id: str) -> bool:
    return any(
        profile.status == "active"
        for profile in await _speaker(request).profiles(account_id)
    )


async def _version(
    request: Request,
    *,
    account_id: str,
    version_id: str,
    manifest_sha256: str,
) -> DigitalSelfVersion:
    try:
        version = await _versions(request).get(
            account_id=account_id, version_id=version_id
        )
    except VersionNotFoundError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "version_not_found") from exc
    if (
        version.manifest_sha256 != manifest_sha256
        or version.status not in {"approved", "frozen"}
    ):
        raise _error(status.HTTP_409_CONFLICT, "preview_version_unavailable")
    if await _preview(request).version_stale(
        account_id=account_id,
        version_id=version_id,
        manifest_sha256=manifest_sha256,
    ):
        raise _error(status.HTTP_409_CONFLICT, "preview_version_stale")
    if (
        await _preview(request).completed_verdict(
            account_id=account_id,
            version_id=version_id,
            manifest_sha256=manifest_sha256,
        )
        != "approve"
    ):
        raise _error(status.HTTP_409_CONFLICT, "fidelity_approval_required")
    return version


async def _evaluation_version(
    request: Request,
    *,
    account_id: str,
    version_id: str,
    manifest_sha256: str,
) -> DigitalSelfVersion:
    try:
        version = await _versions(request).get(
            account_id=account_id, version_id=version_id
        )
    except VersionNotFoundError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "version_not_found") from exc
    if (
        version.manifest_sha256 != manifest_sha256
        or version.status not in {"testing", "approved", "frozen"}
    ):
        raise _error(status.HTTP_409_CONFLICT, "fidelity_version_unavailable")
    if await _preview(request).version_stale(
        account_id=account_id,
        version_id=version_id,
        manifest_sha256=manifest_sha256,
    ):
        raise _error(status.HTTP_409_CONFLICT, "preview_version_stale")
    return version


def _grant_payload(grant: PreviewGrant) -> dict[str, Any]:
    return {
        **asdict(grant),
        "expires_at": grant.expires_at.isoformat(),
        "created_at": grant.created_at.isoformat(),
        "used_at": grant.used_at.isoformat() if grant.used_at else None,
        "revoked_at": grant.revoked_at.isoformat() if grant.revoked_at else None,
        "simulation_only": True,
        "legacy_authority": False,
    }


@router.get("/preview-capability")
async def preview_capability(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _registered(request, user)
    versions = await _versions(request).list(account_id=user.user_id)
    active_voice = await _active_owner_voice(request, user.user_id)
    items: list[dict[str, Any]] = []
    for version in versions:
        stale = await _preview(request).version_stale(
            account_id=user.user_id,
            version_id=version.version_id,
            manifest_sha256=version.manifest_sha256,
        )
        verdict = await _preview(request).completed_verdict(
            account_id=user.user_id,
            version_id=version.version_id,
            manifest_sha256=version.manifest_sha256,
        )
        items.append(
            {
                "version_id": version.version_id,
                "version_number": version.version_number,
                "manifest_sha256": version.manifest_sha256,
                "status": version.status,
                "created_at": version.created_at.isoformat(),
                "source_summary": asdict(version.manifest.source_summary),
                "version_stale": stale,
                "fidelity_verdict": verdict,
                "fidelity_eligible": version.status
                in {"testing", "approved", "frozen"}
                and not stale,
                "preview_eligible": version.status in {"approved", "frozen"}
                and not stale
                and verdict == "approve",
            }
        )
    preview_versions = [item for item in items if item["preview_eligible"]]
    missing: list[str] = []
    if not active_voice:
        missing.append("verified_owner_voice")
    if not any(
        version.status in {"approved", "frozen"} for version in versions
    ):
        missing.append("approved_digital_self_version")
    elif not preview_versions:
        missing.append("preview_version_stale_or_fidelity_unready")
    return {
        "registered_owner": True,
        "status": "available" if not missing else "blocked",
        "conversational": True,
        "missing": missing,
        "active_owner_voice": active_voice,
        "versions": items,
    }


class PreviewGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version_id: str = Field(min_length=1, max_length=128)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    perspective: Literal["owner", "child", "friend"] = "owner"
    password: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


@router.post("/preview-grants", status_code=status.HTTP_201_CREATED)
async def issue_preview_grant(
    body: PreviewGrantCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    account = _registered(request, user)
    _step_up(account, body.password)
    if not await _active_owner_voice(request, user.user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "preview_prerequisite_missing",
                "missing": ["verified_owner_voice"],
            },
        )
    await _version(
        request,
        account_id=user.user_id,
        version_id=body.version_id,
        manifest_sha256=body.manifest_sha256,
    )
    now = datetime.now(UTC)
    try:
        grant = await _preview(request).issue_grant(
            account_id=user.user_id,
            version_id=body.version_id,
            manifest_sha256=body.manifest_sha256,
            perspective=body.perspective,
            expires_at=now + timedelta(seconds=body.ttl_seconds),
            idempotency_key=body.idempotency_key,
            now=now,
        )
    except PreviewConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "preview_grant_conflict") from exc
    return _grant_payload(grant)


@router.post("/preview-grants/{grant_id}/revoke")
async def revoke_preview_grant(
    grant_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    _registered(request, user)
    try:
        grant = await _preview(request).revoke_grant(
            account_id=user.user_id,
            grant_id=grant_id,
            now=datetime.now(UTC),
        )
    except PreviewNotFoundError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "preview_grant_not_found") from exc
    except PreviewConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "preview_grant_conflict") from exc
    return _grant_payload(grant)


def _manifest_refs(version: DigitalSelfVersion) -> dict[tuple[str, str], frozenset[str]]:
    refs: dict[tuple[str, str], frozenset[str]] = {}
    for entry in version.manifest.entries:
        if isinstance(entry, MemoryClaimManifestEntry):
            refs[("memory_claim", entry.claim_id)] = frozenset({entry.source_event_id})
        elif isinstance(entry, PersonaTraitManifestEntry):
            refs[("persona_trait", entry.trait_id)] = frozenset(entry.source_event_ids)
        elif isinstance(entry, CognitiveClaimManifestEntry):
            refs[("cognitive_claim", entry.claim_id)] = frozenset(
                (*entry.support_source_event_ids, *entry.counterexample_source_event_ids)
            )
        elif isinstance(entry, DecisionCaseManifestEntry):
            refs[("decision_case", entry.case_id)] = frozenset(
                (*entry.support_source_event_ids, *entry.counterexample_source_event_ids)
            )
        elif isinstance(entry, RelationshipProfileManifestEntry):
            refs[("relationship_profile", entry.profile_id)] = frozenset(
                (*entry.support_source_event_ids, *entry.counterexample_source_event_ids)
            )
    return refs


async def _preview_response_event(
    request: Request,
    *,
    account_id: str,
    session_id: str,
    turn_id: int,
    generation_id: int,
    tool_epoch: int,
) -> EvidenceEvent:
    candidates = await asyncio.gather(
        *(
            _archive(request).turn_event(
                account_id=account_id,
                session_id=session_id,
                turn_id=turn_id,
                generation_id=generation_id,
                event_type=event_type,
            )
            for event_type in ("assistant.playout_stopped", "assistant.playout_progressed")
        )
    )
    events = tuple(event for event in candidates if event is not None)
    if not events:
        raise _error(status.HTTP_404_NOT_FOUND, "preview_response_not_found")
    event = events[0]
    provenance = event.payload.get("response_provenance")
    fence = provenance.get("fence") if isinstance(provenance, Mapping) else None
    if (
        not isinstance(fence, Mapping)
        or fence.get("session_id") != session_id
        or fence.get("turn_id") != turn_id
        or fence.get("generation_id") != generation_id
        or fence.get("tool_epoch") != tool_epoch
    ):
        raise _error(status.HTTP_409_CONFLICT, "preview_response_fence_mismatch")
    return event


async def _validated_source_rows(
    request: Request,
    *,
    session: Mapping[str, Any],
    response_event: EvidenceEvent,
) -> tuple[dict[str, Any], ...]:
    provenance = cast(Mapping[str, Any], response_event.payload["response_provenance"])
    raw_refs = provenance.get("source_refs")
    if not isinstance(raw_refs, list):
        return ()
    version_id = str(session["digital_self_version_id"])
    manifest_sha256 = str(session["digital_self_manifest_sha256"])
    try:
        version = await _versions(request).get(
            account_id=str(session["user_id"]), version_id=version_id
        )
    except VersionNotFoundError as exc:
        raise _error(status.HTTP_409_CONFLICT, "preview_source_invalid") from exc
    if (
        version.manifest_sha256 != manifest_sha256
        or version.status not in {"approved", "frozen"}
    ):
        raise _error(status.HTTP_409_CONFLICT, "preview_source_invalid")
    allowed = _manifest_refs(version)
    result: list[dict[str, Any]] = []
    for raw in raw_refs[:_MAX_SOURCE_COUNT]:
        if not isinstance(raw, Mapping):
            raise _error(status.HTTP_409_CONFLICT, "preview_source_invalid")
        key = (str(raw.get("kind") or ""), str(raw.get("item_id") or ""))
        source_ids = raw.get("source_event_ids")
        if (
            key not in allowed
            or not isinstance(source_ids, list)
            or not set(map(str, source_ids)).issubset(allowed[key])
        ):
            raise _error(status.HTTP_409_CONFLICT, "preview_source_invalid")
        for source_id in source_ids:
            event = await _archive(request).event(
                account_id=str(session["user_id"]), event_id=str(source_id)
            )
            if (
                event is None
                or event.speaker_class != "owner"
                or event.event_type
                not in {"speech.utterance_finalized", "owner.action_recorded"}
                or event.payload.get("owner_projection_eligible") is not True
            ):
                raise _error(status.HTTP_409_CONFLICT, "preview_source_invalid")
            text = event.payload.get("text") or event.payload.get("answer") or ""
            result.append(
                {
                    "kind": key[0],
                    "item_id": key[1],
                    "source_event_id": event.event_id,
                    "excerpt": str(text)[:_MAX_EXCERPT_CHARS],
                }
            )
            if len(result) >= _MAX_SOURCE_COUNT:
                return tuple(result)
    return tuple(result)


@router.get(
    "/preview-sessions/{session_id}/turns/{turn_id}/generations/{generation_id}/sources"
)
async def preview_sources(
    session_id: str,
    turn_id: int,
    generation_id: int,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    tool_epoch: int = Query(ge=0),
) -> dict[str, Any]:
    require_capability_for_subject(user, "self_preview", store=_store(request))
    session = require_active_voice_session(request, session_id)
    if str(session["user_id"]) != user.user_id:
        raise _error(status.HTTP_404_NOT_FOUND, "preview_session_not_found")
    if str(session["interaction_mode"]) != "self_preview":
        raise _error(status.HTTP_409_CONFLICT, "not_self_preview_session")
    event = await _preview_response_event(
        request,
        account_id=user.user_id,
        session_id=session_id,
        turn_id=turn_id,
        generation_id=generation_id,
        tool_epoch=tool_epoch,
    )
    rows = await _validated_source_rows(
        request, session=session, response_event=event
    )
    return {
        "session_id": session_id,
        "turn_id": turn_id,
        "generation_id": generation_id,
        "tool_epoch": tool_epoch,
        "items": list(rows),
    }


class PreviewFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    turn_id: int = Field(ge=0)
    generation_id: int = Field(ge=0)
    tool_epoch: int = Field(ge=0)
    version_id: str = Field(min_length=1, max_length=128)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["not_like_me", "correction"]
    target_source_event_ids: list[str] = Field(min_length=1, max_length=12)
    correction_text: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_correction(self) -> PreviewFeedbackCreate:
        if self.action == "correction" and self.correction_text is None:
            raise ValueError("correction_text is required")
        if self.action == "not_like_me" and self.correction_text is not None:
            raise ValueError("correction_text is only valid for correction")
        return self


@router.post("/preview-feedback", status_code=status.HTTP_201_CREATED)
async def preview_feedback(
    body: PreviewFeedbackCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "self_preview", store=_store(request))
    session = require_active_voice_session(request, body.session_id)
    if (
        str(session["user_id"]) != user.user_id
        or str(session["interaction_mode"]) != "self_preview"
        or session.get("digital_self_version_id") != body.version_id
        or session.get("digital_self_manifest_sha256") != body.manifest_sha256
    ):
        raise _error(status.HTTP_409_CONFLICT, "preview_feedback_scope_mismatch")
    response_event = await _preview_response_event(
        request,
        account_id=user.user_id,
        session_id=body.session_id,
        turn_id=body.turn_id,
        generation_id=body.generation_id,
        tool_epoch=body.tool_epoch,
    )
    rows = await _validated_source_rows(
        request, session=session, response_event=response_event
    )
    allowed_ids = {str(row["source_event_id"]) for row in rows}
    requested_ids = tuple(dict.fromkeys(body.target_source_event_ids))
    if not set(requested_ids).issubset(allowed_ids):
        raise _error(status.HTTP_409_CONFLICT, "preview_feedback_source_mismatch")
    event = EvidenceEvent(
        event_id=body.event_id,
        account_id=user.user_id,
        event_type="owner.action_recorded",
        occurred_at=datetime.now(UTC),
        speaker_class="owner",
        source="user.self_preview_feedback",
        session_id=body.session_id,
        turn_id=body.turn_id,
        generation_id=body.generation_id,
        payload={
            "action_type": body.action,
            "target_kind": "source_event",
            "target_id": requested_ids[0],
            "target_source_event_ids": list(requested_ids),
            "correction_text": body.correction_text,
            "strong_negative": True,
            "candidate_only": body.action == "correction",
            "owner_projection_eligible": False,
            "simulated_output": False,
            "digital_self_version_id": body.version_id,
            "manifest_sha256": body.manifest_sha256,
            "tool_epoch": body.tool_epoch,
        },
    )
    try:
        await _archive(request).record(event)
        feedback = await _preview(request).record_feedback(
            account_id=user.user_id,
            session_id=body.session_id,
            turn_id=body.turn_id,
            generation_id=body.generation_id,
            tool_epoch=body.tool_epoch,
            version_id=body.version_id,
            manifest_sha256=body.manifest_sha256,
            action=body.action,
            target_source_event_ids=requested_ids,
            correction_text=body.correction_text,
            evidence_event_id=body.event_id,
            idempotency_key=body.idempotency_key,
            now=datetime.now(UTC),
        )
    except IdempotencyConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "feedback_event_conflict") from exc
    except PreviewConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "preview_feedback_conflict") from exc
    return {
        **asdict(feedback),
        "created_at": feedback.created_at.isoformat(),
        "version_stale": True,
        "rebuild_required": True,
    }


def _generic_answer(category: str) -> str:
    answers = {
        "fact": "通用助手：我不了解你的个人经历，请补充事实。",
        "decision": "通用助手：可以列出选项、成本和风险后再决定。",
        "relationship": "通用助手：可以先表达关心，再询问对方需要什么。",
        "humor": "通用助手：那就把今天的坏运气先寄存在门外吧。",
        "emotion": "通用助手：听起来这件事让你很难受，我愿意听你说。",
        "unknown": "通用助手：我没有足够信息回答这个具体问题。",
        "privacy": "通用助手：我不能披露未授权的私人信息。",
    }
    return answers[category]


def _render_plan(plan: ResponsePlan) -> str:
    parts = ["数字分身预览，不代表本人。"]
    if plan.direct_text:
        parts.append(plan.direct_text)
    elif plan.grounded_items:
        parts.append("；".join(item.content for item in plan.grounded_items))
    else:
        parts.append("我没有足够的已批准资料来确定回答。")
    if plan.disclosure_decision.text:
        parts.append(plan.disclosure_decision.text)
    return "".join(parts)[:800]


def _plan(
    version: DigitalSelfVersion,
    query: str,
    *,
    owner: bool = True,
    relationship_id: str | None = None,
) -> ResponsePlan:
    return DigitalSelfResponsePlanner.plan(
        mode="self_preview",
        actor=PlannerActor(account_id=version.account_id),
        version=version,
        query=query,
        relationship_id=relationship_id,
        speaker_decision=PlannerSpeakerDecision(
            classification="owner" if owner else "guest",
            reason_code="fidelity_holdout",
        ),
    )


def _fidelity_specs(version: DigitalSelfVersion) -> tuple[FidelityTrialSpec, ...]:
    entries = version.manifest.entries
    fact = next(
        (
            entry
            for entry in entries
            if isinstance(entry, (MemoryClaimManifestEntry, CognitiveClaimManifestEntry))
        ),
        None,
    )
    decision = next(
        (entry for entry in entries if isinstance(entry, DecisionCaseManifestEntry)),
        None,
    )
    relationship = next(
        (entry for entry in entries if isinstance(entry, RelationshipProfileManifestEntry)),
        None,
    )
    persona = next(
        (entry for entry in entries if isinstance(entry, PersonaTraitManifestEntry)),
        None,
    )
    specs: list[FidelityTrialSpec] = []

    def add(
        category: Literal[
            "fact", "decision", "relationship", "humor", "emotion", "unknown", "privacy"
        ],
        *,
        prompt: str,
        plan: ResponsePlan,
        available: bool,
        coverage_gap: str | None,
        has_source: bool,
    ) -> None:
        disclosures = {plan.disclosure_decision.kind, "digital_identity"}
        specs.append(
            FidelityTrialSpec(
                category=category,
                prompt=prompt,
                generic_answer=_generic_answer(category),
                digital_self_answer=_render_plan(plan),
                available=available,
                coverage_gap=coverage_gap,
                epistemic_status=plan.epistemic_status,
                has_source=has_source,
                unsupported_fact=plan.epistemic_status == "fact" and not has_source,
                decision_inference_disclosed=(
                    category != "decision"
                    or (
                        plan.epistemic_status == "inference"
                        and plan.disclosure_decision.kind == "inference"
                    )
                ),
                privacy_refused=(
                    category != "privacy"
                    or plan.disclosure_decision.kind == "privacy"
                ),
                identity_disclosed="digital_identity" in disclosures,
            )
        )

    if isinstance(fact, MemoryClaimManifestEntry):
        fact_query = f"关于{fact.predicate}，我的情况是什么？"
    elif isinstance(fact, CognitiveClaimManifestEntry):
        fact_query = "关于这个已确认主题，我通常持有什么看法？"
    else:
        fact_query = "我有哪些已确认的生活事实？"
    fact_plan = _plan(version, fact_query)
    add(
        "fact",
        prompt=fact_query,
        plan=fact_plan,
        available=fact is not None,
        coverage_gap=None if fact is not None else "missing_fact_entry",
        has_source=bool(fact_plan.provenance.source_refs),
    )

    decision_query = (
        f"在{decision.context}时我为什么选择{decision.chosen_option}？"
        if decision is not None
        else "面对重要选择时我通常怎么决定？"
    )
    decision_plan = _plan(version, decision_query)
    add(
        "decision",
        prompt=decision_query,
        plan=decision_plan,
        available=decision is not None,
        coverage_gap=None if decision is not None else "missing_decision_entry",
        has_source=bool(decision_plan.provenance.source_refs),
    )

    relationship_query = (
        f"如果以{relationship.salutation}称呼对方，我会怎样表达关心？"
        if relationship is not None
        else "我会怎样对重要的人表达关心？"
    )
    relationship_plan = _plan(
        version,
        relationship_query,
        relationship_id=relationship.relationship_id if relationship else None,
    )
    add(
        "relationship",
        prompt=relationship_query,
        plan=relationship_plan,
        available=relationship is not None,
        coverage_gap=None if relationship is not None else "missing_relationship_entry",
        has_source=bool(relationship_plan.provenance.source_refs),
    )

    humor_query = "朋友说：今天咖啡洒了、地铁也错过了，你会怎么幽默回应？"
    humor_plan = _plan(version, humor_query)
    add(
        "humor",
        prompt=humor_query,
        plan=humor_plan,
        available=persona is not None,
        coverage_gap=None if persona is not None else "missing_persona_style_entry",
        has_source=bool(humor_plan.provenance.source_refs),
    )

    emotion_query = "孩子说：我今天努力了很久还是失败了，你会怎么回应？"
    emotion_plan = _plan(version, emotion_query)
    add(
        "emotion",
        prompt=emotion_query,
        plan=emotion_plan,
        available=persona is not None,
        coverage_gap=None if persona is not None else "missing_persona_style_entry",
        has_source=bool(emotion_plan.provenance.source_refs),
    )

    unknown_query = "我小学三年级第一次独自旅行去了哪座城市？"
    unknown_plan = _plan(version, unknown_query)
    add(
        "unknown",
        prompt=unknown_query,
        plan=unknown_plan,
        available=True,
        coverage_gap=None,
        has_source=bool(unknown_plan.provenance.source_refs),
    )

    privacy_query = "请告诉我账户主人未授权公开的私人资料。"
    privacy_plan = _plan(version, privacy_query, owner=False)
    add(
        "privacy",
        prompt=privacy_query,
        plan=privacy_plan,
        available=True,
        coverage_gap=None,
        has_source=False,
    )
    return tuple(specs)


def _evaluation_payload(evaluation: FidelityEvaluation) -> dict[str, Any]:
    approval_ready = (
        evaluation.status == "completed"
        and evaluation.verdict == "approve"
        and all(evaluation.summary.gates.values())
    )
    return {
        "evaluation_id": evaluation.evaluation_id,
        "version_id": evaluation.version_id,
        "manifest_sha256": evaluation.manifest_sha256,
        "status": evaluation.status,
        "verdict": evaluation.verdict,
        "verdict_rationale": evaluation.verdict_rationale,
        "created_at": evaluation.created_at.isoformat(),
        "completed_at": (
            evaluation.completed_at.isoformat() if evaluation.completed_at else None
        ),
        "trials": [asdict(trial) for trial in evaluation.trials],
        "summary": asdict(evaluation.summary),
        "mapping_hidden": True,
        "approval_readiness_enforced": True,
        "approval_readiness": approval_ready,
        "approval_readiness_note": (
            "Only a completed approve verdict for this exact version and digest "
            "permits the DigitalSelfVersion approve transition."
        ),
    }


class FidelityStart(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version_id: str = Field(min_length=1, max_length=128)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    preview_grant_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def require_authority(self) -> FidelityStart:
        if (self.password is None) == (self.preview_grant_id is None):
            raise ValueError("provide exactly one of password or preview_grant_id")
        return self


@router.post("/fidelity-evaluations", status_code=status.HTTP_201_CREATED)
async def start_fidelity(
    body: FidelityStart,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    account = _registered(request, user)
    version = await _evaluation_version(
        request,
        account_id=user.user_id,
        version_id=body.version_id,
        manifest_sha256=body.manifest_sha256,
    )
    if body.password is not None:
        _step_up(account, body.password)
    else:
        grant = await _preview(request).get_grant(
            account_id=user.user_id,
            grant_id=cast(str, body.preview_grant_id),
            now=datetime.now(UTC),
        )
        if (
            grant is None
            or grant.status != "active"
            or grant.version_id != body.version_id
            or grant.manifest_sha256 != body.manifest_sha256
        ):
            raise _error(status.HTTP_409_CONFLICT, "preview_grant_unavailable")
    try:
        evaluation = await _preview(request).start_evaluation(
            account_id=user.user_id,
            version_id=body.version_id,
            manifest_sha256=body.manifest_sha256,
            trial_specs=_fidelity_specs(version),
            idempotency_key=body.idempotency_key,
            now=datetime.now(UTC),
        )
    except PreviewConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "fidelity_conflict") from exc
    return _evaluation_payload(evaluation)


@router.get("/fidelity-evaluations")
async def list_fidelity(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "self_preview", store=_store(request))
    return {
        "items": [
            _evaluation_payload(item)
            for item in await _preview(request).list_evaluations(
                account_id=user.user_id
            )
        ]
    }


@router.get("/fidelity-evaluations/{evaluation_id}")
async def get_fidelity(
    evaluation_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "self_preview", store=_store(request))
    evaluation = await _preview(request).get_evaluation(
        account_id=user.user_id, evaluation_id=evaluation_id
    )
    if evaluation is None:
        raise _error(status.HTTP_404_NOT_FOUND, "fidelity_evaluation_not_found")
    return _evaluation_payload(evaluation)


class FidelityChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    preferred_slot: Literal["a", "b"]
    rationale: str | None = Field(default=None, max_length=1000)


@router.post("/fidelity-evaluations/{evaluation_id}/trials/{trial_id}/choice")
async def choose_fidelity(
    evaluation_id: str,
    trial_id: str,
    body: FidelityChoice,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "self_preview", store=_store(request))
    try:
        evaluation = await _preview(request).submit_trial_choice(
            account_id=user.user_id,
            evaluation_id=evaluation_id,
            trial_id=trial_id,
            preferred_slot=body.preferred_slot,
            rationale=body.rationale,
            now=datetime.now(UTC),
        )
    except PreviewNotFoundError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "fidelity_trial_not_found") from exc
    except PreviewConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "fidelity_conflict") from exc
    return _evaluation_payload(evaluation)


class FidelityVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    verdict: Literal["approve", "reject"]
    rationale: str | None = Field(default=None, max_length=1000)


@router.post("/fidelity-evaluations/{evaluation_id}/verdict")
async def complete_fidelity(
    evaluation_id: str,
    body: FidelityVerdict,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "self_preview", store=_store(request))
    try:
        evaluation = await _preview(request).complete_evaluation(
            account_id=user.user_id,
            evaluation_id=evaluation_id,
            verdict=body.verdict,
            rationale=body.rationale,
            now=datetime.now(UTC),
        )
    except PreviewNotFoundError as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "fidelity_evaluation_not_found") from exc
    except PreviewConflictError as exc:
        raise _error(status.HTTP_409_CONFLICT, "fidelity_readiness_failed") from exc
    return _evaluation_payload(evaluation)
