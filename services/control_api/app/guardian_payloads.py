"""Build the guardian API payloads and the link-scoped support they read.

The guardian router returns link and consent records, derives the per-binding
confirmation code, resolves the active link a request acts on, and records link
lifecycle events in the archive. Those shapes and lookups live here so the
router reads as endpoints plus the request-scope accessors they call.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from typing import Any, cast

from fastapi import HTTPException, Request

from services.archive.domain import EvidenceEvent, LifeArchivePort
from services.control_api.app.account_gate import require_capability_for_subject
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import AuthenticatedUser
from services.guardian.domain import (
    ConsentRecord,
    GuardianLink,
    GuardianStorePort,
    PersonConsentRecord,
)

__all__ = [
    "active_link_or_403",
    "binding_code",
    "binding_digest",
    "consent_payload",
    "link_payload",
    "person_consent_payload",
    "record_link_event",
    "require_wechat_guardian",
]



def _store(request: Request) -> GuardianStorePort:
    return cast(GuardianStorePort, request.app.state.guardian_store)


def _profiles(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _settings(request: Request) -> ControlSettings:
    return cast(ControlSettings, request.app.state.settings)


def binding_code(
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


def binding_digest(code: str) -> str:
    return hashlib.sha256(f"memoria:guardian-binding-v1\0{code}".encode()).hexdigest()


def require_wechat_guardian(request: Request, user: AuthenticatedUser) -> None:
    require_capability_for_subject(user, "guardian_manage", store=_profiles(request))
    if not _profiles(request).has_external_identity(
        user_id=user.user_id,
        provider="wechat_openid",
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "guardian_wechat_identity_required"},
        )


def link_payload(request: Request, link: GuardianLink, *, actor_user_id: str) -> dict[str, Any]:
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


def consent_payload(record: ConsentRecord) -> dict[str, Any]:
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


def person_consent_payload(record: PersonConsentRecord) -> dict[str, Any]:
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


async def active_link_or_403(
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


async def record_link_event(
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
