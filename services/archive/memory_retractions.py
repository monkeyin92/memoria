"""Read the retracted claims a guardian may still restore.

A retraction is a review event, so ``retracted_at`` is that event's own time.
The claim row keeps the value and category the guardian list needs; the
original conversation text stays in the evidence ledger and is never read
here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from services.archive.memory_domain import (
    DomainCategory,
    MemoryCatalogPort,
    subject_lineage_predicates,
)
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog


@dataclass(frozen=True, slots=True)
class RetractedClaim:
    claim_id: str
    value: str
    domain_category: DomainCategory
    retracted_at: datetime

    @property
    def title(self) -> str:
        return self.value[:80]


def _bind(parameters: list[Any]) -> Callable[[str], str]:
    def bind(value: str) -> str:
        parameters.append(value)
        return f"${len(parameters)}"

    return bind


async def list_retracted_claims(
    catalog: MemoryCatalogPort,
    *,
    account_id: str,
    subject_id: str,
    limit: int = 20,
) -> tuple[RetractedClaim, ...]:
    """This subject's retracted claims, newest retraction first.

    The read runs on the catalog's own pool under the same account scope as
    every other catalog query, so RLS and the explicit subject lineage both
    apply before ordering and limiting.
    """

    if not account_id.strip() or not subject_id.strip():
        raise ValueError("retracted claims require account_id and subject_id")
    if not 1 <= limit <= 50:
        raise ValueError("retracted claims limit must be between 1 and 50")
    parameters: list[Any] = [account_id]
    clauses = ["claim.account_id = $1", "claim.status = 'retracted'"]
    clauses.extend(
        subject_lineage_predicates(
            bind=_bind(parameters),
            subject_id=subject_id,
            account_column="claim.account_id",
            single_source_column="claim.source_event_id",
            evidence_table="archive_evidence_events",
        )
    )
    parameters.append(limit)
    pool = await cast(PostgresMemoryCatalog, catalog)._ready_pool()
    async with pool.acquire() as connection, connection.transaction():
        await PostgresMemoryCatalog._scope(connection, account_id)
        rows = await connection.fetch(
            f"""
            SELECT claim.claim_id, claim.value, claim.domain_category,
                   review.occurred_at AS retracted_at
            FROM memory_claims claim
            JOIN archive_evidence_events review
              ON review.event_id = claim.review_event_id
             AND review.account_id = claim.account_id
            WHERE {" AND ".join(clauses)}
              AND review.event_type = 'memory.claim_reviewed'
              AND review.payload->>'action' IN ('retract', 'correct')
            ORDER BY review.occurred_at DESC, claim.claim_id DESC
            LIMIT ${len(parameters)}
            """,
            *parameters,
        )
    return tuple(
        RetractedClaim(
            claim_id=str(row["claim_id"]),
            value=str(row["value"]),
            domain_category=cast(DomainCategory, row["domain_category"]),
            retracted_at=cast(datetime, row["retracted_at"]),
        )
        for row in rows
    )
