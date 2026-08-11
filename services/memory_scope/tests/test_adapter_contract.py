"""Shared proposal-lifecycle adapter contract (main-review A-E).

The SAME scenario functions run against every durable adapter: in-memory
and SQLite are parametrized here, PostgreSQL reuses the same functions in
``test_postgres_store.py`` against the real FORCE-RLS database.  Coverage:
pending revoke freeze, approvals_complete revoke freeze, pending is never
directly promotable, freeze signature consistency, and a lost freeze CAS
returns ``False`` so the service reports the REAL terminal state instead of
misreporting ``frozen``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from services.memory_scope.domain import (
    ConfirmationVote,
    MemoryAuditEvent,
    MemoryOutboxEvent,
    MemoryScope,
    SharedMemoryProposal,
)
from services.memory_scope.in_memory_store import InMemoryMemoryStore
from services.memory_scope.sqlite_store import SqliteMemoryStore


def _proposal(proposal_id: str | None = None) -> SharedMemoryProposal:
    # Unique per scenario: the PostgreSQL contract runs every scenario
    # against the SAME database, so ids must never collide.
    proposal_id = proposal_id or f"contract-p1-{uuid.uuid4().hex[:10]}"
    now = datetime.now(UTC)
    return SharedMemoryProposal(
        proposal_id=proposal_id,
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
        fence_context_hash="f" * 64,
        generation_id="gen-1",
        turn_id=1,
        valid_until=now + timedelta(minutes=10),
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
        status="pending",
        created_at=now,
    )


def _confirm(proposal_id: str, subject: str) -> ConfirmationVote:
    return ConfirmationVote(
        proposal_id=proposal_id,
        subject_id=subject,
        decision="confirm",
        evidence_id=f"evidence-{subject}",
        approval_receipt_id=f"approval-{subject}",
        approval_snapshot_id=f"approval-snap-{subject}",
        approval_snapshot_revision=1,
        approval_snapshot_hash="a" * 64,
    )


def _freeze_args(proposal_id: str) -> dict[str, object]:
    now = datetime.now(UTC)
    return dict(
        proposal_id=proposal_id,
        actor_subject_id="person-a",
        family_space_id="family-1",
        reason="authorization revoked",
        audit=(
            MemoryAuditEvent(
                event_id=f"audit:{proposal_id}:frozen:authorization",
                action="memory.shared.frozen",
                actor_subject_id="person-a",
                subject_id="person-a",
                record_id=None,
                proposal_id=proposal_id,
                payload={"reason": "authorization revoked"},
                created_at=now,
            ),
        ),
        outbox=(
            MemoryOutboxEvent(
                outbox_id=f"{proposal_id}:frozen:authorization",
                event_id=f"{proposal_id}:frozen:authorization",
                topic="memory.shared.frozen",
                payload={"proposal_id": proposal_id},
                created_at=now,
            ),
        ),
        now=now,
    )


async def _promote(store: object, proposal: SharedMemoryProposal) -> bool:
    return await store.finalize_promotion_atomically(  # type: ignore[attr-defined]
        proposal.proposal_id,
        promotion_receipt_id="promo-1",
        promotion_fence_context_hash="h" * 64,
        required_subject_ids=("person-a", "person-b"),
        approval_revisions=(
            ("person-a", "approval-snap-person-a", 1, "a" * 64),
            ("person-b", "approval-snap-person-b", 1, "a" * 64),
        ),
        actor_subject_id="person-a",
        family_space_id="family-1",
        audit=(),
        now=datetime.now(UTC),
    )


async def scenario_pending_freeze(store: object) -> None:
    """A pending proposal freezes atomically with a stable audit/outbox
    pair; a replay is an idempotent no-op (False)."""
    proposal = _proposal()
    await store.persist_proposal(  # type: ignore[attr-defined]
        proposal, actor_family_space_id="family-1"
    )
    frozen = await store.freeze_proposal_atomically(  # type: ignore[attr-defined]
        **_freeze_args(proposal.proposal_id)
    )
    assert frozen is True
    row = await store.get_proposal(  # type: ignore[attr-defined]
        proposal.proposal_id,
        actor_subject_id="person-a",
        actor_family_space_id="family-1",
    )
    assert row is not None and row.status == "frozen"
    assert (
        await store.freeze_proposal_atomically(  # type: ignore[attr-defined]
            proposal_id=proposal.proposal_id,
            actor_subject_id="person-a",
            family_space_id="family-1",
            reason="again",
            audit=(),
            outbox=(),
            now=datetime.now(UTC),
        )
        is False
    )


async def scenario_approvals_complete_freeze(store: object) -> None:
    """A proposal that reached approvals_complete (all confirms, not yet
    promoted) still freezes - objection/revocation must remain possible
    there (canonical contract)."""
    proposal = _proposal()
    await store.persist_proposal(  # type: ignore[attr-defined]
        proposal, actor_family_space_id="family-1"
    )
    assert (
        await store.vote_and_transition(  # type: ignore[attr-defined]
            _confirm(proposal.proposal_id, "person-a"),
            actor_family_space_id="family-1",
            audit=(),
            now=datetime.now(UTC),
        )
        == "pending"
    )
    assert (
        await store.vote_and_transition(  # type: ignore[attr-defined]
            _confirm(proposal.proposal_id, "person-b"),
            actor_family_space_id="family-1",
            audit=(),
            now=datetime.now(UTC),
        )
        == "approvals_complete"
    )
    frozen = await store.freeze_proposal_atomically(  # type: ignore[attr-defined]
        **_freeze_args(proposal.proposal_id)
    )
    assert frozen is True
    row = await store.get_proposal(  # type: ignore[attr-defined]
        proposal.proposal_id,
        actor_subject_id="person-a",
        actor_family_space_id="family-1",
    )
    assert row is not None and row.status == "frozen"


async def scenario_pending_not_directly_promotable(store: object) -> None:
    """A pending proposal is NEVER promoted directly: the promotion
    finalizer only accepts the durable approvals_complete state."""
    proposal = _proposal()
    await store.persist_proposal(  # type: ignore[attr-defined]
        proposal, actor_family_space_id="family-1"
    )
    assert await _promote(store, proposal) is False
    row = await store.get_proposal(  # type: ignore[attr-defined]
        proposal.proposal_id,
        actor_subject_id="person-a",
        actor_family_space_id="family-1",
    )
    assert row is not None and row.status == "pending"
    records = await store.list_records_for_subject(  # type: ignore[attr-defined]
        "person-a",
        scopes=(MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,),
        actor_subject_id="person-a",
        actor_family_space_id="family-1",
    )
    assert records == ()


async def scenario_freeze_lost_cas_reports_real_state(store: object) -> None:
    """After a successful promotion the freeze CAS is LOST (returns
    False): the service must report the real terminal state (promoted),
    never a false ``frozen``.  The same applies to already-frozen /
    withdrawn proposals."""
    proposal = _proposal()
    await store.persist_proposal(  # type: ignore[attr-defined]
        proposal, actor_family_space_id="family-1"
    )
    await store.vote_and_transition(  # type: ignore[attr-defined]
        _confirm(proposal.proposal_id, "person-a"),
        actor_family_space_id="family-1",
        audit=(),
        now=datetime.now(UTC),
    )
    await store.vote_and_transition(  # type: ignore[attr-defined]
        _confirm(proposal.proposal_id, "person-b"),
        actor_family_space_id="family-1",
        audit=(),
        now=datetime.now(UTC),
    )
    assert await _promote(store, proposal) is True
    row = await store.get_proposal(  # type: ignore[attr-defined]
        proposal.proposal_id,
        actor_subject_id="person-a",
        actor_family_space_id="family-1",
    )
    assert row is not None and row.status == "promoted"
    assert (
        await store.freeze_proposal_atomically(  # type: ignore[attr-defined]
            proposal_id=proposal.proposal_id,
            actor_subject_id="person-a",
            family_space_id="family-1",
            reason="late",
            audit=(),
            outbox=(),
            now=datetime.now(UTC),
        )
        is False
    )


@pytest.fixture
async def adapter_store(request: pytest.FixtureRequest):
    kind = request.param
    if kind == "in_memory":
        store = InMemoryMemoryStore()
        await store.initialize()
    else:
        store = SqliteMemoryStore(
            request.getfixturevalue("tmp_path") / "contract.db"
        )
        await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_store", ["in_memory", "sqlite"], indirect=True)
async def test_pending_freeze_contract(adapter_store: object) -> None:
    await scenario_pending_freeze(adapter_store)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_store", ["in_memory", "sqlite"], indirect=True)
async def test_approvals_complete_freeze_contract(adapter_store: object) -> None:
    await scenario_approvals_complete_freeze(adapter_store)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_store", ["in_memory", "sqlite"], indirect=True)
async def test_pending_not_directly_promotable_contract(
    adapter_store: object,
) -> None:
    await scenario_pending_not_directly_promotable(adapter_store)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_store", ["in_memory", "sqlite"], indirect=True)
async def test_freeze_lost_cas_reports_real_state_contract(
    adapter_store: object,
) -> None:
    await scenario_freeze_lost_cas_reports_real_state(adapter_store)
