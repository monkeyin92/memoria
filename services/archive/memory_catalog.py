"""Rebuildable SQLite projections for the owner's long-term memory catalog."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from services.archive.domain import EvidenceEvent, EvidenceNotFoundError, canonical_payload
from services.archive.episode_consolidator import (
    EpisodeCandidate,
    EpisodeConsolidator,
    ExistingEpisode,
)
from services.archive.life_archive import _SCHEMA as _LEDGER_SCHEMA
from services.archive.memory_domain import (
    AccountWriteGuard,
    AccountWriteRejectedError,
    CompileReport,
    ConflictState,
    DomainCategory,
    MemoryCategory,
    MemoryClaimReview,
    MemoryExtractor,
    MemoryKind,
    MemorySearchItem,
    MemorySearchQuery,
    MemorySearchResult,
    MemorySensitivity,
    MemoryStatus,
    PersonItem,
    ReviewedClaim,
    ReviewQueueItem,
    TimelineItem,
)
from services.common.evidence_policy import contribution_for

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_compile_receipts (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('compiled', 'ignored')),
    compiled_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_claims (
    claim_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'semantic'
        CHECK (memory_kind IN ('semantic', 'episodic', 'procedural', 'relationship')),
    subject_key TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    sensitive_domain TEXT NOT NULL,
    entity_ids_json TEXT NOT NULL DEFAULT '[]',
    extractor_version TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    observed_at TEXT NOT NULL,
    stability REAL NOT NULL DEFAULT 0 CHECK (stability >= 0 AND stability <= 1),
    salience REAL NOT NULL DEFAULT 0.5 CHECK (salience >= 0 AND salience <= 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal'
        CHECK (sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')),
    conflict_state TEXT NOT NULL DEFAULT 'none'
        CHECK (conflict_state IN ('none', 'potential', 'active', 'resolved')),
    created_at TEXT NOT NULL,
    review_event_id TEXT,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_claim_account_subject
ON memory_claims(account_id, subject_key, predicate, status);

CREATE TABLE IF NOT EXISTS person_entities (
    person_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    relationship_to_owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    source_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, canonical_key),
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS person_aliases (
    person_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (person_id, alias, source_event_id),
    FOREIGN KEY (person_id) REFERENCES person_entities(person_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relationships (
    relationship_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    person_id TEXT NOT NULL,
    relationship_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    source_event_id TEXT NOT NULL,
    valid_at TEXT NOT NULL,
    FOREIGN KEY (person_id) REFERENCES person_entities(person_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS life_episodes (
    episode_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'episodic'
        CHECK (memory_kind = 'episodic'),
    consolidation_key TEXT NOT NULL,
    entity_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    event_start TEXT NOT NULL,
    event_end TEXT,
    observed_at TEXT NOT NULL,
    stability REAL NOT NULL DEFAULT 0 CHECK (stability >= 0 AND stability <= 1),
    salience REAL NOT NULL DEFAULT 0.6 CHECK (salience >= 0 AND salience <= 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal'
        CHECK (sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')),
    conflict_state TEXT NOT NULL DEFAULT 'none'
        CHECK (conflict_state IN ('none', 'potential', 'active', 'resolved')),
    evidence_count INTEGER NOT NULL DEFAULT 1 CHECK (evidence_count >= 1),
    source_event_id TEXT NOT NULL,
    UNIQUE (account_id, consolidation_key),
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS timeline_entries (
    timeline_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'episodic'
        CHECK (memory_kind = 'episodic'),
    entity_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    event_start TEXT NOT NULL,
    event_end TEXT,
    time_precision TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    salience REAL NOT NULL DEFAULT 0.6 CHECK (salience >= 0 AND salience <= 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal'
        CHECK (sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')),
    conflict_state TEXT NOT NULL DEFAULT 'none'
        CHECK (conflict_state IN ('none', 'potential', 'active', 'resolved')),
    source_event_id TEXT NOT NULL,
    FOREIGN KEY (episode_id) REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episode_evidence (
    episode_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    PRIMARY KEY (episode_id, source_event_id),
    FOREIGN KEY (episode_id) REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'procedural'
        CHECK (memory_kind = 'procedural'),
    entity_ids_json TEXT NOT NULL DEFAULT '[]',
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    applicability TEXT NOT NULL,
    counterexample TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    source_event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    observed_at TEXT NOT NULL,
    stability REAL NOT NULL DEFAULT 0 CHECK (stability >= 0 AND stability <= 1),
    salience REAL NOT NULL DEFAULT 0.55 CHECK (salience >= 0 AND salience <= 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal'
        CHECK (sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')),
    conflict_state TEXT NOT NULL DEFAULT 'none'
        CHECK (conflict_state IN ('none', 'potential', 'active', 'resolved')),
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_search_documents (
    document_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    memory_kind TEXT NOT NULL
        CHECK (memory_kind IN ('semantic', 'episodic', 'procedural', 'relationship')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    entity_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    source_event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    observed_at TEXT NOT NULL,
    stability REAL NOT NULL DEFAULT 0 CHECK (stability >= 0 AND stability <= 1),
    salience REAL NOT NULL DEFAULT 0 CHECK (salience >= 0 AND salience <= 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal'
        CHECK (sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')),
    conflict_state TEXT NOT NULL DEFAULT 'none'
        CHECK (conflict_state IN ('none', 'potential', 'active', 'resolved')),
    UNIQUE (kind, item_id),
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_search_account_status_time
ON memory_search_documents(account_id, status, occurred_at DESC);

CREATE TABLE IF NOT EXISTS memory_search_document_sources (
    document_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (document_id, source_event_id),
    FOREIGN KEY (document_id) REFERENCES memory_search_documents(document_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);
"""

_SINGLE_VALUE_PREDICATES = frozenset({"age", "birth_date", "birth_place"})
_REVIEW_STATUS: dict[str, MemoryStatus] = {
    "confirm": "confirmed",
    "dispute": "disputed",
    "retract": "retracted",
    "correct": "confirmed",
}
_PROJECTION_REVIEW_STATUS: dict[str, MemoryStatus] = {
    **_REVIEW_STATUS,
    "correct": "retracted",
}
_SOURCE_STATUS_TABLES = (
    "person_entities",
    "person_aliases",
    "relationships",
    "timeline_entries",
    "episode_evidence",
    "knowledge_items",
)


@asynccontextmanager
async def _allow_account_write(_: str) -> AsyncIterator[None]:
    yield


def _stable_id(kind: str, *values: object) -> str:
    key = ":".join(str(value) for value in values)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{kind}:{key}"))


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


_SENSITIVITY_ORDER: dict[MemorySensitivity, int] = {
    "public": 0,
    "personal": 1,
    "sensitive": 2,
    "highly_sensitive": 3,
}


def _sensitivity(value: str) -> MemorySensitivity:
    normalized = value.strip().casefold()
    if normalized == "public":
        return "public"
    if normalized in {"health", "finance", "legal", "biometric", "highly_sensitive"}:
        return "highly_sensitive"
    if normalized in {"relationship", "private", "sensitive"}:
        return "sensitive"
    return "personal"


def _json_ids(values: tuple[str, ...] | list[str] | set[str]) -> str:
    return json.dumps(sorted(set(values)), ensure_ascii=False, separators=(",", ":"))


def _row_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        raw = json.loads(str(value))
    except json.JSONDecodeError:
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(sorted({str(item) for item in raw if str(item).strip()}))


class MemoryCatalog:
    """Compile evidence into projections and expose their stable public seam."""

    def __init__(
        self,
        sqlite_path: Path,
        *,
        extractor: MemoryExtractor,
        account_guard: AccountWriteGuard | None = None,
        episode_consolidator: EpisodeConsolidator | None = None,
    ) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._extractor = extractor
        self._account_guard = account_guard or _allow_account_write
        self._episode_consolidator = episode_consolidator or EpisodeConsolidator()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        extractor: MemoryExtractor,
        account_guard: AccountWriteGuard | None = None,
        episode_consolidator: EpisodeConsolidator | None = None,
    ) -> MemoryCatalog:
        return cls(
            Path(path),
            extractor=extractor,
            account_guard=account_guard,
            episode_consolidator=episode_consolidator,
        )

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
                connection.executescript(_LEDGER_SCHEMA)
                connection.executescript(_SCHEMA)
                self._upgrade_schema(connection)
            self._initialized = True

    @staticmethod
    def _upgrade_schema(connection: sqlite3.Connection) -> None:
        _ensure_columns(
            connection,
            "memory_claims",
            {
                "domain_category": "TEXT",
                "memory_kind": "TEXT NOT NULL DEFAULT 'semantic'",
                "entity_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "valid_from": "TEXT",
                "valid_to": "TEXT",
                "observed_at": "TEXT",
                "stability": "REAL NOT NULL DEFAULT 0",
                "salience": "REAL NOT NULL DEFAULT 0.5",
                "sensitivity": "TEXT NOT NULL DEFAULT 'personal'",
                "conflict_state": "TEXT NOT NULL DEFAULT 'none'",
            },
        )
        _ensure_columns(
            connection,
            "life_episodes",
            {
                "domain_category": "TEXT",
                "memory_kind": "TEXT NOT NULL DEFAULT 'episodic'",
                "consolidation_key": "TEXT",
                "entity_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "observed_at": "TEXT",
                "stability": "REAL NOT NULL DEFAULT 0",
                "salience": "REAL NOT NULL DEFAULT 0.6",
                "sensitivity": "TEXT NOT NULL DEFAULT 'personal'",
                "conflict_state": "TEXT NOT NULL DEFAULT 'none'",
                "evidence_count": "INTEGER NOT NULL DEFAULT 1",
            },
        )
        _ensure_columns(
            connection,
            "timeline_entries",
            {
                "domain_category": "TEXT",
                "memory_kind": "TEXT NOT NULL DEFAULT 'episodic'",
                "entity_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "observed_at": "TEXT",
                "salience": "REAL NOT NULL DEFAULT 0.6",
                "sensitivity": "TEXT NOT NULL DEFAULT 'personal'",
                "conflict_state": "TEXT NOT NULL DEFAULT 'none'",
            },
        )
        _ensure_columns(
            connection,
            "episode_evidence",
            {
                "account_id": "TEXT",
                "status": "TEXT NOT NULL DEFAULT 'candidate'",
            },
        )
        _ensure_columns(
            connection,
            "knowledge_items",
            {
                "domain_category": "TEXT",
                "memory_kind": "TEXT NOT NULL DEFAULT 'procedural'",
                "entity_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "valid_from": "TEXT",
                "valid_to": "TEXT",
                "observed_at": "TEXT",
                "stability": "REAL NOT NULL DEFAULT 0",
                "salience": "REAL NOT NULL DEFAULT 0.55",
                "sensitivity": "TEXT NOT NULL DEFAULT 'personal'",
                "conflict_state": "TEXT NOT NULL DEFAULT 'none'",
            },
        )
        _ensure_columns(
            connection,
            "memory_search_documents",
            {
                "memory_kind": "TEXT NOT NULL DEFAULT 'semantic'",
                "domain_category": "TEXT",
                "entity_ids_json": "TEXT NOT NULL DEFAULT '[]'",
                "valid_from": "TEXT",
                "valid_to": "TEXT",
                "observed_at": "TEXT",
                "stability": "REAL NOT NULL DEFAULT 0",
                "salience": "REAL NOT NULL DEFAULT 0",
                "sensitivity": "TEXT NOT NULL DEFAULT 'personal'",
                "conflict_state": "TEXT NOT NULL DEFAULT 'none'",
            },
        )
        _ensure_columns(
            connection,
            "person_aliases",
            {
                "status": "TEXT NOT NULL DEFAULT 'candidate'",
            },
        )
        connection.executescript(
            """
            UPDATE memory_claims
            SET domain_category = COALESCE(domain_category, category),
                valid_from = COALESCE(valid_from, valid_at),
                observed_at = COALESCE(observed_at, valid_at),
                stability = CASE WHEN stability = 0 THEN confidence ELSE stability END,
                sensitivity = CASE
                    WHEN sensitive_domain IN ('health', 'finance', 'legal', 'biometric')
                        THEN 'highly_sensitive'
                    WHEN sensitive_domain IN ('relationship', 'private')
                        THEN 'sensitive'
                    WHEN sensitive_domain = 'public' THEN 'public'
                    ELSE sensitivity
                END;

            UPDATE life_episodes
            SET domain_category = COALESCE(domain_category, category),
                consolidation_key = COALESCE(
                    NULLIF(consolidation_key, ''),
                    'legacy:' || episode_id
                ),
                observed_at = COALESCE(observed_at, event_start),
                stability = CASE WHEN stability = 0 THEN 0.5 ELSE stability END,
                evidence_count = MAX(
                    evidence_count,
                    (SELECT COUNT(*) FROM episode_evidence evidence
                     WHERE evidence.episode_id = life_episodes.episode_id)
                );

            UPDATE timeline_entries
            SET domain_category = COALESCE(domain_category, category),
                observed_at = COALESCE(observed_at, event_start);

            UPDATE episode_evidence
            SET account_id = COALESCE(
                    account_id,
                    (SELECT account_id FROM life_episodes episode
                     WHERE episode.episode_id = episode_evidence.episode_id)
                ),
                status = COALESCE(
                    (SELECT status FROM timeline_entries timeline
                     WHERE timeline.episode_id = episode_evidence.episode_id
                       AND timeline.source_event_id = episode_evidence.source_event_id
                     ORDER BY timeline.timeline_id LIMIT 1),
                    status
                );

            UPDATE knowledge_items
            SET domain_category = COALESCE(domain_category, category),
                observed_at = COALESCE(observed_at, occurred_at),
                valid_from = COALESCE(valid_from, occurred_at),
                stability = CASE WHEN stability = 0 THEN 0.5 ELSE stability END;

            UPDATE memory_search_documents
            SET memory_kind = CASE kind
                    WHEN 'timeline' THEN 'episodic'
                    WHEN 'episode' THEN 'episodic'
                    WHEN 'knowledge' THEN 'procedural'
                    WHEN 'skill' THEN 'procedural'
                    ELSE memory_kind
                END,
                domain_category = COALESCE(domain_category, category),
                observed_at = COALESCE(observed_at, occurred_at),
                valid_from = COALESCE(valid_from, occurred_at),
                stability = CASE WHEN stability = 0 THEN 0.5 ELSE stability END,
                salience = CASE
                    WHEN salience != 0 THEN salience
                    WHEN kind IN ('timeline', 'episode') THEN 0.6
                    WHEN kind = 'knowledge' THEN 0.55
                    ELSE 0.5
                END;

            INSERT OR IGNORE INTO memory_search_document_sources (
                document_id, account_id, source_event_id
            )
            SELECT document_id, account_id, source_event_id
            FROM memory_search_documents;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_life_episode_consolidation
            ON life_episodes(account_id, consolidation_key);

            CREATE INDEX IF NOT EXISTS idx_memory_search_typed
            ON memory_search_documents(
                account_id, memory_kind, domain_category, status, observed_at DESC
            );

            CREATE INDEX IF NOT EXISTS idx_memory_search_source_account
            ON memory_search_document_sources(account_id, source_event_id);
            """
        )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def compile_pending(self, *, limit: int = 100) -> CompileReport:
        if not 1 <= limit <= 1000:
            raise ValueError("compile limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.*, o.outbox_id
                FROM processing_outbox o
                JOIN evidence_events e ON e.event_id = o.event_id
                WHERE o.task_type = 'compile_evidence'
                  AND o.status IN ('pending', 'processing', 'failed')
                  AND o.available_at <= ?
                ORDER BY o.created_at, o.outbox_id
                LIMIT ?
                """,
                (datetime.now(UTC).isoformat(), limit),
            ).fetchall()

        outcomes = [await self._compile_row(row) for row in rows]
        return CompileReport(
            compiled_events=outcomes.count("compiled"),
            ignored_events=outcomes.count("ignored"),
            failed_events=outcomes.count("failed"),
        )

    async def _compile_row(self, row: sqlite3.Row) -> str:
        event_id = str(row["event_id"])
        outbox_id = str(row["outbox_id"])
        try:
            async with self._account_guard(str(row["account_id"])):
                return await self._compile_row_guarded(row, event_id, outbox_id)
        except AccountWriteRejectedError as exc:
            self._mark_outbox_failed(outbox_id, type(exc).__name__)
            return "failed"

    async def _compile_row_guarded(
        self,
        row: sqlite3.Row,
        event_id: str,
        outbox_id: str,
    ) -> str:
        with self._connect() as connection:
            receipt = connection.execute(
                "SELECT outcome FROM memory_compile_receipts WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if receipt is not None:
                self._complete_outbox(connection, outbox_id)
                return "skipped"
            connection.execute(
                """
                UPDATE processing_outbox
                SET status = 'processing', attempts = attempts + 1,
                    completed_at = NULL, last_error_code = NULL
                WHERE outbox_id = ?
                """,
                (outbox_id,),
            )

        event = self._event_from_row(row)
        if event.event_type == "memory.claim_reviewed" and event.source == "user.memory_review":
            try:
                with self._connect() as connection:
                    self._replay_review_event(connection, event)
                    self._record_receipt(connection, event, outcome="compiled")
                    self._complete_outbox(connection, outbox_id)
                return "compiled"
            except Exception as exc:
                self._mark_outbox_failed(outbox_id, type(exc).__name__)
                return "failed"
        contribution = contribution_for(event)
        if not contribution.accepted:
            with self._connect() as connection:
                self._record_receipt(connection, event, outcome="ignored")
                self._complete_outbox(connection, outbox_id)
            return "ignored"

        try:
            extraction = await self._extractor.extract(event)
            with self._connect() as connection:
                self._write_extraction(connection, event, extraction)
                self._record_receipt(
                    connection,
                    event,
                    outcome="compiled",
                    extractor_version=extraction.extractor_version,
                )
                self._complete_outbox(connection, outbox_id)
            return "compiled"
        except Exception as exc:
            self._mark_outbox_failed(outbox_id, type(exc).__name__)
            return "failed"

    def _mark_outbox_failed(self, outbox_id: str, error_code: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE processing_outbox
                SET status = 'failed', completed_at = NULL, last_error_code = ?
                WHERE outbox_id = ?
                """,
                (error_code, outbox_id),
            )

    def _write_extraction(
        self,
        connection: sqlite3.Connection,
        event: EvidenceEvent,
        extraction: object,
    ) -> None:
        from services.archive.memory_domain import MemoryExtraction

        if not isinstance(extraction, MemoryExtraction):
            raise TypeError("memory extractor returned an invalid result")
        created_at = datetime.now(UTC).isoformat()
        occurred_at = event.occurred_at.isoformat()
        person_ids: dict[str, str] = {}

        for person in extraction.people:
            person_id = _stable_id("person", event.account_id, person.canonical_key)
            person_ids[person.canonical_key] = person_id
            connection.execute(
                """
                INSERT OR IGNORE INTO person_entities (
                    person_id, account_id, canonical_key, display_name,
                    relationship_to_owner, source_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    event.account_id,
                    person.canonical_key,
                    person.display_name,
                    person.relationship_to_owner,
                    event.event_id,
                    occurred_at,
                ),
            )
            for alias in person.aliases:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO person_aliases (
                        person_id, account_id, alias, source_event_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (person_id, event.account_id, alias, event.event_id),
                )

        for index, relationship in enumerate(extraction.relationships):
            person_id = person_ids.get(relationship.person_key) or _stable_id(
                "person", event.account_id, relationship.person_key
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO relationships (
                    relationship_id, account_id, person_id, relationship_type,
                    source_event_id, valid_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _stable_id("relationship", event.event_id, index),
                    event.account_id,
                    person_id,
                    relationship.relationship_type,
                    event.event_id,
                    occurred_at,
                ),
            )

        for index, claim in enumerate(extraction.claims):
            claim_id = _stable_id("claim", event.event_id, index)
            confidence = min(1.0, claim.confidence * contribution_for(event).factor)
            valid_from = claim.valid_from or event.occurred_at
            entity_keys = set(claim.entity_keys)
            if claim.subject_key != "self":
                entity_keys.add(claim.subject_key)
            entity_ids = tuple(
                person_ids.get(key) or _stable_id("person", event.account_id, key)
                for key in sorted(entity_keys)
            )
            sensitivity = _sensitivity(claim.sensitive_domain)
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_claims (
                    claim_id, account_id, category, domain_category, memory_kind,
                    subject_key, predicate, value, confidence, sensitive_domain,
                    entity_ids_json, extractor_version, source_event_id, valid_at,
                    valid_from, valid_to, observed_at, stability, salience,
                    sensitivity, conflict_state, created_at
                ) VALUES (
                    ?, ?, ?, ?, 'semantic', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, 'none', ?
                )
                """,
                (
                    claim_id,
                    event.account_id,
                    claim.domain_category,
                    claim.domain_category,
                    claim.subject_key,
                    claim.predicate,
                    claim.value,
                    confidence,
                    claim.sensitive_domain,
                    _json_ids(entity_ids),
                    extraction.extractor_version or self._extractor.version,
                    event.event_id,
                    valid_from.isoformat(),
                    valid_from.isoformat(),
                    claim.valid_to.isoformat() if claim.valid_to else None,
                    occurred_at,
                    confidence,
                    claim.salience,
                    sensitivity,
                    created_at,
                ),
            )
            self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=claim_id,
                kind="claim",
                memory_kind="semantic",
                title=claim.value[:80],
                body=claim.value,
                domain_category=claim.domain_category,
                entity_ids=entity_ids,
                source_event_ids=(event.event_id,),
                valid_from=valid_from.isoformat(),
                valid_to=claim.valid_to.isoformat() if claim.valid_to else None,
                occurred_at=occurred_at,
                observed_at=occurred_at,
                stability=confidence,
                salience=claim.salience,
                sensitivity=sensitivity,
                conflict_state="none",
            )
            self._refresh_claim_conflicts(
                connection,
                account_id=event.account_id,
                subject_key=claim.subject_key,
                predicate=claim.predicate,
            )

        for index, timeline in enumerate(extraction.timeline):
            entity_ids = tuple(
                person_ids.get(key) or _stable_id("person", event.account_id, key)
                for key in sorted(set(timeline.participant_keys))
            )
            candidate = EpisodeCandidate(
                account_id=event.account_id,
                source_event_id=event.event_id,
                session_id=event.session_id,
                title=timeline.title,
                domain_category=timeline.domain_category,
                event_start=timeline.event_start,
                event_end=timeline.event_end,
                canonical_key=timeline.canonical_key,
                entity_ids=entity_ids,
                salience=timeline.salience,
                sensitivity=timeline.sensitivity,
            )
            existing = self._episode_candidates(
                connection,
                account_id=event.account_id,
                domain_category=timeline.domain_category,
            )
            selected = self._episode_consolidator.choose(candidate, existing)
            episode_id = (
                selected.episode_id
                if selected is not None
                else _stable_id("episode", event.account_id, candidate.fallback_key)
            )
            timeline_id = _stable_id("timeline", event.event_id, index)
            event_start = timeline.event_start.isoformat()
            event_end = timeline.event_end.isoformat() if timeline.event_end else None
            connection.execute(
                """
                INSERT OR IGNORE INTO life_episodes (
                    episode_id, account_id, title, category, domain_category,
                    memory_kind, consolidation_key, entity_ids_json, event_start,
                    event_end, observed_at, stability, salience, sensitivity,
                    conflict_state, evidence_count, source_event_id
                ) VALUES (
                    ?, ?, ?, ?, ?, 'episodic', ?, ?, ?, ?, ?, 0.5, ?, ?,
                    'none', 1, ?
                )
                """,
                (
                    episode_id,
                    event.account_id,
                    timeline.title,
                    timeline.domain_category,
                    timeline.domain_category,
                    candidate.fallback_key,
                    _json_ids(entity_ids),
                    event_start,
                    event_end,
                    occurred_at,
                    timeline.salience,
                    timeline.sensitivity,
                    event.event_id,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO episode_evidence (
                    episode_id, account_id, source_event_id, status
                ) VALUES (?, ?, ?, 'candidate')
                """,
                (episode_id, event.account_id, event.event_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO timeline_entries (
                    timeline_id, account_id, episode_id, title, category,
                    domain_category, memory_kind, entity_ids_json, event_start,
                    event_end, time_precision, observed_at, salience, sensitivity,
                    conflict_state, source_event_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 'episodic', ?, ?, ?, ?, ?, ?, ?, 'none', ?
                )
                """,
                (
                    timeline_id,
                    event.account_id,
                    episode_id,
                    timeline.title,
                    timeline.domain_category,
                    timeline.domain_category,
                    _json_ids(entity_ids),
                    event_start,
                    event_end,
                    timeline.time_precision,
                    occurred_at,
                    timeline.salience,
                    timeline.sensitivity,
                    event.event_id,
                ),
            )
            self._refresh_episode_projection(connection, episode_id=episode_id)

        for index, knowledge in enumerate(extraction.knowledge):
            knowledge_id = _stable_id("knowledge", event.event_id, index)
            entity_ids = tuple(
                person_ids.get(key) or _stable_id("person", event.account_id, key)
                for key in sorted(set(knowledge.entity_keys))
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_items (
                    knowledge_id, account_id, category, domain_category,
                    memory_kind, entity_ids_json, question, answer, applicability,
                    counterexample, source_event_id, occurred_at, valid_from,
                    valid_to, observed_at, stability, salience, sensitivity,
                    conflict_state
                ) VALUES (
                    ?, ?, ?, ?, 'procedural', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?,
                    0.5, ?, ?, 'none'
                )
                """,
                (
                    knowledge_id,
                    event.account_id,
                    knowledge.domain_category,
                    knowledge.domain_category,
                    _json_ids(entity_ids),
                    knowledge.question,
                    knowledge.answer,
                    knowledge.applicability,
                    knowledge.counterexample,
                    event.event_id,
                    occurred_at,
                    occurred_at,
                    occurred_at,
                    knowledge.salience,
                    knowledge.sensitivity,
                ),
            )
            self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=knowledge_id,
                kind="knowledge",
                memory_kind="procedural",
                title=knowledge.question,
                body=" ".join(
                    part
                    for part in (
                        knowledge.answer,
                        knowledge.applicability,
                        knowledge.counterexample,
                    )
                    if part
                ),
                domain_category=knowledge.domain_category,
                entity_ids=entity_ids,
                source_event_ids=(event.event_id,),
                valid_from=occurred_at,
                valid_to=None,
                occurred_at=occurred_at,
                observed_at=occurred_at,
                stability=0.5,
                salience=knowledge.salience,
                sensitivity=knowledge.sensitivity,
                conflict_state="none",
            )

    @staticmethod
    def _episode_candidates(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        domain_category: DomainCategory,
    ) -> tuple[ExistingEpisode, ...]:
        rows = connection.execute(
            """
            SELECT episode_id, consolidation_key, title, domain_category,
                   event_start, event_end, entity_ids_json
            FROM life_episodes
            WHERE account_id = ? AND domain_category = ? AND status != 'retracted'
            ORDER BY observed_at DESC, episode_id
            LIMIT 200
            """,
            (account_id, domain_category),
        ).fetchall()
        return tuple(
            ExistingEpisode(
                episode_id=str(row["episode_id"]),
                consolidation_key=str(row["consolidation_key"]),
                title=str(row["title"]),
                domain_category=cast(DomainCategory, row["domain_category"]),
                event_start=datetime.fromisoformat(str(row["event_start"])),
                event_end=(
                    datetime.fromisoformat(str(row["event_end"]))
                    if row["event_end"] is not None
                    else None
                ),
                entity_ids=_row_ids(row["entity_ids_json"]),
            )
            for row in rows
        )

    @staticmethod
    def _refresh_claim_conflicts(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        subject_key: str,
        predicate: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT claim_id, value
            FROM memory_claims
            WHERE account_id = ? AND subject_key = ? AND predicate = ?
              AND status != 'retracted'
            """,
            (account_id, subject_key, predicate),
        ).fetchall()
        state: ConflictState = (
            "active"
            if predicate in _SINGLE_VALUE_PREDICATES
            and len({str(row["value"]) for row in rows}) > 1
            else "none"
        )
        connection.execute(
            """
            UPDATE memory_claims
            SET conflict_state = ?
            WHERE account_id = ? AND subject_key = ? AND predicate = ?
            """,
            (state, account_id, subject_key, predicate),
        )
        connection.execute(
            """
            UPDATE memory_search_documents
            SET conflict_state = ?
            WHERE account_id = ? AND kind = 'claim' AND item_id IN (
                SELECT claim_id FROM memory_claims
                WHERE account_id = ? AND subject_key = ? AND predicate = ?
            )
            """,
            (state, account_id, account_id, subject_key, predicate),
        )

    @classmethod
    def _refresh_episode_projection(
        cls,
        connection: sqlite3.Connection,
        *,
        episode_id: str,
    ) -> None:
        episode = connection.execute(
            "SELECT * FROM life_episodes WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if episode is None:
            raise EvidenceNotFoundError(episode_id)
        timelines = connection.execute(
            """
            SELECT * FROM timeline_entries
            WHERE episode_id = ?
            ORDER BY event_start, timeline_id
            """,
            (episode_id,),
        ).fetchall()
        evidence = connection.execute(
            """
            SELECT source_event_id, status
            FROM episode_evidence
            WHERE episode_id = ?
            ORDER BY source_event_id
            """,
            (episode_id,),
        ).fetchall()
        if not timelines or not evidence:
            return

        statuses = [cast(MemoryStatus, str(row["status"])) for row in evidence]
        status: MemoryStatus
        if "confirmed" in statuses:
            status = "confirmed"
        elif "candidate" in statuses:
            status = "candidate"
        elif "disputed" in statuses:
            status = "disputed"
        else:
            status = "retracted"
        active_source_event_ids = tuple(
            str(row["source_event_id"])
            for row in evidence
            if str(row["status"]) != "retracted"
        )
        retracted_source_event_ids = tuple(
            str(row["source_event_id"])
            for row in evidence
            if str(row["status"]) == "retracted"
        )
        source_event_ids = (
            *active_source_event_ids,
            *retracted_source_event_ids,
        )
        active_timelines = [
            row for row in timelines if str(row["status"]) != "retracted"
        ]
        surfaced_timelines = active_timelines or list(timelines)
        titles = tuple(
            dict.fromkeys(str(row["title"]) for row in surfaced_timelines)
        )
        title = max(titles, key=lambda value: (len(value), value))
        body = "；".join(titles)[:8000]
        starts = [
            datetime.fromisoformat(str(row["event_start"]))
            for row in surfaced_timelines
        ]
        ends = [
            datetime.fromisoformat(str(row["event_end"]))
            for row in surfaced_timelines
            if row["event_end"] is not None
        ]
        observations = [
            datetime.fromisoformat(str(row["observed_at"]))
            for row in surfaced_timelines
        ]
        entity_ids = tuple(
            sorted(
                {
                    entity_id
                    for row in surfaced_timelines
                    for entity_id in _row_ids(row["entity_ids_json"])
                }
            )
        )
        sensitivities = [
            cast(MemorySensitivity, str(row["sensitivity"]))
            for row in surfaced_timelines
        ]
        sensitivity = max(
            sensitivities,
            key=lambda value: _SENSITIVITY_ORDER[value],
        )
        evidence_count = len(source_event_ids)
        active_evidence_count = len(active_source_event_ids)
        stability = (
            min(0.95, 0.5 + 0.1 * (active_evidence_count - 1))
            if active_evidence_count
            else 0.0
        )
        salience = max(float(row["salience"]) for row in surfaced_timelines)
        event_start = min(starts)
        event_end = max(ends) if ends else None
        observed_at = max(observations)
        connection.execute(
            """
            UPDATE life_episodes
            SET title = ?, category = ?, domain_category = ?, entity_ids_json = ?,
                status = ?, event_start = ?, event_end = ?, observed_at = ?,
                stability = ?, salience = ?, sensitivity = ?, evidence_count = ?,
                source_event_id = ?
            WHERE episode_id = ?
            """,
            (
                title,
                str(episode["domain_category"]),
                str(episode["domain_category"]),
                _json_ids(entity_ids),
                status,
                event_start.isoformat(),
                event_end.isoformat() if event_end else None,
                observed_at.isoformat(),
                stability,
                salience,
                sensitivity,
                evidence_count,
                source_event_ids[0],
                episode_id,
            ),
        )
        cls._insert_search_document(
            connection,
            account_id=str(episode["account_id"]),
            item_id=episode_id,
            kind="episode",
            memory_kind="episodic",
            title=title,
            body=body,
            domain_category=cast(DomainCategory, str(episode["domain_category"])),
            entity_ids=entity_ids,
            source_event_ids=source_event_ids,
            valid_from=event_start.isoformat(),
            valid_to=event_end.isoformat() if event_end else None,
            occurred_at=event_start.isoformat(),
            observed_at=observed_at.isoformat(),
            stability=stability,
            salience=salience,
            sensitivity=sensitivity,
            conflict_state="none",
            status=status,
        )

    @staticmethod
    def _insert_search_document(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        item_id: str,
        kind: str,
        memory_kind: MemoryKind,
        title: str,
        body: str,
        domain_category: DomainCategory,
        entity_ids: tuple[str, ...],
        source_event_ids: tuple[str, ...],
        valid_from: str | None,
        valid_to: str | None,
        occurred_at: str,
        observed_at: str,
        stability: float,
        salience: float,
        sensitivity: MemorySensitivity,
        conflict_state: ConflictState,
        status: MemoryStatus = "candidate",
    ) -> None:
        if not source_event_ids:
            raise ValueError("memory search projection requires evidence")
        document_id = _stable_id("search", kind, item_id)
        connection.execute(
            """
            INSERT INTO memory_search_documents (
                document_id, account_id, item_id, kind, memory_kind, title, body,
                category, domain_category, entity_ids_json, status, source_event_id,
                occurred_at, valid_from, valid_to, observed_at, stability, salience,
                sensitivity, conflict_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, item_id) DO UPDATE SET
                title = excluded.title,
                body = excluded.body,
                memory_kind = excluded.memory_kind,
                category = excluded.category,
                domain_category = excluded.domain_category,
                entity_ids_json = excluded.entity_ids_json,
                status = excluded.status,
                occurred_at = excluded.occurred_at,
                valid_from = excluded.valid_from,
                valid_to = excluded.valid_to,
                observed_at = excluded.observed_at,
                stability = excluded.stability,
                salience = excluded.salience,
                sensitivity = excluded.sensitivity,
                conflict_state = excluded.conflict_state
            """,
            (
                document_id,
                account_id,
                item_id,
                kind,
                memory_kind,
                title,
                body,
                domain_category,
                domain_category,
                _json_ids(entity_ids),
                status,
                source_event_ids[0],
                occurred_at,
                valid_from,
                valid_to,
                observed_at,
                stability,
                salience,
                sensitivity,
                conflict_state,
            ),
        )
        connection.executemany(
            """
            INSERT OR IGNORE INTO memory_search_document_sources (
                document_id, account_id, source_event_id
            ) VALUES (?, ?, ?)
            """,
            [
                (document_id, account_id, source_event_id)
                for source_event_id in source_event_ids
            ],
        )

    def _record_receipt(
        self,
        connection: sqlite3.Connection,
        event: EvidenceEvent,
        *,
        outcome: str,
        extractor_version: str = "",
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_compile_receipts (
                event_id, account_id, extractor_version, outcome, compiled_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.account_id,
                extractor_version or self._extractor.version,
                outcome,
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _complete_outbox(connection: sqlite3.Connection, outbox_id: str) -> None:
        connection.execute(
            """
            UPDATE processing_outbox
            SET status = 'completed', completed_at = ?, last_error_code = NULL
            WHERE outbox_id = ?
            """,
            (datetime.now(UTC).isoformat(), outbox_id),
        )

    async def search(self, query: MemorySearchQuery) -> MemorySearchResult:
        return self._search(query, confirmed_only=False)

    async def context(self, query: MemorySearchQuery) -> MemorySearchResult:
        return self._search(query, confirmed_only=True)

    def _search(
        self,
        query: MemorySearchQuery,
        *,
        confirmed_only: bool,
    ) -> MemorySearchResult:
        if query.speaker_class != "owner":
            return MemorySearchResult()
        clauses = ["document.account_id = ?"]
        parameters: list[object] = [query.account_id]
        if confirmed_only or not query.include_candidates:
            clauses.append("document.status = 'confirmed'")
            if confirmed_only:
                clauses.append("document.conflict_state != 'active'")
        else:
            clauses.append("document.status != 'retracted'")
        if query.text.strip():
            escaped = (
                query.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append(
                "(document.title LIKE ? ESCAPE '\\' OR document.body LIKE ? ESCAPE '\\')"
            )
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        if query.kinds:
            clauses.append(
                f"document.kind IN ({','.join('?' for _ in query.kinds)})"
            )
            parameters.extend(query.kinds)
        if query.memory_kinds:
            clauses.append(
                f"document.memory_kind IN ({','.join('?' for _ in query.memory_kinds)})"
            )
            parameters.extend(query.memory_kinds)
        if query.domain_categories:
            clauses.append(
                "document.domain_category IN "
                f"({','.join('?' for _ in query.domain_categories)})"
            )
            parameters.extend(query.domain_categories)
        if query.entity_ids:
            placeholders = ",".join("?" for _ in query.entity_ids)
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(document.entity_ids_json) entity "
                f"WHERE entity.value IN ({placeholders}))"
            )
            parameters.extend(query.entity_ids)
        if query.valid_at is not None:
            valid_at = query.valid_at.isoformat()
            clauses.append(
                "(document.valid_from IS NULL OR document.valid_from <= ?) "
                "AND (document.valid_to IS NULL OR document.valid_to >= ?)"
            )
            parameters.extend((valid_at, valid_at))
        if query.sensitivities:
            clauses.append(
                "document.sensitivity IN "
                f"({','.join('?' for _ in query.sensitivities)})"
            )
            parameters.extend(query.sensitivities)
        if query.conflict_states:
            clauses.append(
                "document.conflict_state IN "
                f"({','.join('?' for _ in query.conflict_states)})"
            )
            parameters.extend(query.conflict_states)
        if query.occurred_after is not None:
            clauses.append("document.occurred_at >= ?")
            parameters.append(query.occurred_after.isoformat())
        if query.occurred_before is not None:
            clauses.append("document.occurred_at <= ?")
            parameters.append(query.occurred_before.isoformat())
        parameters.append(query.limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT document.*,
                       (0.35 * document.stability + 0.65 * document.salience) AS score,
                       (
                           SELECT json_group_array(source_event_id)
                           FROM (
                               SELECT source.source_event_id
                               FROM memory_search_document_sources source
                               WHERE source.document_id = document.document_id
                               ORDER BY source.source_event_id
                           )
                       ) AS source_event_ids_json
                FROM memory_search_documents document
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE document.status WHEN 'confirmed' THEN 0 ELSE 1 END,
                         score DESC, document.observed_at DESC, document.document_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return MemorySearchResult(items=tuple(self._search_item(row) for row in rows))

    async def timeline(self, *, account_id: str, limit: int = 50) -> tuple[TimelineItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("timeline requires account_id and limit 1..100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT timeline.*,
                       (
                           SELECT json_group_array(source_event_id)
                           FROM (
                               SELECT evidence.source_event_id
                               FROM episode_evidence evidence
                               WHERE evidence.episode_id = timeline.episode_id
                               ORDER BY evidence.source_event_id
                           )
                       ) AS source_event_ids_json
                FROM timeline_entries timeline
                WHERE timeline.account_id = ? AND timeline.status != 'retracted'
                ORDER BY timeline.event_start DESC, timeline.timeline_id
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        return tuple(
            TimelineItem(
                timeline_id=str(row["timeline_id"]),
                title=str(row["title"]),
                category=cast(MemoryCategory, row["category"]),
                status=cast(MemoryStatus, row["status"]),
                event_start=datetime.fromisoformat(str(row["event_start"])),
                event_end=(
                    datetime.fromisoformat(str(row["event_end"]))
                    if row["event_end"] is not None
                    else None
                ),
                time_precision=str(row["time_precision"]),
                source_event_id=str(row["source_event_id"]),
                episode_id=str(row["episode_id"]),
                domain_category=cast(DomainCategory, row["domain_category"]),
                source_event_ids=_row_ids(row["source_event_ids_json"]),
            )
            for row in rows
        )

    async def people(self, *, account_id: str, limit: int = 100) -> tuple[PersonItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("people requires account_id and limit 1..100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM person_entities
                WHERE account_id = ? AND status != 'retracted'
                ORDER BY created_at, person_id
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
            people: list[PersonItem] = []
            for row in rows:
                aliases = connection.execute(
                    """
                    SELECT DISTINCT alias FROM person_aliases
                    WHERE person_id = ? AND status != 'retracted' ORDER BY alias
                    """,
                    (row["person_id"],),
                ).fetchall()
                people.append(
                    PersonItem(
                        person_id=str(row["person_id"]),
                        display_name=str(row["display_name"]),
                        relationship_to_owner=str(row["relationship_to_owner"]),
                        aliases=tuple(str(alias["alias"]) for alias in aliases),
                        status=cast(MemoryStatus, row["status"]),
                        source_event_id=str(row["source_event_id"]),
                    )
                )
        return tuple(people)

    async def review_queue(self, *, account_id: str) -> tuple[ReviewQueueItem, ...]:
        if not account_id.strip():
            raise ValueError("review queue requires account_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*,
                       EXISTS (
                           SELECT 1 FROM memory_claims other
                           WHERE other.account_id = c.account_id
                             AND other.subject_key = c.subject_key
                             AND other.predicate = c.predicate
                             AND other.value != c.value
                             AND other.status != 'retracted'
                       ) AS has_conflict
                FROM memory_claims c
                WHERE c.account_id = ? AND c.status IN ('candidate', 'disputed')
                ORDER BY c.valid_at, c.claim_id
                """,
                (account_id,),
            ).fetchall()
        return tuple(
            ReviewQueueItem(
                item_id=str(row["claim_id"]),
                kind="claim",
                category=cast(MemoryCategory, row["category"]),
                value=str(row["value"]),
                status=cast(MemoryStatus, row["status"]),
                reason=(
                    "conflicting_values"
                    if str(row["predicate"]) in _SINGLE_VALUE_PREDICATES
                    and bool(row["has_conflict"])
                    else "pending_confirmation"
                ),
                source_event_id=str(row["source_event_id"]),
                memory_kind="semantic",
                domain_category=cast(DomainCategory, row["domain_category"]),
                conflict_state=cast(ConflictState, row["conflict_state"]),
            )
            for row in rows
        )

    async def review(self, command: MemoryClaimReview) -> ReviewedClaim:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_claims
                WHERE claim_id = ? AND account_id = ?
                """,
                (command.claim_id, command.account_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(command.claim_id)

            previous_value = str(row["value"])
            value = (command.corrected_value or previous_value).strip()
            status = _REVIEW_STATUS[command.action]
            review_event = EvidenceEvent(
                event_id=str(uuid.uuid4()),
                account_id=command.account_id,
                event_type="memory.claim_reviewed",
                occurred_at=datetime.now(UTC),
                speaker_class="system",
                source="user.memory_review",
                payload={
                    "target_id": command.claim_id,
                    "action": command.action,
                    "previous_value": previous_value,
                    **(
                        {"corrected_value": value}
                        if command.action == "correct"
                        else {}
                    ),
                },
            )
            self._insert_evidence(connection, review_event)
            self._write_review_projection(
                connection,
                account_id=command.account_id,
                claim_id=command.claim_id,
                status=status,
                projection_status=_PROJECTION_REVIEW_STATUS[command.action],
                title=value[:80],
                value=value,
                review_event_id=review_event.event_id,
            )
        return ReviewedClaim(
            claim_id=command.claim_id,
            status=status,
            value=value,
            review_event_id=review_event.event_id,
        )

    def _replay_review_event(
        self,
        connection: sqlite3.Connection,
        event: EvidenceEvent,
    ) -> None:
        target_id = str(event.payload.get("target_id", ""))
        action = str(event.payload.get("action", ""))
        try:
            status = _REVIEW_STATUS[action]
        except KeyError as exc:
            raise ValueError("invalid memory review evidence") from exc
        row = connection.execute(
            """
            SELECT value FROM memory_claims
            WHERE claim_id = ? AND account_id = ?
            """,
            (target_id, event.account_id),
        ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(target_id)
        value = str(row["value"])
        if action == "correct":
            value = str(event.payload.get("corrected_value", "")).strip()
            if not value:
                raise ValueError("corrected memory value must not be blank")
        self._write_review_projection(
            connection,
            account_id=event.account_id,
            claim_id=target_id,
            status=status,
            projection_status=_PROJECTION_REVIEW_STATUS[action],
            title=value[:80],
            value=value,
            review_event_id=event.event_id,
        )

    @classmethod
    def _write_review_projection(
        cls,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        claim_id: str,
        status: MemoryStatus,
        projection_status: MemoryStatus,
        title: str,
        value: str,
        review_event_id: str,
    ) -> None:
        source = connection.execute(
            """
            SELECT source_event_id, subject_key, predicate FROM memory_claims
            WHERE claim_id = ? AND account_id = ?
            """,
            (claim_id, account_id),
        ).fetchone()
        if source is None:
            raise EvidenceNotFoundError(claim_id)
        source_event_id = str(source["source_event_id"])
        subject_key = str(source["subject_key"])
        predicate = str(source["predicate"])
        episode_rows = connection.execute(
            """
            SELECT episode_id FROM episode_evidence
            WHERE account_id = ? AND source_event_id = ?
            """,
            (account_id, source_event_id),
        ).fetchall()
        connection.execute(
            """
            UPDATE memory_claims
            SET status = ?, value = ?, review_event_id = ?,
                stability = CASE
                    WHEN ? = 'confirmed' THEN MAX(stability, 0.9)
                    WHEN ? = 'retracted' THEN 0
                    ELSE stability
                END
            WHERE claim_id = ? AND account_id = ?
            """,
            (
                status,
                value,
                review_event_id,
                status,
                status,
                claim_id,
                account_id,
            ),
        )
        connection.execute(
            """
            UPDATE memory_search_documents
            SET status = ?, title = ?, body = ?,
                stability = CASE
                    WHEN ? = 'confirmed' THEN MAX(stability, 0.9)
                    WHEN ? = 'retracted' THEN 0
                    ELSE stability
                END
            WHERE kind = 'claim' AND item_id = ? AND account_id = ?
            """,
            (status, title, value, status, status, claim_id, account_id),
        )
        for table in _SOURCE_STATUS_TABLES:
            connection.execute(
                f"UPDATE {table} SET status = ? WHERE account_id = ? AND source_event_id = ?",
                (projection_status, account_id, source_event_id),
            )
        connection.execute(
            """
            UPDATE memory_search_documents
            SET status = ?
            WHERE kind NOT IN ('claim', 'episode') AND account_id = ?
              AND document_id IN (
                  SELECT document_id FROM memory_search_document_sources
                  WHERE account_id = ? AND source_event_id = ?
              )
            """,
            (projection_status, account_id, account_id, source_event_id),
        )
        for episode_row in episode_rows:
            cls._refresh_episode_projection(
                connection,
                episode_id=str(episode_row["episode_id"]),
            )
        cls._refresh_claim_conflicts(
            connection,
            account_id=account_id,
            subject_key=subject_key,
            predicate=predicate,
        )

    @staticmethod
    def _insert_evidence(connection: sqlite3.Connection, event: EvidenceEvent) -> None:
        recorded_at = datetime.now(UTC).isoformat()
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
                recorded_at,
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
                outbox_id, account_id, event_id, task_type, available_at, created_at
            ) VALUES (?, ?, ?, 'compile_evidence', ?, ?)
            """,
            (
                _stable_id("outbox", event.event_id),
                event.account_id,
                event.event_id,
                recorded_at,
                recorded_at,
            ),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> EvidenceEvent:
        return EvidenceEvent(
            event_id=str(row["event_id"]),
            account_id=str(row["account_id"]),
            event_type=str(row["event_type"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            speaker_class=cast(object, row["speaker_class"]),  # type: ignore[arg-type]
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

    @staticmethod
    def _search_item(row: sqlite3.Row) -> MemorySearchItem:
        source_event_ids = _row_ids(row["source_event_ids_json"])
        return MemorySearchItem(
            item_id=str(row["item_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            snippet=str(row["body"]),
            category=cast(MemoryCategory, row["category"]),
            status=cast(MemoryStatus, row["status"]),
            source_event_id=str(row["source_event_id"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            score=float(row["score"]),
            memory_kind=cast(MemoryKind, row["memory_kind"]),
            domain_category=cast(DomainCategory, row["domain_category"]),
            entity_ids=_row_ids(row["entity_ids_json"]),
            source_event_ids=source_event_ids,
            valid_from=(
                datetime.fromisoformat(str(row["valid_from"]))
                if row["valid_from"] is not None
                else None
            ),
            valid_to=(
                datetime.fromisoformat(str(row["valid_to"]))
                if row["valid_to"] is not None
                else None
            ),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            stability=float(row["stability"]),
            salience=float(row["salience"]),
            sensitivity=cast(MemorySensitivity, row["sensitivity"]),
            conflict_state=cast(ConflictState, row["conflict_state"]),
        )
