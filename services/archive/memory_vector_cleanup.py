"""Keep a claim's embedding in step with its review state.

Retracting a claim deletes its vector and leaves the search row with
``status = 'retracted'``.  Confirming the claim again restores exactly that
row's vector, so recall does not wait for a full projection rebuild.
"""

from __future__ import annotations

import json
import uuid
from hashlib import sha256

import asyncpg

from services.archive.memory_domain import (
    MemoryEmbedder,
    MemoryEmbeddingUnavailableError,
    MemoryStatus,
)


async def ensure_vector_index(
    connection: asyncpg.Connection,
    embedder: MemoryEmbedder,
) -> None:
    digest = sha256(f"{embedder.model}:{embedder.dimensions}".encode()).hexdigest()[:12]
    index_name = f"idx_memory_vector_hnsw_{digest}"
    model = embedder.model.replace("'", "''")
    dimensions = embedder.dimensions
    await connection.execute(
        f"""
        CREATE INDEX IF NOT EXISTS {index_name}
        ON memory_vector_documents
        USING hnsw ((embedding::vector({dimensions})) vector_cosine_ops)
        WHERE embedding_model = '{model}'
          AND embedding_dimensions = {dimensions}
        """
    )


async def forget_claim_vectors(
    connection: asyncpg.Connection,
    status: MemoryStatus,
    claim_id: uuid.UUID,
    account_id: str,
    vector_enabled: bool,
    review_event_id: str,
) -> None:
    if status != "retracted" or not vector_enabled:
        return
    latest_review = await connection.fetchval(
        """
        SELECT event_id FROM archive_evidence_events
        WHERE account_id = $1 AND event_type = 'memory.claim_reviewed'
          AND source = 'user.memory_review'
          AND payload->>'target_id' = $2
        ORDER BY occurred_at DESC, event_id DESC
        LIMIT 1
        """,
        account_id,
        str(claim_id),
    )
    if latest_review != review_event_id:
        # A later review already restored this claim; replaying the older
        # retraction must not delete the vector that confirmation recreated.
        return
    await connection.execute(
        "DELETE FROM memory_vector_documents WHERE item_id = $1 AND account_id = $2",
        claim_id,
        account_id,
    )


async def restore_claim_vector(
    connection: asyncpg.Connection,
    embedder: MemoryEmbedder | None,
    *,
    claim_id: uuid.UUID,
    account_id: str,
    source_event_id: str,
    title: str,
    body: str,
    vector_enabled: bool,
    fail_on_embedding_error: bool,
) -> None:
    """Recreate one confirmed claim's vector from its search document text.

    The embedding input and upsert match the compile-time write.  An embedding
    failure removes any stale vector for the current model and dimensions; the
    caller decides whether that failure aborts the review.
    """
    if not vector_enabled or embedder is None:
        return
    try:
        embedding = await embedder.embed(" ".join(part for part in (title, body) if part))
    except MemoryEmbeddingUnavailableError:
        await connection.execute(
            """
            DELETE FROM memory_vector_documents
            WHERE item_id = $1 AND account_id = $2
              AND embedding_model = $3 AND embedding_dimensions = $4
            """,
            claim_id,
            account_id,
            embedder.model,
            embedder.dimensions,
        )
        if fail_on_embedding_error:
            raise
        return
    await connection.execute(
        """
        INSERT INTO memory_vector_documents (
            item_id, account_id, embedding_model, embedding_dimensions,
            embedding, source_event_id
        ) VALUES ($1, $2, $3, $4, $5::vector, $6)
        ON CONFLICT (item_id, embedding_model, embedding_dimensions) DO UPDATE
        SET embedding = EXCLUDED.embedding,
            source_event_id = EXCLUDED.source_event_id,
            created_at = now()
        """,
        claim_id,
        account_id,
        embedder.model,
        embedder.dimensions,
        json.dumps(embedding, separators=(",", ":")),
        source_event_id,
    )
