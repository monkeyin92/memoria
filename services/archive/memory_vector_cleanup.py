"""Remove a claim's embedding when a review retracts the claim.

The search row stays with ``status = 'retracted'``; confirming it again
reactivates the row, and a full projection rebuild can recreate the vector.
"""

from __future__ import annotations

import uuid

import asyncpg

from services.archive.memory_domain import MemoryStatus


async def forget_claim_vectors(
    connection: asyncpg.Connection,
    status: MemoryStatus,
    claim_id: uuid.UUID,
    account_id: str,
    vector_enabled: bool,
) -> None:
    if status != "retracted" or not vector_enabled:
        return
    await connection.execute(
        "DELETE FROM memory_vector_documents WHERE item_id = $1 AND account_id = $2",
        claim_id,
        account_id,
    )
