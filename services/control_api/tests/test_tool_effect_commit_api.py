from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from packages.contracts.generated.python.multi_subject_contracts import PolicyReceiptV2
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.transactional_effect_commit import fence_fingerprint
from services.control_api.app.main import create_app
from services.control_api.tests.test_interaction_api import (
    _attach_signed_runtime_profile,
    _configure,
    _identity,
)
from services.policy.action_fence import build_action_resource_fence
from services.session_runtime.service import PersistentSessionUnavailable


def _receipt(profile: Any, now: datetime) -> PolicyReceiptV2:
    fence = GenerationFence(
        session_id=profile.session_id,
        session_epoch=profile.session_epoch,
        generation_id=1,
        turn_id=1,
        tool_epoch=0,
    )
    action_fence = build_action_resource_fence(
        capability="memory_capture",
        purpose="memory_capture",
        action_resource_id="effect-resource",
        action_revision=1,
        generation_id=fence.generation_id,
        turn_id=fence.turn_id,
        tool_epoch=fence.tool_epoch,
        issued_at=now - timedelta(seconds=1),
        valid_until=now + timedelta(minutes=1),
    )
    return PolicyReceiptV2.model_validate(
        {
            "receipt_id": "effect-receipt",
            "actor_id": profile.actor_id,
            "subject_id": profile.active_subject_id,
            "resource_owner_id": profile.active_subject_id,
            "device_id": profile.device_id,
            "capability": "memory_capture",
            "purpose": "memory_capture",
            "effect": "allow",
            "reason_code": "test",
            "obligations": [],
            "policy_version": "test",
            "context_hash": "0" * 64,
            "action_resource_fence": action_fence,
            "action_fence_hash": action_fence.canonical_hash,
            "consent_snapshot_ids": [],
            "consent_snapshot_revisions": [],
            "relationship_snapshot_ids": [],
            "relationship_snapshot_revisions": [],
            "binding_id": profile.binding_id,
            "binding_version": profile.binding_version,
            "binding_canonical_hash": "0" * 64,
            "session_id": profile.session_id,
            "session_epoch": profile.session_epoch,
            "runtime_profile_id": profile.runtime_profile_id,
            "subject_revision": profile.subject_revision,
            "device_trust": "trusted",
            "data_classification": "private",
            "safety_state": "normal",
            "jurisdiction": "CN",
            "created_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=1)).isoformat(),
            "exact_fence": True,
        }
    )


def _body(profile: Any, receipt: PolicyReceiptV2, now: datetime) -> dict[str, object]:
    fence = GenerationFence(
        session_id=profile.session_id,
        session_epoch=profile.session_epoch,
        generation_id=1,
        turn_id=1,
        tool_epoch=0,
    )
    payload = {"value": "prepared"}
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return {
        "session_id": profile.session_id,
        "fence": {
            "session_id": profile.session_id,
            "session_epoch": profile.session_epoch,
            "generation_id": 1,
            "turn_id": 1,
            "tool_epoch": 0,
        },
        "runtime_profile": profile.model_dump(mode="json"),
        "policy_receipt": receipt.model_dump(mode="json"),
        "capability": "memory_capture",
        "purpose": "memory_capture",
        "resource_id": receipt.action_resource_fence.action_resource_id,
        "evidence_refs": [receipt.receipt_id],
        "idempotency_key": "effect-api-key",
        "intent": "memory_capture",
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "fence_fingerprint": fence_fingerprint(fence),
    }


@pytest.mark.asyncio
async def test_tool_effect_commit_requires_token_and_uses_authenticated_session_actor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        user_id, headers = await _identity(client)
        created = await client.post("/v1/sessions", headers=headers, json={})
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        profile = _attach_signed_runtime_profile(
            app,
            user_id=user_id,
            session_id=session_id,
        )
        now = datetime.now(UTC)
        receipt = _receipt(profile, now)
        seen: dict[str, object] = {}

        class Service:
            async def commit_tool_effect(self, command: Any) -> dict[str, object]:
                seen["actor_id"] = command.actor_id
                return {
                    "intent_id": "intent-api",
                    "idempotency_key": command.idempotency_key,
                    "committed_at": now.isoformat(),
                    "fence_fingerprint": command.fence_fingerprint,
                    "authority_revision": 1,
                    "capability": command.capability,
                    "purpose": command.purpose,
                    "resource_id": command.resource_id,
                    "intent_sha256": command.payload_sha256,
                    "evidence_refs": list(command.evidence_refs),
                }

        app.state.session_runtime_service = Service()
        body = _body(profile, receipt, now)
        unauthorized = await client.post(
            "/v1/interaction/tool-effect/commit",
            headers=headers,
            json=body,
        )
        assert unauthorized.status_code == 401

        authorized = await client.post(
            "/v1/interaction/tool-effect/commit",
            headers={
                **headers,
                "X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough",
            },
            json=body,
        )
        assert authorized.status_code == 200
        assert seen["actor_id"] == user_id


@pytest.mark.asyncio
async def test_tool_effect_reconcile_authority_failure_is_unknown_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch, tmp_path)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        class Service:
            async def reconcile_tool_effect(self, *, idempotency_key: str) -> dict[str, object]:
                raise PersistentSessionUnavailable("db down")

        app.state.session_runtime_service = Service()
        response = await client.post(
            "/v1/interaction/tool-effect/reconcile",
            headers={
                "X-Memoria-Internal-Token": "interaction-policy-token-that-is-long-enough",
            },
            json={"idempotency_key": "effect-api-key"},
        )
    assert response.status_code == 503
    assert response.json()["detail"]["state"] == "unknown"
