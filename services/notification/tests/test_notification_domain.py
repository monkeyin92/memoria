"""Pure-domain tests for the notification state machine and templates."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from services.notification.domain import (
    BackoffPolicy,
    ContentPolicyError,
    NotificationFence,
    RecipientBinding,
    RelationshipSnapshot,
    cancelled_recipient,
    classify_failure,
    derive_intent_status,
    recipient_evidence_ok,
    render_notification_content,
)
from services.notification.tests.receipt_helpers import (
    make_notification_receipt,
)


def _now() -> datetime:
    return datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


def _params(**overrides: str) -> dict[str, str]:
    base = {
        "role_label": "孩子",
        "reason_code": "safety_concern",
        "occurred_at": "2026-08-09T09:58:00+00:00",
        "script_version": "2026-08-09.1",
        "action_hint": "联系监护人",
    }
    base.update(overrides)
    return base


def test_render_crisis_safety_template_deterministic() -> None:
    text = render_notification_content("crisis_safety_notice", _params())
    assert text == (
        "【Memoria 安全提醒】您的孩子可能需要关心。"
        "原因代码：safety_concern。发生时间：2026-08-09T09:58:00+00:00。"
        "话术版本：2026-08-09.1。建议：联系监护人。"
    )


def test_notification_fence_requires_positive_integer_binding_version() -> None:
    def _fence(**changes: object) -> NotificationFence:
        base = {
            "session_id": "s",
            "device_id": "device-1",
            "epoch": 1,
            "binding_id": "b",
            "binding_version": 1,
            "runtime_profile_id": "p",
            "actor_person_id": "a",
            "subject_person_id": "s1",
            "valid_until": _now() + timedelta(hours=2),
        }
        base.update(changes)
        return NotificationFence(**base)  # type: ignore[arg-type]
    assert _fence().binding_version == 1
    assert _fence(binding_version=2).fingerprint() != _fence().fingerprint()
    for bad in (0, -1, "v2", None):  # type: ignore[list-item]
        with pytest.raises(ValueError, match="binding_version"):
            _fence(binding_version=bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="epoch"):
        _fence(epoch=0)
    with pytest.raises(ValueError, match="epoch"):
        _fence(epoch=-1)
    with pytest.raises(ValueError, match="session_id"):
        _fence(session_id="")


def test_policy_receipt_v2_invariants() -> None:
    """PolicyReceiptV2 (the authoritative wire) rejects bool/zero versions,
    naive times, unknown enums and unaligned evidence tuples."""
    receipt = make_notification_receipt()
    assert receipt.receipt_id == "policy-receipt-1"
    assert receipt.relationship_snapshot_ids == ("rel-snap-1",)

    with pytest.raises(ValueError, match="epoch"):
        make_notification_receipt(fence=NotificationFence(
            device_id="device-1",
            session_id="s",
            epoch=True,  # type: ignore[arg-type]
            binding_id="b",
            binding_version=1,
            runtime_profile_id="p",
            actor_person_id="a",
            subject_person_id="s1",
            valid_until=_now() + timedelta(hours=2),
        ))
    with pytest.raises(ValueError, match="session_epoch"):
        # Contract-level rejection (V2 field name) when the value reaches
        # the receipt itself.

        from services.policy.receipts import PolicyReceiptV2

        base = make_notification_receipt()
        PolicyReceiptV2(
            receipt_id=base.receipt_id,
            actor_id=base.actor_id,
            subject_id=base.subject_id,
            resource_owner_id=base.resource_owner_id,
            device_id=base.device_id,
            capability=base.capability,
            purpose=base.purpose,
            effect=base.effect,
            reason_code=base.reason_code,
            obligations=base.obligations,
            policy_version=base.policy_version,
            context_hash=base.context_hash,
            action_resource_fence=base.action_resource_fence,
            action_fence_hash=base.action_fence_hash,
            consent_snapshot_ids=base.consent_snapshot_ids,
            consent_snapshot_revisions=base.consent_snapshot_revisions,
            relationship_snapshot_ids=base.relationship_snapshot_ids,
            relationship_snapshot_revisions=base.relationship_snapshot_revisions,
            binding_id=base.binding_id,
            binding_version=base.binding_version,
            binding_canonical_hash=base.binding_canonical_hash,
            session_id=base.session_id,
            session_epoch=True,  # type: ignore[arg-type]
            runtime_profile_id=base.runtime_profile_id,
            subject_revision=base.subject_revision,
            device_trust=base.device_trust,
            data_classification=base.data_classification,
            safety_state=base.safety_state,
            jurisdiction=base.jurisdiction,
            created_at=base.created_at,
            expires_at=base.expires_at,
            exact_fence=base.exact_fence,
        )
    with pytest.raises(ValueError, match="binding_version"):
        make_notification_receipt(fence=NotificationFence(
            device_id="device-1",
            session_id="s",
            epoch=1,
            binding_id="b",
            binding_version=0,
            runtime_profile_id="p",
            actor_person_id="a",
            subject_person_id="s1",
            valid_until=_now() + timedelta(hours=2),
        ))
    with pytest.raises(ValueError, match="Capability"):
        make_notification_receipt(capability="notification_operator")
    with pytest.raises(ValueError, match="Purpose"):
        make_notification_receipt(purpose="operator_override")
    with pytest.raises(ValueError, match="context_hash"):
        make_notification_receipt(context_hash="hash")
    with pytest.raises(ValueError, match="paired arrays"):
        make_notification_receipt(
            consent_snapshot_ids=("c1",), consent_snapshot_revisions=()
        )
    with pytest.raises(ValueError, match="time window"):
        make_notification_receipt(
            created_at=datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
            expires_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC)
        )


def test_recipient_evidence_ok_requires_receipt_covered_relationship() -> None:
    """Policy-crisis coupling: only relationships inside the V2 evidence set
    with matching parameterized obligation may become recipients."""
    fence = NotificationFence(
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id="actor-1",
        subject_person_id="minor-1",
        valid_until=_now() + timedelta(hours=2),
    )
    receipt = make_notification_receipt(
        fence=fence,
        relationship_snapshot_ids=("rel-snap-1",),
    )
    snapshot = RelationshipSnapshot(
        relationship_id="rel-1",
        subject_person_id="minor-1",
        person_id="guardian-1",
        role="guardian",
        status="active",
        snapshot_id="rel-snap-1",
        revision=1,
    )
    assert recipient_evidence_ok(receipt, snapshot=snapshot, intent_kind="crisis_safety")

    # Same subject, DIFFERENT relationship not referenced by the receipt.
    other = RelationshipSnapshot(
        relationship_id="rel-2",
        subject_person_id="minor-1",
        person_id="guardian-9",
        role="guardian",
        status="active",
        snapshot_id="rel-snap-2",
        revision=1,
    )
    assert not recipient_evidence_ok(
        receipt, snapshot=other, intent_kind="crisis_safety"
    )

    # Missing snapshot evidence fails closed even when the obligation matches.
    missing = RelationshipSnapshot(
        relationship_id="rel-3",
        subject_person_id="minor-1",
        person_id="guardian-2",
        role="guardian",
        status="active",
        snapshot_id="",
        revision=1,
    )
    assert not recipient_evidence_ok(
        receipt, snapshot=missing, intent_kind="crisis_safety"
    )

    # Parameterized obligation mismatch (role / intent kind) fails closed.
    from services.notification.tests.receipt_helpers import (
        default_notify_obligations,
        notify_obligations_for_roles,
    )

    # A receipt may legally carry distinct NOTIFY obligations for multiple
    # recipient roles.  The matching role must authorize the recipient even
    # when an unrelated role appears first.
    multi_role = make_notification_receipt(
        fence=fence,
        relationship_snapshot_ids=("rel-snap-1",),
        obligations=notify_obligations_for_roles(
            recipient_roles=("delegate", "guardian"),
            intent_kind="crisis_safety",
        ),
    )
    assert recipient_evidence_ok(
        multi_role, snapshot=snapshot, intent_kind="crisis_safety"
    )

    wrong_role = make_notification_receipt(
        fence=fence,
        relationship_snapshot_ids=("rel-snap-1",),
        obligations=default_notify_obligations(
            recipient_role="delegate", intent_kind="crisis_safety"
        ),
    )
    assert not recipient_evidence_ok(
        wrong_role, snapshot=snapshot, intent_kind="crisis_safety"
    )
    wrong_kind = make_notification_receipt(
        fence=fence,
        relationship_snapshot_ids=("rel-snap-1",),
        obligations=default_notify_obligations(
            recipient_role="guardian", intent_kind="care_alert"
        ),
    )
    assert not recipient_evidence_ok(
        wrong_kind, snapshot=snapshot, intent_kind="crisis_safety"
    )

    # A receipt WITHOUT the parameterized NOTIFY obligation authorizes nobody.
    from packages.contracts.generated.python.multi_subject_contracts import (
        ObligationParams,
        PolicyObligation,
        PolicyObligationSpec,
    )

    bare = make_notification_receipt(
        fence=fence,
        relationship_snapshot_ids=("rel-snap-1",),
        obligations=(
            PolicyObligationSpec(
                code=PolicyObligation.POLICY_OBLIGATION_MINIMAL_NOTIFICATION_CONTENT,
                params=ObligationParams(
                    max_session_seconds=None,
                    retention_ttl_seconds=None,
                    quiet_hours=None,
                    extras=(),
                ),
            ),
        ),
    )
    assert not recipient_evidence_ok(
        bare, snapshot=snapshot, intent_kind="crisis_safety"
    )


def test_relationship_snapshot_binding_version_invariant() -> None:
    def _snapshot(**changes: object) -> RelationshipSnapshot:
        base: dict[str, object] = {
            "relationship_id": "rel-1",
            "subject_person_id": "minor-1",
            "person_id": "guardian-1",
            "role": "guardian",
            "status": "active",
            "binding_version": 1,
        }
        base.update(changes)
        return RelationshipSnapshot(**base)  # type: ignore[arg-type]

    assert _snapshot().binding_version == 1
    assert _snapshot(binding_version=None).binding_version is None
    with pytest.raises(ValueError, match="binding_version"):
        _snapshot(binding_version=0)
    with pytest.raises(ValueError, match="binding_version"):
        _snapshot(binding_version=-1)
    with pytest.raises(ValueError, match="binding_version"):
        _snapshot(binding_version="v2")  # type: ignore[arg-type]


def test_render_all_templates() -> None:
    crisis = render_notification_content("crisis_safety_notice", _params())
    emergency = render_notification_content(
        "emergency_notice",
        _params(role_label="家人", reason_code="emergency_alert", action_hint="联系紧急联系人"),
    )
    care = render_notification_content(
        "care_alert_notice",
        _params(role_label="老人", reason_code="care_reminder", action_hint="陪伴安抚并关注变化"),
    )
    assert "安全提醒" in crisis and "紧急提醒" in emergency and "关怀提醒" in care


@pytest.mark.parametrize(
    "overrides",
    [
        {"transcript": "用户原话"},
        {"diagnosis": "重度抑郁"},
        {"severity": "high"},
        {"quoted_memory": "秘密"},
        {"raw_name": "小明"},
        {"emotional_label": "焦虑"},
    ],
)
def test_forbidden_content_keys_never_render(overrides: dict[str, str]) -> None:
    with pytest.raises(ContentPolicyError):
        render_notification_content("crisis_safety_notice", _params(**overrides))


def test_unknown_and_missing_params_rejected() -> None:
    with pytest.raises(ContentPolicyError):
        render_notification_content("crisis_safety_notice", _params(extra="x"))
    with pytest.raises(ContentPolicyError):
        render_notification_content(
            "crisis_safety_notice",
            {k: v for k, v in _params().items() if k != "reason_code"},
        )
    with pytest.raises(ContentPolicyError):
        render_notification_content("unknown_template", _params())


@pytest.mark.parametrize(
    "overrides",
    [
        {"reason_code": "made_up"},
        {"role_label": "陌生人"},
        {"action_hint": "删除系统"},
        {"script_version": "../etc/passwd"},
        {"occurred_at": "not-a-date"},
    ],
)
def test_out_of_vocabulary_values_rejected(overrides: dict[str, str]) -> None:
    with pytest.raises(ContentPolicyError):
        render_notification_content("crisis_safety_notice", _params(**overrides))


def test_classify_failure_codes() -> None:
    assert classify_failure("timeout") == "retryable"
    assert classify_failure("channel_invalid") == "permanent"
    assert classify_failure("some_unreviewed_code") == "unknown"


def test_backoff_deterministic_and_monotonic() -> None:
    rng = random.Random(42)
    policy = BackoffPolicy(base_seconds=10.0, factor=2.0, max_seconds=100.0, rng=rng)
    first = policy.delay_seconds(1)
    second = policy.delay_seconds(2)
    third = policy.delay_seconds(3)
    assert 10.0 <= first < 12.0
    assert 20.0 <= second < 24.0
    assert second > first
    assert third > second
    assert BackoffPolicy(rng=random.Random(42)).delay_seconds(2) == BackoffPolicy(
        rng=random.Random(42)
    ).delay_seconds(2)


def test_backoff_caps_at_max() -> None:
    policy = BackoffPolicy(base_seconds=5.0, factor=2.0, max_seconds=20.0, rng=random.Random(1))
    assert all(policy.delay_seconds(i) <= 24.0 for i in range(1, 8))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_seconds": 0},
        {"factor": 1.0},
        {"max_seconds": 1.0, "base_seconds": 5.0},
        {"jitter_ratio": 2.0},
    ],
)
def test_backoff_invalid_parameters(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(**kwargs)


def _recipient(**overrides: object) -> RecipientBinding:
    base: dict[str, object] = {
        "recipient_id": "r1",
        "intent_id": "i1",
        "person_id": "p2",
        "role": "guardian",
        "relationship_id": "rel1",
        "relationship_status": "active",
        "channels": ("wechat_subscription", "sms"),
        "channel_index": 0,
        "status": "pending",
        "attempts": 0,
        "max_retries": 3,
        "next_attempt_at": None,
        "leased_until": None,
        "fencing_token": None,
        "last_error_code": None,
        "valid_from": datetime(2026, 8, 1, tzinfo=UTC),
        "valid_until": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    base.update(overrides)
    return RecipientBinding(**base)  # type: ignore[arg-type]


def test_is_due_rules() -> None:
    now = _now()
    assert _recipient().is_due(now)
    assert not _recipient(status="cancelled").is_due(now)
    assert not _recipient(status="delivered").is_due(now)
    assert not _recipient(relationship_status="revoked").is_due(now)
    assert not _recipient(
        valid_until=now - timedelta(minutes=1)
    ).is_due(now)
    assert not _recipient(
        status="failed", next_attempt_at=now + timedelta(minutes=5)
    ).is_due(now)
    assert _recipient(status="failed", next_attempt_at=now).is_due(now)
    assert _recipient(
        status="in_progress",
        leased_until=now - timedelta(seconds=1),
        fencing_token="t",
        attempts=1,
    ).is_due(now)
    assert not _recipient(
        status="in_progress",
        leased_until=now + timedelta(seconds=1),
        fencing_token="t",
        attempts=1,
    ).is_due(now)


def test_cancelled_recipient_is_terminal_and_auditable() -> None:
    now = _now()
    cancelled = cancelled_recipient(_recipient(), reason="wrong_contact", now=now)
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_reason == "wrong_contact"
    assert cancelled.cancelled_at == now
    assert cancelled.fencing_token is None
    assert cancelled.leased_until is None
    assert not cancelled.is_due(now)


def test_derive_intent_status_matrix() -> None:
    def statuses(*items: str) -> tuple[RecipientBinding, ...]:
        return tuple(_recipient(recipient_id=f"r{i}", status=item) for i, item in enumerate(items))

    assert derive_intent_status(statuses("pending")) == "pending"
    assert derive_intent_status(statuses("pending", "cancelled")) == "in_progress"
    assert derive_intent_status(statuses("delivered")) == "delivered"
    assert derive_intent_status(statuses("delivered", "cancelled")) == "delivered"
    assert derive_intent_status(statuses("dead_lettered")) == "dead_lettered"
    assert derive_intent_status(statuses("dead_lettered", "cancelled")) == "dead_lettered"
    assert derive_intent_status(statuses("cancelled", "cancelled")) == "cancelled"
    assert derive_intent_status(statuses("failed")) == "in_progress"
