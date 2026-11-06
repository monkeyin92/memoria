"""PostgreSQL timeline and people projection mixin for the memory catalog.

``PostgresMemoryTimelineMixin`` is composed into ``PostgresMemoryCatalog``. The
catalog owns the pool and the RLS scope setter; this module owns the account
timeline and its people projection.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from services.archive.memory_domain import (
    DomainCategory,
    MemoryCategory,
    MemoryStatus,
    PersonItem,
    TimelineItem,
    subject_lineage_predicates,
)
from services.archive.memory_sql import _bind_dollar

if TYPE_CHECKING:
    import asyncpg


class PostgresMemoryTimelineMixin:
    """Project the account timeline and the people it exposes."""

    if TYPE_CHECKING:
        # Shared members owned by PostgresMemoryCatalog.
        async def _ready_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _scope(connection: asyncpg.Connection, account_id: str) -> None: ...

    async def timeline(
        self,
        *,
        account_id: str,
        limit: int = 50,
        subject_id: str | None = None,
    ) -> tuple[TimelineItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("timeline requires account_id and limit 1..100")
        if subject_id is not None and not subject_id.strip():
            raise ValueError("timeline subject_id must not be blank")
        parameters: list[Any] = [account_id]
        clauses = ["timeline.account_id = $1", "timeline.status != 'retracted'"]
        if subject_id is not None:
            # A timeline entry merges every source of its episode: the entry's
            # own evidence and the whole episode set must be the same subject.
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_dollar(parameters),
                    subject_id=subject_id,
                    account_column="timeline.account_id",
                    single_source_column="timeline.source_event_id",
                    evidence_table="archive_evidence_events",
                    merged_source_link=(
                        "episode_evidence",
                        "episode_id",
                        "timeline.episode_id",
                    ),
                )
            )
        parameters.append(limit)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                f"""
                SELECT timeline.*,
                       ARRAY(
                           SELECT evidence.source_event_id
                           FROM episode_evidence evidence
                           WHERE evidence.episode_id = timeline.episode_id
                           ORDER BY evidence.source_event_id
                       ) AS source_event_ids
                FROM timeline_entries timeline
                WHERE {" AND ".join(clauses)}
                ORDER BY timeline.event_start DESC, timeline.timeline_id
                LIMIT ${len(parameters)}
                """,
                *parameters,
            )
        return tuple(
            TimelineItem(
                timeline_id=str(row["timeline_id"]),
                title=str(row["title"]),
                category=cast(MemoryCategory, row["category"]),
                status=cast(MemoryStatus, row["status"]),
                event_start=cast(datetime, row["event_start"]),
                event_end=cast(datetime | None, row["event_end"]),
                time_precision=str(row["time_precision"]),
                source_event_id=str(row["source_event_id"]),
                episode_id=str(row["episode_id"]),
                domain_category=cast(DomainCategory, row["domain_category"]),
                source_event_ids=tuple(str(value) for value in row["source_event_ids"]),
            )
            for row in rows
        )

    async def people(
        self,
        *,
        account_id: str,
        limit: int = 100,
        subject_id: str | None = None,
    ) -> tuple[PersonItem, ...]:
        if not account_id.strip() or not 1 <= limit <= 100:
            raise ValueError("people requires account_id and limit 1..100")
        if subject_id is not None and not subject_id.strip():
            raise ValueError("people subject_id must not be blank")
        parameters: list[Any] = [account_id]
        clauses = ["entity.account_id = $1", "entity.status != 'retracted'"]
        if subject_id is not None:
            # Every alias is its own evidence row, so one alias learned from
            # another subject hides the whole person instead of leaking a name.
            clauses.extend(
                subject_lineage_predicates(
                    bind=_bind_dollar(parameters),
                    subject_id=subject_id,
                    account_column="entity.account_id",
                    single_source_column="entity.source_event_id",
                    evidence_table="archive_evidence_events",
                    merged_source_link=(
                        "person_aliases",
                        "person_id",
                        "entity.person_id",
                    ),
                    require_merged_source=False,
                    document_source_item=(
                        "person",
                        "entity.person_id",
                        "entity.account_id",
                    ),
                )
            )
        parameters.append(limit)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                f"""
                SELECT entity.*, coalesce(
                    array_agg(DISTINCT alias.alias ORDER BY alias.alias)
                        FILTER (WHERE alias.alias IS NOT NULL),
                    ARRAY[]::text[]
                ) AS aliases
                FROM person_entities entity
                LEFT JOIN person_aliases alias
                  ON alias.person_id = entity.person_id
                 AND alias.status = entity.status
                WHERE {" AND ".join(clauses)}
                GROUP BY entity.person_id
                ORDER BY entity.created_at, entity.person_id
                LIMIT ${len(parameters)}
                """,
                *parameters,
            )
        return tuple(
            PersonItem(
                person_id=str(row["person_id"]),
                display_name=str(row["display_name"]),
                relationship_to_owner=str(row["relationship_to_owner"]),
                aliases=tuple(str(alias) for alias in row["aliases"]),
                status=cast(MemoryStatus, row["status"]),
                source_event_id=str(row["source_event_id"]),
            )
            for row in rows
        )
