"""PostgreSQL memory write mixin for the memory catalog.

``PostgresMemoryWriteMixin`` is composed into ``PostgresMemoryCatalog``. The
catalog owns the pool, outbox compilation and the review/search/timeline seams;
this module owns the extraction projection: claims, conflicts, episodes,
search documents and evidence writes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, cast

import asyncpg

from services.archive.claim_subjects import (
    BoundChildSubjectIds,
    bound_children,
    claim_about_subject,
    confirmed_evidence_subject,
)
from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.episode_consolidator import (
    EpisodeCandidate,
    EpisodeConsolidator,
    ExistingEpisode,
)
from services.archive.memory_domain import (
    ConflictState,
    DomainCategory,
    MemoryEmbedder,
    MemoryExtraction,
    MemoryExtractor,
    MemoryKind,
    MemorySensitivity,
    MemoryStatus,
    subject_lineage_predicates,
)
from services.archive.memory_sql import _bind_dollar
from services.archive.memory_vector_cleanup import (
    claim_vector_body,
    has_correction_review,
    upsert_document_vector,
)
from services.archive.memory_write_policy import (
    SINGLE_VALUE_PREDICATES,
    MemoryWriteDecision,
)
from services.common.evidence_policy import contribution_for
from services.common.redaction import redact_pii

_SINGLE_VALUE_PREDICATES = SINGLE_VALUE_PREDICATES


_SENSITIVITY_ORDER: dict[MemorySensitivity, int] = {
    "public": 0,
    "personal": 1,
    "sensitive": 2,
    "highly_sensitive": 3,
}


def _stable_uuid(kind: str, *values: object) -> uuid.UUID:
    key = ":".join(str(value) for value in values)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{kind}:{key}")


def _sensitivity(value: str) -> MemorySensitivity:
    normalized = value.strip().casefold()
    if normalized == "public":
        return "public"
    if normalized in {"health", "finance", "legal", "biometric", "highly_sensitive"}:
        return "highly_sensitive"
    if normalized in {"relationship", "private", "sensitive"}:
        return "sensitive"
    return "personal"


class PostgresMemoryWriteMixin:
    """Project one extracted memory into claims, conflicts, episodes and documents."""

    if TYPE_CHECKING:
        # Shared state owned by PostgresMemoryCatalog.
        _extractor: MemoryExtractor
        _embedder: MemoryEmbedder | None
        _episode_consolidator: EpisodeConsolidator
        _bound_child_subject_ids: BoundChildSubjectIds | None
        _fail_on_embedding_error: bool
        _vector_enabled: bool

        # Sibling-mixin member consumed by this mixin.
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
        ) -> None: ...


    async def _write_extraction(
        self,
        connection: asyncpg.Connection,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
    ) -> None:
        evidence_subject_id = confirmed_evidence_subject(event)
        bound_child_ids = await bound_children(self._bound_child_subject_ids, event.account_id)
        person_ids: dict[str, uuid.UUID] = {}
        for person in extraction.people:
            person_id = _stable_uuid("person", event.account_id, person.canonical_key)
            person_ids[person.canonical_key] = person_id
            await connection.execute(
                """
                INSERT INTO person_entities (
                    person_id, account_id, canonical_key, display_name,
                    relationship_to_owner, source_event_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (account_id, canonical_key) DO NOTHING
                """,
                person_id,
                event.account_id,
                person.canonical_key,
                person.display_name,
                person.relationship_to_owner,
                event.event_id,
                event.occurred_at,
            )
            for alias in person.aliases:
                await connection.execute(
                    """
                    INSERT INTO person_aliases (
                        person_id, account_id, alias, source_event_id
                    ) VALUES ($1, $2, $3, $4)
                    ON CONFLICT DO NOTHING
                    """,
                    person_id,
                    event.account_id,
                    alias,
                    event.event_id,
                )
            # Aliases live only in person_aliases, which no read path searches, so a confirmed
            # nickname ("阿梅") could never recall the person. The document carries the relationship
            # and every alias and moves with the event's review lifecycle, so confirmed-only recall
            # still gates unconfirmed people.
            await self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=person_id,
                kind="person",
                memory_kind="relationship",
                title=person.display_name,
                body=" ".join(
                    dict.fromkeys(
                        part
                        for part in (
                            person.relationship_to_owner,
                            person.display_name,
                            *person.aliases,
                        )
                        if part
                    )
                )[:8000],
                domain_category=(
                    extraction.claims[0].domain_category if extraction.claims else "daily_life"
                ),
                entity_ids=(person_id,),
                source_event_ids=(event.event_id,),
                valid_from=None,
                valid_to=None,
                occurred_at=event.occurred_at,
                observed_at=event.occurred_at,
                stability=0.7,
                salience=0.6,
                sensitivity="personal",
                conflict_state="none",
            )

        for index, relationship in enumerate(extraction.relationships):
            person_id = person_ids.get(relationship.person_key) or _stable_uuid("person", event.account_id, relationship.person_key)
            await connection.execute(
                """
                INSERT INTO relationships (
                    relationship_id, account_id, person_id, relationship_type,
                    source_event_id, valid_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT DO NOTHING
                """,
                _stable_uuid("relationship", event.event_id, index),
                event.account_id,
                person_id,
                relationship.relationship_type,
                event.event_id,
                event.occurred_at,
            )

        for index, claim in enumerate(extraction.claims):
            claim_id = _stable_uuid("claim", event.event_id, index)
            confidence = min(1.0, claim.confidence * contribution_for(event).factor)
            valid_from = claim.valid_from or event.occurred_at
            entity_keys = set(claim.entity_keys)
            if claim.subject_key != "self":
                entity_keys.add(claim.subject_key)
            entity_ids = tuple(
                person_ids.get(key) or _stable_uuid("person", event.account_id, key)
                for key in sorted(entity_keys)
            )
            sensitivity = _sensitivity(claim.sensitive_domain)
            claim_subject_ids = await claim_about_subject(connection, account_id=event.account_id, evidence_subject_id=evidence_subject_id, subject_key=claim.subject_key, bound_child_ids=bound_child_ids)
            await connection.execute(
                """
                INSERT INTO memory_claims (
                    claim_id, account_id, category, domain_category, memory_kind,
                    subject_key, predicate, value, confidence, sensitive_domain,
                    entity_ids, extractor_version, source_event_id, valid_at,
                    valid_from, valid_to, observed_at, stability, salience,
                    sensitivity, conflict_state
                ) VALUES (
                    $1, $2, $3, $4, 'semantic', $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, $19, 'none'
                )
                ON CONFLICT DO NOTHING
                """,
                claim_id,
                event.account_id,
                claim.domain_category,
                claim.domain_category,
                claim.subject_key,
                claim.predicate,
                claim.value,
                confidence,
                claim.sensitive_domain,
                list(entity_ids),
                extraction.extractor_version or self._extractor.version,
                event.event_id,
                valid_from,
                valid_from,
                claim.valid_to,
                event.occurred_at,
                confidence,
                claim.salience,
                sensitivity,
            )
            await connection.executemany("INSERT INTO memory_claim_subjects (account_id, claim_id, subject_id) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING", [(event.account_id, claim_id, subject_id) for subject_id in claim_subject_ids])
            await self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=claim_id,
                kind="claim",
                memory_kind="semantic",
                title=claim.value[:80],
                body=claim_vector_body(
                    claim.subject_key,
                    claim.predicate,
                    claim.value,
                    context=redact_pii(str(event.payload.get("text") or ""))[:240],
                ),
                domain_category=claim.domain_category,
                entity_ids=entity_ids,
                source_event_ids=(event.event_id,),
                valid_from=valid_from,
                valid_to=claim.valid_to,
                occurred_at=event.occurred_at,
                observed_at=event.occurred_at,
                stability=confidence,
                salience=claim.salience,
                sensitivity=sensitivity,
                conflict_state="none",
            )
            # A named subject folds only its own lineage.  A blank subject
            # folds only other unclaimed rows: passing no subject here would
            # mark the account owner's claims as conflicts too.
            claim_subject_id = confirmed_evidence_subject(event)
            await self._refresh_claim_conflicts(
                connection,
                account_id=event.account_id,
                subject_key=claim.subject_key,
                predicate=claim.predicate,
                subject_id=claim_subject_id,
                unclaimed_only=claim_subject_id is None,
            )

        for index, timeline in enumerate(extraction.timeline):
            entity_ids = tuple(
                person_ids.get(key) or _stable_uuid("person", event.account_id, key)
                for key in sorted(set(timeline.participant_keys))
            )
            candidate = EpisodeCandidate(
                account_id=event.account_id,
                source_event_id=event.event_id,
                session_id=event.session_id,
                title=timeline.title,
                domain_category=timeline.domain_category,
                event_start=timeline.event_start,
                event_end=timeline.event_end,
                canonical_key=timeline.canonical_key,
                entity_ids=tuple(str(value) for value in entity_ids),
                salience=timeline.salience,
                sensitivity=timeline.sensitivity,
            )
            existing = await self._episode_candidates(
                connection,
                account_id=event.account_id,
                domain_category=timeline.domain_category,
            )
            selected = self._episode_consolidator.choose(candidate, existing)
            episode_id = (
                uuid.UUID(selected.episode_id)
                if selected is not None
                else _stable_uuid("episode", event.account_id, candidate.fallback_key)
            )
            timeline_id = _stable_uuid("timeline", event.event_id, index)
            await connection.execute(
                """
                INSERT INTO life_episodes (
                    episode_id, account_id, title, category, domain_category,
                    memory_kind, consolidation_key, entity_ids, event_start,
                    event_end, observed_at, stability, salience, sensitivity,
                    conflict_state, evidence_count, source_event_id
                ) VALUES (
                    $1, $2, $3, $4, $5, 'episodic', $6, $7, $8, $9, $10,
                    0.5, $11, $12, 'none', 1, $13
                )
                ON CONFLICT DO NOTHING
                """,
                episode_id,
                event.account_id,
                timeline.title,
                timeline.domain_category,
                timeline.domain_category,
                candidate.fallback_key,
                list(entity_ids),
                timeline.event_start,
                timeline.event_end,
                event.occurred_at,
                timeline.salience,
                timeline.sensitivity,
                event.event_id,
            )
            await connection.execute(
                """
                INSERT INTO episode_evidence (
                    episode_id, account_id, source_event_id, status
                ) VALUES ($1, $2, $3, 'candidate')
                ON CONFLICT DO NOTHING
                """,
                episode_id,
                event.account_id,
                event.event_id,
            )
            await connection.execute(
                """
                INSERT INTO timeline_entries (
                    timeline_id, account_id, episode_id, title, category,
                    domain_category, memory_kind, entity_ids, event_start,
                    event_end, time_precision, observed_at, salience, sensitivity,
                    conflict_state, source_event_id
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, 'episodic', $7, $8, $9, $10,
                    $11, $12, $13, 'none', $14
                )
                ON CONFLICT DO NOTHING
                """,
                timeline_id,
                event.account_id,
                episode_id,
                timeline.title,
                timeline.domain_category,
                timeline.domain_category,
                list(entity_ids),
                timeline.event_start,
                timeline.event_end,
                timeline.time_precision,
                event.occurred_at,
                timeline.salience,
                timeline.sensitivity,
                event.event_id,
            )
            await self._refresh_episode_projection(connection, episode_id=episode_id)

        for index, knowledge in enumerate(extraction.knowledge):
            knowledge_id = _stable_uuid("knowledge", event.event_id, index)
            entity_ids = tuple(
                person_ids.get(key) or _stable_uuid("person", event.account_id, key)
                for key in sorted(set(knowledge.entity_keys))
            )
            await connection.execute(
                """
                INSERT INTO knowledge_items (
                    knowledge_id, account_id, category, domain_category,
                    memory_kind, entity_ids, question, answer, applicability,
                    counterexample, source_event_id, occurred_at, valid_from,
                    valid_to, observed_at, stability, salience, sensitivity,
                    conflict_state
                ) VALUES (
                    $1, $2, $3, $4, 'procedural', $5, $6, $7, $8, $9, $10,
                    $11, $12, NULL, $13, 0.5, $14, $15, 'none'
                )
                ON CONFLICT DO NOTHING
                """,
                knowledge_id,
                event.account_id,
                knowledge.domain_category,
                knowledge.domain_category,
                list(entity_ids),
                knowledge.question,
                knowledge.answer,
                knowledge.applicability,
                knowledge.counterexample,
                event.event_id,
                event.occurred_at,
                event.occurred_at,
                event.occurred_at,
                knowledge.salience,
                knowledge.sensitivity,
            )
            body = (
                " ".join(
                    part
                    for part in (
                        knowledge.answer,
                        knowledge.applicability,
                        knowledge.counterexample,
                    )
                    if part
                )
                + f"\n[retrieval-context] {redact_pii(str(event.payload.get('text') or ''))[:240]}"
            )[:8000]
            await self._insert_search_document(
                connection,
                account_id=event.account_id,
                item_id=knowledge_id,
                kind="knowledge",
                memory_kind="procedural",
                title=knowledge.question,
                body=body,
                domain_category=knowledge.domain_category,
                entity_ids=entity_ids,
                source_event_ids=(event.event_id,),
                valid_from=event.occurred_at,
                valid_to=None,
                occurred_at=event.occurred_at,
                observed_at=event.occurred_at,
                stability=0.5,
                salience=knowledge.salience,
                sensitivity=knowledge.sensitivity,
                conflict_state="none",
            )

    @staticmethod
    async def _existing_single_value_claims(
        connection: asyncpg.Connection,
        *,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
    ) -> tuple[str, ...]:
        if len(extraction.claims) != 1:
            return ()
        claim = extraction.claims[0]
        if claim.predicate not in SINGLE_VALUE_PREDICATES:
            return ()
        # A blank evidence subject is an unclaimed speaker.  It may still
        # conflict with other unclaimed rows, but it must not inherit a value
        # that already belongs to a named subject, and it must not be stored
        # as the account owner: NULL means "no confirmed speaker".
        subject_id = confirmed_evidence_subject(event) or ""
        lineage = (
            "(SELECT lineage.subject_id FROM archive_evidence_events lineage"
            " WHERE lineage.event_id = memory_claims.source_event_id"
            " AND lineage.account_id = memory_claims.account_id)"
        )
        if subject_id:
            lineage_clause = f" AND {lineage} = $4"
            lineage_parameters: tuple[object, ...] = (subject_id,)
        else:
            lineage_clause = f" AND {lineage} IS NULL"
            lineage_parameters = ()
        rows = await connection.fetch(
            f"""
            SELECT value FROM memory_claims
            WHERE account_id = $1 AND subject_key = $2 AND predicate = $3
              AND status != 'retracted'
              {lineage_clause}
            """,
            event.account_id,
            claim.subject_key,
            claim.predicate,
            *lineage_parameters,
        )
        return tuple(str(row["value"]) for row in rows)

    async def _apply_memory_write_decision(
        self,
        connection: asyncpg.Connection,
        *,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
        decision: MemoryWriteDecision,
    ) -> None:
        if not decision.confirmed:
            return
        claim = extraction.claims[0]
        claim_id = _stable_uuid("claim", event.event_id, 0)
        review_event = decision.confirmation_event(
            source=event,
            claim_id=str(claim_id),
            claim_value=claim.value,
        )
        await self._insert_evidence(connection, review_event, if_absent=True)
        await self._write_review_projection(
            connection,
            account_id=event.account_id,
            claim_id=claim_id,
            status="confirmed",
            projection_status="confirmed",
            title=claim.value[:80],
            value=claim.value,
            review_event_id=review_event.event_id,
        )

    @staticmethod
    async def _episode_candidates(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        domain_category: DomainCategory,
    ) -> tuple[ExistingEpisode, ...]:
        rows = await connection.fetch(
            """
            SELECT episode_id, consolidation_key, title, domain_category,
                   event_start, event_end, entity_ids
            FROM life_episodes
            WHERE account_id = $1
              AND (domain_category = $2 OR consolidation_key LIKE 'canonical:%')
              AND status != 'retracted'
            ORDER BY observed_at DESC, episode_id
            LIMIT 200
            """,
            account_id,
            domain_category,
        )
        return tuple(
            ExistingEpisode(
                episode_id=str(row["episode_id"]),
                consolidation_key=str(row["consolidation_key"]),
                title=str(row["title"]),
                domain_category=cast(DomainCategory, row["domain_category"]),
                event_start=cast(datetime, row["event_start"]),
                event_end=cast(datetime | None, row["event_end"]),
                entity_ids=tuple(str(value) for value in row["entity_ids"]),
            )
            for row in rows
        )

    @staticmethod
    async def _refresh_claim_conflicts(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        subject_key: str,
        predicate: str,
        subject_id: str | None = None,
        unclaimed_only: bool = False,
    ) -> None:
        # An explicit scope keeps the conflict fold inside one speaker: another
        # subject's claims share ``subject_key`` ("self") and must neither
        # decide nor receive this review's conflict state.  The scope is the
        # same lineage contract the read path uses -- account-consistent own
        # evidence plus every source the claim's projection merged -- so a claim
        # that a scoped read hides is not folded here either.  ``unclaimed_only``
        # is the compile path for a blank evidence subject: those rows fold
        # among themselves and never against a named subject.
        def _scope(fixed_count: int) -> tuple[str, list[object]]:
            """Lineage scope for a statement with *fixed_count* leading parameters.

            The placeholder numbers come from the same binder every other call
            site uses, seeded with the number of parameters already bound.
            """

            if unclaimed_only:
                return (
                    " AND (SELECT lineage.subject_id FROM archive_evidence_events lineage"
                    " WHERE lineage.event_id = memory_claims.source_event_id"
                    " AND lineage.account_id = memory_claims.account_id) IS NULL",
                    [],
                )
            if subject_id is None:
                return "", []
            indexed: list[object] = [None] * fixed_count
            rendered = "".join(
                f" AND {lineage}"
                for lineage in subject_lineage_predicates(
                    bind=_bind_dollar(indexed),
                    subject_id=subject_id,
                    account_column="memory_claims.account_id",
                    single_source_column="memory_claims.source_event_id",
                    evidence_table="archive_evidence_events",
                    document_source_item=(
                        "claim",
                        "memory_claims.claim_id",
                        "memory_claims.account_id",
                    ),
                )
            )
            return rendered, indexed[fixed_count:]

        select_scope, select_values = _scope(3)

        values = await connection.fetch(
            f"""
            SELECT value FROM memory_claims
            WHERE account_id = $1 AND subject_key = $2 AND predicate = $3
              AND status != 'retracted'
              {select_scope}
            """,
            account_id,
            subject_key,
            predicate,
            *select_values,
        )
        state: ConflictState = (
            "active"
            if predicate in _SINGLE_VALUE_PREDICATES
            and len({str(row["value"]) for row in values}) > 1
            else "none"
        )
        update_scope, update_values = _scope(4)
        await connection.execute(
            f"""
            UPDATE memory_claims
            SET conflict_state = $1
            WHERE account_id = $2 AND subject_key = $3 AND predicate = $4
              {update_scope}
            """,
            state,
            account_id,
            subject_key,
            predicate,
            *update_values,
        )
        await connection.execute(
            f"""
            UPDATE memory_search_documents document
            SET conflict_state = $1
            WHERE document.account_id = $2 AND document.kind = 'claim'
              AND document.item_id IN (
                  SELECT claim_id FROM memory_claims
                  WHERE account_id = $2 AND subject_key = $3 AND predicate = $4
                    {update_scope}
              )
            """,
            state,
            account_id,
            subject_key,
            predicate,
            *update_values,
        )

    @staticmethod
    async def _episode_merged_other_subjects(
        connection: asyncpg.Connection,
        *,
        episode_id: uuid.UUID,
        account_id: str,
        subject_id: str,
    ) -> bool:
        """Whether an episode carries any source that is not this subject's.

        A link row from another account -- or one whose evidence row sits in a
        different account than the link itself -- counts as foreign, so the
        refresh leaves the episode alone instead of rewriting it.
        """

        row = await connection.fetchval(
            """
            SELECT 1
            FROM episode_evidence evidence
            LEFT JOIN archive_evidence_events source
              ON source.event_id = evidence.source_event_id
             AND source.account_id = evidence.account_id
            WHERE evidence.episode_id = $1
              AND evidence.account_id = $2
              AND (source.subject_id IS NULL OR source.subject_id <> $3)
            LIMIT 1
            """,
            episode_id,
            account_id,
            subject_id,
        )
        return row is not None

    async def _refresh_episode_projection(
        self,
        connection: asyncpg.Connection,
        *,
        episode_id: uuid.UUID,
        account_id: str | None = None,
        subject_id: str | None = None,
    ) -> None:
        if subject_id is not None and account_id is None:
            raise ValueError("scoped episode refresh requires account_id")
        if (
            subject_id is not None
            and account_id is not None
            and await self._episode_merged_other_subjects(
                connection,
                episode_id=episode_id,
                account_id=account_id,
                subject_id=subject_id,
            )
        ):
            # The episode was consolidated across speakers; rewriting it here
            # would push this review into a projection it does not own.
            return
        episode = await connection.fetchrow(
            "SELECT * FROM life_episodes WHERE episode_id = $1",
            episode_id,
        )
        if episode is None:
            raise EvidenceNotFoundError(str(episode_id))
        timelines = await connection.fetch(
            """
            SELECT * FROM timeline_entries
            WHERE episode_id = $1
            ORDER BY event_start, timeline_id
            """,
            episode_id,
        )
        evidence = await connection.fetch(
            """
            SELECT source_event_id, status
            FROM episode_evidence
            WHERE episode_id = $1
            ORDER BY source_event_id
            """,
            episode_id,
        )
        if not timelines or not evidence:
            return
        statuses = [cast(MemoryStatus, str(row["status"])) for row in evidence]
        status: MemoryStatus
        if "confirmed" in statuses:
            status = "confirmed"
        elif "candidate" in statuses:
            status = "candidate"
        elif "disputed" in statuses:
            status = "disputed"
        else:
            status = "retracted"
        active_source_event_ids = tuple(
            str(row["source_event_id"]) for row in evidence if str(row["status"]) != "retracted"
        )
        retracted_source_event_ids = tuple(
            str(row["source_event_id"]) for row in evidence if str(row["status"]) == "retracted"
        )
        source_event_ids = (
            *active_source_event_ids,
            *retracted_source_event_ids,
        )
        active_timelines = [row for row in timelines if str(row["status"]) != "retracted"]
        surfaced_timelines = active_timelines or list(timelines)
        titles = tuple(dict.fromkeys(str(row["title"]) for row in surfaced_timelines))
        title = max(titles, key=lambda value: (len(value), value))
        source_rows = await connection.fetch(
            """
            SELECT payload->>'text' AS text
            FROM archive_evidence_events
            WHERE event_id = ANY($1::text[])
            ORDER BY event_id
            """,
            list(source_event_ids),
        )
        source_texts = [
            redact_pii(str(row["text"]))[:240]
            for row in source_rows
            if str(row["text"] or "").strip()
        ]
        body = f"{';'.join(titles)}\n[retrieval-context] {'；'.join(source_texts)}"[:8000]
        starts = [cast(datetime, row["event_start"]) for row in surfaced_timelines]
        ends = [
            cast(datetime, row["event_end"])
            for row in surfaced_timelines
            if row["event_end"] is not None
        ]
        observations = [cast(datetime, row["observed_at"]) for row in surfaced_timelines]
        entity_ids = tuple(
            sorted(
                {
                    cast(uuid.UUID, entity_id)
                    for row in surfaced_timelines
                    for entity_id in row["entity_ids"]
                },
                key=str,
            )
        )
        sensitivities = [
            cast(MemorySensitivity, str(row["sensitivity"])) for row in surfaced_timelines
        ]
        sensitivity = max(
            sensitivities,
            key=lambda value: _SENSITIVITY_ORDER[value],
        )
        evidence_count = len(source_event_ids)
        active_evidence_count = len(active_source_event_ids)
        stability = (
            min(0.95, 0.5 + 0.1 * (active_evidence_count - 1)) if active_evidence_count else 0.0
        )
        salience = max(float(row["salience"]) for row in surfaced_timelines)
        event_start = min(starts)
        event_end = max(ends) if ends else None
        observed_at = max(observations)
        await connection.execute(
            """
            UPDATE life_episodes
            SET title = $1, category = domain_category, entity_ids = $2,
                status = $3, event_start = $4, event_end = $5, observed_at = $6,
                stability = $7, salience = $8, sensitivity = $9,
                evidence_count = $10, source_event_id = $11
            WHERE episode_id = $12
            """,
            title,
            list(entity_ids),
            status,
            event_start,
            event_end,
            observed_at,
            stability,
            salience,
            sensitivity,
            evidence_count,
            source_event_ids[0],
            episode_id,
        )
        await self._insert_search_document(
            connection,
            account_id=str(episode["account_id"]),
            item_id=episode_id,
            kind="episode",
            memory_kind="episodic",
            title=title,
            body=body,
            domain_category=cast(DomainCategory, str(episode["domain_category"])),
            entity_ids=entity_ids,
            source_event_ids=source_event_ids,
            valid_from=event_start,
            valid_to=event_end,
            occurred_at=event_start,
            observed_at=observed_at,
            stability=stability,
            salience=salience,
            sensitivity=sensitivity,
            conflict_state="none",
            status=status,
        )

    async def _insert_search_document(
        self,
        connection: asyncpg.Connection,
        *,
        account_id: str,
        item_id: uuid.UUID,
        kind: str,
        memory_kind: MemoryKind,
        title: str,
        body: str,
        domain_category: DomainCategory,
        entity_ids: tuple[uuid.UUID, ...],
        source_event_ids: tuple[str, ...],
        valid_from: datetime | None,
        valid_to: datetime | None,
        occurred_at: datetime,
        observed_at: datetime,
        stability: float,
        salience: float,
        sensitivity: MemorySensitivity,
        conflict_state: ConflictState,
        status: MemoryStatus = "candidate",
    ) -> None:
        if not source_event_ids:
            raise ValueError("memory search projection requires evidence")
        document_id = _stable_uuid("search", kind, item_id)
        await connection.execute(
            """
            INSERT INTO memory_search_documents (
                document_id, account_id, item_id, kind, memory_kind, title, body,
                category, domain_category, entity_ids, status, source_event_id,
                occurred_at, valid_from, valid_to, observed_at, stability, salience,
                sensitivity, conflict_state
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                $14, $15, $16, $17, $18, $19, $20
            )
            ON CONFLICT (kind, item_id) DO UPDATE SET
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                memory_kind = EXCLUDED.memory_kind,
                category = EXCLUDED.category,
                domain_category = EXCLUDED.domain_category,
                entity_ids = EXCLUDED.entity_ids,
                status = EXCLUDED.status,
                occurred_at = EXCLUDED.occurred_at,
                valid_from = EXCLUDED.valid_from,
                valid_to = EXCLUDED.valid_to,
                observed_at = EXCLUDED.observed_at,
                stability = EXCLUDED.stability,
                salience = EXCLUDED.salience,
                sensitivity = EXCLUDED.sensitivity,
                conflict_state = EXCLUDED.conflict_state
            """,
            document_id,
            account_id,
            item_id,
            kind,
            memory_kind,
            title,
            body,
            domain_category,
            domain_category,
            list(entity_ids),
            status,
            source_event_ids[0],
            occurred_at,
            valid_from,
            valid_to,
            observed_at,
            stability,
            salience,
            sensitivity,
            conflict_state,
        )
        await connection.executemany(
            """
            INSERT INTO memory_search_document_sources (
                document_id, account_id, source_event_id
            ) VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            [(document_id, account_id, source_event_id) for source_event_id in source_event_ids],
        )
        corrected = self._vector_enabled and kind == "claim" and await has_correction_review(
            connection, item_id, account_id
        )
        if self._vector_enabled and self._embedder is not None and not corrected:
            # A corrected claim is embedded by its review replay, not here.
            await upsert_document_vector(
                connection,
                self._embedder,
                item_id=item_id,
                account_id=account_id,
                source_event_id=source_event_ids[0],
                title=title,
                body=body,
                fail_on_embedding_error=self._fail_on_embedding_error,
            )

    @staticmethod
    async def _insert_evidence(
        connection: asyncpg.Connection,
        event: EvidenceEvent,
        *,
        if_absent: bool = False,
    ) -> None:
        conflict_clause = "ON CONFLICT DO NOTHING" if if_absent else ""
        await connection.execute(
            f"""
            INSERT INTO archive_evidence_events (
                event_id, account_id, session_id, turn_id, generation_id,
                event_type, schema_version, occurred_at, subject_id,
                speaker_identity_id, speaker_class, source, consent_grant_id,
                payload, content_sha256, supersedes_event_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13::jsonb, $14, $15, $16
            )
            {conflict_clause}
            """,
            event.event_id,
            event.account_id,
            event.session_id,
            event.turn_id,
            event.generation_id,
            event.event_type,
            event.schema_version,
            event.occurred_at,
            event.subject_id,
            event.speaker_identity_id,
            event.speaker_class,
            event.source,
            event.consent_grant_id,
            json.dumps(dict(event.payload), ensure_ascii=False, separators=(",", ":")),
            event.content_sha256,
            event.supersedes_event_id,
        )
        await connection.execute(
            f"""
            INSERT INTO archive_processing_outbox (
                outbox_id, account_id, event_id, task_type
            ) VALUES ($1, $2, $3, 'compile_evidence')
            {conflict_clause}
            """,
            _stable_uuid("outbox", event.event_id),
            event.account_id,
            event.event_id,
        )
