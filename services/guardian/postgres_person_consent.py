"""PostgreSQL person-scoped consent mixin.

``PostgresGuardianPersonConsentMixin`` is composed into
``PostgresGuardianStore``. The store owns the pool, the API context setter and
the row mappers; this module owns the consent rules for account-less subjects.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg

from services.guardian.domain import (
    ConsentKind,
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianNotFoundError,
    PersonConsentRecord,
    same_person_consent_grant_request,
)
from services.guardian.postgres_helpers import _uuid


class PostgresGuardianPersonConsentMixin:
    """Grant and read consents for account-less child subjects."""

    if TYPE_CHECKING:
        # Shared members owned by PostgresGuardianStore.
        async def _ready_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _set_api_context(
            connection: asyncpg.Connection,
            *,
            actor_user_id: str,
            subject_user_id: str,
        ) -> None: ...

        @staticmethod
        def _person_consent(row: asyncpg.Record) -> PersonConsentRecord: ...

    # -- person-scoped consents (account-less subjects) ------------------

    async def grant_person_consent(
        self,
        record: PersonConsentRecord,
        *,
        actor_person_id: str,
    ) -> PersonConsentRecord:
        if actor_person_id != record.grantor_person_id:
            raise GuardianAccessDeniedError(
                "only the granting person may write a person consent"
            )
        pool = await self._ready_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=record.grantor_person_id,
                    subject_user_id=record.subject_person_id,
                )
                conflicting = await connection.fetchval(
                    """
                    SELECT consent_id FROM guardian_person_consents
                    WHERE subject_person_id = $1 AND consent_kind = $2
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > $3)
                      AND consent_id <> $4
                    LIMIT 1
                    """,
                    record.subject_person_id,
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
                    INSERT INTO guardian_person_consents(
                        consent_id, subject_person_id, grantor_person_id,
                        consent_kind, policy_version, granted_at, expires_at,
                        evidence_event_id
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    ON CONFLICT(consent_id) DO NOTHING
                    RETURNING *
                    """,
                    _uuid(record.consent_id, field="consent_id"),
                    record.subject_person_id,
                    record.grantor_person_id,
                    record.consent_kind,
                    record.policy_version,
                    record.granted_at,
                    record.expires_at,
                    record.evidence_event_id,
                )
                if row is None:
                    row = await connection.fetchrow(
                        "SELECT * FROM guardian_person_consents WHERE consent_id = $1",
                        _uuid(record.consent_id, field="consent_id"),
                    )
                    if row is None:  # pragma: no cover
                        raise RuntimeError("guardian person consent disappeared")
                    current = self._person_consent(row)
                    if current != record and not same_person_consent_grant_request(
                        current, record
                    ):
                        raise GuardianConflictError("consent id is immutable")
                    return current
        except asyncpg.UniqueViolationError as exc:
            raise GuardianConflictError(
                "an active consent already exists for this capability"
            ) from exc
        assert row is not None
        return self._person_consent(row)

    async def get_person_consent(
        self,
        *,
        consent_id: str,
        actor_person_id: str,
        subject_person_id: str,
    ) -> PersonConsentRecord:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_person_id,
                    subject_user_id=subject_person_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_person_consents
                    WHERE consent_id = $1 AND subject_person_id = $2
                      AND (grantor_person_id = $3 OR subject_person_id = $3)
                    """,
                    _uuid(consent_id, field="consent_id"),
                    subject_person_id,
                    actor_person_id,
                )
        if row is None:
            raise GuardianNotFoundError("guardian person consent not found")
        return self._person_consent(row)

    async def list_person_consents(
        self,
        *,
        subject_person_id: str,
        actor_person_id: str,
    ) -> tuple[PersonConsentRecord, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=actor_person_id,
                    subject_user_id=subject_person_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_person_consents
                    WHERE subject_person_id = $1
                      AND (grantor_person_id = $2 OR subject_person_id = $2)
                    ORDER BY granted_at, consent_id
                    """,
                    subject_person_id,
                    actor_person_id,
                )
        return tuple(self._person_consent(row) for row in rows)

    async def revoke_person_consent(
        self,
        *,
        consent_id: str,
        grantor_person_id: str,
        subject_person_id: str,
        revoked_at: datetime,
        revocation_evidence_event_id: str,
    ) -> PersonConsentRecord:
        pool = await self._ready_pool()
        consent_uuid = _uuid(consent_id, field="consent_id")
        async with pool.acquire() as connection:
            async with connection.transaction():
                # The trusted subject context comes from the caller (already
                # authorized for this subject); it is what lets a grantor read
                # and revoke its own row under FORCE RLS without broadening
                # the policy to the grantor's whole person scope.
                await self._set_api_context(
                    connection,
                    actor_user_id=grantor_person_id,
                    subject_user_id=subject_person_id,
                )
                row = await connection.fetchrow(
                    """
                    UPDATE guardian_person_consents
                    SET revoked_at = $4, revocation_evidence_event_id = $5
                    WHERE consent_id = $1 AND subject_person_id = $2
                      AND grantor_person_id = $3 AND revoked_at IS NULL
                    RETURNING *
                    """,
                    consent_uuid,
                    subject_person_id,
                    grantor_person_id,
                    revoked_at,
                    revocation_evidence_event_id,
                )
                if row is None:
                    row = await connection.fetchrow(
                        """
                        SELECT * FROM guardian_person_consents
                        WHERE consent_id = $1 AND subject_person_id = $2
                          AND grantor_person_id = $3
                        """,
                        consent_uuid,
                        subject_person_id,
                        grantor_person_id,
                    )
                    if row is None:
                        raise GuardianNotFoundError(
                            "guardian person consent not found"
                        )
                    current = self._person_consent(row)
                    if (
                        current.revocation_evidence_event_id
                        != revocation_evidence_event_id
                    ):
                        raise GuardianConflictError("consent was already revoked")
                    return current
        return self._person_consent(row)

    async def active_person_consent(
        self,
        *,
        subject_person_id: str,
        consent_kind: ConsentKind,
    ) -> PersonConsentRecord | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=subject_person_id,
                    subject_user_id=subject_person_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_person_consents
                    WHERE subject_person_id = $1 AND consent_kind = $2
                      AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY granted_at DESC LIMIT 1
                    """,
                    subject_person_id,
                    consent_kind,
                )
        return self._person_consent(row) if row is not None else None
