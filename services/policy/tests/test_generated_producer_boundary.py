from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest
from packages.contracts.generated.python import multi_subject_contracts as generated
from services.policy.decision_service import DecisionService
from services.policy.engine import PolicyEngine
from services.policy.receipt_store import InMemoryPolicyReceiptRepository
from services.policy.tests.fakes import make_context
from services.policy.wire import decision_to_wire, wire_to_receipt

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
POLICY_ROOT = Path(__file__).parents[1]


def test_engine_decide_returns_generated_policy_decision() -> None:
    decision = PolicyEngine().decide(make_context(evaluated_at=NOW))

    assert isinstance(decision, generated.PolicyDecision)


def test_engine_receipt_for_returns_generated_policy_receipt_v2() -> None:
    engine = PolicyEngine()
    context = make_context(evaluated_at=NOW)
    decision = engine.decide(context)

    receipt = engine.receipt_for(context, decision)

    assert isinstance(receipt, generated.PolicyReceiptV2)


@pytest.mark.asyncio
async def test_decision_service_and_in_memory_repository_keep_generated_types() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)

    decision = await service.decide_and_persist(make_context(evaluated_at=NOW))
    receipt = await repository.get(decision.receipt_id)

    assert isinstance(decision, generated.PolicyDecision)
    assert isinstance(receipt, generated.PolicyReceiptV2)


def test_wire_decode_returns_generated_receipt() -> None:
    engine = PolicyEngine()
    context = make_context(evaluated_at=NOW)
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    decoded = wire_to_receipt(decision_to_wire(decision, receipt))

    assert isinstance(decoded, generated.PolicyReceiptV2)


def test_generated_outputs_roundtrip_through_generated_model_validate() -> None:
    engine = PolicyEngine()
    context = make_context(evaluated_at=NOW)
    decision = engine.decide(context)
    receipt = engine.receipt_for(context, decision)

    assert (
        generated.PolicyDecision.model_validate(decision.model_dump(mode="json"))
        == decision
    )
    assert (
        generated.PolicyReceiptV2.model_validate(receipt.model_dump(mode="json"))
        == receipt
    )


def test_new_producer_modules_do_not_define_local_canonical_names() -> None:
    forbidden_definitions: list[str] = []
    deprecated_imports: list[str] = []
    for path in POLICY_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name in {
                "PolicyDecision",
                "PolicyReceiptV2",
            }:
                forbidden_definitions.append(f"{path.name}:{node.name}")
            if isinstance(node, ast.ImportFrom) and node.module == (
                "packages.contracts.generated.python.multi_subject_contracts"
            ):
                for alias in node.names:
                    if alias.name in {"PolicyReceipt", "RuntimeProfile"}:
                        deprecated_imports.append(f"{path.name}:{alias.name}")

    assert forbidden_definitions == []
    assert deprecated_imports == []


def test_new_producer_modules_do_not_bypass_generated_validation() -> None:
    bypasses: list[str] = []
    for path in POLICY_ROOT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if ".model_copy(" in source:
            bypasses.append(path.name)

    assert bypasses == []
