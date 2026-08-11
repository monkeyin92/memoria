#!/usr/bin/env python3
"""Manually replay one audited archive compiler dead letter."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict

from services.archive.postgres_archive import PostgresLifeArchive


async def _run(
    *,
    dsn: str,
    account_id: str,
    outbox_id: str,
    actor_id: str,
    reason: str,
) -> dict[str, object]:
    archive = PostgresLifeArchive(dsn)
    try:
        result = await archive.replay_dead_letter(
            account_id=account_id,
            outbox_id=outbox_id,
            actor_id=actor_id,
            reason=reason,
        )
        payload = asdict(result)
        payload["replayed_at"] = result.replayed_at.isoformat()
        return payload
    finally:
        await archive.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Requeue one archive compiler dead letter and append an account-scoped audit record."
        )
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("MEMORIA_ARCHIVE_DATABASE_URL", ""),
        help="archive API PostgreSQL DSN (defaults to MEMORIA_ARCHIVE_DATABASE_URL)",
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--outbox-id", required=True)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--confirm-replay",
        action="store_true",
        help="required acknowledgement that the dead letter will be requeued",
    )
    args = parser.parse_args()
    dsn = str(args.dsn).strip()
    if not dsn:
        parser.error("--dsn or MEMORIA_ARCHIVE_DATABASE_URL is required")
    if not args.confirm_replay:
        parser.error("--confirm-replay is required")
    result = asyncio.run(
        _run(
            dsn=dsn,
            account_id=str(args.account_id),
            outbox_id=str(args.outbox_id),
            actor_id=str(args.actor_id),
            reason=str(args.reason),
        )
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
