"""Measure live pgvector memory-search P50/P95, storage and shared-buffer residency."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.postgres_memory_benchmark import (
    benchmark_postgres_memory_search,
    benchmark_report_json,
)
from services.archive.postgres_memory_catalog import (
    PostgresMemoryCatalog,
    QwenMemoryEmbedder,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.getenv("MEMORIA_ARCHIVE_DATABASE_URL", ""))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", type=Path)
    return parser


async def _run(args: argparse.Namespace) -> str:
    if not args.dsn:
        raise ValueError("--dsn or MEMORIA_ARCHIVE_DATABASE_URL is required")
    query_data = json.loads(args.queries.read_text(encoding="utf-8"))
    if not isinstance(query_data, list) or not all(
        isinstance(value, str) and value.strip() for value in query_data
    ):
        raise ValueError("--queries must contain a JSON array of non-empty strings")
    endpoint = os.getenv("MEMORIA_MEMORY_EMBEDDING_URL", "")
    api_key = os.getenv("MEMORIA_MEMORY_EMBEDDING_API_KEY", "")
    model = os.getenv("MEMORIA_MEMORY_EMBEDDING_MODEL", "")
    dimensions = int(os.getenv("MEMORIA_MEMORY_EMBEDDING_DIMENSIONS", "1024"))
    if not endpoint or not api_key or not model:
        raise ValueError(
            "memory embedding URL, API key and model are required for pgvector benchmark"
        )
    catalog = PostgresMemoryCatalog(
        args.dsn,
        extractor=RuleBasedMemoryExtractor(),
        embedder=QwenMemoryEmbedder(
            endpoint=endpoint,
            api_key=api_key,
            model=model,
            dimensions=dimensions,
        ),
        require_vector=True,
    )
    try:
        report = await benchmark_postgres_memory_search(
            catalog=catalog,
            dsn=args.dsn,
            account_id=args.account_id,
            queries=query_data,
            iterations=args.iterations,
            embedding_model=model,
            embedding_dimensions=dimensions,
        )
    finally:
        await catalog.close()
    return benchmark_report_json(report)


def main() -> int:
    args = _parser().parse_args()
    payload = asyncio.run(_run(args))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
