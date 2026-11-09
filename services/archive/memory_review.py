"""PostgreSQL review projection mixin for the memory catalog.

``PostgresMemoryReviewMixin`` is composed into ``PostgresMemoryCatalog``. The
catalog owns the pool, scope setter, evidence writer and episode refresh; this
module owns the human review queue and its claim-review projection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import asyncpg

from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.memory_domain import (
    ConflictState,
    DomainCategory,
    MemoryCategory,
    MemoryClaimReview,
    MemoryStatus,
    ReviewedClaim,
    ReviewQueueItem,
    subject_lineage_predicates,
)
from services.archive.memory_sql import _bind_dollar
from services.archive.memory_vector_cleanup import (
    claim_vector_body,
    forget_claim_vectors,
    has_correction_review,
    restore_claim_vector,
)
from services.archive.memory_write_policy import (
    SINGLE_VALUE_PREDICATES as _SINGLE_VALUE_PREDICATES,
)
from services.archive.memory_write_policy import (
    is_policy_confirmation_event,
)
from services.common.redaction import redact_pii

_REVIEW_STATUS: dict[str, MemoryStatus] = {
    "confirm": "confirmed",
    "dispute": "disputed",
    "retract": "retracted",
    "correct": "confirmed",
}
_PROJECTION_REVIEW_STATUS: dict[str, MemoryStatus] = {
    **_REVIEW_STATUS,
    "correct": "retracted",
}
_SOURCE_STATUS_TABLES = (
    "person_entities",
    "person_aliases",
    "relationships",
    "timeline_entries",
    "episode_evidence",
    "knowledge_items",
)

# Scoped-review cascade guards.  Rows whose identity merged several speakers
# are skipped instead of being rewritten through one of their sources: only
# ``person_entities`` (via ``person_aliases``) and ``timeline_entries`` /
# ``episode_evidence`` (via the episode) can carry more than their own source
# event; the remaining tables above are single-source rows.  ``?`` marks the
# subject placeholder, rebound to a ``$n`` index by the caller.
_SCOPED_CASCADE_GUARDS: dict[str, str] = {
    "person_entities": (
        "NOT EXISTS (SELECT 1 FROM person_aliases alias"
        " LEFT JOIN {evidence} lineage ON lineage.event_id = alias.source_event_id"
        " AND lineage.account_id = alias.account_id"
        " WHERE alias.person_id = person_entities.person_id"
        " AND alias.account_id = person_entities.account_id"
        " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
    ),
    "timeline_entries": (
        "NOT EXISTS (SELECT 1 FROM episode_evidence merged"
        " LEFT JOIN {evidence} lineage ON lineage.event_id = merged.source_event_id"
        " AND lineage.account_id = merged.account_id"
        " WHERE merged.episode_id = timeline_entries.episode_id"
        " AND merged.account_id = timeline_entries.account_id"
        " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
    ),
    "episode_evidence": (
        "NOT EXISTS (SELECT 1 FROM episode_evidence merged"
        " LEFT JOIN {evidence} lineage ON lineage.event_id = merged.source_event_id"
        " AND lineage.account_id = merged.account_id"
        " WHERE merged.episode_id = episode_evidence.episode_id"
        " AND merged.account_id = episode_evidence.account_id"
        " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
    ),
}

_SCOPED_DOCUMENT_CASCADE_GUARD = (
    "AND NOT EXISTS (SELECT 1 FROM memory_search_document_sources merged"
    " LEFT JOIN archive_evidence_events lineage"
    " ON lineage.event_id = merged.source_event_id"
    " AND lineage.account_id = merged.account_id"
    " WHERE merged.document_id = memory_search_documents.document_id"
    " AND merged.account_id = memory_search_documents.account_id"
    " AND (lineage.subject_id IS NULL OR lineage.subject_id <> ?))"
)



if TYPE_CHECKING:
    from services.archive.memory_domain import MemoryEmbedder


class PostgresMemoryReviewMixin:
    """Read the review queue and apply one reviewed claim."""

    if TYPE_CHECKING:
        # Shared state owned by PostgresMemoryCatalog.
        _embedder: MemoryEmbedder | None
        _fail_on_embedding_error: bool
        _vector_enabled: bool

        async def _ready_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _scope(connection: asyncpg.Connection, account_id: str) -> None: ...

        @staticmethod
        async def _insert_evidence(
            connection: asyncpg.Connection,
            event: EvidenceEvent,
            *,
            if_absent: bool = False,
        ) -> None: ...

        async def _refresh_claim_conflicts(
            self,
            connection: asyncpg.Connection,
            *,
            account_id: str,
            subject_key: str,
            predicate: str,
            subject_id: str | None = None,
            unclaimed_only: bool = False,
        ) -> None: ...

        async def _refresh_episode_projection(
            self,
            connection: asyncpg.Connection,
            *,
            episode_id: uuid.UUID,
            account_id: str | None = None,
            subject_id: str | None = None,
        ) -> None: ...

    async def review_queue(
        self,
        *,
        account_id: str,
        subject_id: str | None = None,
    ) -> tuple[ReviewQueueItem, ...]:
        if not account_id.strip():
            raise ValueError("review queue requires account_id")
        if subject_id is not None and not subject_id.strip():
            raise ValueError("review queue subject_id must not be blank")
        parameters: list[Any] = [account_id]
        clauses = [
            "claim.account_id = $1",
            "claim.status IN ('candidate', 'disputed')",
        ]
        # The conflict probe reads memory_claims a second time.  Under an
        # explicit scope it must not compare this subject's values against
        # another subject's rows: the probed claim carries the same lineage
        # contract as the read path (account-consistent own evidence plus every
        # source its projection merged).
        conflict_scope = ""
        if subject_id is not None:
            conflict_scope = "".join(
                f" AND {lineage}"
                for lineage in subject_lineage_predicates(
                    bind=_bind_dollar(parameters),
                    subject_id=subject_id,
                    account_column="other.account_id",
                    single_source_column="other.source_event_id",
                    evidence_table="archive_evidence_events",
                    document_source_item=(
                        "claim",
                        "other.claim_id",
                        "other.account_id",
                    ),
                )
            )
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_dollar(parameters),
                    subject_id=subject_id,
                    account_column="claim.account_id",
                    single_source_column="claim.source_event_id",
                    evidence_table="archive_evidence_events",
                    document_source_item=(
                        "claim",
                        "claim.claim_id",
                        "claim.account_id",
                    ),
                )
            )
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                f"""
                SELECT claim.*,
                       EXISTS (
                           SELECT 1 FROM memory_claims other
                           WHERE other.account_id = claim.account_id
                             AND other.subject_key = claim.subject_key
                             AND other.predicate = claim.predicate
                             AND other.value != claim.value
                             AND other.status != 'retracted'
                           {conflict_scope}
                       ) AS has_conflict
                FROM memory_claims claim
                WHERE {" AND ".join(clauses)}
                ORDER BY claim.valid_at, claim.claim_id
                """,
                *parameters,
            )
        return tuple(
            ReviewQueueItem(
                item_id=str(row["claim_id"]),
                kind="claim",
                category=cast(MemoryCategory, row["category"]),
                value=str(row["value"]),
                status=cast(MemoryStatus, row["status"]),
                reason=(
                    "conflicting_values"
                    if str(row["predicate"]) in _SINGLE_VALUE_PREDICATES
                    and bool(row["has_conflict"])
                    else "pending_confirmation"
                ),
                source_event_id=str(row["source_event_id"]),
                memory_kind="semantic",
                domain_category=cast(DomainCategory, row["domain_category"]),
                conflict_state=cast(ConflictState, row["conflict_state"]),
            )
            for row in rows
        )

    async def review(self, command: MemoryClaimReview) -> ReviewedClaim:
        try:
            claim_id = uuid.UUID(command.claim_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(command.claim_id) from exc
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, command.account_id)
            parameters: list[Any] = [claim_id, command.account_id]
            clauses = ["claim.claim_id = $1", "claim.account_id = $2"]
            if command.subject_id is not None:
                # A scoped reviewer may only reach claims fed by its own
                # subject's evidence; the miss below is the same not-found a
                # nonexistent id returns, so ids stay unguessable.
                clauses.extend(
                    subject_lineage_predicates(
                        bind=_bind_dollar(parameters),
                        subject_id=command.subject_id,
                        account_column="claim.account_id",
                        single_source_column="claim.source_event_id",
                        evidence_table="archive_evidence_events",
                        document_source_item=(
                            "claim",
                            "claim.claim_id",
                            "claim.account_id",
                        ),
                    )
                )
            row = await connection.fetchrow(
                f"""
                SELECT claim.*,
                       (
                           SELECT lineage.subject_id
                           FROM archive_evidence_events lineage
                           WHERE lineage.event_id = claim.source_event_id
                             AND lineage.account_id = claim.account_id
                       ) AS source_subject_id
                FROM memory_claims claim
                WHERE {" AND ".join(clauses)}
                FOR UPDATE
                """,
                *parameters,
            )
            if row is None:
                raise EvidenceNotFoundError(command.claim_id)
            value = (command.corrected_value or str(row["value"])).strip()
            status = _REVIEW_STATUS[command.action]
            review_event = EvidenceEvent(
                event_id=str(uuid.uuid4()),
                account_id=command.account_id,
                event_type="memory.claim_reviewed",
                occurred_at=datetime.now(UTC),
                speaker_class="system",
                source="user.memory_review",
                payload={
                    "target_id": command.claim_id,
                    "action": command.action,
                    **({"corrected_value": value} if command.action == "correct" else {}),
                },
                # The review inherits the reviewed claim's own speaker instead
                # of inventing one; an unclaimed source stays unclaimed.
                subject_id=cast(str | None, row["source_subject_id"]),
            )
            await self._insert_evidence(connection, review_event)
            await self._write_review_projection(
                connection,
                account_id=command.account_id,
                claim_id=claim_id,
                status=status,
                projection_status=_PROJECTION_REVIEW_STATUS[command.action],
                title=value[:80],
                value=value,
                review_event_id=review_event.event_id,
                subject_id=command.subject_id,
                restore_vector=command.action == "confirm",
                replace_vector=command.action == "correct",
            )
        return ReviewedClaim(
            claim_id=command.claim_id,
            status=status,
            value=value,
            review_event_id=review_event.event_id,
        )

    async def _replay_review_event(
        self,
        connection: asyncpg.Connection,
        event: EvidenceEvent,
    ) -> None:
        """Rebuild one review; a correction re-embeds and an older one never rewinds."""
        target_id = str(event.payload.get("target_id", ""))
        action = str(event.payload.get("action", ""))
        try:
            claim_id = uuid.UUID(target_id)
            status = _REVIEW_STATUS[action]
        except (KeyError, ValueError) as exc:
            raise ValueError("invalid memory review evidence") from exc
        row = await connection.fetchrow(
            """
            SELECT value, source_event_id, review_event_id FROM memory_claims
            WHERE claim_id = $1 AND account_id = $2
            FOR UPDATE
            """,
            claim_id,
            event.account_id,
        )
        if row is None:
            raise EvidenceNotFoundError(target_id)
        if is_policy_confirmation_event(event) and str(row["source_event_id"]) != str(
            event.payload.get("source_event_id")
        ):
            raise ValueError("memory policy confirmation source does not match its claim")
        value = str(row["value"])
        if action == "correct":
            value = str(event.payload.get("corrected_value", "")).strip()
            if not value:
                raise ValueError("corrected memory value must not be blank")
            applied = await connection.fetchrow(
                "SELECT occurred_at FROM archive_evidence_events"
                " WHERE event_id = $1 AND account_id = $2",
                row["review_event_id"],
                event.account_id,
            )
            if applied is not None and (
                cast(datetime, applied["occurred_at"]),
                str(row["review_event_id"]),
            ) > (event.occurred_at, event.event_id):
                return
        await self._write_review_projection(
            connection,
            account_id=event.account_id,
            claim_id=claim_id,
            status=status,
            projection_status=_PROJECTION_REVIEW_STATUS[action],
            title=value[:80],
            value=value,
            review_event_id=event.event_id,
            subject_id=event.subject_id,
            replace_vector=action == "correct",
        )

    async def _write_review_projection(
        self,
        connection: asyncpg.Connection,
        *,
        account_id: str,
        claim_id: uuid.UUID,
        status: MemoryStatus,
        projection_status: MemoryStatus,
        title: str,
        value: str,
        review_event_id: str,
        subject_id: str | None = None,
        restore_vector: bool = False,
        replace_vector: bool = False,
    ) -> None:
        source = await connection.fetchrow(
            """
            SELECT source_event_id, subject_key, predicate FROM memory_claims
            WHERE claim_id = $1 AND account_id = $2
            """,
            claim_id,
            account_id,
        )
        if source is None:
            raise EvidenceNotFoundError(str(claim_id))
        source_event_id = str(source["source_event_id"])
        subject_key = str(source["subject_key"])
        predicate = str(source["predicate"])
        source_event = await connection.fetchrow(
            "SELECT payload->>'text' AS text FROM archive_evidence_events"
            " WHERE event_id = $1 AND account_id = $2",
            source_event_id,
            account_id,
        )
        source_text = redact_pii(str(source_event["text"] or "") if source_event else "")[:240]
        corrected = replace_vector or await has_correction_review(connection, claim_id, account_id)
        # Keep the replaced wording out of the body, lexical row and vector.
        contextual_body = claim_vector_body(
            subject_key,
            predicate,
            value,
            context=None if corrected else source_text,
        )
        episode_ids = await connection.fetch(
            """
            SELECT episode_id FROM episode_evidence
            WHERE account_id = $1 AND source_event_id = $2
            """,
            account_id,
            source_event_id,
        )
        await connection.execute(
            """
            UPDATE memory_claims
            SET status = $1, value = $2, review_event_id = $3,
                stability = CASE
                    WHEN $1 = 'confirmed' THEN GREATEST(stability, 0.9)
                    WHEN $1 = 'retracted' THEN 0
                    ELSE stability
                END
            WHERE claim_id = $4 AND account_id = $5
            """,
            status,
            value,
            review_event_id,
            claim_id,
            account_id,
        )
        for table in _SOURCE_STATUS_TABLES:
            guard = ""
            if subject_id is not None:
                template = _SCOPED_CASCADE_GUARDS.get(table)
                if template is not None:
                    guard = " AND " + template.format(evidence="archive_evidence_events")
            await connection.execute(
                f"UPDATE {table} SET status = $1"
                " WHERE account_id = $2 AND source_event_id = $3"
                f"{guard.replace('?', '$4')}",
                projection_status,
                account_id,
                source_event_id,
                *([subject_id] if guard else []),
            )
        await connection.execute(
            f"""
            UPDATE memory_search_documents
            SET status = $1
            WHERE kind NOT IN ('claim', 'episode') AND account_id = $2
              AND document_id IN (
                  SELECT document_id FROM memory_search_document_sources
                  WHERE account_id = $2 AND source_event_id = $3
              )
              {_SCOPED_DOCUMENT_CASCADE_GUARD.replace("?", "$4") if subject_id is not None else ""}
            """,
            projection_status,
            account_id,
            source_event_id,
            *([subject_id] if subject_id is not None else []),
        )
        await connection.execute(
            """
            UPDATE memory_search_documents
            SET status = $1, title = $2, body = $3,
                stability = CASE
                    WHEN $1 = 'confirmed' THEN GREATEST(stability, 0.9)
                    WHEN $1 = 'retracted' THEN 0
                    ELSE stability
                END
            WHERE kind = 'claim' AND item_id = $4 AND account_id = $5
            """,
            status,
            title,
            contextual_body,
            claim_id,
            account_id,
        )
        for episode in episode_ids:
            await self._refresh_episode_projection(
                connection,
                episode_id=cast(uuid.UUID, episode["episode_id"]),
                account_id=account_id,
                subject_id=subject_id,
            )
        await self._refresh_claim_conflicts(
            connection,
            account_id=account_id,
            subject_key=subject_key,
            predicate=predicate,
            subject_id=subject_id,
        )
        await forget_claim_vectors(connection, status, claim_id, account_id, self._vector_enabled, review_event_id)
        if replace_vector or (restore_vector and status == "confirmed"):
            # A correction replaces every model's copy; a confirmation only
            # restores the one a retraction removed.
            await restore_claim_vector(
                connection,
                self._embedder,
                claim_id=claim_id,
                account_id=account_id,
                source_event_id=source_event_id,
                title=title,
                body=contextual_body,
                vector_enabled=self._vector_enabled,
                fail_on_embedding_error=self._fail_on_embedding_error,
                replace_vectors=replace_vector,
            )
