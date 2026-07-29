"""Read-only pgvector latency, storage and ANN-plan benchmark."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from typing import Protocol

import asyncpg

from services.archive.memory_domain import (
    MemorySearchQuery,
    MemorySearchResult,
)


class MemorySearchPort(Protocol):
    async def search(self, query: MemorySearchQuery) -> MemorySearchResult: ...


@dataclass(frozen=True, slots=True)
class PostgresMemoryBenchmarkReport:
    account_id: str
    query_count: int
    iterations: int
    latency_p50_ms: float
    latency_p95_ms: float
    search_document_rows: int
    vector_rows: int
    table_bytes: int
    index_bytes: int
    shared_buffer_resident_bytes: int | None
    index_sizes: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "query_count": self.query_count,
            "iterations": self.iterations,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "search_document_rows": self.search_document_rows,
            "vector_rows": self.vector_rows,
            "table_bytes": self.table_bytes,
            "index_bytes": self.index_bytes,
            "shared_buffer_resident_bytes": self.shared_buffer_resident_bytes,
            "index_sizes": [
                {"name": name, "bytes": byte_count}
                for name, byte_count in self.index_sizes
            ],
        }


async def benchmark_postgres_memory_search(
    *,
    catalog: MemorySearchPort,
    dsn: str,
    account_id: str,
    queries: Sequence[str],
    iterations: int = 10,
    embedding_model: str | None = None,
    embedding_dimensions: int | None = None,
) -> PostgresMemoryBenchmarkReport:
    if not account_id.strip() or not queries or any(not query.strip() for query in queries):
        raise ValueError("pgvector benchmark requires account_id and non-empty queries")
    if iterations < 1:
        raise ValueError("pgvector benchmark iterations must be positive")
    for text in queries:
        await catalog.search(
            MemorySearchQuery(
                account_id=account_id,
                speaker_class="owner",
                text=text,
                limit=10,
            )
        )
    latencies: list[float] = []
    for _ in range(iterations):
        for text in queries:
            started = perf_counter()
            await catalog.search(
                MemorySearchQuery(
                    account_id=account_id,
                    speaker_class="owner",
                    text=text,
                    limit=10,
                )
            )
            latencies.append((perf_counter() - started) * 1000)

    connection = await asyncpg.connect(dsn)
    try:
        await connection.execute("SELECT set_config('app.account_id', $1, false)", account_id)
        search_document_rows = int(
            await connection.fetchval(
                "SELECT count(*) FROM memory_search_documents WHERE account_id = $1",
                account_id,
            )
        )
        vector_clause = "account_id = $1"
        vector_parameters: list[object] = [account_id]
        if embedding_model is not None:
            vector_parameters.append(embedding_model)
            vector_clause += f" AND embedding_model = ${len(vector_parameters)}"
        if embedding_dimensions is not None:
            vector_parameters.append(embedding_dimensions)
            vector_clause += f" AND embedding_dimensions = ${len(vector_parameters)}"
        vector_rows = int(
            await connection.fetchval(
                f"SELECT count(*) FROM memory_vector_documents WHERE {vector_clause}",
                *vector_parameters,
            )
        )
        table_bytes = int(
            await connection.fetchval(
                """
                SELECT COALESCE(sum(pg_relation_size(relation)), 0)
                FROM unnest(
                    ARRAY[
                        'memory_search_documents'::regclass,
                        'memory_vector_documents'::regclass
                    ]
                ) relation
                """
            )
        )
        index_rows = await connection.fetch(
            """
            SELECT index_class.relname AS index_name,
                   pg_relation_size(index_class.oid) AS byte_count
            FROM pg_class table_class
            JOIN pg_index index_meta ON index_meta.indrelid = table_class.oid
            JOIN pg_class index_class ON index_class.oid = index_meta.indexrelid
            WHERE table_class.relname = ANY(
                ARRAY['memory_search_documents', 'memory_vector_documents']
            )
            ORDER BY index_class.relname
            """
        )
        index_sizes = tuple(
            (str(row["index_name"]), int(row["byte_count"])) for row in index_rows
        )
        resident_bytes = await _resident_bytes(connection)
    finally:
        await connection.close()
    return PostgresMemoryBenchmarkReport(
        account_id=account_id,
        query_count=len(queries),
        iterations=iterations,
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        search_document_rows=search_document_rows,
        vector_rows=vector_rows,
        table_bytes=table_bytes,
        index_bytes=sum(byte_count for _, byte_count in index_sizes),
        shared_buffer_resident_bytes=resident_bytes,
        index_sizes=index_sizes,
    )


async def explain_pgvector_ann(
    *,
    dsn: str,
    account_id: str,
    embedding_model: str,
    embedding_dimensions: int,
    vector: Sequence[float],
    limit: int = 10,
) -> tuple[str, bool]:
    if (
        not account_id.strip()
        or not embedding_model.strip()
        or embedding_dimensions < 1
        or len(vector) != embedding_dimensions
        or limit < 1
    ):
        raise ValueError("invalid pgvector EXPLAIN request")
    digest = sha256(
        f"{embedding_model}:{embedding_dimensions}".encode()
    ).hexdigest()[:12]
    expected_index = f"idx_memory_vector_hnsw_{digest}"
    connection = await asyncpg.connect(dsn)
    try:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.account_id', $1, true)",
                account_id,
            )
            await connection.execute("SET LOCAL enable_seqscan = off")
            plan = await connection.fetchval(
                f"""
                EXPLAIN (FORMAT JSON, COSTS TRUE)
                SELECT item_id
                FROM memory_vector_documents
                WHERE account_id = $1
                  AND embedding_model = $2
                  AND embedding_dimensions = $3
                ORDER BY embedding::vector({embedding_dimensions})
                         <=> $4::vector({embedding_dimensions})
                LIMIT $5
                """,
                account_id,
                embedding_model,
                embedding_dimensions,
                json.dumps(tuple(float(value) for value in vector), separators=(",", ":")),
                limit,
            )
    finally:
        await connection.close()
    plan_text = str(plan)
    return plan_text, expected_index in plan_text


async def _resident_bytes(connection: asyncpg.Connection) -> int | None:
    available = bool(
        await connection.fetchval(
            "SELECT to_regclass('pg_buffercache') IS NOT NULL"
        )
    )
    if not available:
        return None
    return int(
        await connection.fetchval(
            """
            SELECT COALESCE(count(*) * current_setting('block_size')::bigint, 0)
            FROM pg_buffercache
            WHERE relfilenode = ANY(
                ARRAY[
                    pg_relation_filenode('memory_search_documents'::regclass),
                    pg_relation_filenode('memory_vector_documents'::regclass)
                ]
            )
            """
        )
    )


def benchmark_report_json(report: PostgresMemoryBenchmarkReport) -> str:
    return json.dumps(
        report.as_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]
