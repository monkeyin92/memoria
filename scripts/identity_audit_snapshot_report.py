#!/usr/bin/env python3
"""Report the persons-update audit rows that still carry a whole-row snapshot.

Issue #283 changed ``identity_audit_person_update_event`` so a person update
records only the changed field names, the actor and the time. Earlier rows kept
``to_jsonb(NEW)``: a full snapshot including ``display_name`` and ``age_band``.
This script never rewrites them. It reports how many exist and the time range
they span so the owner of the data can decide separately.

Read-only by construction: only a COUNT/MIN/MAX query is issued, and the DSN
comes from ``--dsn`` or ``MEMORIA_TEST_POSTGRES_DSN`` so an application or
production setting can never be selected by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import asyncpg

#: Update audit actions written by ``identity_audit_person_update_event``.
PERSON_UPDATE_ACTIONS: Final[tuple[str, ...]] = (
    "person.self_update",
    "person.system_update",
    "person.age_evidence.update",
)

_SNAPSHOT_WHERE = """
WHERE action = ANY($1::text[])
  AND payload_json ? 'display_name'
"""


@dataclass(frozen=True)
class SnapshotReport:
    """How many legacy snapshot rows exist and when they were written."""

    row_count: int
    earliest: datetime | None
    latest: datetime | None

    def as_line(self) -> str:
        if self.row_count == 0:
            return "0"
        earliest = self.earliest.isoformat() if self.earliest else "unknown"
        latest = self.latest.isoformat() if self.latest else "unknown"
        return f"{self.row_count} {earliest} {latest}"


async def report(connection: asyncpg.Connection) -> SnapshotReport:
    row = await connection.fetchrow(
        f"""
        SELECT count(*) AS row_count,
               min(created_at) AS earliest,
               max(created_at) AS latest
        FROM identity_audit_events
        {_SNAPSHOT_WHERE}
        """,
        list(PERSON_UPDATE_ACTIONS),
    )
    if row is None:
        return SnapshotReport(row_count=0, earliest=None, latest=None)
    return SnapshotReport(
        row_count=int(row["row_count"] or 0),
        earliest=row["earliest"],
        latest=row["latest"],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip(),
        help="PostgreSQL DSN (defaults to MEMORIA_TEST_POSTGRES_DSN)",
    )
    return parser


async def run(*, dsn: str) -> SnapshotReport:
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        return await report(connection)
    finally:
        await connection.close()


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dsn:
        print("pass --dsn or set MEMORIA_TEST_POSTGRES_DSN", file=sys.stderr)
        return 2
    print((await run(dsn=args.dsn)).as_line())
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
