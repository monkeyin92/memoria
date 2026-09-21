#!/usr/bin/env python3
"""Archive old outputs/acceptance runs without touching other outputs."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
import time
from pathlib import Path


def tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("outputs/acceptance"))
    parser.add_argument("--older-than-days", type=int, default=30)
    parser.add_argument("--keep", type=int, default=5)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.keep < 0 or args.older_than_days < 0:
        parser.error("retention values must be non-negative")
    root = args.root.resolve()
    if root.name != "acceptance" or not root.is_dir():
        parser.error("--root must be an existing outputs/acceptance directory")

    runs = sorted(
        (path for path in root.glob("run-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    cutoff = time.time() - args.older_than_days * 86400
    candidates: list[Path] = []
    for index, run in enumerate(runs):
        if index < args.keep or run.stat().st_mtime > cutoff:
            print(f"KEEP {run.name}")
        else:
            candidates.append(run)
            print(f"ARCHIVE candidate {run.name}")
    if not args.apply:
        print(f"archive_mode=dry-run candidates={len(candidates)}")
        return 0

    archive_root = root.parent / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    for run in candidates:
        target = archive_root / f"{run.name}.tar.gz"
        with tarfile.open(target, "w:gz") as archive:
            archive.add(run, arcname=run.name)
        checksum = hashlib.sha256(target.read_bytes()).hexdigest()
        target.with_suffix(target.suffix + ".sha256").write_text(
            f"{checksum}  {target.name}\n", encoding="utf-8"
        )
        verify_root = archive_root / ".verify"
        shutil.rmtree(verify_root, ignore_errors=True)
        verify_root.mkdir()
        with tarfile.open(target, "r:gz") as archive:
            archive.extractall(verify_root, filter="data")
        if tree_digest(run) != tree_digest(verify_root / run.name):
            raise RuntimeError(f"archive verification failed: {run}")
        shutil.rmtree(run)
        shutil.rmtree(verify_root)
    print(f"archive_mode=apply archived={len(candidates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
