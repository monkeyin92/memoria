from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from services.policy.action_fence import (
    build_action_resource_fence,
    build_approval_snapshot_fence,
    verify_action_resource_fence,
)
from services.policy.context import context_hash
from services.policy.engine import PolicyEngine
from services.policy.tests.fakes import make_context
from services.policy.wire import decision_to_wire, wire_to_receipt

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)


def _ordinary_fence():
    return build_action_resource_fence(
        capability="chat",
        purpose="user_request",
        action_resource_id="turn-resource-1",
        action_revision=1,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        issued_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )


def test_builder_returns_generated_fence_with_recomputed_hashes() -> None:
    fence = _ordinary_fence()

    assert fence.__class__.__module__.endswith("multi_subject_contracts")
    assert verify_action_resource_fence(fence)
    assert len(fence.action_evidence_hash) == 64
    assert len(fence.canonical_hash) == 64


def test_caller_supplied_hash_cannot_self_prove_after_field_tamper() -> None:
    fence = _ordinary_fence()
    tampered = fence.model_copy(update={"turn_id": fence.turn_id + 1})

    assert not verify_action_resource_fence(tampered)


def test_promotion_approval_snapshots_are_sorted_and_unique() -> None:
    second = build_approval_snapshot_fence(
        subject_id="subject-b",
        snapshot_id="approval-b",
        revision=2,
        canonical_hash="b" * 64,
    )
    first = build_approval_snapshot_fence(
        subject_id="subject-a",
        snapshot_id="approval-a",
        revision=1,
        canonical_hash="a" * 64,
    )

    fence = build_action_resource_fence(
        capability="family_shared_memory_promotion",
        purpose="family_shared_memory_promotion",
        action_resource_id="proposal-1",
        action_revision=3,
        family_space_id="family-1",
        family_owner_subject_id="owner-1",
        proposal_id="proposal-1",
        proposal_revision=3,
        required_approval_subject_ids=("subject-a", "subject-b"),
        approval_snapshots=(second, first),
        consent_snapshot_id="consent-snapshot-1",
        consent_snapshot_revision=4,
        consent_snapshot_hash="c" * 64,
        membership_snapshot_id="membership-snapshot-1",
        membership_snapshot_revision=5,
        membership_snapshot_hash="d" * 64,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        issued_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )

    assert [item.subject_id for item in fence.approval_snapshots] == [
        "subject-a",
        "subject-b",
    ]
    assert verify_action_resource_fence(fence)


def test_context_decision_and_receipt_bind_same_action_fence() -> None:
    fence = _ordinary_fence()
    context = make_context(
        evaluated_at=NOW,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        action_resource_fence=fence,
    )

    engine = PolicyEngine()
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert decision.action_resource_fence == fence
    assert receipt.action_resource_fence == fence
    assert decision.action_fence_hash == fence.canonical_hash
    assert receipt.action_fence_hash == fence.canonical_hash


def test_context_hash_changes_for_every_action_fence_identity() -> None:
    fence = _ordinary_fence()
    context = make_context(
        evaluated_at=NOW,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        action_resource_fence=fence,
    )
    changed_fence = build_action_resource_fence(
        capability="chat",
        purpose="user_request",
        action_resource_id="turn-resource-2",
        action_revision=1,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        issued_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )

    assert context_hash(context) != context_hash(
        replace(context, action_resource_fence=changed_fence)
    )


def test_generated_action_fence_roundtrips_through_v2_wire() -> None:
    context = make_context(
        evaluated_at=NOW,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        action_resource_fence=_ordinary_fence(),
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    wire = decision_to_wire(decision, receipt)
    decoded = wire_to_receipt(wire)

    assert wire["action_resource_fence"] == _ordinary_fence().model_dump(mode="json")
    assert wire["action_fence_hash"] == _ordinary_fence().canonical_hash
    assert decoded.action_resource_fence == _ordinary_fence()


def test_wire_rejects_action_fence_whose_structure_does_not_match_hash() -> None:
    context = make_context(
        evaluated_at=NOW,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        action_resource_fence=_ordinary_fence(),
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    wire = decision_to_wire(decision, engine.receipt_for(context, decision))
    action = dict(wire["action_resource_fence"])  # type: ignore[arg-type]
    action["turn_id"] = 999
    wire["action_resource_fence"] = action

    import pytest

    with pytest.raises(ValueError):
        wire_to_receipt(wire)


def test_idempotency_scope_includes_action_resource_identity() -> None:
    first_context = make_context(
        evaluated_at=NOW,
        idempotency_key="request-1",
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        action_resource_fence=_ordinary_fence(),
    )
    second_fence = build_action_resource_fence(
        capability="chat",
        purpose="user_request",
        action_resource_id="turn-resource-2",
        action_revision=1,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        issued_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )
    second_context = replace(first_context, action_resource_fence=second_fence)

    engine = PolicyEngine()
    assert engine.decide(first_context).receipt_id != engine.decide(
        second_context
    ).receipt_id
