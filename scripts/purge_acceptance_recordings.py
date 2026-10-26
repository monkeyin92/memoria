#!/usr/bin/env python3
"""Delete device-acceptance recordings older than 30 days and log the hash.

The corpus path already has ``CorpusRetentionWorker``. This covers the files a
field run leaves under ``share/真机测试`` and ``docs/acceptance``. A run with no
``--apply`` prints what it would delete and changes nothing. A receipt lists
the sha256 and the byte size of every deleted file, never its contents.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

RECORDING_SUFFIXES = {".wav", ".ogg", ".mp3", ".pcm", ".flac"}
DEFAULT_ROOTS = (Path("share/真机测试"), Path("docs/acceptance"))
RETENTION_SECONDS = 30 * 86400


def due(root: Path, *, now: float, retention_seconds: int) -> list[Path]:
    if retention_seconds < 0:
        raise ValueError("retention must be non-negative")
    cutoff = now - retention_seconds
    found = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in RECORDING_SUFFIXES
        and path.stat().st_mtime <= cutoff
    ]
    return sorted(found)


def purge(paths: list[Path], receipt: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in paths:
        data = path.read_bytes()
        rows.append(
            {
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "path": str(path),
            }
        )
        path.unlink()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(
        json.dumps({"deleted": rows}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, action="append", dest="roots")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.retention_days < 0:
        parser.error("retention must be non-negative")
    roots = args.roots or list(DEFAULT_ROOTS)
    now = time.time() if args.now is None else args.now
    pending = [
        path
        for root in roots
        if root.is_dir()
        for path in due(root, now=now, retention_seconds=args.retention_days * 86400)
    ]
    for path in pending:
        print(f"DUE {path}")
    if not args.apply:
        print(f"purge_mode=dry-run due={len(pending)}")
        return 0
    if args.receipt is None:
        print("purge_mode=refused reason=receipt_required", file=sys.stderr)
        return 2
    rows = purge(pending, args.receipt)
    print(f"purge_mode=apply deleted={len(rows)} receipt={args.receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
