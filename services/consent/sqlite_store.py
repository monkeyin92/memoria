"""SQLite consent store (local development fixture only, NOT production authority).

SQLite is a local development fixture and is never the authority for
multi-device or production permissions (see the product plan §11.7): production
uses the PostgreSQL store with FORCE row-level security.  Transactions use WAL
mode and a store-level lock so version computation is serialized; every row is
stored as strict canonical JSON and hash-verified on read (tamper detection).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime

from services.consent.events import (
    AuditEntry,
    ConsentOutboxEvent,
    payload_from_json,
    payload_to_json,
)
from services.consent.evidence import ConsentEvidence, ConsentOffer, ConsentSnapshot
from services.consent.store import (
    ConsentConflictError,
    ConsentUnitOfWork,
    IdempotencyRecord,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS consent_offer (
    offer_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'revoked', 'expired', 'superseded')
    ),
    offer_json TEXT NOT NULL,
    PRIMARY KEY (offer_id, version)
);

CREATE TABLE IF NOT EXISTS consent_evidence (
    consent_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    subject_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'revoked', 'expired', 'disputed', 'superseded')
    ),
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (consent_id, version)
);
CREATE INDEX IF NOT EXISTS idx_consent_evidence_active
    ON consent_evidence (subject_id, binding_id, binding_version, status);

CREATE TABLE IF NOT EXISTS consent_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 1),
    subject_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    snapshot_json TEXT NOT NULL,
    UNIQUE (subject_id, binding_id, binding_version, version)
);

CREATE TABLE IF NOT EXISTS consent_outbox (
    event_id TEXT PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    idempotency_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_consent_outbox_created ON consent_outbox (created_at);

CREATE TABLE IF NOT EXISTS consent_audit (
    audit_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    consent_id TEXT,
    snapshot_id TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consent_audit_subject ON consent_audit (subject_id, created_at);

CREATE TABLE IF NOT EXISTS consent_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    consent_id TEXT,
    version INTEGER,
    snapshot_id TEXT,
    event_id TEXT NOT NULL,
    audit_id TEXT,
    subject_id TEXT,
    actor_id TEXT,
    created_at TEXT NOT NULL
);
"""


class SqliteConsentStore:
    """SQLite-backed consent store with transactional unit-of-work semantics."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def transaction(self) -> ConsentUnitOfWork:
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise
        return _SqliteUow(self._conn, self._lock)

    def close(self) -> None:
        self._conn.close()


class _SqliteUow:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock) -> None:
        self._conn = conn
        self._lock = lock
        self._closed = False

    def _row_to_offer(self, row: sqlite3.Row | None) -> ConsentOffer | None:
        if row is None:
            return None
        return ConsentOffer.from_canonical_dict(json.loads(row["offer_json"]))

    def _row_to_evidence(self, row: sqlite3.Row | None) -> ConsentEvidence | None:
        if row is None:
            return None
        return ConsentEvidence.from_canonical_dict(json.loads(row["evidence_json"]))

    def _row_to_snapshot(self, row: sqlite3.Row | None) -> ConsentSnapshot | None:
        if row is None:
            return None
        return ConsentSnapshot.from_canonical_dict(json.loads(row["snapshot_json"]))

    def _row_to_event(self, row: sqlite3.Row | None) -> ConsentOutboxEvent | None:
        if row is None:
            return None
        return ConsentOutboxEvent(
            event_id=row["event_id"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            version=row["version"],
            payload=payload_from_json(row["payload_json"]),
            created_at=_parse_datetime(row["created_at"]),
            idempotency_key=row["idempotency_key"],
        )

    def _row_to_audit(self, row: sqlite3.Row | None) -> AuditEntry | None:
        if row is None:
            return None
        return AuditEntry(
            audit_id=row["audit_id"],
            event_id=row["event_id"],
            action=row["action"],
            actor_id=row["actor_id"],
            subject_id=row["subject_id"],
            consent_id=row["consent_id"],
            snapshot_id=row["snapshot_id"],
            payload=payload_from_json(row["payload_json"]),
            created_at=_parse_datetime(row["created_at"]),
        )

    def _row_to_idempotency(self, row: sqlite3.Row | None) -> IdempotencyRecord | None:
        if row is None:
            return None
        return IdempotencyRecord(
            idempotency_key=row["idempotency_key"],
            content_hash=row["content_hash"],
            consent_id=row["consent_id"],
            version=row["version"],
            snapshot_id=row["snapshot_id"],
            event_id=row["event_id"],
            audit_id=row["audit_id"],
            subject_id=row["subject_id"],
            actor_id=row["actor_id"],
            created_at=_parse_datetime(row["created_at"]),
        )

    def latest_consent(self, consent_id: str) -> ConsentEvidence | None:
        row = self._conn.execute(
            "SELECT evidence_json FROM consent_evidence WHERE consent_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (consent_id,),
        ).fetchone()
        return self._row_to_evidence(row)

    def latest_offer(self, offer_id: str) -> ConsentOffer | None:
        row = self._conn.execute(
            "SELECT offer_json FROM consent_offer WHERE offer_id = ? ORDER BY version DESC LIMIT 1",
            (offer_id,),
        ).fetchone()
        return self._row_to_offer(row)

    def offer_by_id(self, offer_id: str, version: int) -> ConsentOffer | None:
        row = self._conn.execute(
            "SELECT offer_json FROM consent_offer WHERE offer_id = ? AND version = ?",
            (offer_id, version),
        ).fetchone()
        return self._row_to_offer(row)

    def lock_offer_head(
        self, offer_id: str, actor_id: str, subject_id: str
    ) -> ConsentOffer | None:
        del actor_id, subject_id
        return self.latest_offer(offer_id)

    def get_consent(self, consent_id: str, version: int) -> ConsentEvidence | None:
        row = self._conn.execute(
            "SELECT evidence_json FROM consent_evidence WHERE consent_id = ? AND version = ?",
            (consent_id, version),
        ).fetchone()
        return self._row_to_evidence(row)

    def lock_consent_head(
        self,
        request_actor_id: str,
        evidence_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: str,
        purpose: str,
    ) -> ConsentEvidence | None:
        rows = self._conn.execute(
            "SELECT evidence_json FROM consent_evidence WHERE subject_id = ? "
            "AND binding_id = ? AND binding_version = ?",
            (subject_id, binding_id, binding_version),
        ).fetchall()
        candidates = [
            evidence
            for row in rows
            if (evidence := self._row_to_evidence(row)) is not None
            and evidence.actor_id == evidence_actor_id
            and evidence.capability == capability
            and evidence.purpose == purpose
        ]
        del request_actor_id
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.version, item.status == "active"))

    def active_chains(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> tuple[ConsentEvidence, ...]:
        rows = self._conn.execute(
            "SELECT e.evidence_json FROM consent_evidence e "
            "WHERE e.status = 'active' AND e.subject_id = ? AND e.binding_id = ? "
            "AND e.binding_version = ? AND e.version = ("
            "  SELECT MAX(e2.version) FROM consent_evidence e2 "
            "  WHERE e2.consent_id = e.consent_id"
            ")",
            (subject_id, binding_id, binding_version),
        ).fetchall()
        evidences: list[ConsentEvidence] = []
        for row in rows:
            evidence = self._row_to_evidence(row)
            assert evidence is not None
            evidences.append(evidence)
        return tuple(evidences)

    def all_active_chains(self) -> tuple[ConsentEvidence, ...]:
        rows = self._conn.execute(
            "SELECT e.evidence_json FROM consent_evidence e "
            "WHERE e.status = 'active' AND e.version = ("
            "  SELECT MAX(e2.version) FROM consent_evidence e2 "
            "  WHERE e2.consent_id = e.consent_id"
            ")",
        ).fetchall()
        evidences: list[ConsentEvidence] = []
        for row in rows:
            evidence = self._row_to_evidence(row)
            assert evidence is not None
            evidences.append(evidence)
        return tuple(evidences)

    def latest_snapshot(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None:
        row = self._conn.execute(
            "SELECT snapshot_json FROM consent_snapshot "
            "WHERE subject_id = ? AND binding_id = ? AND binding_version = ? "
            "ORDER BY version DESC LIMIT 1",
            (subject_id, binding_id, binding_version),
        ).fetchone()
        return self._row_to_snapshot(row)

    def lock_snapshot_head(
        self,
        request_actor_id: str,
        subject_id: str,
        binding_id: str,
        binding_version: int,
    ) -> ConsentSnapshot | None:
        del request_actor_id
        return self.latest_snapshot(subject_id, binding_id, binding_version)

    def snapshot_by_id(self, snapshot_id: str) -> ConsentSnapshot | None:
        row = self._conn.execute(
            "SELECT snapshot_json FROM consent_snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return self._row_to_snapshot(row)

    def outbox_by_id(self, event_id: str) -> ConsentOutboxEvent | None:
        row = self._conn.execute(
            "SELECT * FROM consent_outbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        return self._row_to_event(row)

    def audit_by_id(self, audit_id: str) -> AuditEntry | None:
        row = self._conn.execute(
            "SELECT * FROM consent_audit WHERE audit_id = ?",
            (audit_id,),
        ).fetchone()
        return self._row_to_audit(row)

    def get_idempotency(self, idempotency_key: str) -> IdempotencyRecord | None:
        row = self._conn.execute(
            "SELECT * FROM consent_idempotency WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        return self._row_to_idempotency(row)

    def append_consent(self, evidence: ConsentEvidence) -> None:
        try:
            self._conn.execute(
                "INSERT INTO consent_evidence ("
                " consent_id, version, subject_id, binding_id, binding_version, status,"
                " evidence_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.consent_id,
                    evidence.version,
                    evidence.subject_id,
                    evidence.binding_id,
                    evidence.binding_version,
                    evidence.status,
                    json.dumps(evidence.to_canonical_dict(), sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConsentConflictError(
                f"consent version ({evidence.consent_id}, {evidence.version}) already exists"
            ) from exc

    def append_offer(self, offer: ConsentOffer) -> None:
        try:
            self._conn.execute(
                "INSERT INTO consent_offer (offer_id, version, status, offer_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    offer.offer_id,
                    offer.version,
                    offer.status,
                    json.dumps(offer.to_canonical_dict(), sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConsentConflictError(
                f"offer version ({offer.offer_id}, {offer.version}) already exists"
            ) from exc

    def append_snapshot(self, snapshot: ConsentSnapshot) -> None:
        try:
            self._conn.execute(
                "INSERT INTO consent_snapshot ("
                " snapshot_id, version, subject_id, binding_id, binding_version, snapshot_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot.snapshot_id,
                    snapshot.version,
                    snapshot.subject_id,
                    snapshot.binding_id,
                    snapshot.binding_version,
                    json.dumps(snapshot.to_canonical_dict(), sort_keys=True),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConsentConflictError(
                f"snapshot {snapshot.snapshot_id!r} or its version already exists"
            ) from exc

    def append_outbox(self, event: ConsentOutboxEvent) -> None:
        try:
            self._conn.execute(
                "INSERT INTO consent_outbox ("
                " event_id, aggregate_type, aggregate_id, version, payload_json,"
                " created_at, idempotency_key"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.aggregate_type,
                    event.aggregate_id,
                    event.version,
                    payload_to_json(event.payload),
                    event.created_at.isoformat(),
                    event.idempotency_key,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConsentConflictError(f"outbox event {event.event_id!r} already exists") from exc

    def append_audit(self, entry: AuditEntry) -> None:
        try:
            self._conn.execute(
                "INSERT INTO consent_audit ("
                " audit_id, event_id, action, actor_id, subject_id, consent_id,"
                " snapshot_id, payload_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.audit_id,
                    entry.event_id,
                    entry.action,
                    entry.actor_id,
                    entry.subject_id,
                    entry.consent_id,
                    entry.snapshot_id,
                    payload_to_json(entry.payload),
                    entry.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConsentConflictError(f"audit entry {entry.audit_id!r} already exists") from exc

    def save_idempotency(self, record: IdempotencyRecord) -> None:
        try:
            self._conn.execute(
                "INSERT INTO consent_idempotency ("
                " idempotency_key, content_hash, consent_id, version, snapshot_id,"
                " event_id, audit_id, subject_id, actor_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.idempotency_key,
                    record.content_hash,
                    record.consent_id,
                    record.version,
                    record.snapshot_id,
                    record.event_id,
                    record.audit_id,
                    record.subject_id,
                    record.actor_id,
                    record.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConsentConflictError(
                f"idempotency key {record.idempotency_key!r} already exists"
            ) from exc

    def commit(self) -> None:
        if self._closed:
            return
        self._conn.commit()
        self._closed = True
        self._lock.release()

    def rollback(self) -> None:
        if self._closed:
            return
        self._conn.rollback()
        self._closed = True
        self._lock.release()

    def __enter__(self) -> ConsentUnitOfWork:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._closed:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


__all__ = ["SqliteConsentStore"]
