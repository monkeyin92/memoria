"""A device session acts for the one person the device is bound to."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from services.policy.context import context_hash
from services.policy.engine import PolicyEngine
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_relationship,
)
from services.policy.tests.test_engine import _context

NOW = datetime(2026, 8, 9, tzinfo=UTC)


def _minor(capability: str, **overrides: Any) -> Any:
    purpose = {"memory_recall_private": "memory_recall"}.get(capability, capability)
    consent = make_consent(
        capability=capability,  # type: ignore[arg-type]
        purpose=purpose,
        subject_id="person-child",
        actor_id="person-parent",
        actor_kind="guardian",
        now=NOW,
    )
    values: dict[str, Any] = {
        "capability": capability,
        "actor_id": "person-parent",
        "subject_id": "person-child",
        "resource_owner_id": "person-child",
        "subject_category": "minor",
        "age_band": "under_14",
        "speaker_state": "confirmed",
        "declared_device_mode": "parent_for_child",
        "current_session_mode": "student_minor",
        "data_classification": "private",
        "evaluated_at": NOW,
        "consent_evidence": (consent,),
        "relationship_evidence": (
            make_relationship(relation_type="guardian_of", now=NOW),
        ),
        "binding_evidence": make_binding(declared_mode="parent_for_child", now=NOW),
    }
    values.update(overrides)
    return _context(**values)


@pytest.mark.parametrize("capability", ["memory_recall_private", "memory_capture"])
def test_child_on_own_device_uses_own_memory_with_guardian_consent(capability: str) -> None:
    decision = PolicyEngine().decide(_minor(capability, subject_presence="device_bound"))

    assert decision.effect == "allow_with_obligations"


@pytest.mark.parametrize(
    ("capability", "reason"),
    [
        ("memory_recall_private", "private_memory_subject_only"),
        ("memory_capture", "memory_consent_required"),
    ],
)
def test_guardian_app_session_still_cannot_read_the_childs_memory(
    capability: str, reason: str
) -> None:
    # Same consent, same guardian: but the guardian, not the child, is talking.
    decision = PolicyEngine().decide(_minor(capability))

    assert (decision.effect, decision.reason_code) == ("deny", reason)


def test_device_bound_child_still_needs_the_guardian_consent() -> None:
    decision = PolicyEngine().decide(
        _minor("memory_recall_private", subject_presence="device_bound", consent_evidence=())
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "memory_consent_required"


def test_device_bound_child_still_needs_an_active_guardian() -> None:
    decision = PolicyEngine().decide(
        _minor(
            "memory_recall_private",
            subject_presence="device_bound",
            relationship_evidence=(
                make_relationship(relation_type="guardian_of", status="pending", now=NOW),
            ),
        )
    )

    assert decision.effect == "deny"
    assert decision.reason_code == "guardian_evidence_required"


def test_device_bound_child_may_use_tutor_on_own_device() -> None:
    decision = PolicyEngine().decide(
        _minor("tutor", subject_presence="device_bound", data_classification="public")
    )
    guardian_session = PolicyEngine().decide(_minor("tutor", data_classification="public"))

    assert decision.effect == "allow_with_obligations"
    assert guardian_session.reason_code == "tutor_subject_mismatch"


def _elder(**overrides: Any) -> Any:
    consent = make_consent(
        capability="memory_recall_private",
        purpose="memory_recall",
        subject_id="person-elder",
        actor_id="person-son",
        actor_kind="delegate",
        now=NOW,
    )
    values: dict[str, Any] = {
        "capability": "memory_recall_private",
        "actor_id": "person-son",
        "subject_id": "person-elder",
        "resource_owner_id": "person-elder",
        "subject_category": "adult",
        "age_band": "adult",
        "speaker_state": "confirmed",
        "declared_device_mode": "child_for_parent",
        "current_session_mode": "senior_companion",
        "data_classification": "private",
        "evaluated_at": NOW,
        "subject_presence": "device_bound",
        "consent_evidence": (consent,),
        "relationship_evidence": (
            make_relationship(
                relation_type="delegate_for",
                source_person_id="person-son",
                target_person_id="person-elder",
                now=NOW,
            ),
        ),
        "binding_evidence": make_binding(declared_mode="child_for_parent", now=NOW),
    }
    values.update(overrides)
    return _context(**values)


def test_elder_on_own_device_uses_memory_under_the_childs_delegation() -> None:
    decision = PolicyEngine().decide(_elder())

    assert decision.effect == "allow_with_obligations"
    assert decision.reason_code == "private_memory_subject_authorized"


def test_elder_memory_needs_the_actors_own_delegation() -> None:
    someone_else = make_relationship(
        relation_type="delegate_for",
        source_person_id="person-other",
        target_person_id="person-elder",
        now=NOW,
    )
    missing = PolicyEngine().decide(_elder(relationship_evidence=()))
    foreign = PolicyEngine().decide(_elder(relationship_evidence=(someone_else,)))

    assert missing.reason_code == "delegate_evidence_required"
    assert foreign.reason_code == "delegate_evidence_required"


def test_elder_memory_is_not_readable_from_the_childs_app() -> None:
    decision = PolicyEngine().decide(_elder(subject_presence="resolved"))

    assert decision.reason_code == "private_memory_subject_only"


def test_device_bound_requires_a_confirmed_subject() -> None:
    with pytest.raises(ValueError, match="device-bound"):
        _minor("chat", subject_presence="device_bound", speaker_state="unconfirmed")


def test_default_presence_keeps_existing_receipt_hashes() -> None:
    resolved = _minor("memory_recall_private")
    bound = _minor("memory_recall_private", subject_presence="device_bound")

    assert context_hash(resolved) != context_hash(bound)
    # The default is not part of the hash payload: an explicitly defaulted
    # context hashes exactly like one built before the field existed.
    assert context_hash(resolved) == context_hash(
        _minor("memory_recall_private", subject_presence="resolved")
    )
