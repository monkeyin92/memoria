"""SQLite development adapter for guardian links and versioned consents."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import cast

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guardian_links (
    link_id TEXT PRIMARY KEY,
    guardian_user_id TEXT NOT NULL,
    minor_user_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('parent', 'legal_guardian')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked')),
    verified_via TEXT NOT NULL CHECK (
        verified_via IN ('wechat_identity', 'manual_review')
    ),
    binding_code_hash TEXT NOT NULL CHECK (length(binding_code_hash) = 64),
    binding_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    revoked_at TEXT,
    CHECK (guardian_user_id <> minor_user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_links_live_pair
ON guardian_links(guardian_user_id, minor_user_id)
WHERE status IN ('pending', 'active');

CREATE INDEX IF NOT EXISTS idx_guardian_links_actor
ON guardian_links(guardian_user_id, minor_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS guardian_consents (
    consent_id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES guardian_links(link_id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (consent_kind IN (
        'minor_voice_session', 'memory_retention',
        'weekly_report', 'corpus_recording'
    )),
    policy_version TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    evidence_event_id TEXT NOT NULL UNIQUE,
    revocation_evidence_event_id TEXT UNIQUE,
    CHECK (
        (revoked_at IS NULL AND revocation_evidence_event_id IS NULL)
        OR (revoked_at IS NOT NULL AND revocation_evidence_event_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_consents_active_kind
ON guardian_consents(link_id, consent_kind)
WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_guardian_consents_link
ON guardian_consents(link_id, granted_at DESC);

CREATE TABLE IF NOT EXISTS guardian_corpus_samples (
    sample_id TEXT PRIMARY KEY,
    minor_user_id TEXT NOT NULL,
    consent_id TEXT NOT NULL REFERENCES guardian_consents(consent_id),
    source_event_id TEXT NOT NULL UNIQUE,
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count > 0),
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    encryption_key_version TEXT NOT NULL,
    object_backend TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_guardian_corpus_expiry
ON guardian_corpus_samples(deleted_at, expires_at, sample_id);

CREATE INDEX IF NOT EXISTS idx_guardian_corpus_minor
ON guardian_corpus_samples(minor_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tutor_practice_sessions (
    session_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    subject_id TEXT,
    actor_id TEXT,
    voice_session_id TEXT,
    focus TEXT NOT NULL CHECK (focus IN ('tutor_english', 'tutor_homework')),
    task_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'paused', 'completed')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    event_ids_json TEXT NOT NULL,
    practiced_seconds INTEGER NOT NULL DEFAULT 0 CHECK (practiced_seconds >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_practice_account_updated
ON tutor_practice_sessions(account_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS tutor_study_progress (
    account_id TEXT PRIMARY KEY,
    subject_id TEXT,
    actor_id TEXT,
    practiced_seconds INTEGER NOT NULL DEFAULT 0 CHECK (practiced_seconds >= 0),
    active_days_json TEXT NOT NULL,
    current_streak_days INTEGER NOT NULL DEFAULT 0 CHECK (current_streak_days >= 0),
    weak_points_json TEXT NOT NULL,
    mastered_skills_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL,
    last_practiced_at TEXT,
    rebuilt_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tutor_practice_evidence (
    event_id TEXT PRIMARY KEY,
    assessment_id TEXT UNIQUE,
    kind TEXT NOT NULL CHECK (
        kind IN ('tutor.practice_turn_recorded', 'tutor.practice_completed')
    ),
    subject_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    envelope_sha256 TEXT NOT NULL,
    commit_sha256 TEXT NOT NULL,
    outcome TEXT,
    skill_key TEXT,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_evidence_subject_created
ON tutor_practice_evidence(subject_id, created_at);

CREATE TABLE IF NOT EXISTS tutor_commit_outbox (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (
        kind IN ('tutor.practice_turn_recorded', 'tutor.practice_completed')
    ),
    subject_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    archive_payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered')),
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_at TEXT,
    lease_until TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_outbox_pending
ON tutor_commit_outbox(status, subject_id, created_at);

CREATE TABLE IF NOT EXISTS guardian_crisis_events (
    crisis_event_id TEXT PRIMARY KEY,
    evidence_event_id TEXT NOT NULL UNIQUE,
    minor_user_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    script_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_guardian_crisis_minor_occurred
ON guardian_crisis_events(minor_user_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS guardian_notification_outbox (
    notification_id TEXT PRIMARY KEY,
    crisis_event_id TEXT NOT NULL REFERENCES guardian_crisis_events(crisis_event_id)
        ON DELETE CASCADE,
    guardian_user_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel = 'wechat_subscription'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    last_error_code TEXT,
    UNIQUE(crisis_event_id, guardian_user_id)
);

CREATE INDEX IF NOT EXISTS idx_guardian_notification_recipient_status
ON guardian_notification_outbox(guardian_user_id, status, created_at DESC);
"""


def _timestamp(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("binding_code_hash must be a SHA-256 hex digest")
    return normalized


def _statuses(values: tuple[GuardianLinkStatus, ...]) -> tuple[GuardianLinkStatus, ...]:
    if not values or any(value not in {"pending", "active", "revoked"} for value in values):
        raise ValueError("guardian link statuses are invalid")
    return values


class SqliteGuardianStore:
    """Small standalone store; production uses the FORCE-RLS PostgreSQL adapter."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                self._migrate_tutor_subject_scope(connection)
                columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(guardian_consents)")
                }
                if "expires_at" not in columns:
                    connection.execute(
                        "ALTER TABLE guardian_consents ADD COLUMN expires_at TEXT"
                    )
                connection.execute("DROP INDEX IF EXISTS idx_guardian_consents_active_kind")
                connection.execute(
                    """
                    CREATE UNIQUE INDEX idx_guardian_consents_active_kind
                    ON guardian_consents(link_id, consent_kind)
                    WHERE revoked_at IS NULL AND expires_at IS NULL
                    """
                )
            self._initialized = True

    @staticmethod
    def _migrate_tutor_subject_scope(connection: sqlite3.Connection) -> None:
        """Add subject-scope columns and quarantine legacy ownership.

        Legacy rows (created before the subject contract) keep their
        ``account_id`` but receive no ``subject_id``: they are quarantined and
        cannot enter any subject-scoped read or projection.  ``actor_id`` is
        backfilled from the legacy operating account so account-scoped
        export/delete still covers them.
        """

        sessions_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tutor_practice_sessions)")
        }
        if "subject_id" not in sessions_columns:
            connection.execute(
                "ALTER TABLE tutor_practice_sessions ADD COLUMN subject_id TEXT"
            )
        if "actor_id" not in sessions_columns:
            connection.execute(
                "ALTER TABLE tutor_practice_sessions ADD COLUMN actor_id TEXT"
            )
        if "voice_session_id" not in sessions_columns:
            connection.execute(
                "ALTER TABLE tutor_practice_sessions ADD COLUMN voice_session_id TEXT"
            )
        connection.execute(
            "UPDATE tutor_practice_sessions SET actor_id = account_id WHERE actor_id IS NULL"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tutor_practice_subject_updated
            ON tutor_practice_sessions(subject_id, updated_at DESC)
            """
        )
        progress_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tutor_study_progress)")
        }
        if "subject_id" not in progress_columns:
            connection.execute(
                "ALTER TABLE tutor_study_progress ADD COLUMN subject_id TEXT"
            )
        if "actor_id" not in progress_columns:
            connection.execute("ALTER TABLE tutor_study_progress ADD COLUMN actor_id TEXT")
        connection.execute(
            "UPDATE tutor_study_progress SET actor_id = account_id WHERE actor_id IS NULL"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tutor_progress_subject
            ON tutor_study_progress(subject_id) WHERE subject_id IS NOT NULL
            """
        )
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS tutor_sessions_authority_insert_guard
            BEFORE INSERT ON tutor_practice_sessions
            FOR EACH ROW WHEN (
                NEW.subject_id IS NULL OR trim(NEW.subject_id) = ''
                OR NEW.actor_id IS NULL OR trim(NEW.actor_id) = ''
                OR NEW.voice_session_id IS NULL OR trim(NEW.voice_session_id) = ''
            )
            BEGIN
                SELECT RAISE(ABORT, 'tutor authority columns are required');
            END;

            CREATE TRIGGER IF NOT EXISTS tutor_sessions_authority_update_guard
            BEFORE UPDATE OF subject_id, actor_id, voice_session_id
            ON tutor_practice_sessions
            FOR EACH ROW WHEN (
                NEW.subject_id IS NULL OR trim(NEW.subject_id) = ''
                OR NEW.actor_id IS NULL OR trim(NEW.actor_id) = ''
                OR NEW.voice_session_id IS NULL OR trim(NEW.voice_session_id) = ''
            )
            BEGIN
                SELECT RAISE(ABORT, 'tutor authority columns are required');
            END;

            CREATE TRIGGER IF NOT EXISTS tutor_progress_authority_insert_guard
            BEFORE INSERT ON tutor_study_progress
            FOR EACH ROW WHEN (
                NEW.subject_id IS NULL OR trim(NEW.subject_id) = ''
                OR NEW.actor_id IS NULL OR trim(NEW.actor_id) = ''
            )
            BEGIN
                SELECT RAISE(ABORT, 'tutor authority columns are required');
            END;

            CREATE TRIGGER IF NOT EXISTS tutor_progress_authority_update_guard
            BEFORE UPDATE OF subject_id, actor_id ON tutor_study_progress
            FOR EACH ROW WHEN (
                NEW.subject_id IS NULL OR trim(NEW.subject_id) = ''
                OR NEW.actor_id IS NULL OR trim(NEW.actor_id) = ''
            )
            BEGIN
                SELECT RAISE(ABORT, 'tutor authority columns are required');
            END;

            CREATE TRIGGER IF NOT EXISTS tutor_evidence_authority_insert_guard
            BEFORE INSERT ON tutor_practice_evidence
            FOR EACH ROW WHEN (
                NEW.subject_id IS NULL OR trim(NEW.subject_id) = ''
                OR NEW.actor_id IS NULL OR trim(NEW.actor_id) = ''
            )
            BEGIN
                SELECT RAISE(ABORT, 'tutor authority columns are required');
            END;
            """
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def close(self) -> None:
        return None

    def _ready(self) -> None:
        self.initialize()

    @staticmethod
    def _link(row: sqlite3.Row) -> GuardianLink:
        return GuardianLink(
            link_id=str(row["link_id"]),
            guardian_user_id=str(row["guardian_user_id"]),
            minor_user_id=str(row["minor_user_id"]),
            relation=cast(Relation, str(row["relation"])),
            status=cast(GuardianLinkStatus, str(row["status"])),
            verified_via=cast(VerifiedVia, str(row["verified_via"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            binding_expires_at=datetime.fromisoformat(str(row["binding_expires_at"])),
            activated_at=(
                datetime.fromisoformat(str(row["activated_at"]))
                if row["activated_at"] is not None
                else None
            ),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _consent(row: sqlite3.Row) -> ConsentRecord:
        return ConsentRecord(
            consent_id=str(row["consent_id"]),
            link_id=str(row["link_id"]),
            consent_kind=cast(ConsentKind, str(row["consent_kind"])),
            policy_version=str(row["policy_version"]),
            granted_at=datetime.fromisoformat(str(row["granted_at"])),
            evidence_event_id=str(row["evidence_event_id"]),
            expires_at=(
                datetime.fromisoformat(str(row["expires_at"]))
                if row["expires_at"] is not None
                else None
            ),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
            revocation_evidence_event_id=(
                str(row["revocation_evidence_event_id"])
                if row["revocation_evidence_event_id"] is not None
                else None
            ),
        )

    @staticmethod
    def _corpus_sample(row: sqlite3.Row) -> CorpusSample:
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
            created_at=datetime.fromisoformat(str(row["created_at"])),
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            deleted_at=(
                datetime.fromisoformat(str(row["deleted_at"]))
                if row["deleted_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _practice_session(row: sqlite3.Row) -> PracticeSession:
        event_ids = json.loads(str(row["event_ids_json"]))
        if not isinstance(event_ids, list) or any(
            not isinstance(value, str) for value in event_ids
        ):
            raise RuntimeError("tutor practice event ids are invalid")
        subject_id = row["subject_id"]
        if not isinstance(subject_id, str) or not subject_id:
            raise RuntimeError("tutor practice session has no authoritative subject")
        return PracticeSession(
            session_id=str(row["session_id"]),
            subject_id=subject_id,
            actor_id=str(row["actor_id"] or row["account_id"]),
            voice_session_id=str(row["voice_session_id"] or ""),
            focus=cast(TutorFocus, str(row["focus"])),
            task_id=str(row["task_id"]),
            status=cast(PracticeStatus, str(row["status"])),
            revision=int(row["revision"]),
            event_ids=tuple(event_ids),
            practiced_seconds=int(row["practiced_seconds"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _study_progress(row: sqlite3.Row) -> StudyProgress:
        weak_points = json.loads(str(row["weak_points_json"]))
        if not isinstance(weak_points, list):
            raise RuntimeError("tutor weak-point projection is invalid")
        subject_id = row["subject_id"]
        if not isinstance(subject_id, str) or not subject_id:
            raise RuntimeError("tutor study progress has no authoritative subject")
        return StudyProgress(
            subject_id=subject_id,
            actor_id=str(row["actor_id"] or row["account_id"]),
            practiced_seconds=int(row["practiced_seconds"]),
            active_days=tuple(
                date.fromisoformat(str(value))
                for value in json.loads(str(row["active_days_json"]))
            ),
            current_streak_days=int(row["current_streak_days"]),
            weak_points=tuple((str(value[0]), int(value[1])) for value in weak_points),
            mastered_skills=tuple(
                str(value) for value in json.loads(str(row["mastered_skills_json"]))
            ),
            source_event_ids=tuple(
                str(value) for value in json.loads(str(row["source_event_ids_json"]))
            ),
            last_practiced_at=(
                datetime.fromisoformat(str(row["last_practiced_at"]))
                if row["last_practiced_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _notification(row: sqlite3.Row) -> GuardianNotification:
        return GuardianNotification(
            notification_id=str(row["notification_id"]),
            crisis_event_id=str(row["crisis_event_id"]),
            guardian_user_id=str(row["guardian_user_id"]),
            minor_user_id=str(row["minor_user_id"]),
            channel="wechat_subscription",
            status=cast(NotificationStatus, str(row["status"])),
            attempts=int(row["attempts"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            delivered_at=(
                datetime.fromisoformat(str(row["delivered_at"]))
                if row["delivered_at"] is not None
                else None
            ),
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
        self._ready()
        created_at = _timestamp(now, field="now")
        expires_at = _timestamp(binding_expires_at, field="binding_expires_at")
        if expires_at <= created_at:
            raise ValueError("binding code expiry must follow link creation")
        resolved_link_id = str(uuid.UUID(link_id)) if link_id is not None else str(uuid.uuid4())
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO guardian_links(
                        link_id, guardian_user_id, minor_user_id, relation, status,
                        verified_via, binding_code_hash, binding_expires_at, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        resolved_link_id,
                        guardian_user_id,
                        minor_user_id,
                        relation,
                        verified_via,
                        _hash(binding_code_hash),
                        expires_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise GuardianConflictError("a live guardian link already exists") from exc
        return await self.get_link(
            link_id=resolved_link_id,
            actor_user_id=guardian_user_id,
        )

    async def verify_binding_code(
        self,
        *,
        link_id: str,
        minor_user_id: str,
        binding_code_hash: str,
        now: datetime,
    ) -> GuardianLink:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM guardian_links
                WHERE link_id = ? AND minor_user_id = ? AND status = 'pending'
                  AND binding_code_hash = ? AND binding_expires_at > ?
                """,
                (
                    link_id,
                    minor_user_id,
                    _hash(binding_code_hash),
                    _timestamp(now, field="now").isoformat(),
                ),
            ).fetchone()
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
        self._ready()
        confirmed_at = _timestamp(now, field="now")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE guardian_links
                SET status = 'active', activated_at = ?
                WHERE link_id = ? AND minor_user_id = ? AND status = 'pending'
                  AND binding_code_hash = ? AND binding_expires_at > ?
                """,
                (
                    confirmed_at.isoformat(),
                    link_id,
                    minor_user_id,
                    _hash(binding_code_hash),
                    confirmed_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM guardian_links WHERE link_id = ?",
                (link_id,),
            ).fetchone()
        if cursor.rowcount != 1 or row is None:
            raise GuardianAccessDeniedError("binding code is invalid or expired")
        return self._link(row)

    async def get_link(self, *, link_id: str, actor_user_id: str) -> GuardianLink:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM guardian_links
                WHERE link_id = ? AND (guardian_user_id = ? OR minor_user_id = ?)
                """,
                (link_id, actor_user_id, actor_user_id),
            ).fetchone()
        if row is None:
            raise GuardianNotFoundError("guardian link not found")
        return self._link(row)

    async def active_link(
        self,
        *,
        guardian_user_id: str,
        minor_user_id: str,
    ) -> GuardianLink | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM guardian_links
                WHERE guardian_user_id = ? AND minor_user_id = ? AND status = 'active'
                ORDER BY activated_at DESC LIMIT 1
                """,
                (guardian_user_id, minor_user_id),
            ).fetchone()
        return self._link(row) if row is not None else None

    async def list_links(
        self,
        *,
        guardian_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]:
        self._ready()
        values = _statuses(statuses)
        placeholders = ",".join("?" for _ in values)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM guardian_links
                WHERE guardian_user_id = ? AND status IN ({placeholders})
                ORDER BY created_at DESC
                """,  # noqa: S608 - placeholders are generated, not user-controlled
                (guardian_user_id, *values),
            ).fetchall()
        return tuple(self._link(row) for row in rows)

    async def list_links_for_actor(
        self,
        *,
        actor_user_id: str,
        statuses: tuple[GuardianLinkStatus, ...] = ("pending", "active"),
    ) -> tuple[GuardianLink, ...]:
        self._ready()
        values = _statuses(statuses)
        placeholders = ",".join("?" for _ in values)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM guardian_links
                WHERE (guardian_user_id = ? OR minor_user_id = ?)
                  AND status IN ({placeholders})
                ORDER BY created_at DESC
                """,  # noqa: S608 - placeholders are generated, not user-controlled
                (actor_user_id, actor_user_id, *values),
            ).fetchall()
        return tuple(self._link(row) for row in rows)

    async def active_guardian_links(
        self,
        *,
        minor_user_id: str,
    ) -> tuple[GuardianLink, ...]:
        self._ready()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM guardian_links
                WHERE minor_user_id = ? AND status = 'active'
                ORDER BY activated_at DESC
                """,
                (minor_user_id,),
            ).fetchall()
        return tuple(self._link(row) for row in rows)

    async def grant_consent(
        self,
        record: ConsentRecord,
        *,
        actor_user_id: str | None = None,
    ) -> ConsentRecord:
        self._ready()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT status FROM guardian_links WHERE link_id = ?",
                    (record.link_id,),
                ).fetchone()
                if active is None or str(active["status"]) != "active":
                    raise GuardianAccessDeniedError("an active guardian link is required")
                conflicting = connection.execute(
                    """
                    SELECT consent_id FROM guardian_consents
                    WHERE link_id = ? AND consent_kind = ? AND revoked_at IS NULL
                      AND (expires_at IS NULL OR expires_at > ?)
                      AND consent_id <> ?
                    LIMIT 1
                    """,
                    (
                        record.link_id,
                        record.consent_kind,
                        record.granted_at.isoformat(),
                        record.consent_id,
                    ),
                ).fetchone()
                if conflicting is not None:
                    raise GuardianConflictError(
                        "an active consent already exists for this capability"
                    )
                connection.execute(
                    """
                    INSERT INTO guardian_consents(
                        consent_id, link_id, consent_kind, policy_version,
                        granted_at, expires_at, evidence_event_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(consent_id) DO NOTHING
                    """,
                    (
                        record.consent_id,
                        record.link_id,
                        record.consent_kind,
                        record.policy_version,
                        record.granted_at.isoformat(),
                        record.expires_at.isoformat() if record.expires_at else None,
                        record.evidence_event_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM guardian_consents WHERE consent_id = ?",
                    (record.consent_id,),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise GuardianConflictError(
                "an active consent already exists for this capability"
            ) from exc
        if row is None:  # pragma: no cover
            raise RuntimeError("guardian consent disappeared")
        current = self._consent(row)
        if current != record:
            raise GuardianConflictError("consent id is immutable")
        return current

    async def get_consent(
        self,
        *,
        consent_id: str,
        actor_user_id: str,
    ) -> ConsentRecord:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT consent.* FROM guardian_consents consent
                JOIN guardian_links link ON link.link_id = consent.link_id
                WHERE consent.consent_id = ?
                  AND (link.guardian_user_id = ? OR link.minor_user_id = ?)
                """,
                (consent_id, actor_user_id, actor_user_id),
            ).fetchone()
        if row is None:
            raise GuardianNotFoundError("guardian consent not found")
        return self._consent(row)

    async def list_consents(
        self,
        *,
        link_id: str,
        actor_user_id: str,
    ) -> tuple[ConsentRecord, ...]:
        self._ready()
        await self.get_link(link_id=link_id, actor_user_id=actor_user_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM guardian_consents
                WHERE link_id = ? ORDER BY granted_at, consent_id
                """,
                (link_id,),
            ).fetchall()
        return tuple(self._consent(row) for row in rows)

    async def revoke_consent(
        self,
        *,
        consent_id: str,
        guardian_user_id: str,
        revoked_at: datetime,
        revocation_evidence_event_id: str,
    ) -> ConsentRecord:
        self._ready()
        timestamp = _timestamp(revoked_at, field="revoked_at")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE guardian_consents
                SET revoked_at = ?, revocation_evidence_event_id = ?
                WHERE consent_id = ? AND revoked_at IS NULL
                  AND link_id IN (
                      SELECT link_id FROM guardian_links WHERE guardian_user_id = ?
                  )
                """,
                (
                    timestamp.isoformat(),
                    revocation_evidence_event_id,
                    consent_id,
                    guardian_user_id,
                ),
            )
            row = connection.execute(
                """
                SELECT consent.* FROM guardian_consents consent
                JOIN guardian_links link ON link.link_id = consent.link_id
                WHERE consent.consent_id = ? AND link.guardian_user_id = ?
                """,
                (consent_id, guardian_user_id),
            ).fetchone()
        if row is None:
            raise GuardianNotFoundError("guardian consent not found")
        current = self._consent(row)
        if cursor.rowcount != 1 and (
            current.revocation_evidence_event_id != revocation_evidence_event_id
        ):
            raise GuardianConflictError("consent was already revoked")
        return current

    async def active_consent(
        self,
        *,
        minor_user_id: str,
        consent_kind: ConsentKind,
    ) -> ConsentRecord | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT consent.* FROM guardian_consents consent
                JOIN guardian_links link ON link.link_id = consent.link_id
                WHERE link.minor_user_id = ? AND link.status = 'active'
                  AND consent.consent_kind = ? AND consent.revoked_at IS NULL
                  AND (consent.expires_at IS NULL OR consent.expires_at > ?)
                ORDER BY consent.granted_at DESC LIMIT 1
                """,
                (minor_user_id, consent_kind, datetime.now(UTC).isoformat()),
            ).fetchone()
        return self._consent(row) if row is not None else None

    async def corpus_sample_by_event(
        self,
        *,
        minor_user_id: str,
        source_event_id: str,
    ) -> CorpusSample | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM guardian_corpus_samples
                WHERE minor_user_id = ? AND source_event_id = ?
                """,
                (minor_user_id, source_event_id),
            ).fetchone()
        return self._corpus_sample(row) if row is not None else None

    async def record_corpus_sample(self, sample: CorpusSample) -> CorpusSample:
        self._ready()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM guardian_corpus_samples WHERE sample_id = ?",
                    (sample.sample_id,),
                ).fetchone()
                if row is None:
                    authorized = connection.execute(
                        """
                        SELECT 1 FROM guardian_consents consent
                        JOIN guardian_links link ON link.link_id = consent.link_id
                        WHERE consent.consent_id = ?
                          AND consent.consent_kind = 'corpus_recording'
                          AND consent.revoked_at IS NULL
                          AND consent.expires_at > ?
                          AND link.minor_user_id = ?
                          AND link.status = 'active'
                        """,
                        (
                            sample.consent_id,
                            datetime.now(UTC).isoformat(),
                            sample.minor_user_id,
                        ),
                    ).fetchone()
                    if authorized is None:
                        raise CorpusConsentInactiveError(
                            "corpus consent became inactive before persistence"
                        )
                    active_count = int(
                        connection.execute(
                            """
                            SELECT count(*) FROM guardian_corpus_samples
                            WHERE minor_user_id = ? AND deleted_at IS NULL
                              AND expires_at > ?
                            """,
                            (sample.minor_user_id, datetime.now(UTC).isoformat()),
                        ).fetchone()[0]
                    )
                    if active_count >= MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR:
                        raise CorpusSampleLimitError(
                            "active corpus sample limit reached"
                        )
                    connection.execute(
                        """
                        INSERT INTO guardian_corpus_samples(
                            sample_id, minor_user_id, consent_id, source_event_id,
                            object_key, media_type, byte_count, content_sha256,
                            encryption_key_version, object_backend, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sample.sample_id,
                            sample.minor_user_id,
                            sample.consent_id,
                            sample.source_event_id,
                            sample.reference.object_key,
                            sample.reference.media_type,
                            sample.reference.byte_count,
                            sample.reference.content_sha256,
                            sample.reference.encryption_key_version,
                            sample.reference.backend,
                            sample.created_at.isoformat(),
                            sample.expires_at.isoformat(),
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM guardian_corpus_samples WHERE sample_id = ?",
                        (sample.sample_id,),
                    ).fetchone()
        except sqlite3.IntegrityError as exc:
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
        self._ready()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM guardian_corpus_samples
                WHERE minor_user_id = ? AND (? OR deleted_at IS NULL)
                ORDER BY created_at, sample_id
                """,
                (minor_user_id, int(include_deleted)),
            ).fetchall()
        return tuple(self._corpus_sample(row) for row in rows)

    async def expired_corpus_samples(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[CorpusSample, ...]:
        self._ready()
        timestamp = _timestamp(now, field="now")
        if not 1 <= limit <= 1000:
            raise ValueError("corpus purge limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM guardian_corpus_samples
                WHERE deleted_at IS NULL AND expires_at <= ?
                ORDER BY expires_at, sample_id LIMIT ?
                """,
                (timestamp.isoformat(), limit),
            ).fetchall()
        return tuple(self._corpus_sample(row) for row in rows)

    async def mark_corpus_sample_deleted(
        self,
        *,
        sample_id: str,
        deleted_at: datetime,
    ) -> CorpusSample:
        self._ready()
        timestamp = _timestamp(deleted_at, field="deleted_at")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE guardian_corpus_samples SET deleted_at = COALESCE(deleted_at, ?)
                WHERE sample_id = ?
                """,
                (timestamp.isoformat(), sample_id),
            )
            row = connection.execute(
                "SELECT * FROM guardian_corpus_samples WHERE sample_id = ?",
                (sample_id,),
            ).fetchone()
        if row is None:
            raise GuardianNotFoundError("corpus sample not found")
        return self._corpus_sample(row)

    async def practice_session(
        self,
        *,
        subject_id: str,
        session_id: str,
    ) -> PracticeSession | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM tutor_practice_sessions
                WHERE subject_id = ? AND session_id = ?
                """,
                (subject_id, session_id),
            ).fetchone()
        return self._practice_session(row) if row is not None else None

    async def save_practice_session(self, session: PracticeSession) -> PracticeSession:
        self._ready()
        if session.created_at is None or session.updated_at is None:
            raise ValueError("persisted practice sessions require timestamps")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            saved = self._save_practice_session_locked(connection, session)
        return saved

    @staticmethod
    def _load_session_locked(
        connection: sqlite3.Connection,
        subject_id: str,
        session_id: str,
    ) -> PracticeSession | None:
        row = connection.execute(
            """
            SELECT * FROM tutor_practice_sessions
            WHERE subject_id = ? AND session_id = ?
            """,
            (subject_id, session_id),
        ).fetchone()
        return SqliteGuardianStore._practice_session(row) if row is not None else None

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
        failure rolls the whole transaction back, so no partial state (token
        eaten without a session advance, evidence without a session, archive
        event without a commit) can be observed.
        """

        self._ready()
        if commit.session.created_at is None or commit.session.updated_at is None:
            raise ValueError("persisted practice sessions require timestamps")
        envelope_hash = hashlib.sha256(
            json.dumps(commit.evidence_envelope, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()
        commit_hash = commit.commit_sha256()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # The receipt is re-verified INSIDE this transaction: the write
            # only proceeds while the action receipt (and whatever current
            # consent/binding state the Policy verifier consults) is still
            # valid.  Any revocation or mismatch rolls the whole commit back.
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
            evidence_row = connection.execute(
                "SELECT * FROM tutor_practice_evidence WHERE event_id = ?",
                (commit.event_id,),
            ).fetchone()
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
                saved = self._load_session_locked(
                    connection,
                    commit.subject_id,
                    commit.session.session_id,
                )
                if saved is None:
                    raise PracticeConflictError("practice_session_not_found")
                return saved
            try:
                connection.execute(
                    """
                    INSERT INTO tutor_practice_evidence(
                        event_id, assessment_id, kind, subject_id, actor_id,
                        envelope_json, envelope_sha256, commit_sha256,
                        outcome, skill_key,
                        session_id, session_revision, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
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
                        commit.occurred_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TutorEvidenceRejected("assessment_already_consumed") from exc
            saved = self._save_practice_session_locked(connection, commit.session)
            connection.execute(
                """
                INSERT INTO tutor_commit_outbox(
                    event_id, kind, subject_id, actor_id,
                    archive_payload_json, status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(event_id) DO NOTHING
                """,
                (
                    commit.event_id,
                    commit.kind,
                    commit.subject_id,
                    commit.actor_id,
                    json.dumps(commit.archive_payload, ensure_ascii=False),
                    commit.occurred_at.isoformat(),
                ),
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
        """Atomically claim pending outbox rows for one worker pass."""

        self._ready()
        claimed_at = datetime.now(UTC).isoformat()
        lease_until = (datetime.now(UTC) + timedelta(seconds=lease_ttl_s)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if subject_id is not None:
                rows = connection.execute(
                    """
                    SELECT * FROM tutor_commit_outbox
                    WHERE subject_id = ?
                      AND (status = 'pending'
                           OR (status = 'claimed' AND lease_until < ?))
                    ORDER BY created_at, event_id LIMIT ?
                    """,
                    (subject_id, claimed_at, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM tutor_commit_outbox
                    WHERE status = 'pending'
                       OR (status = 'claimed' AND lease_until < ?)
                    ORDER BY created_at, event_id LIMIT ?
                    """,
                    (claimed_at, limit),
                ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE tutor_commit_outbox
                    SET status = 'claimed', claimed_by = ?,
                        attempt_count = attempt_count + 1, claimed_at = ?,
                        lease_until = ?
                    WHERE event_id = ? AND (status = 'pending' OR lease_until < ?)
                    """,
                    (
                        worker_id,
                        claimed_at,
                        lease_until,
                        str(row["event_id"]),
                        claimed_at,
                    ),
                )
        return tuple(
            PendingTutorCommit(
                event_id=str(row["event_id"]),
                kind=cast(TutorAggregateKind, str(row["kind"])),
                subject_id=str(row["subject_id"]),
                actor_id=str(row["actor_id"]),
                archive_payload=json.loads(str(row["archive_payload_json"])),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        )

    async def mark_commit_delivered(self, *, event_id: str) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tutor_commit_outbox
                SET status = 'delivered', lease_until = NULL
                WHERE event_id = ? AND status = 'claimed'
                """,
                (event_id,),
            )

    async def release_commit_claim(self, *, event_id: str) -> None:
        self._ready()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE tutor_commit_outbox
                SET status = 'pending', claimed_by = NULL, claimed_at = NULL,
                    lease_until = NULL
                WHERE event_id = ? AND status = 'claimed'
                """,
                (event_id,),
            )

    @staticmethod
    def _save_practice_session_locked(
        connection: sqlite3.Connection,
        session: PracticeSession,
    ) -> PracticeSession:
        if session.created_at is None or session.updated_at is None:
            raise ValueError("persisted practice sessions require timestamps")
        row = connection.execute(
            """
            SELECT * FROM tutor_practice_sessions
            WHERE subject_id = ? AND session_id = ?
            """,
            (session.subject_id, session.session_id),
        ).fetchone()
        if row is None:
            if session.revision != 0:
                raise PracticeConflictError("practice_session_not_found")
            connection.execute(
                """
                INSERT INTO tutor_practice_sessions(
                    session_id, account_id, subject_id, actor_id,
                    voice_session_id, focus, task_id, status, revision,
                    event_ids_json, practiced_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
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
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                ),
            )
            return session
        current = SqliteGuardianStore._practice_session(row)
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
        cursor = connection.execute(
            """
            UPDATE tutor_practice_sessions
            SET status = ?, revision = ?, event_ids_json = ?, practiced_seconds = ?,
                updated_at = ?
            WHERE session_id = ? AND subject_id = ? AND revision = ?
            """,
            (
                session.status,
                session.revision,
                json.dumps(session.event_ids, ensure_ascii=False),
                session.practiced_seconds,
                session.updated_at.isoformat(),
                session.session_id,
                session.subject_id,
                current.revision,
            ),
        )
        if cursor.rowcount != 1:  # pragma: no cover - transaction holds writer lock
            raise PracticeConflictError("revision_conflict")
        return session

    async def study_progress(self, *, subject_id: str) -> StudyProgress | None:
        self._ready()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tutor_study_progress WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        return self._study_progress(row) if row is not None else None

    async def save_study_progress(
        self,
        progress: StudyProgress,
        *,
        rebuilt_at: datetime,
    ) -> StudyProgress:
        self._ready()
        rebuilt = _timestamp(rebuilt_at, field="rebuilt_at")
        account_id = progress.actor_id or progress.subject_id
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tutor_study_progress(
                    account_id, subject_id, actor_id,
                    practiced_seconds, active_days_json,
                    current_streak_days, weak_points_json, mastered_skills_json,
                    source_event_ids_json, last_practiced_at, rebuilt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                (
                    account_id,
                    progress.subject_id,
                    progress.actor_id,
                    progress.practiced_seconds,
                    json.dumps([value.isoformat() for value in progress.active_days]),
                    progress.current_streak_days,
                    json.dumps(progress.weak_points, ensure_ascii=False),
                    json.dumps(progress.mastered_skills, ensure_ascii=False),
                    json.dumps(progress.source_event_ids, ensure_ascii=False),
                    (
                        progress.last_practiced_at.isoformat()
                        if progress.last_practiced_at is not None
                        else None
                    ),
                    rebuilt.isoformat(),
                ),
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
        self._ready()
        event_uuid = str(uuid.UUID(crisis_event_id))
        occurred = _timestamp(occurred_at, field="occurred_at")
        created = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO guardian_crisis_events(
                    crisis_event_id, evidence_event_id, minor_user_id,
                    occurred_at, script_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(crisis_event_id) DO NOTHING
                """,
                (
                    event_uuid,
                    evidence_event_id,
                    minor_user_id,
                    occurred.isoformat(),
                    script_version,
                    created.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM guardian_crisis_events WHERE crisis_event_id = ?",
                (event_uuid,),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise RuntimeError("guardian crisis event disappeared")
            if (
                str(row["evidence_event_id"]) != evidence_event_id
                or str(row["minor_user_id"]) != minor_user_id
                or str(row["script_version"]) != script_version
            ):
                raise GuardianConflictError("crisis event id is immutable")
            guardians = connection.execute(
                """
                SELECT DISTINCT guardian_user_id FROM guardian_links
                WHERE minor_user_id = ? AND status = 'active'
                """,
                (minor_user_id,),
            ).fetchall()
            for guardian in guardians:
                guardian_user_id = str(guardian["guardian_user_id"])
                notification_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"memoria:guardian-crisis:{event_uuid}:{guardian_user_id}",
                    )
                )
                connection.execute(
                    """
                    INSERT INTO guardian_notification_outbox(
                        notification_id, crisis_event_id, guardian_user_id,
                        channel, status, attempts, created_at
                    ) VALUES (?, ?, ?, 'wechat_subscription', 'pending', 0, ?)
                    ON CONFLICT(crisis_event_id, guardian_user_id) DO NOTHING
                    """,
                    (notification_id, event_uuid, guardian_user_id, created.isoformat()),
                )
            notification_count = int(
                connection.execute(
                    """
                    SELECT count(*) FROM guardian_notification_outbox
                    WHERE crisis_event_id = ?
                    """,
                    (event_uuid,),
                ).fetchone()[0]
            )
        return CrisisNotificationReceipt(
            crisis_event_id=event_uuid,
            evidence_event_id=evidence_event_id,
            minor_user_id=minor_user_id,
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            script_version=script_version,
            notification_count=notification_count,
        )

    async def guardian_notifications(
        self,
        *,
        guardian_user_id: str,
        limit: int = 50,
    ) -> tuple[GuardianNotification, ...]:
        self._ready()
        if not 1 <= limit <= 100:
            raise ValueError("notification limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT outbox.*, crisis.minor_user_id
                FROM guardian_notification_outbox outbox
                JOIN guardian_crisis_events crisis
                  ON crisis.crisis_event_id = outbox.crisis_event_id
                WHERE outbox.guardian_user_id = ?
                ORDER BY outbox.created_at DESC, outbox.notification_id DESC
                LIMIT ?
                """,
                (guardian_user_id, limit),
            ).fetchall()
        return tuple(self._notification(row) for row in rows)

    async def export_for_account(self, *, account_id: str) -> dict[str, object]:
        self._ready()
        with self._connect() as connection:
            links = connection.execute(
                """
                SELECT link_id, guardian_user_id, minor_user_id, relation, status,
                       verified_via, binding_expires_at, created_at, activated_at, revoked_at
                FROM guardian_links
                WHERE guardian_user_id = ? OR minor_user_id = ?
                ORDER BY created_at, link_id
                """,
                (account_id, account_id),
            ).fetchall()
            link_ids = tuple(str(row["link_id"]) for row in links)
            consents: list[dict[str, object]] = []
            if link_ids:
                placeholders = ",".join("?" for _ in link_ids)
                consents = [
                    dict(row)
                    for row in connection.execute(
                        f"""
                        SELECT consent_id, link_id, consent_kind, policy_version,
                               granted_at, expires_at, revoked_at, evidence_event_id,
                               revocation_evidence_event_id
                        FROM guardian_consents WHERE link_id IN ({placeholders})
                        ORDER BY granted_at, consent_id
                        """,  # noqa: S608 - placeholders are generated internally
                        link_ids,
                    ).fetchall()
                ]
            practice_sessions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM tutor_practice_sessions
                    WHERE account_id = ? OR actor_id = ?
                    ORDER BY created_at, session_id
                    """,
                    (account_id, account_id),
                ).fetchall()
            ]
            progress = connection.execute(
                """
                SELECT * FROM tutor_study_progress
                WHERE account_id = ? OR actor_id = ?
                ORDER BY subject_id IS NULL, subject_id
                LIMIT 1
                """,
                (account_id, account_id),
            ).fetchone()
            crisis_events = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM guardian_crisis_events
                    WHERE minor_user_id = ? ORDER BY occurred_at, crisis_event_id
                    """,
                    (account_id,),
                ).fetchall()
            ]
            notifications = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT outbox.* FROM guardian_notification_outbox outbox
                    JOIN guardian_crisis_events crisis
                      ON crisis.crisis_event_id = outbox.crisis_event_id
                    WHERE outbox.guardian_user_id = ? OR crisis.minor_user_id = ?
                    ORDER BY outbox.created_at, outbox.notification_id
                    """,
                    (account_id, account_id),
                ).fetchall()
            ]
            corpus_samples = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT sample_id, minor_user_id, consent_id, source_event_id,
                           media_type, byte_count, content_sha256,
                           created_at, expires_at, deleted_at
                    FROM guardian_corpus_samples
                    WHERE minor_user_id = ? ORDER BY created_at, sample_id
                    """,
                    (account_id,),
                ).fetchall()
            ]
        return {
            "links": [dict(row) for row in links],
            "consents": consents,
            "tutor_practice_sessions": practice_sessions,
            "tutor_study_progress": dict(progress) if progress is not None else None,
            "crisis_events": crisis_events,
            "guardian_notifications": notifications,
            "corpus_samples": corpus_samples,
        }

    async def delete_for_account(self, *, account_id: str) -> dict[str, int]:
        self._ready()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            practice_count = connection.execute(
                """
                DELETE FROM tutor_practice_sessions
                WHERE account_id = ? OR actor_id = ?
                """,
                (account_id, account_id),
            ).rowcount
            progress_count = connection.execute(
                """
                DELETE FROM tutor_study_progress
                WHERE account_id = ? OR actor_id = ?
                """,
                (account_id, account_id),
            ).rowcount
            notification_count = connection.execute(
                """
                DELETE FROM guardian_notification_outbox
                WHERE guardian_user_id = ? OR crisis_event_id IN (
                    SELECT crisis_event_id FROM guardian_crisis_events
                    WHERE minor_user_id = ?
                )
                """,
                (account_id, account_id),
            ).rowcount
            crisis_count = connection.execute(
                "DELETE FROM guardian_crisis_events WHERE minor_user_id = ?",
                (account_id,),
            ).rowcount
            corpus_count = connection.execute(
                """
                DELETE FROM guardian_corpus_samples
                WHERE minor_user_id = ? OR consent_id IN (
                    SELECT consent.consent_id FROM guardian_consents consent
                    JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE link.guardian_user_id = ? OR link.minor_user_id = ?
                )
                """,
                (account_id, account_id, account_id),
            ).rowcount
            link_ids = tuple(
                str(row["link_id"])
                for row in connection.execute(
                    """
                    SELECT link_id FROM guardian_links
                    WHERE guardian_user_id = ? OR minor_user_id = ?
                    """,
                    (account_id, account_id),
                ).fetchall()
            )
            consent_count = 0
            if link_ids:
                placeholders = ",".join("?" for _ in link_ids)
                consent_count = connection.execute(
                    f"DELETE FROM guardian_consents WHERE link_id IN ({placeholders})",  # noqa: S608
                    link_ids,
                ).rowcount
            link_count = connection.execute(
                """
                DELETE FROM guardian_links
                WHERE guardian_user_id = ? OR minor_user_id = ?
                """,
                (account_id, account_id),
            ).rowcount
        return {
            "links": link_count,
            "consents": consent_count,
            "tutor_practice_sessions": practice_count,
            "tutor_study_progress": progress_count,
            "crisis_events": crisis_count,
            "guardian_notifications": notification_count,
            "corpus_samples": corpus_count,
        }

    async def remaining_account_rows(self, *, account_id: str) -> dict[str, int]:
        self._ready()
        with self._connect() as connection:
            links = int(
                connection.execute(
                    """
                    SELECT count(*) FROM guardian_links
                    WHERE guardian_user_id = ? OR minor_user_id = ?
                    """,
                    (account_id, account_id),
                ).fetchone()[0]
            )
            consents = int(
                connection.execute(
                    """
                    SELECT count(*) FROM guardian_consents consent
                    JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE link.guardian_user_id = ? OR link.minor_user_id = ?
                    """,
                    (account_id, account_id),
                ).fetchone()[0]
            )
            practice_sessions = int(
                connection.execute(
                    """
                    SELECT count(*) FROM tutor_practice_sessions
                    WHERE account_id = ? OR actor_id = ?
                    """,
                    (account_id, account_id),
                ).fetchone()[0]
            )
            study_progress = int(
                connection.execute(
                    """
                    SELECT count(*) FROM tutor_study_progress
                    WHERE account_id = ? OR actor_id = ?
                    """,
                    (account_id, account_id),
                ).fetchone()[0]
            )
            crisis_events = int(
                connection.execute(
                    "SELECT count(*) FROM guardian_crisis_events WHERE minor_user_id = ?",
                    (account_id,),
                ).fetchone()[0]
            )
            notifications = int(
                connection.execute(
                    """
                    SELECT count(*) FROM guardian_notification_outbox outbox
                    JOIN guardian_crisis_events crisis
                      ON crisis.crisis_event_id = outbox.crisis_event_id
                    WHERE outbox.guardian_user_id = ? OR crisis.minor_user_id = ?
                    """,
                    (account_id, account_id),
                ).fetchone()[0]
            )
            corpus_samples = int(
                connection.execute(
                    """
                    SELECT count(*) FROM guardian_corpus_samples sample
                    LEFT JOIN guardian_consents consent ON consent.consent_id = sample.consent_id
                    LEFT JOIN guardian_links link ON link.link_id = consent.link_id
                    WHERE sample.minor_user_id = ?
                       OR link.guardian_user_id = ? OR link.minor_user_id = ?
                    """,
                    (account_id, account_id, account_id),
                ).fetchone()[0]
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
        self._ready()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT minor_user_id FROM guardian_links
                WHERE guardian_user_id = ? AND status = 'active'
                ORDER BY minor_user_id
                """,
                (guardian_user_id,),
            ).fetchall()
        return tuple(str(row["minor_user_id"]) for row in rows)


__all__ = ["SqliteGuardianStore"]
