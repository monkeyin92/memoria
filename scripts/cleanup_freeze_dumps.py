#!/usr/bin/env python3
"""Expire release freeze dumps and migration backups under Memoria trees.

Scans ``<root>/releases/*/.cutover/`` and ``<root>/releases/*/.control-store-*/`` for:

* ``memoria-pre-*.dump`` (pg_dump from ``release_ops.sh`` freeze)
* ``memoria.pre-*.sqlite3`` (control-store snapshot from freeze)
* ``memoria-pre-control-store.dump`` and ``memoria.sqlite3*``

Scans ``<migrations_root>/`` (default ``/var/backups/memoria/migrations``) for:

* Pre-migration snapshots and dumps (``*.dump``, ``*.sqlite3*``, ``*.sql``, ``*.tar.zst``, ``*.tar.gz``, ``*.bak``)

Files whose mtime is older than the retention window (default 3 days) are
selected. The newest dump stays available for rollback while it is still
inside that window. Paths outside approved roots are never touched.
"""

from __future__ import annotations

import argparse
import re
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

DEFAULT_RETENTION_DAYS = 3
DUMP_NAME_RE = re.compile(r"^memoria-pre-.+\.dump\Z")
SQLITE_NAME_RE = re.compile(r"^memoria\.pre-.+\.sqlite3\Z")
CONTROL_STORE_RE = re.compile(r"^(memoria-pre-control-store\.dump|memoria\.sqlite3(-wal|-shm)?)\Z")
MIGRATION_BACKUP_RE = re.compile(
    r"^.+\.(dump|sqlite3|sqlite3-wal|sqlite3-shm|sql|tar\.zst|tar\.gz|bak)\Z"
)


def is_freeze_dump_name(name: str) -> bool:
    return DUMP_NAME_RE.match(name) is not None or SQLITE_NAME_RE.match(name) is not None


def is_control_store_dump_name(name: str) -> bool:
    return CONTROL_STORE_RE.match(name) is not None


def is_migration_backup_name(name: str) -> bool:
    return MIGRATION_BACKUP_RE.match(name) is not None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _safe_is_dir(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        return path.resolve().is_dir()
    except (PermissionError, OSError):
        return False


def list_freeze_dump_candidates(memoria_root: Path) -> list[Path]:
    """Return freeze and control-store dump paths under releases.

    Candidates are deduped by resolved real path so a ``current`` symlink (or
    any other duplicate link under ``releases/``) does not list the same file
    twice.
    """

    releases = (memoria_root / "releases").resolve()
    if not releases.is_dir():
        return []
    found: list[Path] = []
    seen_real: set[Path] = set()

    # Freeze dumps in .cutover
    for cutover in sorted(releases.glob("*/.cutover")):
        if not cutover.is_dir() or not _is_under(cutover, releases):
            continue
        for path in sorted(cutover.iterdir()):
            if not path.is_file() or not is_freeze_dump_name(path.name):
                continue
            if not _is_under(path, releases):
                continue
            real = path.resolve()
            if real in seen_real:
                continue
            seen_real.add(real)
            found.append(path)

    # Control store migration dumps in .control-store-*
    for cstore in sorted(releases.glob("*/.control-store-*")):
        if not cstore.is_dir() or not _is_under(cstore, releases):
            continue
        for path in sorted(cstore.iterdir()):
            if not path.is_file() or not is_control_store_dump_name(path.name):
                continue
            if not _is_under(path, releases):
                continue
            real = path.resolve()
            if real in seen_real:
                continue
            seen_real.add(real)
            found.append(path)

    return found


def list_migration_backup_candidates(migrations_root: Path) -> list[Path]:
    """Return migration backup paths under ``migrations_root``."""

    try:
        resolved_root = migrations_root.resolve()
        if not resolved_root.is_dir():
            return []
    except (PermissionError, OSError):
        return []

    found: list[Path] = []
    seen_real: set[Path] = set()
    try:
        paths = sorted(resolved_root.rglob("*"))
    except (PermissionError, OSError):
        return []

    for path in paths:
        try:
            if not path.is_file():
                continue
        except (PermissionError, OSError):
            continue
        if not is_migration_backup_name(path.name):
            continue
        if not _is_under(path, resolved_root):
            continue
        try:
            real = path.resolve()
        except (PermissionError, OSError):
            continue
        if real in seen_real:
            continue
        seen_real.add(real)
        found.append(path)
    return found


def expired_freeze_dumps(
    candidates: Sequence[Path],
    *,
    now: float | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    mtimes: dict[Path, float] | None = None,
) -> list[Path]:
    """Return candidates whose age is strictly greater than ``retention_days``."""

    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    clock = time.time() if now is None else now
    cutoff = clock - retention_days * 86400
    expired: list[Path] = []
    for path in candidates:
        mtime = mtimes[path] if mtimes is not None else path.stat().st_mtime
        if mtime < cutoff:
            expired.append(path)
    return sorted(expired)


def delete_paths(paths: Iterable[Path]) -> list[Path]:
    removed: list[Path] = []
    parent_dirs: set[Path] = set()
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            # Already gone (race, or a second name for the same inode).
            continue
        removed.append(path)
        parent_dirs.add(path.parent)
    for parent in sorted(parent_dirs, key=lambda p: len(str(p)), reverse=True):
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
    return removed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/opt/memoria"),
        help="Memoria install root (must contain releases/)",
    )
    parser.add_argument(
        "--migrations-root",
        type=Path,
        default=Path("/var/backups/memoria/migrations"),
        help="Migration backups directory (default /var/backups/memoria/migrations)",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"delete dumps older than this many days (default {DEFAULT_RETENTION_DAYS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delete expired dumps; without this flag only list them",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    releases = root / "releases"
    candidates: list[Path] = []
    if _safe_is_dir(releases):
        candidates.extend(list_freeze_dump_candidates(root))
    else:
        print(f"no releases directory under {root}; nothing to clean")

    migrations_root = args.migrations_root
    if _safe_is_dir(migrations_root):
        candidates.extend(list_migration_backup_candidates(migrations_root))

    if not candidates and not _safe_is_dir(releases) and not _safe_is_dir(migrations_root):
        return 0

    expired = expired_freeze_dumps(candidates, retention_days=args.retention_days)
    for path in expired:
        print(path)
    if not args.apply:
        print(f"expired={len(expired)} candidates={len(candidates)} dry-run")
        return 0
    removed = delete_paths(expired)
    print(f"expired={len(expired)} removed={len(removed)} candidates={len(candidates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
