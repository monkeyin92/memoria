"""Action-policy authorization at the real side-effect tool seam."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams,
    PolicyObligation,
    PolicyObligationSpec,
)
from services.agent.src.action_policy_client import (
    ActionPolicyClient,
    ActionPolicyClientConfig,
    is_action_policy_capability,
)
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.task_manager import TaskManager, ToolSpec
from services.agent.src.transactional_effect_commit import (
    CommitReceipt,
    CommitReconcileResult,
    CommitReconcileState,
    PreparedToolEffect,
    fence_fingerprint,
)
from services.agent.tests.unit.runtime_profile_test_helpers import owner_profile_for_session
from services.policy.action_fence import build_action_resource_fence
from services.policy.receipts import PolicyReceiptV2

SESSION_ID = "ses_action_tool"
ENDPOINT = "http://test/v1/interaction/action-policy"
TOKEN = "action-policy-token-that-is-long-enough"


def test_deferred_capability_is_identified_without_profile_allowlisting() -> None:
    assert is_action_policy_capability("memory_capture")
    assert not is_action_policy_capability("chat")


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _fence() -> GenerationFence:
    return GenerationFence(SESSION_ID, 1, 1, 0, session_epoch=1)


def _receipt(*, effect: str = "allow", with_ttl: bool = False) -> PolicyReceiptV2:
    profile = owner_profile_for_session(SESSION_ID).profile
    fence = _fence()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=5)
    action_fence = build_action_resource_fence(
        capability="memory_capture",
        purpose="memory_capture",
        action_resource_id="memory_capture:ses_action_tool:1:1",
        action_revision=1,
        generation_id=fence.generation_id,
        turn_id=fence.turn_id,
        tool_epoch=fence.tool_epoch,
        issued_at=now - timedelta(seconds=1),
        valid_until=expires_at,
    )
    obligations = ()
    if with_ttl:
        obligations = (
            PolicyObligationSpec(
                code=PolicyObligation.POLICY_OBLIGATION_RETENTION_TTL,
                params=ObligationParams(
                    max_session_seconds=None,
                    retention_ttl_seconds=3600,
                    quiet_hours=None,
                    extras=(),
                ),
            ),
            PolicyObligationSpec(
                code=PolicyObligation.POLICY_OBLIGATION_NO_MODEL_TRAINING,
                params=ObligationParams(
                    max_session_seconds=None,
                    retention_ttl_seconds=None,
                    quiet_hours=None,
                    extras=(),
                ),
            ),
            PolicyObligationSpec(
                code=PolicyObligation.POLICY_OBLIGATION_WRITE_POLICY_RECEIPT,
                params=ObligationParams(
                    max_session_seconds=None,
                    retention_ttl_seconds=None,
                    quiet_hours=None,
                    extras=(),
                ),
            ),
        )
    return PolicyReceiptV2(
        receipt_id="receipt_action_tool",
        actor_id=profile.actor_id,
        subject_id=profile.active_subject_id,
        resource_owner_id=profile.active_subject_id,
        device_id=profile.device_id,
        capability="memory_capture",
        purpose="memory_capture",
        effect=effect,
        reason_code="action_allowed",
        obligations=obligations,
        policy_version="test-policy",
        context_hash="c" * 64,
        action_resource_fence=action_fence,
        action_fence_hash=action_fence.canonical_hash,
        consent_snapshot_ids=(),
        consent_snapshot_revisions=(),
        relationship_snapshot_ids=(),
        relationship_snapshot_revisions=(),
        binding_id=profile.binding_id,
        binding_version=profile.binding_version,
        binding_canonical_hash="b" * 64,
        session_id=profile.session_id,
        session_epoch=profile.session_epoch,
        runtime_profile_id=profile.runtime_profile_id,
        subject_revision=profile.subject_revision,
        device_trust="trusted",
        data_classification="private",
        safety_state="normal",
        jurisdiction="CN",
        created_at=_rfc3339(now - timedelta(seconds=1)),
        expires_at=_rfc3339(expires_at),
        exact_fence=True,
    )


class _RecordingCommitPort:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def reconcile(self, *, idempotency_key: str) -> CommitReconcileResult:
        del idempotency_key
        return CommitReconcileResult(CommitReconcileState.NOT_FOUND)

    async def commit_prepared(self, **kwargs: object) -> CommitReceipt:
        self.calls.append(kwargs)
        authorization = kwargs["authorization"]
        prepared = kwargs["prepared"]
        return CommitReceipt(
            intent_id="intent-action-tool",
            idempotency_key=str(kwargs["idempotency_key"]),
            committed_at=datetime.now(UTC),
            fence_fingerprint=fence_fingerprint(authorization.fence),  # type: ignore[attr-defined]
            authority_revision=1,
            capability=authorization.capability,  # type: ignore[attr-defined]
            purpose=authorization.purpose,  # type: ignore[attr-defined]
            resource_id=authorization.resource_id,  # type: ignore[attr-defined]
            intent_sha256=prepared.payload_sha256,  # type: ignore[attr-defined]
            evidence_refs=authorization.evidence_refs,  # type: ignore[attr-defined]
        )


def _manager(
    client: ActionPolicyClient,
    current: list[GenerationFence],
) -> tuple[TaskManager, _RecordingCommitPort, list[int]]:
    manager = TaskManager()
    port = _RecordingCommitPort()
    manager.set_effect_commit_port(port)
    profile = owner_profile_for_session(SESSION_ID)
    manager.set_action_policy_client(
        client,
        profile_for_fence=lambda fence: profile if fence.session_id == SESSION_ID else None,
        current_fence=lambda: current[0],
    )
    prepared_calls: list[int] = []

    async def prepare(
        _args: dict[str, object], _cancel: object
    ) -> PreparedToolEffect:
        prepared_calls.append(1)
        return PreparedToolEffect(intent="memory_capture", payload={"text": "secret"})

    manager.register_side_effect(
        ToolSpec(
            name="capture",
            description="capture memory",
            input_schema={},
            cancellable=True,
            idempotent=True,
            timeout_s=1.0,
            contains_sensitive_data=True,
            side_effect_policy="idempotent",
            required_capability="memory_capture",
            required_purpose="memory_capture",
        ),
        prepare,
    )
    return manager, port, prepared_calls


async def _client_for(
    receipt: PolicyReceiptV2 | None,
    *,
    status: int = 200,
    on_request: object | None = None,
) -> ActionPolicyClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if callable(on_request):
            on_request(request)
        if receipt is None:
            return httpx.Response(status, json={"detail": {"code": "denied"}})
        return httpx.Response(status, json=receipt.model_dump(mode="json"))

    return ActionPolicyClient(
        ActionPolicyClientConfig(ENDPOINT, TOKEN, timeout_s=1.0),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0),
    )


@pytest.mark.asyncio
async def test_side_effect_tool_requests_current_action_policy_before_commit() -> None:
    fence = _fence()
    client = await _client_for(_receipt())
    manager, port, prepared_calls = _manager(client, [fence])
    try:
        record = await manager.start(
            "capture",
            {},
            fence,
            task_epoch=1,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 10_000,
            side_effect_policy="idempotent",
            committed=True,
        )
        await record.task
    finally:
        await client.aclose()

    assert prepared_calls == [1]
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_side_effect_tool_denial_never_prepares_or_commits() -> None:
    fence = _fence()
    client = await _client_for(None, status=403)
    manager, port, prepared_calls = _manager(client, [fence])
    try:
        with pytest.raises(PermissionError, match="action policy"):
            await manager.start(
                "capture",
                {},
                fence,
                task_epoch=1,
                context_version=1,
                expires_at_ms=int(time.time() * 1_000) + 10_000,
                side_effect_policy="idempotent",
                committed=True,
            )
    finally:
        await client.aclose()

    assert prepared_calls == []
    assert port.calls == []


@pytest.mark.asyncio
async def test_allow_with_obligations_is_executed_before_side_effect_commit() -> None:
    fence = _fence()
    client = await _client_for(_receipt(effect="allow_with_obligations", with_ttl=True))
    manager, port, prepared_calls = _manager(client, [fence])
    try:
        record = await manager.start(
            "capture",
            {},
            fence,
            task_epoch=1,
            context_version=1,
            expires_at_ms=int(time.time() * 1_000) + 10_000,
            side_effect_policy="idempotent",
            committed=True,
        )
        await record.task
    finally:
        await client.aclose()

    assert prepared_calls == [1]
    assert len(port.calls) == 1
    authorization = port.calls[0]["authorization"]  # type: ignore[index]
    assert set(authorization.obligations) == {  # type: ignore[attr-defined]
        "RETENTION_TTL",
        "NO_MODEL_TRAINING",
        "WRITE_POLICY_RECEIPT",
    }


@pytest.mark.asyncio
async def test_policy_success_followed_by_fence_advance_never_prepares() -> None:
    fence = _fence()
    current = [fence]

    def advance_after_policy(_request: httpx.Request) -> None:
        current[0] = fence.bump_tool_epoch()

    client = await _client_for(_receipt(), on_request=advance_after_policy)
    manager, port, prepared_calls = _manager(client, current)
    try:
        with pytest.raises(PermissionError, match="fence"):
            await manager.start(
                "capture",
                {},
                fence,
                task_epoch=1,
                context_version=1,
                expires_at_ms=int(time.time() * 1_000) + 10_000,
                side_effect_policy="idempotent",
                committed=True,
            )
    finally:
        await client.aclose()

    assert prepared_calls == []
    assert port.calls == []
