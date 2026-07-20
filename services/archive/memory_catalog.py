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
from services.archive.memory_domain import (
    AccountWriteGuard,
    AccountWriteRejectedError,
    CompileReport,
    MemoryCategory,
    MemoryClaimReview,
    MemoryExtractor,
    MemorySearchItem,
    MemorySearchQuery,
    MemorySearchResult,
    MemoryStatus,
    PersonItem,
    ReviewedClaim,
    ReviewQueueItem,
    TimelineItem,
)

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
    subject_key TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    sensitive_domain TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    valid_at TEXT NOT NULL,
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
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    event_start TEXT NOT NULL,
    event_end TEXT,
    source_event_id TEXT NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS timeline_entries (
    timeline_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    event_start TEXT NOT NULL,
    event_end TEXT,
    time_precision TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    FOREIGN KEY (episode_id) REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episode_evidence (
    episode_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    PRIMARY KEY (episode_id, source_event_id),
    FOREIGN KEY (episode_id) REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    applicability TEXT NOT NULL,
    counterexample TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    source_event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_search_documents (
    document_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'confirmed', 'disputed', 'retracted')),
    source_event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (kind, item_id),
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_search_account_status_time
ON memory_search_documents(account_id, status, occurred_at DESC);
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
    "life_episodes",
    "timeline_entries",
    "knowledge_items",
)


@asynccontextmanager
async def _allow_account_write(_: str) -> AsyncIterator[None]:
    yield


def _stable_id(kind: str, *values: object) -> str:
    key = ":".join(str(value) for value in values)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{kind}:{key}"))


class MemoryCatalog:
    """Compile evidence into projections and expose their stable public seam."""

    def __init__(
        self,
        sqlite_path: Path,
        *,
        extractor: MemoryExtractor,
        account_guard: AccountWriteGuard | None = None,
    ) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._extractor = extractor
        self._account_guard = account_guard or _allow_account_write
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        extractor: MemoryExtractor,
        account_guard: AccountWriteGuard | None = None,
    ) -> MemoryCatalog:
        return cls(Path(path), extractor=extractor, account_guard=account_guard)

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
                episode_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(episode_evidence)")
                }
                if "account_id" not in episode_columns:
                    connection.execute("ALTER TABLE episode_evidence ADD COLUMN account_id TEXT")
                    connection.execute(
                        """
                        UPDATE episode_evidence
                        SET account_id = (
                            SELECT account_id FROM life_episodes
                            WHERE life_episodes.episode_id = episode_evidence.episode_id
                        )
                        """
                    )
                alias_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(person_aliases)")
                }
                if "status" not in alias_columns:
                    connection.execute(
                        """
                        ALTER TABLE person_aliases ADD COLUMN status TEXT NOT NULL
                        DEFAULT 'candidate' CHECK (
                            status IN ('candidate', 'confirmed', 'disputed', 'retracted')
                        )
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
        if event.event_type != "speech.utterance_finalized" or event.speaker_class != "owner":
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
            connection.execute(
                """
                INSERT OR IGNORE INTO memory_claims (
                    claim_id, account_id, category, subject_key, predicate,
                    value, confidence, sensitive_domain, extractor_version,
                    source_event_id, valid_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    event.account_id,
                    claim.category,
                    claim.subject_key,
                    claim.predicate,
                    claim.value,
                    claim.confidence,
                    claim.sensitive_domain,
                    extraction.extractor_version or self._extractor.version,
                    event.event_id,
                    occurred_at,
                    created_at,
                ),
            )
            self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=claim_id,
                kind="claim",
                title=claim.value[:80],
                body=claim.value,
                category=claim.category,
                source_event_id=event.event_id,
                occurred_at=occurred_at,
            )

        for index, timeline in enumerate(extraction.timeline):
            episode_id = _stable_id(
                "episode",
                event.account_id,
                event.session_id or event.event_id,
                timeline.category,
                timeline.event_start.date(),
                index,
            )
            timeline_id = _stable_id("timeline", event.event_id, index)
            event_start = timeline.event_start.isoformat()
            event_end = timeline.event_end.isoformat() if timeline.event_end else None
            connection.execute(
                """
                INSERT OR IGNORE INTO life_episodes (
                    episode_id, account_id, title, category, event_start,
                    event_end, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    event.account_id,
                    timeline.title,
                    timeline.category,
                    event_start,
                    event_end,
                    event.event_id,
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO episode_evidence (
                    episode_id, account_id, source_event_id
                ) VALUES (?, ?, ?)
                """,
                (episode_id, event.account_id, event.event_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO timeline_entries (
                    timeline_id, account_id, episode_id, title, category,
                    event_start, event_end, time_precision, source_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timeline_id,
                    event.account_id,
                    episode_id,
                    timeline.title,
                    timeline.category,
                    event_start,
                    event_end,
                    timeline.time_precision,
                    event.event_id,
                ),
            )
            self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=timeline_id,
                kind="timeline",
                title=timeline.title,
                body=timeline.title,
                category=timeline.category,
                source_event_id=event.event_id,
                occurred_at=event_start,
            )

        for index, knowledge in enumerate(extraction.knowledge):
            knowledge_id = _stable_id("knowledge", event.event_id, index)
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_items (
                    knowledge_id, account_id, category, question, answer,
                    applicability, counterexample, source_event_id, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    knowledge_id,
                    event.account_id,
                    knowledge.category,
                    knowledge.question,
                    knowledge.answer,
                    knowledge.applicability,
                    knowledge.counterexample,
                    event.event_id,
                    occurred_at,
                ),
            )
            self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=knowledge_id,
                kind="knowledge",
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
                category=knowledge.category,
                source_event_id=event.event_id,
                occurred_at=occurred_at,
            )

    @staticmethod
    def _insert_search_document(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        item_id: str,
        kind: str,
        title: str,
        body: str,
        category: MemoryCategory,
        source_event_id: str,
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO memory_search_documents (
                document_id, account_id, item_id, kind, title, body,
                category, status, source_event_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
            """,
            (
                _stable_id("search", kind, item_id),
                account_id,
                item_id,
                kind,
                title,
                body,
                category,
                source_event_id,
                occurred_at,
            ),
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
        clauses = ["account_id = ?"]
        parameters: list[object] = [query.account_id]
        if confirmed_only or not query.include_candidates:
            clauses.append("status = 'confirmed'")
        else:
            clauses.append("status != 'retracted'")
        if query.text.strip():
            escaped = (
                query.text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            clauses.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')")
            parameters.extend((f"%{escaped}%", f"%{escaped}%"))
        if query.kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in query.kinds)})")
            parameters.extend(query.kinds)
        if query.categories:
            clauses.append(f"category IN ({','.join('?' for _ in query.categories)})")
            parameters.extend(query.categories)
        if query.occurred_after is not None:
            clauses.append("occurred_at >= ?")
            parameters.append(query.occurred_after.isoformat())
        if query.occurred_before is not None:
            clauses.append("occurred_at <= ?")
            parameters.append(query.occurred_before.isoformat())
        parameters.append(query.limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_search_documents
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END,
                         occurred_at DESC, document_id
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
                SELECT * FROM timeline_entries
                WHERE account_id = ? AND status != 'retracted'
                ORDER BY event_start DESC, timeline_id
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

    @staticmethod
    def _write_review_projection(
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
            SELECT source_event_id FROM memory_claims
            WHERE claim_id = ? AND account_id = ?
            """,
            (claim_id, account_id),
        ).fetchone()
        if source is None:
            raise EvidenceNotFoundError(claim_id)
        source_event_id = str(source["source_event_id"])
        connection.execute(
            """
            UPDATE memory_claims
            SET status = ?, value = ?, review_event_id = ?
            WHERE claim_id = ? AND account_id = ?
            """,
            (status, value, review_event_id, claim_id, account_id),
        )
        connection.execute(
            """
            UPDATE memory_search_documents
            SET status = ?, title = ?, body = ?
            WHERE kind = 'claim' AND item_id = ? AND account_id = ?
            """,
            (status, title, value, claim_id, account_id),
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
            WHERE kind != 'claim' AND account_id = ? AND source_event_id = ?
            """,
            (projection_status, account_id, source_event_id),
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
        return MemorySearchItem(
            item_id=str(row["item_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            snippet=str(row["body"]),
            category=cast(MemoryCategory, row["category"]),
            status=cast(MemoryStatus, row["status"]),
            source_event_id=str(row["source_event_id"]),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            score=1.0,
        )
