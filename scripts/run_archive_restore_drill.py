#!/usr/bin/env python3
"""Run a PostgreSQL and encrypted-object restore drill into fresh targets."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path

from services.archive.restore_drill import (
    LocalObjectRestorePlan,
    run_postgres_restore_drill,
)


def _key_map(environment_name: str) -> Mapping[str, str]:
    raw = os.getenv(environment_name, "{}").strip() or "{}"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(version, str) and version.strip() and isinstance(key, str) and key.strip()
        for version, key in parsed.items()
    ):
        raise ValueError(f"{environment_name} must be a JSON object of version to key")
    return parsed


def _plan(
    *,
    domain: str,
    source_root: Path | None,
    restore_root: Path | None,
    key_environment: str,
) -> LocalObjectRestorePlan | None:
    if source_root is None and restore_root is None:
        return None
    if source_root is None or restore_root is None:
        raise ValueError(f"{domain} source and restore roots must be provided together")
    return LocalObjectRestorePlan(
        domain=domain,
        source_root=source_root,
        restore_root=restore_root,
        keys=_key_map(key_environment),
    )


def _write_report(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
        json.dump(payload, destination, ensure_ascii=False, indent=2, sort_keys=True)
        destination.write("\n")
        destination.flush()
        os.fsync(destination.fileno())


async def _run(args: argparse.Namespace) -> dict[str, object]:
    source_dsn = os.getenv("MEMORIA_RESTORE_SOURCE_DSN", "").strip()
    admin_dsn = os.getenv("MEMORIA_RESTORE_ADMIN_DSN", "").strip()
    if not source_dsn or not admin_dsn:
        raise ValueError("MEMORIA_RESTORE_SOURCE_DSN and MEMORIA_RESTORE_ADMIN_DSN are required")
    plans = tuple(
        plan
        for plan in (
            _plan(
                domain="archive",
                source_root=args.archive_source_root,
                restore_root=args.archive_restore_root,
                key_environment="MEMORIA_RESTORE_ARCHIVE_KEYS_JSON",
            ),
            _plan(
                domain="voice",
                source_root=args.voice_source_root,
                restore_root=args.voice_restore_root,
                key_environment="MEMORIA_RESTORE_VOICE_KEYS_JSON",
            ),
        )
        if plan is not None
    )
    report = await run_postgres_restore_drill(
        source_dsn=source_dsn,
        admin_dsn=admin_dsn,
        restore_database=args.restore_database,
        dump_path=args.dump,
        postgres_container=args.postgres_container,
        local_objects=plans,
    )
    return report.as_dict()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restore-database", required=True)
    parser.add_argument("--dump", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--postgres-container")
    parser.add_argument("--archive-source-root", type=Path)
    parser.add_argument("--archive-restore-root", type=Path)
    parser.add_argument("--voice-source-root", type=Path)
    parser.add_argument("--voice-restore-root", type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(_run(args))
    except Exception as exc:
        payload = {"passed": False, "error_code": type(exc).__name__}
    _write_report(args.report, payload)
    print(json.dumps({"passed": payload["passed"], "report": str(args.report)}))
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
