#!/usr/bin/env python3
"""Remove transcript fields from historical minor and unknown evidence rows.

The default mode is a read-only count. ``--apply`` performs the same targeted
update in one transaction. The script accepts only ``--dsn`` or
``MEMORIA_TEST_POSTGRES_DSN`` so it cannot accidentally select an application
or production database setting.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from services.guardian.child_memory import (
    TRANSCRIPT_KEYS,
    strip_transcript_fields,
)

if TYPE_CHECKING:
    import asyncpg

SUBJECT_CATEGORIES: Final[tuple[str, ...]] = ("minor", "unknown")

_TARGET_WHERE = """
WHERE payload ->> 'subject_category' IN ('minor', 'unknown')
  AND payload ?| $1::text[]
"""


def strip_minor_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """Return the historical-row transformation without exposing payload data."""

    cleaned = dict(payload)
    if cleaned.get("subject_category") in SUBJECT_CATEGORIES:
        return strip_transcript_fields(cleaned)
    return cleaned


async def count_affected(connection: asyncpg.Connection) -> int:
    count = await connection.fetchval(
        f"""
        SELECT count(*)
        FROM archive_evidence_events
        {_TARGET_WHERE}
        """,
        list(TRANSCRIPT_KEYS),
    )
    return int(count or 0)


async def strip_affected(connection: asyncpg.Connection) -> int:
    async with connection.transaction():
        count = await connection.fetchval(
            f"""
            WITH touched AS (
                UPDATE archive_evidence_events
                SET payload = payload
                    - 'text'
                    - 'transcript'
                    - 'user_text'
                    - 'assistant_text'
                {_TARGET_WHERE}
                RETURNING event_id
            )
            SELECT count(*) FROM touched
            """,
            list(TRANSCRIPT_KEYS),
        )
    return int(count or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip(),
        help="PostgreSQL test DSN (defaults to MEMORIA_TEST_POSTGRES_DSN)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="remove the four transcript keys (default is dry-run)",
    )
    return parser


async def run(*, dsn: str, apply: bool) -> int:
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        return await (strip_affected(connection) if apply else count_affected(connection))
    finally:
        await connection.close()


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.dsn:
        print("pass --dsn or set MEMORIA_TEST_POSTGRES_DSN", file=sys.stderr)
        return 2
    print(await run(dsn=args.dsn, apply=args.apply))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
