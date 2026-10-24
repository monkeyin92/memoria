#!/usr/bin/env python3
"""Read-only daily patrol counts for claim vectors and unlinked child claims.

Prints exactly one JSON object with counts, never an account, claim, subject or
event id:

  {"corrected": 0, "missing": 2, "unlinked_child_claims": 1}

``unlinked_child_claims`` counts child claims that have no
``memory_claim_subjects`` row: live source evidence, not retracted, and a
``subject_key`` that names a child either by the ``son:``/``daughter:``
prefixes from ``services/archive/claim_subjects.py`` or by an archive person
row whose ``relationship_to_owner`` is son/daughter.  A parent claim linked to
every bound child is the normal state, so a leftover count means identity was
unavailable or no child was bound at compile time.  It does not change the
exit code; it only makes the state visible.

Exit codes follow ``disk_patrol.sh``: 0 means no gap, 1 means at least one
claim is missing its current-model vector, 3 means this check could not run
(no archive DSN, no embedder, a query error).  Only SELECTs run here; the
repair itself is the Control API background worker or the manual backfill.

Pass ``--account`` to check one account; without it the control store's
registered accounts are read.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from services.archive.memory_vector_backfill import ClaimVectorBackfill, MissingVectorCount
from services.control_api.app.config import ControlSettings
from services.control_api.app.memory_components import build_memory_embedder

__all__ = ["main"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--account",
        action="append",
        default=[],
        help="limit to one account id (repeatable); default: every registered account",
    )
    return parser


def _registered_accounts(settings: ControlSettings) -> tuple[str, ...]:
    from services.control_api.app.database import MemoryStore

    store = MemoryStore(
        settings.memoria_db_path,
        dsn=settings.control_database_url.get_secret_value().strip(),
        initialize_schema=False,
    )
    try:
        return store.list_account_ids()
    finally:
        store.close()


async def _count(
    settings: ControlSettings,
    account_ids: Sequence[str],
) -> MissingVectorCount:
    import asyncpg

    dsn = settings.archive_database_url.get_secret_value().strip()
    if not dsn:
        raise RuntimeError("MEMORIA_ARCHIVE_DATABASE_URL is required")
    embedder = build_memory_embedder(settings)
    if embedder is None:
        raise RuntimeError("memory embeddings are not configured")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2, command_timeout=15)
    if pool is None:  # pragma: no cover
        raise RuntimeError("failed to create the archive pool")
    try:
        helper = ClaimVectorBackfill(pool, embedder)
        total = corrected = unlinked = 0
        for account_id in account_ids:
            count = await helper.count(account_id=account_id)
            total += count.total
            corrected += count.corrected
            unlinked += count.unlinked_child_claims
        return MissingVectorCount(
            total=total,
            corrected=corrected,
            unlinked_child_claims=unlinked,
        )
    finally:
        await pool.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = ControlSettings()
        account_ids = args.account or _registered_accounts(settings)
        count = asyncio.run(_count(settings, account_ids))
    except Exception as exc:
        print(f"memory vector count failed: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(
        json.dumps(
            {
                "missing": count.total,
                "corrected": count.corrected,
                "unlinked_child_claims": count.unlinked_child_claims,
            },
            sort_keys=True,
        )
    )
    return 1 if count.total else 0


if __name__ == "__main__":
    raise SystemExit(main())
