from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from packages.contracts.generated.python.multi_subject_contracts import PolicyReceiptV2
from services.agent.src import policy_runtime_wiring
from services.agent.src.config import AgentSettings
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.task_manager import ToolSpec
from services.agent.src.transactional_effect_commit import (
    CommitReconcileState,
    EffectAuthorization,
    PreparedToolEffect,
    TransactionalToolEffectCommitClient,
    fence_fingerprint,
)
from services.agent.tests.unit.runtime_profile_test_helpers import owner_profile_for_session
from services.policy.action_fence import build_action_resource_fence


def _authorized_effect(now: datetime) -> tuple[EffectAuthorization, PreparedToolEffect]:
    verified_profile = owner_profile_for_session("effect-client-session")
    fence = GenerationFence(
        session_id=verified_profile.profile.session_id,
        session_epoch=verified_profile.profile.session_epoch,
        generation_id=1,
        turn_id=1,
        tool_epoch=0,
    )
    action_fence = build_action_resource_fence(
        capability="memory_capture",
        purpose="memory_capture",
        action_resource_id="resource-1",
        action_revision=1,
        generation_id=fence.generation_id,
        turn_id=fence.turn_id,
        tool_epoch=fence.tool_epoch,
        issued_at=now - timedelta(seconds=1),
        valid_until=now + timedelta(minutes=1),
    )
    receipt = PolicyReceiptV2.model_validate(
        {
            "receipt_id": "receipt-effect-1",
            "actor_id": verified_profile.profile.actor_id,
            "subject_id": verified_profile.profile.active_subject_id,
            "resource_owner_id": verified_profile.profile.active_subject_id,
            "device_id": verified_profile.profile.device_id,
            "capability": "memory_capture",
            "purpose": "memory_capture",
            "effect": "allow",
            "reason_code": "test",
            "obligations": [],
            "policy_version": "test-policy",
            "context_hash": "0" * 64,
            "action_resource_fence": action_fence,
            "action_fence_hash": action_fence.canonical_hash,
            "consent_snapshot_ids": [],
            "consent_snapshot_revisions": [],
            "relationship_snapshot_ids": [],
            "relationship_snapshot_revisions": [],
            "binding_id": verified_profile.profile.binding_id,
            "binding_version": verified_profile.profile.binding_version,
            "binding_canonical_hash": "0" * 64,
            "session_id": verified_profile.profile.session_id,
            "session_epoch": verified_profile.profile.session_epoch,
            "runtime_profile_id": verified_profile.profile.runtime_profile_id,
            "subject_revision": verified_profile.profile.subject_revision,
            "device_trust": "trusted",
            "data_classification": "private",
            "safety_state": "normal",
            "jurisdiction": "CN",
            "created_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=1)).isoformat(),
            "exact_fence": True,
        }
    )
    authorization = EffectAuthorization(
        fence=fence,
        capability="memory_capture",
        purpose="memory_capture",
        resource_id="resource-1",
        evidence_refs=(receipt.receipt_id,),
        runtime_profile=verified_profile,
        policy_receipt=receipt,
    )
    return authorization, PreparedToolEffect(
        intent="memory_capture",
        payload={"text": "hello"},
    )


def _spec() -> ToolSpec:
    return ToolSpec(
        name="memory_capture",
        description="test",
        input_schema={},
        cancellable=True,
        idempotent=True,
        timeout_s=1.0,
        side_effect_policy="write_once",
        required_capability="memory_capture",
        required_purpose="memory_capture",
    )


@pytest.mark.asyncio
async def test_commit_client_sends_signed_profile_and_policy_receipt() -> None:
    now = datetime.now(UTC)
    authorization, prepared = _authorized_effect(now)
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["token"] = request.headers.get("X-Memoria-Internal-Token")
        return httpx.Response(
            200,
            json={
                "intent_id": "intent-1",
                "idempotency_key": "key-1",
                "committed_at": now.isoformat(),
                "fence_fingerprint": fence_fingerprint(authorization.fence),
                "authority_revision": 1,
                "capability": "memory_capture",
                "purpose": "memory_capture",
                "resource_id": "resource-1",
                "intent_sha256": prepared.payload_sha256,
                "evidence_refs": ["receipt-effect-1"],
            },
        )

    client = TransactionalToolEffectCommitClient(
        endpoint="http://control/v1/interaction/tool-effect/commit",
        reconcile_endpoint="http://control/v1/interaction/tool-effect/reconcile",
        internal_token="token",
        transport=httpx.MockTransport(handler),
    )
    receipt = await client.commit_prepared(
        spec=_spec(),
        authorization=authorization,
        prepared=prepared,
        idempotency_key="key-1",
        now=now,
    )

    assert receipt is not None
    body = seen["body"]
    assert isinstance(body, dict)
    profile_wire = body["runtime_profile"]
    policy_wire = body["policy_receipt"]
    assert isinstance(profile_wire, dict)
    assert isinstance(policy_wire, dict)
    assert authorization.runtime_profile is not None
    assert profile_wire["signature"] == authorization.runtime_profile.signature
    assert policy_wire["receipt_id"] == "receipt-effect-1"
    assert body["evidence_refs"] == ["receipt-effect-1"]
    assert seen["token"] == "token"
    await client.aclose()


@pytest.mark.asyncio
async def test_commit_client_rejects_non_contract_response_and_unknown_authority() -> None:
    now = datetime.now(UTC)
    authorization, prepared = _authorized_effect(now)

    async def malformed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"intent_id": "only-one-field"})

    client = TransactionalToolEffectCommitClient(
        endpoint="http://control/v1/interaction/tool-effect/commit",
        reconcile_endpoint="http://control/v1/interaction/tool-effect/reconcile",
        internal_token="token",
        transport=httpx.MockTransport(malformed),
    )
    assert (
        await client.commit_prepared(
            spec=_spec(),
            authorization=authorization,
            prepared=prepared,
            idempotency_key="key-2",
            now=now,
        )
        is None
    )
    await client.aclose()

    async def unavailable(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "authority unavailable"})

    client = TransactionalToolEffectCommitClient(
        endpoint="http://control/v1/interaction/tool-effect/commit",
        reconcile_endpoint="http://control/v1/interaction/tool-effect/reconcile",
        internal_token="token",
        transport=httpx.MockTransport(unavailable),
    )
    outcome = await client.reconcile(idempotency_key="key-2")
    assert outcome.state is CommitReconcileState.UNKNOWN
    await client.aclose()


@pytest.mark.asyncio
async def test_production_policy_wiring_installs_commit_port_only_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMORIA_INTERACTION_POLICY_TOKEN", "interaction-policy-token")
    runtime = DuplexRuntime.create(session_id="wiring-effect-session")
    profile = owner_profile_for_session(runtime.session_id)
    policy = replace(
        ModePolicy.companion_for_test(
            policy_version="test",
            private_context=True,
            owner_evidence=True,
            tools=True,
            voice_profile=False,
            shadow_low_sensitivity_persona=False,
        ),
        runtime_profile=profile,
    )

    class FakeModeClient:
        def __init__(self, _config: object) -> None:
            self.closed = False

        async def fetch(self, *, session_id: str) -> ModePolicy:
            assert session_id == runtime.session_id
            return policy

        async def aclose(self) -> None:
            self.closed = True

    class FakeActionClient:
        def __init__(self, _config: object) -> None:
            return None

        async def authorize(self, **_kwargs: object) -> object:
            return None

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(policy_runtime_wiring, "ModePolicyClient", FakeModeClient)
    monkeypatch.setattr(policy_runtime_wiring, "ActionPolicyClient", FakeActionClient)
    settings = AgentSettings(
        _env_file=None,
        environment="development",
        interaction_policy_token="interaction-policy-token",
        transactional_effect_commit_url="http://control/v1/interaction/tool-effect/commit",
        transactional_effect_reconcile_url="http://control/v1/interaction/tool-effect/reconcile",
    )
    _mode, _action, effect = await policy_runtime_wiring.install_runtime_policy_clients(
        runtime=runtime,
        settings=settings,
        session_id=runtime.session_id,
        offline=False,
    )
    assert effect is not None
    assert runtime.orchestrator.task_manager._effect_commit_port is effect
    await effect.aclose()
    await runtime.close()
