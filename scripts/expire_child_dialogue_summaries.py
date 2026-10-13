#!/usr/bin/env python3
"""Physically delete expired ``dialogue_summary`` rows and verify remaining=0.

Uses the maintenance role via ``memory_kind_erase(..., only_expired=true)``.
Does not touch production unless you pass a real DSN; default is dry-run
listing of due subjects only.

Example (local):

  MEMORIA_MEMORY_API_DATABASE_URL=... \\
  MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=... \\
  uv run python scripts/expire_child_dialogue_summaries.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import urlsplit

import asyncpg
from services.guardian.child_memory import KIND_DIALOGUE_SUMMARY
from services.memory_scope.child_layers import ChildLayerStore


async def _due_subjects(maintenance_dsn: str) -> list[str]:
    connection = await asyncpg.connect(maintenance_dsn)
    try:
        subjects = await connection.fetchval(
            "SELECT memory_kind_due_subjects($1)", KIND_DIALOGUE_SUMMARY
        )
    finally:
        await connection.close()
    if not subjects:
        return []
    return [str(item) for item in subjects]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform physical deletes (default: dry-run list only)",
    )
    parser.add_argument(
        "--subject",
        action="append",
        default=[],
        help="limit to one subject id (repeatable); skips discovery SELECT",
    )
    args = parser.parse_args()
    api_dsn = os.environ.get("MEMORIA_MEMORY_API_DATABASE_URL", "").strip()
    maintenance_dsn = os.environ.get(
        "MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL", ""
    ).strip()
    if not api_dsn or not maintenance_dsn:
        print(
            "need MEMORIA_MEMORY_API_DATABASE_URL and "
            "MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL",
            file=sys.stderr,
        )
        return 2
    if urlsplit(maintenance_dsn).username != "memoria_memory_maintenance":
        print(
            "maintenance DSN username must be memoria_memory_maintenance",
            file=sys.stderr,
        )
        return 2

    subjects = list(args.subject) or await _due_subjects(maintenance_dsn)
    report: dict[str, object] = {
        "dry_run": not args.apply,
        "subjects": subjects,
        "results": [],
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    store = ChildLayerStore(api_dsn=api_dsn, maintenance_dsn=maintenance_dsn)
    await store.initialize()
    try:
        for subject_id in subjects:
            result = await store.expire_due_summaries(subject_id=subject_id)
            report["results"].append({"subject_id": subject_id, **result})  # type: ignore[arg-type]
    finally:
        await store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
