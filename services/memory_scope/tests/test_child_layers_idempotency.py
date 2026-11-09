"""Offline + optional PostgreSQL idempotency for child-layer unique keys.

Offline tests prove receipt-key uniqueness and crash-resume semantics with an
in-process store. PostgreSQL contracts run when ``MEMORIA_TEST_POSTGRES_DSN``
is set.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

import pytest
from services.guardian.child_memory import (
    KIND_DIALOGUE_SUMMARY,
    ArchiveFact,
    DialogueSummary,
    archive_fact_policy_receipt,
    dialogue_summary_policy_receipt,
)
from services.memory_scope.child_layers import ChildLayerRecord, EnsuredChildLayer
from services.memory_scope.tests import test_postgres_store

pg_env = test_postgres_store.pg_env


class _MemoryUniqueStore:
    """Minimal unique-key store mirroring ensure_summary / upsert_archive_fact."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.summaries: dict[str, ChildLayerRecord] = {}
        self.archives: dict[str, ChildLayerRecord] = {}
        self.summary_inserts = 0
        self.archive_inserts = 0

    async def ensure_summary(self, summary: DialogueSummary) -> EnsuredChildLayer:
        receipt = dialogue_summary_policy_receipt(
            subject_id=summary.subject_id, session_id=summary.session_id
        )
        async with self._lock:
            existing = self.summaries.get(receipt)
            if existing is not None:
                return EnsuredChildLayer(record=existing, created=False)
            self.summary_inserts += 1
            record = ChildLayerRecord(
                record_id=f"dlgsum:{self.summary_inserts}",
                subject_id=summary.subject_id,
                kind=KIND_DIALOGUE_SUMMARY,
                text=summary.text,
                field=None,
                session_id=summary.session_id,
                created_at=datetime.now(UTC),
                retention_expires_at=None,
                payload={"policy_receipt_id": receipt},
            )
            self.summaries[receipt] = record
            return EnsuredChildLayer(record=record, created=True)

    async def upsert_archive_fact(self, fact: ArchiveFact) -> ChildLayerRecord:
        receipt = archive_fact_policy_receipt(
            subject_id=fact.subject_id, field=fact.field, text=fact.text
        )
        async with self._lock:
            existing = self.archives.get(receipt)
            if existing is not None:
                return existing
            self.archive_inserts += 1
            record = ChildLayerRecord(
                record_id=f"charch:{self.archive_inserts}",
                subject_id=fact.subject_id,
                kind="child_archive",
                text=fact.text,
                field=fact.field,
                session_id=None,
                created_at=datetime.now(UTC),
                retention_expires_at=None,
                payload={"policy_receipt_id": receipt},
            )
            self.archives[receipt] = record
            return record


@pytest.mark.asyncio
async def test_offline_concurrent_ensure_summary_converges() -> None:
    store = _MemoryUniqueStore()
    summary = DialogueSummary(
        subject_id="child-1",
        text="聊了恐龙",
        source_event_id="end:1",
        session_id="sess-1",
    )

    async def _once() -> EnsuredChildLayer:
        return await store.ensure_summary(summary)

    results = await asyncio.gather(*[_once() for _ in range(20)])
    ids = {item.record.record_id for item in results}
    assert ids == {"dlgsum:1"}
    assert store.summary_inserts == 1
    assert sum(1 for item in results if item.created) == 1


@pytest.mark.asyncio
async def test_offline_partial_then_retry_completes_archive() -> None:
    store = _MemoryUniqueStore()
    summary = DialogueSummary(
        subject_id="child-1",
        text="聊了恐龙",
        source_event_id="end:1",
        session_id="sess-old",
    )
    first = await store.ensure_summary(summary)
    assert first.created is True
    for index in range(25):
        await store.ensure_summary(
            DialogueSummary(
                subject_id="child-1",
                text=f"新会话{index}",
                source_event_id=f"end:{index}",
                session_id=f"sess-new-{index}",
            )
        )
    retry = await store.ensure_summary(summary)
    assert retry.created is False
    assert retry.record.record_id == first.record.record_id
    fact = ArchiveFact(subject_id="child-1", field="preference", text="喜欢恐龙")
    written = await store.upsert_archive_fact(fact)
    again = await store.upsert_archive_fact(fact)
    assert written.record_id == again.record_id
    assert store.archive_inserts == 1
    assert store.summary_inserts == 26


@pytest.mark.asyncio
async def test_offline_cross_subject_same_session_id() -> None:
    store = _MemoryUniqueStore()
    a = await store.ensure_summary(
        DialogueSummary(
            subject_id="child-a",
            text="A",
            source_event_id="e",
            session_id="shared",
        )
    )
    b = await store.ensure_summary(
        DialogueSummary(
            subject_id="child-b",
            text="B",
            source_event_id="e",
            session_id="shared",
        )
    )
    assert a.record.record_id != b.record.record_id
    assert store.summary_inserts == 2


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_postgres_ensure_summary_is_idempotent_under_concurrency(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    from services.memory_scope.tests import test_child_layers_postgres as helpers

    store, bootstrap, admin_db_dsn = await helpers._layers(pg_env)
    try:
        summary = DialogueSummary(
            subject_id=helpers.CHILD,
            text="孩子聊了幂等",
            source_event_id="evt-idem",
            session_id="sess-idem-concurrent",
        )

        async def _once() -> EnsuredChildLayer:
            return await store.ensure_summary(
                summary,
                resource_owner_id=helpers.PARENT,
                actor_id=helpers.PARENT,
                consent_snapshot_id="consent-idem",
            )

        results = await asyncio.gather(*[_once() for _ in range(8)])
        ids = {item.record.record_id for item in results}
        assert len(ids) == 1
        assert sum(1 for item in results if item.created) == 1
        listed = await store.list(
            subject_id=helpers.CHILD,
            kind=KIND_DIALOGUE_SUMMARY,
            viewer_subject_id=helpers.PARENT,
            limit=100,
        )
        matching = [item for item in listed if item.session_id == "sess-idem-concurrent"]
        assert len(matching) == 1
    finally:
        await helpers._close_layers(store, bootstrap, admin_db_dsn)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_postgres_partial_resume_and_old_session_retry(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    from services.memory_scope.tests import test_child_layers_postgres as helpers

    store, bootstrap, admin_db_dsn = await helpers._layers(pg_env)
    try:
        old = await store.ensure_summary(
            DialogueSummary(
                subject_id=helpers.CHILD,
                text="旧会话摘要",
                source_event_id="evt-old",
                session_id="sess-old",
            ),
            resource_owner_id=helpers.PARENT,
            actor_id=helpers.PARENT,
            consent_snapshot_id="consent-old",
        )
        assert old.created is True
        for index in range(25):
            await store.ensure_summary(
                DialogueSummary(
                    subject_id=helpers.CHILD,
                    text=f"新会话{index}",
                    source_event_id=f"evt-new-{index}",
                    session_id=f"sess-new-{index}",
                ),
                resource_owner_id=helpers.PARENT,
                actor_id=helpers.PARENT,
                consent_snapshot_id=f"consent-new-{index}",
            )
        retry = await store.ensure_summary(
            DialogueSummary(
                subject_id=helpers.CHILD,
                text="旧会话摘要",
                source_event_id="evt-old-retry",
                session_id="sess-old",
            ),
            resource_owner_id=helpers.PARENT,
            actor_id=helpers.PARENT,
            consent_snapshot_id="consent-old",
        )
        assert retry.created is False
        assert retry.record.record_id == old.record.record_id
        fact = ArchiveFact(
            subject_id=helpers.CHILD, field="preference", text="喜欢恐龙"
        )
        first_fact = await store.upsert_archive_fact(
            fact,
            resource_owner_id=helpers.PARENT,
            actor_id=helpers.PARENT,
            consent_snapshot_id="consent-arch",
        )
        second_fact = await store.upsert_archive_fact(
            fact,
            resource_owner_id=helpers.PARENT,
            actor_id=helpers.PARENT,
            consent_snapshot_id="consent-arch",
        )
        assert first_fact.record_id == second_fact.record_id
    finally:
        await helpers._close_layers(store, bootstrap, admin_db_dsn)
