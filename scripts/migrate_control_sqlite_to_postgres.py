#!/usr/bin/env python3
"""Copy the Control API SQLite store into its PostgreSQL schema, then verify.

One-way, one-time, fail closed:

* The source file is opened read-only; nothing in it changes.
* Every target table must be empty, so the copy never runs twice.
* A non-empty source table without a PostgreSQL home stops the run, so no
  data is left behind silently.
* All rows go in one transaction. Afterwards each table's row count and a
  content checksum must match the source, or the transaction rolls back.
* ``messages_id_seq`` continues after the highest copied message id.

The DSN is read from ``MEMORIA_CONTROL_DATABASE_URL`` (never from argv, so it
stays out of shell history and process lists). ``--dry-run`` does everything,
including the checks, then rolls back. The receipt JSON names each table's
count and checksum and the source file's SHA-256; it contains no row data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.control_api.app.database.schema import POSTGRES_TABLES  # noqa: E402

# Leftovers of old SQLite migrations: renamed originals whose rows were copied
# into the current tables. They are reported, never copied.
_LEGACY_SUFFIXES = ("_legacy", "_legacy_subject")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return {"bytes_sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return value
    return value


def _checksum(columns: list[str], rows: list[tuple[Any, ...]]) -> str:
    encoded = sorted(
        json.dumps(
            [_canonical(value) for value in row],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for row in rows
    )
    digest = hashlib.sha256(json.dumps(columns).encode("utf-8"))
    for line in encoded:
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _sqlite_tables(source: sqlite3.Connection) -> dict[str, int]:
    names = [
        str(row[0])
        for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {name: int(source.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]) for name in names}


def _pg_columns(target: psycopg.Connection[Any], table: str) -> list[str]:
    rows = target.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def migrate(source_path: Path, dsn: str, *, dry_run: bool) -> dict[str, Any]:
    if not source_path.is_file():
        raise SystemExit(f"source SQLite file not found: {source_path}")
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        source_counts = _sqlite_tables(source)
        unmapped = {
            name: count
            for name, count in source_counts.items()
            if name not in POSTGRES_TABLES and not name.endswith(_LEGACY_SUFFIXES)
        }
        stranded = {name: count for name, count in unmapped.items() if count}
        if stranded:
            raise SystemExit(
                "refusing to migrate: non-empty SQLite tables have no PostgreSQL home: "
                + json.dumps(stranded, sort_keys=True)
            )
        report: dict[str, Any] = {
            "schema": "memoria-control-sqlite-to-postgres-v1",
            "source_sha256": _file_sha256(source_path),
            "started_at": datetime.now(UTC).isoformat(),
            "dry_run": dry_run,
            "tables": {},
            "ignored_empty_tables": sorted(unmapped),
            "ignored_legacy_tables": sorted(
                name for name in source_counts if name.endswith(_LEGACY_SUFFIXES)
            ),
        }
        with psycopg.connect(dsn) as target:
            for table in POSTGRES_TABLES:
                existing = target.execute(f'SELECT count(*) FROM "{table}"').fetchone()
                if existing is not None and int(existing[0]):
                    raise SystemExit(f"refusing to migrate: target table {table} is not empty")
            # POSTGRES_TABLES is ordered parents first, so foreign keys hold row by row.
            for table in POSTGRES_TABLES:
                if table not in source_counts:
                    report["tables"][table] = {"rows": 0, "source": "absent"}
                    continue
                target_columns = _pg_columns(target, table)
                source_columns = [
                    str(row[1]) for row in source.execute(f'PRAGMA table_info("{table}")')
                ]
                missing = sorted(set(source_columns) - set(target_columns))
                if missing:
                    raise SystemExit(f"{table}: SQLite columns missing in PostgreSQL: {missing}")
                columns = [column for column in target_columns if column in source_columns]
                selected = ", ".join(f'"{column}"' for column in columns)
                rows = [tuple(row) for row in source.execute(f'SELECT {selected} FROM "{table}"')]
                if rows:
                    placeholders = ", ".join(["%s"] * len(columns))
                    with target.cursor() as cursor:
                        cursor.executemany(
                            f'INSERT INTO "{table}" ({selected}) VALUES ({placeholders})',
                            rows,
                        )
                copied = [
                    tuple(row)
                    for row in target.execute(f'SELECT {selected} FROM "{table}"').fetchall()
                ]
                source_sum = _checksum(columns, rows)
                target_sum = _checksum(columns, copied)
                if len(copied) != len(rows) or source_sum != target_sum:
                    raise SystemExit(
                        f"{table}: verification failed "
                        f"(source rows={len(rows)}, target rows={len(copied)})"
                    )
                report["tables"][table] = {"rows": len(rows), "checksum": source_sum}
            target.execute(
                "SELECT setval('messages_id_seq', COALESCE((SELECT max(id) FROM messages), 0) + 1,"
                " false)"
            )
            if dry_run:
                target.rollback()
            else:
                target.commit()
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["total_rows"] = sum(entry["rows"] for entry in report["tables"].values())
        return report
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", required=True, type=Path, help="the control SQLite file")
    parser.add_argument("--receipt", type=Path, help="write the JSON receipt here")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="copy and verify, then roll back")
    mode.add_argument("--apply", action="store_true", help="copy, verify and commit")
    args = parser.parse_args(argv)
    dsn = os.environ.get("MEMORIA_CONTROL_DATABASE_URL", "").strip()
    if not dsn:
        parser.error("set MEMORIA_CONTROL_DATABASE_URL to the target PostgreSQL DSN")
    report = migrate(args.sqlite, dsn, dry_run=args.dry_run)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.receipt is not None:
        args.receipt.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
