"""Versioned guardian-consent lifecycle backed by the Evidence Ledger."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from services.archive.domain import (
    EvidenceEvent,
    IdempotencyConflictError,
    LifeArchivePort,
)
from services.guardian.domain import (
    ConsentKind,
    ConsentRecord,
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianNotFoundError,
    GuardianStorePort,
    PersonConsentRecord,
    same_person_consent_grant_request,
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
        return await self._store.grant_consent(
            record,
            actor_user_id=guardian_user_id,
        )

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

    # ------------------------------------------------------------------
    # Person-scoped consents (subjects with no account)
    # ------------------------------------------------------------------

    async def grant_for_person(
        self,
        *,
        subject_person_id: str,
        grantor_person_id: str,
        consent_kind: ConsentKind,
        policy_version: str,
        evidence_event_id: str,
        expires_at: datetime | None = None,
        now: datetime | None = None,
        consent_id: str | None = None,
    ) -> PersonConsentRecord:
        """Grant one consent for a subject that has no account.

        The grantor is the owner of the ACTIVE ``parent_for_child`` binding
        naming the subject; the caller has already checked that binding
        authority (the route does it against Identity).  Evidence is written
        first, exactly like the link-scoped grant, so a ledger failure cannot
        leave a usable consent behind.  Replaying the same idempotency key
        returns the original authorization and evidence; only an actually
        different request payload conflicts.
        """

        granted_at = (now or datetime.now(UTC)).astimezone(UTC)
        record = PersonConsentRecord(
            consent_id=consent_id or str(uuid.uuid4()),
            subject_person_id=subject_person_id,
            grantor_person_id=grantor_person_id,
            consent_kind=consent_kind,
            policy_version=policy_version,
            granted_at=granted_at,
            evidence_event_id=evidence_event_id,
            expires_at=expires_at,
        )
        existing = await self._existing_person_grant(
            subject_person_id=subject_person_id,
            grantor_person_id=grantor_person_id,
            record=record,
        )
        if existing is not None:
            return existing
        try:
            await self._archive.record(
                EvidenceEvent(
                    event_id=evidence_event_id,
                    account_id=subject_person_id,
                    event_type="guardian.person_consent_granted",
                    occurred_at=granted_at,
                    speaker_class="system",
                    source="guardian.consent",
                    payload={
                        "subject_person_id": subject_person_id,
                        "grantor_person_id": grantor_person_id,
                        "consent_id": record.consent_id,
                        "consent_kind": consent_kind,
                        "policy_version": policy_version,
                        "expires_at": (
                            record.expires_at.isoformat()
                            if record.expires_at
                            else None
                        ),
                    },
                )
            )
        except IdempotencyConflictError:
            # A concurrent retry won the evidence write with a recomputed
            # grant clock.  If it also committed the consent, this attempt is
            # the replay and returns the original authorization.
            existing = await self._existing_person_grant(
                subject_person_id=subject_person_id,
                grantor_person_id=grantor_person_id,
                record=record,
            )
            if existing is not None:
                return existing
            raise
        return await self._store.grant_person_consent(
            record,
            actor_person_id=grantor_person_id,
        )

    async def _existing_person_grant(
        self,
        *,
        subject_person_id: str,
        grantor_person_id: str,
        record: PersonConsentRecord,
    ) -> PersonConsentRecord | None:
        try:
            current = await self._store.get_person_consent(
                consent_id=record.consent_id,
                actor_person_id=grantor_person_id,
                subject_person_id=subject_person_id,
            )
        except GuardianNotFoundError:
            return None
        if current == record or same_person_consent_grant_request(current, record):
            return current
        raise GuardianConflictError("consent id is immutable")

    async def revoke_for_person(
        self,
        *,
        consent_id: str,
        grantor_person_id: str,
        subject_person_id: str,
        evidence_event_id: str,
        now: datetime | None = None,
    ) -> PersonConsentRecord:
        current = await self._store.get_person_consent(
            consent_id=consent_id,
            actor_person_id=grantor_person_id,
            subject_person_id=subject_person_id,
        )
        if current.grantor_person_id != grantor_person_id:
            raise GuardianAccessDeniedError(
                "only the granting person may revoke a person consent"
            )
        revoked_at = (now or datetime.now(UTC)).astimezone(UTC)
        revoked = await self._store.revoke_person_consent(
            consent_id=consent_id,
            grantor_person_id=grantor_person_id,
            subject_person_id=subject_person_id,
            revoked_at=revoked_at,
            revocation_evidence_event_id=evidence_event_id,
        )
        if self._on_revoked is not None:
            result = self._on_revoked(
                revoked.subject_person_id, revoked.consent_kind
            )
            if inspect.isawaitable(result):
                await result
        await self._archive.record(
            EvidenceEvent(
                event_id=evidence_event_id,
                account_id=revoked.subject_person_id,
                event_type="guardian.person_consent_revoked",
                occurred_at=revoked_at,
                speaker_class="system",
                source="guardian.consent",
                supersedes_event_id=revoked.evidence_event_id,
                payload={
                    "subject_person_id": revoked.subject_person_id,
                    "grantor_person_id": revoked.grantor_person_id,
                    "consent_id": revoked.consent_id,
                    "consent_kind": revoked.consent_kind,
                    "policy_version": revoked.policy_version,
                },
            )
        )
        return revoked


__all__ = ["ConsentRevocationHook", "GuardianConsentService"]
