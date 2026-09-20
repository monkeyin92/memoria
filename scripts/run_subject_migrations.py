#!/usr/bin/env python3
"""Operator CLI for the four account→subject migrations (P2-03).

The command is the only supported way to run the four SQLite seams; nothing in
the Control API calls them at startup.  Every write needs two fences: the
manifest SHA-256 the operator approved in ``plan`` and an exact confirmation
token.  ``status`` is the read path of the receipts and never writes.

Examples::

    uv run python scripts/run_subject_migrations.py list
    uv run python scripts/run_subject_migrations.py plan --migration persona
    uv run python scripts/run_subject_migrations.py apply --migration persona \\
        --expect-manifest-sha256 <sha256> --confirm apply-account-subject-migration
    uv run python scripts/run_subject_migrations.py status --migration persona
    uv run python scripts/run_subject_migrations.py rollback --migration persona \\
        --migration-id <id> --confirm rollback-account-subject-migration

Paths default to the repository's local development databases
(``data/memoria.sqlite3`` and ``data/memoria-identity.sqlite3``) and can be
overridden with ``--db`` / ``--identity`` / ``--archive`` / ``--target``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from services.governance.subject_migrations import (
    APPLY_CONFIRMATION,
    MIGRATION_NAMES,
    ROLLBACK_CONFIRMATION,
    MigrationError,
    MigrationTargets,
    apply,
    plan,
    rollback,
    status,
)

_DEFAULT_DB = "data/memoria.sqlite3"
_DEFAULT_IDENTITY = "data/memoria-identity.sqlite3"

_EXIT_OK = 0
_EXIT_REFUSED = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_subject_migrations",
        description=(
            "Plan, apply, roll back and inspect the four account→subject "
            "migrations. Nothing here runs automatically."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--migration",
            required=True,
            choices=list(MIGRATION_NAMES),
            help="which seam to operate on",
        )
        target.add_argument(
            "--db",
            default=os.environ.get("MEMORIA_DB_PATH", _DEFAULT_DB),
            help="Control API SQLite store (default: %(default)s)",
        )
        target.add_argument(
            "--identity",
            default=os.environ.get("MEMORIA_IDENTITY_DB_PATH", _DEFAULT_IDENTITY),
            help="identity authority SQLite store (default: %(default)s)",
        )
        target.add_argument(
            "--archive",
            default=None,
            help="legacy Archive store (default: the --db file)",
        )
        target.add_argument(
            "--target",
            default=None,
            help="Memory Scope database (default: the archive file)",
        )
        target.add_argument(
            "--receipt-out",
            default=None,
            help="write the JSON report to this file as well",
        )

    listing = subparsers.add_parser("list", help="list the four migrations")
    listing.add_argument("--receipt-out", default=None)

    plan_parser = subparsers.add_parser("plan", help="build a read-only manifest")
    add_common(plan_parser)

    apply_parser = subparsers.add_parser("apply", help="apply one approved manifest")
    add_common(apply_parser)
    apply_parser.add_argument(
        "--expect-manifest-sha256",
        required=True,
        help="manifest SHA-256 approved from the plan output",
    )
    apply_parser.add_argument(
        "--confirm",
        required=True,
        help=f"must be exactly {APPLY_CONFIRMATION!r}",
    )

    rollback_parser = subparsers.add_parser("rollback", help="roll back one stored run")
    add_common(rollback_parser)
    rollback_parser.add_argument("--migration-id", default=None)
    rollback_parser.add_argument("--manifest-sha256", default=None)
    rollback_parser.add_argument(
        "--expect-source-after-digest",
        default=None,
        help="durable_subject: the source digest the rollback must land on",
    )
    rollback_parser.add_argument(
        "--confirm",
        required=True,
        help=f"must be exactly {ROLLBACK_CONFIRMATION!r}",
    )

    status_parser = subparsers.add_parser("status", help="read the stored receipts")
    add_common(status_parser)
    status_parser.add_argument("--run-limit", type=int, default=20)
    return parser


def _targets(args: argparse.Namespace) -> MigrationTargets:
    identity = str(getattr(args, "identity", "") or "").strip()
    archive = str(getattr(args, "archive", "") or "").strip()
    target = str(getattr(args, "target", "") or "").strip()
    return MigrationTargets(
        control=Path(args.db).expanduser(),
        identity=Path(identity).expanduser() if identity else None,
        archive=Path(archive).expanduser() if archive else None,
        target=Path(target).expanduser() if target else None,
    )


def _emit(report: dict[str, Any], *, receipt_out: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(text)
    if receipt_out:
        Path(receipt_out).expanduser().write_text(text + "\n", encoding="utf-8")


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "list":
        return {
            "migrations": list(MIGRATION_NAMES),
            "apply_confirmation": APPLY_CONFIRMATION,
            "rollback_confirmation": ROLLBACK_CONFIRMATION,
            "note": "application startup never runs these; only this command does",
        }
    targets = _targets(args)
    if args.command == "plan":
        return plan(args.migration, targets)
    if args.command == "apply":
        return apply(
            args.migration,
            targets,
            expected_manifest_sha256=args.expect_manifest_sha256,
            confirmation=args.confirm,
        )
    if args.command == "rollback":
        return rollback(
            args.migration,
            targets,
            confirmation=args.confirm,
            migration_id=args.migration_id,
            manifest_sha256=args.manifest_sha256,
            expected_source_after_digest=args.expect_source_after_digest,
        )
    if args.command == "status":
        return status(args.migration, targets, run_limit=args.run_limit)
    raise MigrationError(f"unsupported command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = _run(args)
    except MigrationError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return _EXIT_REFUSED
    except Exception as error:  # noqa: BLE001 - surface the seam's own message
        print(
            json.dumps(
                {"ok": False, "error": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
            )
        )
        return _EXIT_REFUSED
    _emit(report, receipt_out=getattr(args, "receipt_out", None))
    return _EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
