"""Subject-scoped archive deletion on PostgreSQL (``SubjectArchivePort``).

The adapter extends the account repository, so one instance serves account
deletion and subject deletion from the same DSN.  Deletion runs one plan
inside one transaction:

1. *collect* the ids of every projection that cites any doomed event --
   merged ones included: an episode, search document (and its vectors),
   persona trait, skill or self-model item citing one doomed event goes whole;
2. *write* child rows first, then the projections, then the non-cascading
   references (transcripts, skill runs, persona consent events, ``supersedes``
   pointers from outside the set), and the evidence rows last.

Every statement is filtered by ``account_id`` and runs under the account's
``app.account_id`` RLS scope -- the adapter never bypasses RLS.
Deleting already-deleted ids matches nothing, so a retry is a no-op.

The subject's own persona (traits, ``speech_style_stats``, ``persona_versions``)
is keyed by subject since 2026-09-26 and removed by the saga's
``persona_forgotten`` step through the persona engine.

Not covered (no row-level lineage to follow): ``digital_self_versions``
snapshots and ``entity_ids`` arrays that may still name a deleted person.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import asyncpg

from services.archive.object_store import ObjectRef
from services.governance.account_data import (
    _POSTGRES_ARCHIVE_DELETE_ORDER,
    _POSTGRES_ARCHIVE_EXPORT_TABLES,
    PostgresAccountRepository,
)
from services.governance.subject_ports import SubjectScope

__all__ = ["PostgresSubjectArchive"]

#: Consent-ledger evidence stays: it is the audit of what was authorized.
_CONSENT_EVENT_PREFIX: Final[str] = "guardian.person_consent_"

# ``:name`` is a scalar (``acct``/``subject``/``prefix``) or an id set; ``::`` never occurs.
_TOKEN = re.compile(r":([a-z_]+)")
_SCALARS: Final[frozenset[str]] = frozenset({"acct", "subject", "prefix"})


@dataclass(frozen=True, slots=True)
class _Collect:
    """Add the single text column of ``sql`` to the id set ``into``."""

    into: str
    table: str
    sql: str


@dataclass(frozen=True, slots=True)
class _Union:
    into: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Write:
    """One DELETE/UPDATE counted under ``label`` (skipped if ``table`` is absent)."""

    label: str
    table: str
    sql: str


_Step = _Collect | _Union | _Write


_EVIDENCE: Final[str] = "archive_evidence_events"
_OUTBOX: Final[str] = "archive_processing_outbox"
_BLOBS: Final[str] = "archive_evidence_blobs"
_TRANSCRIPTS: Final[str] = "archive_transcript_versions"

#: Projections with one ``source_event_id`` (cascading) that must not outlive it.
_SOURCE_TABLES: Final[tuple[str, ...]] = (
    "memory_vector_documents",
    "memory_search_document_sources",
    "memory_search_documents",
    "timeline_entries",
    "episode_evidence",
    "life_episodes",
    "relationships",
    "person_aliases",
    "person_entities",
    "knowledge_items",
    "memory_claims",
    "persona_evidence",
    "persona_observation_receipts",
    "skill_version_evidence",
    "self_model_cognitive_claim_sources",
    "self_model_decision_case_sources",
    "self_model_relationship_profile_sources",
)


def _where(table: str, clause: str) -> str:
    return f"FROM {table} WHERE account_id = :acct AND ({clause})"


def _collect(into: str, table: str, column: str, clause: str) -> _Collect:
    return _Collect(into, table, f"SELECT CAST({column} AS TEXT) {_where(table, clause)}")


def _delete(table: str, clause: str) -> _Write:
    return _Write(table, table, f"DELETE {_where(table, clause)}")


def _nullify(table: str, column: str, clause: str) -> _Write:
    return _Write(
        f"{table}.{column}",
        table,
        f"UPDATE {table} SET {column} = NULL WHERE account_id = :acct AND ({clause})",
    )


def _self_model_steps() -> tuple[tuple[_Step, ...], tuple[_Step, ...]]:
    """Collect/write steps for self-model items citing a doomed event."""

    owned_profiles = (
        "CAST(person_id AS TEXT) IN :persons OR CAST(relationship_id AS TEXT) IN :relationships"
    )
    collects: tuple[_Step, ...] = (
        _collect(
            "sm_claims",
            "self_model_cognitive_claim_sources",
            "claim_id",
            "source_event_id IN :ids",
        ),
        _collect(
            "sm_cases",
            "self_model_decision_case_sources",
            "case_id",
            "source_event_id IN :ids",
        ),
        _collect(
            "sm_profiles",
            "self_model_relationship_profile_sources",
            "profile_id",
            "source_event_id IN :ids",
        ),
        _collect(
            "sm_profiles",
            "self_model_relationship_profiles",
            "profile_id",
            owned_profiles,
        ),
        _Union("sm_all", ("sm_claims", "sm_cases", "sm_profiles")),
    )
    writes: tuple[_Step, ...] = (
        _delete(
            "self_model_audit_events",
            "target_kind <> 'account' AND CAST(target_id AS TEXT) IN :sm_all",
        ),
        _delete("self_model_command_receipts", "CAST(result_id AS TEXT) IN :sm_all"),
        _delete(
            "self_model_relationship_profile_sources",
            "CAST(profile_id AS TEXT) IN :sm_profiles OR source_event_id IN :ids",
        ),
        _delete("self_model_relationship_profiles", "CAST(profile_id AS TEXT) IN :sm_profiles"),
        _delete(
            "self_model_decision_case_sources",
            "CAST(case_id AS TEXT) IN :sm_cases OR source_event_id IN :ids",
        ),
        _delete("self_model_decision_cases", "CAST(case_id AS TEXT) IN :sm_cases"),
        _delete(
            "self_model_cognitive_claim_sources",
            "CAST(claim_id AS TEXT) IN :sm_claims OR source_event_id IN :ids",
        ),
        _delete("self_model_cognitive_claims", "CAST(claim_id AS TEXT) IN :sm_claims"),
    )
    return collects, writes


def _delete_plan() -> tuple[_Step, ...]:
    self_model_collects, self_model_writes = _self_model_steps()
    return (
        # -- collect every projection id citing a doomed event ------------------
        _collect("episodes", "episode_evidence", "episode_id", "source_event_id IN :ids"),
        _collect("episodes", "life_episodes", "episode_id", "source_event_id IN :ids"),
        _collect(
            "timeline",
            "timeline_entries",
            "timeline_id",
            "source_event_id IN :ids OR CAST(episode_id AS TEXT) IN :episodes",
        ),
        _collect(
            "claims",
            "memory_claims",
            "claim_id",
            "source_event_id IN :ids OR review_event_id IN :ids",
        ),
        _collect("knowledge", "knowledge_items", "knowledge_id", "source_event_id IN :ids"),
        _collect("persons", "person_entities", "person_id", "source_event_id IN :ids"),
        _collect(
            "relationships",
            "relationships",
            "relationship_id",
            "source_event_id IN :ids OR CAST(person_id AS TEXT) IN :persons",
        ),
        _Union("items", ("episodes", "timeline", "claims", "knowledge", "persons")),
        _collect(
            "docs", "memory_search_document_sources", "document_id", "source_event_id IN :ids"
        ),
        _collect(
            "docs",
            "memory_search_documents",
            "document_id",
            "source_event_id IN :ids OR CAST(item_id AS TEXT) IN :items",
        ),
        # Vectors carry no FK to their document: follow the documents' items.
        _collect(
            "items", "memory_search_documents", "item_id", "CAST(document_id AS TEXT) IN :docs"
        ),
        _collect("traits", "persona_evidence", "trait_id", "source_event_id IN :ids"),
        _collect("traits", "persona_traits", "trait_id", "review_event_id IN :ids"),
        # A skill version learned from any doomed turn takes its definition with it:
        # later versions are refinements of the same learned content.
        _collect("skills", "skill_version_evidence", "skill_id", "source_event_id IN :ids"),
        _collect(
            "runs",
            "skill_runs",
            "run_id",
            "confirmation_event_id IN :ids OR CAST(skill_id AS TEXT) IN :skills",
        ),
        _collect("outbox", _OUTBOX, "outbox_id", "event_id IN :ids"),
        *self_model_collects,
        # -- merged and single-source projections, children first ---------------
        _delete(
            "memory_vector_documents",
            "source_event_id IN :ids OR CAST(item_id AS TEXT) IN :items",
        ),
        _delete(
            "memory_search_document_sources",
            "CAST(document_id AS TEXT) IN :docs OR source_event_id IN :ids",
        ),
        _delete("memory_search_documents", "CAST(document_id AS TEXT) IN :docs"),
        _delete("timeline_entries", "CAST(timeline_id AS TEXT) IN :timeline"),
        _delete(
            "episode_evidence",
            "CAST(episode_id AS TEXT) IN :episodes OR source_event_id IN :ids",
        ),
        _delete("life_episodes", "CAST(episode_id AS TEXT) IN :episodes"),
        # Relationship profiles reference people without a cascade on PostgreSQL.
        *self_model_writes,
        _delete("relationships", "CAST(relationship_id AS TEXT) IN :relationships"),
        _delete("person_aliases", "CAST(person_id AS TEXT) IN :persons OR source_event_id IN :ids"),
        _delete("person_entities", "CAST(person_id AS TEXT) IN :persons"),
        _delete("knowledge_items", "CAST(knowledge_id AS TEXT) IN :knowledge"),
        _delete("memory_claims", "CAST(claim_id AS TEXT) IN :claims"),
        _delete("persona_evidence", "CAST(trait_id AS TEXT) IN :traits OR source_event_id IN :ids"),
        _delete("persona_observation_receipts", "source_event_id IN :ids"),
        _delete("persona_traits", "CAST(trait_id AS TEXT) IN :traits"),
        # A learning consent granted by a doomed event is withdrawn with it.
        _delete("persona_learning_consents", "grant_event_id IN :ids"),
        _nullify("persona_learning_consents", "revoke_event_id", "revoke_event_id IN :ids"),
        _delete("skill_run_steps", "CAST(run_id AS TEXT) IN :runs"),
        _delete("skill_runs", "CAST(run_id AS TEXT) IN :runs"),
        _delete(
            "skill_version_evidence",
            "CAST(skill_id AS TEXT) IN :skills OR source_event_id IN :ids",
        ),
        _delete("skill_versions", "CAST(skill_id AS TEXT) IN :skills"),
        _delete("skill_definitions", "CAST(skill_id AS TEXT) IN :skills"),
        _nullify("skill_versions", "approval_event_id", "approval_event_id IN :ids"),
        _delete("memory_compile_receipts", "event_id IN :ids"),
        # -- evidence-owned rows, then the evidence ----------------------------
        _delete(_TRANSCRIPTS, "evidence_event_id IN :ids"),
        _delete(_BLOBS, "evidence_event_id IN :ids"),
        _delete("archive_outbox_replay_audit", "CAST(outbox_id AS TEXT) IN :outbox"),
        _delete(_OUTBOX, "event_id IN :ids"),
        _nullify(
            _EVIDENCE,
            "supersedes_event_id",
            "supersedes_event_id IN :ids AND event_id NOT IN :ids",
        ),
        _delete(_EVIDENCE, "event_id IN :ids"),
    )


def _remaining_checks(*, subject: bool) -> tuple[_Write, ...]:
    """Count queries (as ``_Write`` for label/table) of rows still citing the ids."""

    def count(label: str, table: str, clause: str) -> _Write:
        return _Write(label, table, f"SELECT count(*) {_where(table, clause)}")

    return (
        count(_EVIDENCE, _EVIDENCE, "event_id IN :ids"),
        count(f"{_EVIDENCE}.supersedes_event_id", _EVIDENCE, "supersedes_event_id IN :ids"),
        *(
            (count(f"{_EVIDENCE}.subject_id", _EVIDENCE, "subject_id = :subject"),)
            if subject
            else ()
        ),
        count(_OUTBOX, _OUTBOX, "event_id IN :ids"),
        count(_BLOBS, _BLOBS, "evidence_event_id IN :ids"),
        count(_TRANSCRIPTS, _TRANSCRIPTS, "evidence_event_id IN :ids"),
        count("memory_compile_receipts", "memory_compile_receipts", "event_id IN :ids"),
        *(count(table, table, "source_event_id IN :ids") for table in _SOURCE_TABLES),
        *(
            count(f"{table}.{column}", table, f"{column} IN :ids")
            for table, column in (
                ("memory_claims", "review_event_id"),
                ("persona_traits", "review_event_id"),
                ("persona_learning_consents", "grant_event_id"),
                ("persona_learning_consents", "revoke_event_id"),
                ("skill_versions", "approval_event_id"),
                ("skill_runs", "confirmation_event_id"),
            )
        ),
    )


def _chain_sql(evidence: str, chain: tuple[str, str]) -> str:
    """Seed events plus every event whose ``supersedes`` chain reaches one."""

    seed, follow = chain
    return f"""
        WITH RECURSIVE chain(event_id) AS (
            SELECT event_id FROM {evidence} WHERE account_id = :acct AND ({seed})
            UNION
            SELECT later.event_id FROM {evidence} later
            JOIN chain ON later.supersedes_event_id = chain.event_id
            WHERE later.account_id = :acct AND ({follow})
        )
        SELECT event_id FROM chain ORDER BY event_id
    """


#: (seed, follow) filters: a subject's rows and anything superseding them.
_SUBJECT_CHAIN: Final[tuple[str, str]] = ("subject_id = :subject", "1 = 1")
# LIKE treats '_' as a wildcard; a prefix compare is exact.
_ACCOUNT_CHAIN: Final[tuple[str, str]] = (
    f"substr(event_type, 1, {len(_CONSENT_EVENT_PREFIX)}) <> :prefix",
    f"substr(later.event_type, 1, {len(_CONSENT_EVENT_PREFIX)}) <> :prefix",
)


def _event_ids(event_ids: Sequence[str]) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(str(value) for value in event_ids))
    if any(not value or len(value) > 512 for value in values):
        raise ValueError("event ids must be bounded non-empty strings")
    return values


def _account(account_id: str) -> str:
    if not account_id.strip() or len(account_id) > 128:
        raise ValueError("account_id must be a bounded non-empty id")
    return account_id


def _set_names(plan: Sequence[_Step]) -> set[str]:
    names = {"ids"}
    for step in plan:
        names.update(_TOKEN.findall(step.sql) if not isinstance(step, _Union) else ())
        if not isinstance(step, _Write):
            names.add(step.into)
    return names - _SCALARS


def _object_ref(account_id: str, row: asyncpg.Record) -> ObjectRef:
    return ObjectRef(
        account_id=account_id,
        object_key=str(row["object_key"]),
        media_type=str(row["media_type"]),
        byte_count=int(row["byte_count"]),
        content_sha256=str(row["content_sha256"]),
        encryption_key_version=str(row["encryption_key_version"]),
        backend="archive",
    )


class _PostgresRun:
    """One PostgreSQL transaction; id sets travel as ``text[]`` parameters."""

    def __init__(self, connection: asyncpg.Connection, scalars: dict[str, str]) -> None:
        self._connection = connection
        self._scalars = scalars
        self._tables: dict[str, bool] = {}
        self.sets: dict[str, set[str]] = {}

    async def has(self, table: str) -> bool:
        if table not in self._tables:
            self._tables[table] = (
                await self._connection.fetchval("SELECT to_regclass($1)", table) is not None
            )
        return self._tables[table]

    def render(self, sql: str) -> tuple[str, list[object]]:
        arguments: list[object] = []
        positions: dict[str, int] = {}

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in positions:
                arguments.append(
                    self._scalars[name] if name in _SCALARS else sorted(self.sets.get(name, ()))
                )
                positions[name] = len(arguments)
            if name in _SCALARS:
                return f"${positions[name]}"
            return f"(SELECT unnest(${positions[name]}::text[]))"

        return _TOKEN.sub(replace, sql), arguments

    async def rows(self, sql: str) -> list[asyncpg.Record]:
        query, arguments = self.render(sql)
        return list(await self._connection.fetch(query, *arguments))

    async def write(self, sql: str) -> int:
        query, arguments = self.render(sql)
        status = await self._connection.execute(query, *arguments)
        return int(str(status).rsplit(" ", 1)[-1])


class PostgresSubjectArchive(PostgresAccountRepository):
    """PostgreSQL archive repository that also answers ``SubjectArchivePort``."""

    def __init__(self, dsn: str) -> None:
        super().__init__(
            dsn,
            export_tables=_POSTGRES_ARCHIVE_EXPORT_TABLES,
            delete_order=_POSTGRES_ARCHIVE_DELETE_ORDER,
            blob_table=_BLOBS,
        )

    async def subject_event_ids(self, scope: SubjectScope) -> tuple[str, ...]:
        return await self._chain(scope.account_id, _SUBJECT_CHAIN, scope.subject_id)

    async def subject_account_event_ids(self, subject_id: str) -> tuple[str, ...]:
        return await self._chain(_account(subject_id), _ACCOUNT_CHAIN, "")

    async def object_references_for(
        self, *, account_id: str, event_ids: tuple[str, ...]
    ) -> tuple[ObjectRef, ...]:
        account_id, ids = _account(account_id), _event_ids(event_ids)
        if not ids:
            return ()
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction(readonly=True):
                await self._scope(connection, account_id)
                run = _PostgresRun(connection, {"acct": account_id})
                if not await run.has(_BLOBS):
                    return ()
                run.sets["ids"] = set(ids)
                rows = await run.rows(
                    "SELECT object_key, media_type, byte_count, content_sha256,"
                    " encryption_key_version "
                    + _where(_BLOBS, "evidence_event_id IN :ids")
                    + " ORDER BY object_key"
                )
        finally:
            await connection.close()
        return tuple(_object_ref(account_id, row) for row in rows)

    async def delete_events(self, *, account_id: str, event_ids: tuple[str, ...]) -> dict[str, int]:
        account_id, ids = _account(account_id), _event_ids(event_ids)
        if not ids:
            return {}
        plan = _delete_plan()
        counts: dict[str, int] = {}
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction():
                await self._scope(connection, account_id)
                await connection.execute("SELECT set_config('app.self_model_delete', 'true', true)")
                run = _PostgresRun(connection, {"acct": account_id})
                run.sets = {name: set() for name in _set_names(plan)}
                run.sets["ids"] = set(ids)
                for step in plan:
                    if isinstance(step, _Union):
                        for source in step.sources:
                            run.sets[step.into] |= run.sets[source]
                    elif not await run.has(step.table):
                        continue
                    elif isinstance(step, _Collect):
                        run.sets[step.into].update(str(row[0]) for row in await run.rows(step.sql))
                    else:
                        counts[step.label] = counts.get(step.label, 0) + await run.write(step.sql)
        finally:
            await connection.close()
        return counts

    async def remaining_rows_for(
        self, *, account_id: str, event_ids: tuple[str, ...], subject_id: str | None
    ) -> dict[str, int]:
        account_id, ids = _account(account_id), _event_ids(event_ids)
        remaining: dict[str, int] = {}
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction(readonly=True):
                await self._scope(connection, account_id)
                run = _PostgresRun(connection, {"acct": account_id, "subject": subject_id or ""})
                run.sets["ids"] = set(ids)
                for check in _remaining_checks(subject=subject_id is not None):
                    if await run.has(check.table):
                        count = int((await run.rows(check.sql))[0][0])
                        if count:
                            remaining[check.label] = count
        finally:
            await connection.close()
        return remaining

    async def _chain(
        self, account_id: str, chain: tuple[str, str], subject: str
    ) -> tuple[str, ...]:
        connection = await asyncpg.connect(self._dsn)
        try:
            async with connection.transaction(readonly=True):
                await self._scope(connection, account_id)
                run = _PostgresRun(
                    connection,
                    {"acct": account_id, "subject": subject, "prefix": _CONSENT_EVENT_PREFIX},
                )
                if not await run.has(_EVIDENCE):
                    return ()
                rows = await run.rows(_chain_sql(_EVIDENCE, chain))
        finally:
            await connection.close()
        return tuple(str(row[0]) for row in rows)
