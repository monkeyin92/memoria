"""Legacy adult-default remediation seam (PR-02, section 8.8).

Historical rows that carry ``subject_category = 'adult'`` without *verified*
age evidence must never be treated as verified adults.  This seam demotes
them to ``unknown`` / ``unknown`` / ``unverified`` with:

- a ``dry_run`` report (count + person ids + checksum) before anything is
  written;
- an immutable checksum of the pre-migration state that ``rollback``
  re-verifies before restoring;
- a backup table plus audit events for every demoted row.

The seam is SQLite-only and operator-invoked; it never auto-runs in
production.  Production PostgreSQL remediation must run through the
``memoria_identity_migration`` role inside ``app.identity_scope =
'migration'`` (see ``postgres_schema.sql``); this module only guards the
local/legacy SQLite databases.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

_BACKUP_TABLE = "identity_legacy_adult_backup"
_NEW_PERSONS_SCHEMA = """
CREATE TABLE identity_persons (
    person_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '朋友',
    subject_category TEXT NOT NULL DEFAULT 'unknown'
        CHECK (subject_category IN ('unknown', 'minor', 'adult')),
    age_band TEXT NOT NULL DEFAULT 'unknown'
        CHECK (age_band IN ('unknown', 'under_14', '14_17', 'adult')),
    age_evidence_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed')),
    locale TEXT NOT NULL DEFAULT 'zh-CN',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (subject_category = 'adult' AND age_band = 'adult'
            AND age_evidence_status = 'verified')
        OR (subject_category = 'minor' AND age_band IN ('under_14', '14_17'))
        OR (subject_category = 'unknown'
            AND age_band IN ('unknown', 'adult')
            AND NOT (age_band = 'adult' AND age_evidence_status = 'verified'))
    )
)
"""
_LEGACY_PERSONS_SCHEMA = """
CREATE TABLE identity_persons (
    person_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '朋友',
    subject_category TEXT NOT NULL DEFAULT 'adult'
        CHECK (subject_category IN ('adult', 'minor')),
    age_band TEXT NOT NULL DEFAULT 'unknown'
        CHECK (age_band IN ('unknown', 'under_14', '14_17', 'adult')),
    age_evidence_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed')),
    locale TEXT NOT NULL DEFAULT 'zh-CN',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""
_PERSON_COLUMNS = (
    "person_id, display_name, subject_category, age_band, "
    "age_evidence_status, locale, timezone, status, created_at, updated_at"
)


def _now(value: datetime | None) -> datetime:
    return value.astimezone(UTC) if value is not None else datetime.now(UTC)


def _rows(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = connection.execute(
        """
        SELECT person_id, subject_category, age_band, age_evidence_status,
               updated_at
        FROM identity_persons
        WHERE subject_category = 'adult'
          AND age_evidence_status <> 'verified'
        ORDER BY person_id
        """
    ).fetchall()
    return [
        {
            "person_id": str(row["person_id"]),
            "subject_category": str(row["subject_category"]),
            "age_band": str(row["age_band"]),
            "age_evidence_status": str(row["age_evidence_status"]),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


def checksum(rows: list[dict[str, str]]) -> str:
    """Stable sha256 over the canonical sorted JSON of the affected rows."""
    canonical = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _affected_state_checksum(
    connection: sqlite3.Connection, person_ids: list[str]
) -> str:
    """Checksum of the *current* state of the affected persons."""
    rows: list[dict[str, str]] = []
    for person_id in sorted(person_ids):
        row = connection.execute(
            """
            SELECT subject_category, age_band, age_evidence_status, updated_at
            FROM identity_persons WHERE person_id = ?
            """,
            (person_id,),
        ).fetchone()
        if row is not None:
            rows.append(
                {
                    "person_id": person_id,
                    "subject_category": str(row["subject_category"]),
                    "age_band": str(row["age_band"]),
                    "age_evidence_status": str(row["age_evidence_status"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
    return checksum(rows)


def dry_run(path: str | Path) -> dict[str, object]:
    """Report what would change without writing anything."""
    path = Path(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        rows = _rows(connection)
    return {
        "dry_run": True,
        "database": str(path),
        "affected_count": len(rows),
        "person_ids": [row["person_id"] for row in rows],
        "checksum_before": checksum(rows),
    }


def _ensure_support_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_BACKUP_TABLE} (
            person_id TEXT PRIMARY KEY,
            subject_category TEXT NOT NULL,
            age_band TEXT NOT NULL,
            age_evidence_status TEXT NOT NULL,
            display_name TEXT NOT NULL,
            locale TEXT NOT NULL,
            timezone TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            migrated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_audit_events (
            event_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            actor_person_id TEXT,
            subject_person_id TEXT,
            person_id TEXT,
            device_id TEXT,
            binding_id TEXT,
            relationship_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _supports_unknown_category(connection: sqlite3.Connection) -> bool:
    """Probe whether the table CHECK accepts an ``unknown`` category."""
    try:
        connection.execute(
            """
            INSERT INTO identity_persons (
                person_id, display_name, subject_category, age_band,
                age_evidence_status, locale, timezone, status,
                created_at, updated_at
            ) VALUES (
                '__identity_probe__', 'probe', 'unknown', 'unknown',
                'unverified', 'zh-CN', 'Asia/Shanghai', 'active', 't', 't'
            )
            """
        )
        connection.execute("DELETE FROM identity_persons WHERE person_id = '__identity_probe__'")
        return True
    except sqlite3.IntegrityError:
        raise


def _rebuild_persons_table(
    connection: sqlite3.Connection,
    *,
    schema: Literal["new", "legacy"],
    now_iso: str,
) -> None:
    """Rebuild identity_persons with the target schema, demoting inline when
    the legacy CHECK cannot store ``unknown`` categories."""
    temporary = "identity_persons_migration_tmp"
    connection.execute(f"ALTER TABLE identity_persons RENAME TO {temporary}")
    connection.execute(_NEW_PERSONS_SCHEMA if schema == "new" else _LEGACY_PERSONS_SCHEMA)
    if schema == "new":
        select = (
            f"SELECT person_id, display_name, "
            f"CASE WHEN subject_category = 'adult' "
            f"     AND age_evidence_status <> 'verified' "
            f"     THEN 'unknown' ELSE subject_category END, "
            f"CASE WHEN subject_category = 'adult' "
            f"     AND age_evidence_status <> 'verified' "
            f"     THEN 'unknown' ELSE age_band END, "
            f"CASE WHEN subject_category = 'adult' "
            f"     AND age_evidence_status <> 'verified' "
            f"     THEN 'unverified' ELSE age_evidence_status END, "
            f"locale, timezone, status, created_at, "
            f"CASE WHEN subject_category = 'adult' "
            f"     AND age_evidence_status <> 'verified' "
            f"     THEN '{now_iso}' ELSE updated_at END "
            f"FROM {temporary}"
        )
        connection.execute(
            f"INSERT INTO identity_persons ({_PERSON_COLUMNS}) {select}"
        )
    else:
        # Unaffected rows copy as-is; affected rows are restored from the
        # immutable backup so the legacy CHECK never sees demoted values.
        connection.execute(
            f"""
            INSERT INTO identity_persons ({_PERSON_COLUMNS})
            SELECT {_PERSON_COLUMNS} FROM {temporary}
            WHERE person_id NOT IN (
                SELECT person_id FROM {_BACKUP_TABLE}
            )
            """
        )
        for backup in connection.execute(
            f"SELECT * FROM {_BACKUP_TABLE} ORDER BY person_id"
        ).fetchall():
            original = connection.execute(
                f"SELECT created_at FROM {temporary} WHERE person_id = ?",
                (str(backup["person_id"]),),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO identity_persons (
                    person_id, display_name, subject_category, age_band,
                    age_evidence_status, locale, timezone, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(backup["person_id"]),
                    str(backup["display_name"]),
                    str(backup["subject_category"]),
                    str(backup["age_band"]),
                    str(backup["age_evidence_status"]),
                    str(backup["locale"]),
                    str(backup["timezone"]),
                    str(backup["status"]),
                    (
                        str(original["created_at"])
                        if original is not None
                        else now_iso
                    ),
                    str(backup["updated_at"]),
                ),
            )
    connection.execute(f"DROP TABLE {temporary}")


def apply(path: str | Path, *, dry_run: bool = False, now: datetime | None = None) -> dict[str, object]:
    """Demote legacy adult rows; with ``dry_run=True`` nothing is written."""
    timestamp = _now(now)
    path = Path(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = _rows(connection)
        checksum_before = checksum(rows)
        if dry_run:
            connection.rollback()
            return {
                "dry_run": True,
                "database": str(path),
                "affected_count": len(rows),
                "person_ids": [row["person_id"] for row in rows],
                "checksum_before": checksum_before,
            }
        _ensure_support_tables(connection)
        now_iso = timestamp.isoformat()
        migrated_ids: list[str] = []
        for row in rows:
            person_id = row["person_id"]
            original = connection.execute(
                """
                SELECT person_id, subject_category, age_band,
                       age_evidence_status, display_name, locale, timezone,
                       status, updated_at
                FROM identity_persons WHERE person_id = ?
                """,
                (person_id,),
            ).fetchone()
            if original is None:
                continue
            connection.execute(
                f"""
                INSERT OR REPLACE INTO {_BACKUP_TABLE} (
                    person_id, subject_category, age_band,
                    age_evidence_status, display_name, locale, timezone,
                    status, updated_at, migrated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    person_id,
                    str(original["subject_category"]),
                    str(original["age_band"]),
                    str(original["age_evidence_status"]),
                    str(original["display_name"]),
                    str(original["locale"]),
                    str(original["timezone"]),
                    str(original["status"]),
                    str(original["updated_at"]),
                    now_iso,
                ),
            )
            payload = {
                "before": {
                    "subject_category": str(original["subject_category"]),
                    "age_band": str(original["age_band"]),
                    "age_evidence_status": str(original["age_evidence_status"]),
                },
                "after": {
                    "subject_category": "unknown",
                    "age_band": "unknown",
                    "age_evidence_status": "unverified",
                },
            }
            connection.execute(
                """
                INSERT INTO identity_audit_events (
                    event_id, action, actor_person_id, subject_person_id,
                    person_id, device_id, binding_id, relationship_id,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    "migration.legacy_adult.demote",
                    None,
                    person_id,
                    person_id,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now_iso,
                ),
            )
            migrated_ids.append(person_id)
        if migrated_ids:
            try:
                _supports_unknown_category(connection)
                connection.execute(
                    """
                    UPDATE identity_persons
                    SET subject_category = 'unknown',
                        age_band = 'unknown',
                        age_evidence_status = 'unverified',
                        updated_at = ?
                    WHERE subject_category = 'adult'
                      AND age_evidence_status <> 'verified'
                    """,
                    (now_iso,),
                )
            except sqlite3.IntegrityError:
                # Legacy CHECK rejects 'unknown': rebuild the table with the
                # new constraints while demoting inline.
                _rebuild_persons_table(connection, schema="new", now_iso=now_iso)
        checksum_after = _affected_state_checksum(connection, migrated_ids)
        connection.commit()
        return {
            "dry_run": False,
            "database": str(path),
            "affected_count": len(migrated_ids),
            "person_ids": migrated_ids,
            "checksum_before": checksum_before,
            "checksum_after": checksum_after,
            "backup_table": _BACKUP_TABLE,
            "migrated_at": now_iso,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def rollback(
    path: str | Path,
    *,
    expected_checksum: str | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Restore the pre-migration state; verifies the checksum first."""
    timestamp = _now(now)
    path = Path(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN IMMEDIATE")
        present = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_BACKUP_TABLE,),
        ).fetchone()
        if present is None:
            raise RuntimeError("no migration backup table found; nothing to roll back")
        backups = connection.execute(
            f"SELECT * FROM {_BACKUP_TABLE} ORDER BY person_id"
        ).fetchall()
        affected_ids = [str(backup["person_id"]) for backup in backups]
        current_checksum = _affected_state_checksum(connection, affected_ids)
        if expected_checksum is not None:
            if current_checksum != expected_checksum:
                raise RuntimeError(
                    "current state drifted from the migration checksum; "
                    "refusing to roll back"
                )
        restored_ids: list[str] = []
        now_iso = timestamp.isoformat()
        if any(
            str(backup["subject_category"]) == "adult"
            and str(backup["age_evidence_status"]) != "verified"
            for backup in backups
        ):
            # The new CHECK cannot store the legacy adult defaults; restore
            # the legacy schema together with the original values.
            _rebuild_persons_table(connection, schema="legacy", now_iso=now_iso)
        for backup in backups:
            person_id = str(backup["person_id"])
            connection.execute(
                """
                UPDATE identity_persons
                SET subject_category = ?,
                    age_band = ?,
                    age_evidence_status = ?,
                    display_name = ?,
                    locale = ?,
                    timezone = ?,
                    status = ?,
                    updated_at = ?
                WHERE person_id = ?
                """,
                (
                    str(backup["subject_category"]),
                    str(backup["age_band"]),
                    str(backup["age_evidence_status"]),
                    str(backup["display_name"]),
                    str(backup["locale"]),
                    str(backup["timezone"]),
                    str(backup["status"]),
                    str(backup["updated_at"]),
                    person_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO identity_audit_events (
                    event_id, action, actor_person_id, subject_person_id,
                    person_id, device_id, binding_id, relationship_id,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    "migration.legacy_adult.rollback",
                    None,
                    person_id,
                    person_id,
                    json.dumps(
                        {"restored_from": _BACKUP_TABLE},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now_iso,
                ),
            )
            restored_ids.append(person_id)
        restored_checksum = _affected_state_checksum(connection, affected_ids)
        connection.execute(f"DELETE FROM {_BACKUP_TABLE}")
        connection.commit()
        return {
            "dry_run": False,
            "database": str(path),
            "restored_count": len(restored_ids),
            "person_ids": restored_ids,
            "checksum_before_restore": current_checksum,
            "checksum_after_restore": restored_checksum,
            "backup_table": _BACKUP_TABLE,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
