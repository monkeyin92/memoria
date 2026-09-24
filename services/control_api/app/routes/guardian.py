"""Guardian binding, consent, child-data, and transcript-free summary routes."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from packages.contracts.generated.python.multi_subject_contracts import CapabilityValue
from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.archive.domain import (
    EvidenceEvent,
    LifeArchivePort,
    RawVoiceConsentRequiredError,
)
from services.archive.object_store import ObjectStore
from services.consent.bound_subject import (
    MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectConsentService,
    BoundSubjectGrant,
)
from services.control_api.app.account_gate import (
    require_capability_for_account_id,
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.guardian_push import resolve_minor_display_name
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
from services.guardian.consent import GuardianConsentService
from services.guardian.corpus import CorpusRetentionService
from services.guardian.crisis import CrisisNotificationStorePort
from services.guardian.domain import (
    AgeEvidenceStatus,
    BirthYearBand,
    ConsentKind,
    ConsentRecord,
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianLink,
    GuardianNotFoundError,
    GuardianStorePort,
    PersonConsentRecord,
    Relation,
    SubjectCategory,
    SubjectTransitionError,
    validate_subject_transition,
)
from services.guardian.push import PushSubscriptionResult, PushSubscriptionStorePort
from services.guardian.weekly_report import WeeklyReportProjector
from services.identity.service import IdentityService
from services.legacy.domain import LegacyRegistryPort
from services.speaker.domain import RevokeSpeakerProfile, SpeakerAuthorityPort
from services.voice_profile.domain import VoiceProfilePort

router = APIRouter(prefix="/v1/guardian", tags=["guardian"])
_BINDING_TTL = timedelta(minutes=15)
_CONSENT_KINDS: tuple[ConsentKind, ...] = (
    "minor_voice_session",
    "memory_retention",
    "weekly_report",
    "corpus_recording",
)
_WEEKLY_EVENT_TYPES = (
    "emotion_observation",
    "tutor.practice_completed",
    "study.progress_updated",
    "topic.observation",
)
_DELETE_CONFIRMATION = "永久删除孩子的全部数据"


class GuardianLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    minor_user_id: str = Field(min_length=1, max_length=128)
    relation: Relation = "parent"


class GuardianLinkConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    binding_code: str = Field(pattern=r"^\d{8}$")
    birth_year_band: BirthYearBand


class GuardianConsentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    consent_kind: ConsentKind
    policy_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    retention_days: int | None = Field(default=None, ge=1, le=30)

    @model_validator(mode="after")
    def validate_corpus_retention(self) -> GuardianConsentCreate:
        if self.consent_kind == "corpus_recording" and self.retention_days is None:
            raise ValueError("corpus recording consent requires retention_days")
        if self.consent_kind != "corpus_recording" and self.retention_days is not None:
            raise ValueError("retention_days is only valid for corpus recording consent")
        return self


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


def _archive_objects(request: Request) -> ObjectStore:
    return cast(ObjectStore, request.app.state.archive_object_store)


def _consents(request: Request) -> GuardianConsentService:
    return cast(GuardianConsentService, request.app.state.guardian_consent_service)


def _terminator(request: Request) -> AccountSessionTerminator:
    return cast(AccountSessionTerminator, request.app.state.session_terminator)


def _governance(request: Request) -> AccountDataGovernance:
    return cast(AccountDataGovernance, request.app.state.account_data_governance)


async def _disable_minor_restricted_assets(request: Request, account_id: str) -> None:
    now = _now()
    legacy = cast(LegacyRegistryPort, request.app.state.legacy_registry)
    for grant in await legacy.list_grants(actor_account_id=account_id, role="owner"):
        if grant.revoked_at is None:
            await legacy.revoke(
                actor_account_id=account_id,
                grant_id=grant.grant_id,
                expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,
                idempotency_key=f"minor-transition:{grant.grant_id}",
                now=now,
            )
    voice = cast(VoiceProfilePort, request.app.state.voice_profile_manager)
    for voice_profile in await voice.profiles(account_id=account_id):
        if voice_profile.status != "revoked":
            await voice.revoke_profile(
                account_id=account_id,
                profile_id=voice_profile.profile_id,
            )
    consent = await voice.consent(account_id=account_id)
    if consent is not None and consent.revoked_at is None:
        await voice.revoke_consent(account_id=account_id)
    speaker = cast(SpeakerAuthorityPort, request.app.state.speaker_authority)
    for speaker_profile in await speaker.profiles(account_id):
        if speaker_profile.status != "revoked":
            await speaker.revoke(
                RevokeSpeakerProfile(
                    account_id=account_id,
                    profile_id=speaker_profile.profile_id,
                    reason="subject_transition_to_minor",
                )
            )


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


def _binding_code(
    request: Request,
    *,
    guardian_user_id: str,
    minor_user_id: str,
    idempotency_key: str,
) -> str:
    secret = _settings(request).memoria_auth_secret.get_secret_value().encode("utf-8")
    digest = hmac.new(
        secret,
        (
            "memoria:guardian-binding-v1\0"
            f"{guardian_user_id}\0{minor_user_id}\0{idempotency_key}"
        ).encode(),
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 100_000_000:08d}"


def _binding_digest(code: str) -> str:
    return hashlib.sha256(f"memoria:guardian-binding-v1\0{code}".encode()).hexdigest()


def _require_wechat_guardian(request: Request, user: AuthenticatedUser) -> None:
    require_capability_for_subject(user, "guardian_manage", store=_profiles(request))
    if not _profiles(request).has_external_identity(
        user_id=user.user_id,
        provider="wechat_openid",
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "guardian_wechat_identity_required"},
        )


def _link_payload(request: Request, link: GuardianLink, *, actor_user_id: str) -> dict[str, Any]:
    minor = _profiles(request).get_subject_profile(user_id=link.minor_user_id) or {}
    return {
        "link_id": link.link_id,
        "guardian_user_id": link.guardian_user_id,
        "minor_user_id": link.minor_user_id,
        "minor_display_name": str(minor.get("display_name") or "孩子"),
        "relation": link.relation,
        "status": link.status,
        "verified_via": link.verified_via,
        "actor_role": "guardian" if actor_user_id == link.guardian_user_id else "minor",
        "binding_expires_at": link.binding_expires_at.isoformat(),
        "created_at": link.created_at.isoformat(),
        "activated_at": link.activated_at.isoformat() if link.activated_at else None,
        "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
    }


def _consent_payload(record: ConsentRecord) -> dict[str, Any]:
    return {
        "consent_id": record.consent_id,
        "link_id": record.link_id,
        "consent_kind": record.consent_kind,
        "policy_version": record.policy_version,
        "granted_at": record.granted_at.isoformat(),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "active": record.active,
    }


def _person_consent_payload(record: PersonConsentRecord) -> dict[str, Any]:
    return {
        "consent_id": record.consent_id,
        "subject_person_id": record.subject_person_id,
        "grantor_person_id": record.grantor_person_id,
        "consent_kind": record.consent_kind,
        "policy_version": record.policy_version,
        "granted_at": record.granted_at.isoformat(),
        "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None,
        "active": record.active,
        "basis": "binding_owner_declaration",
    }


def _person_grant_body_matches(
    record: PersonConsentRecord,
    body: GuardianConsentCreate,
) -> bool:
    """Whether a replayed idempotency key carries the same grant request.

    Absolute timestamps are recomputed per attempt, so the request is matched
    on the grantor/subject/kind/policy identity plus the requested retention
    span; a different payload under the same key conflicts.
    """

    if (
        record.consent_kind != body.consent_kind
        or record.policy_version != body.policy_version
    ):
        return False
    if body.retention_days is None:
        return record.expires_at is None
    return (
        record.expires_at is not None
        and record.expires_at - record.granted_at
        == timedelta(days=body.retention_days)
    )


async def _require_binding_owner_for_subject(
    request: Request,
    *,
    user: AuthenticatedUser,
    subject_person_id: str,
) -> None:
    """Authorize a person-level consent against the device binding.

    The only accepted grantor is the account owner of an ACTIVE
    ``parent_for_child`` binding that names this subject as its primary
    subject — the same binding-scoped authority that makes a guardian
    declaration count.  A subject without such a binding is refused: nobody
    may grant capabilities over a child they do not actually manage.
    """

    identity = getattr(request.app.state, "identity_service", None)
    if not isinstance(identity, IdentityService):
        raise HTTPException(
            status_code=503,
            detail={"code": "identity_authority_unavailable"},
        )
    manifests = await identity.list_active_manifests_for_person(
        subject_person_id,
        actor_person_id=user.user_id,
    )
    if not any(
        manifest.declared_mode == "parent_for_child"
        and manifest.account_owner_id == user.user_id
        and subject_person_id in manifest.primary_subject_ids
        for manifest in manifests
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "guardian_binding_owner_required"},
        )


#: Guardian consent kinds that stand for standing grants in the consent
#: authority Policy reads (see ``services.consent.bound_subject``).
_BOUND_SUBJECT_CAPABILITIES: dict[str, tuple[CapabilityValue, ...]] = {
    "memory_retention": MEMORY_CAPABILITIES,
    "minor_voice_session": MINOR_SESSION_CAPABILITIES,
}


async def _sync_bound_subject_authority(
    request: Request,
    *,
    user: AuthenticatedUser,
    subject_person_id: str,
    consent_kind: str,
    granted: bool,
    source_key: str,
) -> None:
    """Mirror a person-level guardian consent into the consent authority.

    The retention ceiling reads the Guardian ledger and Policy reads the
    consent authority; one toggle must move both, on every active binding
    this guardian owns for the child. Without a consent authority (SQLite
    deployments have no Session Runtime) there is nothing to mirror.
    """

    capabilities = _BOUND_SUBJECT_CAPABILITIES.get(consent_kind)
    service = cast(
        BoundSubjectConsentService | None,
        getattr(request.app.state, "bound_subject_consent", None),
    )
    identity = getattr(request.app.state, "identity_service", None)
    if capabilities is None or service is None or not isinstance(identity, IdentityService):
        return
    manifests = await identity.list_active_manifests_for_person(
        subject_person_id,
        actor_person_id=user.user_id,
    )
    for manifest in manifests:
        if (
            manifest.declared_mode != "parent_for_child"
            or manifest.account_owner_id != user.user_id
            or subject_person_id not in manifest.primary_subject_ids
        ):
            continue
        if granted:
            await service.grant(
                BoundSubjectGrant(
                    actor_person_id=user.user_id,
                    subject_person_id=subject_person_id,
                    binding_id=manifest.binding_id,
                    kind="guardian",
                    capabilities=capabilities,
                    source_key=source_key,
                )
            )
        else:
            await service.revoke(
                actor_person_id=user.user_id,
                subject_person_id=subject_person_id,
                binding_id=manifest.binding_id,
                capabilities=capabilities,
                reason=f"guardian_{consent_kind}_revoked",
            )


def _deletion_confirmed(confirmation: str) -> bool:
    # compare_digest rejects non-ASCII str, and the phrase is Chinese.
    return secrets.compare_digest(
        confirmation.encode("utf-8"), _DELETE_CONFIRMATION.encode("utf-8")
    )


async def _owns_accountless_child(
    request: Request,
    *,
    user: AuthenticatedUser,
    subject_person_id: str,
) -> bool:
    """Whether this guardian binds a device for this child, who has no account."""

    if _profiles(request).get_account(user_id=subject_person_id) is not None:
        return False
    try:
        await _require_binding_owner_for_subject(
            request, user=user, subject_person_id=subject_person_id
        )
    except HTTPException:
        return False
    return True


async def _active_link_or_403(
    request: Request,
    *,
    guardian_user_id: str,
    minor_user_id: str,
) -> GuardianLink:
    link = await _store(request).active_link(
        guardian_user_id=guardian_user_id,
        minor_user_id=minor_user_id,
    )
    if link is None:
        raise HTTPException(status_code=403, detail={"code": "guardian_link_required"})
    return link


async def _record_link_event(
    request: Request,
    *,
    link: GuardianLink,
    event_type: str,
    event_id: str,
    occurred_at: datetime,
) -> None:
    await _archive(request).record(
        EvidenceEvent(
            event_id=event_id,
            account_id=link.minor_user_id,
            event_type=event_type,
            occurred_at=occurred_at,
            speaker_class="system",
            source="guardian.link",
            payload={
                "link_id": link.link_id,
                "relation": link.relation,
                "verified_via": link.verified_via,
                "status": link.status,
            },
        )
    )


async def _purge_minor_raw_voice(request: Request, account_id: str) -> None:
    archive = _archive(request)
    references = await archive.raw_voice_blobs(account_id=account_id)
    active = await archive.active_raw_voice_consent(account_id=account_id)
    if active is not None:
        try:
            revocation = await archive.revoke_raw_voice_consent(
                account_id=account_id,
                revoked_at=_now(),
            )
            references = tuple({item.object_key: item for item in (*references, *revocation.references)}.values())
        except RawVoiceConsentRequiredError:
            pass
    if not references:
        return
    for reference in references:
        await _archive_objects(request).delete(reference)
    await archive.purge_raw_voice_blobs(
        account_id=account_id,
        object_keys=tuple(reference.object_key for reference in references),
    )


@router.post("/links", status_code=status.HTTP_201_CREATED)
async def create_link(
    body: GuardianLinkCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    _require_wechat_guardian(request, user)
    key = _require_idempotency_key(idempotency_key)
    if body.minor_user_id == user.user_id:
        raise HTTPException(status_code=422, detail={"code": "guardian_self_link_forbidden"})
    target = _profiles(request).get_subject_profile(user_id=body.minor_user_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "minor_account_not_found"})
    code = _binding_code(
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
                binding_code_hash=_binding_digest(code),
                binding_expires_at=now + _BINDING_TTL,
                now=now,
            )
        if (
            link.guardian_user_id != user.user_id
            or link.minor_user_id != body.minor_user_id
            or link.relation != body.relation
        ):
            raise GuardianConflictError("idempotency key was reused for another guardian link")
        await _record_link_event(
            request,
            link=link,
            event_type="guardian.link_created",
            event_id=f"guardian-link-created:{link.link_id}",
            occurred_at=link.created_at,
        )
    except GuardianConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": "guardian_link_conflict"}) from exc
    payload = _link_payload(request, link, actor_user_id=user.user_id)
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
            _link_payload(request, link, actor_user_id=user.user_id) for link in links
        ]
    }


@router.get("/notifications")
async def guardian_notifications(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    _require_wechat_guardian(request, user)
    notifications = await _notification_store(request).guardian_notifications(
        guardian_user_id=user.user_id,
        limit=limit,
    )
    display_name = resolve_minor_display_name(
        _profiles(request),
        getattr(request.app.state, "identity_service", None),
    )
    items = []
    for item in notifications:
        minor_display_name = await display_name(user.user_id, item.minor_user_id)
        items.append(
            {
                "notification_id": item.notification_id,
                "minor_user_id": item.minor_user_id,
                "minor_display_name": str(minor_display_name or "孩子"),
                "occurred_at": item.created_at.isoformat(),
                "delivery_status": item.status,
                "channel": item.channel,
                "message": "孩子此刻可能需要可信任的大人陪伴，请尽快联系并确认安全；紧急时联系当地急救或报警。",
                "contains_transcript": False,
                "contains_severity": False,
            }
        )
    return {"items": items}


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

    _require_wechat_guardian(request, user)
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
    digest = _binding_digest(body.binding_code)
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
            await _purge_minor_raw_voice(request, user.user_id)
            await _disable_minor_restricted_assets(request, user.user_id)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "minor_restricted_asset_cleanup_incomplete"},
            ) from exc
        await _terminator(request).terminate_account(user.user_id)
        await _record_link_event(
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
        "link": _link_payload(request, link, actor_user_id=user.user_id),
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
    _require_wechat_guardian(request, user)
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
    return _consent_payload(consent)


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
    return {"items": [_consent_payload(record) for record in records]}


@router.delete("/links/{link_id}/consents/{consent_id}")
async def revoke_consent(
    link_id: str,
    consent_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    _require_wechat_guardian(request, user)
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
    return _consent_payload(consent)


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

    _require_wechat_guardian(request, user)
    key = _require_idempotency_key(idempotency_key)
    await _require_binding_owner_for_subject(
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
        if _person_grant_body_matches(replayed, body):
            return _person_consent_payload(replayed)
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
    await _sync_bound_subject_authority(
        request,
        user=user,
        subject_person_id=person_id,
        consent_kind=body.consent_kind,
        granted=True,
        source_key=consent.consent_id,
    )
    return _person_consent_payload(consent)


@router.get("/minors/{person_id}/consents")
async def list_person_consents(
    person_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _require_wechat_guardian(request, user)
    await _require_binding_owner_for_subject(
        request,
        user=user,
        subject_person_id=person_id,
    )
    records = await _store(request).list_person_consents(
        subject_person_id=person_id,
        actor_person_id=user.user_id,
    )
    return {"items": [_person_consent_payload(record) for record in records]}


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

    _require_wechat_guardian(request, user)
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
    await _sync_bound_subject_authority(
        request,
        user=user,
        subject_person_id=person_id,
        consent_kind=consent.consent_kind,
        granted=False,
        source_key=consent.consent_id,
    )
    return _person_consent_payload(consent)


@router.get("/minors/{minor_user_id}/summary")
async def weekly_summary(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    week_start: Annotated[date | None, Query()] = None,
) -> dict[str, Any]:
    _require_wechat_guardian(request, user)
    await _active_link_or_403(
        request,
        guardian_user_id=user.user_id,
        minor_user_id=minor_user_id,
    )
    require_capability_for_account_id(
        minor_user_id,
        "guardian_weekly_report",
        store=_profiles(request),
    )
    consent = await _store(request).active_consent(
        minor_user_id=minor_user_id,
        consent_kind="weekly_report",
    )
    if consent is None:
        raise HTTPException(status_code=403, detail={"code": "guardian_consent_required"})
    zone = ZoneInfo(_settings(request).memoria_timezone)
    local_today = _now().astimezone(zone).date()
    current_week = local_today - timedelta(days=local_today.weekday())
    start_day = week_start or current_week
    if start_day > current_week or start_day < current_week - timedelta(weeks=52):
        raise HTTPException(status_code=422, detail={"code": "weekly_window_invalid"})
    start_at = datetime.combine(start_day, time.min, tzinfo=zone).astimezone(UTC)
    end_at = (start_at + timedelta(days=7)) - timedelta(microseconds=1)
    events = await _archive(request).evidence_window(
        account_id=minor_user_id,
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
    )
    return report.public_payload()


@router.post("/minors/{minor_user_id}/export")
async def export_minor(
    minor_user_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> JSONResponse:
    _require_wechat_guardian(request, user)
    if await _owns_accountless_child(request, user=user, subject_person_id=minor_user_id):
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
    await _active_link_or_403(
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
    _require_wechat_guardian(request, user)
    if await _owns_accountless_child(request, user=user, subject_person_id=minor_user_id):
        # Their rows live in the binding owner's account: erase exactly the
        # child's, never the parent's (account deletion is account-wide).
        if not _deletion_confirmed(body.confirmation):
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
        try:
            return await deletion.delete_subject(
                SubjectScope(account_id=user.user_id, subject_id=minor_user_id)
            )
        except SubjectDeletionIncompleteError as exc:
            # Checkpointed: the deletion worker resumes it.
            raise HTTPException(
                status_code=503, detail={"code": "child_data_deletion_incomplete"}
            ) from exc
    await _active_link_or_403(
        request,
        guardian_user_id=user.user_id,
        minor_user_id=minor_user_id,
    )
    if not _deletion_confirmed(body.confirmation):
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
