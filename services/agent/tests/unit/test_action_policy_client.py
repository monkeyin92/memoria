"""Action-time action-policy client and archive evidence-fence tests."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    ObligationParams as GeneratedObligationParams,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyObligation as GeneratedPolicyObligation,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyObligationSpec,
)
from services.agent.src.action_policy_client import (
    EVIDENCE_FENCE_KEYS,
    EVIDENCE_FENCE_SCHEMA,
    ActionFenceRejected,
    ActionPolicyClient,
    ActionPolicyClientConfig,
    ActionPolicyUnavailable,
    DefaultDenyActionPolicy,
    FrozenActionFence,
    VerifiedActionReceipt,
    archive_fence_valid,
    assistant_child_evidence_fence,
    user_event_evidence_fence,
    verify_action_receipt,
    verify_assistant_child_fence,
)
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.receipt_evidence import RECEIPT_PURPOSE_CONTRACT
from services.policy.action_fence import build_action_resource_fence
from services.policy.context import PURPOSE_VALUES
from services.policy.receipts import PolicyReceiptV2

SESSION_ID = "ses_01J_test"
ENDPOINT = "http://test/v1/interaction/action-policy"
TOKEN = "action-policy-token-that-is-long-enough"


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _profile() -> Any:
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        owner_profile_for_session,
    )

    return owner_profile_for_session(SESSION_ID)


def _fence() -> GenerationFence:
    return GenerationFence(
        session_id=SESSION_ID,
        turn_id=4,
        generation_id=7,
        tool_epoch=1,
        session_epoch=1,
    )


def _obligation_spec(code: str) -> PolicyObligationSpec:
    return PolicyObligationSpec(
        code=GeneratedPolicyObligation(code),
        params=GeneratedObligationParams(
            max_session_seconds=None,
            retention_ttl_seconds=None,
            quiet_hours=None,
            extras=(),
        ),
    )


def _receipt(**overrides: object) -> PolicyReceiptV2:
    """One contract-valid exact-fence receipt pinned to the test profile/fence."""

    profile = _profile().profile
    fence = _fence()
    now = datetime.now(UTC)
    created_at = now - timedelta(seconds=1)
    expires_at = now + timedelta(minutes=5)
    action_fence = build_action_resource_fence(
        capability="memory_capture",
        purpose="memory_capture",
        action_resource_id=f"memory_capture:{SESSION_ID}:{fence.turn_id}:{fence.generation_id}",
        action_revision=1,
        generation_id=fence.generation_id,
        turn_id=fence.turn_id,
        tool_epoch=fence.tool_epoch,
        issued_at=created_at,
        valid_until=expires_at,
    )
    values: dict[str, object] = {
        "receipt_id": f"receipt-{fence.session_epoch}-{fence.generation_id}",
        "actor_id": profile.actor_id,
        "subject_id": profile.active_subject_id,
        "resource_owner_id": profile.active_subject_id,
        "device_id": profile.device_id,
        "capability": "memory_capture",
        "purpose": "memory_capture",
        "effect": "allow",
        "reason_code": "exact_fence_consent",
        "obligations": (),
        "policy_version": "runtime-action-test-v1",
        "context_hash": "c" * 64,
        "action_resource_fence": action_fence,
        "action_fence_hash": action_fence.canonical_hash,
        "consent_snapshot_ids": (),
        "consent_snapshot_revisions": (),
        "relationship_snapshot_ids": (),
        "relationship_snapshot_revisions": (),
        "binding_id": profile.binding_id,
        "binding_version": profile.binding_version,
        "binding_canonical_hash": "b" * 64,
        "session_id": profile.session_id,
        "session_epoch": profile.session_epoch,
        "runtime_profile_id": profile.runtime_profile_id,
        "subject_revision": profile.subject_revision,
        "device_trust": "trusted",
        "data_classification": "private",
        "safety_state": "normal",
        "jurisdiction": "CN",
        "created_at": _rfc3339(created_at),
        "expires_at": _rfc3339(expires_at),
        "exact_fence": True,
    }
    values.update(overrides)
    action_fence = values["action_resource_fence"]
    if "action_fence_hash" not in overrides:
        values["action_fence_hash"] = action_fence.canonical_hash
    obligations: list[PolicyObligationSpec] = []
    for item in values.get("obligations", ()):
        if isinstance(item, PolicyObligationSpec):
            obligations.append(item)
        else:
            obligations.append(_obligation_spec(str(item)))
    values["obligations"] = tuple(obligations)
    return PolicyReceiptV2(**values)


def _raw_receipt_json(**overrides: object) -> dict[str, object]:
    """One wire payload without model invariants (simulates a bad server)."""

    raw = _receipt().model_dump(mode="json")
    raw.update(overrides)
    return raw


def _verified_receipt_for(
    profile: Any,
    fence: GenerationFence,
    **overrides: object,
) -> VerifiedActionReceipt:
    frozen = FrozenActionFence.from_profile(profile, fence)
    return verify_action_receipt(
        _receipt(**overrides),
        fence=frozen,
        capability="memory_capture",
        now=datetime.now(UTC),
    )


def _verified_receipt(**overrides: object) -> VerifiedActionReceipt:
    return _verified_receipt_for(_profile().profile, _fence(), **overrides)


def _client(handler: Any) -> ActionPolicyClient:
    transport = httpx.MockTransport(handler)
    return ActionPolicyClient(
        ActionPolicyClientConfig(endpoint=ENDPOINT, internal_token=TOKEN, timeout_s=0.4),
        client=httpx.AsyncClient(transport=transport, timeout=0.4),
    )


def test_purpose_contract_is_aligned_with_policy_v2_authority() -> None:
    assert set(RECEIPT_PURPOSE_CONTRACT.values()) <= PURPOSE_VALUES
    assert RECEIPT_PURPOSE_CONTRACT == {
        "chat": "user_request",
        "memory_capture": "memory_capture",
        "raw_audio_retention": "raw_audio",
        "model_training_contribution": "model_training",
    }


def test_frozen_fence_pins_exact_identity_pair() -> None:
    profile = _profile().profile
    frozen = FrozenActionFence.from_profile(profile, _fence())
    assert frozen.session_id == SESSION_ID
    assert frozen.runtime_profile_id == profile.runtime_profile_id
    assert frozen.session_epoch == profile.session_epoch
    assert frozen.subject_id == profile.active_subject_id
    assert frozen.resource_owner_id == profile.active_subject_id
    assert (frozen.turn_id, frozen.generation_id, frozen.tool_epoch) == (4, 7, 1)
    body = frozen.request_body(
        capability="memory_capture",
        data_classification="private",
        safety_state="normal",
    )
    assert set(body) == {
        "session_id",
        "runtime_profile_id",
        "capability",
        "session_epoch",
        "generation_id",
        "turn_id",
        "tool_epoch",
        "data_classification",
        "safety_state",
    }
    assert "actor_id" not in body


def test_frozen_fence_rejects_subject_switch_epoch_drift_and_unknown_speaker() -> None:
    profile = _profile().profile
    switched = _fence().with_session_epoch(2)
    with pytest.raises(ActionFenceRejected):
        FrozenActionFence.from_profile(profile, switched)
    foreign_session = replace(_fence(), session_id="ses_other")
    with pytest.raises(ActionFenceRejected):
        FrozenActionFence.from_profile(profile, foreign_session)
    unconfirmed = replace(profile, speaker_state="unconfirmed")
    with pytest.raises(ActionFenceRejected):
        FrozenActionFence.from_profile(unconfirmed, _fence())
    unnamed = replace(
        profile,
        active_subject_id=None,
        speaker_state="unconfirmed",
        service_mode="unknown_safe",
    )
    with pytest.raises(ActionFenceRejected):
        FrozenActionFence.from_profile(unnamed, _fence())


def test_verify_action_receipt_accepts_only_exact_fence_receipt() -> None:
    profile = _profile().profile
    frozen = FrozenActionFence.from_profile(profile, _fence())
    verified = verify_action_receipt(
        _receipt(),
        fence=frozen,
        capability="memory_capture",
        now=datetime.now(UTC),
    )
    assert verified.receipt_id.startswith("receipt-")
    assert verified.exact_fence is True
    assert verified.matches_fence(frozen)
    assert verified.capability == "memory_capture"
    assert verified.purpose == "memory_capture"
    assert not verified.is_expired(datetime.now(UTC))
    assert verified.forbids_persistence is False
    fence_payload = verified.evidence_fence()
    assert set(fence_payload) == EVIDENCE_FENCE_KEYS
    assert fence_payload["schema"] == EVIDENCE_FENCE_SCHEMA
    assert fence_payload["exact_fence"] is True
    assert fence_payload["policy_receipt_id"] == verified.receipt_id
    assert archive_fence_valid(fence_payload, capability="memory_capture")


@pytest.mark.asyncio
async def test_client_authorizes_and_sends_only_frozen_fields() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        assert request.url.path.endswith("/action-policy")
        return httpx.Response(200, json=_receipt().model_dump(mode="json"))

    client = _client(handler)
    try:
        result = await client.authorize(
            profile=_profile().profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert isinstance(result, VerifiedActionReceipt)
    assert captured["headers"]["x-memoria-internal-token"] == TOKEN
    assert "actor_id" not in captured["body"]
    assert captured["body"]["capability"] == "memory_capture"
    assert captured["body"]["session_epoch"] == 1
    assert (captured["body"]["turn_id"], captured["body"]["generation_id"]) == (4, 7)
    assert result.evidence_fence()["turn_id"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 409, 425, 503])
async def test_client_fails_closed_on_non_200_status(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status, json={"detail": {"code": "x"}})

    client = _client(handler)
    try:
        result = await client.authorize(
            profile=_profile().profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert isinstance(result, ActionPolicyUnavailable)
    assert result.reason == f"http_{status}"


@pytest.mark.asyncio
async def test_client_fails_closed_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("authority unreachable")

    client = _client(handler)
    try:
        result = await client.authorize(
            profile=_profile().profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert result == ActionPolicyUnavailable("authority_unreachable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        ["not", "an", "object"],
        {},
        {"receipt_id": "only-one-field"},
    ],
)
async def test_client_fails_closed_on_invalid_responses(payload: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=payload)

    client = _client(handler)
    try:
        result = await client.authorize(
            profile=_profile().profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert isinstance(result, ActionPolicyUnavailable)
    assert result.reason in {"response_not_json", "response_not_object", "receipt_invalid"}


@pytest.mark.asyncio
async def test_client_fails_closed_on_non_json_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"{not-json")

    client = _client(handler)
    try:
        result = await client.authorize(
            profile=_profile().profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert result == ActionPolicyUnavailable("response_not_json")


@pytest.mark.asyncio
async def test_client_rejects_each_forged_identity_field() -> None:
    profile = _profile().profile
    forged_fields: dict[str, object] = {
        "actor_id": "forged-actor",
        "subject_id": "forged-subject",
        "resource_owner_id": "forged-owner",
        "device_id": "forged-device",
        "binding_id": "forged-binding",
        "binding_version": 2,
        "runtime_profile_id": "rp_forged",
        "session_id": "ses_forged",
        "session_epoch": 2,
        "subject_revision": 99,
    }
    for field_name, forged in forged_fields.items():
        def handler(
            request: httpx.Request,
            *,
            _field_name: str = field_name,
            _forged: object = forged,
        ) -> httpx.Response:
            del request
            return httpx.Response(200, json=_receipt(**_overrides(_field_name, _forged)).model_dump(mode="json"))

        client = _client(handler)
        try:
            result = await client.authorize(
                profile=profile,
                fence=_fence(),
                capability="memory_capture",
            )
        finally:
            await client.aclose()
        assert isinstance(result, ActionPolicyUnavailable), field_name
        assert result.reason == "receipt_rejected", field_name


def _overrides(field_name: str, forged: object) -> dict[str, object]:
    if field_name == "binding_version":
        return {"binding_version": forged}
    if field_name == "session_epoch":
        return {"session_epoch": forged}
    if field_name == "subject_revision":
        return {"subject_revision": forged}
    return {field_name: forged}


@pytest.mark.asyncio
async def test_client_rejects_action_fence_drift_and_forged_hashes() -> None:
    profile = _profile().profile

    def drifted_handler(request: httpx.Request) -> httpx.Response:
        del request
        drifted = build_action_resource_fence(
            capability="memory_capture",
            purpose="memory_capture",
            action_resource_id="drifted-resource",
            action_revision=1,
            generation_id=8,
            turn_id=4,
            tool_epoch=1,
            issued_at=datetime.now(UTC) - timedelta(seconds=1),
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        return httpx.Response(200, json=_receipt(action_resource_fence=drifted).model_dump(mode="json"))

    client = _client(drifted_handler)
    try:
        result = await client.authorize(
            profile=profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert result == ActionPolicyUnavailable("receipt_rejected")

    original = _receipt()
    valid_fence = original.action_resource_fence
    tampered = valid_fence.model_copy(
        update={
            "generation_id": 8,
            "canonical_hash": valid_fence.canonical_hash,
            "action_evidence_hash": valid_fence.action_evidence_hash,
        }
    )

    def hash_handler(request: httpx.Request) -> httpx.Response:
        del request
        forged = _receipt(action_resource_fence=tampered)
        return httpx.Response(
            200,
            json=forged.model_dump(mode="json"),
        )

    client = _client(hash_handler)
    try:
        result = await client.authorize(
            profile=profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert result == ActionPolicyUnavailable("receipt_rejected")


@pytest.mark.asyncio
async def test_client_rejects_deny_effect_and_do_not_persist() -> None:
    profile = _profile().profile
    for overrides in (
        {"effect": "deny"},
        {"obligations": ("DO_NOT_PERSIST",)},
    ):
        def handler(
            request: httpx.Request,
            *,
            _overrides: object = overrides,
        ) -> httpx.Response:
            del request
            assert isinstance(_overrides, dict)
            return httpx.Response(200, json=_receipt(**_overrides).model_dump(mode="json"))

        client = _client(handler)
        try:
            result = await client.authorize(
                profile=profile,
                fence=_fence(),
                capability="memory_capture",
            )
        finally:
            await client.aclose()
        assert result == ActionPolicyUnavailable("receipt_rejected")


@pytest.mark.asyncio
async def test_client_rejects_non_exact_and_purpose_drift() -> None:
    profile = _profile().profile
    now = datetime.now(UTC)
    training_fence = build_action_resource_fence(
        capability="model_training_contribution",
        purpose="user_request",
        action_resource_id="training-drift",
        action_revision=1,
        generation_id=7,
        turn_id=4,
        tool_epoch=1,
        issued_at=now - timedelta(seconds=1),
        valid_until=now + timedelta(minutes=5),
    )
    cases = (
        (
            _receipt(exact_fence=False).model_dump(mode="json"),
            "memory_capture",
            "receipt_rejected",
        ),
        (
            _raw_receipt_json(binding_canonical_hash=None),
            "memory_capture",
            "receipt_invalid",
        ),
        (
            _receipt(
                capability="model_training_contribution",
                purpose="user_request",
                action_resource_fence=training_fence,
            ).model_dump(mode="json"),
            "model_training_contribution",
            "receipt_rejected",
        ),
    )
    for payload, capability, expected in cases:
        def handler(
            request: httpx.Request,
            *,
            _payload: object = payload,
        ) -> httpx.Response:
            del request
            return httpx.Response(200, json=_payload)

        client = _client(handler)
        try:
            result = await client.authorize(
                profile=profile,
                fence=_fence(),
                capability=capability,
            )
        finally:
            await client.aclose()
        assert isinstance(result, ActionPolicyUnavailable)
        assert result.reason == expected


@pytest.mark.asyncio
async def test_client_rejects_expired_receipt() -> None:
    profile = _profile().profile
    now = datetime.now(UTC)
    expired_at = now - timedelta(seconds=1)
    expired_fence = build_action_resource_fence(
        capability="memory_capture",
        purpose="memory_capture",
        action_resource_id="expired-resource",
        action_revision=1,
        generation_id=7,
        turn_id=4,
        tool_epoch=1,
        issued_at=expired_at - timedelta(minutes=5),
        valid_until=expired_at,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        receipt = _receipt(
            created_at=_rfc3339(expired_at - timedelta(minutes=5)),
            expires_at=_rfc3339(expired_at),
            action_resource_fence=expired_fence,
        )
        return httpx.Response(200, json=receipt.model_dump(mode="json"))

    client = _client(handler)
    try:
        result = await client.authorize(
            profile=profile,
            fence=_fence(),
            capability="memory_capture",
        )
    finally:
        await client.aclose()
    assert result == ActionPolicyUnavailable("receipt_rejected")


@pytest.mark.asyncio
async def test_client_rejects_capability_without_archive_contract() -> None:
    client = _client(lambda request: httpx.Response(200, json=_receipt().model_dump(mode="json")))
    try:
        result = await client.authorize(
            profile=_profile().profile,
            fence=_fence(),
            capability="payment",
        )
    finally:
        await client.aclose()
    assert result == ActionPolicyUnavailable("capability_not_deferred")


@pytest.mark.asyncio
async def test_default_deny_port_never_authorizes() -> None:
    result = await DefaultDenyActionPolicy().authorize(
        profile=_profile().profile,
        fence=_fence(),
        capability="memory_capture",
    )
    assert result == ActionPolicyUnavailable("default_deny")


def test_user_and_assistant_child_fences_are_identical_for_one_turn() -> None:
    verified = _verified_receipt()
    parent = user_event_evidence_fence(verified)
    child = assistant_child_evidence_fence(verified)
    assert parent == child
    assert verify_assistant_child_fence(parent, child) is True


def test_assistant_child_fence_rejects_drift_and_missing_parent() -> None:
    parent = _verified_receipt().evidence_fence()
    assert verify_assistant_child_fence(None, parent) is False
    assert verify_assistant_child_fence(parent, None) is False

    profile = _profile().profile
    bumped_fence = replace(_fence(), generation_id=8)
    bumped = _verified_receipt_for(
        profile,
        bumped_fence,
        receipt_id="receipt-other-generation",
        action_resource_fence=build_action_resource_fence(
            capability="memory_capture",
            purpose="memory_capture",
            action_resource_id="child-bumped",
            action_revision=1,
            generation_id=8,
            turn_id=4,
            tool_epoch=1,
            issued_at=datetime.now(UTC) - timedelta(seconds=1),
            valid_until=datetime.now(UTC) + timedelta(minutes=5),
        ),
    ).evidence_fence()
    assert verify_assistant_child_fence(parent, bumped) is False

    switched_profile = replace(profile, session_epoch=2)
    switched_epoch = _verified_receipt_for(
        switched_profile,
        _fence().with_session_epoch(2),
        session_epoch=2,
    ).evidence_fence()
    assert verify_assistant_child_fence(parent, switched_epoch) is False

    different_receipt = _verified_receipt(receipt_id="receipt-different").evidence_fence()
    assert verify_assistant_child_fence(parent, different_receipt) is False


def test_archive_fence_structural_gate() -> None:
    valid = _verified_receipt().evidence_fence()
    assert archive_fence_valid(valid, capability="memory_capture") is True
    assert archive_fence_valid(valid, capability="raw_audio_retention") is False

    missing_key = {key: value for key, value in valid.items() if key != "context_hash"}
    assert archive_fence_valid(missing_key) is False
    assert archive_fence_valid({**valid, "caller_field": "x"}) is False
    assert archive_fence_valid({**valid, "schema": "other-schema"}) is False
    assert archive_fence_valid({**valid, "exact_fence": False}) is False
    assert archive_fence_valid({**valid, "action_fence_hash": "not-a-hash"}) is False
    assert archive_fence_valid({**valid, "session_epoch": 0}) is False
    assert archive_fence_valid({**valid, "expires_at": "not-a-timestamp"}) is False
    assert archive_fence_valid({**valid, "turn_id": "4"}) is False
    assert archive_fence_valid({**valid, "expires_at": valid["issued_at"]}) is False
    assert archive_fence_valid("not-a-fence") is False

    now = datetime.now(UTC)
    expired = {
        **valid,
        "issued_at": _rfc3339(now - timedelta(minutes=10)),
        "expires_at": _rfc3339(now - timedelta(seconds=1)),
    }
    assert archive_fence_valid(expired) is True
    assert archive_fence_valid(expired, now=now) is False


def test_config_rejects_bad_endpoint_token_and_timeout() -> None:
    with pytest.raises(ValueError):
        ActionPolicyClientConfig(endpoint="not-a-url", internal_token=TOKEN)
    with pytest.raises(ValueError):
        ActionPolicyClientConfig(endpoint="http://test/wrong-path", internal_token=TOKEN)
    with pytest.raises(ValueError):
        ActionPolicyClientConfig(endpoint=ENDPOINT, internal_token="  ")
    with pytest.raises(ValueError):
        ActionPolicyClientConfig(endpoint=ENDPOINT, internal_token=TOKEN, timeout_s=0)


def test_verified_action_receipt_rejects_forged_construction() -> None:
    verified = _verified_receipt()
    with pytest.raises(TypeError):
        VerifiedActionReceipt(  # type: ignore[call-arg]
            receipt_id=verified.receipt_id,
            capability=verified.capability,
            purpose=verified.purpose,
            effect=verified.effect,
            reason_code=verified.reason_code,
            policy_version=verified.policy_version,
            context_hash=verified.context_hash,
            action_fence_hash=verified.action_fence_hash,
            exact_fence=verified.exact_fence,
            actor_id=verified.actor_id,
            subject_id=verified.subject_id,
            resource_owner_id=verified.resource_owner_id,
            device_id=verified.device_id,
            binding_id=verified.binding_id,
            binding_version=verified.binding_version,
            runtime_profile_id=verified.runtime_profile_id,
            session_id=verified.session_id,
            session_epoch=verified.session_epoch,
            subject_revision=verified.subject_revision,
            generation_id=verified.generation_id,
            turn_id=verified.turn_id,
            tool_epoch=verified.tool_epoch,
            issued_at=verified.issued_at,
            expires_at=verified.expires_at,
            obligations=verified.obligations,
            receipt=verified.receipt,
        )
