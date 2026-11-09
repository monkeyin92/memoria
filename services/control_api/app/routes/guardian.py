"""Guardian binding, consent, child-data, and transcript-free summary routes."""

from __future__ import annotations

import hmac
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import (
    EvidenceNotFoundError,
    LifeArchivePort,
)
from services.archive.memory_domain import (
    MemoryCatalogPort,
    MemoryClaimReview,
    MemorySearchQuery,
)
from services.archive.memory_retractions import list_retracted_claims
from services.control_api.app.account_gate import (
    require_capability_for_account_id,
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.guardian_binding_authority import (
    GuardianConsentCreate,
    owns_accountless_child,
    person_grant_body_matches,
    require_binding_owner_for_subject,
    sync_bound_subject_authority,
)
from services.control_api.app.guardian_child_data import (
    child_memory_account,
    child_turns,
    consent_reference,
    guardian_evidence_account,
)
from services.control_api.app.guardian_lifecycle import (
    deletion_confirmed,
    disable_minor_restricted_assets,
    erase_child_kind,
    purge_minor_raw_voice,
)
from services.control_api.app.guardian_notifications import safety_alert_items
from services.control_api.app.guardian_payloads import (
    active_link_or_403,
    binding_code,
    binding_digest,
    consent_payload,
    link_payload,
    person_consent_payload,
    record_link_event,
    require_wechat_guardian,
)
from services.control_api.app.guardian_recap import (
    day_activity,
    recap_for_day,
)
from services.control_api.app.response_plan_cache import forget_subject_plans
from services.control_api.app.routes.crisis_script_interrupt import (
    router as crisis_script_interrupt_router,
)
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.control_api.app.session_termination import AccountSessionTerminator
from services.control_api.app.wechat_auth import WechatAuthError, code_to_session, openid_hash
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionIncompleteError,
)
from services.governance.subject_deletion import (
    SubjectDeletionIncompleteError,
    SubjectDeletionService,
)
from services.governance.subject_ports import SubjectScope
from services.guardian.child_archive_review import (
    ChildArchiveReviewCommand,
    ChildArchiveReviewError,
    view_to_api,
)
from services.guardian.child_memory import (
    KIND_CHILD_ARCHIVE,
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    DialogueSummary,
)
from services.guardian.consent import GuardianConsentService
from services.guardian.corpus import CorpusRetentionService
from services.guardian.crisis import CrisisNotificationStorePort
from services.guardian.domain import (
    AgeEvidenceStatus,
    BirthYearBand,
    ConsentKind,
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianNotFoundError,
    GuardianStorePort,
    Relation,
    SubjectCategory,
    SubjectTransitionError,
    validate_subject_transition,
)
from services.guardian.push import PushSubscriptionResult, PushSubscriptionStorePort
from services.guardian.weekly_report import WeeklyReportProjector
from services.memory_scope.child_layers import ChildLayerStore

router = APIRouter(prefix="/v1/guardian", tags=["guardian"])
router.include_router(crisis_script_interrupt_router)
_BINDING_TTL = timedelta(minutes=15)
_CONSENT_KINDS: tuple[ConsentKind, ...] = (
    "minor_voice_session",
    "memory_retention",
    "weekly_report",
    "corpus_recording",
    "child_archive",
)
_WEEKLY_EVENT_TYPES = (
    "emotion_observation",
    "tutor.practice_completed",
    "study.progress_updated",
    "topic.observation",
)


class GuardianLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    minor_user_id: str = Field(min_length=1, max_length=128)
    relation: Relation = "parent"


class GuardianLinkConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    binding_code: str = Field(pattern=r"^\d{8}$")
    birth_year_band: BirthYearBand


class GuardianPushSubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    template_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    result: PushSubscriptionResult
    login_code: str | None = Field(default=None, min_length=1, max_length=256)


class GuardianDeleteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    confirmation: str = Field(min_length=1, max_length=64)


def _store(request: Request) -> GuardianStorePort:
    return cast(GuardianStorePort, request.app.state.guardian_store)


def _notification_store(request: Request) -> CrisisNotificationStorePort:
    return cast(CrisisNotificationStorePort, request.app.state.guardian_store)


def _corpus_retention(request: Request) -> CorpusRetentionService:
    return cast(CorpusRetentionService, request.app.state.corpus_retention_service)


def _profiles(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _catalog(request: Request) -> MemoryCatalogPort:
    return cast(MemoryCatalogPort, request.app.state.memory_catalog)


def _consents(request: Request) -> GuardianConsentService:
    return cast(GuardianConsentService, request.app.state.guardian_consent_service)


def _terminator(request: Request) -> AccountSessionTerminator:
    return cast(AccountSessionTerminator, request.app.state.session_terminator)


def _governance(request: Request) -> AccountDataGovernance:
    return cast(AccountDataGovernance, request.app.state.account_data_governance)


def _settings(request: Request) -> ControlSettings:
    return cast(ControlSettings, request.app.state.settings)


def _now() -> datetime:
    return datetime.now(UTC)


def _now_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _idempotency_uuid(namespace: str, idempotency_key: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{namespace}:{idempotency_key}"))


def _require_idempotency_key(value: str | None) -> str:
    normalized = (value or "").strip()
    if not 8 <= len(normalized) <= 128:
        raise HTTPException(
            status_code=400,
            detail={"code": "idempotency_key_required"},
        )
    return normalized


@router.post("/links", status_code=status.HTTP_201_CREATED)
async def create_link(
    body: GuardianLinkCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    key = _require_idempotency_key(idempotency_key)
    if body.minor_user_id == user.user_id:
        raise HTTPException(status_code=422, detail={"code": "guardian_self_link_forbidden"})
    target = _profiles(request).get_subject_profile(user_id=body.minor_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "minor_account_not_found"})
    code = binding_code(
        request,
        guardian_user_id=user.user_id,
        minor_user_id=body.minor_user_id,
        idempotency_key=key,
    )
    link_id = _idempotency_uuid("guardian-link", key)
    now = _now()
    try:
        try:
            link = await _store(request).get_link(
                link_id=link_id,
                actor_user_id=user.user_id,
            )
        except GuardianNotFoundError:
            link = await _store(request).create_link(
                link_id=link_id,
                guardian_user_id=user.user_id,
                minor_user_id=body.minor_user_id,
                relation=body.relation,
                verified_via="wechat_identity",
                binding_code_hash=binding_digest(code),
                binding_expires_at=now + _BINDING_TTL,
                now=now,
            )
        if (
            link.guardian_user_id != user.user_id
            or link.minor_user_id != body.minor_user_id
            or link.relation != body.relation
        ):
            raise GuardianConflictError("idempotency key was reused for another guardian link")
        await record_link_event(
            request,
            link=link,
            event_type="guardian.link_created",
            event_id=f"guardian-link-created:{link.link_id}",
            occurred_at=link.created_at,
        )
    except GuardianConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "guardian_link_conflict"}) from exc
    payload = link_payload(request, link, actor_user_id=user.user_id)
    payload["binding_code"] = code
    return payload


@router.get("/links")
async def list_links(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    links = await _store(request).list_links_for_actor(actor_user_id=user.user_id)
    return {
        "items": [
            link_payload(request, link, actor_user_id=user.user_id) for link in links
        ]
    }


@router.get("/notifications")
async def guardian_notifications(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    notifications = await _notification_store(request).guardian_notifications(
        guardian_user_id=user.user_id,
        limit=limit,
    )
    return {"items": await safety_alert_items(request, user, notifications)}


@router.get("/notifications/unread")
async def unread_guardian_notifications(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    unread = await _notification_store(request).unread_guardian_notifications(
        guardian_user_id=user.user_id,
    )
    if unread is None:
        return {"newest": None, "others_count": 0}
    (newest,) = await safety_alert_items(request, user, (unread.newest,))
    return {"newest": newest, "others_count": unread.others_count}


@router.get("/notifications/{notification_id}")
async def guardian_notification(
    notification_id: uuid.UUID,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    try:
        notification = await _notification_store(request).guardian_notification(
            guardian_user_id=user.user_id,
            notification_id=str(notification_id),
        )
    except GuardianNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "guardian_notification_not_found"}
        ) from exc
    (item,) = await safety_alert_items(request, user, (notification,))
    return item


@router.put("/notifications/{notification_id}/read")
async def mark_guardian_notification_read(
    notification_id: uuid.UUID,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    try:
        read_at = await _notification_store(request).mark_guardian_notification_read(
            guardian_user_id=user.user_id,
            notification_id=str(notification_id),
            now=_now(),
        )
    except GuardianNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "guardian_notification_not_found"}
        ) from exc
    return {"notification_id": str(notification_id), "read_at": read_at.isoformat()}


def _push_store(request: Request) -> PushSubscriptionStorePort:
    return cast(PushSubscriptionStorePort, request.app.state.guardian_store)


@router.get("/push-config")
async def guardian_push_config(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Tell the guardian page whether to offer the crisis subscribe prompt.

    The template id is only disclosed while delivery is enabled, so a
    disabled deployment never prompts.  A guardian without a bound WeChat
    identity cannot receive a subscribe message and is told push is off.
    """

    require_capability_for_subject(user, "guardian_manage", store=_profiles(request))
    settings = _settings(request)
    enabled = settings.guardian_push_enabled and _profiles(request).has_external_identity(
        user_id=user.user_id,
        provider="wechat_openid",
    )
    template_id = settings.wechat_subscribe_crisis_template_id.strip()
    return {
        "enabled": bool(enabled and template_id),
        "template_ids": {"crisis": template_id} if enabled and template_id else {},
    }


@router.get("/push-subscriptions")
async def guardian_push_subscription_status(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Return this guardian's crisis subscribe-message ledger balance.

    ``remaining > 0`` means the settings toggle should read as on.  WeChat
    one-time accepts cannot be revoked client-side; a later ``opt_out``
    zeroes the server balance so we stop sending pushes.
    """

    require_wechat_guardian(request, user)
    settings = _settings(request)
    template_id = settings.wechat_subscribe_crisis_template_id.strip()
    if not template_id:
        return {"template_id": "", "remaining": 0, "last_result": None, "subscribed": False}
    subscription = await _push_store(request).push_subscription(
        guardian_user_id=user.user_id,
        template_id=template_id,
    )
    if subscription is None:
        return {
            "template_id": template_id,
            "remaining": 0,
            "last_result": None,
            "subscribed": False,
        }
    remaining = int(subscription.remaining)
    return {
        "template_id": subscription.template_id,
        "remaining": remaining,
        "last_result": subscription.last_result,
        "subscribed": remaining > 0,
    }


@router.post("/push-subscriptions")
async def record_guardian_push_subscription(
    body: GuardianPushSubscriptionCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Record one ``wx.requestSubscribeMessage`` answer for the guardian.

    An acceptance must carry a fresh ``wx.login`` code: the server exchanges
    it for the openid and only stores it when it matches the guardian's bound
    WeChat identity, so a client can never direct alerts to another user.
    """

    require_wechat_guardian(request, user)
    settings = _settings(request)
    template_id = settings.wechat_subscribe_crisis_template_id.strip()
    if not settings.guardian_push_enabled or not template_id:
        raise HTTPException(status_code=409, detail={"code": "guardian_push_disabled"})
    if not hmac.compare_digest(body.template_id, template_id):
        raise HTTPException(status_code=422, detail={"code": "guardian_push_template_unknown"})
    openid: str | None = None
    if body.result == "accept":
        if not body.login_code:
            raise HTTPException(
                status_code=422,
                detail={"code": "wechat_login_code_required"},
            )
        try:
            session = await code_to_session(settings, body.login_code)
        except WechatAuthError as exc:
            raise HTTPException(status_code=502, detail={"code": exc.code}) from exc
        bound = _profiles(request).external_identity_subject_hash(
            user_id=user.user_id,
            provider="wechat_openid",
        )
        if bound is None or not hmac.compare_digest(bound, openid_hash(session.openid)):
            raise HTTPException(
                status_code=403,
                detail={"code": "guardian_push_identity_mismatch"},
            )
        openid = session.openid
    subscription = await _push_store(request).record_push_subscription(
        guardian_user_id=user.user_id,
        template_id=template_id,
        result=body.result,
        openid=openid,
        now=_now(),
    )
    return {
        "template_id": subscription.template_id,
        "result": subscription.last_result,
        "remaining": subscription.remaining,
    }


@router.post("/links/{link_id}/confirm")
async def confirm_link(
    link_id: str,
    body: GuardianLinkConfirm,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    if body.birth_year_band not in {"under_14", "14_17"}:
        raise HTTPException(status_code=422, detail={"code": "minor_age_band_required"})
    now = _now()
    digest = binding_digest(body.binding_code)
    try:
        current = await _store(request).get_link(
            link_id=link_id,
            actor_user_id=user.user_id,
        )
        if current.minor_user_id != user.user_id:
            raise GuardianAccessDeniedError("only the child account can confirm a link")
        profile = _profiles(request).get_subject_profile(user_id=user.user_id)
        if profile is None:
            raise GuardianNotFoundError("child account not found")
        validate_subject_transition(
            current_category=cast(SubjectCategory, profile.get("subject_category")),
            current_birth_year_band=cast(BirthYearBand, profile.get("birth_year_band")),
            current_age_evidence_status=cast(
                AgeEvidenceStatus,
                profile.get("age_evidence_status"),
            ),
            target_category="minor",
            target_birth_year_band=body.birth_year_band,
            target_age_evidence_status="unverified",
        )
        if current.status == "pending":
            await _store(request).verify_binding_code(
                link_id=link_id,
                minor_user_id=user.user_id,
                binding_code_hash=digest,
                now=now,
            )
            link = await _store(request).confirm_link(
                link_id=link_id,
                minor_user_id=user.user_id,
                binding_code_hash=digest,
                now=now,
            )
        elif current.status == "active":
            link = current
        else:
            raise GuardianAccessDeniedError("guardian link is unavailable")
        updated = _profiles(request).update_subject_profile(
            user_id=user.user_id,
            subject_category="minor",
            birth_year_band=body.birth_year_band,
            now=_now_text(now),
        )
        try:
            await purge_minor_raw_voice(request, user.user_id)
            await disable_minor_restricted_assets(request, user.user_id)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "minor_restricted_asset_cleanup_incomplete"},
            ) from exc
        await _terminator(request).terminate_account(user.user_id)
        await record_link_event(
            request,
            link=link,
            event_type="guardian.link_confirmed",
            event_id=f"guardian-link-confirmed:{link.link_id}",
            occurred_at=link.activated_at or now,
        )
    except GuardianNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "guardian_link_not_found"}) from exc
    except (GuardianAccessDeniedError, SubjectTransitionError, ValueError) as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "guardian_link_confirmation_rejected"},
        ) from exc
    return {
        "link": link_payload(request, link, actor_user_id=user.user_id),
        "subject": {
            "subject_category": updated["subject_category"],
            "birth_year_band": updated["birth_year_band"],
            "subject_revision": updated["subject_revision"],
        },
        "reauthentication_required": True,
    }


@router.post("/links/{link_id}/consents", status_code=status.HTTP_201_CREATED)
async def grant_consent(
    link_id: str,
    body: GuardianConsentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    key = _require_idempotency_key(idempotency_key)
    event_id = f"guardian-consent-grant:{_idempotency_uuid('guardian-consent', key)}"
    now = _now()
    expires_at = (
        now + timedelta(days=body.retention_days)
        if body.retention_days is not None
        else None
    )
    try:
        consent = await _consents(request).grant(
            link_id=link_id,
            guardian_user_id=user.user_id,
            consent_kind=body.consent_kind,
            policy_version=body.policy_version,
            evidence_event_id=event_id,
            consent_id=_idempotency_uuid("guardian-consent-record", key),
            expires_at=expires_at,
            now=now,
        )
    except GuardianNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "guardian_link_not_found"}) from exc
    except GuardianAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail={"code": "guardian_link_required"}) from exc
    except GuardianConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "guardian_consent_conflict"}) from exc
    return consent_payload(consent)


@router.get("/links/{link_id}/consents")
async def list_consents(
    link_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    try:
        records = await _store(request).list_consents(
            link_id=link_id,
            actor_user_id=user.user_id,
        )
    except GuardianNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "guardian_link_not_found"}) from exc
    return {"items": [consent_payload(record) for record in records]}


@router.delete("/links/{link_id}/consents/{consent_id}")
async def revoke_consent(
    link_id: str,
    consent_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    key = _require_idempotency_key(idempotency_key)
    try:
        current = await _store(request).get_consent(
            consent_id=consent_id,
            actor_user_id=user.user_id,
        )
        if current.link_id != link_id:
            raise GuardianNotFoundError("guardian consent not found")
        consent = await _consents(request).revoke(
            consent_id=consent_id,
            guardian_user_id=user.user_id,
            evidence_event_id=(
                f"guardian-consent-revoke:"
                f"{_idempotency_uuid('guardian-consent-revoke', key)}"
            ),
        )
        if consent.consent_kind == "corpus_recording":
            link = await _store(request).get_link(
                link_id=consent.link_id,
                actor_user_id=user.user_id,
            )
            try:
                await _corpus_retention(request).purge_minor(
                    minor_user_id=link.minor_user_id
                )
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "corpus_deletion_incomplete"},
                ) from exc
    except GuardianNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "guardian_consent_not_found"}) from exc
    except GuardianAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail={"code": "guardian_link_required"}) from exc
    except GuardianConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "guardian_consent_conflict"}) from exc
    return consent_payload(consent)


@router.post(
    "/minors/{person_id}/consents",
    status_code=status.HTTP_201_CREATED,
)
async def grant_person_consent(
    person_id: str,
    body: GuardianConsentCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Grant one consent for a subject that has no account of their own.

    The subject was created by an adult's ``parent_for_child`` binding, so no
    guardian link can ever be confirmed for them.  The binding owner is the
    grantor; there is no child confirmation and no manufactured link.  The
    same record is then read by the policy gates through ``active_consent``.
    """

    require_wechat_guardian(request, user)
    key = _require_idempotency_key(idempotency_key)
    await require_binding_owner_for_subject(
        request,
        user=user,
        subject_person_id=person_id,
    )
    consent_id = _idempotency_uuid("guardian-person-consent-record", key)
    try:
        replayed = await _store(request).get_person_consent(
            consent_id=consent_id,
            actor_person_id=user.user_id,
            subject_person_id=person_id,
        )
    except GuardianNotFoundError:
        replayed = None
    if replayed is not None:
        if person_grant_body_matches(replayed, body):
            return person_consent_payload(replayed)
        raise HTTPException(
            status_code=409, detail={"code": "guardian_consent_conflict"}
        )
    now = _now()
    expires_at = (
        now + timedelta(days=body.retention_days)
        if body.retention_days is not None
        else None
    )
    event_id = (
        f"guardian-person-consent-grant:"
        f"{_idempotency_uuid('guardian-person-consent', key)}"
    )
    try:
        consent = await _consents(request).grant_for_person(
            subject_person_id=person_id,
            grantor_person_id=user.user_id,
            consent_kind=body.consent_kind,
            policy_version=body.policy_version,
            evidence_event_id=event_id,
            consent_id=consent_id,
            expires_at=expires_at,
            now=now,
        )
    except GuardianAccessDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": "guardian_binding_owner_required"}
        ) from exc
    except GuardianConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "guardian_consent_conflict"}
        ) from exc
    await sync_bound_subject_authority(
        request,
        user=user,
        subject_person_id=person_id,
        consent_kind=body.consent_kind,
        granted=True,
        source_key=consent.consent_id,
    )
    return person_consent_payload(consent)


@router.get("/minors/{person_id}/consents")
async def list_person_consents(
    person_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    await require_binding_owner_for_subject(
        request,
        user=user,
        subject_person_id=person_id,
    )
    records = await _store(request).list_person_consents(
        subject_person_id=person_id,
        actor_person_id=user.user_id,
    )
    return {"items": [person_consent_payload(record) for record in records]}


@router.delete("/minors/{person_id}/consents/{consent_id}")
async def revoke_person_consent(
    person_id: str,
    consent_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Revoke a person-level consent; only its recorded grantor may.

    The revoke path deliberately stays open after an unbind so the grantor
    always keeps a way to withdraw the authorization it issued.  A manager
    takeover does not transfer a previous grantor's consent: a different
    person cannot read or revoke it, and its policy effect lasts until the
    recorded grantor revokes it or the subject lineage is deleted (P2-03).
    """

    require_wechat_guardian(request, user)
    key = _require_idempotency_key(idempotency_key)
    # Deliberately no ACTIVE-binding prerequisite: unbinding must never strand
    # a consent without a revoke entry.  The person-scoped store still proves
    # that this actor is the recorded grantor for this subject, so a stranger
    # gets a 404 and a different signed-in person cannot revoke someone
    # else's grant.
    try:
        consent = await _consents(request).revoke_for_person(
            consent_id=consent_id,
            grantor_person_id=user.user_id,
            subject_person_id=person_id,
            evidence_event_id=(
                f"guardian-person-consent-revoke:"
                f"{_idempotency_uuid('guardian-person-consent-revoke', key)}"
            ),
        )
    except GuardianNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail={"code": "guardian_consent_not_found"}
        ) from exc
    except GuardianAccessDeniedError as exc:
        raise HTTPException(
            status_code=403, detail={"code": "guardian_binding_owner_required"}
        ) from exc
    except GuardianConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "guardian_consent_conflict"}
        ) from exc
    # Withdraw the authority grants first-class too; with the binding gone
    # the unbind path already revoked them, so this finds nothing to do.
    await sync_bound_subject_authority(
        request,
        user=user,
        subject_person_id=person_id,
        consent_kind=consent.consent_kind,
        granted=False,
        source_key=consent.consent_id,
    )
    return person_consent_payload(consent)


@router.get("/minors/{minor_user_id}/summary")
async def weekly_summary(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    week_start: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    evidence_account_id = await guardian_evidence_account(
        request, user=user, minor_user_id=minor_user_id
    )
    zone = ZoneInfo(_settings(request).memoria_timezone)
    local_today = _now().astimezone(zone).date()
    current_week = local_today - timedelta(days=local_today.weekday())
    start_day = week_start or current_week
    if start_day > current_week or start_day < current_week - timedelta(weeks=52):
        raise HTTPException(status_code=422, detail={"code": "weekly_window_invalid"})
    start_at = datetime.combine(start_day, time.min, tzinfo=zone).astimezone(UTC)
    end_at = (start_at + timedelta(days=7)) - timedelta(microseconds=1)
    events = await _archive(request).evidence_window(
        account_id=evidence_account_id,
        subject_id=minor_user_id,
        occurred_after=start_at,
        occurred_before=end_at,
        event_types=_WEEKLY_EVENT_TYPES,
    )
    report = WeeklyReportProjector.build(
        minor_user_id=minor_user_id,
        week_start=start_day,
        events=events,
        timezone=_settings(request).memoria_timezone,
        evidence_account_id=(
            evidence_account_id if evidence_account_id != minor_user_id else None
        ),
    )
    return report.public_payload()


@router.get("/minors/{minor_user_id}/days")
async def child_days(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: Annotated[int, Query(ge=1, le=90)] = 30,
) -> dict[str, Any]:
    """Which days the child talked, newest first. Counts only: no words, no recap."""

    utterances, _ = await child_turns(request, user=user, minor_user_id=minor_user_id)
    return {"items": day_activity(utterances, limit=limit)}


@router.post("/minors/{minor_user_id}/days/{recap_day}/recap")
async def child_day_recap(
    minor_user_id: str,
    recap_day: date,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """A short recap of one day for the guardian, in the model's words, never the child's.

    Computed per request from the child's own consented turns and not stored, so it can
    outlive neither the child's data nor a per-subject deletion. A recap that repeats a
    stretch of the child's words, or cannot be produced, is a counts-only one.
    """

    utterances, _ = await child_turns(request, user=user, minor_user_id=minor_user_id)
    day_turns = [item for item in utterances if item.day == recap_day]
    if not day_turns:
        raise HTTPException(status_code=404, detail={"code": "no_conversation_that_day"})
    recap, source = await recap_for_day(_settings(request), day=recap_day, utterances=day_turns)
    return {
        "day": recap_day.isoformat(),
        "message_count": len(day_turns),
        "summary": recap,
        "source": source,
        "generated_at": _now().isoformat().replace("+00:00", "Z"),
        "privacy": {"contains_transcript": False},
    }


class GuardianMemoryReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["confirm", "retract"]


class ChildSessionSummaryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=128)
    source_event_id: str = Field(min_length=1, max_length=128)


class ChildArchiveFactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    field: Literal["appellation", "preference", "pet_name"]
    text: str = Field(min_length=1, max_length=40)


class ChildArchiveFactReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    action: Literal["confirm", "correct", "withdraw"]
    text: str | None = Field(default=None, min_length=1, max_length=40)
    field: Literal["appellation", "preference", "pet_name"] | None = None
    polarity: Literal["positive", "negative", "substituted", "unknown"] | None = None
    history_change: bool = False

    @model_validator(mode="after")
    def _correct_needs_patch(self) -> ChildArchiveFactReview:
        if self.action == "correct" and self.text is None and self.field is None and self.polarity is None:
            raise ValueError("correct requires text, field, or polarity")
        return self


def _child_layers(request: Request) -> ChildLayerStore:
    store = getattr(request.app.state, "child_layer_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail={"code": "child_layers_unavailable"})
    return cast(ChildLayerStore, store)


@router.get("/minors/{minor_user_id}/session-summaries")
async def list_session_summaries(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Stored dialogue summaries for the parent mini program (no verbatim quotes)."""

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    layers = _child_layers(request)
    items = await layers.list(
        subject_id=minor_user_id,
        kind=KIND_DIALOGUE_SUMMARY,
        viewer_subject_id=account_id,
        limit=limit,
    )
    return {
        "items": [
            {
                "record_id": item.record_id,
                "text": item.text,
                "session_id": item.session_id,
                "created_at": item.created_at.isoformat(),
                "retention_expires_at": (
                    item.retention_expires_at.isoformat()
                    if item.retention_expires_at
                    else None
                ),
            }
            for item in items
        ],
        "privacy": {"contains_transcript": False},
    }


@router.post("/minors/{minor_user_id}/session-summaries")
async def create_session_summary(
    minor_user_id: str,
    body: ChildSessionSummaryCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Ingest one paraphrased session summary after a conversation ends."""

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    consent = await _store(request).active_consent(
        minor_user_id=minor_user_id, consent_kind="memory_retention"
    )
    if consent is None:
        raise HTTPException(
            status_code=403, detail={"code": "memory_retention_consent_required"}
        )
    consent_snapshot_id = consent.consent_id
    try:
        summary = DialogueSummary(
            subject_id=minor_user_id,
            text=body.text,
            source_event_id=body.source_event_id,
            session_id=body.session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_dialogue_summary"}) from exc
    record = await _child_layers(request).write_summary(
        summary,
        resource_owner_id=account_id,
        actor_id=account_id,
        consent_snapshot_id=consent_snapshot_id,
    )
    return {
        "record_id": record.record_id,
        "text": record.text,
        "session_id": record.session_id,
        "created_at": record.created_at.isoformat(),
        "retention_expires_at": (
            record.retention_expires_at.isoformat() if record.retention_expires_at else None
        ),
    }


@router.delete("/minors/{minor_user_id}/session-summaries/{record_id}")
async def delete_session_summary(
    minor_user_id: str,
    record_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Physically delete one dialogue summary; a re-query must return nothing."""

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    layers = _child_layers(request)
    existing = await layers.get(
        subject_id=minor_user_id, record_id=record_id, viewer_subject_id=account_id
    )
    if existing is None or existing.kind != KIND_DIALOGUE_SUMMARY:
        raise HTTPException(status_code=404, detail={"code": "session_summary_not_found"})
    erased = await layers.delete_record(subject_id=minor_user_id, record_id=record_id)
    again = await layers.get(
        subject_id=minor_user_id, record_id=record_id, viewer_subject_id=account_id
    )
    if again is not None:
        raise HTTPException(status_code=500, detail={"code": "session_summary_delete_incomplete"})
    return {"deleted": True, "record_id": record_id, "erased": dict(erased)}


@router.delete("/minors/{minor_user_id}/session-summaries")
async def clear_session_summaries(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Physically erase every dialogue summary for this child; remaining must be zero."""

    await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    return await erase_child_kind(
        request,
        subject_id=minor_user_id,
        kind=KIND_DIALOGUE_SUMMARY,
        incomplete_code="session_summaries_clear_incomplete",
    )


@router.get("/minors/{minor_user_id}/child-archive")
async def list_child_archive(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    include_withdrawn: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Whitelist long-term archive facts (appellation, preference, pet_name)."""

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    layers = _child_layers(request)
    views = await layers.list_archive_views(
        subject_id=minor_user_id,
        viewer_subject_id=account_id,
        limit=limit,
        include_withdrawn=include_withdrawn,
    )
    return {"items": [view_to_api(view) for view in views]}


@router.post("/minors/{minor_user_id}/child-archive")
async def create_child_archive_fact(
    minor_user_id: str,
    body: ChildArchiveFactCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Write one whitelist archive fact; requires separate child_archive consent."""

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    consent = await _store(request).active_consent(
        minor_user_id=minor_user_id, consent_kind="child_archive"
    )
    if consent is None:
        raise HTTPException(status_code=403, detail={"code": "child_archive_consent_required"})
    try:
        fact = ArchiveFact(subject_id=minor_user_id, field=body.field, text=body.text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_child_archive_fact"}) from exc
    record = await _child_layers(request).write_archive_fact(
        fact,
        resource_owner_id=account_id,
        actor_id=account_id,
        consent_snapshot_id=consent.consent_id,
    )
    return {
        "record_id": record.record_id,
        "field": record.field,
        "text": record.text,
        "created_at": record.created_at.isoformat(),
    }


@router.post("/minors/{minor_user_id}/child-archive/{record_id}/review")
async def review_child_archive_fact(
    minor_user_id: str,
    record_id: str,
    body: ChildArchiveFactReview,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Parent confirm, correct (supersede), or withdraw one archive fact."""

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    consent = await _store(request).active_consent(
        minor_user_id=minor_user_id, consent_kind="child_archive"
    )
    consent_id = consent_reference(consent)
    if consent is None or not consent_id:
        raise HTTPException(status_code=403, detail={"code": "child_archive_consent_required"})
    layers = _child_layers(request)
    if not hasattr(layers, "review_archive_fact"):
        raise HTTPException(status_code=503, detail={"code": "child_archive_review_unavailable"})
    try:
        receipt = await layers.review_archive_fact(
            subject_id=minor_user_id,
            resource_owner_id=account_id,
            consent_snapshot_id=consent_id,
            command=ChildArchiveReviewCommand(
                action=body.action,
                record_id=record_id,
                actor_id=account_id,
                text=body.text,
                field=body.field,
                polarity=body.polarity,
                history_change=body.history_change,
            ),
        )
    except ChildArchiveReviewError as exc:
        code = exc.code
        if code in {"child_archive_not_found"}:
            raise HTTPException(status_code=404, detail={"code": code}) from exc
        if code in {"child_archive_already_withdrawn", "correct_noop", "correct_requires_change"}:
            raise HTTPException(status_code=409, detail={"code": code}) from exc
        raise HTTPException(status_code=422, detail={"code": code}) from exc
    await forget_subject_plans(request.app.state, minor_user_id)
    return {
        "action": receipt.action,
        "record_id": receipt.record_id,
        "status": receipt.status,
        "display_lifecycle": receipt.display_lifecycle,
        "display_label": receipt.display_label,
        "reviewed_at": receipt.reviewed_at.isoformat(),
        "audit_event_id": receipt.audit_event_id,
        "old_record_id": receipt.old_record_id,
        "new_record_id": receipt.new_record_id,
        "old_version": receipt.old_version,
        "new_version": receipt.new_version,
        "auth": "binding_owner",
        "r09_guardian_token": "未验",
    }


@router.delete("/minors/{minor_user_id}/child-archive/{record_id}")
async def delete_child_archive_fact(
    minor_user_id: str,
    record_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    layers = _child_layers(request)
    existing = await layers.get(
        subject_id=minor_user_id, record_id=record_id, viewer_subject_id=account_id
    )
    if existing is None or existing.kind != KIND_CHILD_ARCHIVE:
        raise HTTPException(status_code=404, detail={"code": "child_archive_not_found"})
    erased = await layers.delete_record(subject_id=minor_user_id, record_id=record_id)
    await forget_subject_plans(request.app.state, minor_user_id)
    return {
        "deleted": True,
        "record_id": record_id,
        "erased": dict(erased),
        "auth": "binding_owner",
        "a05_handoff": "new_session_and_cache_proof",
    }


@router.delete("/minors/{minor_user_id}/child-archive")
async def clear_child_archive(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Physically erase every whitelist archive fact for this child; remaining must be zero."""

    await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    return await erase_child_kind(
        request,
        subject_id=minor_user_id,
        kind=KIND_CHILD_ARCHIVE,
        incomplete_code="child_archive_clear_incomplete",
    )


@router.post("/minors/{minor_user_id}/activity")
async def touch_child_activity(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    source: Annotated[Literal["dialogue", "miniprogram"], Query()] = "miniprogram",
) -> dict[str, Any]:
    """Reset the 180-day child_archive idle clock (dialogue or mini program open)."""

    await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    activity = getattr(request.app.state, "subject_activity_store", None)
    if activity is None:
        raise HTTPException(status_code=503, detail={"code": "subject_activity_unavailable"})
    stamped = await activity.touch(subject_person_id=minor_user_id, source=source)
    return {"subject_id": minor_user_id, "last_active_at": stamped.isoformat(), "source": source}


@router.get("/minors/{minor_user_id}/memories")
async def child_memories(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """What the robot may remember about the child: candidates waiting for a guardian, and kept ones.

    A child's candidates have nobody else to confirm them (2026-10-02 round 10: the child's own
    review endpoint only answers the account it is signed in as).  Only confirmed memories ever
    reach the child's conversations, so this is the guardian's gate on what the robot recalls.
    """

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    catalog = _catalog(request)
    queue = await catalog.review_queue(account_id=account_id, subject_id=minor_user_id)
    kept = await catalog.search(
        MemorySearchQuery(
            account_id=account_id,
            subject_id=minor_user_id,
            speaker_class="owner",
            kinds=("claim",),
            include_candidates=False,
            limit=limit,
        )
    )
    return {
        "candidates": [
            {
                "claim_id": item.item_id,
                "value": item.value,
                "status": item.status,
                "reason": item.reason,
                "domain_category": item.domain_category,
            }
            for item in queue[:limit]
        ],
        "confirmed": [
            {
                "memory_id": item.item_id,
                "title": item.title,
                "snippet": item.snippet,
                "status": item.status,
                "domain_category": item.domain_category,
                "occurred_at": item.occurred_at.isoformat(),
            }
            for item in kept.items
        ],
    }


@router.get("/minors/{minor_user_id}/memories/retracted")
async def child_retracted_memories(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Memories this child's guardian retracted, newest first, still restorable.

    Corrections are not retractions and stay out of this list. The review
    endpoint already puts a confirmed claim's vector back, so the page needs
    the claim ids across page loads: the list is one query on the child's own
    retracted claims, not a cached page in the mini program.
    """

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    items = await list_retracted_claims(
        _catalog(request),
        account_id=account_id,
        subject_id=minor_user_id,
        limit=limit,
    )
    return {
        "subject_id": minor_user_id,
        "items": [
            {
                "claim_id": item.claim_id,
                "title": item.title,
                "value": item.value,
                "domain_category": item.domain_category,
                "retracted_at": item.retracted_at.isoformat(),
            }
            for item in items
        ],
        "privacy": {"contains_transcript": False},
    }


@router.post("/minors/{minor_user_id}/memories/{claim_id}/review")
async def review_child_memory(
    minor_user_id: str,
    claim_id: str,
    body: GuardianMemoryReview,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    """Confirm a candidate (the robot may then recall it) or retract one (it is forgotten).

    The claim must belong to this child's own evidence: another subject's claim answers the same
    not-found as an id that does not exist.
    """

    account_id = await child_memory_account(request, user=user, minor_user_id=minor_user_id)
    try:
        reviewed = await _catalog(request).review(
            MemoryClaimReview(
                account_id=account_id,
                subject_id=minor_user_id,
                claim_id=claim_id,
                action=body.action,
            )
        )
    except EvidenceNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "memory_claim_not_found"}) from exc
    return {
        "claim_id": reviewed.claim_id,
        "status": reviewed.status,
        "value": reviewed.value,
        "review_event_id": reviewed.review_event_id,
        "reviewed_at": _now_text(_now()),
    }


@router.post("/minors/{minor_user_id}/export")
async def export_minor(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    require_wechat_guardian(request, user)
    if await owns_accountless_child(request, user=user, subject_person_id=minor_user_id):
        # A child with no account: the device stored their turns under the
        # binding owner's account, attributed to the child. Export exactly
        # the rows that prove that attribution, in the guardian audience.
        exported = await _governance(request).export_account(
            user.user_id, subject_id=minor_user_id, audience="guardian"
        )
        return JSONResponse(
            content=exported,
            headers={
                "Content-Disposition": 'attachment; filename="memoria-child-export.json"',
                "Cache-Control": "no-store",
            },
        )
    await active_link_or_403(
        request,
        guardian_user_id=user.user_id,
        minor_user_id=minor_user_id,
    )
    require_capability_for_account_id(
        minor_user_id, "guardian_weekly_report", store=_profiles(request)
    )
    # An active relationship authorizes governance metadata, never verbatim
    # conversation. This export is deliberately narrower than self access.
    exported = await _governance(request).export_account(
        minor_user_id, subject_id=minor_user_id, audience="guardian"
    )
    return JSONResponse(
        content=exported,
        headers={
            "Content-Disposition": 'attachment; filename="memoria-child-export.json"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/minors/{minor_user_id}/delete")
async def delete_minor(
    minor_user_id: str,
    body: GuardianDeleteCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_wechat_guardian(request, user)
    if await owns_accountless_child(request, user=user, subject_person_id=minor_user_id):
        # Their rows live in the binding owner's account: erase exactly the
        # child's, never the parent's (account deletion is account-wide).
        if not deletion_confirmed(body.confirmation):
            raise HTTPException(
                status_code=422, detail={"code": "deletion_confirmation_invalid"}
            )
        deletion = cast(
            SubjectDeletionService | None,
            getattr(request.app.state, "subject_deletion", None),
        )
        if deletion is None:
            raise HTTPException(
                status_code=503, detail={"code": "child_subject_deletion_unavailable"}
            )
        # Cached response plans may hold the child's private context.
        await forget_subject_plans(request.app.state, minor_user_id)
        try:
            return await deletion.delete_subject(
                SubjectScope(account_id=user.user_id, subject_id=minor_user_id)
            )
        except SubjectDeletionIncompleteError as exc:
            # Checkpointed: the deletion worker resumes it.
            raise HTTPException(
                status_code=503, detail={"code": "child_data_deletion_incomplete"}
            ) from exc
    await active_link_or_403(
        request,
        guardian_user_id=user.user_id,
        minor_user_id=minor_user_id,
    )
    if not deletion_confirmed(body.confirmation):
        raise HTTPException(status_code=422, detail={"code": "deletion_confirmation_invalid"})
    try:
        return await _governance(request).delete_account(minor_user_id)
    except AccountDeletionIncompleteError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "child_data_deletion_incomplete"},
        ) from exc


@router.get("/consent-kinds", include_in_schema=False)
async def consent_kinds() -> dict[str, tuple[ConsentKind, ...]]:
    return {"items": _CONSENT_KINDS}
