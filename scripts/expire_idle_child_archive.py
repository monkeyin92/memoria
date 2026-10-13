#!/usr/bin/env python3
"""Delete ``child_archive`` rows for subjects idle past 180 days.

Lists subjects with ``child_archive`` rows via the maintenance role, then
keeps only those whose ``guardian_subject_activity.last_active_at`` is older
than 180 days (or missing). Default dry-run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import asyncpg
from services.guardian.child_memory import ARCHIVE_IDLE_TTL, KIND_CHILD_ARCHIVE
from services.memory_scope.child_layers import ChildLayerStore


async def archive_subjects(maintenance_dsn: str) -> list[str]:
    connection = await asyncpg.connect(maintenance_dsn)
    try:
        subjects = await connection.fetchval(
            "SELECT memory_kind_subjects($1)", KIND_CHILD_ARCHIVE
        )
    finally:
        await connection.close()
    if not subjects:
        return []
    return [str(item) for item in subjects]


async def idle_filter(
    guardian_dsn: str, subjects: list[str], *, now: datetime
) -> list[str]:
    if not subjects:
        return []
    cutoff = now - ARCHIVE_IDLE_TTL
    connection = await asyncpg.connect(guardian_dsn)
    try:
        rows = await connection.fetch(
            """
            SELECT subject_person_id, last_active_at
            FROM guardian_subject_activity
            WHERE subject_person_id = ANY($1::text[])
            """,
            subjects,
        )
    except asyncpg.UndefinedTableError:
        # No activity table yet: treat every archive subject as idle.
        return subjects
    finally:
        await connection.close()
    active = {
        str(row["subject_person_id"]): row["last_active_at"] for row in rows
    }
    idle: list[str] = []
    for subject_id in subjects:
        last = active.get(subject_id)
        if last is None or last <= cutoff:
            idle.append(subject_id)
    return idle


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

    now = datetime.now(UTC)
    if args.subject:
        subjects = list(args.subject)
    else:
        if not guardian_dsn:
            print(
                "pass --subject or MEMORIA_GUARDIAN_DATABASE_URL for idle filter",
                file=sys.stderr,
            )
            return 2
        subjects = await idle_filter(
            guardian_dsn, await archive_subjects(maintenance_dsn), now=now
        )

    report: dict[str, object] = {
        "dry_run": not args.apply,
        "idle_ttl_days": ARCHIVE_IDLE_TTL // timedelta(days=1),
        "subjects": subjects,
        "results": [],
    }
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0

    store = ChildLayerStore(api_dsn=api_dsn, maintenance_dsn=maintenance_dsn)
    await store.initialize()
    try:
        results = []
        for subject_id in subjects:
            erased = await store.erase_kind(
                subject_id=subject_id, kind=KIND_CHILD_ARCHIVE, only_expired=False
            )
            remaining = await store.remaining_kind(
                subject_id=subject_id, kind=KIND_CHILD_ARCHIVE, only_expired=False
            )
            if int(remaining.get("memory_records", 0)):
                raise RuntimeError(
                    f"child_archive remaining after idle purge: {remaining}"
                )
            results.append(
                {"subject_id": subject_id, "erased": erased, "remaining": remaining}
            )
        report["results"] = results
    finally:
        await store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
