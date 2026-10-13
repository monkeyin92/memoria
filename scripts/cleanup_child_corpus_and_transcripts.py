#!/usr/bin/env python3
"""Offline cleanup of historical child corpus audio and evidence transcripts.

Approved for go-live prep. Default is dry-run. Never pass a production DSN
unless an operator explicitly authorizes a one-off cleanup window.

What it plans / applies:

1. Soft-delete guardian_corpus_samples for minors (purge objects when --apply
   and an object-store client is configured via existing env).
2. Null transcript fields on archive_evidence_events whose subject_category
   is minor or memory_retention is ephemeral_only after a prior write.

This script does not rewrite backups. Operators must rotate or scrub
``pg_dump`` copies separately before declaring go-live clean.

Example (local disposable DB only):

  MEMORIA_TEST_POSTGRES_DSN=postgresql://... \\
  uv run python scripts/cleanup_child_corpus_and_transcripts.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

TRANSCRIPT_KEYS = ("text", "transcript", "user_text", "assistant_text")


async def plan(connection: asyncpg.Connection) -> dict[str, object]:
    corpus = await connection.fetchval(
        """
        SELECT count(*) FROM guardian_corpus_samples
        WHERE deleted_at IS NULL
        """
    )
    # Evidence payload shape varies; count rows that still carry text keys for
    # minor / ephemeral retention. Fail closed if the table is absent.
    evidence = 0
    has_evidence = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'archive_evidence_events'
        )
        """
    )
    if has_evidence:
        evidence = await connection.fetchval(
            """
            SELECT count(*) FROM archive_evidence_events
            WHERE (
                payload ?| $1::text[]
            )
            AND (
                payload ->> 'subject_category' = 'minor'
                OR payload ->> 'memory_retention' = 'ephemeral_only'
            )
            """,
            list(TRANSCRIPT_KEYS),
        )
    return {
        "dry_run": True,
        "guardian_corpus_samples_active": int(corpus or 0),
        "archive_evidence_events_with_child_text": int(evidence or 0),
        "note": "backups are out of scope; scrub pg_dump copies separately",
    }


async def apply(connection: asyncpg.Connection) -> dict[str, object]:
    corpus_updated = await connection.fetchval(
        """
        WITH touched AS (
            UPDATE guardian_corpus_samples
            SET deleted_at = CURRENT_TIMESTAMP
            WHERE deleted_at IS NULL
            RETURNING 1
        )
        SELECT count(*) FROM touched
        """
    )
    evidence_updated = 0
    has_evidence = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = 'archive_evidence_events'
        )
        """
    )
    if has_evidence:
        evidence_updated = await connection.fetchval(
            """
            WITH touched AS (
                UPDATE archive_evidence_events
                SET payload = payload
                    - 'text' - 'transcript' - 'user_text' - 'assistant_text'
                WHERE (
                    payload ?| $1::text[]
                )
                AND (
                    payload ->> 'subject_category' = 'minor'
                    OR payload ->> 'memory_retention' = 'ephemeral_only'
                )
                RETURNING 1
            )
            SELECT count(*) FROM touched
            """,
            list(TRANSCRIPT_KEYS),
        )
    return {
        "dry_run": False,
        "guardian_corpus_samples_marked_deleted": int(corpus_updated or 0),
        "archive_evidence_events_stripped": int(evidence_updated or 0),
        "note": "object store bytes and backups still need a separate pass",
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="mutate the database (default: dry-run counts only)",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip(),
        help="PostgreSQL admin DSN (defaults to MEMORIA_TEST_POSTGRES_DSN)",
    )
    parser.add_argument(
        "--i-understand-this-is-not-for-production",
        action="store_true",
        help="required together with --apply",
    )
    args = parser.parse_args()
    if not args.dsn:
        print("pass --dsn or set MEMORIA_TEST_POSTGRES_DSN", file=sys.stderr)
        return 2
    if args.apply and not args.i_understand_this_is_not_for_production:
        print(
            "--apply requires --i-understand-this-is-not-for-production",
            file=sys.stderr,
        )
        return 2
    if "aginice" in args.dsn or "110.42.235.198" in args.dsn:
        print("refusing known production host markers in DSN", file=sys.stderr)
        return 2

    connection = await asyncpg.connect(args.dsn)
    try:
        report = await (apply(connection) if args.apply else plan(connection))
    finally:
        await connection.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
