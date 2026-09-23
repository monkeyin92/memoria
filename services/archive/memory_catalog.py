"""Rebuildable SQLite projections for the owner's long-term memory catalog."""

from __future__ import annotations

import inspect
import json
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
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
    MemoryExtraction,
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
    lexical_query_terms,
    subject_lineage_predicates,
)
from services.archive.memory_write_policy import (
    SINGLE_VALUE_PREDICATES,
    MemoryWriteDecision,
    MemoryWritePolicy,
    filter_extraction_for_subject,
    is_policy_confirmation_event,
)
from services.common.evidence_policy import contribution_for
from services.common.redaction import redact_pii

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

_SINGLE_VALUE_PREDICATES = SINGLE_VALUE_PREDICATES
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

# Scoped-review cascade guards.  Rows whose identity merged several speakers
# are skipped instead of being rewritten through one of their sources: only
# ``person_entities`` (via ``person_aliases``) and ``timeline_entries`` /
# ``episode_evidence`` (via the episode) can carry more than their own source
# event; the remaining tables above are single-source rows.
_SCOPED_CASCADE_GUARDS: dict[str, str] = {
    "person_entities": (
        "NOT EXISTS (SELECT 1 FROM person_aliases alias"
        " LEFT JOIN {evidence} lineage ON lineage.event_id = alias.source_event_id"
        " AND lineage.account_id = alias.account_id"
        " WHERE alias.person_id = person_entities.person_id"
        " AND alias.account_id = person_entities.account_id"
        " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
    ),
    "timeline_entries": (
        "NOT EXISTS (SELECT 1 FROM episode_evidence merged"
        " LEFT JOIN {evidence} lineage ON lineage.event_id = merged.source_event_id"
        " AND lineage.account_id = merged.account_id"
        " WHERE merged.episode_id = timeline_entries.episode_id"
        " AND merged.account_id = timeline_entries.account_id"
        " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
    ),
    "episode_evidence": (
        "NOT EXISTS (SELECT 1 FROM episode_evidence merged"
        " LEFT JOIN {evidence} lineage ON lineage.event_id = merged.source_event_id"
        " AND lineage.account_id = merged.account_id"
        " WHERE merged.episode_id = episode_evidence.episode_id"
        " AND merged.account_id = episode_evidence.account_id"
        " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
    ),
}

_SCOPED_DOCUMENT_CASCADE_GUARD = (
    "AND NOT EXISTS (SELECT 1 FROM memory_search_document_sources merged"
    " LEFT JOIN evidence_events lineage ON lineage.event_id = merged.source_event_id"
    " AND lineage.account_id = merged.account_id"
    " WHERE merged.document_id = memory_search_documents.document_id"
    " AND merged.account_id = memory_search_documents.account_id"
    " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
)


@asynccontextmanager
async def _allow_account_write(_: str) -> AsyncIterator[None]:
    yield


def _stable_id(kind: str, *values: object) -> str:
    key = ":".join(str(value) for value in values)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{kind}:{key}"))


def _bind_question_mark(parameters: list[object]) -> Callable[[str], str]:
    """SQLite placeholder binder for :func:`subject_lineage_predicates`."""

    def bind(value: str) -> str:
        parameters.append(value)
        return "?"

    return bind


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
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


class SubjectCategoryUnresolved(Exception):
    """The evidence names another person whose category cannot be classified.

    ``None`` from a resolver is "category unknown", and unknown still compiles
    through the non-minor path.  Only a caller that looked and could not decide
    -- identity down, or the person missing -- raises this.  The catalog then
    records an ignored receipt and leaves the archive evidence unprojected.
    """


async def _await_category(value: str | None | Awaitable[str | None]) -> str | None:
    """Accept a sync category or one looked up on the compiler's own loop.

    The compiler calls this from ``async def``.  An awaitable resolver must be
    awaited here so a Postgres identity pool stays on the loop that created it.
    Opening another thread and calling ``asyncio.run`` binds that pool to the
    wrong loop and turns every other-subject lookup into a skipped projection.
    """

    if inspect.isawaitable(value):
        return await value
    return value


def _confirmed_evidence_subject(event: EvidenceEvent) -> str | None:
    """The speaker the evidence actually names, or None when it names nobody.

    A blank subject is unclaimed.  It is not the account owner and not another
    person, so conflict folds stay inside other unclaimed rows.
    """

    subject_id = event.subject_id.strip() if isinstance(event.subject_id, str) else ""
    return subject_id or None


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
        subject_category_resolver: Callable[[str], str | None] | None = None,
        evidence_subject_category_resolver: (
            Callable[[EvidenceEvent], str | None | Awaitable[str | None]] | None
        ) = None,
    ) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._extractor = extractor
        self._account_guard = account_guard or _allow_account_write
        self._episode_consolidator = episode_consolidator or EpisodeConsolidator()
        self._memory_write_policy = MemoryWritePolicy()
        self._subject_category_resolver = subject_category_resolver or (lambda _: None)
        # Compilation must ask about the evidence subject.  Callers that only
        # know the account owner keep the account resolver; it is not a guess
        # that every subject is that owner.
        self._evidence_subject_category_resolver = (
            evidence_subject_category_resolver
            or (lambda event: self._subject_category_resolver(event.account_id))
        )
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
        subject_category_resolver: Callable[[str], str | None] | None = None,
        evidence_subject_category_resolver: (
            Callable[[EvidenceEvent], str | None | Awaitable[str | None]] | None
        ) = None,
    ) -> MemoryCatalog:
        return cls(
            Path(path),
            extractor=extractor,
            account_guard=account_guard,
            episode_consolidator=episode_consolidator,
            subject_category_resolver=subject_category_resolver,
            evidence_subject_category_resolver=evidence_subject_category_resolver,
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
        if event.event_type == "memory.claim_reviewed" and (
            event.source == "user.memory_review" or is_policy_confirmation_event(event)
        ):
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
            if not isinstance(extraction, MemoryExtraction):
                raise TypeError("memory extractor returned an invalid result")
            try:
                subject_category = await _await_category(
                    self._evidence_subject_category_resolver(event)
                )
            except SubjectCategoryUnresolved:
                # Identity could not classify this subject.  Skip the long-term
                # projection rather than guessing adult or copying the account
                # owner's allowlist.  None is not this signal: an unknown
                # category still compiles on the non-minor path.  The archive
                # evidence itself stays; this receipt only records that the
                # catalog did not project it.
                with self._connect() as connection:
                    self._record_receipt(connection, event, outcome="ignored")
                    self._complete_outbox(connection, outbox_id)
                return "ignored"
            extraction = filter_extraction_for_subject(
                event,
                extraction,
                subject_category=subject_category,
            )
            with self._connect() as connection:
                decision = self._memory_write_policy.decide(
                    event,
                    extraction,
                    existing_values=self._existing_single_value_claims(
                        connection,
                        event=event,
                        extraction=extraction,
                    ),
                    subject_category=subject_category,
                )
                self._write_extraction(connection, event, extraction)
                self._apply_memory_write_decision(
                    connection,
                    event=event,
                    extraction=extraction,
                    decision=decision,
                )
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
            # A person is only recallable through the search projection. Without
            # it the owner can confirm "我妈妈叫李梅，家里人也叫她阿梅" and still
            # never get an answer to "阿梅是谁？": the alias lives in
            # person_aliases, which no read path searches. The document carries
            # the relationship and every alias so nickname-only questions work.
            self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=person_id,
                kind="person",
                memory_kind="relationship",
                title=person.display_name,
                body=" ".join(
                    dict.fromkeys(
                        part
                        for part in (
                            person.relationship_to_owner,
                            person.display_name,
                            *person.aliases,
                        )
                        if part
                    )
                )[:8000],
                domain_category=(
                    extraction.claims[0].domain_category
                    if extraction.claims
                    else "daily_life"
                ),
                entity_ids=(person_id,),
                source_event_ids=(event.event_id,),
                valid_from=None,
                valid_to=None,
                occurred_at=occurred_at,
                observed_at=created_at,
                stability=0.7,
                salience=0.6,
                sensitivity="personal",
                conflict_state="none",
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
                body=(
                    f"subject:{claim.subject_key} predicate:{claim.predicate} "
                    f"value:{claim.value} context:{redact_pii(str(event.payload.get('text') or ''))[:240]}"
                )[:8000],
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
            # A named subject folds only its own lineage.  A blank subject
            # folds only other unclaimed rows: passing no subject here would
            # mark the account owner's claims as conflicts too.
            claim_subject_id = _confirmed_evidence_subject(event)
            self._refresh_claim_conflicts(
                connection,
                account_id=event.account_id,
                subject_key=claim.subject_key,
                predicate=claim.predicate,
                subject_id=claim_subject_id,
                unclaimed_only=claim_subject_id is None,
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
                body=(
                    " ".join(
                        part
                        for part in (
                            knowledge.answer,
                            knowledge.applicability,
                            knowledge.counterexample,
                        )
                        if part
                    )
                    + f"\n[retrieval-context] {redact_pii(str(event.payload.get('text') or ''))[:240]}"
                )[:8000],
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
    def _existing_single_value_claims(
        connection: sqlite3.Connection,
        *,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
    ) -> tuple[str, ...]:
        if len(extraction.claims) != 1:
            return ()
        claim = extraction.claims[0]
        if claim.predicate not in SINGLE_VALUE_PREDICATES:
            return ()
        # A blank evidence subject is an unclaimed speaker.  It may still
        # conflict with other unclaimed rows, but it must not inherit a value
        # that already belongs to a named subject, and it must not be stored
        # as the account owner: NULL means "no confirmed speaker".  Two named
        # subjects that share subject_key="self" are different speakers and
        # must not see each other's values here either.
        subject_id = _confirmed_evidence_subject(event) or ""
        lineage = (
            "(SELECT lineage.subject_id FROM evidence_events lineage"
            " WHERE lineage.event_id = memory_claims.source_event_id"
            " AND lineage.account_id = memory_claims.account_id)"
        )
        if subject_id:
            lineage_clause = f" AND {lineage} = ?"
            lineage_parameters: tuple[object, ...] = (subject_id,)
        else:
            lineage_clause = f" AND {lineage} IS NULL"
            lineage_parameters = ()
        rows = connection.execute(
            f"""
            SELECT value, source_event_id FROM memory_claims
            WHERE account_id = ? AND subject_key = ? AND predicate = ?
              AND status != 'retracted'
              {lineage_clause}
            """,
            (event.account_id, claim.subject_key, claim.predicate, *lineage_parameters),
        ).fetchall()
        return tuple(str(row["value"]) for row in rows)

    def _apply_memory_write_decision(
        self,
        connection: sqlite3.Connection,
        *,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
        decision: MemoryWriteDecision,
    ) -> None:
        if not decision.confirmed:
            return
        claim = extraction.claims[0]
        claim_id = _stable_id("claim", event.event_id, 0)
        review_event = decision.confirmation_event(
            source=event,
            claim_id=claim_id,
            claim_value=claim.value,
        )
        self._insert_evidence(connection, review_event, if_absent=True)
        self._write_review_projection(
            connection,
            account_id=event.account_id,
            claim_id=claim_id,
            status="confirmed",
            projection_status="confirmed",
            title=claim.value[:80],
            value=claim.value,
            review_event_id=review_event.event_id,
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
            WHERE account_id = ?
              AND (domain_category = ? OR consolidation_key LIKE 'canonical:%')
              AND status != 'retracted'
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
        subject_id: str | None = None,
        unclaimed_only: bool = False,
    ) -> None:
        # An explicit scope keeps the conflict fold inside one speaker: another
        # subject's claims share ``subject_key`` ("self") and must neither
        # decide nor receive this review's conflict state.  The scope is the
        # same lineage contract the read path uses -- account-consistent own
        # evidence plus every source the claim's projection merged -- so a claim
        # that a scoped read hides is not folded here either.  ``unclaimed_only``
        # is the compile path for a blank evidence subject: those rows fold
        # among themselves and never against a named subject.
        scope = ""
        scope_parameters: list[object] = []
        if unclaimed_only:
            scope = (
                " AND (SELECT lineage.subject_id FROM evidence_events lineage"
                " WHERE lineage.event_id = memory_claims.source_event_id"
                " AND lineage.account_id = memory_claims.account_id) IS NULL"
            )
        elif subject_id is not None:

            def bind(value: str) -> str:
                scope_parameters.append(value)
                return "?"

            scope = "".join(
                f" AND {lineage}"
                for lineage in subject_lineage_predicates(
                    bind=bind,
                    subject_id=subject_id,
                    account_column="memory_claims.account_id",
                    single_source_column="memory_claims.source_event_id",
                    document_source_item=(
                        "claim",
                        "memory_claims.claim_id",
                        "memory_claims.account_id",
                    ),
                )
            )
        rows = connection.execute(
            f"""
            SELECT claim_id, value
            FROM memory_claims
            WHERE account_id = ? AND subject_key = ? AND predicate = ?
              AND status != 'retracted'
              {scope}
            """,
            [account_id, subject_key, predicate, *scope_parameters],
        ).fetchall()
        state: ConflictState = (
            "active"
            if predicate in _SINGLE_VALUE_PREDICATES
            and len({str(row["value"]) for row in rows}) > 1
            else "none"
        )
        connection.execute(
            f"""
            UPDATE memory_claims
            SET conflict_state = ?
            WHERE account_id = ? AND subject_key = ? AND predicate = ?
              {scope}
            """,
            [state, account_id, subject_key, predicate, *scope_parameters],
        )
        connection.execute(
            f"""
            UPDATE memory_search_documents
            SET conflict_state = ?
            WHERE account_id = ? AND kind = 'claim' AND item_id IN (
                SELECT claim_id FROM memory_claims
                WHERE account_id = ? AND subject_key = ? AND predicate = ?
                  {scope}
            )
            """,
            [state, account_id, account_id, subject_key, predicate, *scope_parameters],
        )

    @staticmethod
    def _episode_merged_other_subjects(
        connection: sqlite3.Connection,
        *,
        episode_id: str,
        account_id: str,
        subject_id: str,
    ) -> bool:
        """Whether an episode carries any source that is not this subject's.

        A link row from another account -- or one whose evidence row sits in a
        different account than the link itself -- counts as foreign, so the
        refresh leaves the episode alone instead of rewriting it.
        """

        row = connection.execute(
            """
            SELECT 1
            FROM episode_evidence evidence
            LEFT JOIN evidence_events source
              ON source.event_id = evidence.source_event_id
             AND source.account_id = evidence.account_id
            WHERE evidence.episode_id = ?
              AND evidence.account_id = ?
              AND (source.subject_id IS NULL OR source.subject_id <> ?)
            LIMIT 1
            """,
            (episode_id, account_id, subject_id),
        ).fetchone()
        return row is not None

    @classmethod
    def _refresh_episode_projection(
        cls,
        connection: sqlite3.Connection,
        *,
        episode_id: str,
        account_id: str | None = None,
        subject_id: str | None = None,
    ) -> None:
        if subject_id is not None and account_id is None:
            raise ValueError("scoped episode refresh requires account_id")
        if subject_id is not None and account_id is not None and cls._episode_merged_other_subjects(
            connection,
            episode_id=episode_id,
            account_id=account_id,
            subject_id=subject_id,
        ):
            # The episode was consolidated across speakers; rewriting it here
            # would push this review into a projection it does not own.
            return
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
            str(row["source_event_id"]) for row in evidence if str(row["status"]) != "retracted"
        )
        retracted_source_event_ids = tuple(
            str(row["source_event_id"]) for row in evidence if str(row["status"]) == "retracted"
        )
        source_event_ids = (
            *active_source_event_ids,
            *retracted_source_event_ids,
        )
        active_timelines = [row for row in timelines if str(row["status"]) != "retracted"]
        surfaced_timelines = active_timelines or list(timelines)
        titles = tuple(dict.fromkeys(str(row["title"]) for row in surfaced_timelines))
        title = max(titles, key=lambda value: (len(value), value))
        source_texts: list[str] = []
        for source_event_id in source_event_ids:
            source_row = connection.execute(
                "SELECT payload_json FROM evidence_events WHERE event_id = ?",
                (source_event_id,),
            ).fetchone()
            if source_row is None:
                continue
            try:
                payload = json.loads(str(source_row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict) and str(payload.get("text") or "").strip():
                source_texts.append(redact_pii(str(payload["text"]))[:240])
        body = f"{';'.join(titles)}\n[retrieval-context] {'；'.join(source_texts)}"[:8000]
        starts = [datetime.fromisoformat(str(row["event_start"])) for row in surfaced_timelines]
        ends = [
            datetime.fromisoformat(str(row["event_end"]))
            for row in surfaced_timelines
            if row["event_end"] is not None
        ]
        observations = [
            datetime.fromisoformat(str(row["observed_at"])) for row in surfaced_timelines
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
            cast(MemorySensitivity, str(row["sensitivity"])) for row in surfaced_timelines
        ]
        sensitivity = max(
            sensitivities,
            key=lambda value: _SENSITIVITY_ORDER[value],
        )
        evidence_count = len(source_event_ids)
        active_evidence_count = len(active_source_event_ids)
        stability = (
            min(0.95, 0.5 + 0.1 * (active_evidence_count - 1)) if active_evidence_count else 0.0
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
            [(document_id, account_id, source_event_id) for source_event_id in source_event_ids],
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
            clauses.append("document.conflict_state != 'active'")
        else:
            clauses.append("document.status != 'retracted'")
        if query.text.strip():
            terms = lexical_query_terms(query.text)
            if not terms:
                clauses.append("1 = 0")
            else:
                term_clauses: list[str] = []
                for term in terms:
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    term_clauses.append(
                        "(document.title LIKE ? ESCAPE '\\' OR document.body LIKE ? ESCAPE '\\')"
                    )
                    parameters.extend((f"%{escaped}%", f"%{escaped}%"))
                clauses.append("(" + " OR ".join(term_clauses) + ")")
        if query.kinds:
            clauses.append(f"document.kind IN ({','.join('?' for _ in query.kinds)})")
            parameters.extend(query.kinds)
        if query.memory_kinds:
            clauses.append(f"document.memory_kind IN ({','.join('?' for _ in query.memory_kinds)})")
            parameters.extend(query.memory_kinds)
        if query.domain_categories:
            clauses.append(
                f"document.domain_category IN ({','.join('?' for _ in query.domain_categories)})"
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
                f"document.sensitivity IN ({','.join('?' for _ in query.sensitivities)})"
            )
            parameters.extend(query.sensitivities)
        if query.conflict_states:
            clauses.append(
                f"document.conflict_state IN ({','.join('?' for _ in query.conflict_states)})"
            )
            parameters.extend(query.conflict_states)
        if query.occurred_after is not None:
            clauses.append("document.occurred_at >= ?")
            parameters.append(query.occurred_after.isoformat())
        if query.occurred_before is not None:
            clauses.append("document.occurred_at <= ?")
            parameters.append(query.occurred_before.isoformat())
        if query.subject_id is not None:
            # Explicit scope: the document's own evidence plus every source it
            # was merged from must resolve to this subject.
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_question_mark(parameters),
                    subject_id=query.subject_id,
                    account_column="document.account_id",
                    single_source_column="document.source_event_id",
                    merged_source_link=(
                        "memory_search_document_sources",
                        "document_id",
                        "document.document_id",
                    ),
                )
            )
        parameters.append(query.limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT document.*, claim.value AS claim_value,
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
                LEFT JOIN memory_claims claim
                  ON document.kind = 'claim'
                 AND claim.claim_id = document.item_id
                 AND claim.account_id = document.account_id
                WHERE {" AND ".join(clauses)}
                ORDER BY CASE document.status WHEN 'confirmed' THEN 0 ELSE 1 END,
                         score DESC, document.observed_at DESC, document.document_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            items = tuple(self._search_item(row) for row in rows)
        if query.text.strip() and items:
            terms = lexical_query_terms(query.text)
            ranked = sorted(
                (
                    (
                        sum(
                            1
                            for term in terms
                            if term in str(row["title"]).lower() or term in str(row["body"]).lower()
                        )
                        + item.score * 0.1,
                        item,
                    )
                    for row, item in zip(rows, items, strict=True)
                ),
                key=lambda scored: (
                    scored[0],
                    scored[1].observed_at,
                ),
                reverse=True,
            )
            items = tuple(replace(item, score=score) for score, item in ranked)
        return MemorySearchResult(items=items)

    async def timeline(
        self,
        *,
        account_id: str,
        limit: int = 50,
        subject_id: str | None = None,
    ) -> tuple[TimelineItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("timeline requires account_id and limit 1..100")
        if subject_id is not None and not subject_id.strip():
            raise ValueError("timeline subject_id must not be blank")
        clauses = ["timeline.account_id = ?", "timeline.status != 'retracted'"]
        parameters: list[object] = [account_id]
        if subject_id is not None:
            # A timeline entry merges every source of its episode: the entry's
            # own evidence and the whole episode set must be the same subject.
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_question_mark(parameters),
                    subject_id=subject_id,
                    account_column="timeline.account_id",
                    single_source_column="timeline.source_event_id",
                    merged_source_link=(
                        "episode_evidence",
                        "episode_id",
                        "timeline.episode_id",
                    ),
                )
            )
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
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
                WHERE {" AND ".join(clauses)}
                ORDER BY timeline.event_start DESC, timeline.timeline_id
                LIMIT ?
                """,
                parameters,
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

    async def people(
        self,
        *,
        account_id: str,
        limit: int = 100,
        subject_id: str | None = None,
    ) -> tuple[PersonItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("people requires account_id and limit 1..100")
        if subject_id is not None and not subject_id.strip():
            raise ValueError("people subject_id must not be blank")
        clauses = ["entity.account_id = ?", "entity.status != 'retracted'"]
        parameters: list[object] = [account_id]
        if subject_id is not None:
            # Every alias is its own evidence row, so one alias learned from
            # another subject hides the whole person instead of leaking a name.
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_question_mark(parameters),
                    subject_id=subject_id,
                    account_column="entity.account_id",
                    single_source_column="entity.source_event_id",
                    merged_source_link=(
                        "person_aliases",
                        "person_id",
                        "entity.person_id",
                    ),
                    require_merged_source=False,
                    document_source_item=(
                        "person",
                        "entity.person_id",
                        "entity.account_id",
                    ),
                )
            )
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT entity.* FROM person_entities entity
                WHERE {" AND ".join(clauses)}
                ORDER BY entity.created_at, entity.person_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            people: list[PersonItem] = []
            for row in rows:
                aliases = connection.execute(
                    """
                    SELECT DISTINCT alias FROM person_aliases
                    WHERE person_id = ? AND status = ? ORDER BY alias
                    """,
                    (row["person_id"], row["status"]),
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

    async def review_queue(
        self,
        *,
        account_id: str,
        subject_id: str | None = None,
    ) -> tuple[ReviewQueueItem, ...]:
        if not account_id.strip():
            raise ValueError("review queue requires account_id")
        if subject_id is not None and not subject_id.strip():
            raise ValueError("review queue subject_id must not be blank")
        parameters: list[object] = []
        # The conflict probe reads memory_claims a second time.  Under an
        # explicit scope it must not compare this subject's values against
        # another subject's rows: the probed claim carries the same lineage
        # contract as the read path (account-consistent own evidence plus every
        # source its projection merged).  Its parameters come first in the
        # statement text because the probe sits in the select list.
        conflict_scope = ""
        if subject_id is not None:
            conflict_scope = "".join(
                f" AND {lineage}"
                for lineage in subject_lineage_predicates(
                    bind=_bind_question_mark(parameters),
                    subject_id=subject_id,
                    account_column="other.account_id",
                    single_source_column="other.source_event_id",
                    document_source_item=(
                        "claim",
                        "other.claim_id",
                        "other.account_id",
                    ),
                )
            )
        clauses = ["c.account_id = ?", "c.status IN ('candidate', 'disputed')"]
        parameters.append(account_id)
        if subject_id is not None:
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_question_mark(parameters),
                    subject_id=subject_id,
                    account_column="c.account_id",
                    single_source_column="c.source_event_id",
                    document_source_item=("claim", "c.claim_id", "c.account_id"),
                )
            )
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT c.*,
                       EXISTS (
                           SELECT 1 FROM memory_claims other
                           WHERE other.account_id = c.account_id
                             AND other.subject_key = c.subject_key
                             AND other.predicate = c.predicate
                             AND other.value != c.value
                             AND other.status != 'retracted'
                           {conflict_scope}
                       ) AS has_conflict
                FROM memory_claims c
                WHERE {" AND ".join(clauses)}
                ORDER BY c.valid_at, c.claim_id
                """,
                parameters,
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
            parameters: list[object] = [command.claim_id, command.account_id]
            clauses = ["claim.claim_id = ?", "claim.account_id = ?"]
            if command.subject_id is not None:
                # A scoped reviewer may only reach claims fed by its own
                # subject's evidence; the miss below is the same not-found a
                # nonexistent id returns, so ids stay unguessable.
                clauses.extend(
                    subject_lineage_predicates(
                        bind=_bind_question_mark(parameters),
                        subject_id=command.subject_id,
                        account_column="claim.account_id",
                        single_source_column="claim.source_event_id",
                        document_source_item=(
                            "claim",
                            "claim.claim_id",
                            "claim.account_id",
                        ),
                    )
                )
            row = connection.execute(
                f"""
                SELECT claim.*,
                       (
                           SELECT lineage.subject_id FROM evidence_events lineage
                           WHERE lineage.event_id = claim.source_event_id
                             AND lineage.account_id = claim.account_id
                       ) AS source_subject_id
                FROM memory_claims claim
                WHERE {" AND ".join(clauses)}
                """,
                parameters,
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
                    **({"corrected_value": value} if command.action == "correct" else {}),
                },
                # The review inherits the reviewed claim's own speaker instead
                # of inventing one; an unclaimed source stays unclaimed.
                subject_id=cast(str | None, row["source_subject_id"]),
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
                subject_id=command.subject_id,
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
            SELECT value, source_event_id FROM memory_claims
            WHERE claim_id = ? AND account_id = ?
            """,
            (target_id, event.account_id),
        ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(target_id)
        if is_policy_confirmation_event(event) and str(row["source_event_id"]) != str(
            event.payload.get("source_event_id")
        ):
            raise ValueError("memory policy confirmation source does not match its claim")
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
            subject_id=event.subject_id,
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
        subject_id: str | None = None,
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
        source_event = connection.execute(
            "SELECT payload_json FROM evidence_events"
            " WHERE event_id = ? AND account_id = ?",
            (source_event_id, account_id),
        ).fetchone()
        source_text = ""
        if source_event is not None:
            try:
                payload = json.loads(str(source_event["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                source_text = redact_pii(str(payload.get("text") or ""))[:240]
        contextual_body = (
            f"subject:{subject_key} predicate:{predicate} value:{value} context:{source_text}"
        )[:8000]
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
            (status, title, contextual_body, status, status, claim_id, account_id),
        )
        for table in _SOURCE_STATUS_TABLES:
            guard = ""
            if subject_id is not None:
                template = _SCOPED_CASCADE_GUARDS.get(table)
                if template is not None:
                    guard = " AND " + template.format(evidence="evidence_events")
            connection.execute(
                f"UPDATE {table} SET status = ?"
                " WHERE account_id = ? AND source_event_id = ?"
                f"{guard}",
                (
                    projection_status,
                    account_id,
                    source_event_id,
                    *([subject_id] if guard else []),
                ),
            )
        connection.execute(
            f"""
            UPDATE memory_search_documents
            SET status = ?
            WHERE kind NOT IN ('claim', 'episode') AND account_id = ?
              AND document_id IN (
                  SELECT document_id FROM memory_search_document_sources
                  WHERE account_id = ? AND source_event_id = ?
              )
              {_SCOPED_DOCUMENT_CASCADE_GUARD if subject_id is not None else ""}
            """,
            (
                projection_status,
                account_id,
                account_id,
                source_event_id,
                *([subject_id] if subject_id is not None else []),
            ),
        )
        for episode_row in episode_rows:
            cls._refresh_episode_projection(
                connection,
                episode_id=str(episode_row["episode_id"]),
                account_id=account_id,
                subject_id=subject_id,
            )
        cls._refresh_claim_conflicts(
            connection,
            account_id=account_id,
            subject_key=subject_key,
            predicate=predicate,
            subject_id=subject_id,
        )

    @staticmethod
    def _insert_evidence(
        connection: sqlite3.Connection,
        event: EvidenceEvent,
        *,
        if_absent: bool = False,
    ) -> None:
        recorded_at = datetime.now(UTC).isoformat()
        insert = "INSERT OR IGNORE" if if_absent else "INSERT"
        connection.execute(
            f"""
            {insert} INTO evidence_events (
                event_id, account_id, session_id, turn_id, generation_id,
                event_type, schema_version, occurred_at, recorded_at,
                subject_id, speaker_identity_id, speaker_class, source,
                consent_grant_id, payload_json, content_sha256,
                supersedes_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                event.subject_id,
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
            f"""
            {insert} INTO processing_outbox (
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
            subject_id=cast(str | None, row["subject_id"]),
            speaker_identity_id=row["speaker_identity_id"],
            consent_grant_id=row["consent_grant_id"],
            schema_version=int(row["schema_version"]),
            supersedes_event_id=row["supersedes_event_id"],
        )

    @staticmethod
    def _search_item(row: sqlite3.Row) -> MemorySearchItem:
        source_event_ids = _row_ids(row["source_event_ids_json"])
        claim_value = row["claim_value"]
        body = str(row["body"]).split("\n[retrieval-context]", 1)[0]
        return MemorySearchItem(
            item_id=str(row["item_id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            snippet=(
                str(claim_value)
                if str(row["kind"]) == "claim" and claim_value is not None
                else body
            ),
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
