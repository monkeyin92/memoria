#!/usr/bin/env python3
"""Re-apply every completed bound-subject deletion after a data restore.

Run inside a one-off Control API container, after restoring PostgreSQL/MinIO
from a backup and BEFORE traffic resumes or memory projections are rebuilt:

    docker compose ... run --rm --no-deps control-api \\
        /app/.venv/bin/python -m scripts.replay_subject_deletions --confirm-replay

The deletion ledger lives in the control database; a restored data store brings
erased rows back while the ledger still says ``completed``. This replays each of
those deletions through the fully wired production saga (idempotent for data
that is already gone). Prints counts only, never subject ids.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any


async def _run(limit: int) -> dict[str, int]:
    from services.control_api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        service: Any = getattr(app.state, "subject_deletion", None)
        if service is None:
            raise RuntimeError("subject deletion service is not configured")
        report: dict[str, int] = await service.replay_completed_deletions(limit=limit)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-apply completed bound-subject deletions after a restore."
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--confirm-replay", action="store_true")
    args = parser.parse_args()
    if not args.confirm_replay:
        parser.error("--confirm-replay is required: restored subject data will be deleted again")
    if not 1 <= args.limit <= 100_000:
        parser.error("--limit must be between 1 and 100000")
    report = asyncio.run(_run(args.limit))
    print(json.dumps(report, sort_keys=True))
    return 1 if report.get("incomplete") else 0


if __name__ == "__main__":
    raise SystemExit(main())
