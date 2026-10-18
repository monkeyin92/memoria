"""Real-PostgreSQL contract for the B02 write gate on child memory layers.

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import os
import uuid

import asyncpg
import pytest
from services.guardian.child_memory import ArchiveFact, DialogueSummary
from services.memory_scope.child_layers import MemoryPurposeStopped
from services.memory_scope.tests import test_postgres_store
from services.memory_scope.tests.test_child_layers_postgres import (
    _close_layers,
    _layers,
)

pg_env = test_postgres_store.pg_env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
        reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
    ),
]

PARENT = "parent-gate"
CHILD = "child-gate"
OTHER = "child-gate-other"


async def _head(
    admin: asyncpg.Connection, subject: str, scope_id: str, state: str = "active"
) -> uuid.UUID:
    head = uuid.uuid4()
    await admin.execute(
        "INSERT INTO memory_purpose_heads(purpose_scope_id, subject_id, resource_owner_id,"
        " purpose, authority_scope_type, authority_scope_id, state, stopped_at)"
        " VALUES ($1, $2, $3, 'memory_use', 'binding', $4, $5,"
        " CASE WHEN $5 = 'stopped' THEN now() END)",
        head,
        subject,
        PARENT,
        scope_id,
        state,
    )
    return head


def _summary(subject: str, event: str) -> DialogueSummary:
    return DialogueSummary(
        subject_id=subject, text="孩子聊了天气", source_event_id=event, session_id="s"
    )


async def _write(store, subject: str, event: str):  # type: ignore[no-untyped-def]
    return await store.write_summary(
        _summary(subject, event),
        resource_owner_id=PARENT,
        actor_id=PARENT,
        consent_snapshot_id="consent-1",
    )


async def test_writes_follow_the_purpose_head(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        # no heads: legacy behaviour, writes pass
        await _write(store, CHILD, "e0")
        head = await _head(admin, CHILD, "bind-1")
        await _write(store, CHILD, "e1")  # open head
        await admin.execute(
            "UPDATE memory_purpose_heads SET state='stopped', stopped_at=now(),"
            " restriction_revision = restriction_revision + 1 WHERE purpose_scope_id=$1",
            head,
        )
        with pytest.raises(MemoryPurposeStopped):
            await _write(store, CHILD, "e2")
        with pytest.raises(MemoryPurposeStopped):
            await store.write_archive_fact(
                ArchiveFact(subject_id=CHILD, field="pet_name", text="旺财"),
                resource_owner_id=PARENT,
                actor_id=PARENT,
                consent_snapshot_id="consent-1",
            )
        # another subject is untouched
        await _write(store, OTHER, "e3")
        # a second, still-open binding for the same subject keeps writes alive
        await _head(admin, CHILD, "bind-2")
        await _write(store, CHILD, "e4")
        # an expired head is closed
        await admin.execute(
            "UPDATE memory_purpose_heads SET state='active', stopped_at=NULL,"
            " restriction_revision = restriction_revision + 1, valid_until = now() - interval '1 second'"
            " WHERE purpose_scope_id=$1",
            head,
        )
        # both heads now closed (one stopped earlier, one expired): writes stop
        await admin.execute(
            "UPDATE memory_purpose_heads SET valid_until = now() - interval '1 second',"
            " restriction_revision = restriction_revision + 1"
            " WHERE authority_scope_id = 'bind-2'"
        )
        with pytest.raises(MemoryPurposeStopped):
            await _write(store, CHILD, "e5")
    finally:
        await admin.close()
        await _close_layers(store, bootstrap, admin_db_dsn)


async def test_touch_refresh_is_blocked_after_stop(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        fact = ArchiveFact(subject_id=CHILD, field="pet_name", text="旺财")
        first = await store.upsert_archive_fact(
            fact, resource_owner_id=PARENT, actor_id=PARENT, consent_snapshot_id="consent-1"
        )
        head = await _head(admin, CHILD, "bind-t")
        await admin.execute(
            "UPDATE memory_purpose_heads SET state='stopped', stopped_at=now(),"
            " restriction_revision = restriction_revision + 1 WHERE purpose_scope_id=$1",
            head,
        )
        with pytest.raises(MemoryPurposeStopped):
            await store.upsert_archive_fact(
                fact,
                resource_owner_id=PARENT,
                actor_id=PARENT,
                consent_snapshot_id="consent-1",
            )
        again = await store.get(
            subject_id=CHILD, record_id=first.record_id, viewer_subject_id=PARENT
        )
        assert again is not None and again.payload == first.payload  # not refreshed
    finally:
        await admin.close()
        await _close_layers(store, bootstrap, admin_db_dsn)


async def test_memory_purpose_open_reads_the_heads(
    pg_env: tuple[str, str, str, str, str],
) -> None:
    store, bootstrap, admin_db_dsn = await _layers(pg_env)
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        assert await store.memory_purpose_open(subject_id=CHILD) is True  # no heads
        head = await _head(admin, CHILD, "bind-r")
        assert await store.memory_purpose_open(subject_id=CHILD) is True
        await admin.execute(
            "UPDATE memory_purpose_heads SET state='stopped', stopped_at=now(),"
            " restriction_revision = restriction_revision + 1 WHERE purpose_scope_id=$1",
            head,
        )
        assert await store.memory_purpose_open(subject_id=CHILD) is False
        assert await store.memory_purpose_open(subject_id=OTHER) is True
    finally:
        await admin.close()
        await _close_layers(store, bootstrap, admin_db_dsn)
