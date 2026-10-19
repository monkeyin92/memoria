#!/usr/bin/env python3
"""Retention policy for nightly database backups.

Scans ``<backup_dir>/`` for:
* ``memoria-YYYYMMDD-HHMM.dump`` (PostgreSQL backup)
* ``control-YYYYMMDD-HHMM.sqlite3`` (SQLite control store backup)

Applies the retention policy:
- Age 0-6 days: keep all daily backups
- Age 7 days or more: delete all

The newest dump and the newest control-store copy are always kept, whatever
their age. That is the only exception: it keeps one recoverable copy when
backups have been failing for a week or longer.

Returns 0 on success (or if no backups found).
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

DUMP_NAME_RE = re.compile(r"^memoria-(\d{8})-(\d{4})\.dump\Z")
SQLITE_NAME_RE = re.compile(r"^control-(\d{8})-(\d{4})\.sqlite3\Z")


def parse_backup_filename(name: str) -> tuple[str, int, int] | None:
    """Extract date from backup filename.

    Returns (filename, year_month_day, hour_minute) or None if not a backup.
    """
    for pattern in [DUMP_NAME_RE, SQLITE_NAME_RE]:
        match = pattern.match(name)
        if match:
            datestr = match.group(1)  # YYYYMMDD
            timestr = match.group(2)  # HHMM
            date_int = int(datestr)
            time_int = int(timestr)
            return (name, date_int, time_int)
    return None


def date_to_datetime(date_int: int, time_int: int) -> datetime:
    """Convert YYYYMMDD and HHMM to datetime (CST, UTC+8)."""
    datestr = str(date_int).zfill(8)
    timestr = str(time_int).zfill(4)
    year = int(datestr[0:4])
    month = int(datestr[4:6])
    day = int(datestr[6:8])
    hour = int(timestr[0:2])
    minute = int(timestr[2:4])
    tz = timezone(timedelta(hours=8))  # CST
    return datetime(year, month, day, hour, minute, 0, tzinfo=tz)


def should_keep(filename: str, date_int: int, time_int: int, now: datetime) -> bool:
    """Determine if a backup should be kept based on retention policy.

    Rules:
    - Age 0-6 days: keep all
    - Age 7 days or more: delete all, Sunday included
    """
    dt = date_to_datetime(date_int, time_int)
    age = now - dt
    age_days = age.days

    return age_days <= 6


def list_backup_candidates(backup_dir: Path) -> list[tuple[Path, str, int, int]]:
    """List all backup files in backup_dir.

    Returns list of (path, filename, date_int, time_int) tuples.
    Sorted by date/time descending (newest first).
    """
    candidates: list[tuple[Path, str, int, int]] = []
    if not backup_dir.is_dir():
        return candidates

    for path in sorted(backup_dir.iterdir(), reverse=True):
        if not path.is_file():
            continue
        parsed = parse_backup_filename(path.name)
        if parsed:
            filename, date_int, time_int = parsed
            candidates.append((path, filename, date_int, time_int))

    return candidates


def backups_to_delete(backup_dir: Path, now: datetime | None = None) -> list[Path]:
    """Return list of backups that should be deleted per retention policy.

    If now is None, uses current time in CST.
    """
    if now is None:
        now = datetime.now(tz=timezone(timedelta(hours=8)))

    candidates = list_backup_candidates(backup_dir)
    newest = _newest_per_kind(candidates)
    to_delete: list[Path] = []

    for path, filename, date_int, time_int in candidates:
        if path in newest:
            continue
        if not should_keep(filename, date_int, time_int, now):
            to_delete.append(path)

    return sorted(to_delete)


def _newest_per_kind(candidates: Sequence[tuple[Path, str, int, int]]) -> set[Path]:
    """The newest dump and the newest control-store copy are never deleted.

    Without this, backups that fail for 30 days would let the next cleanup
    remove the last copies there are.
    """
    newest: dict[str, Path] = {}
    for path, filename, _date, _time in candidates:
        kind = "dump" if DUMP_NAME_RE.match(filename) else "sqlite"
        newest.setdefault(kind, path)
    return set(newest.values())


def delete_paths(paths: Sequence[Path]) -> list[Path]:
    """Delete files. Return list of successfully deleted paths."""
    deleted: list[Path] = []
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            # Already gone (race condition).
            continue
        deleted.append(path)
    return deleted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("/var/lib/memoria/backups/nightly"),
        help="Directory containing nightly backups",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete old backups; without this flag only list them",
    )
    args = parser.parse_args(argv)

    backup_dir = args.backup_dir.resolve()
    if not backup_dir.is_dir():
        print(f"no backup directory: {backup_dir}")
        return 0

    to_delete = backups_to_delete(backup_dir)
    for path in to_delete:
        print(path)

    if not args.apply:
        print(f"to_delete={len(to_delete)} dry-run")
        return 0

    deleted = delete_paths(to_delete)
    print(f"to_delete={len(to_delete)} deleted={len(deleted)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
