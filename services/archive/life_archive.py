"""LifeArchive public seam backed by an append-only SQLite evidence ledger."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from services.archive.domain import (
    ContextBundle,
    ContextQuery,
    EvidenceEvent,
    EvidenceNotFoundError,
    IdempotencyConflictError,
    MemoryReview,
    RawVoiceConsent,
    RawVoiceConsentRequiredError,
    RawVoiceRetentionPolicy,
    RawVoiceRevocation,
    RecordResult,
    ReviewedMemory,
    canonical_payload,
)
from services.archive.object_store import ObjectRef

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    session_id TEXT,
    turn_id INTEGER,
    generation_id INTEGER,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    speaker_identity_id TEXT,
    speaker_class TEXT NOT NULL CHECK (
        speaker_class IN ('owner', 'guest', 'uncertain', 'assistant', 'system')
    ),
    source TEXT NOT NULL,
    consent_grant_id TEXT,
    payload_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    supersedes_event_id TEXT,
    FOREIGN KEY (supersedes_event_id) REFERENCES evidence_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_evidence_account_occurred
ON evidence_events(account_id, occurred_at DESC, event_id DESC);

CREATE TABLE IF NOT EXISTS processing_outbox (
    outbox_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    last_error_code TEXT,
    FOREIGN KEY (event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
ON processing_outbox(status, available_at, outbox_id);

CREATE TABLE IF NOT EXISTS consent_grants (
    consent_grant_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    retention_policy TEXT NOT NULL DEFAULT 'account_lifetime',
    granted_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    evidence_event_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_consent_account_purpose
ON consent_grants(account_id, purpose, granted_at DESC);

UPDATE consent_grants
SET revoked_at = COALESCE(revoked_at, granted_at)
WHERE consent_grant_id IN (
    SELECT consent_grant_id
    FROM (
        SELECT consent_grant_id,
               ROW_NUMBER() OVER (
                   PARTITION BY account_id, purpose
                   ORDER BY granted_at DESC, consent_grant_id DESC
               ) AS active_rank
        FROM consent_grants
        WHERE revoked_at IS NULL
    ) ranked
    WHERE active_rank > 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_consent_active
ON consent_grants(account_id, purpose)
WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS evidence_blobs (
    blob_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    evidence_event_id TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
    content_sha256 TEXT NOT NULL,
    encryption_key_version TEXT NOT NULL,
    retention_policy TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (evidence_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_blob_event
ON evidence_blobs(evidence_event_id);

CREATE TABLE IF NOT EXISTS transcript_versions (
    transcript_version_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id INTEGER NOT NULL,
    evidence_event_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE (account_id, session_id, turn_id, evidence_event_id),
    FOREIGN KEY (evidence_event_id) REFERENCES evidence_events(event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_transcript_current
ON transcript_versions(account_id, session_id, turn_id)
WHERE is_current = 1;
"""


class LifeArchive:
    """Small public archive interface; storage details stay behind this seam."""

    def __init__(self, sqlite_path: Path) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(cls, path: str | Path) -> LifeArchive:
        return cls(Path(path))

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._path, timeout=5) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_SCHEMA)
                consent_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(consent_grants)")
                }
                if "retention_policy" not in consent_columns:
                    connection.execute(
                        """
                        ALTER TABLE consent_grants ADD COLUMN retention_policy TEXT NOT NULL
                        DEFAULT 'account_lifetime'
                        """
                    )
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def record(self, event: EvidenceEvent) -> RecordResult:
        recorded_at = datetime.now(UTC)
        with self._connect() as connection:
            return self._record_with_connection(connection, event, recorded_at)

    def _record_with_connection(
        self,
        connection: sqlite3.Connection,
        event: EvidenceEvent,
        recorded_at: datetime,
    ) -> RecordResult:
        existing = connection.execute(
                "SELECT * FROM evidence_events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
        if existing is not None:
            if (
                str(existing["content_sha256"]) != event.content_sha256
                and self._event_from_row(existing).idempotency_sha256
                != event.idempotency_sha256
            ):
                raise IdempotencyConflictError(
                    f"event_id {event.event_id!r} already has different content"
                )
            outbox = connection.execute(
                "SELECT outbox_id FROM processing_outbox WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if outbox is None:  # pragma: no cover - protected by one transaction
                raise RuntimeError("evidence event is missing its outbox entry")
            return RecordResult(
                event_id=event.event_id,
                outbox_id=str(outbox["outbox_id"]),
                recorded_at=datetime.fromisoformat(str(existing["recorded_at"])),
                duplicate=True,
            )

        outbox_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:outbox:{event.event_id}"))
        recorded_text = recorded_at.isoformat()
        connection.execute(
                """
                INSERT INTO evidence_events (
                    event_id, account_id, session_id, turn_id, generation_id,
                    event_type, schema_version, occurred_at, recorded_at,
                    speaker_identity_id, speaker_class, source, consent_grant_id,
                    payload_json, content_sha256, supersedes_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.account_id,
                    event.session_id,
                    event.turn_id,
                    event.generation_id,
                    event.event_type,
                    event.schema_version,
                    event.occurred_at.isoformat(),
                    recorded_text,
                    event.speaker_identity_id,
                    event.speaker_class,
                    event.source,
                    event.consent_grant_id,
                    canonical_payload(event.payload),
                    event.content_sha256,
                    event.supersedes_event_id,
                ),
        )
        connection.execute(
                """
                INSERT INTO processing_outbox (
                    outbox_id, account_id, event_id, task_type,
                    available_at, created_at
                ) VALUES (?, ?, ?, 'compile_evidence', ?, ?)
                """,
                (
                    outbox_id,
                    event.account_id,
                    event.event_id,
                    recorded_text,
                    recorded_text,
                ),
        )
        return RecordResult(
            event_id=event.event_id,
            outbox_id=outbox_id,
            recorded_at=recorded_at,
            duplicate=False,
        )

    async def grant_raw_voice_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
        retention_policy: RawVoiceRetentionPolicy,
        granted_at: datetime,
    ) -> RawVoiceConsent:
        if not account_id.strip() or not policy_version.strip():
            raise ValueError("raw voice consent requires account_id and policy_version")
        if granted_at.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        granted_at = granted_at.astimezone(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._unrevoked_raw_voice_consent(connection, account_id)
            active = (
                self._consent_from_row(row)
                if row is not None and self._consent_row_is_active(row)
                else None
            )
            if (
                active is not None
                and active.policy_version == policy_version
                and active.retention_policy == retention_policy
            ):
                return active
            if row is not None:
                connection.execute(
                    "UPDATE consent_grants SET revoked_at = ? WHERE consent_grant_id = ?",
                    (granted_at.isoformat(), str(row["consent_grant_id"])),
                )
            consent = RawVoiceConsent(
                consent_grant_id=str(uuid.uuid4()),
                account_id=account_id,
                policy_version=policy_version,
                retention_policy=retention_policy,
                granted_at=granted_at,
            )
            connection.execute(
                """
                INSERT INTO consent_grants (
                    consent_grant_id, account_id, purpose, policy_version,
                    retention_policy, granted_at
                ) VALUES (?, ?, 'raw_voice_archive', ?, ?, ?)
                """,
                (
                    consent.consent_grant_id,
                    account_id,
                    policy_version,
                    retention_policy,
                    granted_at.isoformat(),
                ),
            )
            return consent

    async def active_raw_voice_consent(self, *, account_id: str) -> RawVoiceConsent | None:
        with self._connect() as connection:
            return self._active_raw_voice_consent(connection, account_id)

    @staticmethod
    def _active_raw_voice_consent(
        connection: sqlite3.Connection,
        account_id: str,
    ) -> RawVoiceConsent | None:
        row = LifeArchive._unrevoked_raw_voice_consent(connection, account_id)
        if row is None or not LifeArchive._consent_row_is_active(row):
            return None
        return LifeArchive._consent_from_row(row)

    @staticmethod
    def _unrevoked_raw_voice_consent(
        connection: sqlite3.Connection,
        account_id: str,
    ) -> sqlite3.Row | None:
        row: sqlite3.Row | None = connection.execute(
            """
            SELECT * FROM consent_grants
            WHERE account_id = ? AND purpose = 'raw_voice_archive' AND revoked_at IS NULL
            ORDER BY granted_at DESC, consent_grant_id DESC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()
        return row

    @staticmethod
    def _consent_row_is_active(row: sqlite3.Row) -> bool:
        expires_at = row["expires_at"]
        if expires_at is None:
            return True
        try:
            expiry = datetime.fromisoformat(str(expires_at))
        except ValueError:
            return False
        return expiry.tzinfo is not None and expiry.astimezone(UTC) > datetime.now(UTC)

    async def revoke_raw_voice_consent(
        self,
        *,
        account_id: str,
        revoked_at: datetime,
    ) -> RawVoiceRevocation:
        if revoked_at.tzinfo is None:
            raise ValueError("timestamps must include a timezone")
        revoked_at = revoked_at.astimezone(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._unrevoked_raw_voice_consent(connection, account_id)
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM consent_grants
                    WHERE account_id = ? AND purpose = 'raw_voice_archive'
                    ORDER BY granted_at DESC, consent_grant_id DESC
                    LIMIT 1
                    """,
                    (account_id,),
                ).fetchone()
            if row is None:
                raise RawVoiceConsentRequiredError("active raw voice consent is required")
            consent = self._consent_from_row(row)
            effective_revoked_at = consent.revoked_at or revoked_at
            if consent.revoked_at is None:
                connection.execute(
                    "UPDATE consent_grants SET revoked_at = ? WHERE consent_grant_id = ?",
                    (effective_revoked_at.isoformat(), consent.consent_grant_id),
                )
            references = self._raw_voice_references(connection, account_id)
        return RawVoiceRevocation(
            consent=RawVoiceConsent(
                consent_grant_id=consent.consent_grant_id,
                account_id=consent.account_id,
                policy_version=consent.policy_version,
                retention_policy=consent.retention_policy,
                granted_at=consent.granted_at,
                revoked_at=effective_revoked_at,
            ),
            references=references,
        )

    async def purge_raw_voice_blobs(
        self,
        *,
        account_id: str,
        object_keys: tuple[str, ...],
    ) -> None:
        keys = tuple(dict.fromkeys(object_keys))
        if not keys:
            return
        if not account_id.strip() or any(not key.strip() for key in keys):
            raise ValueError("raw voice purge requires account_id and object keys")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                DELETE FROM evidence_blobs
                WHERE account_id = ? AND object_key = ? AND EXISTS (
                    SELECT 1
                    FROM evidence_events event
                    JOIN consent_grants consent
                      ON consent.consent_grant_id = event.consent_grant_id
                    WHERE event.event_id = evidence_blobs.evidence_event_id
                      AND consent.purpose = 'raw_voice_archive'
                )
                """,
                ((account_id, key) for key in keys),
            )

    async def record_with_blob(
        self,
        event: EvidenceEvent,
        reference: ObjectRef,
        *,
        retention_policy: RawVoiceRetentionPolicy,
    ) -> RecordResult:
        if event.speaker_class != "owner" or reference.account_id != event.account_id:
            raise RawVoiceConsentRequiredError("raw voice archive is restricted to the owner")
        recorded_at = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            consent = connection.execute(
                """
                SELECT * FROM consent_grants
                WHERE consent_grant_id = ? AND account_id = ?
                  AND purpose = 'raw_voice_archive' AND retention_policy = ?
                  AND revoked_at IS NULL
                """,
                (event.consent_grant_id, event.account_id, retention_policy),
            ).fetchone()
            if consent is None or not self._consent_row_is_active(consent):
                raise RawVoiceConsentRequiredError("active raw voice consent is required")
            result = self._record_with_connection(connection, event, recorded_at)
            existing = connection.execute(
                "SELECT * FROM evidence_blobs WHERE evidence_event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["content_sha256"]) != reference.content_sha256
                    or int(existing["byte_count"]) != reference.byte_count
                    or str(existing["retention_policy"]) != retention_policy
                ):
                    raise IdempotencyConflictError("raw voice retry has different blob content")
                return replace(
                    result,
                    blob_duplicate=True,
                    retained_object_key=str(existing["object_key"]),
                )
            connection.execute(
                """
                INSERT INTO evidence_blobs (
                    blob_id, account_id, evidence_event_id, object_key, media_type,
                    byte_count, content_sha256, encryption_key_version,
                    retention_policy, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:blob:{event.event_id}")),
                    event.account_id,
                    event.event_id,
                    reference.object_key,
                    reference.media_type,
                    reference.byte_count,
                    reference.content_sha256,
                    reference.encryption_key_version,
                    retention_policy,
                    recorded_at.isoformat(),
                ),
            )
            return replace(
                result,
                blob_duplicate=False,
                retained_object_key=reference.object_key,
            )

    async def raw_voice_blobs(self, *, account_id: str) -> tuple[ObjectRef, ...]:
        with self._connect() as connection:
            return self._raw_voice_references(connection, account_id)

    @staticmethod
    def _raw_voice_references(
        connection: sqlite3.Connection,
        account_id: str,
    ) -> tuple[ObjectRef, ...]:
        rows = connection.execute(
            """
            SELECT blob.* FROM evidence_blobs blob
            JOIN evidence_events event ON event.event_id = blob.evidence_event_id
            JOIN consent_grants consent
              ON consent.consent_grant_id = event.consent_grant_id
            WHERE blob.account_id = ? AND consent.purpose = 'raw_voice_archive'
            ORDER BY blob.created_at, blob.blob_id
            """,
            (account_id,),
        ).fetchall()
        return tuple(
            ObjectRef(
                account_id=account_id,
                object_key=str(row["object_key"]),
                media_type=str(row["media_type"]),
                byte_count=int(row["byte_count"]),
                content_sha256=str(row["content_sha256"]),
                encryption_key_version=str(row["encryption_key_version"]),
                backend="archive",
            )
            for row in rows
        )

    @staticmethod
    def _consent_from_row(row: sqlite3.Row) -> RawVoiceConsent:
        return RawVoiceConsent(
            consent_grant_id=str(row["consent_grant_id"]),
            account_id=str(row["account_id"]),
            policy_version=str(row["policy_version"]),
            retention_policy=str(row["retention_policy"]),  # type: ignore[arg-type]
            granted_at=datetime.fromisoformat(str(row["granted_at"])),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
        )

    async def context(self, query: ContextQuery) -> ContextBundle:
        clauses = ["account_id = ?"]
        parameters: list[object] = [query.account_id]
        if query.speaker_class == "owner":
            clauses.append("speaker_class IN ('owner', 'assistant', 'system')")
        else:
            if query.session_id is None:
                return ContextBundle()
            clauses.extend(
                [
                    "session_id = ?",
                    "speaker_class IN (?, 'assistant', 'system')",
                ]
            )
            parameters.extend([query.session_id, query.speaker_class])
        if query.text.strip():
            clauses.append("payload_json LIKE ? ESCAPE '\\'")
            escaped = (
                query.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.append(f"%{escaped}%")
        parameters.append(query.limit)
        sql = f"""
            SELECT * FROM evidence_events
            WHERE {' AND '.join(clauses)}
            ORDER BY occurred_at DESC, event_id DESC
            LIMIT ?
        """
        with self._connect() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return ContextBundle(evidence=tuple(self._event_from_row(row) for row in rows))

    async def review(self, command: MemoryReview) -> ReviewedMemory:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM evidence_events
                WHERE event_id = ? AND account_id = ?
                """,
                (command.target_id, command.account_id),
            ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(command.target_id)
        target = self._event_from_row(row)
        status_by_action = {
            "confirm": "confirmed",
            "dispute": "disputed",
            "retract": "retracted",
            "correct": "corrected",
        }
        current_text = (
            command.corrected_text.strip()
            if command.corrected_text is not None
            else str(target.payload.get("text") or "") or None
        )
        event_type = (
            "speech.transcript_revised"
            if command.action == "correct"
            else "memory.claim_reviewed"
        )
        await self.record(
            EvidenceEvent(
                event_id=command.review_event_id,
                account_id=command.account_id,
                event_type=event_type,
                occurred_at=command.occurred_at,
                speaker_class="system",
                source="user.archive_review",
                payload={
                    "target_id": command.target_id,
                    "action": command.action,
                    **({"corrected_text": current_text} if command.action == "correct" else {}),
                },
                session_id=target.session_id,
                turn_id=target.turn_id,
                generation_id=target.generation_id,
                supersedes_event_id=command.target_id,
            )
        )
        return ReviewedMemory(
            target_id=command.target_id,
            review_event_id=command.review_event_id,
            status=status_by_action[command.action],  # type: ignore[arg-type]
            current_text=current_text,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EvidenceEvent:
        return EvidenceEvent(
            event_id=str(row["event_id"]),
            account_id=str(row["account_id"]),
            event_type=str(row["event_type"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            speaker_class=str(row["speaker_class"]),  # type: ignore[arg-type]
            source=str(row["source"]),
            payload=json.loads(str(row["payload_json"])),
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            generation_id=row["generation_id"],
            speaker_identity_id=row["speaker_identity_id"],
            consent_grant_id=row["consent_grant_id"],
            schema_version=int(row["schema_version"]),
            supersedes_event_id=row["supersedes_event_id"],
        )
