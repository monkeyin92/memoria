from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from packages.contracts.generated.python import multi_subject_contracts as contracts
from services.policy.engine import PolicyEngine
from services.policy.migration import LegacyPolicyReceiptRecord, decode_legacy_policy_receipt
from services.policy.producer import require_policy_producer
from services.policy.tests.fakes import make_context


def _legacy_receipt_wire() -> dict[str, object]:
    now = datetime(2026, 8, 10, tzinfo=UTC)
    return {
        "receipt_id": "legacy-receipt-1",
        "actor_id": "person-1",
        "subject_id": "person-1",
        "device_id": "device-1",
        "capability": "chat",
        "purpose": "user_request",
        "effect": "allow",
        "reason_code": "legacy_allow",
        "obligations": [],
        "policy_version": "legacy-v1",
        "context_hash": "a" * 64,
        "binding_id": "binding-1",
        "binding_version": 1,
        "session_id": "session-1",
        "session_epoch": 1,
        "runtime_profile_id": "profile-1",
        "subject_revision": 0,
        "created_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        ),
    }


def test_engine_producer_paths_call_generated_lifecycle_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    original = contracts.require_new_producer_contract

    def observing_guard(name: str) -> None:
        calls.append(name)
        original(name)

    monkeypatch.setattr(contracts, "require_new_producer_contract", observing_guard)
    engine = PolicyEngine()
    context = make_context()

    decision = engine.decide(context)
    engine.receipt_for(context, decision)

    assert calls == ["PolicyDecision", "PolicyReceiptV2"]


@pytest.mark.parametrize("legacy_name", ["PolicyReceipt", "RuntimeProfile"])
def test_new_v1_producer_is_rejected_by_generated_guard(legacy_name: str) -> None:
    with pytest.raises(ValueError, match="forbidden for new producers"):
        require_policy_producer(legacy_name)


def test_legacy_receipt_decoder_returns_immutable_non_authorizing_record() -> None:
    record = decode_legacy_policy_receipt(_legacy_receipt_wire())

    assert record.source_contract == "PolicyReceipt"
    assert record.disposition == "migration_only"
    assert record.may_authorize is False
    assert record.payload.receipt_id == "legacy-receipt-1"
    with pytest.raises((AttributeError, TypeError)):
        record.may_authorize = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        LegacyPolicyReceiptRecord(  # type: ignore[call-arg]
            payload=record.payload,
            may_authorize=True,
        )
