#!/usr/bin/env python3
"""Re-apply every completed bound-subject deletion after a data restore.

Run inside a one-off Control API container, after restoring PostgreSQL/MinIO
from a backup and BEFORE traffic resumes or memory projections are rebuilt:

    docker compose ... run --rm --no-deps control-api \\
        /app/.venv/bin/python -m scripts.replay_subject_deletions --confirm-replay

The deletion ledger lives in the control database; a restored data store brings
erased rows back while the ledger still says ``completed``. A backup also rolls
the ledger back, so deletions completed after that backup are missing from it.
The restore runbook therefore exports the online ledger first, restores, then
merges the snapshot before replaying:

    ... --export-ledger /root/memoria-deletion-ledger.json
    ... --ledger-snapshot /root/memoria-deletion-ledger.json --confirm-replay

The snapshot contains subject/account identifiers: keep it root-only 0600 and
delete it after the replay. This command prints counts only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any


def _export_ledger(path: Path, limit: int) -> int:
    """Read the live ledger without starting the app or its background workers."""

    from services.control_api.app.config import ControlSettings
    from services.control_api.app.database import MemoryStore
    from services.governance.subject_deletion import SubjectDeletionLedger

    settings = ControlSettings()
    store = MemoryStore(
        settings.memoria_db_path,
        dsn=settings.control_database_url.get_secret_value().strip(),
        initialize_schema=False,
    )
    try:
        ledger = SubjectDeletionLedger.beside(store, settings.memoria_db_path)
        ledger.initialize()
        return ledger.export_completed(path, limit=limit)
    finally:
        store.close()


async def _run(
    *,
    limit: int,
    export_ledger: Path | None,
    ledger_snapshot: Path | None,
) -> dict[str, int]:
    if export_ledger is not None:
        exported = await asyncio.to_thread(_export_ledger, export_ledger, limit)
        return {"exported": exported}

    from services.control_api.app.main import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        service: Any = getattr(app.state, "subject_deletion", None)
        if service is None:
            raise RuntimeError("subject deletion service is not configured")
        report: dict[str, int] = {}
        if ledger_snapshot is not None:
            report.update(await asyncio.to_thread(service.ledger.merge_completed, ledger_snapshot))
        report.update(await service.replay_completed_deletions(limit=limit))
        account_service: Any = getattr(app.state, "account_data_governance", None)
        if account_service is not None and hasattr(account_service, "replay_completed_deletions"):
            report.update(await account_service.replay_completed_deletions(limit=limit))
        return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-apply completed bound-subject deletions after a restore."
    )
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--export-ledger",
        type=Path,
        metavar="PATH",
        help="write the online ledger's completed rows to PATH (root 0600) and exit",
    )
    parser.add_argument(
        "--ledger-snapshot",
        type=Path,
        metavar="PATH",
        help="merge a pre-restore ledger snapshot into the restored ledger, then replay",
    )
    parser.add_argument("--confirm-replay", action="store_true")
    args = parser.parse_args()
    if args.export_ledger is not None:
        if args.ledger_snapshot is not None or args.confirm_replay:
            parser.error("--export-ledger cannot be combined with replay flags")
    elif not args.confirm_replay:
        parser.error("--confirm-replay is required: restored subject data will be deleted again")
    if not 1 <= args.limit <= 100_000:
        parser.error("--limit must be between 1 and 100000")
    report = asyncio.run(
        _run(
            limit=args.limit,
            export_ledger=args.export_ledger,
            ledger_snapshot=args.ledger_snapshot,
        )
    )
    print(json.dumps(report, sort_keys=True))
    incomplete = int(report.get("incomplete", 0)) + int(report.get("account_incomplete", 0))
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
