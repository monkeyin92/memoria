"""Capability-specific receipt verification port tests (audit 3/4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
from services.agent.src.receipt_evidence import (
    RECEIPT_PURPOSE_CONTRACT,
    DefaultDenyReceiptVerifier,
    evidence_from_policy_receipt,
)
from services.policy.context import PURPOSE_VALUES
from services.policy.receipts import PolicyReceiptV2

_WRITE_RECEIPT_SPEC = PolicyObligationSpec(
    code=GeneratedPolicyObligation("WRITE_POLICY_RECEIPT"),
    params=GeneratedObligationParams(
        max_session_seconds=None,
        retention_ttl_seconds=None,
        quiet_hours=None,
        extras=(),
    ),
)


def _obligation_spec(
    code: str,
    *,
    retention_ttl_seconds: int | None = None,
) -> PolicyObligationSpec:
    return PolicyObligationSpec(
        code=GeneratedPolicyObligation(code),
        params=GeneratedObligationParams(
            max_session_seconds=None,
            retention_ttl_seconds=retention_ttl_seconds,
            quiet_hours=None,
            extras=(),
        ),
    )


def _profile() -> object:
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        canonical_wire_payload,
        parse_runtime_profile,
    )

    verified = parse_runtime_profile(
        canonical_wire_payload(), verify_key="test-runtime-profile-verify-key"
    )
    assert verified is not None
    return verified


def _v2_receipt(**overrides: object) -> PolicyReceiptV2:
    from services.policy.action_fence import build_action_resource_fence

    profile = _profile().profile
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    values: dict[str, object] = {
        "receipt_id": profile.policy_receipt_ids[0],
        "actor_id": profile.actor_id,
        "subject_id": profile.active_subject_id,
        "resource_owner_id": profile.actor_id,
        "device_id": profile.device_id,
        "capability": "memory_capture",
        "purpose": "memory_capture",
        "effect": "allow",
        "reason_code": "exact_fence_consent",
        "obligations": (),
        "policy_version": "cn-v2",
        "context_hash": "0" * 64,
        "consent_snapshot_ids": (),
        "consent_snapshot_revisions": (),
        "relationship_snapshot_ids": (),
        "relationship_snapshot_revisions": (),
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
        "created_at": created_at,
        "expires_at": datetime(2099, 1, 1, tzinfo=UTC),
        "exact_fence": False,
    }
    values.update(overrides)
    expires_at = values["expires_at"]
    assert isinstance(expires_at, datetime)
    # Canonical Policy V2 wire: derived action/resource fence + RFC3339
    # timestamps; local obligation dataclasses are normalised to the
    # generated PolicyObligationSpec.
    action_fence = build_action_resource_fence(
        capability="memory_capture",
        purpose="memory_capture",
        action_resource_id="action-test",
        action_revision=max(profile.session_epoch, 1),
        generation_id=0,
        turn_id=0,
        tool_epoch=0,
        issued_at=created_at,
        valid_until=max(expires_at, created_at + timedelta(seconds=1)),
    )
    values["action_resource_fence"] = action_fence
    values["action_fence_hash"] = action_fence.canonical_hash
    values["created_at"] = _rfc3339(created_at)
    values["expires_at"] = _rfc3339(expires_at)
    obligations: list[PolicyObligationSpec] = []
    for item in values.get("obligations", ()):
        if isinstance(item, PolicyObligationSpec):
            obligations.append(item)
            continue
        params = getattr(item, "params", None)
        obligations.append(
            PolicyObligationSpec(
                code=GeneratedPolicyObligation(item.code),
                params=GeneratedObligationParams(
                    max_session_seconds=(
                        getattr(params, "max_session_seconds", None) if params is not None else None
                    ),
                    retention_ttl_seconds=(
                        getattr(params, "retention_ttl_seconds", None)
                        if params is not None
                        else None
                    ),
                    quiet_hours=(
                        getattr(params, "quiet_hours", None) if params is not None else None
                    ),
                    extras=(getattr(params, "extras", ()) if params is not None else ()),
                ),
            )
        )
    values["obligations"] = tuple(obligations)
    return PolicyReceiptV2(**values)


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_purpose_contract_is_aligned_with_policy_v2_authority() -> None:
    """Audit 4: purposes come from the Policy V2 authority, never a second set."""

    assert set(RECEIPT_PURPOSE_CONTRACT.values()) <= PURPOSE_VALUES
    assert RECEIPT_PURPOSE_CONTRACT == {
        "chat": "user_request",
        "memory_capture": "memory_capture",
        "raw_audio_retention": "raw_audio",
        "model_training_contribution": "model_training",
    }


def test_default_deny_verifier_never_authorizes() -> None:
    verifier = DefaultDenyReceiptVerifier()
    assert verifier.verify_receipt(capability="memory_capture", profile=_profile().profile) is None


def test_evidence_from_policy_receipt_derives_exact_evidence() -> None:
    profile = _profile().profile
    evidence = evidence_from_policy_receipt(
        _v2_receipt(),
        capability="memory_capture",
        profile=profile,
    )
    assert evidence.receipt_id == profile.policy_receipt_ids[0]
    assert evidence.purpose == "memory_capture"
    assert evidence.matches_profile(profile)
    assert not evidence.is_expired(datetime.now(UTC))


def test_policy_engine_receipt_for_produces_acceptable_evidence() -> None:
    """Cross-module contract: PolicyEngine.receipt_for output (lowercase
    ``allow``/``allow_with_obligations``) must be accepted by the seam."""

    from services.agent.tests.unit.runtime_profile_test_helpers import (
        canonical_wire_payload,
        parse_runtime_profile,
    )
    from services.policy.context import PolicyContext
    from services.policy.engine import PolicyEngine
    from services.policy.tests.fakes import (
        make_binding,
        make_consent,
        make_consent_snapshot_ref,
        make_relationship,
    )

    # The minor acts for themselves (actor == subject) so the engine's
    # receipt fence matches the signed RuntimeProfile exactly.
    verified = parse_runtime_profile(
        canonical_wire_payload(actor_id="person_child"),
        verify_key="test-runtime-profile-verify-key",
    )
    assert verified is not None
    profile = verified.profile
    evaluated_at = datetime.now(UTC)
    context = PolicyContext(
        actor_id=profile.active_subject_id,
        subject_id=profile.active_subject_id,
        resource_owner_id=profile.active_subject_id,
        device_id=profile.device_id,
        capability="memory_capture",
        purpose="memory_capture",
        declared_device_mode="self_use",
        current_session_mode="student_minor",
        relationship_roles=frozenset({"self", "primary_subject"}),
        subject_category="minor",
        age_band="under_14",
        speaker_state="confirmed",
        speaker_confidence=0.95,
        device_trust="trusted",
        safety_state="normal",
        jurisdiction="CN",
        data_classification="private",
        binding_id=profile.binding_id,
        binding_version=profile.binding_version,
        session_id=profile.session_id,
        session_epoch=profile.session_epoch,
        runtime_profile_id=profile.runtime_profile_id,
        subject_revision=profile.subject_revision,
        evaluated_at=evaluated_at,
        consent_evidence=(
            make_consent(
                subject_id=profile.active_subject_id,
                actor_id=profile.active_subject_id,
                device_id=profile.device_id,
                binding_id=profile.binding_id,
                binding_version=profile.binding_version,
                capability="memory_capture",
                purpose="memory_capture",
                now=evaluated_at,
            ),
        ),
        consent_snapshot_evidence=(
            make_consent_snapshot_ref(
                snapshot_id="current-consent-snapshot-1",
                revision=1,
                canonical_hash="a" * 64,
                consents=(
                    make_consent(
                        subject_id=profile.active_subject_id,
                        actor_id=profile.active_subject_id,
                        device_id=profile.device_id,
                        binding_id=profile.binding_id,
                        binding_version=profile.binding_version,
                        capability="memory_capture",
                        purpose="memory_capture",
                        now=evaluated_at,
                    ),
                ),
                now=evaluated_at,
            ),
        ),
        binding_evidence=make_binding(
            binding_id=profile.binding_id,
            version=profile.binding_version,
            device_id=profile.device_id,
            now=evaluated_at,
        ),
        relationship_evidence=(
            make_relationship(
                source_person_id="person_parent",
                target_person_id=profile.active_subject_id,
                binding_id=profile.binding_id,
                now=evaluated_at,
            ),
        ),
    )
    engine = PolicyEngine(policy_version="multi-subject-v2")
    # The engine is the single authority for decision shape: decide() then
    # receipt_for() produce the canonical allow/allow_with_obligations wire.
    decision = engine.decide(context)
    assert decision.effect in {"allow", "allow_with_obligations"}
    receipt = engine.receipt_for(context, decision)
    evidence = evidence_from_policy_receipt(
        receipt,
        capability="memory_capture",
        profile=profile,
    )
    assert evidence.receipt_id == receipt.receipt_id
    assert evidence.matches_profile(profile)
    assert not evidence.is_expired(datetime.now(UTC))
    # allow_with_obligations (without persistence-forbidding obligations) is
    # also executable.
    obliged = _v2_receipt(
        effect="allow_with_obligations",
        obligations=(_WRITE_RECEIPT_SPEC,),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        actor_id="person_child",
    )
    assert (
        evidence_from_policy_receipt(
            obliged,
            capability="memory_capture",
            profile=profile,
        ).receipt_id
        == profile.policy_receipt_ids[0]
    )


def test_deny_or_persistence_forbidding_receipt_is_rejected() -> None:
    profile = _profile().profile
    for overrides in (
        {"effect": "deny"},
        {"obligations": (_obligation_spec("DO_NOT_PERSIST"),)},
        {"obligations": (_obligation_spec("RETENTION_TTL"),)},
    ):
        with pytest.raises(ValueError):
            evidence_from_policy_receipt(
                _v2_receipt(**overrides),
                capability="memory_capture",
                profile=profile,
            )


def test_receipt_with_positive_retention_ttl_is_accepted() -> None:
    profile = _profile().profile
    receipt = _v2_receipt(
        obligations=(_obligation_spec("RETENTION_TTL", retention_ttl_seconds=7_200),)
    )
    evidence = evidence_from_policy_receipt(
        receipt,
        capability="memory_capture",
        profile=profile,
    )
    assert evidence.receipt_id == profile.policy_receipt_ids[0]


def test_chat_receipt_never_authorizes_memory_or_raw() -> None:
    """A chat (user_request) receipt must not authorize memory or raw audio."""

    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        FakeReceiptVerifier,
        bind_owner_policy,
    )

    runtime = DuplexRuntime.create(session_id="ses-receipt-audit", device_id="dev_01J_test")
    bind_owner_policy(runtime)
    runtime.orchestrator.runtime_profiles.receipt_verifier = FakeReceiptVerifier(
        capabilities=("chat",)
    )
    decision = runtime.orchestrator.runtime_profiles.persistence_decision(
        runtime.fence, current_fence=runtime.fence
    )
    assert decision.allowed is False
    assert decision.raw_audio_allowed is False


def test_memory_receipt_never_authorizes_raw_audio() -> None:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy

    runtime = DuplexRuntime.create(session_id="ses-receipt-raw", device_id="dev_01J_test")
    bind_owner_policy(runtime)
    decision = runtime.orchestrator.runtime_profiles.persistence_decision(
        runtime.fence, current_fence=runtime.fence
    )
    assert decision.allowed is True
    assert decision.raw_audio_allowed is False


def test_wrong_device_or_epoch_receipt_is_rejected() -> None:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        FakeReceiptVerifier,
        bind_owner_policy,
    )

    runtime = DuplexRuntime.create(session_id="ses-receipt-tamper", device_id="dev_01J_test")
    bind_owner_policy(runtime)
    for tamper in (
        {"device_id": "dev_other"},
        {"session_epoch": 99},
        {"binding_version": 999},
    ):
        runtime.orchestrator.runtime_profiles.receipt_verifier = FakeReceiptVerifier(
            ("memory_capture",), tamper=tamper
        )
        decision = runtime.orchestrator.runtime_profiles.persistence_decision(
            runtime.fence, current_fence=runtime.fence
        )
        assert decision.allowed is False


def test_receipt_not_listed_in_signed_profile_is_rejected() -> None:
    """Audit 1: a validly verified receipt outside the profile's receipt list
    must never authorize persistence."""

    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        FakeReceiptVerifier,
        bind_owner_policy,
    )

    runtime = DuplexRuntime.create(session_id="ses-receipt-list", device_id="dev_01J_test")
    bind_owner_policy(runtime)
    runtime.orchestrator.runtime_profiles.receipt_verifier = FakeReceiptVerifier(
        ("memory_capture",),
        receipt_id_override="not-in-profile",
    )
    decision = runtime.orchestrator.runtime_profiles.persistence_decision(
        runtime.fence, current_fence=runtime.fence
    )
    assert decision.allowed is False


def test_expired_receipt_is_rejected() -> None:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        FakeReceiptVerifier,
        bind_owner_policy,
    )

    runtime = DuplexRuntime.create(session_id="ses-receipt-expiry", device_id="dev_01J_test")
    bind_owner_policy(runtime)
    runtime.orchestrator.runtime_profiles.receipt_verifier = FakeReceiptVerifier(
        ("memory_capture",),
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    decision = runtime.orchestrator.runtime_profiles.persistence_decision(
        runtime.fence, current_fence=runtime.fence
    )
    assert decision.allowed is False


def test_exact_receipt_enables_persistence() -> None:
    from services.agent.src.duplex_runtime import DuplexRuntime
    from services.agent.tests.unit.runtime_profile_test_helpers import (
        bind_owner_policy,
        install_receipt_verifier,
    )

    runtime = DuplexRuntime.create(session_id="ses-receipt-ok", device_id="dev_01J_test")
    bind_owner_policy(runtime, include_raw_audio=True)
    install_receipt_verifier(
        runtime,
        capabilities=("memory_capture", "raw_audio_retention"),
    )
    decision = runtime.orchestrator.runtime_profiles.persistence_decision(
        runtime.fence, current_fence=runtime.fence
    )
    assert decision.allowed is True
    assert decision.raw_audio_allowed is True
