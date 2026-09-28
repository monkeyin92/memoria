"""Versioned migrations for the PostgreSQL control store.

``postgres_schema.sql`` is the idempotent baseline (version 1). Changes it
cannot express idempotently live in ``migrations/NNNN_<name>.sql`` (NNNN >= 2):
each is applied once, in order, inside one transaction that also records it in
``control_schema_migrations``. Released files are immutable; fix forward with
a new number.

Production applies them with the admin role from ``scripts/release_ops.sh``
(schema step); tests and development apply them here. The runtime role only
verifies (``initialize_postgres``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
BASELINE_VERSION = 1
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
# Serializes concurrent appliers (two releases, or tests on one database).
_LOCK_KEY = 0x6D656D6F  # "memo"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    directory = MIGRATIONS_DIR if directory is None else directory
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")) if directory.is_dir() else ():
        match = _NAME.match(path.name)
        if match is None:
            raise RuntimeError(f"control migration file is misnamed: {path.name}")
        found.append(Migration(int(match.group(1)), path.stem, path))
    versions = [item.version for item in found]
    expected = list(range(BASELINE_VERSION + 1, BASELINE_VERSION + 1 + len(found)))
    if versions != expected:
        raise RuntimeError(f"control migrations must be numbered {expected}, found {versions}")
    return tuple(found)


def expected_version(directory: Path | None = None) -> int:
    found = migrations(directory)
    return found[-1].version if found else BASELINE_VERSION


def applied_version(connection: Any) -> int:
    row = connection.execute("SELECT max(version) FROM control_schema_migrations").fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def apply_pending(connection: Any, directory: Path | None = None) -> list[str]:
    """Apply every migration newer than the ledger; return their names.

    ``connection`` is a psycopg connection with the admin role, outside a
    transaction; the baseline must already be applied.
    """

    applied: list[str] = []
    for migration in migrations(directory):
        with connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            if migration.version <= applied_version(connection):
                continue
            # No parameters: psycopg sends the script as-is.
            connection.execute(migration.path.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO control_schema_migrations (version, name, applied_at) "
                "VALUES (%s, %s, to_char(now() AT TIME ZONE 'UTC', "
                "'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"'))",
                (migration.version, migration.name),
            )
        applied.append(migration.name)
    return applied
