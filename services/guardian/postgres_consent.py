"""Guardian consent persistence mixin.

``PostgresGuardianConsentMixin`` owns the link-scoped consent lifecycle
(grant/get/list/revoke/active).  Pools, API-role context, link lookups and the
row mapper stay owned by ``PostgresGuardianStore`` and are declared here for
type checking only.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg

from services.guardian.domain import (
    ConsentKind,
    ConsentRecord,
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianLink,
    GuardianNotFoundError,
    PersonConsentRecord,
)
from services.guardian.postgres_helpers import _timestamp, _uuid


class PostgresGuardianConsentMixin:
    """Read and mutate link-scoped guardian consent rows."""

    if TYPE_CHECKING:
        @staticmethod
        async def _set_api_context(
            connection: asyncpg.Connection,
            *,
            actor_user_id: str,
            subject_user_id: str,
        ) -> None: ...

        async def _ready_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        def _consent(row: asyncpg.Record) -> ConsentRecord: ...

        @staticmethod
        def _person_consent(row: asyncpg.Record) -> PersonConsentRecord: ...

        async def get_link(
            self, *, link_id: str, actor_user_id: str
        ) -> GuardianLink: ...


    async def grant_consent(
        self,
        record: ConsentRecord,
        *,
        actor_user_id: str | None = None,
    ) -> ConsentRecord:
        if actor_user_id is None or not str(actor_user_id).strip():
            raise GuardianAccessDeniedError(
                "guardian consent grant requires an actor context"
            )
        pool = await self._ready_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_user_id,
                    subject_user_id=actor_user_id,
                )
                link_row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_links
                    WHERE link_id = $1 AND guardian_user_id = $2
                    FOR UPDATE
                    """,
                    _uuid(record.link_id, field="link_id"),
                    actor_user_id,
                )
                if link_row is None or str(link_row["status"]) != "active":
                    raise GuardianAccessDeniedError("an active guardian link is required")
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_user_id,
                    subject_user_id=str(link_row["minor_user_id"]),
                )
                conflicting = await connection.fetchval(
                    """
                    SELECT consent_id FROM guardian_consents
                    WHERE link_id = $1 AND consent_kind = $2 AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > $3)
                      AND consent_id <> $4
                    LIMIT 1
                    """,
                    _uuid(record.link_id, field="link_id"),
                    record.consent_kind,
                    record.granted_at,
                    _uuid(record.consent_id, field="consent_id"),
                )
                if conflicting is not None:
                    raise GuardianConflictError(
                        "an active consent already exists for this capability"
                    )
                row = await connection.fetchrow(
                    """
                    INSERT INTO guardian_consents(
                        consent_id, link_id, consent_kind, policy_version,
                        granted_at, expires_at, evidence_event_id
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                    ON CONFLICT(consent_id) DO NOTHING
                    RETURNING *
                    """,
                    _uuid(record.consent_id, field="consent_id"),
                    _uuid(record.link_id, field="link_id"),
                    record.consent_kind,
                    record.policy_version,
                    record.granted_at,
                    record.expires_at,
                    record.evidence_event_id,
                )
                if row is None:
                    row = await connection.fetchrow(
                        "SELECT * FROM guardian_consents WHERE consent_id = $1",
                        _uuid(record.consent_id, field="consent_id"),
                    )
                    if row is None:  # pragma: no cover
                        raise RuntimeError("guardian consent disappeared")
                    current = self._consent(row)
                    if current != record:
                        raise GuardianConflictError("consent id is immutable")
                    return current
        except asyncpg.UniqueViolationError as exc:
            raise GuardianConflictError(
                "an active consent already exists for this capability"
            ) from exc
        assert row is not None
        return self._consent(row)

    async def get_consent(
        self,
        *,
        consent_id: str,
        actor_user_id: str,
    ) -> ConsentRecord:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_user_id,
                    subject_user_id=actor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT consent.* FROM guardian_consents consent
                    JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE consent.consent_id = $1
                      AND (link.guardian_user_id = $2 OR link.minor_user_id = $2)
                    """,
                    _uuid(consent_id, field="consent_id"),
                    actor_user_id,
                )
        if row is None:
            raise GuardianNotFoundError("guardian consent not found")
        return self._consent(row)

    async def list_consents(
        self,
        *,
        link_id: str,
        actor_user_id: str,
    ) -> tuple[ConsentRecord, ...]:
        await self.get_link(link_id=link_id, actor_user_id=actor_user_id)
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_user_id,
                    subject_user_id=actor_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_consents
                    WHERE link_id = $1 ORDER BY granted_at, consent_id
                    """,
                    _uuid(link_id, field="link_id"),
                )
        return tuple(self._consent(row) for row in rows)

    async def revoke_consent(
        self,
        *,
        consent_id: str,
        guardian_user_id: str,
        revoked_at: datetime,
        revocation_evidence_event_id: str,
    ) -> ConsentRecord:
        timestamp = _timestamp(revoked_at, field="revoked_at")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=guardian_user_id,
                subject_user_id=guardian_user_id,
            )
            linked_minor = await connection.fetchval(
                """
                SELECT link.minor_user_id
                FROM guardian_consents consent
                JOIN guardian_links link ON link.link_id = consent.link_id
                WHERE consent.consent_id = $1
                  AND link.guardian_user_id = $2
                """,
                _uuid(consent_id, field="consent_id"),
                guardian_user_id,
            )
            if linked_minor is None:
                raise GuardianNotFoundError("guardian consent not found")
            await self._set_api_context(
                connection,
                actor_user_id=guardian_user_id,
                subject_user_id=str(linked_minor),
            )
            row = await connection.fetchrow(
                """
                UPDATE guardian_consents AS consent
                SET revoked_at = $3, revocation_evidence_event_id = $4
                FROM guardian_links AS link
                WHERE consent.consent_id = $1
                  AND link.link_id = consent.link_id
                  AND link.guardian_user_id = $2
                  AND consent.revoked_at IS NULL
                RETURNING consent.*
                """,
                _uuid(consent_id, field="consent_id"),
                guardian_user_id,
                timestamp,
                revocation_evidence_event_id,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT consent.* FROM guardian_consents consent
                    JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE consent.consent_id = $1 AND link.guardian_user_id = $2
                    """,
                    _uuid(consent_id, field="consent_id"),
                    guardian_user_id,
                )
                if row is None:
                    raise GuardianNotFoundError("guardian consent not found")
                current = self._consent(row)
                if current.revocation_evidence_event_id != revocation_evidence_event_id:
                    raise GuardianConflictError("consent was already revoked")
                return current
        return self._consent(row)

    async def active_consent(
        self,
        *,
        minor_user_id: str,
        consent_kind: ConsentKind,
    ) -> ConsentRecord | PersonConsentRecord | None:
        """One read gate over both consent key spaces.

        Link-scoped consents (a minor with an account confirmed a guardian
        link) and person-scoped consents (an account-less subject whose
        binding owner granted) are different records, but every caller asks
        the same question: may this subject's capability run?  Both are
        unioned here so the policy gate never has to know which key space the
        authority used.
        """

        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=minor_user_id,
                    subject_user_id=minor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT consent.* FROM guardian_consents consent
                    JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE link.minor_user_id = $1 AND link.status = 'active'
                      AND consent.consent_kind = $2 AND consent.revoked_at IS NULL
                      AND (consent.expires_at IS NULL OR consent.expires_at > now())
                    ORDER BY consent.granted_at DESC LIMIT 1
                    """,
                    minor_user_id,
                    consent_kind,
                )
                if row is not None:
                    return self._consent(row)
                person_row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_person_consents
                    WHERE subject_person_id = $1 AND consent_kind = $2
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY granted_at DESC LIMIT 1
                    """,
                    minor_user_id,
                    consent_kind,
                )
        return (
            self._person_consent(person_row) if person_row is not None else None
        )
