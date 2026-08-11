"""PostgreSQL/FORCE-RLS persistence for guardian links and consents."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import asyncpg

from services.archive.object_store import ObjectRef
from services.guardian.corpus import (
    MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR,
    CorpusConsentInactiveError,
    CorpusSample,
    CorpusSampleLimitError,
)
from services.guardian.crisis import (
    CrisisNotificationReceipt,
    GuardianNotification,
    NotificationStatus,
)
from services.guardian.domain import (
    ConsentKind,
    ConsentRecord,
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianLink,
    GuardianLinkStatus,
    GuardianNotFoundError,
    Relation,
    VerifiedVia,
)
from services.tutor.authority import (
    TutorEvidenceRejected,
    TutorPolicyReceiptVerifierPort,
)
from services.tutor.domain import (
    PendingTutorCommit,
    PracticeConflictError,
    PracticeSession,
    PracticeStatus,
    StudyProgress,
    TutorAggregateCommit,
    TutorAggregateKind,
    TutorFocus,
)

_REQUIRED_TABLES = frozenset(
    {
        "guardian_links",
        "guardian_consents",
        "guardian_corpus_samples",
        "tutor_practice_sessions",
        "tutor_study_progress",
        "tutor_practice_evidence",
        "tutor_commit_outbox",
        "guardian_crisis_events",
        "guardian_notification_outbox",
    }
)


def _timestamp(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _uuid(value: str, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("binding_code_hash must be a SHA-256 hex digest")
    return normalized


def _json_array(value: object, *, field: str) -> list[object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list):
        raise RuntimeError(f"{field} must be a JSON array")
    return decoded


class PostgresGuardianStore:
    """Controller store with dedicated governance/worker roles.

    ``dsn`` authenticates the API role (memoria_guardian, subject-scoped).
    ``maintenance_dsn`` and ``worker_dsn`` must authenticate the dedicated
    LOGIN roles that own the SECURITY DEFINER functions; without them the
    account-scoped export/delete and outbox worker operations fail closed.
    """

    def __init__(
        self,
        dsn: str,
        *,
        bootstrap_dsn: str | None = None,
        maintenance_dsn: str | None = None,
        worker_dsn: str | None = None,
        initialize_schema: bool = True,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("guardian DSN must use PostgreSQL")
        self._dsn = dsn
        self._bootstrap_dsn = bootstrap_dsn
        self._maintenance_dsn = maintenance_dsn
        self._worker_dsn = worker_dsn
        self._initialize_schema = initialize_schema
        self._pool: asyncpg.Pool | None = None
        self._maintenance_pool: asyncpg.Pool | None = None
        self._worker_pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        if self._initialize_schema:
            # DDL runs once as the bootstrap (admin) role; the API role never
            # receives CREATE privileges.  Production deploys the schema out
            # of band and runs with initialize_schema=False.
            bootstrap = self._bootstrap_dsn or self._dsn
            connection = await asyncpg.connect(bootstrap)
            try:
                schema = Path(__file__).with_name("postgres_schema.sql").read_text(
                    encoding="utf-8"
                )
                await connection.execute(schema)
            finally:
                await connection.close()
        pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            command_timeout=15,
        )
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL guardian pool")
        try:
            async with pool.acquire() as connection:
                if not self._initialize_schema:
                    await self._verify_production_schema(connection)
        except Exception:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        for attr in ("_pool", "_maintenance_pool", "_worker_pool"):
            pool = getattr(self, attr)
            if pool is not None:
                await pool.close()
                setattr(self, attr, None)

    async def healthcheck(self) -> None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1 FROM guardian_links LIMIT 1")

    async def _ready_role_pool(
        self,
        *,
        dsn: str | None,
        pool_attr: str,
        expected_role: str,
        operation: str,
    ) -> asyncpg.Pool:
        if dsn is None:
            raise RuntimeError(
                f"{operation} requires the {expected_role} role DSN; "
                "it is not configured (fail closed)"
            )
        pool = getattr(self, pool_attr)
        if pool is not None:
            return pool
        candidate = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=5,
            command_timeout=15,
        )
        if candidate is None:  # pragma: no cover
            raise RuntimeError(f"failed to create {expected_role} pool")
        try:
            async with candidate.acquire() as connection:
                current_user = str(await connection.fetchval("SELECT current_user"))
                if current_user != expected_role:
                    raise RuntimeError(
                        f"{operation} DSN must authenticate as {expected_role}, "
                        f"got {current_user}"
                    )
                bypass = await connection.fetchval(
                    "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
                if bypass:
                    raise RuntimeError(
                        f"{expected_role} must be NOBYPASSRLS for {operation}"
                    )
        except Exception:
            await candidate.close()
            raise
        setattr(self, pool_attr, candidate)
        return candidate

    @staticmethod
    async def _verify_production_schema(connection: asyncpg.Connection) -> None:
        role = await connection.fetchrow(
            "SELECT current_user AS role_name, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        if (
            role is None
            or str(role["role_name"]) != "memoria_guardian"
            or bool(role["rolbypassrls"])
        ):
            raise RuntimeError(
                "production guardian store requires the memoria_guardian NOBYPASSRLS role"
            )
        rows = await connection.fetch(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE relnamespace = current_schema()::regnamespace
              AND relname = ANY($1::text[])
            """,
            list(_REQUIRED_TABLES),
        )
        by_name = {str(row["relname"]): row for row in rows}
        if set(by_name) != _REQUIRED_TABLES or any(
            not bool(row["relrowsecurity"]) or not bool(row["relforcerowsecurity"])
            for row in by_name.values()
        ):
            raise RuntimeError(
                "production guardian schema and FORCE RLS must be installed before startup"
            )

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL guardian store is not initialized")
        return self._pool

    @staticmethod
    async def _set_api_context(
        connection: asyncpg.Connection,
        *,
        actor_user_id: str,
        subject_user_id: str,
    ) -> None:
        """Bind every API transaction to a non-empty actor and subject.

        The values are transaction-local so a pooled connection cannot leak
        one request's authority into the next request.  PostgreSQL RLS is the
        second gate: an application query that omits this context sees no
        rows and cannot write any of the protected Guardian tables.
        """

        actor = str(actor_user_id).strip()
        subject = str(subject_user_id).strip()
        if not actor or not subject:
            raise ValueError("guardian API transactions require actor and subject")
        await connection.execute(
            """
            SELECT set_config('memoria.guardian_actor_id', $1, true),
                   set_config('memoria.guardian_subject_id', $2, true)
            """,
            actor,
            subject,
        )

    @staticmethod
    def _link(row: asyncpg.Record) -> GuardianLink:
        return GuardianLink(
            link_id=str(row["link_id"]),
            guardian_user_id=str(row["guardian_user_id"]),
            minor_user_id=str(row["minor_user_id"]),
            relation=cast(Relation, str(row["relation"])),
            status=cast(GuardianLinkStatus, str(row["status"])),
            verified_via=cast(VerifiedVia, str(row["verified_via"])),
            created_at=cast(datetime, row["created_at"]),
            binding_expires_at=cast(datetime, row["binding_expires_at"]),
            activated_at=cast(datetime | None, row["activated_at"]),
            revoked_at=cast(datetime | None, row["revoked_at"]),
        )

    @staticmethod
    def _consent(row: asyncpg.Record) -> ConsentRecord:
        return ConsentRecord(
            consent_id=str(row["consent_id"]),
            link_id=str(row["link_id"]),
            consent_kind=cast(ConsentKind, str(row["consent_kind"])),
            policy_version=str(row["policy_version"]),
            granted_at=cast(datetime, row["granted_at"]),
            evidence_event_id=str(row["evidence_event_id"]),
            expires_at=cast(datetime | None, row["expires_at"]),
            revoked_at=cast(datetime | None, row["revoked_at"]),
            revocation_evidence_event_id=(
                str(row["revocation_evidence_event_id"])
                if row["revocation_evidence_event_id"] is not None
                else None
            ),
        )

    @staticmethod
    def _corpus_sample(row: asyncpg.Record) -> CorpusSample:
        account_id = str(row["minor_user_id"])
        return CorpusSample(
            sample_id=str(row["sample_id"]),
            minor_user_id=account_id,
            consent_id=str(row["consent_id"]),
            source_event_id=str(row["source_event_id"]),
            reference=ObjectRef(
                account_id=account_id,
                object_key=str(row["object_key"]),
                media_type=str(row["media_type"]),
                byte_count=int(row["byte_count"]),
                content_sha256=str(row["content_sha256"]),
                encryption_key_version=str(row["encryption_key_version"]),
                backend=str(row["object_backend"]),
            ),
            created_at=cast(datetime, row["created_at"]),
            expires_at=cast(datetime, row["expires_at"]),
            deleted_at=cast(datetime | None, row["deleted_at"]),
        )

    @staticmethod
    def _practice_session(row: asyncpg.Record) -> PracticeSession:
        subject_id = row["subject_id"]
        if subject_id is None or not str(subject_id):
            raise RuntimeError("tutor practice session has no authoritative subject")
        return PracticeSession(
            session_id=str(row["session_id"]),
            subject_id=str(subject_id),
            actor_id=str(row["actor_id"] or row["account_id"]),
            voice_session_id=str(row["voice_session_id"] or ""),
            focus=cast(TutorFocus, str(row["focus"])),
            task_id=str(row["task_id"]),
            status=cast(PracticeStatus, str(row["status"])),
            revision=int(row["revision"]),
            event_ids=tuple(
                str(value)
                for value in _json_array(row["event_ids_json"], field="event_ids_json")
            ),
            practiced_seconds=int(row["practiced_seconds"]),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    @staticmethod
    def _study_progress(row: asyncpg.Record) -> StudyProgress:
        active_days = _json_array(row["active_days_json"], field="active_days_json")
        weak_points = _json_array(row["weak_points_json"], field="weak_points_json")
        mastered = _json_array(
            row["mastered_skills_json"],
            field="mastered_skills_json",
        )
        source_ids = _json_array(
            row["source_event_ids_json"],
            field="source_event_ids_json",
        )
        return StudyProgress(
            subject_id=str(row["subject_id"]),
            actor_id=str(row["actor_id"] or row["account_id"]),
            practiced_seconds=int(row["practiced_seconds"]),
            active_days=tuple(date.fromisoformat(str(value)) for value in active_days),
            current_streak_days=int(row["current_streak_days"]),
            weak_points=tuple(
                (
                    str(cast(list[object], value)[0]),
                    int(str(cast(list[object], value)[1])),
                )
                for value in weak_points
            ),
            mastered_skills=tuple(str(value) for value in mastered),
            source_event_ids=tuple(str(value) for value in source_ids),
            last_practiced_at=cast(datetime | None, row["last_practiced_at"]),
        )

    @staticmethod
    def _notification(row: asyncpg.Record) -> GuardianNotification:
        return GuardianNotification(
            notification_id=str(row["notification_id"]),
            crisis_event_id=str(row["crisis_event_id"]),
            guardian_user_id=str(row["guardian_user_id"]),
            minor_user_id=str(row["minor_user_id"]),
            channel="wechat_subscription",
            status=cast(NotificationStatus, str(row["status"])),
            attempts=int(row["attempts"]),
            created_at=cast(datetime, row["created_at"]),
            delivered_at=cast(datetime | None, row["delivered_at"]),
            last_error_code=(
                str(row["last_error_code"])
                if row["last_error_code"] is not None
                else None
            ),
        )

    async def create_link(
        self,
        *,
        link_id: str | None = None,
        guardian_user_id: str,
        minor_user_id: str,
        relation: Relation,
        verified_via: VerifiedVia,
        binding_code_hash: str,
        binding_expires_at: datetime,
        now: datetime,
    ) -> GuardianLink:
        link = GuardianLink(
            link_id=str(uuid.uuid4()) if link_id is None else str(_uuid(link_id, field="link_id")),
            guardian_user_id=guardian_user_id,
            minor_user_id=minor_user_id,
            relation=relation,
            status="pending",
            verified_via=verified_via,
            created_at=_timestamp(now, field="now"),
            binding_expires_at=_timestamp(binding_expires_at, field="binding_expires_at"),
        )
        digest = _hash(binding_code_hash)
        pool = await self._ready_pool()
        try:
            async with pool.acquire() as connection, connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=minor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    INSERT INTO guardian_links(
                        link_id, guardian_user_id, minor_user_id, relation, status,
                        verified_via, binding_code_hash, binding_expires_at, created_at
                    ) VALUES ($1,$2,$3,$4,'pending',$5,$6,$7,$8)
                    RETURNING *
                    """,
                    uuid.UUID(link.link_id),
                    link.guardian_user_id,
                    link.minor_user_id,
                    link.relation,
                    link.verified_via,
                    digest,
                    link.binding_expires_at,
                    link.created_at,
                )
        except asyncpg.UniqueViolationError as exc:
            raise GuardianConflictError("a live guardian link already exists") from exc
        if row is None:  # pragma: no cover
            raise RuntimeError("guardian link insert failed")
        return self._link(row)

    async def verify_binding_code(
        self,
        *,
        link_id: str,
        minor_user_id: str,
        binding_code_hash: str,
        now: datetime,
    ) -> GuardianLink:
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
                    SELECT * FROM guardian_links
                    WHERE link_id = $1 AND minor_user_id = $2
                      AND status = 'pending' AND binding_code_hash = $3
                      AND binding_expires_at > $4
                    """,
                    _uuid(link_id, field="link_id"),
                    minor_user_id,
                    _hash(binding_code_hash),
                    _timestamp(now, field="now"),
                )
        if row is None:
            raise GuardianAccessDeniedError("binding code is invalid or expired")
        return self._link(row)

    async def confirm_link(
        self,
        *,
        link_id: str,
        minor_user_id: str,
        binding_code_hash: str,
        now: datetime,
    ) -> GuardianLink:
        confirmed_at = _timestamp(now, field="now")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=minor_user_id,
                subject_user_id=minor_user_id,
            )
            row = await connection.fetchrow(
                """
                UPDATE guardian_links
                SET status = 'active', activated_at = $4
                WHERE link_id = $1 AND minor_user_id = $2
                  AND status = 'pending' AND binding_code_hash = $3
                  AND binding_expires_at > $4
                RETURNING *
                """,
                _uuid(link_id, field="link_id"),
                minor_user_id,
                _hash(binding_code_hash),
                confirmed_at,
            )
            if row is None:
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_links
                    WHERE link_id = $1 AND minor_user_id = $2
                      AND status = 'active' AND binding_code_hash = $3
                    """,
                    _uuid(link_id, field="link_id"),
                    minor_user_id,
                    _hash(binding_code_hash),
                )
        if row is None:
            raise GuardianAccessDeniedError("binding code is invalid or expired")
        return self._link(row)

    async def get_link(self, *, link_id: str, actor_user_id: str) -> GuardianLink:
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
                    SELECT * FROM guardian_links
                    WHERE link_id = $1
                      AND (guardian_user_id = $2 OR minor_user_id = $2)
                    """,
                    _uuid(link_id, field="link_id"),
                    actor_user_id,
                )
        if row is None:
            raise GuardianNotFoundError("guardian link not found")
        return self._link(row)

    async def active_link(
        self,
        *,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> GuardianLink | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=minor_user_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM guardian_links
                    WHERE guardian_user_id = $1 AND minor_user_id = $2 AND status = 'active'
                    ORDER BY activated_at DESC LIMIT 1
                    """,
                    guardian_user_id,
                    minor_user_id,
                )
        return self._link(row) if row is not None else None

    async def list_links(
        self,
        *,
        guardian_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]:
        allowed = {"pending", "active", "revoked"}
        if not statuses or any(status not in allowed for status in statuses):
            raise ValueError("guardian link statuses are invalid")
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=guardian_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_links
                    WHERE guardian_user_id = $1 AND status = ANY($2::text[])
                    ORDER BY created_at DESC
                    """,
                    guardian_user_id,
                    list(statuses),
                )
        return tuple(self._link(row) for row in rows)

    async def list_links_for_actor(
        self,
        *,
        actor_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]:
        allowed = {"pending", "active", "revoked"}
        if not statuses or any(status not in allowed for status in statuses):
            raise ValueError("guardian link statuses are invalid")
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
                    SELECT * FROM guardian_links
                    WHERE (guardian_user_id = $1 OR minor_user_id = $1)
                      AND status = ANY($2::text[])
                    ORDER BY created_at DESC
                    """,
                    actor_user_id,
                    list(statuses),
                )
        return tuple(self._link(row) for row in rows)

    async def active_guardian_links(
        self,
        *,
        minor_user_id: str,
    ) -> tuple[GuardianLink, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=minor_user_id,
                    subject_user_id=minor_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_links
                    WHERE minor_user_id = $1 AND status = 'active'
                    ORDER BY activated_at DESC
                    """,
                    minor_user_id,
                )
        return tuple(self._link(row) for row in rows)

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
    ) -> ConsentRecord | None:
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
        return self._consent(row) if row is not None else None

    async def corpus_sample_by_event(
        self,
        *,
        minor_user_id: str,
        source_event_id: str,
    ) -> CorpusSample | None:
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
                    SELECT * FROM guardian_corpus_samples
                    WHERE minor_user_id = $1 AND source_event_id = $2
                    """,
                    minor_user_id,
                    source_event_id,
                )
        return self._corpus_sample(row) if row is not None else None

    async def record_corpus_sample(self, sample: CorpusSample) -> CorpusSample:
        pool = await self._ready_pool()
        sample_id = _uuid(sample.sample_id, field="sample_id")
        consent_id = _uuid(sample.consent_id, field="consent_id")
        try:
            async with pool.acquire() as connection, connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=sample.minor_user_id,
                    subject_user_id=sample.minor_user_id,
                )
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    sample.minor_user_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM guardian_corpus_samples WHERE sample_id = $1",
                    sample_id,
                )
                if row is None:
                    authorized = await connection.fetchval(
                        """
                        SELECT true FROM guardian_consents consent
                        JOIN guardian_links link ON link.link_id = consent.link_id
                        WHERE consent.consent_id = $1
                          AND consent.consent_kind = 'corpus_recording'
                          AND consent.revoked_at IS NULL
                          AND consent.expires_at > now()
                          AND link.minor_user_id = $2
                          AND link.status = 'active'
                        FOR UPDATE OF consent, link
                        """,
                        consent_id,
                        sample.minor_user_id,
                    )
                    if authorized is not True:
                        raise CorpusConsentInactiveError(
                            "corpus consent became inactive before persistence"
                        )
                    active_count = int(
                        await connection.fetchval(
                            """
                            SELECT count(*) FROM guardian_corpus_samples
                            WHERE minor_user_id = $1 AND deleted_at IS NULL
                              AND expires_at > now()
                            """,
                            sample.minor_user_id,
                        )
                    )
                    if active_count >= MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR:
                        raise CorpusSampleLimitError(
                            "active corpus sample limit reached"
                        )
                    row = await connection.fetchrow(
                        """
                        INSERT INTO guardian_corpus_samples(
                            sample_id, minor_user_id, consent_id, source_event_id,
                            object_key, media_type, byte_count, content_sha256,
                            encryption_key_version, object_backend, created_at, expires_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                        RETURNING *
                        """,
                        sample_id,
                        sample.minor_user_id,
                        consent_id,
                        sample.source_event_id,
                        sample.reference.object_key,
                        sample.reference.media_type,
                        sample.reference.byte_count,
                        sample.reference.content_sha256,
                        sample.reference.encryption_key_version,
                        sample.reference.backend,
                        sample.created_at,
                        sample.expires_at,
                    )
        except asyncpg.UniqueViolationError as exc:
            raise GuardianConflictError("corpus sample conflicts with existing data") from exc
        if row is None:  # pragma: no cover
            raise RuntimeError("corpus sample disappeared")
        current = self._corpus_sample(row)
        if current != sample:
            raise GuardianConflictError("corpus sample id is immutable")
        return current

    async def corpus_samples(
        self,
        *,
        minor_user_id: str,
        include_deleted: bool = False,
    ) -> tuple[CorpusSample, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=minor_user_id,
                    subject_user_id=minor_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_corpus_samples
                    WHERE minor_user_id = $1 AND ($2 OR deleted_at IS NULL)
                    ORDER BY created_at, sample_id
                    """,
                    minor_user_id,
                    include_deleted,
                )
        return tuple(self._corpus_sample(row) for row in rows)

    async def expired_corpus_samples(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[CorpusSample, ...]:
        timestamp = _timestamp(now, field="now")
        if not 1 <= limit <= 1000:
            raise ValueError("corpus purge limit must be between 1 and 1000")
        pool = await self._ready_role_pool(
            dsn=self._maintenance_dsn,
            pool_attr="_maintenance_pool",
            expected_role="memoria_guardian_maintenance",
            operation="expired corpus sweep",
        )
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('memoria.guardian_maintenance_scope', '1', true)"
                )
                rows = await connection.fetch(
                    """
                    SELECT * FROM guardian_corpus_samples
                    WHERE deleted_at IS NULL AND expires_at <= $1
                    ORDER BY expires_at, sample_id LIMIT $2
                    """,
                    timestamp,
                    limit,
                )
        return tuple(self._corpus_sample(row) for row in rows)

    async def mark_corpus_sample_deleted(
        self,
        *,
        sample_id: str,
        deleted_at: datetime,
    ) -> CorpusSample:
        timestamp = _timestamp(deleted_at, field="deleted_at")
        pool = await self._ready_role_pool(
            dsn=self._maintenance_dsn,
            pool_attr="_maintenance_pool",
            expected_role="memoria_guardian_maintenance",
            operation="corpus retention delete",
        )
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('memoria.guardian_maintenance_scope', '1', true)"
                )
                row = await connection.fetchrow(
                    """
                    UPDATE guardian_corpus_samples
                    SET deleted_at = COALESCE(deleted_at, $2)
                    WHERE sample_id = $1 RETURNING *
                    """,
                    _uuid(sample_id, field="sample_id"),
                    timestamp,
                )
        if row is None:
            raise GuardianNotFoundError("corpus sample not found")
        return self._corpus_sample(row)

    async def practice_session(
        self,
        *,
        subject_id: str,
        session_id: str,
    ) -> PracticeSession | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('memoria.subject_id', $1, true)",
                    subject_id,
                )
                row = await connection.fetchrow(
                    """
                    SELECT * FROM tutor_practice_sessions
                    WHERE subject_id = $1 AND session_id = $2
                    """,
                    subject_id,
                    _uuid(session_id, field="session_id"),
                )
        return self._practice_session(row) if row is not None else None

    async def save_practice_session(self, session: PracticeSession) -> PracticeSession:
        if session.created_at is None or session.updated_at is None:
            raise ValueError("persisted practice sessions require timestamps")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('memoria.subject_id', $1, true)",
                session.subject_id,
            )
            saved = await self._save_practice_session_locked(connection, session)
        return saved

    @staticmethod
    async def _save_practice_session_locked(
        connection: asyncpg.Connection,
        session: PracticeSession,
    ) -> PracticeSession:
        if session.created_at is None or session.updated_at is None:
            raise ValueError("persisted practice sessions require timestamps")
        session_uuid = _uuid(session.session_id, field="session_id")
        row = await connection.fetchrow(
            """
            SELECT * FROM tutor_practice_sessions
            WHERE subject_id = $1 AND session_id = $2 FOR UPDATE
            """,
            session.subject_id,
            session_uuid,
        )
        if row is None:
            if session.revision != 0:
                raise PracticeConflictError("practice_session_not_found")
            await connection.execute(
                """
                INSERT INTO tutor_practice_sessions(
                    session_id, account_id, subject_id, actor_id,
                    voice_session_id, focus, task_id, status, revision,
                    event_ids_json, practiced_seconds, created_at, updated_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb,$11,$12,$13)
                """,
                session_uuid,
                session.actor_id,
                session.subject_id,
                session.actor_id,
                session.voice_session_id,
                session.focus,
                session.task_id,
                session.status,
                session.revision,
                json.dumps(session.event_ids, ensure_ascii=False),
                session.practiced_seconds,
                session.created_at,
                session.updated_at,
            )
            return session
        current = PostgresGuardianStore._practice_session(row)
        if current == session:
            return current
        if (
            session.subject_id != current.subject_id
            or session.actor_id != current.actor_id
            or session.voice_session_id != current.voice_session_id
            or session.revision != current.revision + 1
            or session.event_ids[:-1] != current.event_ids
        ):
            raise PracticeConflictError("revision_conflict")
        result = await connection.execute(
            """
            UPDATE tutor_practice_sessions
            SET status = $1, revision = $2, event_ids_json = $3::jsonb,
                practiced_seconds = $4, updated_at = $5
            WHERE session_id = $6 AND subject_id = $7 AND revision = $8
            """,
            session.status,
            session.revision,
            json.dumps(session.event_ids, ensure_ascii=False),
            session.practiced_seconds,
            session.updated_at,
            session_uuid,
            session.subject_id,
            current.revision,
        )
        if result != "UPDATE 1":  # pragma: no cover - row is locked
            raise PracticeConflictError("revision_conflict")
        return session

    async def commit_aggregate(
        self,
        *,
        commit: TutorAggregateCommit,
        receipt_verifier: TutorPolicyReceiptVerifierPort,
        now: datetime,
    ) -> PracticeSession:
        """Atomic one-time commit inside one transaction.

        Re-verifies the action receipt against the current policy state
        FIRST, then consumes the assessment once (UNIQUE), CASes the session
        revision, and persists the immutable evidence plus outbox row.  Any
        failure rolls the whole transaction back, so no partial state can be
        observed.
        """

        envelope_hash = hashlib.sha256(
            json.dumps(
                commit.evidence_envelope,
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        commit_hash = commit.commit_sha256()
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            # Receipt re-verification happens INSIDE this transaction: while
            # the write holds the connection, the Policy verifier must
            # consult the current receipt/consent/binding state on the SAME
            # connection (its production seam binds to this transaction).
            # Any revocation or mismatch rolls the whole commit back.
            verification = await receipt_verifier.verify_receipt(
                receipt_id=commit.receipt_id,
                expectation=commit.receipt_expectation,
                action_fence=commit.action_fence,
                now=now,
                connection=connection,
            )
            if not verification.ok or verification.expires_at is None:
                raise TutorEvidenceRejected(
                    verification.reason or "tutor_receipt_required"
                )
            if verification.expires_at <= now:
                raise TutorEvidenceRejected("receipt_expired")
            await connection.execute(
                "SELECT set_config('memoria.subject_id', $1, true)",
                commit.subject_id,
            )
            evidence_row = await connection.fetchrow(
                "SELECT * FROM tutor_practice_evidence WHERE event_id = $1",
                commit.event_id,
            )
            if evidence_row is not None:
                identity_fields = {
                    "subject_id": str(evidence_row["subject_id"]),
                    "actor_id": str(evidence_row["actor_id"]),
                    "kind": str(evidence_row["kind"]),
                    "assessment_id": (
                        str(evidence_row["assessment_id"])
                        if evidence_row["assessment_id"] is not None
                        else None
                    ),
                    "envelope_sha256": str(evidence_row["envelope_sha256"]),
                    "commit_sha256": str(evidence_row["commit_sha256"]),
                    "outcome": (
                        str(evidence_row["outcome"])
                        if evidence_row["outcome"] is not None
                        else None
                    ),
                    "skill_key": (
                        str(evidence_row["skill_key"])
                        if evidence_row["skill_key"] is not None
                        else None
                    ),
                    "session_id": str(evidence_row["session_id"]),
                    "session_revision": int(evidence_row["session_revision"]),
                }
                expected = {
                    "subject_id": commit.subject_id,
                    "actor_id": commit.actor_id,
                    "kind": commit.kind,
                    "assessment_id": commit.assessment_id,
                    "envelope_sha256": envelope_hash,
                    "commit_sha256": commit_hash,
                    "outcome": commit.outcome,
                    "skill_key": commit.skill_key,
                    "session_id": commit.session.session_id,
                    "session_revision": commit.session.revision,
                }
                if identity_fields != expected:
                    raise PracticeConflictError("idempotency_conflict")
                row = await connection.fetchrow(
                    """
                    SELECT * FROM tutor_practice_sessions
                    WHERE subject_id = $1 AND session_id = $2 FOR UPDATE
                    """,
                    commit.subject_id,
                    _uuid(commit.session.session_id, field="session_id"),
                )
                if row is None:
                    raise PracticeConflictError("practice_session_not_found")
                return self._practice_session(row)
            try:
                await connection.execute(
                    """
                    INSERT INTO tutor_practice_evidence(
                        event_id, assessment_id, kind, subject_id, actor_id,
                        envelope_json, envelope_sha256, commit_sha256,
                        outcome, skill_key,
                        session_id, session_revision, created_at
                    ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9,$10,$11,$12,$13)
                    """,
                    commit.event_id,
                    commit.assessment_id,
                    commit.kind,
                    commit.subject_id,
                    commit.actor_id,
                    json.dumps(commit.evidence_envelope, ensure_ascii=False),
                    envelope_hash,
                    commit_hash,
                    commit.outcome,
                    commit.skill_key,
                    commit.session.session_id,
                    commit.session.revision,
                    commit.occurred_at,
                )
            except asyncpg.UniqueViolationError as exc:
                raise TutorEvidenceRejected("assessment_already_consumed") from exc
            saved = await self._save_practice_session_locked(connection, commit.session)
            await connection.execute(
                """
                INSERT INTO tutor_commit_outbox(
                    event_id, kind, subject_id, actor_id,
                    archive_payload_json, status, created_at
                ) VALUES ($1,$2,$3,$4,$5::jsonb,'pending',$6)
                ON CONFLICT(event_id) DO NOTHING
                """,
                commit.event_id,
                commit.kind,
                commit.subject_id,
                commit.actor_id,
                json.dumps(commit.archive_payload, ensure_ascii=False),
                commit.occurred_at,
            )
        return saved

    async def claim_commit_events(
        self,
        *,
        worker_id: str,
        subject_id: str | None = None,
        limit: int = 64,
        lease_ttl_s: int = 60,
    ) -> tuple[PendingTutorCommit, ...]:
        """Claim outbox rows through the maintenance-role worker function."""

        pool = await self._ready_role_pool(
            dsn=self._worker_dsn,
            pool_attr="_worker_pool",
            expected_role="memoria_guardian_worker",
            operation="outbox worker",
        )
        async with pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM guardian_tutor_outbox_claim($1, $2, $3, $4)
                """,
                worker_id,
                subject_id,
                limit,
                lease_ttl_s,
            )
        return tuple(
            PendingTutorCommit(
                event_id=str(row["event_id"]),
                kind=cast(TutorAggregateKind, str(row["kind"])),
                subject_id=str(row["subject_id"]),
                actor_id=str(row["actor_id"]),
                archive_payload=dict(row["archive_payload_json"]),
                created_at=cast(datetime, row["created_at"]),
            )
            for row in rows
        )

    async def mark_commit_delivered(self, *, event_id: str) -> None:
        pool = await self._ready_role_pool(
            dsn=self._worker_dsn,
            pool_attr="_worker_pool",
            expected_role="memoria_guardian_worker",
            operation="outbox worker",
        )
        async with pool.acquire() as connection:
            await connection.execute(
                "SELECT guardian_tutor_outbox_mark_delivered($1)",
                event_id,
            )

    async def release_commit_claim(self, *, event_id: str) -> None:
        pool = await self._ready_role_pool(
            dsn=self._worker_dsn,
            pool_attr="_worker_pool",
            expected_role="memoria_guardian_worker",
            operation="outbox worker",
        )
        async with pool.acquire() as connection:
            await connection.execute(
                "SELECT guardian_tutor_outbox_release($1)",
                event_id,
            )

    async def study_progress(self, *, subject_id: str) -> StudyProgress | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('memoria.subject_id', $1, true)",
                    subject_id,
                )
                row = await connection.fetchrow(
                    "SELECT * FROM tutor_study_progress WHERE subject_id = $1",
                    subject_id,
                )
        return self._study_progress(row) if row is not None else None

    async def save_study_progress(
        self,
        progress: StudyProgress,
        *,
        rebuilt_at: datetime,
    ) -> StudyProgress:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('memoria.subject_id', $1, true)",
                    progress.subject_id,
                )
                account_id = progress.actor_id or progress.subject_id
                await connection.execute(
                    """
                    INSERT INTO tutor_study_progress(
                        account_id, subject_id, actor_id,
                        practiced_seconds, active_days_json,
                        current_streak_days, weak_points_json, mastered_skills_json,
                        source_event_ids_json, last_practiced_at, rebuilt_at
                    ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10,$11)
                    ON CONFLICT(subject_id) WHERE subject_id IS NOT NULL DO UPDATE SET
                        account_id = excluded.account_id,
                        actor_id = excluded.actor_id,
                        practiced_seconds = excluded.practiced_seconds,
                        active_days_json = excluded.active_days_json,
                        current_streak_days = excluded.current_streak_days,
                        weak_points_json = excluded.weak_points_json,
                        mastered_skills_json = excluded.mastered_skills_json,
                        source_event_ids_json = excluded.source_event_ids_json,
                        last_practiced_at = excluded.last_practiced_at,
                        rebuilt_at = excluded.rebuilt_at
                    """,
                    account_id,
                    progress.subject_id,
                    progress.actor_id,
                    progress.practiced_seconds,
                    json.dumps([value.isoformat() for value in progress.active_days]),
                    progress.current_streak_days,
                    json.dumps(progress.weak_points, ensure_ascii=False),
                    json.dumps(progress.mastered_skills, ensure_ascii=False),
                    json.dumps(progress.source_event_ids, ensure_ascii=False),
                    progress.last_practiced_at,
                    _timestamp(rebuilt_at, field="rebuilt_at"),
                )
        return progress

    async def enqueue_crisis_event(
        self,
        *,
        crisis_event_id: str,
        evidence_event_id: str,
        minor_user_id: str,
        occurred_at: datetime,
        script_version: str,
    ) -> CrisisNotificationReceipt:
        event_uuid = _uuid(crisis_event_id, field="crisis_event_id")
        occurred = _timestamp(occurred_at, field="occurred_at")
        created = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._set_api_context(
                connection,
                actor_user_id=minor_user_id,
                subject_user_id=minor_user_id,
            )
            row = await connection.fetchrow(
                """
                INSERT INTO guardian_crisis_events(
                    crisis_event_id, evidence_event_id, minor_user_id,
                    occurred_at, script_version, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT(crisis_event_id) DO NOTHING
                RETURNING *
                """,
                event_uuid,
                evidence_event_id,
                minor_user_id,
                occurred,
                script_version,
                created,
            )
            if row is None:
                row = await connection.fetchrow(
                    "SELECT * FROM guardian_crisis_events WHERE crisis_event_id = $1",
                    event_uuid,
                )
            if row is None:  # pragma: no cover
                raise RuntimeError("guardian crisis event disappeared")
            if (
                str(row["evidence_event_id"]) != evidence_event_id
                or str(row["minor_user_id"]) != minor_user_id
                or str(row["script_version"]) != script_version
            ):
                raise GuardianConflictError("crisis event id is immutable")
            guardians = await connection.fetch(
                """
                SELECT DISTINCT guardian_user_id FROM guardian_links
                WHERE minor_user_id = $1 AND status = 'active'
                """,
                minor_user_id,
            )
            for guardian in guardians:
                guardian_user_id = str(guardian["guardian_user_id"])
                notification_id = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"memoria:guardian-crisis:{event_uuid}:{guardian_user_id}",
                )
                await connection.execute(
                    """
                    SELECT guardian_enqueue_notification($1, $2, $3, $4)
                    """,
                    notification_id,
                    event_uuid,
                    guardian_user_id,
                    created,
                )
            notification_count = int(
                await connection.fetchval(
                    """
                    SELECT count(*) FROM guardian_notification_outbox
                    WHERE crisis_event_id = $1
                    """,
                    event_uuid,
                )
            )
        return CrisisNotificationReceipt(
            crisis_event_id=str(event_uuid),
            evidence_event_id=evidence_event_id,
            minor_user_id=minor_user_id,
            occurred_at=cast(datetime, row["occurred_at"]),
            script_version=script_version,
            notification_count=notification_count,
        )

    async def guardian_notifications(
        self,
        *,
        guardian_user_id: str,
        limit: int = 50,
    ) -> tuple[GuardianNotification, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("notification limit must be between 1 and 100")
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=guardian_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT outbox.*, crisis.minor_user_id
                    FROM guardian_notification_outbox outbox
                    JOIN guardian_crisis_events crisis
                      ON crisis.crisis_event_id = outbox.crisis_event_id
                    WHERE outbox.guardian_user_id = $1
                    ORDER BY outbox.created_at DESC, outbox.notification_id DESC
                    LIMIT $2
                    """,
                    guardian_user_id,
                    limit,
                )
        return tuple(self._notification(row) for row in rows)

    async def export_for_account(self, *, account_id: str) -> dict[str, object]:
        maintenance_pool = await self._ready_role_pool(
            dsn=self._maintenance_dsn,
            pool_attr="_maintenance_pool",
            expected_role="memoria_guardian_maintenance",
            operation="account export",
        )
        async with maintenance_pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('memoria.guardian_maintenance_scope', '1', true)"
            )
            links = await connection.fetch(
                """
                SELECT link_id, guardian_user_id, minor_user_id, relation, status,
                       verified_via, binding_expires_at, created_at, activated_at, revoked_at
                FROM guardian_links
                WHERE guardian_user_id = $1 OR minor_user_id = $1
                ORDER BY created_at, link_id
                """,
                account_id,
            )
            consents = await connection.fetch(
                """
                SELECT consent.consent_id, consent.link_id, consent.consent_kind,
                       consent.policy_version, consent.granted_at, consent.expires_at,
                       consent.revoked_at,
                       consent.evidence_event_id, consent.revocation_evidence_event_id
                FROM guardian_consents consent
                JOIN guardian_links link ON link.link_id = consent.link_id
                WHERE link.guardian_user_id = $1 OR link.minor_user_id = $1
                ORDER BY consent.granted_at, consent.consent_id
                """,
                account_id,
            )
            crisis_events = await connection.fetch(
                """
                SELECT * FROM guardian_crisis_events
                WHERE minor_user_id = $1 ORDER BY occurred_at, crisis_event_id
                """,
                account_id,
            )
            notifications = await connection.fetch(
                """
                SELECT outbox.* FROM guardian_notification_outbox outbox
                JOIN guardian_crisis_events crisis
                  ON crisis.crisis_event_id = outbox.crisis_event_id
                WHERE outbox.guardian_user_id = $1 OR crisis.minor_user_id = $1
                ORDER BY outbox.created_at, outbox.notification_id
                """,
                account_id,
            )
            corpus_samples = await connection.fetch(
                """
                SELECT sample_id, minor_user_id, consent_id, source_event_id,
                       media_type, byte_count, content_sha256,
                       created_at, expires_at, deleted_at
                FROM guardian_corpus_samples
                WHERE minor_user_id = $1 ORDER BY created_at, sample_id
                """,
                account_id,
            )
        async with maintenance_pool.acquire() as connection:
            row = await connection.fetchval(
                "SELECT guardian_tutor_account_scope_export($1)",
                account_id,
            )
        data = json.loads(row) if row else {}
        return {
            "links": [dict(row) for row in links],
            "consents": [dict(row) for row in consents],
            "tutor_practice_sessions": list(
                data.get("tutor_practice_sessions") or []
            ),
            "tutor_study_progress": data.get("tutor_study_progress"),
            "crisis_events": [dict(row) for row in crisis_events],
            "guardian_notifications": [dict(row) for row in notifications],
            "corpus_samples": [dict(row) for row in corpus_samples],
        }

    async def delete_for_account(self, *, account_id: str) -> dict[str, int]:
        maintenance_pool = await self._ready_role_pool(
            dsn=self._maintenance_dsn,
            pool_attr="_maintenance_pool",
            expected_role="memoria_guardian_maintenance",
            operation="account deletion",
        )
        async with maintenance_pool.acquire() as connection:
            tutor_result = await connection.fetchval(
                "SELECT guardian_tutor_account_scope_delete($1)",
                account_id,
            )
        async with maintenance_pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('memoria.guardian_maintenance_scope', '1', true)"
            )
            await connection.execute(
                "SELECT set_config('app.guardian_account_deletion', '1', true)"
            )
            notification_result = await connection.execute(
                """
                DELETE FROM guardian_notification_outbox outbox
                USING guardian_crisis_events crisis
                WHERE outbox.crisis_event_id = crisis.crisis_event_id
                  AND (outbox.guardian_user_id = $1 OR crisis.minor_user_id = $1)
                """,
                account_id,
            )
            crisis_result = await connection.execute(
                "DELETE FROM guardian_crisis_events WHERE minor_user_id = $1",
                account_id,
            )
            corpus_result = await connection.execute(
                """
                DELETE FROM guardian_corpus_samples sample
                USING guardian_consents consent, guardian_links link
                WHERE sample.consent_id = consent.consent_id
                  AND consent.link_id = link.link_id
                  AND (sample.minor_user_id = $1
                       OR link.guardian_user_id = $1 OR link.minor_user_id = $1)
                """,
                account_id,
            )
            consent_result = await connection.execute(
                """
                DELETE FROM guardian_consents consent
                USING guardian_links link
                WHERE consent.link_id = link.link_id
                  AND (link.guardian_user_id = $1 OR link.minor_user_id = $1)
                """,
                account_id,
            )
            link_result = await connection.execute(
                """
                DELETE FROM guardian_links
                WHERE guardian_user_id = $1 OR minor_user_id = $1
                """,
                account_id,
            )
        return {
            "links": int(link_result.rsplit(" ", 1)[-1]),
            "consents": int(consent_result.rsplit(" ", 1)[-1]),
            "tutor_practice_sessions": int(
                json.loads(tutor_result).get("tutor_practice_sessions") or 0
            ),
            "tutor_study_progress": int(
                json.loads(tutor_result).get("tutor_study_progress") or 0
            ),
            "crisis_events": int(crisis_result.rsplit(" ", 1)[-1]),
            "guardian_notifications": int(notification_result.rsplit(" ", 1)[-1]),
            "corpus_samples": int(corpus_result.rsplit(" ", 1)[-1]),
        }

    async def remaining_account_rows(self, *, account_id: str) -> dict[str, int]:
        maintenance_pool = await self._ready_role_pool(
            dsn=self._maintenance_dsn,
            pool_attr="_maintenance_pool",
            expected_role="memoria_guardian_maintenance",
            operation="account row accounting",
        )
        async with maintenance_pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('memoria.guardian_maintenance_scope', '1', true)"
            )
            links = int(
                await connection.fetchval(
                    """
                    SELECT count(*) FROM guardian_links
                    WHERE guardian_user_id = $1 OR minor_user_id = $1
                    """,
                    account_id,
                )
            )
            consents = int(
                await connection.fetchval(
                    """
                    SELECT count(*) FROM guardian_consents consent
                    JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE link.guardian_user_id = $1 OR link.minor_user_id = $1
                    """,
                    account_id,
                )
            )
            crisis_events = int(
                await connection.fetchval(
                    "SELECT count(*) FROM guardian_crisis_events WHERE minor_user_id = $1",
                    account_id,
                )
            )
            notifications = int(
                await connection.fetchval(
                    """
                    SELECT count(*) FROM guardian_notification_outbox outbox
                    JOIN guardian_crisis_events crisis
                      ON crisis.crisis_event_id = outbox.crisis_event_id
                    WHERE outbox.guardian_user_id = $1 OR crisis.minor_user_id = $1
                    """,
                    account_id,
                )
            )
            corpus_samples = int(
                await connection.fetchval(
                    """
                    SELECT count(*) FROM guardian_corpus_samples sample
                    JOIN guardian_consents consent ON consent.consent_id = sample.consent_id
                    JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE sample.minor_user_id = $1
                       OR link.guardian_user_id = $1 OR link.minor_user_id = $1
                    """,
                    account_id,
                )
            )
        async with maintenance_pool.acquire() as connection:
            tutor_result = await connection.fetchval(
                "SELECT guardian_tutor_account_scope_remaining($1)",
                account_id,
            )
        practice_sessions = int(
            json.loads(tutor_result).get("tutor_practice_sessions") or 0
        )
        study_progress = int(
            json.loads(tutor_result).get("tutor_study_progress") or 0
        )
        return {
            key: value
            for key, value in {
                "links": links,
                "consents": consents,
                "tutor_practice_sessions": practice_sessions,
                "tutor_study_progress": study_progress,
                "crisis_events": crisis_events,
                "guardian_notifications": notifications,
                "corpus_samples": corpus_samples,
            }.items()
            if value
        }

    async def related_minor_accounts(self, *, guardian_user_id: str) -> tuple[str, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await self._set_api_context(
                    connection,
                    actor_user_id=guardian_user_id,
                    subject_user_id=guardian_user_id,
                )
                rows = await connection.fetch(
                    """
                    SELECT DISTINCT minor_user_id FROM guardian_links
                    WHERE guardian_user_id = $1 AND status = 'active'
                    ORDER BY minor_user_id
                    """,
                    guardian_user_id,
                )
        return tuple(str(row["minor_user_id"]) for row in rows)


__all__ = ["PostgresGuardianStore"]
