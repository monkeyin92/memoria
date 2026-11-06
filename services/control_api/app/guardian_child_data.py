"""Resolve the account that holds a child's data for a guardian request.

A guardian's views of a child's turns, memories and reports are served from the
account that actually holds them: the binding owner's account for a device-bound
child, the child's own account otherwise. These resolvers prove the guardian's
access and return that account, so the guardian router keeps the endpoints.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request

from services.archive.domain import LifeArchivePort
from services.control_api.app.account_gate import require_capability_for_account_id
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.guardian_binding_authority import owns_accountless_child
from services.control_api.app.guardian_payloads import (
    active_link_or_403,
    require_wechat_guardian,
)
from services.control_api.app.guardian_recap import child_utterances
from services.control_api.app.security import AuthenticatedUser
from services.guardian.domain import GuardianStorePort

__all__ = [
    "child_memory_account",
    "child_turns",
    "consent_reference",
    "guardian_evidence_account",
]



def _store(request: Request) -> GuardianStorePort:
    return cast(GuardianStorePort, request.app.state.guardian_store)


def _profiles(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _settings(request: Request) -> ControlSettings:
    return cast(ControlSettings, request.app.state.settings)


def _archive(request: Request) -> LifeArchivePort:
    return cast(LifeArchivePort, request.app.state.life_archive)


def _now() -> datetime:
    return datetime.now(UTC)


_RECAP_LOOKBACK_DAYS = 90


async def guardian_evidence_account(
    request: Request, *, user: AuthenticatedUser, minor_user_id: str
) -> str:
    """Whose account holds this child's evidence, once the guardian's access is proven.

    A one-to-one device stores the child's turns in the binding owner's account,
    attributed to the child; the guardian's views come with the long-term memory the
    owner ticked for the child (user decision 2026-09-26): switching that off closes
    them too. A child with an account of their own needs an active guardian link and
    the weekly-report consent instead.
    """

    evidence_account_id = minor_user_id
    if await owns_accountless_child(request, user=user, subject_person_id=minor_user_id):
        consent = await _store(request).active_consent(
            minor_user_id=minor_user_id,
            consent_kind="memory_retention",
        )
        evidence_account_id = user.user_id
    else:
        await active_link_or_403(
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
    return evidence_account_id


async def child_turns(
    request: Request, *, user: AuthenticatedUser, minor_user_id: str
) -> tuple[list[Any], ZoneInfo]:
    """The child's consented turns of the last 90 days (guardian access already proven)."""

    require_wechat_guardian(request, user)
    evidence_account_id = await guardian_evidence_account(
        request, user=user, minor_user_id=minor_user_id
    )
    zone = ZoneInfo(_settings(request).memoria_timezone)
    now = _now()
    events = await _archive(request).evidence_window(
        account_id=evidence_account_id,
        subject_id=minor_user_id,
        occurred_after=now - timedelta(days=_RECAP_LOOKBACK_DAYS),
        occurred_before=now,
        event_types=("speech.utterance_finalized",),
        limit=10_000,
    )
    return child_utterances(events, subject_id=minor_user_id, zone=zone), zone


def consent_reference(consent: object) -> str | None:
    if consent is None:
        return None
    value = getattr(consent, "consent_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(consent, Mapping):
        raw = consent.get("consent_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


async def child_memory_account(
    request: Request, *, user: AuthenticatedUser, minor_user_id: str
) -> str:
    """Whose archive holds the memories of a child who has no account, once the guardian is proven.

    Only the adult who bound the device for the child reviews what the robot may remember about
    them.  A child with an account of their own keeps their memories behind that account: a
    guardian link carries summaries and reports, not the child's memory.
    """

    require_wechat_guardian(request, user)
    if not await owns_accountless_child(request, user=user, subject_person_id=minor_user_id):
        raise HTTPException(
            status_code=403, detail={"code": "guardian_memory_review_unavailable"}
        )
    return await guardian_evidence_account(request, user=user, minor_user_id=minor_user_id)
