"""Strict Policy Engine V2 wire seam."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from packages.contracts.generated.python import multi_subject_contracts as generated
from services.policy.engine import PolicyEngine
from services.policy.tests.fakes import make_binding, make_consent, make_context
from services.policy.wire import (
    decision_to_wire,
    obligations_to_wire,
    wire_to_receipt,
)

NOW = datetime(2026, 8, 9, 8, 0, 0, 123456, tzinfo=UTC)


def test_obligations_to_wire_preserves_all_parameter_fields() -> None:
    obligations = (
        generated.PolicyObligationSpec(
            code=generated.PolicyObligation.POLICY_OBLIGATION_MAX_SESSION_SECONDS,
            params=generated.ObligationParams(
                max_session_seconds=1800,
                retention_ttl_seconds=86400,
                quiet_hours=("21:30", "06:30"),
                extras=(("scope", "minor"),),
            ),
        ),
    )

    assert obligations_to_wire(obligations) == [
        {
            "code": "MAX_SESSION_SECONDS",
            "params": {
                "max_session_seconds": 1800,
                "retention_ttl_seconds": 86400,
                "quiet_hours": ["21:30", "06:30"],
                "extras": [["scope", "minor"]],
            },
        }
    ]


def test_decision_to_wire_projects_complete_v2_receipt_in_utc() -> None:
    evaluated_at = datetime(
        2026, 8, 9, 16, 0, 0, 123456, tzinfo=timezone(timedelta(hours=8))
    )
    context = make_context(evaluated_at=evaluated_at)
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-wire-1")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    wire = decision_to_wire(decision, receipt)

    assert set(wire) == {
        "receipt_id",
        "action_resource_fence",
        "action_fence_hash",
        "actor_id",
        "subject_id",
        "resource_owner_id",
        "device_id",
        "capability",
        "purpose",
        "effect",
        "reason_code",
        "obligations",
        "policy_version",
        "context_hash",
        "consent_snapshot_ids",
        "consent_snapshot_revisions",
        "relationship_snapshot_ids",
        "relationship_snapshot_revisions",
        "binding_id",
        "binding_version",
        "binding_canonical_hash",
        "session_id",
        "session_epoch",
        "runtime_profile_id",
        "subject_revision",
        "device_trust",
        "data_classification",
        "safety_state",
        "jurisdiction",
        "created_at",
        "expires_at",
        "exact_fence",
    }
    assert wire["receipt_id"] == "receipt-wire-1"
    assert wire["created_at"] == "2026-08-09T08:00:00.123456Z"
    assert wire["expires_at"] == "2026-08-09T08:05:00.123456Z"
    assert wire["obligations"] == []


def test_decision_to_wire_rejects_mismatched_decision_and_receipt() -> None:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-wire-2")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)
    other_decision = PolicyEngine(receipt_id_factory=lambda: "receipt-other").decide(
        context
    )

    with pytest.raises(ValueError, match="decision and receipt"):
        decision_to_wire(other_decision, receipt)


def test_parameterized_receipt_wire_roundtrip_is_lossless() -> None:
    context = make_context(
        capability="memory_capture",
        purpose="memory_capture",
        consent_evidence=(
            make_consent(
                capability="memory_capture",
                purpose="memory_capture",
                now=NOW,
            ),
        ),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-roundtrip")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    decoded = wire_to_receipt(decision_to_wire(decision, receipt))

    assert decoded == receipt
    retention = next(
        item
        for item in decoded.obligations
        if item.code.value == "RETENTION_TTL"
    )
    assert retention.params.retention_ttl_seconds == 2592000


def test_wire_obligation_without_params_is_rejected() -> None:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-empty-params")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)
    wire = decision_to_wire(decision, receipt)
    wire["obligations"] = [{"code": "NO_MODEL_TRAINING"}]

    with pytest.raises(ValueError, match="exact code and params"):
        wire_to_receipt(wire)


def _valid_wire() -> dict[str, object]:
    context = make_context(evaluated_at=NOW)
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-invalid-cases")
    decision = engine.decide(context)
    return decision_to_wire(decision, engine.receipt_for(context, decision))


def test_wire_rejects_unknown_top_level_key() -> None:
    wire = _valid_wire()
    wire["unknown"] = "value"

    with pytest.raises(ValueError, match="keys mismatch"):
        wire_to_receipt(wire)


@pytest.mark.parametrize(
    "field",
    ["binding_version", "session_epoch", "subject_revision"],
)
def test_wire_rejects_bool_numeric_fields(field: str) -> None:
    wire = _valid_wire()
    wire[field] = True

    with pytest.raises(ValueError, match=field):
        wire_to_receipt(wire)


def test_wire_rejects_bool_snapshot_revision() -> None:
    wire = _valid_wire()
    wire["consent_snapshot_ids"] = ["snapshot-1"]
    wire["consent_snapshot_revisions"] = [True]

    with pytest.raises(ValueError, match="consent_snapshot_revisions"):
        wire_to_receipt(wire)


@pytest.mark.parametrize("field", ["created_at", "expires_at"])
def test_wire_rejects_naive_timestamp(field: str) -> None:
    wire = _valid_wire()
    wire[field] = "2026-08-09T08:00:00"

    with pytest.raises(ValueError, match="RFC3339 UTC"):
        wire_to_receipt(wire)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("capability", "teleport"),
        ("effect", "maybe"),
        ("purpose", "anything"),
        ("device_trust", "almost_trusted"),
        ("data_classification", "secret"),
        ("safety_state", "panic"),
    ],
)
def test_wire_rejects_invalid_enums(field: str, bad_value: str) -> None:
    wire = _valid_wire()
    wire[field] = bad_value

    with pytest.raises(ValueError, match=field):
        wire_to_receipt(wire)


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64])
def test_wire_rejects_invalid_context_hash(digest: str) -> None:
    wire = _valid_wire()
    wire["context_hash"] = digest

    with pytest.raises(ValueError, match="context_hash"):
        wire_to_receipt(wire)


@pytest.mark.parametrize("digest", ["", "a" * 63, "A" * 64, "g" * 64])
def test_wire_rejects_invalid_binding_canonical_hash(digest: str) -> None:
    wire = _valid_wire()
    wire["binding_canonical_hash"] = digest

    with pytest.raises(ValueError, match="binding_canonical_hash"):
        wire_to_receipt(wire)


def test_wire_rejects_obligation_without_code() -> None:
    wire = _valid_wire()
    wire["obligations"] = [{"params": {}}]

    with pytest.raises(ValueError, match="exact code and params"):
        wire_to_receipt(wire)


def test_unknown_subject_deny_receipt_roundtrips_null_resource_owner() -> None:
    context = make_context(
        subject_id=None,
        resource_owner_id=None,
        capability="voice_clone_use",
        current_session_mode="unknown_safe",
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        speaker_confidence=None,
        evaluated_at=NOW,
    )
    engine = PolicyEngine(receipt_id_factory=lambda: "receipt-unknown-deny")
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    wire = decision_to_wire(decision, receipt)

    assert decision.effect == "deny"
    assert wire["resource_owner_id"] is None
    assert wire_to_receipt(wire) == receipt


def test_unknown_safe_chat_obligations_roundtrip_with_complete_params_shape() -> None:
    context = make_context(
        subject_id=None,
        resource_owner_id=None,
        capability="chat",
        current_session_mode="unknown_safe",
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        speaker_confidence=None,
        evaluated_at=NOW,
    )
    engine = PolicyEngine()
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    wire = decision_to_wire(decision, receipt)

    assert [entry["code"] for entry in wire["obligations"]] == [  # type: ignore[index]
        "DO_NOT_PERSIST",
        "DO_NOT_WRITE_LEARNING_PROGRESS",
        "NO_MODEL_TRAINING",
        "REQUIRE_SPEAKER_CONFIRMATION",
    ]
    assert all(
        entry["params"]
        == {
            "max_session_seconds": None,
            "retention_ttl_seconds": None,
            "quiet_hours": None,
            "extras": [],
        }
        for entry in wire["obligations"]  # type: ignore[union-attr]
    )
    assert wire_to_receipt(wire) == receipt
