"""Read path for the subject-keyed Persona projection (P2-03).

The projection tables are written by the operator-invoked migration
(``services.persona.migrations.account_projection``); this module is the
product side of the same rows.  The read lives here on purpose: the Control API
must never import a migration seam (application startup must never reach
``plan``/``apply``), while a session whose current subject is not the account
owner still has to read that subject's persona.

Both reads fail closed.  A missing database, missing projection tables, a
revoked consent or no active version answer "no subject persona" -- never the
account-keyed learner's traits, which belong to a different person.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

#: Source (account-keyed) table -> subject-keyed table.  The migration owns the
#: writes and imports this map, so the two sides cannot drift apart.
PROJECTED_TABLES: Final[Mapping[str, str]] = {
    "persona_traits": "persona_subject_traits",
    "persona_evidence": "persona_subject_evidence",
    "persona_observation_receipts": "persona_subject_observation_receipts",
    "speech_style_stats": "persona_subject_style_stats",
    "persona_learning_consents": "persona_subject_learning_consents",
    "persona_versions": "persona_subject_versions",
}

_VERSIONS: Final[str] = PROJECTED_TABLES["persona_versions"]
_CONSENTS: Final[str] = PROJECTED_TABLES["persona_learning_consents"]

SUBJECT_READ_LIMIT: Final[int] = 50

_READ_TABLES: Final[tuple[str, ...]] = (
    "persona_versions",
    "persona_traits",
    "speech_style_stats",
    "persona_learning_consents",
)
_READ_ORDER: Final[Mapping[str, str]] = {
    "persona_versions": "version_number DESC, version_id",
    "persona_traits": "updated_at DESC, trait_id",
    "speech_style_stats": "updated_at DESC, scene",
    "persona_learning_consents": "granted_at DESC",
}


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def read_subject(
    db_path: str | Path,
    subject_id: str,
    *,
    limit: int = SUBJECT_READ_LIMIT,
) -> dict[str, Any]:
    """Read one subject's projected persona rows (read-only).

    A missing database or a missing projection table answers
    ``projection_present=False`` instead of being created, so a read can never
    make an un-migrated store look migrated, and a subject that was never
    projected answers with zero rows.
    """

    subject = subject_id.strip()
    if not subject:
        raise ValueError("read_subject requires a subject_id")
    if limit < 1:
        raise ValueError("read_subject limit must be positive")
    path = Path(db_path).expanduser()
    report: dict[str, Any] = {
        "scope": "persona_subject_projection",
        "db_path": str(path),
        "subject_id": subject,
        "projection_present": False,
        "tables": {},
        "truncated": False,
    }
    if not path.exists():
        return {**report, "reason": "database_missing"}
    connection = _read_only_connection(path)
    try:
        if not _table_exists(connection, _VERSIONS):
            return {**report, "reason": "projection_missing"}
        report["projection_present"] = True
        tables: dict[str, Any] = {}
        truncated = False
        for source_table in _READ_TABLES:
            target = PROJECTED_TABLES[source_table]
            total = int(
                connection.execute(
                    f'SELECT count(*) FROM "{target}" WHERE subject_id = ?',
                    (subject,),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f'SELECT * FROM "{target}" WHERE subject_id = ? '
                f"ORDER BY {_READ_ORDER[source_table]} LIMIT ?",
                (subject, limit),
            ).fetchall()
            tables[target] = {"count": total, "rows": [dict(row) for row in rows]}
            truncated = truncated or total > len(rows)
        report["tables"] = tables
        report["truncated"] = truncated
        return report
    finally:
        connection.close()


def read_active_version(
    db_path: str | Path,
    subject_id: str,
) -> dict[str, Any] | None:
    """The active projected persona version of one subject, or ``None``.

    The product read: a session whose current subject is not the account owner
    takes its persona from here.  A missing database or table, a revoked
    consent or no active version answers ``None``, and the caller must then
    render no persona rather than fall back to the account.
    """

    subject = subject_id.strip()
    if not subject:
        raise ValueError("read_active_version requires a subject_id")
    path = Path(db_path).expanduser()
    if not path.exists():
        return None
    connection = _read_only_connection(path)
    try:
        if not _table_exists(connection, _VERSIONS) or not _table_exists(
            connection, _CONSENTS
        ):
            return None
        row = connection.execute(
            "SELECT v.version_id, v.version_number, v.snapshot_json, "
            "v.source_account_id, v.projected_at "
            f'FROM "{_VERSIONS}" AS v '
            "WHERE v.subject_id = ? AND v.status = 'active' "
            "AND EXISTS (SELECT 1 FROM "
            f'"{_CONSENTS}" AS c '
            "WHERE c.subject_id = v.subject_id AND c.revoked_at IS NULL) "
            "ORDER BY v.version_number DESC LIMIT 1",
            (subject,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    raw = row["snapshot_json"]
    if not isinstance(raw, str):
        raise ValueError("projected persona snapshot is not text")
    try:
        snapshot = json.loads(raw)
    except ValueError as exc:
        raise ValueError("projected persona snapshot is not JSON") from exc
    if not isinstance(snapshot, list):
        raise ValueError("projected persona snapshot is not a list")
    return {
        "version_id": str(row["version_id"]),
        "version_number": int(row["version_number"]),
        "snapshot": snapshot,
        "source_account_id": row["source_account_id"],
        "projected_at": row["projected_at"],
    }
