"""Repair claims whose current-model vector is missing.

A best-effort correction commits its review even when the embedding service is
unavailable: #330 deletes the old vector, keeps the corrected search row, and
leaves the new vector missing.  Compilation has the same gap when an embedding
fails under ``fail_on_embedding_error=False``.  Nothing recalled such a claim
through the vector channel until this repair path existed.

The repair reads only the current ``memory_claims`` row: its subject key,
predicate and value.  It never reads a captured sentence, transcript, payload,
or the optional ``context:`` segment of a search row.  Every claim, corrected
or not, is embedded in the corrected-value format.  A full projection rebuild
still re-embeds an uncorrected claim in the compile-time format (with
``context:``); that difference is the existing #330 behavior.

A subject deletion removes that subject's evidence in the same transaction as
its claims, search documents and vectors, so a surviving claim always has live
evidence.  The queries below require that evidence row to still exist and
recheck it under the claim's row lock before writing; an orphan left by a
partial restore is skipped.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import cast

import asyncpg

from services.archive.claim_subjects import CHILD_KEY_PREFIXES, CHILD_RELATIONS
from services.archive.memory_domain import MemoryEmbedder, MemoryEmbeddingUnavailableError
from services.archive.memory_vector_cleanup import (
    claim_vector_body,
    claim_vector_title,
    document_embedding_input,
    write_document_vector,
)

logger = logging.getLogger(__name__)

# ``claim_subjects`` normalizes the key and checks these ASCII prefixes; the
# SQL below matches the same prefixes case-insensitively after lower(btrim()).
_CHILD_KEY_PATTERNS = tuple(f"{prefix}%" for prefix in CHILD_KEY_PREFIXES)
_CHILD_RELATION_VALUES = list(CHILD_RELATIONS)

# ``PostgresSubjectArchive.delete_events`` deletes the subject's evidence and
# cascades its claims, search rows and vectors in one transaction, so the
# source-evidence join is the reliable archive-side "subject still exists"
# test.  An orphan left by a partial restore has no evidence and is skipped.
_MISSING_CLAIM_SELECT = """
SELECT document.item_id AS claim_id, claim.value, claim.subject_key,
       claim.predicate, claim.source_event_id
FROM memory_search_documents document
JOIN memory_claims claim
  ON claim.claim_id = document.item_id
 AND claim.account_id = document.account_id
JOIN archive_evidence_events evidence
  ON evidence.event_id = claim.source_event_id
 AND evidence.account_id = claim.account_id
WHERE document.account_id = $1
  AND document.kind = 'claim'
  AND document.status <> 'retracted'
  AND claim.status <> 'retracted'
  AND NOT EXISTS (
      SELECT 1 FROM memory_vector_documents stored
      WHERE stored.account_id = document.account_id
        AND stored.item_id = document.item_id
        AND stored.embedding_model = $2
        AND stored.embedding_dimensions = $3
  )
ORDER BY document.observed_at, document.item_id
LIMIT $4
"""

_ONE_MISSING_CLAIM = """
SELECT document.item_id AS claim_id, claim.value, claim.subject_key,
       claim.predicate, claim.source_event_id
FROM memory_search_documents document
JOIN memory_claims claim
  ON claim.claim_id = document.item_id
 AND claim.account_id = document.account_id
JOIN archive_evidence_events evidence
  ON evidence.event_id = claim.source_event_id
 AND evidence.account_id = claim.account_id
WHERE document.account_id = $1
  AND document.item_id = $4
  AND document.kind = 'claim'
  AND document.status <> 'retracted'
  AND claim.status <> 'retracted'
  AND NOT EXISTS (
      SELECT 1 FROM memory_vector_documents stored
      WHERE stored.account_id = document.account_id
        AND stored.item_id = document.item_id
        AND stored.embedding_model = $2
        AND stored.embedding_dimensions = $3
  )
"""

# Claims that name a child but have no ``memory_claim_subjects`` row.  The
# child test matches ``claim_subjects._names_a_child``: a son:/daughter: key
# prefix, or a person row with the same canonical key and a son/daughter
# relation.  A linked claim is the normal state, including one parent claim
# linked to every bound child; a leftover unlinked claim means identity was
# unavailable or no child was bound at compile time.  The count is
# observational and never a repair target.
_UNLINKED_CHILD_CLAIM_COUNT = """
SELECT count(*)
FROM memory_claims claim
JOIN archive_evidence_events evidence
  ON evidence.event_id = claim.source_event_id
 AND evidence.account_id = claim.account_id
WHERE claim.account_id = $1
  AND claim.status <> 'retracted'
  AND NOT EXISTS (
      SELECT 1 FROM memory_claim_subjects linked
      WHERE linked.account_id = claim.account_id
        AND linked.claim_id = claim.claim_id
  )
  AND (
      lower(btrim(claim.subject_key)) LIKE ANY($2::text[])
      OR EXISTS (
          SELECT 1 FROM person_entities person
          WHERE person.account_id = claim.account_id
            AND person.canonical_key = claim.subject_key
            AND lower(person.relationship_to_owner) = ANY($3::text[])
      )
  )
"""

_MISSING_CLAIM_COUNT = """
SELECT count(*) AS total,
       count(*) FILTER (
           WHERE EXISTS (
               SELECT 1 FROM archive_evidence_events review
               WHERE review.account_id = claim.account_id
                 AND review.event_type = 'memory.claim_reviewed'
                 AND review.source = 'user.memory_review'
                 AND review.payload->>'action' = 'correct'
                 AND review.payload->>'target_id' = claim.claim_id::text
           )
       ) AS corrected
FROM memory_search_documents document
JOIN memory_claims claim
  ON claim.claim_id = document.item_id
 AND claim.account_id = document.account_id
JOIN archive_evidence_events evidence
  ON evidence.event_id = claim.source_event_id
 AND evidence.account_id = claim.account_id
WHERE document.account_id = $1
  AND document.kind = 'claim'
  AND document.status <> 'retracted'
  AND claim.status <> 'retracted'
  AND NOT EXISTS (
      SELECT 1 FROM memory_vector_documents stored
      WHERE stored.account_id = document.account_id
        AND stored.item_id = document.item_id
        AND stored.embedding_model = $2
        AND stored.embedding_dimensions = $3
  )
"""

_RECHECK_CLAIM = """
SELECT 1
FROM memory_claims claim
JOIN archive_evidence_events evidence
  ON evidence.event_id = claim.source_event_id
 AND evidence.account_id = claim.account_id
WHERE claim.claim_id = $1
  AND claim.account_id = $2
  AND claim.status <> 'retracted'
  AND claim.value = $3
FOR UPDATE OF claim
"""


@dataclass(frozen=True, slots=True)
class MissingVectorCount:
    total: int = 0
    corrected: int = 0
    #: Observational; an unlinked child claim did not resolve at compile time.
    unlinked_child_claims: int = 0


@dataclass(frozen=True, slots=True)
class BackfillReport:
    attempted: int = 0
    embedded: int = 0
    failed: int = 0
    skipped: int = 0
    remaining: int = 0


async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
    await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)


async def _vector_table_exists(connection: asyncpg.Connection) -> bool:
    return bool(
        await connection.fetchval("SELECT to_regclass('memory_vector_documents') IS NOT NULL")
    )


class ClaimVectorBackfill:
    """Count, repair and background-schedule one account's missing claim vectors."""

    def __init__(self, pool: asyncpg.Pool, embedder: MemoryEmbedder) -> None:
        self._pool = pool
        self._embedder = embedder
        self._tasks: set[asyncio.Task[None]] = set()
        self._inflight: set[tuple[str, uuid.UUID]] = set()

    async def count(self, *, account_id: str) -> MissingVectorCount:
        async with self._pool.acquire() as connection, connection.transaction():
            await _scope(connection, account_id)
            if not await _vector_table_exists(connection):
                return MissingVectorCount()
            unlinked = int(
                await connection.fetchval(
                    _UNLINKED_CHILD_CLAIM_COUNT,
                    account_id,
                    list(_CHILD_KEY_PATTERNS),
                    _CHILD_RELATION_VALUES,
                )
            )
            row = await connection.fetchrow(
                _MISSING_CLAIM_COUNT,
                account_id,
                self._embedder.model,
                self._embedder.dimensions,
            )
        if row is None:  # pragma: no cover - count(*) always returns one row
            return MissingVectorCount()
        return MissingVectorCount(
            total=int(row["total"]),
            corrected=int(row["corrected"]),
            unlinked_child_claims=unlinked,
        )

    async def backfill(self, *, account_id: str, limit: int) -> BackfillReport:
        if not 1 <= limit <= 10_000:
            raise ValueError("vector backfill limit must be between 1 and 10000")
        async with self._pool.acquire() as connection, connection.transaction():
            await _scope(connection, account_id)
            if not await _vector_table_exists(connection):
                return BackfillReport()
            rows = await connection.fetch(
                _MISSING_CLAIM_SELECT,
                account_id,
                self._embedder.model,
                self._embedder.dimensions,
                limit,
            )
        embedded = failed = skipped = 0
        for row in rows:
            try:
                outcome = await self._repair_row(account_id, row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed += 1
                logger.warning(
                    "memory vector backfill candidate failed error=%s",
                    type(exc).__name__,
                )
                continue
            if outcome == "embedded":
                embedded += 1
            elif outcome == "failed":
                failed += 1
            else:
                skipped += 1
        remaining = (await self.count(account_id=account_id)).total
        return BackfillReport(
            attempted=len(rows),
            embedded=embedded,
            failed=failed,
            skipped=skipped,
            remaining=remaining,
        )

    async def backfill_accounts(self, *, account_ids: Sequence[str], limit: int) -> BackfillReport:
        accounts = list(dict.fromkeys(account_ids))
        if not accounts:
            return BackfillReport()
        if not 1 <= limit <= 10_000:
            raise ValueError("vector backfill limit must be between 1 and 10000")
        # Fair share per account so one busy account cannot consume the whole
        # round every hour; the next round continues with the same order.
        per_account = max(1, limit // len(accounts))
        budget = limit
        attempted = embedded = failed = skipped = 0
        for account_id in accounts:
            if budget <= 0:
                break
            report = await self.backfill(account_id=account_id, limit=min(per_account, budget))
            budget -= report.attempted
            attempted += report.attempted
            embedded += report.embedded
            failed += report.failed
            skipped += report.skipped
        remaining = sum(
            [(await self.count(account_id=account_id)).total for account_id in accounts]
        )
        return BackfillReport(
            attempted=attempted,
            embedded=embedded,
            failed=failed,
            skipped=skipped,
            remaining=remaining,
        )

    def schedule(self, *, account_id: str, claim_ids: Iterable[uuid.UUID]) -> None:
        for claim_id in claim_ids:
            key = (account_id, claim_id)
            if key in self._inflight:
                continue
            self._inflight.add(key)
            task = asyncio.create_task(
                self._repair_one(account_id, claim_id),
                name="memory-vector-backfill",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            task.add_done_callback(partial(self._finish, key))

    async def wait(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    def _finish(self, key: tuple[str, uuid.UUID], _task: asyncio.Task[None]) -> None:
        self._inflight.discard(key)

    async def _repair_one(self, account_id: str, claim_id: uuid.UUID) -> None:
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                await _scope(connection, account_id)
                if not await _vector_table_exists(connection):
                    return
                row = await connection.fetchrow(
                    _ONE_MISSING_CLAIM,
                    account_id,
                    self._embedder.model,
                    self._embedder.dimensions,
                    claim_id,
                )
            if row is not None:
                await self._repair_row(account_id, row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "memory vector backfill task failed count=1 error=%s",
                type(exc).__name__,
            )

    async def _repair_row(self, account_id: str, row: asyncpg.Record) -> str:
        claim_id = cast(uuid.UUID, row["claim_id"])
        value = str(row["value"])
        title = claim_vector_title(value)
        body = claim_vector_body(
            str(row["subject_key"]),
            str(row["predicate"]),
            value,
        )
        try:
            embedding = await self._embedder.embed(document_embedding_input(title, body))
        except MemoryEmbeddingUnavailableError:
            return "failed"
        async with self._pool.acquire() as connection, connection.transaction():
            await _scope(connection, account_id)
            current = await connection.fetchval(
                _RECHECK_CLAIM,
                claim_id,
                account_id,
                value,
            )
            if current is None:
                # Retracted, corrected again, or its subject's evidence was
                # deleted while the embedding was in flight.
                return "skipped"
            await write_document_vector(
                connection,
                self._embedder,
                item_id=claim_id,
                account_id=account_id,
                source_event_id=str(row["source_event_id"]),
                embedding=embedding,
            )
        return "embedded"


async def count_missing_claim_vectors(
    pool: asyncpg.Pool,
    embedder: MemoryEmbedder,
    *,
    account_id: str,
) -> MissingVectorCount:
    return await ClaimVectorBackfill(pool, embedder).count(account_id=account_id)


async def backfill_missing_claim_vectors(
    pool: asyncpg.Pool,
    embedder: MemoryEmbedder,
    *,
    account_ids: Sequence[str],
    limit: int,
) -> BackfillReport:
    return await ClaimVectorBackfill(pool, embedder).backfill_accounts(
        account_ids=account_ids,
        limit=limit,
    )


class MemoryVectorBackfillWorker:
    """Run the repair at startup, then once per interval."""

    def __init__(
        self,
        backfill: Callable[[int], Awaitable[BackfillReport]],
        *,
        interval_s: float = 3600.0,
        batch_size: int = 200,
    ) -> None:
        if not 10 <= interval_s <= 86_400:
            raise ValueError("memory vector backfill interval must be between 10 and 86400")
        if not 1 <= batch_size <= 1000:
            raise ValueError("memory vector backfill batch must be between 1 and 1000")
        self._backfill = backfill
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="memory-vector-backfill")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def run_once(self) -> BackfillReport | None:
        try:
            report = await self._backfill(self._batch_size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "memory vector backfill round failed error=%s",
                type(exc).__name__,
            )
            return None
        logger.info(
            "memory vector backfill attempted=%d embedded=%d failed=%d skipped=%d remaining=%d",
            report.attempted,
            report.embedded,
            report.failed,
            report.skipped,
            report.remaining,
        )
        return report

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue


def build_memory_vector_backfill_worker(
    backfill: Callable[[int], Awaitable[BackfillReport]],
    *,
    vector_enabled: bool,
    interval_s: float = 3600.0,
    batch_size: int = 200,
) -> MemoryVectorBackfillWorker | None:
    """The worker exists only when the deployment has pgvector and an embedder."""

    if not vector_enabled:
        return None
    return MemoryVectorBackfillWorker(
        backfill,
        interval_s=interval_s,
        batch_size=batch_size,
    )
