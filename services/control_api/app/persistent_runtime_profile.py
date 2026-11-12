"""Read and cross-check the signed persistent Runtime Profile.

Both the interaction and archive routers make private writes only after this
guard accepts the same signed profile and session context. The guard lives in
its own module so neither router owns the other's authority check.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException, Request

from services.common.failure_log import failure_log
from services.consent.bound_subject import MINOR_SESSION_CAPABILITIES
from services.session_runtime.errors import PersistentSessionDenied, PersistentSessionTerminated
from services.session_runtime.issue_authority import ProfileConsentWithdrawn
from services.session_runtime.postgres_store import SessionRuntimeConflict
from services.session_runtime.service import (
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    PostgresSessionRuntimeService,
)

logger = logging.getLogger(__name__)


async def current_persistent_runtime_profile(
    request: Request,
    session: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    """Read and cross-check the signed profile from the persistent authority."""

    runtime_service = cast(
        PostgresSessionRuntimeService | None,
        getattr(request.app.state, "session_runtime_service", None),
    )
    if runtime_service is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        )
    actor_id = session.get("user_id")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_context_unavailable"},
        )
    try:
        profile, context = await runtime_service.current(
            actor_id=actor_id,
            session_id=session_id,
            now=datetime.now(UTC),
        )
    except ProfileConsentWithdrawn as exc:
        if set(exc.withdrawn) & set(MINOR_SESSION_CAPABILITIES):
            try:
                await runtime_service.close_session(actor_id=actor_id, session_id=session_id, reason_code="consent_withdrawn", now=datetime.now(UTC))
            except (PersistentSessionUnavailable, PersistentSessionDenied, PersistentSessionNotFound, SessionRuntimeConflict):
                logger.warning("consent withdrawn; session authority close remains pending session_id=%s", session_id)
            raise HTTPException(status_code=410, detail={"code": "session_terminated", "reason": "consent_withdrawn"}) from exc
        raise HTTPException(status_code=503, detail={"code": "session_profile_consent_changed"}) from exc
    except PersistentSessionTerminated as exc:
        raise HTTPException(status_code=410, detail={"code": "session_terminated", "reason": "binding_revoked"}) from exc
    except PersistentSessionNotFound as exc:
        state = await runtime_service.session_state(actor_id=actor_id, session_id=session_id)
        if state in {"closed", "failed"}:
            raise HTTPException(status_code=410, detail={"code": "session_terminated", "reason": state}) from exc
        raise HTTPException(status_code=503, detail={"code": "session_runtime_authority_unavailable"}) from exc
    except PersistentSessionUnavailable as exc:
        logger.warning(
            "persistent Runtime Profile unavailable session_id=%s %s", session_id, failure_log(exc)
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    except Exception as exc:
        logger.exception(
            "persistent Runtime Profile read failed session_id=%s",
            session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc

    expected = {
        "session_id": session_id,
        "actor_id": actor_id,
        "device_id": context.device_id,
        "binding_id": context.binding_id,
        "binding_version": context.binding_version,
        "active_subject_id": context.active_subject_id,
        "subject_revision": context.subject_revision,
        "session_epoch": context.session_epoch,
        "runtime_profile_id": context.current_runtime_profile_id,
    }
    actual = {
        "session_id": getattr(profile, "session_id", None),
        "actor_id": getattr(profile, "actor_id", None),
        "device_id": getattr(profile, "device_id", None),
        "binding_id": getattr(profile, "binding_id", None),
        "binding_version": getattr(profile, "binding_version", None),
        "active_subject_id": getattr(profile, "active_subject_id", None),
        "subject_revision": getattr(profile, "subject_revision", None),
        "session_epoch": getattr(profile, "session_epoch", None),
        "runtime_profile_id": getattr(profile, "runtime_profile_id", None),
    }
    if actual != expected:
        logger.error(
            "persistent Runtime Profile/context mismatch session_id=%s expected=%s actual=%s",
            session_id,
            expected,
            actual,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_profile_binding_mismatch"},
        )
    now = datetime.now(UTC)
    issued_at = getattr(profile, "issued_at", None)
    expires_at = getattr(profile, "expires_at", None)
    if (
        not isinstance(issued_at, datetime)
        or not isinstance(expires_at, datetime)
        or issued_at.tzinfo is None
        or expires_at.tzinfo is None
        or now < issued_at
        or now >= expires_at
    ):
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_profile_expired"},
        )
    active_subject_id = getattr(profile, "active_subject_id", None)
    speaker_state = getattr(profile, "speaker_state", None)
    subject_category = getattr(profile, "subject_category", None)
    service_mode = getattr(profile, "service_mode", None)
    confirmed_subject = (
        isinstance(active_subject_id, str)
        and bool(active_subject_id.strip())
        and speaker_state == "confirmed"
        and subject_category != "unknown"
    )
    unknown_safe_subject = (
        active_subject_id is None
        and speaker_state == "unconfirmed"
        and subject_category == "unknown"
        and service_mode == "unknown_safe"
    )
    if not confirmed_subject and not unknown_safe_subject:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_subject_unavailable"},
        )
    try:
        payload = profile.model_dump(mode="json")
    except (AttributeError, TypeError, ValueError):
        logger.exception(
            "persistent Runtime Profile serialization failed session_id=%s",
            session_id,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_profile_unavailable"},
        ) from None
    signature = getattr(profile, "signature", None)
    if not isinstance(payload, dict) or not isinstance(signature, str) or not signature:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_profile_unavailable"},
        )
    payload["signature"] = signature
    return payload
