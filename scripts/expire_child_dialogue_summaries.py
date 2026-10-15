#!/usr/bin/env python3
"""Physically delete expired ``dialogue_summary`` rows and verify remaining=0.

control-api runs the same expiry at startup and daily
(``ChildLayerRetentionWorker``); this script is the manual check. Uses the
maintenance role via ``memory_kind_erase(..., only_expired=true)``. Default is
a dry-run listing of due subjects only. A repeated ``--apply`` erases zero rows
and still exits 0.

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
from dataclasses import asdict
from urllib.parse import urlsplit

from services.guardian.child_memory import KIND_DIALOGUE_SUMMARY
from services.memory_scope.child_layer_retention import (
    ExpirySummary,
    due_summary_subjects,
    expire_due_summaries,
)
from services.memory_scope.child_layers import ChildLayerStore


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

    store = ChildLayerStore(api_dsn=api_dsn, maintenance_dsn=maintenance_dsn)
    await store.initialize()
    try:
        subjects = list(args.subject) or list(await due_summary_subjects(store))
        results = await expire_due_summaries(store, subjects) if args.apply else []
    finally:
        await store.close()
    report = {
        "dry_run": not args.apply,
        "subjects": subjects,
        "results": results,
        "summary": asdict(ExpirySummary.of(KIND_DIALOGUE_SUMMARY, subjects, results)),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if any("error" in result for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
