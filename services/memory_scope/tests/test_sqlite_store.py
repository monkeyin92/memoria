"""SQLite adapter contract: append-only, withdrawal visibility, outbox."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.memory_scope.domain import (
    AlreadyVotedError,
    ConfirmationVote,
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryRecord,
    MemoryRecordStatusEvent,
    MemoryScope,
    SharedMemoryProposal,
)
from services.memory_scope.sqlite_store import SqliteMemoryStore


def _record(record_id: str = "r1", *, scope: MemoryScope = MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        scope=scope,
        subject_id="person-a",
        resource_owner_id="person-a",
        source_evidence_ids=("evidence-1",),
        policy_receipt_id="receipt-1",
        consent_snapshot_id="consent-1",
        created_by_actor_id="person-a",
    )


@pytest.fixture
async def store(tmp_path) -> SqliteMemoryStore:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.initialize()
    yield store
    await store.close()


class TestRecordPersistence:
    async def test_persist_and_read_roundtrip(self, store: SqliteMemoryStore) -> None:
        record = _record()
        await store.persist_record(
            record,
            actor_family_space_id=None,
            status_events=(
                MemoryRecordStatusEvent(
                    event_id="e1",
                    record_id=record.record_id,
                    status="confirmed",
                    reason_code="captured",
                    created_at=datetime.now(UTC),
                ),
            ),
        )
        fetched = await store.get_record(
            record.record_id, actor_subject_id="person-a"
        )
        assert fetched is not None
        assert fetched.scope is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        assert fetched.status == "confirmed"
        assert fetched.subject_id == "person-a"

    async def test_append_only_trigger_blocks_updates(
        self, store: SqliteMemoryStore
    ) -> None:
        record = _record()
        await store.persist_record(record, actor_family_space_id=None)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._connect().execute(
                "UPDATE memory_records SET scope = 'family_shared'"
                " WHERE record_id = ?",
                (record.record_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._connect().execute(
                "DELETE FROM memory_records WHERE record_id = ?",
                (record.record_id,),
            )

    async def test_withdrawal_makes_record_invisible(
        self, store: SqliteMemoryStore
    ) -> None:
        record = _record()
        await store.persist_record(
            record,
            actor_family_space_id=None,
            status_events=(
                MemoryRecordStatusEvent(
                    event_id="e1",
                    record_id=record.record_id,
                    status="confirmed",
                    reason_code="captured",
                    created_at=datetime.now(UTC),
                ),
            ),
        )
        await store.persist_record(
            _record("other"),
            actor_family_space_id=None,
            status_events=(),
        )
        # Withdraw via a new status event (append-only transition).
        await store.persist_withdrawal(
            "proposal-1",
            actor_subject_id="person-a",
            record_id=record.record_id,
            status_events=(
                MemoryRecordStatusEvent(
                    event_id="e2",
                    record_id=record.record_id,
                    status="revoked",
                    reason_code="shared_memory_withdrawn",
                    created_at=datetime.now(UTC),
                ),
            ),
        )
        assert (
            await store.get_record(record.record_id, actor_subject_id="person-a")
            is not None
        )
        visible = await store.list_records_for_subject(
            "person-a", actor_subject_id="person-a"
        )
        assert all(item.record_id != record.record_id for item in visible)
        assert any(item.record_id == "other" for item in visible)
        # include_revoked reveals it for audit/ops only.
        all_records = await store.list_records_for_subject(
            "person-a", actor_subject_id="person-a", include_revoked=True
        )
        assert any(item.record_id == record.record_id for item in all_records)
        revoked = await store.get_record(
            record.record_id, actor_subject_id="person-a"
        )
        assert revoked is not None
        assert revoked.status == "revoked"
        assert revoked.withdrawn_at is not None

    async def test_list_filters_by_subject_and_family(
        self, store: SqliteMemoryStore
    ) -> None:
        await store.persist_record(_record("r-a"), actor_family_space_id=None)
        family = _record("r-fam", scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED)
        family = replace(
            family,
            family_space_id="family-1",
            co_subject_ids=("person-b",),
        )
        await store.persist_record(family, actor_family_space_id="family-1")

        mine = await store.list_records_for_subject(
            "person-b",
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id="person-b",
            actor_family_space_id="family-1",
        )
        assert [r.record_id for r in mine] == ["r-fam"]
        family_records = await store.list_records_in_family(
            "family-1",
            "person-b",
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id="person-b",
            actor_family_space_id="family-1",
        )
        assert [r.record_id for r in family_records] == ["r-fam"]
        # A subject outside the family cannot reach the record.
        assert (
            await store.list_records_in_family(
                "family-2",
                "person-z",
                scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
                actor_subject_id="person-z",
                actor_family_space_id="family-2",
            )
            == ()
        )


class TestVotesAndOutbox:
    async def test_double_vote_rejected_by_primary_key(
        self, store: SqliteMemoryStore
    ) -> None:
        proposal = SharedMemoryProposal(
            proposal_id="p1",
            family_space_id="family-1",
            proposer_subject_id="person-a",
            co_subject_ids=("person-b",),
            binding_version=1,
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_role="primary_subject",
            runtime_profile_id="profile-1",
            device_id="device-1",
            subject_revision=0,
            fence_context_hash="f"*64,
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            proposal_policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            proposal_revision=1,
            capture_evidence_hash="c" * 64,
            consent_snapshot_revision=1,
            consent_snapshot_hash="b" * 64,
            membership_snapshot_id="membership:family-1:1",
            membership_snapshot_revision=1,
            membership_snapshot_hash="a" * 64,
        )
        await store.persist_proposal(
            proposal, actor_family_space_id="family-1"
        )
        vote = ConfirmationVote(
            proposal_id="p1",
            subject_id="person-b",
            decision="confirm",
            approval_receipt_id="approval-person-b",
            approval_snapshot_id="approval-snap-person-b",
            approval_snapshot_revision=1,
            approval_snapshot_hash="a" * 64,
        )
        await store.persist_vote(vote)
        with pytest.raises(AlreadyVotedError):
            await store.persist_vote(vote)

    async def test_outbox_and_audit_written_atomically(
        self, store: SqliteMemoryStore
    ) -> None:
        record = _record()
        await store.persist_record(
            record,
            actor_family_space_id=None,
            outbox=(
                MemoryOutboxEvent(
                    outbox_id="o1",
                    event_id="r1:captured",
                    topic="memory.record.captured",
                    payload={"record_id": "r1"},
                ),
            ),
            audit=(
                MemoryAuditEvent(
                    event_id="a1",
                    action="memory.capture",
                    actor_subject_id="person-a",
                    subject_id="person-a",
                    record_id="r1",
                    proposal_id=None,
                    payload={"scope": "personal_private"},
                ),
            ),
        )
        pending = await store.list_pending_outbox()
        assert [event.event_id for event in pending] == ["r1:captured"]
        await store.mark_outbox_processed("o1")
        assert await store.list_pending_outbox() == ()


class TestAtomicVotePromotion:
    async def test_vote_uses_authoritative_proposal_row_not_caller_view(
        self, store: SqliteMemoryStore
    ) -> None:
        """P0-6: the store decides promotion from the DB proposal row - a
        caller cannot smuggle a tampered co-subject view."""
        from datetime import UTC

        proposal = SharedMemoryProposal(
            proposal_id="p-auth",
            family_space_id="family-1",
            proposer_subject_id="person-a",
            co_subject_ids=("person-b", "person-c"),
            binding_version=1,
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_role="primary_subject",
            runtime_profile_id="profile-1",
            device_id="device-1",
            subject_revision=0,
            fence_context_hash="f"*64,
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            proposal_policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            proposal_revision=1,
            capture_evidence_hash="c" * 64,
            consent_snapshot_revision=1,
            consent_snapshot_hash="b" * 64,
            membership_snapshot_id="membership:family-1:1",
            membership_snapshot_revision=1,
            membership_snapshot_hash="a" * 64,
        )
        await store.persist_proposal(
            proposal, actor_family_space_id="family-1"
        )
        now = datetime.now(UTC)
        # Voting a+b confirms only those two - the DB row requires a, b AND
        # c, so a caller who believed the co-subjects were only (a,b) must
        # still NOT get a promotion.
        for subject in ("person-a", "person-b"):
            outcome = await store.vote_and_transition(
                ConfirmationVote(
                    proposal_id="p-auth",
                    subject_id=subject,
                    decision="confirm",
                    voted_at=now,
                    approval_receipt_id=f"approval-{subject}",
                    approval_snapshot_id=f"approval-snap-{subject}",
                    approval_snapshot_revision=1,
                    approval_snapshot_hash="a" * 64,
                ),
                actor_family_space_id="family-1",
                audit=(),
                now=now,
            )
            assert outcome == "pending"
        fam = await store.list_records_in_family(
            "family-1",
            "person-b",
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id="person-b",
            actor_family_space_id="family-1",
        )
        assert fam == ()
        proposal_row = await store.get_proposal(
            "p-auth",
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert proposal_row is not None and proposal_row.status == "pending"


async def test_sqlite_naive_timestamps_rejected_at_persistence_boundary(
    tmp_path: Path,
) -> None:
    """_iso/_parse_iso reject naive or malformed timestamps at the store
    boundary - not just at domain construction."""
    from services.memory_scope.sqlite_store import _iso, _parse_iso

    with pytest.raises(ValueError, match="timezone-aware"):
        _iso(datetime(2026, 8, 9, 10, 0))  # naive
    with pytest.raises(ValueError, match="timezone-aware"):
        _parse_iso("2026-08-09T10:00:00")  # naive persisted value
    with pytest.raises(ValueError):
        _parse_iso("not-a-date")
    aware = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    assert _parse_iso(_iso(aware)) == aware

    async def test_atomic_vote_transition_and_promotion(
        self, store: SqliteMemoryStore
    ) -> None:
        from datetime import UTC

        proposal = SharedMemoryProposal(
            proposal_id="p-atomic",
            family_space_id="family-1",
            proposer_subject_id="person-a",
            co_subject_ids=("person-b",),
            binding_version=1,
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_role="primary_subject",
            runtime_profile_id="profile-1",
            device_id="device-1",
            subject_revision=0,
            fence_context_hash="f"*64,
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            proposal_policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            proposal_revision=1,
            capture_evidence_hash="c" * 64,
            consent_snapshot_revision=1,
            consent_snapshot_hash="b" * 64,
            membership_snapshot_id="membership:family-1:1",
            membership_snapshot_revision=1,
            membership_snapshot_hash="a" * 64,
        )
        await store.persist_proposal(
            proposal, actor_family_space_id="family-1"
        )
        now = datetime.now(UTC)
        # Two racing last votes: exactly one promotion.
        results = []
        for subject in ("person-a", "person-b"):
            vote = ConfirmationVote(
                proposal_id="p-atomic",
                subject_id=subject,
                decision="confirm",
                voted_at=now,
                approval_receipt_id=f"approval-{subject}",
                approval_snapshot_id=f"approval-snap-{subject}",
                approval_snapshot_revision=1,
                approval_snapshot_hash="a" * 64,
            )
            outcome = await store.vote_and_transition(
                vote,
                actor_family_space_id="family-1",
                audit=(),
                now=now,
            )
            results.append(outcome)
        assert results.count("confirmed") == 1
        assert results[0] != results[1] or results[0] == "confirmed"
        fam = await store.list_records_in_family(
            "family-1",
            "person-b",
            scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
            actor_subject_id="person-b",
            actor_family_space_id="family-1",
        )
        assert len(fam) == 1
        proposal_row = await store.get_proposal(
            "p-atomic",
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert proposal_row is not None and proposal_row.status == "confirmed"

    async def test_atomic_vote_rolls_back_on_midway_failure(
        self, store: SqliteMemoryStore
    ) -> None:
        from datetime import UTC

        proposal = SharedMemoryProposal(
            proposal_id="p-fail",
            family_space_id="family-1",
            proposer_subject_id="person-a",
            co_subject_ids=("person-b",),
            binding_version=1,
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_role="primary_subject",
            runtime_profile_id="profile-1",
            device_id="device-1",
            subject_revision=0,
            fence_context_hash="f"*64,
            title="t",
            content="c",
            source_evidence_ids=("evidence-1",),
            proposal_policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            proposal_revision=1,
            capture_evidence_hash="c" * 64,
            consent_snapshot_revision=1,
            consent_snapshot_hash="b" * 64,
            membership_snapshot_id="membership:family-1:1",
            membership_snapshot_revision=1,
            membership_snapshot_hash="a" * 64,
        )
        await store.persist_proposal(
            proposal, actor_family_space_id="family-1"
        )
        now = datetime.now(UTC)
        vote = ConfirmationVote(
            proposal_id="p-fail",
            subject_id="person-a",
            decision="confirm",
            voted_at=now,
            approval_receipt_id="approval-person-a",
            approval_snapshot_id="approval-snap-person-a",
            approval_snapshot_revision=1,
            approval_snapshot_hash="a" * 64,
        )
        # Mid-way failure: promote a record whose shared_proposal_id already
        # exists -> unique partial index violation rolls the whole
        # transaction back (vote + transition included).
        existing = MemoryRecord(
            record_id="existing-rec",
            scope=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            subject_id="person-a",
            resource_owner_id="person-a",
            family_space_id="family-1",
            co_subject_ids=("person-b",),
            source_evidence_ids=("evidence-1",),
            proposal_policy_receipt_id="receipt-1",
            consent_snapshot_id="consent-1",
            status="confirmed",
            payload={"content": "c"},
            created_by_actor_id="person-a",
            created_at=now,
            shared_proposal_id="p-fail",
        )
        await store.persist_record(
            existing, actor_family_space_id="family-1"
        )
        # person-a confirms (no promotion yet), then person-b's confirm
        # tries to promote against the existing record -> unique partial
        # index violation rolls the whole transaction back.
        first = await store.vote_and_transition(
            vote,
            actor_family_space_id="family-1",
            audit=(),
            now=now,
        )
        assert first == "pending"
        with pytest.raises(sqlite3.IntegrityError):
            await store.vote_and_transition(
                ConfirmationVote(
                    proposal_id="p-fail",
                    subject_id="person-b",
                    decision="confirm",
                    voted_at=now,
                    approval_receipt_id="approval-person-b",
                    approval_snapshot_id="approval-snap-person-b",
                    approval_snapshot_revision=1,
                    approval_snapshot_hash="a" * 64,
                ),
                actor_family_space_id="family-1",
                audit=(),
                now=now,
            )
        # The failed (second) transaction rolled back: only person-a's vote
        # remains and the proposal is still pending - no promotion, no
        # partial transition.
        votes = await store.list_votes("p-fail", actor_subject_id="person-a")
        assert [vote.subject_id for vote in votes] == ["person-a"]
        subjects = {
            vote.subject_id
            for vote in await store.list_votes(
                "p-fail", actor_subject_id="person-b"
            )
        }
        assert subjects == {"person-a"}
        proposal_row = await store.get_proposal(
            "p-fail",
            actor_subject_id="person-a",
            actor_family_space_id="family-1",
        )
        assert proposal_row is not None and proposal_row.status == "pending"
