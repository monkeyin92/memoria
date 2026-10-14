#!/usr/bin/env python3
"""Expire release freeze dumps under Memoria release trees only.

Scans ``<root>/releases/*/.cutover/`` for:

* ``memoria-pre-*.dump`` (pg_dump from ``release_ops.sh`` freeze)
* ``memoria.pre-*.sqlite3`` (control-store snapshot from freeze)

Files whose mtime is older than the retention window (default 3 days) are
selected. The newest dump stays available for rollback while it is still
inside that window. Paths outside ``<root>/releases`` are never touched.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

DEFAULT_RETENTION_DAYS = 3
DUMP_NAME_RE = re.compile(r"^memoria-pre-.+\.dump\Z")
SQLITE_NAME_RE = re.compile(r"^memoria\.pre-.+\.sqlite3\Z")


def is_freeze_dump_name(name: str) -> bool:
    return DUMP_NAME_RE.match(name) is not None or SQLITE_NAME_RE.match(name) is not None


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def list_freeze_dump_candidates(memoria_root: Path) -> list[Path]:
    """Return freeze dump paths under ``memoria_root/releases/*/ .cutover/``."""

    releases = (memoria_root / "releases").resolve()
    if not releases.is_dir():
        return []
    found: list[Path] = []
    for cutover in sorted(releases.glob("*/.cutover")):
        if not cutover.is_dir():
            continue
        if not _is_under(cutover, releases):
            continue
        for path in sorted(cutover.iterdir()):
            if not path.is_file():
                continue
            if not is_freeze_dump_name(path.name):
                continue
            if not _is_under(path, releases):
                continue
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
    for path in paths:
        path.unlink()
        removed.append(path)
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
    if not releases.is_dir():
        print(f"no releases directory under {root}", file=sys.stderr)
        return 2

    candidates = list_freeze_dump_candidates(root)
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
