from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from services.policy.action_fence import (
    build_action_resource_fence,
    build_approval_snapshot_fence,
)
from services.policy.engine import PolicyEngine, privacy_action_requires_allow_receipt
from services.policy.tests.fakes import (
    FakeApprovalEvidence,
    FakeCaptureEvidence,
    FakeMembershipEvidence,
    FakeProposalEvidence,
    make_binding,
    make_consent,
    make_context,
)

NOW = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)


def _base_context(capability: str, fence: object, **overrides: object):
    consent = make_consent(
        snapshot_id="consent-snapshot-1",
        version=4,
        canonical_hash="c" * 64,
        subject_id="subject-a",
        actor_id="subject-a",
        resource_owner_id="owner-1",
        capability=capability,  # type: ignore[arg-type]
        purpose=capability,
        now=NOW,
    )
    values: dict[str, object] = {
        "actor_id": "subject-a",
        "subject_id": "subject-a",
        "resource_owner_id": "owner-1",
        "capability": capability,
        "purpose": capability,
        "current_session_mode": "family_shared",
        "consent_evidence": (consent,),
        "binding_evidence": make_binding(now=NOW),
        "membership_evidence": FakeMembershipEvidence(
            snapshot_id="membership-snapshot-1",
            revision=5,
            canonical_hash="d" * 64,
        ),
        "proposal_evidence": FakeProposalEvidence(),
        "generation_id": 7,
        "turn_id": 8,
        "tool_epoch": 9,
        "action_resource_fence": fence,
        "evaluated_at": NOW,
    }
    values.update(overrides)
    return make_context(**values)  # type: ignore[arg-type]


def _family_fence(capability: str, **overrides: object):
    values: dict[str, object] = {
        "capability": capability,
        "purpose": capability,
        "action_resource_id": "proposal-1",
        "action_revision": 1,
        "family_space_id": "family-1",
        "family_owner_subject_id": "owner-1",
        "proposal_id": "proposal-1",
        "proposal_revision": 1,
        "required_approval_subject_ids": ("subject-a", "subject-b"),
        "consent_snapshot_id": "consent-snapshot-1",
        "consent_snapshot_revision": 4,
        "consent_snapshot_hash": "c" * 64,
        "membership_snapshot_id": "membership-snapshot-1",
        "membership_snapshot_revision": 5,
        "membership_snapshot_hash": "d" * 64,
        "generation_id": 7,
        "turn_id": 8,
        "tool_epoch": 9,
        "issued_at": NOW,
        "valid_until": NOW + timedelta(minutes=5),
    }
    values.update(overrides)
    return build_action_resource_fence(**values)  # type: ignore[arg-type]


def test_family_proposal_uses_distinct_exact_receipt() -> None:
    capture = FakeCaptureEvidence()
    fence = _family_fence(
        "family_shared_memory_proposal",
        capture_evidence_records=((
            capture.evidence_id,
            capture.revision,
            capture.canonical_hash,
        ),),
    )
    decision = PolicyEngine().decide(
        _base_context(
            "family_shared_memory_proposal",
            fence,
            capture_evidence=(capture,),
        )
    )

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "family_shared_memory_proposal_authorized"


def test_memory_promotion_is_distinct_from_capture() -> None:
    fence = build_action_resource_fence(
        capability="memory_promotion",
        purpose="memory_promotion",
        action_resource_id="candidate-memory-1",
        action_revision=2,
        consent_snapshot_id="consent-snapshot-1",
        consent_snapshot_revision=4,
        consent_snapshot_hash="c" * 64,
        generation_id=7,
        turn_id=8,
        tool_epoch=9,
        issued_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
    )
    context = _base_context(
        "memory_promotion",
        fence,
        actor_id="subject-a",
        subject_id="subject-a",
        resource_owner_id="subject-a",
        consent_evidence=(
            make_consent(
                snapshot_id="consent-snapshot-1",
                version=4,
                canonical_hash="c" * 64,
                subject_id="subject-a",
                resource_owner_id="subject-a",
                actor_id="subject-a",
                capability="memory_promotion",
                purpose="memory_promotion",
                now=NOW,
            ),
        ),
    )

    decision = PolicyEngine().decide(context)

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "memory_promotion_authorized"


@pytest.mark.parametrize(
    ("capability", "wrong_purpose"),
    [
        ("memory_capture", "memory_promotion"),
        ("memory_promotion", "memory_capture"),
        ("family_shared_memory_proposal", "family_shared_memory_approval"),
        ("family_shared_memory_approval", "family_shared_memory_promotion"),
        ("family_shared_memory_promotion", "family_shared_memory_proposal"),
    ],
)
def test_five_canonical_action_pairs_are_not_interchangeable(
    capability: str, wrong_purpose: str
) -> None:
    with pytest.raises(ValueError, match="canonical action"):
        make_context(
            capability=capability,  # type: ignore[arg-type]
            purpose=wrong_purpose,
        )


def test_family_approval_binds_voter_and_proposal_revision() -> None:
    fence = _family_fence(
        "family_shared_memory_approval",
        voter_subject_id="subject-a",
        approval_decision="confirm",
    )
    engine = PolicyEngine()

    allowed = engine.decide(_base_context("family_shared_memory_approval", fence))
    wrong_voter = engine.decide(
        _base_context(
            "family_shared_memory_approval",
            fence,
            actor_id="subject-b",
            subject_id="subject-b",
        )
    )
    wrong_revision_fence = _family_fence(
        "family_shared_memory_approval",
        action_revision=2,
        proposal_revision=2,
        voter_subject_id="subject-a",
        approval_decision="confirm",
    )
    wrong_revision = engine.decide(
        _base_context("family_shared_memory_approval", wrong_revision_fence)
    )

    assert allowed.effect == "allow_with_obligations"
    assert wrong_voter.effect == "deny"
    assert wrong_revision.effect == "deny"


def test_family_promotion_requires_complete_unique_current_votes() -> None:
    approvals = (
        FakeApprovalEvidence("approval-a", 1, "a" * 64, "subject-a"),
        FakeApprovalEvidence("approval-b", 2, "b" * 64, "subject-b"),
    )
    fence = _family_fence(
        "family_shared_memory_promotion",
        approval_snapshots=tuple(
            build_approval_snapshot_fence(
                subject_id=item.subject_id,
                snapshot_id=item.snapshot_id,
                revision=item.revision,
                canonical_hash=item.canonical_hash,
            )
            for item in approvals
        ),
    )
    context = _base_context(
        "family_shared_memory_promotion",
        fence,
        approval_evidence=approvals,
    )
    engine = PolicyEngine()

    allowed = engine.decide(context)
    missing_vote = engine.decide(replace(context, approval_evidence=approvals[:1]))
    stale_vote = engine.decide(
        replace(
            context,
            approval_evidence=(replace(approvals[0], revision=9), approvals[1]),
        )
    )

    assert allowed.effect == "allow_with_obligations"
    assert allowed.reason_code == "family_shared_memory_promotion_authorized"
    assert missing_vote.effect == "deny"
    assert stale_vote.effect == "deny"


def test_cross_proposal_and_cross_voter_receipts_are_not_interchangeable() -> None:
    approval = FakeApprovalEvidence("approval-a", 1, "a" * 64, "subject-a")
    fence = _family_fence(
        "family_shared_memory_promotion",
        proposal_id="proposal-2",
        action_resource_id="proposal-2",
        approval_snapshots=(
            build_approval_snapshot_fence(
                subject_id="subject-a",
                snapshot_id="approval-a",
                revision=1,
                canonical_hash="a" * 64,
            ),
            build_approval_snapshot_fence(
                subject_id="subject-b",
                snapshot_id="approval-b",
                revision=2,
                canonical_hash="b" * 64,
            ),
        ),
    )

    decision = PolicyEngine().decide(
        _base_context(
            "family_shared_memory_promotion",
            fence,
            approval_evidence=(approval,),
        )
    )

    assert decision.effect == "deny"


@pytest.mark.parametrize("action", ["object", "withdraw"])
def test_privacy_fail_closed_actions_do_not_require_allow_receipt(action: str) -> None:
    assert not privacy_action_requires_allow_receipt(action)
