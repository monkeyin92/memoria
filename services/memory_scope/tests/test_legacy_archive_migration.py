"""End-to-end tests for the legacy Archive -> Memory Scope migration seam.

What these tests pin
--------------------
* The seam is an operator-invoked, SQLite-only migration.  The source Archive
  is opened read-only, and the target may be the same file or a separate one.
* Rows come from the *real* source schema (``LifeArchive`` + ``MemoryCatalog``)
  and migrated rows are read back through the *real* target adapter
  (``SqliteMemoryStore``), never through the seam's private helpers.
* Classification is asserted per row (``table`` + ``source_row_id``) and the
  report aggregates are checked against those rows, so an extra source row
  cannot silently change an expected count.

The public API under test::

    plan(archive_path, target_path=None, *, now=None) -> dict
    dry_run(archive_path, target_path=None, *, now=None) -> dict
    apply(archive_path, target_path=None, *, dry_run=False, now=None,
          expected_manifest_sha256=None) -> dict
    rollback(archive_path, target_path=None, *, migration_id=None,
             manifest_sha256=None, now=None) -> dict

The seam is imported lazily inside each test: it is under active development
and a partially written module must not break collection of its neighbours.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.memory_scope.domain import MemoryScope
from services.memory_scope.sqlite_store import SqliteMemoryStore

_ACCOUNT = "account-legacy"
_OTHER_ACCOUNT = "account-elsewhere"
_SUBJECT = "person-legacy-owner"
_MEMBER_SUBJECT = "person-legacy-member"
_OCCURRED = "2026-01-05T07:59:00+00:00"
_CREATED = "2026-01-05T08:00:00+00:00"
_RECEIPT = "receipt-legacy-1"
_SNAPSHOT = "snapshot-legacy-1"
_OWNER = _ACCOUNT
_ACTOR = _SUBJECT
_GRANT = "grant-legacy-1"

_SOURCE_TABLES = (
    "evidence_events",
    "consent_grants",
    "memory_claims",
    "memory_search_documents",
    "memory_search_document_sources",
    "memory_vector_documents",
    "knowledge_items",
    "life_episodes",
    "episode_evidence",
    "timeline_entries",
    "person_entities",
    "person_aliases",
    "relationships",
)
_SUPPORT_TABLES = (
    "legacy_archive_migrations",
    "legacy_archive_row_receipts",
    "legacy_archive_quarantine",
    "legacy_archive_backups",
    "legacy_archive_audit_events",
)

_AUTHORITY_DDL = """
CREATE TABLE IF NOT EXISTS legacy_archive_authority (
    authority_id TEXT PRIMARY KEY,
    table_name TEXT,
    source_row_id TEXT,
    record_id TEXT,
    source_event_id TEXT,
    account_id TEXT,
    policy_receipt_id TEXT,
    consent_snapshot_id TEXT,
    resource_owner_id TEXT,
    created_by_actor_id TEXT
);
CREATE TABLE IF NOT EXISTS legacy_archive_authority_mapping (
    mapping_id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    source_row_id TEXT NOT NULL,
    authority_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_vector_documents (
    item_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimensions INTEGER NOT NULL,
    embedding TEXT NOT NULL,
    source_event_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (item_id, embedding_model, embedding_dimensions)
);
"""


# -- seam access --------------------------------------------------------------


def _api() -> Any:
    """Import the migration seam lazily (it is under active development)."""

    from services.memory_scope.migrations import legacy_archive

    return legacy_archive


def _record_id(table: str, row_id: str) -> str:
    """The frozen record id contract of the seam."""

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:memory-scope:legacy-archive:{table}:{row_id}",
        )
    )


# -- fixtures -----------------------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _connect_legacy_orphans(path: Path) -> sqlite3.Connection:
    """Write rows a legacy database could hold but the FK now fences."""

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    return connection


def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608 - test fixture
        list(values.values()),
    )


def _build_archive(path: Path) -> None:
    """Create the real source schema; tests then insert their own rows."""

    LifeArchive.sqlite(path).initialize()
    MemoryCatalog.sqlite(path, extractor=RuleBasedMemoryExtractor()).initialize()
    with _connect(path) as connection:
        connection.executescript(_AUTHORITY_DDL)
    _checkpoint(path)


def _checkpoint(path: Path) -> None:
    """Fold the WAL back so later read-only opens are deterministic."""

    with _connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _seed_evidence(
    connection: sqlite3.Connection,
    event_id: str,
    *,
    subject: str | None = _SUBJECT,
    account: str = _ACCOUNT,
    consent_grant_id: str | None = None,
    occurred_at: str = _OCCURRED,
    speaker_class: str = "owner",
    text: str = "历史话轮原文。",
) -> None:
    payload = json.dumps({"text": text, "history_eligible": True}, ensure_ascii=False)
    _insert(
        connection,
        "evidence_events",
        {
            "event_id": event_id,
            "account_id": account,
            "session_id": None,
            "turn_id": None,
            "generation_id": None,
            "event_type": "speech.utterance_finalized",
            "schema_version": 1,
            "occurred_at": occurred_at,
            "recorded_at": occurred_at,
            "subject_id": subject,
            "speaker_identity_id": None,
            "speaker_class": speaker_class,
            "source": "test.legacy-archive-migration",
            "consent_grant_id": consent_grant_id,
            "payload_json": payload,
            "content_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "supersedes_event_id": None,
        },
    )


def _seed_consent_grant(
    connection: sqlite3.Connection,
    grant_id: str = _GRANT,
    *,
    account: str = _ACCOUNT,
    revoked_at: str | None = None,
) -> None:
    _insert(
        connection,
        "consent_grants",
        {
            "consent_grant_id": grant_id,
            "account_id": account,
            "purpose": "raw_voice",
            "policy_version": "legacy-archive-v1",
            "retention_policy": "account_lifetime",
            "granted_at": _CREATED,
            "expires_at": None,
            "revoked_at": revoked_at,
            "evidence_event_id": None,
        },
    )


def _seed_claim(
    connection: sqlite3.Connection,
    claim_id: str,
    *,
    source_event_id: str,
    account: str = _ACCOUNT,
    status: str = "confirmed",
    value: str = "历史记忆内容。",
    confidence: float = 0.8,
    created_at: str = _CREATED,
    observed_at: str | None = None,
    consent_grant_id: str | None = None,
) -> None:
    # ``stability`` is written equal to ``confidence``: the real claim table
    # carries both columns, so a differing pair is an ambiguous confidence
    # source rather than a safer row.
    _insert(
        connection,
        "memory_claims",
        {
            "claim_id": claim_id,
            "account_id": account,
            "category": "life_story",
            "domain_category": "life_story",
            "memory_kind": "semantic",
            "subject_key": "self",
            "predicate": "life_story.fact",
            "value": value,
            "confidence": confidence,
            "status": status,
            "sensitive_domain": "personal",
            "entity_ids_json": "[]",
            "extractor_version": "rule-v1",
            "source_event_id": source_event_id,
            "valid_at": created_at,
            "valid_from": None,
            "valid_to": None,
            "observed_at": observed_at if observed_at is not None else created_at,
            "stability": confidence,
            "salience": 0.5,
            "sensitivity": "personal",
            "conflict_state": "none",
            "created_at": created_at,
            "review_event_id": None,
        },
    )
    if consent_grant_id is not None:
        # The operator-provided consent link is not part of the compiled
        # schema; add it the way an operator mapping column would exist.
        columns = {
            str(row["name"]) for row in connection.execute("PRAGMA table_info(memory_claims)")
        }
        if "consent_grant_id" not in columns:
            connection.execute("ALTER TABLE memory_claims ADD COLUMN consent_grant_id TEXT")
        connection.execute(
            "UPDATE memory_claims SET consent_grant_id = ? WHERE claim_id = ?",
            (consent_grant_id, claim_id),
        )


def _seed_episode(
    connection: sqlite3.Connection,
    episode_id: str,
    *,
    source_event_id: str,
    account: str = _ACCOUNT,
    status: str = "confirmed",
) -> None:
    # One timestamp for every alias the planner may read, so a safe row is not
    # quarantined merely because two aliased columns differ in the real table.
    _insert(
        connection,
        "life_episodes",
        {
            "episode_id": episode_id,
            "account_id": account,
            "title": f"episode {episode_id}",
            "category": "life_story",
            "domain_category": "life_story",
            "memory_kind": "episodic",
            "consolidation_key": f"consolidation:{episode_id}",
            "entity_ids_json": "[]",
            "status": status,
            "event_start": _OCCURRED,
            "event_end": None,
            "observed_at": _OCCURRED,
            "stability": 0.4,
            "salience": 0.6,
            "sensitivity": "personal",
            "conflict_state": "none",
            "evidence_count": 1,
            "source_event_id": source_event_id,
        },
    )


def _seed_episode_evidence(
    connection: sqlite3.Connection,
    episode_id: str,
    source_event_id: str,
    *,
    account: str = _ACCOUNT,
) -> None:
    _insert(
        connection,
        "episode_evidence",
        {
            "episode_id": episode_id,
            "account_id": account,
            "source_event_id": source_event_id,
            "status": "confirmed",
        },
    )


def _seed_timeline(
    connection: sqlite3.Connection,
    timeline_id: str,
    *,
    episode_id: str,
    source_event_id: str,
) -> None:
    _insert(
        connection,
        "timeline_entries",
        {
            "timeline_id": timeline_id,
            "account_id": _ACCOUNT,
            "episode_id": episode_id,
            "title": f"timeline {timeline_id}",
            "category": "life_story",
            "domain_category": "life_story",
            "memory_kind": "episodic",
            "entity_ids_json": "[]",
            "status": "confirmed",
            "event_start": _OCCURRED,
            "event_end": None,
            "time_precision": "day",
            "observed_at": _OCCURRED,
            "salience": 0.6,
            "sensitivity": "personal",
            "conflict_state": "none",
            "source_event_id": source_event_id,
        },
    )


def _seed_person(
    connection: sqlite3.Connection,
    person_id: str,
    *,
    source_event_id: str,
    display_name: str = "家人",
) -> None:
    _insert(
        connection,
        "person_entities",
        {
            "person_id": person_id,
            "account_id": _ACCOUNT,
            "canonical_key": f"person:{person_id}",
            "display_name": display_name,
            "relationship_to_owner": "family",
            "status": "confirmed",
            "source_event_id": source_event_id,
            "created_at": _CREATED,
        },
    )


def _seed_alias(
    connection: sqlite3.Connection,
    person_id: str,
    alias: str,
    *,
    source_event_id: str,
) -> None:
    _insert(
        connection,
        "person_aliases",
        {
            "person_id": person_id,
            "account_id": _ACCOUNT,
            "alias": alias,
            "status": "confirmed",
            "source_event_id": source_event_id,
        },
    )


def _seed_document(
    connection: sqlite3.Connection,
    document_id: str,
    *,
    item_id: str,
    source_event_id: str,
    kind: str = "claim",
    memory_kind: str = "semantic",
    status: str = "confirmed",
) -> None:
    _insert(
        connection,
        "memory_search_documents",
        {
            "document_id": document_id,
            "account_id": _ACCOUNT,
            "item_id": item_id,
            "kind": kind,
            "memory_kind": memory_kind,
            "title": f"document {document_id}",
            "body": f"body {document_id}",
            "category": "life_story",
            "domain_category": "life_story",
            "entity_ids_json": "[]",
            "status": status,
            "source_event_id": source_event_id,
            "occurred_at": _OCCURRED,
            "valid_from": None,
            "valid_to": None,
            "observed_at": _OCCURRED,
            "stability": 0.5,
            "salience": 0.5,
            "sensitivity": "personal",
            "conflict_state": "none",
        },
    )


def _seed_document_source(
    connection: sqlite3.Connection,
    document_id: str,
    source_event_id: str,
) -> None:
    _insert(
        connection,
        "memory_search_document_sources",
        {
            "document_id": document_id,
            "account_id": _ACCOUNT,
            "source_event_id": source_event_id,
        },
    )


def _seed_vector(
    connection: sqlite3.Connection,
    item_id: str,
    *,
    source_event_id: str,
) -> None:
    # PostgreSQL declares ``memory_vector_documents.source_event_id NOT NULL``;
    # the SQLite seam has to hold the same shape.
    _insert(
        connection,
        "memory_vector_documents",
        {
            "item_id": item_id,
            "account_id": _ACCOUNT,
            "embedding_model": "test-embedding-v1",
            "embedding_dimensions": 3,
            "embedding": "[0.1, 0.2, 0.3]",
            "source_event_id": source_event_id,
            "created_at": _CREATED,
        },
    )


def _seed_authority(
    connection: sqlite3.Connection,
    authority_id: str,
    *,
    table_name: str | None = None,
    source_row_id: str | None = None,
    record_id: str | None = None,
    source_event_id: str | None = None,
    account: str | None = None,
    receipt: str | None = _RECEIPT,
    snapshot: str | None = _SNAPSHOT,
    owner: str | None = _OWNER,
    actor: str | None = _ACTOR,
) -> None:
    _insert(
        connection,
        "legacy_archive_authority",
        {
            "authority_id": authority_id,
            "table_name": table_name,
            "source_row_id": source_row_id,
            "record_id": record_id,
            "source_event_id": source_event_id,
            "account_id": account,
            "policy_receipt_id": receipt,
            "consent_snapshot_id": snapshot,
            "resource_owner_id": owner,
            "created_by_actor_id": actor,
        },
    )


def _seed_authority_mapping(
    connection: sqlite3.Connection,
    mapping_id: str,
    *,
    table_name: str,
    source_row_id: str,
    authority_id: str,
) -> None:
    _insert(
        connection,
        "legacy_archive_authority_mapping",
        {
            "mapping_id": mapping_id,
            "table_name": table_name,
            "source_row_id": source_row_id,
            "authority_id": authority_id,
        },
    )


def _seed_granted_claim(
    connection: sqlite3.Connection,
    claim_id: str = "claim-ok",
    *,
    event_id: str = "evidence-a",
) -> None:
    """One fully attributable, fully authorized claim."""

    _seed_evidence(connection, event_id)
    _seed_authority(
        connection,
        f"authority-{claim_id}",
        table_name="memory_claims",
        source_row_id=claim_id,
    )
    _seed_claim(connection, claim_id, source_event_id=event_id)


def _minimal_archive(tmp_path: Path) -> Path:
    """Schema plus exactly one migratable claim with direct authority."""

    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-a")
        _seed_claim(connection, "claim-ok", source_event_id="evidence-a")
        _seed_authority(
            connection,
            "authority-claim-ok",
            table_name="memory_claims",
            source_row_id="claim-ok",
        )
    _checkpoint(archive)
    return archive


# -- inspection helpers -------------------------------------------------------


def _plan_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = report["rows"]
    assert isinstance(rows, list)
    return rows


def _row(
    report: Mapping[str, Any],
    table: str,
    source_row_id: str,
) -> dict[str, Any]:
    matches = [
        row
        for row in _plan_rows(report)
        if row.get("table") == table and row.get("source_row_id") == source_row_id
    ]
    assert len(matches) == 1, f"expected one {table}:{source_row_id} row, got {matches!r}"
    return matches[0]


def _assert_report_counts(report: Mapping[str, Any]) -> None:
    rows = _plan_rows(report)
    migrated = [row for row in rows if row["outcome"] == "migrated"]
    quarantined = [row for row in rows if row["outcome"] == "quarantined"]
    assert set(row["outcome"] for row in rows) <= {"migrated", "quarantined"}
    assert report["row_count"] == len(rows)
    assert report["migrated"] == len(migrated)
    assert report["quarantined"] == len(quarantined)
    assert report["migrated"] + report["quarantined"] == report["row_count"]
    for row in rows:
        assert isinstance(row.get("record_id"), str) and row["record_id"]
        assert isinstance(row.get("source_snapshot_digest"), str)
        assert isinstance(row.get("details"), list)
        assert all(isinstance(detail, str) for detail in row["details"])
        if row["outcome"] == "migrated":
            assert row["details"] == []
            assert row["record"] is not None
        else:
            assert row["reason"] in row["details"]
            assert row["record"] is None


def _manifest(report: Mapping[str, Any]) -> dict[str, Any]:
    manifest = report["manifest"]
    assert isinstance(manifest, dict)
    return manifest


def _table_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with _connect_read_only(path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(row["name"]) for row in rows}


def _row_count(path: Path, table: str) -> int:
    if table not in _table_names(path):
        return 0
    with _connect_read_only(path) as connection:
        row = connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row["n"])


def _migration_row(path: Path, migration_id: str) -> dict[str, Any]:
    """One stored journal row, read straight from the support schema."""

    with _connect_read_only(path) as connection:
        row = connection.execute(
            "SELECT * FROM legacy_archive_migrations WHERE migration_id = ?",
            (migration_id,),
        ).fetchone()
    assert row is not None, f"no stored migration {migration_id}"
    return dict(row)


def _normalised_target_schema(path: Path) -> dict[str, str]:
    """Normalised DDL of the two target tables, their indexes and triggers."""

    with _connect_read_only(path) as connection:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND tbl_name IN ('memory_records', 'memory_status_events')"
        ).fetchall()
    return {f"{row['type']}:{row['name']}": " ".join(str(row["sql"]).split()) for row in rows}


def _content_digest(path: Path, tables: tuple[str, ...] = _SOURCE_TABLES) -> str:
    """Content digest of the source tables (never the file bytes)."""

    payload: dict[str, list[dict[str, Any]]] = {}
    with _connect_read_only(path) as connection:
        names = _table_names(path)
        for table in tables:
            if table not in names:
                payload[table] = []
                continue
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()  # noqa: S608
            payload[table] = [dict(row) for row in rows]
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _store(path: Path) -> SqliteMemoryStore:
    store = SqliteMemoryStore(path)
    await store.initialize()
    return store


# -- plan / dry_run -----------------------------------------------------------


def test_plan_is_read_only_stable_and_never_creates_the_target(tmp_path: Path) -> None:
    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    before = _content_digest(archive)

    first = module.plan(archive_path=archive, target_path=target)
    second = module.plan(
        archive_path=archive, target_path=target, now=datetime(2026, 3, 1, tzinfo=UTC)
    )
    dry = module.dry_run(archive_path=archive, target_path=target)

    for report in (first, second, dry):
        _assert_report_counts(report)
        manifest = _manifest(report)
        assert manifest["scope"] == "legacy_archive"
        sha = manifest["manifest_sha256"]
        assert isinstance(sha, str) and len(sha) == 64
        assert set(sha) <= set("0123456789abcdef")
    # A stable plan: no wall-clock value may enter the manifest digest.
    assert _manifest(first)["manifest_sha256"] == _manifest(second)["manifest_sha256"]
    assert _manifest(dry)["manifest_sha256"] == _manifest(first)["manifest_sha256"]
    assert _plan_rows(dry) == _plan_rows(first)

    assert not target.exists(), "plan/dry_run must not create the target database"
    assert not set(_SUPPORT_TABLES).intersection(_table_names(archive))
    assert _content_digest(archive) == before


def test_plan_requires_the_evidence_ledger(tmp_path: Path) -> None:
    module = _api()
    bare = tmp_path / "bare.sqlite3"
    with _connect(bare) as connection:
        connection.execute("CREATE TABLE memory_claims (claim_id TEXT PRIMARY KEY)")

    with pytest.raises(module.LegacyArchiveMigrationError):
        module.plan(archive_path=bare, target_path=tmp_path / "target.sqlite3")
    assert not (tmp_path / "target.sqlite3").exists()


def test_plan_classifies_rows_and_quarantines_unprovable_history(tmp_path: Path) -> None:
    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-a")
        _seed_evidence(connection, "evidence-b")
        _seed_evidence(connection, "evidence-null", subject=None)
        _seed_evidence(connection, "evidence-member", subject=_MEMBER_SUBJECT)
        _seed_evidence(connection, "evidence-grant", consent_grant_id=_GRANT)
        _seed_consent_grant(connection)

        # Safe rows.
        _seed_claim(connection, "claim-ok", source_event_id="evidence-a")
        _seed_authority(
            connection,
            "authority-claim-ok",
            table_name="memory_claims",
            source_row_id="claim-ok",
        )
        _seed_claim(connection, "claim-retracted", source_event_id="evidence-a", status="retracted")
        _seed_authority(
            connection,
            "authority-claim-retracted",
            table_name="memory_claims",
            source_row_id="claim-retracted",
        )
        _seed_claim(
            connection,
            "claim-consent",
            source_event_id="evidence-grant",
            consent_grant_id=_GRANT,
        )
        _seed_authority(
            connection,
            "authority-claim-consent",
            table_name="memory_claims",
            source_row_id="claim-consent",
        )
        _seed_episode(connection, "episode-clean", source_event_id="evidence-b")
        _seed_episode_evidence(connection, "episode-clean", "evidence-a")
        _seed_episode_evidence(connection, "episode-clean", "evidence-b")
        _seed_authority(
            connection,
            "authority-episode-clean",
            table_name="life_episodes",
            source_row_id="episode-clean",
        )
        _seed_person(connection, "person-clean", source_event_id="evidence-b")
        _seed_authority(
            connection,
            "authority-person-clean",
            table_name="person_entities",
            source_row_id="person-clean",
        )
        _seed_document(connection, "document-ok", item_id="claim-ok", source_event_id="evidence-a")
        _seed_document_source(connection, "document-ok", "evidence-a")
        _seed_document_source(connection, "document-ok", "evidence-b")
        _seed_authority(
            connection,
            "authority-document-ok",
            table_name="memory_search_documents",
            source_row_id="document-ok",
        )

        # Rows that cannot prove a subject.
        _seed_claim(connection, "claim-null-subject", source_event_id="evidence-null")
        _seed_authority(
            connection,
            "authority-claim-null",
            table_name="memory_claims",
            source_row_id="claim-null-subject",
        )
        _seed_claim(
            connection,
            "claim-account-mismatch",
            account=_OTHER_ACCOUNT,
            source_event_id="evidence-a",
        )
        _seed_authority(
            connection,
            "authority-claim-account-mismatch",
            table_name="memory_claims",
            source_row_id="claim-account-mismatch",
        )
        _seed_episode(connection, "episode-mixed", source_event_id="evidence-a")
        _seed_episode_evidence(connection, "episode-mixed", "evidence-member")
        _seed_authority(
            connection,
            "authority-episode-mixed",
            table_name="life_episodes",
            source_row_id="episode-mixed",
        )
        _seed_timeline(
            connection, "timeline-mixed", episode_id="episode-mixed", source_event_id="evidence-a"
        )
        _seed_authority(
            connection,
            "authority-timeline-mixed",
            table_name="timeline_entries",
            source_row_id="timeline-mixed",
        )
        _seed_person(connection, "person-mixed", source_event_id="evidence-b")
        _seed_alias(connection, "person-mixed", "别名", source_event_id="evidence-member")
        _seed_authority(
            connection,
            "authority-person-mixed",
            table_name="person_entities",
            source_row_id="person-mixed",
        )

        # Rows with missing, conflicting or unlinked authority.
        _seed_claim(connection, "claim-no-authority", source_event_id="evidence-a")
        _seed_claim(connection, "claim-account-only-authority", source_event_id="evidence-a")
        _seed_authority(connection, "authority-account-only", account=_ACCOUNT)
        _seed_claim(connection, "claim-authority-conflict", source_event_id="evidence-a")
        _seed_authority(
            connection,
            "authority-conflict-a",
            table_name="memory_claims",
            source_row_id="claim-authority-conflict",
            receipt="receipt-conflict-a",
        )
        _seed_authority(
            connection,
            "authority-conflict-b",
            table_name="memory_claims",
            source_row_id="claim-authority-conflict",
            receipt="receipt-conflict-b",
        )
        _seed_claim(connection, "claim-partial-authority", source_event_id="evidence-a")
        _seed_authority(
            connection,
            "authority-partial",
            table_name="memory_claims",
            source_row_id="claim-partial-authority",
            snapshot=None,
        )
        _seed_claim(
            connection,
            "claim-consent-missing-row",
            source_event_id="evidence-a",
            consent_grant_id="grant-absent",
        )
        _seed_authority(
            connection,
            "authority-claim-consent-missing-row",
            table_name="memory_claims",
            source_row_id="claim-consent-missing-row",
        )
        _seed_claim(
            connection,
            "claim-distinct-time-columns",
            source_event_id="evidence-a",
            observed_at="2026-02-01T00:00:00+00:00",
        )
        _seed_authority(
            connection,
            "authority-claim-distinct-time-columns",
            table_name="memory_claims",
            source_row_id="claim-distinct-time-columns",
        )
        _seed_claim(
            connection,
            "claim-created-at-invalid",
            source_event_id="evidence-a",
            created_at="not-a-timestamp",
            observed_at="not-a-timestamp",
        )
        _seed_authority(
            connection,
            "authority-claim-created-at-invalid",
            table_name="memory_claims",
            source_row_id="claim-created-at-invalid",
        )

    # The real schema fences ``source_event_id``/``episode_id``, but a legacy
    # database can still hold orphan rows (written before the FK existed or
    # with ``foreign_keys`` off).  The seam must refuse them instead of
    # trusting the projection, so they are written the legacy way.
    with _connect_legacy_orphans(archive) as connection:
        _seed_claim(connection, "claim-missing-event", source_event_id="evidence-absent")
        _seed_authority(
            connection,
            "authority-claim-missing-event",
            table_name="memory_claims",
            source_row_id="claim-missing-event",
        )
        _seed_timeline(
            connection,
            "timeline-missing-episode",
            episode_id="episode-absent",
            source_event_id="evidence-a",
        )
        _seed_authority(
            connection,
            "authority-timeline-missing-episode",
            table_name="timeline_entries",
            source_row_id="timeline-missing-episode",
        )

    report = module.plan(archive_path=archive, target_path=tmp_path / "target.sqlite3")
    _assert_report_counts(report)

    expected_migrated = {
        ("memory_claims", "claim-ok"),
        ("memory_claims", "claim-retracted"),
        ("memory_claims", "claim-consent"),
        # Two real columns of the claim table carry a time; a differing pair is
        # an aliasing question for the seam, not a reason to drop a safe row.
        ("memory_claims", "claim-distinct-time-columns"),
        ("life_episodes", "episode-clean"),
        ("person_entities", "person-clean"),
        ("memory_search_documents", "document-ok"),
    }
    actual_migrated = {
        (row["table"], row["source_row_id"])
        for row in _plan_rows(report)
        if row["outcome"] == "migrated"
    }
    assert actual_migrated == expected_migrated
    assert report["migrated"] == len(expected_migrated)

    ok = _row(report, "memory_claims", "claim-ok")
    assert ok["record_id"] == _record_id("memory_claims", "claim-ok")
    assert ok["subject_id"] == _SUBJECT
    assert ok["record"]["scope"] == "legacy_archive"
    assert ok["record"]["subject_id"] == _SUBJECT
    assert ok["record"]["policy_receipt_id"] == _RECEIPT
    assert ok["record"]["consent_snapshot_id"] == _SNAPSHOT
    assert ok["record"]["resource_owner_id"] == _OWNER
    assert ok["record"]["created_by_actor_id"] == _ACTOR
    assert ok["record"]["source_evidence_ids"] == ["evidence-a"]
    # The status event time is the source record's time, not "now".
    assert ok["status_event"]["created_at"] == ok["record"]["created_at"] == _CREATED

    assert _row(report, "memory_claims", "claim-retracted")["status"] == "revoked"
    assert _row(report, "memory_claims", "claim-consent")["record"]["source_evidence_ids"] == [
        "evidence-grant"
    ]
    assert _row(report, "life_episodes", "episode-clean")["source_evidence_ids"] == [
        "evidence-a",
        "evidence-b",
    ]
    assert _row(report, "memory_search_documents", "document-ok")["status"] == "confirmed"

    for table, row_id, expected_detail in (
        ("memory_claims", "claim-null-subject", "source_subject_missing"),
        ("memory_claims", "claim-missing-event", "source_event_missing:evidence-absent"),
        ("timeline_entries", "timeline-missing-episode", "timeline_episode_missing"),
        (
            "memory_claims",
            "claim-account-mismatch",
            "source_event_account_mismatch:evidence-a",
        ),
        ("life_episodes", "episode-mixed", "multiple_source_subjects"),
        ("timeline_entries", "timeline-mixed", "multiple_source_subjects"),
        ("person_entities", "person-mixed", "multiple_source_subjects"),
        (
            "memory_claims",
            "claim-authority-conflict",
            "authority_conflict:policy_receipt_id:receipt-conflict-a,receipt-conflict-b",
        ),
        ("memory_claims", "claim-partial-authority", "authority_missing:consent_snapshot_id"),
        (
            "memory_claims",
            "claim-consent-missing-row",
            "consent_row_missing:grant-absent",
        ),
        ("memory_claims", "claim-created-at-invalid", "invalid_created_at"),
    ):
        row = _row(report, table, row_id)
        assert row["outcome"] == "quarantined", (table, row_id, row["details"])
        # Reason codes may carry a suffix (event id, column value); the prefix
        # is the stable contract.
        assert any(expected_detail in detail for detail in row["details"]), (
            table,
            row_id,
            row["details"],
        )
        assert row["subject_id"] is None
        assert row["record_id"] == _record_id(table, row_id)

    # No authority at all, and an account-only authority row, are both refused:
    # the account is never used as a fallback authority or subject.
    for row_id in ("claim-no-authority", "claim-account-only-authority"):
        row = _row(report, "memory_claims", row_id)
        assert row["outcome"] == "quarantined"
        assert row["authority"] == {}
        for field in (
            "policy_receipt_id",
            "consent_snapshot_id",
            "resource_owner_id",
            "created_by_actor_id",
        ):
            assert any(
                detail.startswith(f"authority_missing:{field}") for detail in row["details"]
            ), (row_id, field, row["details"])


@pytest.mark.parametrize(
    "scope_style",
    ("table_and_row", "record_id", "source_event_id"),
)
def test_authority_can_be_scoped_by_table_row_record_or_source_event(
    tmp_path: Path,
    scope_style: str,
) -> None:
    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-a")
        _seed_claim(connection, "claim-ok", source_event_id="evidence-a")
        scoped: dict[str, str] = {"table_name": "memory_claims", "source_row_id": "claim-ok"}
        if scope_style == "record_id":
            scoped = {"record_id": _record_id("memory_claims", "claim-ok")}
        elif scope_style == "source_event_id":
            scoped = {"source_event_id": "evidence-a"}
        _seed_authority(connection, f"authority-{scope_style}", **scoped)
    _checkpoint(archive)

    report = module.plan(archive_path=archive, target_path=tmp_path / "target.sqlite3")
    row = _row(report, "memory_claims", "claim-ok")
    assert row["outcome"] == "migrated", row["details"]
    assert row["authority"] == {
        "policy_receipt_id": _RECEIPT,
        "consent_snapshot_id": _SNAPSHOT,
        "resource_owner_id": _OWNER,
        "created_by_actor_id": _ACTOR,
    }


def test_authority_through_the_mapping_table(tmp_path: Path) -> None:
    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-a")
        _seed_claim(connection, "claim-ok", source_event_id="evidence-a")
        _seed_authority(
            connection,
            "authority-referenced",
            table_name=None,
            source_row_id=None,
            record_id=None,
            source_event_id=None,
        )
        _seed_authority_mapping(
            connection,
            "mapping-claim-ok",
            table_name="memory_claims",
            source_row_id="claim-ok",
            authority_id="authority-referenced",
        )
    _checkpoint(archive)

    report = module.plan(archive_path=archive, target_path=tmp_path / "target.sqlite3")
    row = _row(report, "memory_claims", "claim-ok")
    assert row["outcome"] == "migrated", row["details"]
    assert row["record"]["policy_receipt_id"] == _RECEIPT
    assert row["authority_rows"], "the referenced authority row must be reported"


def test_authority_columns_on_the_projection_or_the_evidence_row(tmp_path: Path) -> None:
    module = _api()

    for carrier in ("projection", "evidence"):
        root = tmp_path / carrier
        root.mkdir()
        archive = root / "archive.sqlite3"
        _build_archive(archive)
        with _connect(archive) as connection:
            _seed_evidence(connection, "evidence-a")
            _seed_claim(connection, "claim-ok", source_event_id="evidence-a")
            table = "memory_claims" if carrier == "projection" else "evidence_events"
            connection.execute(f"ALTER TABLE {table} ADD COLUMN policy_receipt_id TEXT")
            connection.execute(f"ALTER TABLE {table} ADD COLUMN consent_snapshot_id TEXT")
            connection.execute(f"ALTER TABLE {table} ADD COLUMN resource_owner_id TEXT")
            connection.execute(f"ALTER TABLE {table} ADD COLUMN created_by_actor_id TEXT")
            key = "claim_id" if carrier == "projection" else "event_id"
            value = "claim-ok" if carrier == "projection" else "evidence-a"
            connection.execute(
                f"UPDATE {table} SET policy_receipt_id = ?, consent_snapshot_id = ?,"
                " resource_owner_id = ?, created_by_actor_id = ? WHERE "
                f"{key} = ?",
                (_RECEIPT, _SNAPSHOT, _OWNER, _ACTOR, value),
            )
        _checkpoint(archive)

        report = module.plan(archive_path=archive, target_path=root / "target.sqlite3")
        row = _row(report, "memory_claims", "claim-ok")
        assert row["outcome"] == "migrated", (carrier, row["details"])
        assert row["record"]["resource_owner_id"] == _OWNER


def test_member_subject_keeps_the_account_as_resource_owner(tmp_path: Path) -> None:
    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-member", subject=_MEMBER_SUBJECT)
        _seed_claim(connection, "claim-member", source_event_id="evidence-member")
        _seed_authority(
            connection,
            "authority-claim-member",
            table_name="memory_claims",
            source_row_id="claim-member",
        )
    _checkpoint(archive)

    report = module.plan(archive_path=archive, target_path=tmp_path / "target.sqlite3")
    row = _row(report, "memory_claims", "claim-member")
    assert row["outcome"] == "migrated", row["details"]
    # The subject comes from the evidence row; the account is never the subject.
    assert row["subject_id"] == _MEMBER_SUBJECT
    assert row["record"]["subject_id"] == _MEMBER_SUBJECT
    assert row["record"]["resource_owner_id"] == _ACCOUNT


def test_vector_row_resolves_lineage_through_its_search_document(tmp_path: Path) -> None:
    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-a")
        _seed_evidence(connection, "evidence-b")
        _seed_document(connection, "document-ok", item_id="claim-ok", source_event_id="evidence-a")
        _seed_document_source(connection, "document-ok", "evidence-a")
        _seed_document_source(connection, "document-ok", "evidence-b")
        # The vector row carries its own source event (the PG column is NOT
        # NULL) and still resolves the document's wider source set.
        _seed_vector(connection, "claim-ok", source_event_id="evidence-a")
        _seed_authority(
            connection,
            "authority-vector",
            table_name="memory_vector_documents",
            source_row_id=json.dumps(
                [
                    {"column": "item_id", "value": "claim-ok"},
                    {"column": "embedding_model", "value": "test-embedding-v1"},
                    {"column": "embedding_dimensions", "value": 3},
                ],
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    _checkpoint(archive)

    report = module.plan(archive_path=archive, target_path=tmp_path / "target.sqlite3")
    vector_rows = [row for row in _plan_rows(report) if row["table"] == "memory_vector_documents"]
    assert len(vector_rows) == 1
    assert vector_rows[0]["outcome"] == "migrated", vector_rows[0]["details"]
    assert vector_rows[0]["source_evidence_ids"] == ["evidence-a", "evidence-b"]
    assert vector_rows[0]["subject_id"] == _SUBJECT


# -- apply --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_writes_records_readable_through_the_real_store(tmp_path: Path) -> None:
    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    source_before = _content_digest(archive)

    plan = module.plan(archive_path=archive, target_path=target)
    report = module.apply(
        archive_path=archive,
        target_path=target,
        expected_manifest_sha256=_manifest(plan)["manifest_sha256"],
    )

    assert isinstance(report["migration_id"], str) and report["migration_id"]
    records = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
    )
    assert [record.record_id for record in records] == [_record_id("memory_claims", "claim-ok")]
    record = records[0]
    assert record.scope is MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE
    assert record.subject_id == _SUBJECT
    assert record.resource_owner_id == _OWNER
    assert record.policy_receipt_id == _RECEIPT
    assert record.consent_snapshot_id == _SNAPSHOT
    assert record.created_by_actor_id == _ACTOR
    assert list(record.source_evidence_ids) == ["evidence-a"]
    assert record.retention == "indefinite"
    assert record.created_at.isoformat() == _CREATED
    assert json.loads(json.dumps(record.payload, default=str))["value"] == "历史记忆内容。"

    events = await store.get_status_events(record.record_id, actor_subject_id=_SUBJECT)
    assert [(event.status, event.reason_code) for event in events] == [
        ("confirmed", "legacy_archive_migration")
    ]
    assert events[0].created_at.isoformat() == _CREATED

    # Support tables live in the target only, and the source is untouched.
    for table in _SUPPORT_TABLES:
        assert table in _table_names(target), table
    assert not set(_SUPPORT_TABLES).intersection(_table_names(archive))
    assert _content_digest(archive) == source_before
    assert _row_count(target, "legacy_archive_migrations") == 1
    assert _row_count(target, "legacy_archive_row_receipts") == report.get("migrated", 1)
    assert _row_count(target, "legacy_archive_quarantine") == report.get("quarantined", 0)
    await store.close()


@pytest.mark.asyncio
async def test_reapplying_an_applied_migration_is_idempotent(tmp_path: Path) -> None:
    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)

    first = module.apply(archive_path=archive, target_path=target)
    records_after_first = _row_count(target, "memory_records")
    receipts_after_first = _row_count(target, "legacy_archive_row_receipts")

    second = module.apply(archive_path=archive, target_path=target)

    assert second["migration_id"] == first["migration_id"]
    assert _row_count(target, "memory_records") == records_after_first
    assert _row_count(target, "memory_status_events") == 1
    assert _row_count(target, "legacy_archive_row_receipts") == receipts_after_first
    assert _row_count(target, "legacy_archive_migrations") == 1
    records = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
    )
    assert len(records) == 1
    await store.close()


@pytest.mark.asyncio
async def test_apply_refuses_source_target_and_status_drift(tmp_path: Path) -> None:
    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)

    # A plan that does not match the requested manifest digest is refused.
    with pytest.raises(module.LegacyArchiveMigrationError):
        module.apply(
            archive_path=archive,
            target_path=target,
            expected_manifest_sha256="0" * 64,
        )
    assert _row_count(target, "legacy_archive_migrations") == 0
    assert _row_count(target, "memory_records") == 0

    plan = module.plan(archive_path=archive, target_path=target)
    manifest_sha = _manifest(plan)["manifest_sha256"]
    applied = module.apply(
        archive_path=archive,
        target_path=target,
        expected_manifest_sha256=manifest_sha,
    )

    # Source drift after the plan is refused without writing anything.
    with _connect(archive) as connection:
        connection.execute(
            "UPDATE memory_claims SET value = ? WHERE claim_id = ?",
            ("plan 之后被改写的内容。", "claim-ok"),
        )
    _checkpoint(archive)
    with pytest.raises(module.LegacyArchiveMigrationError):
        module.apply(
            archive_path=archive,
            target_path=target,
            expected_manifest_sha256=manifest_sha,
        )
    assert _row_count(target, "memory_records") == 1

    # Target drift (a manually appended status event) is refused as well.
    with _connect(target) as connection:
        connection.execute(
            "INSERT INTO memory_status_events"
            " (event_id, record_id, status, reason_code, created_at)"
            " VALUES (?, ?, 'revoked', 'manual_drift', ?)",
            (
                "status-event-drift",
                _record_id("memory_claims", "claim-ok"),
                "2026-03-01T00:00:00+00:00",
            ),
        )
    with pytest.raises(module.LegacyArchiveMigrationError):
        module.apply(archive_path=archive, target_path=target)
    with pytest.raises(module.LegacyArchiveMigrationError):
        module.rollback(
            archive_path=archive,
            target_path=target,
            migration_id=applied["migration_id"],
        )
    assert _row_count(target, "memory_records") == 1
    await store.close()


@pytest.mark.asyncio
async def test_existing_record_without_a_receipt_raises(tmp_path: Path) -> None:
    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    record_id = _record_id("memory_claims", "claim-ok")
    with _connect(target) as connection:
        _insert(
            connection,
            "memory_records",
            {
                "record_id": record_id,
                "scope": "legacy_archive",
                "subject_id": _SUBJECT,
                "resource_owner_id": _OWNER,
                "family_space_id": None,
                "co_subject_ids": "[]",
                "source_evidence_ids": '["evidence-a"]',
                "policy_receipt_id": _RECEIPT,
                "promotion_receipt_id": "",
                "promotion_fence_context_hash": "",
                "approval_evidence_refs": "[]",
                "consent_snapshot_id": _SNAPSHOT,
                "memory_type": "semantic",
                "confidence": 0.8,
                "retention": "indefinite",
                "retention_expires_at": None,
                "payload": "{}",
                "created_by_actor_id": _ACTOR,
                "created_at": _CREATED,
                "shared_proposal_id": None,
            },
        )

    with pytest.raises(module.LegacyArchiveMigrationError):
        module.apply(archive_path=archive, target_path=target)

    assert _row_count(target, "memory_records") == 1
    assert _row_count(target, "memory_status_events") == 0
    assert _row_count(target, "legacy_archive_migrations") == 0
    await store.close()


@pytest.mark.asyncio
async def test_rollback_appends_revoked_status_without_deleting_records(tmp_path: Path) -> None:
    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    applied = module.apply(archive_path=archive, target_path=target)
    record_id = _record_id("memory_claims", "claim-ok")
    audit_before = _row_count(target, "legacy_archive_audit_events")

    report = module.rollback(
        archive_path=archive,
        target_path=target,
        migration_id=applied["migration_id"],
    )

    assert isinstance(report, dict)
    assert _row_count(target, "memory_records") == 1
    events = await store.get_status_events(record_id, actor_subject_id=_SUBJECT)
    rollbacks = [event for event in events if event.reason_code == "legacy_archive_rollback"]
    assert [event.status for event in rollbacks] == ["revoked"]
    assert events[-1].status == "revoked"
    assert (
        await store.list_records_for_subject(
            _SUBJECT,
            (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
            actor_subject_id=_SUBJECT,
        )
        == ()
    )
    with_revoked = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
        include_revoked=True,
    )
    assert [record.record_id for record in with_revoked] == [record_id]
    assert _row_count(target, "legacy_archive_audit_events") > audit_before
    await store.close()


@pytest.mark.asyncio
async def test_fault_injection_rolls_back_the_whole_apply(tmp_path: Path) -> None:
    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-a")
        for claim_id in ("claim-ok", "claim-second"):
            _seed_claim(connection, claim_id, source_event_id="evidence-a")
            _seed_authority(
                connection,
                f"authority-{claim_id}",
                table_name="memory_claims",
                source_row_id=claim_id,
            )
    _checkpoint(archive)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    failing = _record_id("memory_claims", "claim-second")
    with _connect(target) as connection:
        connection.execute(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON memory_records"
            f" WHEN NEW.record_id = '{failing}'"
            " BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
        )

    with pytest.raises((module.LegacyArchiveMigrationError, sqlite3.Error)):
        module.apply(archive_path=archive, target_path=target)

    assert _row_count(target, "memory_records") == 0
    assert _row_count(target, "memory_status_events") == 0
    assert _row_count(target, "legacy_archive_row_receipts") == 0
    assert _row_count(target, "legacy_archive_migrations") == 0
    assert _row_count(target, "legacy_archive_quarantine") == 0

    # Removing the injected fault leaves a retry that migrates both rows.
    with _connect(target) as connection:
        connection.execute("DROP TRIGGER injected_failure")
    retry = module.apply(archive_path=archive, target_path=target)
    assert _row_count(target, "memory_records") == 2
    assert retry["migration_id"]
    await store.close()


@pytest.mark.asyncio
async def test_same_database_archive_and_target(tmp_path: Path) -> None:
    """``target_path=None`` means the archive file itself is the target."""

    module = _api()
    archive = _minimal_archive(tmp_path)
    store = await _store(archive)

    plan = module.plan(archive_path=archive)
    assert _manifest(plan)["scope"] == "legacy_archive"
    report = module.apply(
        archive_path=archive,
        expected_manifest_sha256=_manifest(plan)["manifest_sha256"],
    )

    assert report["migration_id"]
    assert _row_count(archive, "memory_records") == 1
    assert _row_count(archive, "legacy_archive_migrations") == 1
    records = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
    )
    assert [record.record_id for record in records] == [_record_id("memory_claims", "claim-ok")]
    # The source projections themselves stay untouched even in one file.
    with _connect_read_only(archive) as connection:
        row = connection.execute(
            "SELECT value FROM memory_claims WHERE claim_id = 'claim-ok'"
        ).fetchone()
    assert row is not None and row["value"] == "历史记忆内容。"
    await store.close()


def test_apply_dry_run_flag_writes_nothing(tmp_path: Path) -> None:
    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    before = _content_digest(archive)

    report = module.apply(archive_path=archive, target_path=target, dry_run=True)

    _assert_report_counts(report)
    assert not target.exists()
    assert _content_digest(archive) == before


# -- real compile path --------------------------------------------------------


@pytest.mark.asyncio
async def test_real_life_archive_and_catalog_rows_migrate_end_to_end(
    tmp_path: Path,
) -> None:
    """Rows produced by the real compiler migrate through the real store."""

    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    life = LifeArchive.sqlite(archive)
    catalog = MemoryCatalog.sqlite(archive, extractor=RuleBasedMemoryExtractor())
    event_id = "compiled-evidence-1"
    await life.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=_ACCOUNT,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 1, 5, 7, 59, tzinfo=UTC),
            speaker_class="owner",
            source="test.legacy-archive-migration",
            subject_id=_SUBJECT,
            payload={
                "text": "我们家的家训是答应别人的事一定做到。",
                "interaction_mode": "companion",
                "simulated_output": False,
                "history_eligible": True,
                "owner_projection_eligible": True,
            },
        )
    )
    _checkpoint(archive)
    await catalog.compile_pending()

    target = tmp_path / "target.sqlite3"
    with _connect(archive) as connection:
        claims = connection.execute(
            "SELECT claim_id, account_id, source_event_id FROM memory_claims"
        ).fetchall()
        documents = connection.execute("SELECT document_id FROM memory_search_documents").fetchall()
        assert claims, "the rule-based compiler must produce at least one claim"
        assert all(row["account_id"] == _ACCOUNT for row in claims)
        assert all(row["source_event_id"] == event_id for row in claims)
        for claim in claims:
            _seed_authority(
                connection,
                f"authority-{claim['claim_id']}",
                table_name="memory_claims",
                source_row_id=str(claim["claim_id"]),
            )
        unauthorized_documents = [str(row["document_id"]) for row in documents]
    _checkpoint(archive)

    store = await _store(target)
    plan = module.plan(archive_path=archive, target_path=target)
    migrated = {
        (row["table"], row["source_row_id"])
        for row in _plan_rows(plan)
        if row["outcome"] == "migrated"
    }
    expected = {("memory_claims", str(claim["claim_id"])) for claim in claims}
    details = {
        (row["table"], row["source_row_id"]): row["details"]
        for row in _plan_rows(plan)
        if (row["table"], row["source_row_id"]) in expected
    }
    assert expected <= migrated, (
        "compiled rows with a proven subject and an authority mapping must"
        f" migrate, got quarantined details: {details!r}"
    )
    quarantined = {
        (row["table"], row["source_row_id"])
        for row in _plan_rows(plan)
        if row["outcome"] == "quarantined"
    }
    assert {
        ("memory_search_documents", document_id) for document_id in unauthorized_documents
    } <= quarantined

    report = module.apply(
        archive_path=archive,
        target_path=target,
        expected_manifest_sha256=_manifest(plan)["manifest_sha256"],
    )
    assert report["migration_id"]

    records = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
    )
    assert {record.record_id for record in records} == {
        _record_id("memory_claims", str(claim["claim_id"])) for claim in claims
    }
    assert all(list(record.source_evidence_ids) == [event_id] for record in records)
    assert all(record.payload for record in records)
    await store.close()


@pytest.mark.asyncio
async def test_compiler_created_at_stamp_does_not_quarantine_a_safe_row(
    tmp_path: Path,
) -> None:
    """A safe compiled row must migrate on the time the user actually spoke.

    The compiler stamps ``created_at`` with its own clock while the turn time
    lives in ``observed_at``/``valid_at``.  Both are real columns of the real
    claim table, so the seam owes an explicit priority instead of treating the
    pair as a conflict and quarantining a row that has a proven subject and a
    complete authority mapping.  Which of the two columns is the record time is
    the seam's documented choice; this test only pins that the row survives.
    """

    module = _api()
    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    life = LifeArchive.sqlite(archive)
    catalog = MemoryCatalog.sqlite(archive, extractor=RuleBasedMemoryExtractor())
    event_id = "compiled-evidence-2"
    occurred_at = datetime(2026, 1, 5, 7, 59, tzinfo=UTC)
    await life.record(
        EvidenceEvent(
            event_id=event_id,
            account_id=_ACCOUNT,
            event_type="speech.utterance_finalized",
            occurred_at=occurred_at,
            speaker_class="owner",
            source="test.legacy-archive-migration",
            subject_id=_SUBJECT,
            payload={
                "text": "我们家的家训是答应别人的事一定做到。",
                "interaction_mode": "companion",
                "simulated_output": False,
                "history_eligible": True,
                "owner_projection_eligible": True,
            },
        )
    )
    await catalog.compile_pending()
    with _connect(archive) as connection:
        claims = connection.execute("SELECT claim_id FROM memory_claims").fetchall()
        assert claims, "the rule-based compiler must produce at least one claim"
        for claim in claims:
            _seed_authority(
                connection,
                f"authority-{claim['claim_id']}",
                table_name="memory_claims",
                source_row_id=str(claim["claim_id"]),
            )
    _checkpoint(archive)

    report = module.plan(archive_path=archive, target_path=tmp_path / "target.sqlite3")
    claim_rows = [row for row in _plan_rows(report) if row["table"] == "memory_claims"]
    assert claim_rows
    for row in claim_rows:
        assert row["outcome"] == "migrated", (row["source_row_id"], row["details"])
    # Deterministic: the same archive must produce the same record time on
    # every plan, whatever priority the seam applies to the two columns.
    again = module.plan(archive_path=archive, target_path=tmp_path / "target-2.sqlite3")
    for row in claim_rows:
        repeated = _row(again, "memory_claims", row["source_row_id"])
        assert repeated["record"]["created_at"] == row["record"]["created_at"]


# -- incremental runs ---------------------------------------------------------


def _seed_second_claim(archive: Path) -> None:
    """Append one more migratable claim, the way a running product would."""

    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-b", occurred_at="2026-02-01T07:59:00+00:00")
        _seed_claim(
            connection,
            "claim-new",
            source_event_id="evidence-b",
            value="第二条历史记忆内容。",
            created_at="2026-02-01T08:00:00+00:00",
        )
        _seed_authority(
            connection,
            "authority-claim-new",
            table_name="memory_claims",
            source_row_id="claim-new",
        )
    _checkpoint(archive)


@pytest.mark.asyncio
async def test_incremental_apply_writes_only_the_rows_the_target_lacks(
    tmp_path: Path,
) -> None:
    """A later run migrates new Archive rows and never rewrites the old ones.

    Appending history is the normal production path, so the seam owes an
    incremental run: the plan names the row the target already has, the apply
    writes only the new one, and the receipt of the first run - not its
    whole-target digest, which the second run makes stale - keeps the first row
    proven.
    """

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)

    first = module.apply(archive_path=archive, target_path=target)
    _seed_second_claim(archive)

    plan = module.plan(archive_path=archive, target_path=target)

    assert plan["row_count"] == 1
    assert plan["statistics"]["source_row_count"] == 2
    assert plan["already_migrated_count"] == 1
    assert plan["already_migrated"][0]["source_row_id"] == "claim-ok"
    assert plan["already_migrated"][0]["migration_id"] == first["migration_id"]
    assert [row["source_row_id"] for row in _plan_rows(plan)] == ["claim-new"]
    assert _row(plan, "memory_claims", "claim-new")["outcome"] == "migrated"

    second = module.apply(
        archive_path=archive,
        target_path=target,
        expected_manifest_sha256=_manifest(plan)["manifest_sha256"],
    )

    assert second["migration_id"] != first["migration_id"]
    assert second["idempotent"] is False
    assert second["migrated"] == 1
    assert second["already_migrated_count"] == 1
    assert [row["source_row_id"] for row in _manifest(second)["already_migrated"]] == [
        "claim-ok"
    ]
    assert _row_count(target, "memory_records") == 2
    assert _row_count(target, "memory_status_events") == 2
    assert _row_count(target, "legacy_archive_row_receipts") == 2
    assert _row_count(target, "legacy_archive_migrations") == 2
    assert (
        _migration_row(target, first["migration_id"])["target_digest_after"]
        != second["target_digest_after"]
    ), "the first run's whole-target digest is stale once a later run wrote"

    records = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
    )
    assert {record.record_id for record in records} == {
        _record_id("memory_claims", "claim-ok"),
        _record_id("memory_claims", "claim-new"),
    }
    assert all(record.payload for record in records)
    await store.close()
@pytest.mark.asyncio
async def test_incremental_apply_refuses_a_skipped_record_that_drifted(
    tmp_path: Path,
) -> None:
    """Writing new rows does not license migrating past a drifted old one."""

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    module.apply(archive_path=archive, target_path=target)
    _seed_second_claim(archive)
    with _connect(target) as connection:
        connection.execute(
            "INSERT INTO memory_status_events"
            " (event_id, record_id, status, reason_code, created_at)"
            " VALUES (?, ?, 'disputed', 'manual_after_migration', ?)",
            (
                "status-event-drift-incremental",
                _record_id("memory_claims", "claim-ok"),
                "2026-03-01T00:00:00+00:00",
            ),
        )

    with pytest.raises(module.LegacyArchiveMigrationError):
        module.apply(archive_path=archive, target_path=target)
    assert _row_count(target, "memory_records") == 1
    assert _row_count(target, "legacy_archive_row_receipts") == 1
    assert _row_count(target, "legacy_archive_migrations") == 1
    await store.close()


@pytest.mark.asyncio
async def test_a_run_with_nothing_pending_changes_no_migrated_row(
    tmp_path: Path,
) -> None:
    """Re-planning a fully migrated inventory writes no record, receipt or event.

    The inventory now spans two stored migrations, so no single one of them can
    be reported for it: the seam journals the no-op run instead of claiming one
    of them covered the whole inventory, and the second no-op is idempotent
    because that journal entry exists.
    """

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    first = module.apply(archive_path=archive, target_path=target)
    _seed_second_claim(archive)
    second = module.apply(archive_path=archive, target_path=target)

    before = (
        _row_count(target, "memory_records"),
        _row_count(target, "memory_status_events"),
        _row_count(target, "legacy_archive_row_receipts"),
    )
    no_op = module.apply(archive_path=archive, target_path=target)

    assert no_op["applied"] is True
    assert no_op["idempotent"] is False
    assert no_op["row_count"] == 0
    assert no_op["migrated"] == 0
    assert no_op["already_migrated_count"] == 2
    assert no_op["target_digest_after"] == second["target_digest_after"]
    assert (
        _row_count(target, "memory_records"),
        _row_count(target, "memory_status_events"),
        _row_count(target, "legacy_archive_row_receipts"),
    ) == before

    again = module.apply(archive_path=archive, target_path=target)

    assert again["idempotent"] is True
    assert again["migration_id"] == no_op["migration_id"]
    assert again["migration_id"] not in (first["migration_id"], second["migration_id"])
    assert (
        _row_count(target, "memory_records"),
        _row_count(target, "memory_status_events"),
        _row_count(target, "legacy_archive_row_receipts"),
    ) == before
    await store.close()


@pytest.mark.asyncio
async def test_an_unrelated_archive_change_does_not_block_the_receipts(
    tmp_path: Path,
) -> None:
    """Only receipt-covered rows are fenced; the rest of the Archive may move.

    An evidence row that no migrated projection references is exactly the kind
    of change a whole-Archive digest would have refused.  The fence is the row
    receipt, so the plan stays valid and the earlier migration can still be
    rolled back without an operator investigating a non-issue.
    """

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    first = module.apply(archive_path=archive, target_path=target)

    with _connect(archive) as connection:
        _seed_evidence(connection, "evidence-unreferenced")
    _checkpoint(archive)

    plan = module.plan(archive_path=archive, target_path=target)

    assert plan["row_count"] == 0
    assert plan["already_migrated_count"] == 1
    assert plan["source_digest"] != first["source_digest"]
    assert _row_count(target, "legacy_archive_migrations") == 1

    report = module.rollback(
        archive_path=archive,
        target_path=target,
        migration_id=first["migration_id"],
    )

    assert report["idempotent"] is False
    assert report["revoked_count"] == 1
    await store.close()


@pytest.mark.asyncio
async def test_apply_and_rollback_refuse_a_receipt_covered_row_that_changed(
    tmp_path: Path,
) -> None:
    """A source row rewritten under its receipt is investigated, not re-migrated."""

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    applied = module.apply(archive_path=archive, target_path=target)
    before = (
        _row_count(target, "memory_records"),
        _row_count(target, "memory_status_events"),
        _row_count(target, "legacy_archive_row_receipts"),
        _row_count(target, "legacy_archive_migrations"),
    )

    with _connect(archive) as connection:
        connection.execute(
            "UPDATE memory_claims SET value = ? WHERE claim_id = ?",
            ("收据之后被改写的内容。", "claim-ok"),
        )
    _checkpoint(archive)

    with pytest.raises(module.LegacyArchiveMigrationError):
        module.plan(archive_path=archive, target_path=target)
    with pytest.raises(module.LegacyArchiveMigrationError):
        module.apply(archive_path=archive, target_path=target)
    with pytest.raises(module.LegacyArchiveMigrationError):
        module.rollback(
            archive_path=archive,
            target_path=target,
            migration_id=applied["migration_id"],
        )
    with pytest.raises(module.LegacyArchiveMigrationError):
        module.rollback(
            archive_path=archive,
            target_path=target,
            manifest_sha256=_manifest(applied)["manifest_sha256"],
        )
    assert (
        _row_count(target, "memory_records"),
        _row_count(target, "memory_status_events"),
        _row_count(target, "legacy_archive_row_receipts"),
        _row_count(target, "legacy_archive_migrations"),
    ) == before
    await store.close()


@pytest.mark.asyncio
async def test_apply_after_a_rollback_is_refused(tmp_path: Path) -> None:
    """A revoked migration is not silently re-migrated."""

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    applied = module.apply(archive_path=archive, target_path=target)
    module.rollback(
        archive_path=archive,
        target_path=target,
        migration_id=applied["migration_id"],
    )

    with pytest.raises(module.LegacyArchiveMigrationError) as plan_error:
        module.plan(archive_path=archive, target_path=target)
    assert "cannot be reactivated" in str(plan_error.value)
    with pytest.raises(module.LegacyArchiveMigrationError) as apply_error:
        module.apply(archive_path=archive, target_path=target)
    assert "cannot be reactivated" in str(apply_error.value)
    assert _row_count(target, "memory_records") == 1
    assert _row_count(target, "legacy_archive_migrations") == 1
    await store.close()


@pytest.mark.asyncio
async def test_rollback_revokes_only_the_records_of_its_own_run(tmp_path: Path) -> None:
    """An earlier run can be rolled back while a later incremental run stands."""

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    first = module.apply(archive_path=archive, target_path=target)
    _seed_second_claim(archive)
    second = module.apply(archive_path=archive, target_path=target)
    first_record = _record_id("memory_claims", "claim-ok")
    second_record = _record_id("memory_claims", "claim-new")

    report = module.rollback(
        archive_path=archive,
        target_path=target,
        migration_id=first["migration_id"],
    )

    assert report["idempotent"] is False
    assert report["revoked_count"] == 1
    assert [entry["record_id"] for entry in report["revoked"]] == [first_record]
    assert _row_count(target, "memory_records") == 2
    assert _row_count(target, "memory_status_events") == 3
    confirmed = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
    )
    assert [record.record_id for record in confirmed] == [second_record]

    # The later run's own status chain is untouched, so it still rolls back.
    later = module.rollback(
        archive_path=archive,
        target_path=target,
        migration_id=second["migration_id"],
    )
    assert later["revoked_count"] == 1
    assert [entry["record_id"] for entry in later["revoked"]] == [second_record]

    # And the first run stays idempotent after the target moved again.
    again = module.rollback(
        archive_path=archive,
        target_path=target,
        migration_id=first["migration_id"],
    )
    assert again["idempotent"] is True
    assert again["status"] == "rolled_back"
    assert [entry["record_id"] for entry in again["revoked"]] == [first_record]
    with_revoked = await store.list_records_for_subject(
        _SUBJECT,
        (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
        actor_subject_id=_SUBJECT,
        include_revoked=True,
    )
    assert {record.record_id for record in with_revoked} == {first_record, second_record}
    await store.close()


@pytest.mark.asyncio
async def test_a_status_event_after_a_rollback_is_refused(tmp_path: Path) -> None:
    """The idempotent rollback path still fences the record it revoked."""

    module = _api()
    archive = _minimal_archive(tmp_path)
    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    applied = module.apply(archive_path=archive, target_path=target)
    record_id = _record_id("memory_claims", "claim-ok")
    module.rollback(
        archive_path=archive,
        target_path=target,
        migration_id=applied["migration_id"],
    )
    with _connect(target) as connection:
        connection.execute(
            "INSERT INTO memory_status_events"
            " (event_id, record_id, status, reason_code, created_at)"
            " VALUES (?, ?, 'confirmed', 'manual_after_rollback', ?)",
            ("status-event-after-rollback", record_id, "2026-03-01T00:00:00+00:00"),
        )

    with pytest.raises(module.LegacyArchiveMigrationError):
        module.rollback(
            archive_path=archive,
            target_path=target,
            migration_id=applied["migration_id"],
        )
    assert _row_count(target, "memory_records") == 1
    await store.close()


# -- target schema parity -----------------------------------------------------


@pytest.mark.asyncio
async def test_migrated_target_schema_matches_the_real_store(tmp_path: Path) -> None:
    """A target the seam creates must be the schema the real adapter creates.

    "CREATE TABLE IF NOT EXISTS" never reconciles a differing table, so a
    database first created by this seam would keep the seam's own constraints
    and lack the adapter's indexes forever.  The two target tables, their
    indexes and their append-only triggers are therefore compared, object by
    object, against a database the real adapter created.
    """

    module = _api()
    archive = _minimal_archive(tmp_path)
    adapter_target = tmp_path / "adapter.sqlite3"
    store = await _store(adapter_target)
    migrated_target = tmp_path / "migrated.sqlite3"

    module.apply(archive_path=archive, target_path=migrated_target)

    migrated_schema = _normalised_target_schema(migrated_target)
    assert migrated_schema, "the seam must create the two target tables"
    assert migrated_schema == _normalised_target_schema(adapter_target)
    assert "uq_memory_records_shared_proposal" in ",".join(migrated_schema)
    await store.close()


# -- identity seam ------------------------------------------------------------


@pytest.mark.asyncio
async def test_migrated_records_are_keyed_by_identity_persons(tmp_path: Path) -> None:
    """The Archive seam writes the subject ids the identity service owns.

    The seam never invents a subject and never falls back to the account: the
    subject of a migrated record is the subject_id of its lineage evidence,
    and the identity service is the authority for those ids.  The seam does not
    read the identity database (operator tool, SQLite only), so this pins the
    seam between the two: every migrated subject resolves to a registered
    person while the account stays the resource owner.
    """

    from services.identity.domain import PersonSubject
    from services.identity.sqlite_store import SqliteIdentityStore

    module = _api()
    identity = SqliteIdentityStore(tmp_path / "identity.sqlite3")
    identity.initialize()
    owner = PersonSubject(
        person_id="person-identity-owner", display_name="本人", status="active"
    )
    member = PersonSubject(
        person_id="person-identity-member", display_name="家人", status="active"
    )
    await identity.register_person(owner)
    await identity.register_person(member)

    archive = tmp_path / "archive.sqlite3"
    _build_archive(archive)
    with _connect(archive) as connection:
        for person_id, evidence_id in (
            (owner.person_id, "evidence-owner"),
            (member.person_id, "evidence-member"),
        ):
            _seed_evidence(connection, evidence_id, subject=person_id)
            claim_id = f"claim-{evidence_id}"
            _seed_claim(connection, claim_id, source_event_id=evidence_id)
            _seed_authority(
                connection,
                f"authority-{claim_id}",
                table_name="memory_claims",
                source_row_id=claim_id,
            )
    _checkpoint(archive)

    target = tmp_path / "target.sqlite3"
    store = await _store(target)
    module.apply(archive_path=archive, target_path=target)

    assert await identity.person_exists(owner.person_id)
    assert await identity.person_exists(member.person_id)
    assert not await identity.person_exists(_ACCOUNT)
    for person in (owner, member):
        records = await store.list_records_for_subject(
            person.person_id,
            (MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,),
            actor_subject_id=person.person_id,
        )
        assert [record.subject_id for record in records] == [person.person_id]
        assert all(record.resource_owner_id == _ACCOUNT for record in records)
        assert all(record.subject_id != _ACCOUNT for record in records)
    await store.close()
