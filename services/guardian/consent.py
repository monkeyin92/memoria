"""Versioned guardian-consent lifecycle backed by the Evidence Ledger."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from services.archive.domain import EvidenceEvent, LifeArchivePort
from services.guardian.domain import (
    ConsentKind,
    ConsentRecord,
    GuardianAccessDeniedError,
    GuardianStorePort,
)

ConsentRevocationHook = Callable[[str, ConsentKind], Awaitable[None] | None]


class GuardianConsentService:
    """Keep authorization state and immutable audit evidence aligned.

    Grants write evidence before they become usable, so a ledger failure cannot
    accidentally enable a capability.  Revocations take the opposite ordering:
    the authorization row is disabled first, then its deterministic evidence is
    appended.  A retry records the same event while the capability stays closed.
    """

    def __init__(
        self,
        store: GuardianStorePort,
        archive: LifeArchivePort,
        *,
        on_revoked: ConsentRevocationHook | None = None,
    ) -> None:
        self._store = store
        self._archive = archive
        self._on_revoked = on_revoked

    async def grant(
        self,
        *,
        link_id: str,
        guardian_user_id: str,
        consent_kind: ConsentKind,
        policy_version: str,
        evidence_event_id: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
        consent_id: str | None = None,
    ) -> ConsentRecord:
        link = await self._store.get_link(
            link_id=link_id,
            actor_user_id=guardian_user_id,
        )
        if link.guardian_user_id != guardian_user_id or link.status != "active":
            raise GuardianAccessDeniedError("an active guardian link is required")
        granted_at = (now or datetime.now(UTC)).astimezone(UTC)
        record = ConsentRecord(
            consent_id=consent_id or str(uuid.uuid4()),
            link_id=link.link_id,
            consent_kind=consent_kind,
            policy_version=policy_version,
            granted_at=granted_at,
            evidence_event_id=evidence_event_id,
            expires_at=expires_at,
        )
        await self._archive.record(
            EvidenceEvent(
                event_id=evidence_event_id,
                account_id=link.minor_user_id,
                event_type="guardian.consent_granted",
                occurred_at=granted_at,
                speaker_class="system",
                source="guardian.consent",
                payload={
                    "link_id": link.link_id,
                    "consent_id": record.consent_id,
                    "consent_kind": consent_kind,
                    "policy_version": policy_version,
                    "expires_at": record.expires_at.isoformat() if record.expires_at else None,
                },
            )
        )
        return await self._store.grant_consent(record)

    async def revoke(
        self,
        *,
        consent_id: str,
        guardian_user_id: str,
        evidence_event_id: str,
        now: datetime | None = None,
    ) -> ConsentRecord:
        current = await self._store.get_consent(
            consent_id=consent_id,
            actor_user_id=guardian_user_id,
        )
        link = await self._store.get_link(
            link_id=current.link_id,
            actor_user_id=guardian_user_id,
        )
        if link.guardian_user_id != guardian_user_id:
            raise GuardianAccessDeniedError("only the granting guardian may revoke consent")
        revoked_at = (now or datetime.now(UTC)).astimezone(UTC)
        revoked = await self._store.revoke_consent(
            consent_id=consent_id,
            guardian_user_id=guardian_user_id,
            revoked_at=revoked_at,
            revocation_evidence_event_id=evidence_event_id,
        )
        if self._on_revoked is not None:
            result = self._on_revoked(link.minor_user_id, revoked.consent_kind)
            if inspect.isawaitable(result):
                await result
        await self._archive.record(
            EvidenceEvent(
                event_id=evidence_event_id,
                account_id=link.minor_user_id,
                event_type="guardian.consent_revoked",
                occurred_at=revoked_at,
                speaker_class="system",
                source="guardian.consent",
                supersedes_event_id=revoked.evidence_event_id,
                payload={
                    "link_id": link.link_id,
                    "consent_id": revoked.consent_id,
                    "consent_kind": revoked.consent_kind,
                    "policy_version": revoked.policy_version,
                },
            )
        )
        return revoked


__all__ = ["ConsentRevocationHook", "GuardianConsentService"]
