#!/usr/bin/env python3
"""Migrate legacy SQLite messages into a separate idempotent archive database."""

from __future__ import annotations

import argparse
import asyncio
import json

from services.archive.migration import (
    migrate_legacy_sqlite,
    migrate_legacy_sqlite_to_postgres,
)
from services.archive.postgres_archive import PostgresLifeArchive


async def _postgres(source: str, dsn: str, *, dry_run: bool) -> dict[str, object]:
    archive = PostgresLifeArchive(dsn)
    try:
        await archive.initialize()
        report = await migrate_legacy_sqlite_to_postgres(source, archive, dry_run=dry_run)
        return report.as_dict()
    finally:
        await archive.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="read-only legacy SQLite file")
    parser.add_argument("target", help="separate archive SQLite file or PostgreSQL DSN")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.target.startswith(("postgresql://", "postgres://")):
        result = asyncio.run(_postgres(args.source, args.target, dry_run=args.dry_run))
    else:
        result = migrate_legacy_sqlite(
            args.source, args.target, dry_run=args.dry_run
        ).as_dict()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
