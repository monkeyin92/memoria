#!/usr/bin/env python3
"""Rebuild PostgreSQL memory projections from the immutable archive ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict

from services.archive.restore_drill import rebuild_postgres_memory_projections
from services.control_api.app.config import ControlSettings
from services.control_api.app.memory_components import build_memory_embedder, build_memory_extractor


async def _run(dsn: str) -> dict[str, int]:
    settings = ControlSettings()
    embedder = build_memory_embedder(settings)
    if embedder is None:
        raise ValueError(
            "projection rebuild requires MEMORIA_MEMORY_EMBEDDING_URL, "
            "MEMORIA_MEMORY_EMBEDDING_API_KEY and MEMORIA_MEMORY_EMBEDDING_MODEL"
        )
    report = await rebuild_postgres_memory_projections(
        dsn,
        extractor=build_memory_extractor(settings),
        embedder=embedder,
        require_vector=True,
    )
    return {key: int(value) for key, value in asdict(report).items()}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild derived memory projections; the evidence ledger is unchanged."
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("MEMORIA_MEMORY_REBUILD_DATABASE_URL", ""),
        help="maintenance PostgreSQL DSN (defaults to MEMORIA_MEMORY_REBUILD_DATABASE_URL)",
    )
    parser.add_argument("--confirm-rebuild", action="store_true")
    args = parser.parse_args()
    dsn = str(args.dsn).strip()
    if not dsn:
        parser.error("--dsn or MEMORIA_MEMORY_REBUILD_DATABASE_URL is required")
    if not args.confirm_rebuild:
        parser.error("--confirm-rebuild is required because derived tables will be truncated")
    report = asyncio.run(_run(dsn))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if report["failed_events"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
