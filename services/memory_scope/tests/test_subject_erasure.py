"""Subject erasure through the append-only memory scope store (SQLite).

A bound subject (child/elder, no account) is erased as subject and as
co-subject; the owner's and another subject's rows survive unchanged, the
append-only guards still reject every ordinary delete/update afterwards, and
a second erase is a no-op.  The scenario helpers are shared with the
PostgreSQL contract (``test_subject_erasure_postgres``).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import pytest
from services.governance.subject_ports import SubjectMemoryScopePort
from services.memory_scope.domain import (
    ConfirmationVote,
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    SharedMemoryProposal,
)
from services.memory_scope.sqlite_store import SqliteMemoryStore

OWNER = "person-owner"
CHILD = "person-child"
ELDER = "person-elder"
FAMILY = "family-erase"
CHILD_EVIDENCE = "evidence-child-1"

VoteKeys = Callable[[], Awaitable[set[tuple[str, str, str]]]]


class ErasableStore(Protocol):
    async def erase_subject(self, *, subject_id: str) -> dict[str, int]: ...

    async def remaining_subject_rows(self, *, subject_id: str) -> dict[str, int]: ...


def _record(
    record_id: str,
    subject_id: str,
    *,
    co_subject_ids: tuple[str, ...] = (),
    evidence: str = "evidence-1",
    shared_proposal_id: str | None = None,
) -> MemoryRecord:
    shared = bool(co_subject_ids)
    return MemoryRecord(
        record_id=record_id,
        scope=(
            MemoryScope.MEMORY_SCOPE_FAMILY_SHARED
            if shared
            else MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        ),
        subject_id=subject_id,
        resource_owner_id=FAMILY if shared else subject_id,
        family_space_id=FAMILY if shared else None,
        co_subject_ids=co_subject_ids,
        source_evidence_ids=(evidence,),
        policy_receipt_id=f"receipt-{record_id}",
        consent_snapshot_id="consent-1",
        created_by_actor_id=subject_id,
        payload={"text": f"what {subject_id} said in {record_id}"},
        created_at=datetime.now(UTC),
        shared_proposal_id=shared_proposal_id,
    )


def _proposal(proposal_id: str, co_subject_ids: tuple[str, ...]) -> SharedMemoryProposal:
    return SharedMemoryProposal(
        proposal_id=proposal_id,
        family_space_id=FAMILY,
        proposer_subject_id=OWNER,
        co_subject_ids=co_subject_ids,
        binding_version=1,
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_role="primary_subject",
        runtime_profile_id="profile-1",
        device_id="device-1",
        subject_revision=0,
        fence_context_hash="f" * 64,
        title=f"trip {proposal_id}",
        content=f"the family trip told by {', '.join(co_subject_ids)}",
        source_evidence_ids=("evidence-1",),
        proposal_policy_receipt_id=f"receipt-{proposal_id}",
        consent_snapshot_id="consent-1",
        proposal_revision=1,
        capture_evidence_hash="c" * 64,
        consent_snapshot_revision=1,
        consent_snapshot_hash="b" * 64,
        membership_snapshot_id="membership:family-erase:1",
        membership_snapshot_revision=1,
        membership_snapshot_hash="a" * 64,
        status="pending",
        created_at=datetime.now(UTC),
    )


def _confirm(proposal_id: str, subject_id: str) -> ConfirmationVote:
    return ConfirmationVote(
        proposal_id=proposal_id,
        subject_id=subject_id,
        decision="confirm",
        approval_receipt_id=f"approval-{proposal_id}-{subject_id}",
        approval_snapshot_id=f"snapshot-{subject_id}",
        approval_snapshot_revision=1,
        approval_snapshot_hash="e" * 64,
    )


def _audit(event_id: str, *, subject_id: str, record_id: str | None, payload: dict[str, object]) -> MemoryAuditEvent:
    return MemoryAuditEvent(
        event_id=event_id,
        action="memory.capture",
        actor_subject_id=subject_id,
        subject_id=subject_id,
        record_id=record_id,
        proposal_id=None,
        payload=payload,
        created_at=datetime.now(UTC),
    )


def _outbox(outbox_id: str, payload: dict[str, object]) -> MemoryOutboxEvent:
    return MemoryOutboxEvent(
        outbox_id=outbox_id,
        event_id=outbox_id,
        topic="memory.record.captured",
        payload=payload,
        created_at=datetime.now(UTC),
    )


@dataclass(frozen=True, slots=True)
class Seeded:
    #: record id -> the reader that may see it (actor, family context).
    survivors: dict[str, tuple[str, str | None]]
    erased: dict[str, tuple[str, str | None]]


async def seed_subject_scenario(personal: object, family: object) -> Seeded:
    """Write rows for the child (as subject and co-subject), the owner and an
    elder through the real store API (``persist_*`` / ``append_*``).

    ``family`` carries every family-context call and ``personal`` every
    other one (the same object for SQLite): a pooled PostgreSQL connection
    that once set ``app.memory.family_space_id`` reads it back as '' rather
    than NULL, which hides personal rows from later calls on it.
    """

    async def record(item: MemoryRecord, outbox: tuple[MemoryOutboxEvent, ...] = (), audit: tuple[MemoryAuditEvent, ...] = ()) -> None:
        store = family if item.family_space_id else personal
        await store.persist_record(  # type: ignore[attr-defined]
            item,
            actor_family_space_id=item.family_space_id,
            status_events=(
                MemoryRecordStatusEvent(
                    event_id=f"{item.record_id}:captured",
                    record_id=item.record_id,
                    status="confirmed",
                    reason_code="captured",
                    created_at=item.created_at,
                ),
            ),
            outbox=outbox,
            audit=audit,
        )

    await family.persist_proposal(  # type: ignore[attr-defined]
        _proposal("proposal-child", (CHILD,)),
        actor_family_space_id=FAMILY,
        outbox=(_outbox("out-proposal-child", {"proposal_id": "proposal-child", "co_subject_ids": [CHILD]}),),
    )
    await family.persist_proposal(  # type: ignore[attr-defined]
        _proposal("proposal-elder", (ELDER,)),
        actor_family_space_id=FAMILY,
        outbox=(_outbox("out-proposal-elder", {"proposal_id": "proposal-elder", "co_subject_ids": [ELDER]}),),
    )
    await family.persist_vote(_confirm("proposal-child", OWNER))  # type: ignore[attr-defined]
    await family.persist_vote(  # type: ignore[attr-defined]
        ConfirmationVote(proposal_id="proposal-child", subject_id=CHILD, decision="object")
    )
    await family.persist_vote(_confirm("proposal-elder", ELDER))  # type: ignore[attr-defined]

    await record(
        _record("r-child", CHILD, evidence=CHILD_EVIDENCE),
        outbox=(_outbox("out-r-child", {"record_id": "r-child", "subject_id": CHILD}),),
        audit=(_audit("audit-r-child", subject_id=CHILD, record_id="r-child", payload={"scope": "personal_private"}),),
    )
    await record(
        _record("r-shared-child", OWNER, co_subject_ids=(CHILD,), shared_proposal_id="proposal-child"),
        audit=(_audit("audit-r-shared-child", subject_id=OWNER, record_id="r-shared-child", payload={"co_subject_ids": [CHILD]}),),
    )
    await record(
        _record("r-owner", OWNER),
        outbox=(_outbox("out-r-owner", {"record_id": "r-owner", "subject_id": OWNER}),),
        audit=(_audit("audit-r-owner", subject_id=OWNER, record_id="r-owner", payload={"scope": "personal_private"}),),
    )
    await record(_record("r-elder", ELDER))
    await record(_record("r-shared-elder", OWNER, co_subject_ids=(ELDER,)))
    return Seeded(
        survivors={
            "r-owner": (OWNER, None),
            "r-elder": (ELDER, None),
            "r-shared-elder": (OWNER, FAMILY),
        },
        erased={
            "r-child": (CHILD, None),
            "r-shared-child": (OWNER, FAMILY),
        },
    )


async def _read(
    stores: tuple[object, object], record_id: str, reader: tuple[str, str | None]
) -> MemoryRecord | None:
    actor, family = reader
    store = stores[1] if family else stores[0]
    return await store.get_record(  # type: ignore[attr-defined, no-any-return]
        record_id, actor_subject_id=actor, actor_family_space_id=family
    )


async def assert_subject_erasure(
    store: tuple[object, object],
    erasure: ErasableStore,
    seeded: Seeded,
    vote_keys: VoteKeys,
    *,
    extra_erased: dict[str, int] | None = None,
) -> None:
    before = {rid: await _read(store, rid, reader) for rid, reader in seeded.survivors.items()}
    assert all(item is not None for item in before.values())
    for rid, reader in seeded.erased.items():
        assert await _read(store, rid, reader) is not None
    assert await erasure.remaining_subject_rows(subject_id=CHILD) != {}

    counts = await erasure.erase_subject(subject_id=CHILD)

    expected = {
        "memory_records": 2,
        "memory_status_events": 2,
        "memory_shared_proposals": 1,
        # The owner's confirm and the child's objection on the child's proposal.
        "memory_shared_votes": 2,
        # proposal-child + r-child payloads; out-r-child was already dispatched.
        "memory_outbox": 2,
        "memory_outbox_dispatched": 1,
        # audit-r-child (subject) + audit-r-shared-child (record + payload).
        "memory_audit_events": 2,
        **(extra_erased or {}),
    }
    assert {table: counts.get(table) for table in expected} == expected
    for rid, reader in seeded.erased.items():
        assert await _read(store, rid, reader) is None
    for rid, reader in seeded.survivors.items():
        # Byte-for-byte unchanged, including payload, evidence and status.
        assert await _read(store, rid, reader) == before[rid]
    assert await vote_keys() == {("proposal-elder", ELDER, "confirm")}
    assert await erasure.remaining_subject_rows(subject_id=CHILD) == {}
    assert await erasure.remaining_subject_rows(subject_id=ELDER) != {}

    again = await erasure.erase_subject(subject_id=CHILD)
    assert set(again.values()) == {0}
    assert await erasure.remaining_subject_rows(subject_id=CHILD) == {}


# -- SQLite ------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteMemoryStore]:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    yield store
    await store.close()


def _sqlite_vote_keys(store: SqliteMemoryStore) -> VoteKeys:
    async def keys() -> set[tuple[str, str, str]]:
        rows = store._connect().execute(  # noqa: SLF001
            "SELECT proposal_id, subject_id, decision FROM memory_shared_votes"
        )
        return {(row[0], row[1], row[2]) for row in rows}

    return keys


async def test_sqlite_erases_subject_and_keeps_others(store: SqliteMemoryStore) -> None:
    port: SubjectMemoryScopePort = store
    assert port is store
    seeded = await seed_subject_scenario(store, store)
    await store.mark_outbox_processed("out-r-child")
    await assert_subject_erasure((store, store), store, seeded, _sqlite_vote_keys(store))

    connection = store._connect()  # noqa: SLF001
    outbox = {
        row["outbox_id"]: (row["payload"], row["status"])
        for row in connection.execute("SELECT * FROM memory_outbox")
    }
    assert outbox["out-r-child"] == ("{}", "processed")
    assert outbox["out-proposal-child"] == ("{}", "processed")
    assert outbox["out-r-owner"][1] == "pending" and OWNER in outbox["out-r-owner"][0]
    audit = {
        row["event_id"]: row
        for row in connection.execute("SELECT * FROM memory_audit_events")
    }
    # Content-free audit rows are kept; the erasure itself is audited once.
    assert audit["audit-r-child"]["payload"] == "{}"
    assert audit["audit-r-child"]["subject_id"] == CHILD
    assert audit["audit-r-owner"]["payload"] != "{}"
    assert audit[f"memory.subject.erased:{CHILD}"]["payload"] == "{}"


async def test_sqlite_append_only_survives_erasure(
    store: SqliteMemoryStore, tmp_path: Path
) -> None:
    await seed_subject_scenario(store, store)
    connection = store._connect()  # noqa: SLF001
    # Outside an erase even the subject's own rows cannot be deleted.
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM memory_records WHERE record_id = 'r-child'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM memory_shared_votes WHERE subject_id = ?", (CHILD,))
    await store.erase_subject(subject_id=CHILD)
    for statement in (
        "DELETE FROM memory_records WHERE record_id = 'r-owner'",
        "UPDATE memory_records SET payload = '{}' WHERE record_id = 'r-owner'",
        "DELETE FROM memory_status_events WHERE record_id = 'r-owner'",
        "DELETE FROM memory_shared_votes WHERE subject_id = 'person-elder'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)
    # Another connection never has the permit function: it fails closed.
    foreign = sqlite3.connect(tmp_path / "memory.db")
    try:
        with pytest.raises(sqlite3.OperationalError, match="memory_subject_erase_permits"):
            foreign.execute("DELETE FROM memory_records WHERE record_id = 'r-owner'")
    finally:
        foreign.close()
    assert await _read((store, store), "r-owner", (OWNER, None)) is not None


async def test_sqlite_upgrades_unguarded_delete_triggers(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    first = SqliteMemoryStore(path)
    await first.initialize()
    await first.close()
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        DROP TRIGGER trg_memory_records_no_delete;
        CREATE TRIGGER trg_memory_records_no_delete
        BEFORE DELETE ON memory_records
        BEGIN
            SELECT RAISE(ABORT, 'memory_records is append-only');
        END;
        """
    )
    legacy.close()
    store = SqliteMemoryStore(path)
    await store.initialize()
    try:
        await seed_subject_scenario(store, store)
        assert (await store.erase_subject(subject_id=CHILD))["memory_records"] == 2
        assert await store.remaining_subject_rows(subject_id=CHILD) == {}
    finally:
        await store.close()


async def test_sqlite_rejects_unbounded_subject(store: SqliteMemoryStore) -> None:
    with pytest.raises(ValueError, match="bounded"):
        await store.erase_subject(subject_id=" ")
    with pytest.raises(ValueError, match="bounded"):
        await store.remaining_subject_rows(subject_id="x" * 129)
