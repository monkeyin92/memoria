"""Keep a claim's embedding in step with its review state.

Retracting a claim deletes its vector and leaves the search row with
``status = 'retracted'``.  Confirming the claim again restores exactly that
row's vector, so recall does not wait for a full projection rebuild.
Correcting a claim deletes every stored copy of the old wording and embeds the
corrected search text in the same transaction, so recall never serves the text
the parent replaced.
"""

from __future__ import annotations

import json
import math
import uuid
from hashlib import sha256

import asyncpg
import httpx

from services.archive.memory_domain import (
    MemoryEmbedder,
    MemoryEmbeddingUnavailableError,
    MemoryStatus,
)


def claim_vector_body(
    subject_key: str,
    predicate: str,
    value: str,
    *,
    context: str | None = None,
) -> str:
    """The search body #330's correction writes for one claim.

    ``context`` is the captured sentence and is only passed by the compile or
    uncorrected-review paths.  A corrected claim, and every repair, must leave
    it out: it usually states the wording the parent replaced.
    """

    body = f"subject:{subject_key} predicate:{predicate} value:{value}"
    if context:
        body += f" context:{context}"
    return body[:8000]


def claim_vector_title(value: str) -> str:
    return value[:80]


def document_embedding_input(title: str, body: str) -> str:
    return " ".join(part for part in (title, body) if part)


class QwenMemoryEmbedder:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str,
        dimensions: int,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("memory embedding endpoint must use HTTP(S)")
        if not api_key or not model.strip() or timeout_s <= 0 or not 1 <= dimensions <= 2000:
            raise ValueError(
                "memory embedding API key, model, dimensions 1..2000 and timeout are required"
            )
        self._endpoint = endpoint
        self._api_key = api_key
        self.model = model.strip()
        self.dimensions = dimensions
        self._timeout_s = timeout_s
        self._transport = transport

    async def embed(self, text: str) -> tuple[float, ...]:
        if not text.strip():
            raise ValueError("memory embedding text must not be blank")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._endpoint,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self.model,
                        "input": text,
                        "encoding_format": "float",
                        "dimensions": self.dimensions,
                    },
                )
                response.raise_for_status()
                values = response.json()["data"][0]["embedding"]
            embedding = tuple(float(value) for value in values)
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise MemoryEmbeddingUnavailableError(
                "memory embedding service returned an invalid response"
            ) from exc
        if len(embedding) != self.dimensions or any(
            not math.isfinite(value) for value in embedding
        ):
            raise MemoryEmbeddingUnavailableError(
                "memory embedding service returned an invalid vector dimension"
            )
        return embedding


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


async def has_correction_review(
    connection: asyncpg.Connection,
    claim_id: uuid.UUID,
    account_id: str,
) -> bool:
    """Whether a user correction already superseded this claim's captured text.

    Every review write after that correction builds its search body from the
    corrected value alone: the captured sentence usually states the wording
    the user replaced, so it must stay out of the body, the lexical row and
    the embedding.  A full projection rebuild compiles the capture event
    before it replays the review, so compile also skips embedding a claim
    whose correction is already recorded.
    """

    return bool(
        await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM archive_evidence_events
                WHERE account_id = $1 AND event_type = 'memory.claim_reviewed'
                  AND source = 'user.memory_review'
                  AND payload->>'action' = 'correct'
                  AND payload->>'target_id' = $2
            )
            """,
            account_id,
            str(claim_id),
        )
    )


async def write_document_vector(
    connection: asyncpg.Connection,
    embedder: MemoryEmbedder,
    *,
    item_id: uuid.UUID,
    account_id: str,
    source_event_id: str,
    embedding: tuple[float, ...],
) -> None:
    """Upsert one already-computed embedding on the caller's connection."""

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
        item_id,
        account_id,
        embedder.model,
        embedder.dimensions,
        json.dumps(embedding, separators=(",", ":")),
        source_event_id,
    )


async def upsert_document_vector(
    connection: asyncpg.Connection,
    embedder: MemoryEmbedder,
    *,
    item_id: uuid.UUID,
    account_id: str,
    source_event_id: str,
    title: str,
    body: str,
    fail_on_embedding_error: bool,
) -> bool:
    """Embed one search document and upsert its vector, exactly as compile does.

    The embedding input is the search document's title and contextual body.
    An unavailable embedding service removes the current model's row; the
    caller decides whether that failure aborts the surrounding projection.
    Returns whether an embedding was written.
    """

    try:
        embedding = await embedder.embed(document_embedding_input(title, body))
    except MemoryEmbeddingUnavailableError:
        await connection.execute(
            """
            DELETE FROM memory_vector_documents
            WHERE item_id = $1 AND account_id = $2
              AND embedding_model = $3 AND embedding_dimensions = $4
            """,
            item_id,
            account_id,
            embedder.model,
            embedder.dimensions,
        )
        if fail_on_embedding_error:
            raise
        return False
    await write_document_vector(
        connection,
        embedder,
        item_id=item_id,
        account_id=account_id,
        source_event_id=source_event_id,
        embedding=embedding,
    )
    return True


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
    replace_vectors: bool = False,
) -> None:
    """Recreate one claim's vector from its search document text.

    ``replace_vectors`` first deletes every stored copy, including rows from
    another embedding model or dimensionality, because a correction makes all
    of them stale.  Without it only the current model's row is replaced, which
    is what a confirmation needs.  A missing embedder still deletes when
    ``replace_vectors`` is set: the old wording must not stay recallable.
    """

    if not vector_enabled:
        # initialize() only enables vectors when the table exists, so a
        # non-vector deployment has nothing to delete.
        return
    if replace_vectors:
        await connection.execute(
            "DELETE FROM memory_vector_documents WHERE item_id = $1 AND account_id = $2",
            claim_id,
            account_id,
        )
    if embedder is None:
        return
    await upsert_document_vector(
        connection,
        embedder,
        item_id=claim_id,
        account_id=account_id,
        source_event_id=source_event_id,
        title=title,
        body=body,
        fail_on_embedding_error=fail_on_embedding_error,
    )
