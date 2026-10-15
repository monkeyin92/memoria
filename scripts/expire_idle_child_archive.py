#!/usr/bin/env python3
"""Delete ``child_archive`` rows for subjects idle past 180 days.

control-api runs the same purge at startup and daily
(``ChildLayerRetentionWorker``); this script is the manual check. Lists
subjects with ``child_archive`` rows via the maintenance role, then keeps only
those whose ``guardian_subject_activity.last_active_at`` is older than 180 days
(or missing). Refuses to select subjects when the activity table is missing.
Default dry-run. A repeated ``--apply`` erases zero rows and still exits 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from services.guardian.child_memory import ARCHIVE_IDLE_TTL, KIND_CHILD_ARCHIVE
from services.guardian.subject_activity import SubjectActivityStore
from services.memory_scope.child_layer_retention import (
    ExpirySummary,
    IdleActivityUnavailable,
    expire_idle_archives,
    idle_archive_subjects,
)
from services.memory_scope.child_layers import ChildLayerStore


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--subject", action="append", default=[])
    args = parser.parse_args()
    api_dsn = os.environ.get("MEMORIA_MEMORY_API_DATABASE_URL", "").strip()
    maintenance_dsn = os.environ.get(
        "MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL", ""
    ).strip()
    guardian_dsn = os.environ.get("MEMORIA_GUARDIAN_DATABASE_URL", "").strip()
    if not api_dsn or not maintenance_dsn:
        print("need memory API and maintenance DSNs", file=sys.stderr)
        return 2
    if urlsplit(maintenance_dsn).username != "memoria_memory_maintenance":
        print(
            "maintenance DSN username must be memoria_memory_maintenance",
            file=sys.stderr,
        )
        return 2
    if not args.subject and not guardian_dsn:
        print(
            "pass --subject or MEMORIA_GUARDIAN_DATABASE_URL for idle filter",
            file=sys.stderr,
        )
        return 2

    store = ChildLayerStore(api_dsn=api_dsn, maintenance_dsn=maintenance_dsn)
    activity = SubjectActivityStore(guardian_dsn) if guardian_dsn else None
    await store.initialize()
    try:
        subjects = list(args.subject) or await idle_archive_subjects(
            store, activity, now=datetime.now(UTC)
        )
        results = await expire_idle_archives(store, subjects) if args.apply else []
    except IdleActivityUnavailable as exc:
        print(f"refusing to select idle archives: {exc}", file=sys.stderr)
        return 2
    finally:
        await store.close()
        if activity is not None:
            await activity.close()
    report = {
        "dry_run": not args.apply,
        "idle_ttl_days": ARCHIVE_IDLE_TTL // timedelta(days=1),
        "subjects": subjects,
        "results": results,
        "summary": asdict(ExpirySummary.of(KIND_CHILD_ARCHIVE, subjects, results)),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 1 if any("error" in result for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
