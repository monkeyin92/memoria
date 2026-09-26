"""PR-01 object contracts (section 9.6): schema, codegen, golden vs Control, fail-closed tests.

These tests pin the object contracts to the canonical schema and prove:

- every definition is structurally sound (additionalProperties false,
  required subset, resolvable $refs, bounded arrays);
- the generated Pydantic v2 models validate real wire payloads and reject
  extra/missing fields, wrong enum values, string/0/null ``binding_version``
  and pre-issuance ``session_epoch``/``epoch`` 0 (except SessionEvent, which
  may express pre-issuance lifecycle and cannot authorize sensitive behavior);
- ``RuntimeProfileSigned`` really requires ``signature`` and rejects unknown
  keys in a real JSON Schema validator (draft 2020-12), not just on the
  schema surface;
- the golden test imports Control's ``runtime_profile_wire_payload`` and
  proves the field set and nested shape match the schema exactly, for both a
  confirmed profile and an unknown-safe null profile;
- generation is deterministic and every artifact embeds the schema sha256.

``binding_version`` is canonical ``integer >= 1`` everywhere (a string, 0 or
null always fails closed). Services that still carry ``str | None``
(``services/memory_scope`` WriteFence, ``services/notification``
NotificationFence/RelationshipSnapshot) must migrate to the canonical int;
see the ADR-0033 object-contract section.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from scripts import generate_multi_subject_contracts as gen

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / gen.SCHEMA_REL_PATH
GENERATED_ROOT = REPO_ROOT / "packages/contracts/generated"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _object_defs() -> dict[str, dict]:
    return _schema()["objects"]["properties"]["definitions"]["properties"]


def _load_python_module(path: Path) -> object:
    name = "multi_subject_contracts_objects"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CONTRACTS_MODULE = _load_python_module(GENERATED_ROOT / "python/multi_subject_contracts.py")


# ---------------------------------------------------------------------------
# Fixtures (matching the Control wire shape)
# ---------------------------------------------------------------------------


def _control_profile(confirmed: bool):
    from packages.contracts.generated.python.multi_subject_contracts import (
        ObligationParams,
        PolicyObligation,
        PolicyObligationSpec,
    )
    from services.session_runtime.profile_service import (
        RuntimeProfile as ControlRuntimeProfile,
    )

    now = datetime(2026, 8, 9, 10, 0, tzinfo=timezone(timedelta(hours=9)))
    return ControlRuntimeProfile(
        runtime_profile_id="rp_01JTEST",
        device_id="dev_01JTEST",
        session_id="ses_01JTEST",
        actor_id="person_parent",
        binding_id="bind_01JTEST",
        binding_version=1,
        active_subject_id="person_child" if confirmed else None,
        subject_revision=2,
        subject_category="minor" if confirmed else "unknown",
        age_band="under_14" if confirmed else "unknown",
        speaker_state="confirmed" if confirmed else "unconfirmed",
        speaker_confidence=0.91 if confirmed else None,
        service_mode="student_minor" if confirmed else "unknown_safe",
        persona_assignment_id="pa_01JTEST",
        persona_id="persona_miya",
        persona_version=4,
        relationship_stage="familiar",
        policy_bundle_version="cn-minor-v5",
        capabilities=("chat", "tutor"),
        obligations=(
            PolicyObligationSpec(
                code=PolicyObligation.POLICY_OBLIGATION_DO_NOT_PERSIST,
                params=ObligationParams(
                    max_session_seconds=None,
                    retention_ttl_seconds=None,
                    quiet_hours=None,
                    extras=(),
                ),
            ),
        ),
        policy_receipt_ids=("receipt_1",),
        session_epoch=1,
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        signature="",
    )


def _wire_payload(confirmed: bool) -> dict[str, object]:
    from services.session_runtime.profile_service import runtime_profile_wire_payload

    return runtime_profile_wire_payload(_control_profile(confirmed))


def _signed_wire_payload(confirmed: bool = False) -> dict[str, object]:
    return {**_wire_payload(confirmed), "signature": "a" * 64}


def _legacy_wire_payload(confirmed: bool) -> dict[str, object]:
    payload = _wire_payload(confirmed)
    obligations = payload["obligations"]
    assert isinstance(obligations, list)
    return {
        **payload,
        "signature_schema": "runtime-profile-v1",
        "obligations": [item["code"] for item in obligations],
    }


def _legacy_signed_wire_payload(confirmed: bool = False) -> dict[str, object]:
    return {**_legacy_wire_payload(confirmed), "signature": "a" * 64}


def _fence_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "ses_1",
        "epoch": 1,
        "device_id": "dev_1",
        "binding_id": "bind_1",
        "binding_version": 2,
        "runtime_profile_id": "rp_1",
        "actor_person_id": "person_a",
        "subject_person_id": "person_s",
        "subject_revision": 2,
        "generation_id": 5,
        "turn_id": 3,
        "tool_epoch": 1,
        "issued_at": "2026-08-09T10:00:00+09:00",
        "valid_until": "2026-08-09T10:05:00+09:00",
        "fingerprint": "f" * 64,
    }
    payload.update(overrides)
    return payload


def _write_fence_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "ses_1",
        "epoch": 1,
        "device_id": "dev_1",
        "binding_id": "bind_1",
        "binding_role": "guardian",
        "runtime_profile_id": "rp_1",
        "actor_subject_id": "person_a",
        "active_subject_id": "person_s",
        "subject_revision": 2,
        "binding_version": 2,
        "family_space_id": None,
        "generation_id": 5,
        "turn_id": 3,
        "tool_epoch": 1,
        "issued_at": "2026-08-09T10:00:00+09:00",
        "valid_until": "2026-08-09T10:05:00+09:00",
        "fingerprint": "f" * 64,
    }
    payload.update(overrides)
    return payload


def _policy_receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "receipt_id": "receipt_1",
        "actor_id": "person_a",
        "subject_id": "person_s",
        "device_id": "dev_1",
        "capability": "memory_capture",
        "effect": "allow_with_obligations",
        "reason_code": "unknown_safe_ephemeral",
        "obligations": ["DO_NOT_PERSIST"],
        "policy_version": "multi-subject-v1",
        "context_hash": "c" * 64,
        "binding_id": "bind_1",
        "binding_version": 2,
        "session_id": "ses_1",
        "session_epoch": 1,
        "runtime_profile_id": "rp_1",
        "subject_revision": 0,
        "created_at": "2026-08-09T10:00:00+09:00",
        "expires_at": "2026-08-09T10:05:00+09:00",
    }
    payload.update(overrides)
    return payload


def _session_event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_id": "evt_1",
        "event_type": "degraded_offline",
        "session_id": "ses_1",
        "session_epoch": 0,
        "device_id": None,
        "binding_id": None,
        "binding_version": None,
        "generation_id": None,
        "turn_id": None,
        "tool_epoch": None,
        "event_sequence": 0,
        "active_subject_id": None,
        "runtime_profile_id": None,
        "actor_id": None,
        "subject_revision": None,
        "occurred_at": "2026-08-09T10:00:00+09:00",
        "payload": {},
    }
    payload.update(overrides)
    return payload


def _binding_manifest_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "binding_id": "bind_1",
        "device_id": "dev_1",
        "declared_mode": "parent_for_child",
        "binding_version": 1,
        "status": "active",
        "reason": "create",
        "supersedes_binding_id": None,
        "family_space_id": "fam_1",
        "account_owner_id": "person_parent",
        "device_admin_ids": ["person_parent"],
        "primary_subject_ids": ["person_child"],
        "guardian_ids": ["person_parent"],
        "delegate_ids": [],
        "emergency_contact_ids": [],
        "member_ids": [],
        "roles": [{"person_id": "person_parent", "role": "guardian", "permissions": ["content.read"]}],
        "service_profile_version": "sp-v1",
        "policy_bundle_version": "cn-minor-v5",
        "consent_snapshot_id": "cs_1",
        "persona_assignment_id": "pa_1",
        "valid_from": "2026-08-09T10:00:00+09:00",
        "valid_until": None,
        "created_at": "2026-08-09T10:00:00+09:00",
    }
    payload.update(overrides)
    return payload


def _policy_decision_payload(**overrides: object) -> dict[str, object]:
    action_fence = _policy_action_fence_payload()
    payload: dict[str, object] = {
        "capability": "memory_capture",
        "purpose": "memory_capture",
        "effect": "allow_with_obligations",
        "reason_code": "unknown_safe_ephemeral",
        "obligations": [_structured_obligation("DO_NOT_PERSIST"), _structured_obligation("NO_MODEL_TRAINING")],
        "policy_version": "multi-subject-v1",
        "receipt_id": "receipt_1",
        "context_hash": "c" * 64,
        "action_resource_fence": action_fence,
        "action_fence_hash": action_fence["canonical_hash"],
        "created_at": "2026-08-09T10:00:00+09:00",
        "expires_at": "2026-08-09T10:05:00+09:00",
    }
    payload.update(overrides)
    return payload


def _structured_obligation(code: str) -> dict[str, object]:
    return {
        "code": code,
        "params": {
            "max_session_seconds": None,
            "retention_ttl_seconds": None,
            "quiet_hours": None,
            "extras": [],
        },
    }


def _policy_action_fence_payload() -> dict[str, object]:
    return {
        "action_fence_schema": "policy-action-resource-fence-v1",
        "capability": "memory_capture",
        "purpose": "memory_capture",
        "action_resource_id": "memory-candidate-1",
        "action_revision": 1,
        "action_evidence_hash": "e" * 64,
        "canonical_hash": "a" * 64,
        "family_space_id": None,
        "family_owner_subject_id": None,
        "proposal_id": None,
        "proposal_revision": None,
        "voter_subject_id": None,
        "approval_decision": None,
        "required_approval_subject_ids": [],
        "approval_snapshots": [],
        "capture_evidence_ids": [],
        "capture_evidence_hash": None,
        "consent_snapshot_id": None,
        "consent_snapshot_revision": None,
        "consent_snapshot_hash": None,
        "membership_snapshot_id": None,
        "membership_snapshot_revision": None,
        "membership_snapshot_hash": None,
        "generation_id": 5,
        "turn_id": 3,
        "tool_epoch": 1,
        "issued_at": "2026-08-09T10:00:00+09:00",
        "valid_until": "2026-08-09T10:05:00+09:00",
    }


def _subject_resolution_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "resolution": "confirmed",
        "candidate_subjects": [
            {"person_id": "person_child", "display_name": "小明", "confidence": 0.91},
            {"person_id": "person_parent", "display_name": "家长", "confidence": 0.05},
        ],
        "temporary_service_mode": "student_minor",
        "allowed_confirmation_methods": ["voice_question", "app_confirm"],
        "runtime_profile_id": "rp_1",
    }
    payload.update(overrides)
    return payload


def _memory_event_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_id": "evt_m1",
        "event_type": "memory_captured",
        "session_id": "ses_1",
        "session_epoch": 1,
        "generation_id": 5,
        "turn_id": 3,
        "event_sequence": 7,
        "actor_id": "person_a",
        "active_subject_id": "person_s",
        "binding_id": "bind_1",
        "binding_version": 2,
        "runtime_profile_id": "rp_1",
        "scope": "personal_private",
        "policy_receipt_id": "receipt_1",
        "occurred_at": "2026-08-09T10:00:00+09:00",
        "payload": {"text": "sample"},
    }
    payload.update(overrides)
    return payload


def _relationship_snapshot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "relationship_id": "rel_1",
        "snapshot_id": "rel_snapshot_1",
        "revision": 2,
        "relation_type": "guardian_of",
        "status": "active",
        "source_person_id": "person_g",
        "target_person_id": "person_s",
        "binding_id": "bind_1",
        "binding_version": 2,
        "binding_canonical_hash": "b" * 64,
        "canonical_hash": "c" * 64,
        "valid_from": "2026-08-09T10:00:00+09:00",
        "valid_until": "2026-08-09T11:00:00+09:00",
    }
    payload.update(overrides)
    return payload


def _recipient_snapshot_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "recipient_id": "rec_1",
        "person_id": "person_g",
        "role": "guardian",
        "relationship_id": "rel_1",
        "relationship_snapshot_id": "rel_snapshot_1",
        "relationship_revision": 2,
        "relationship_status": "active",
        "channels": ["wechat_subscription", "sms"],
        "channel_index": 0,
        "status": "pending",
        "valid_from": "2026-08-09T10:00:00+09:00",
        "valid_until": None,
    }
    payload.update(overrides)
    return payload


def _notification_intent_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "intent_id": "intent_1",
        "idempotency_key": "ik_1",
        "intent_kind": "crisis_safety",
        "subject_person_id": "person_s",
        "source_event_id": "evt_1",
        "policy_receipt_id": "receipt_1",
        "template_key": "crisis_safety_notice",
        "template_params": {"role_label": "孩子"},
        "reason_code": "safety_concern",
        "script_version": "v1.0.0",
        "occurred_at": "2026-08-09T10:00:00+09:00",
        "status": "pending",
        "created_at": "2026-08-09T10:00:00+09:00",
        "updated_at": "2026-08-09T10:00:00+09:00",
        "cancelled_reason": None,
        "cancelled_at": None,
        "delivered_at": None,
        "fence": _fence_payload(),
        "relationship_snapshots": [_relationship_snapshot_payload()],
        "recipients": [_recipient_snapshot_payload()],
    }
    payload.update(overrides)
    return payload


def _session_epoch_fence_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": "ses_1",
        "session_epoch": 2,
        "device_id": "dev_1",
        "binding_id": "bind_1",
        "binding_version": 2,
        "generation_id": 5,
        "turn_id": 3,
        "tool_epoch": 1,
        "runtime_profile_id": "rp_1",
        "actor_id": "person_a",
        "active_subject_id": "person_s",
        "subject_revision": 2,
        "issued_at": "2026-08-09T10:00:00+09:00",
        "valid_until": "2026-08-09T10:05:00+09:00",
        "fingerprint": "f" * 64,
    }
    payload.update(overrides)
    return payload


OBJECT_FIXTURES: dict[str, dict[str, object]] = {
    "BindingManifest": _binding_manifest_payload(),
    "RuntimeProfile": _legacy_wire_payload(True),
    "RuntimeProfileSigned": _legacy_signed_wire_payload(),
    "RuntimeProfileV2": _wire_payload(True),
    "RuntimeProfileSignedV2": _signed_wire_payload(),
    "PolicyDecision": _policy_decision_payload(),
    "PolicyReceipt": _policy_receipt_payload(),
    "SubjectResolution": _subject_resolution_payload(),
    "MemoryWriteFence": _write_fence_payload(),
    "MemoryEvent": _memory_event_payload(),
    "NotificationFence": _fence_payload(),
    "RelationshipSnapshot": _relationship_snapshot_payload(),
    "RecipientSnapshot": _recipient_snapshot_payload(),
    "NotificationIntent": _notification_intent_payload(),
    "SessionEpochFence": _session_epoch_fence_payload(),
    "SessionEvent": _session_event_payload(),
}


# One invalid-boundary mutation per contract (a superset is exercised in the
# dedicated tests below; this table keeps the per-object matrix readable).
OBJECT_BOUNDARY_CASES: dict[str, list[dict[str, object]]] = {
    "BindingManifest": [{"binding_version": 0}, {"declared_mode": "bogus"}, {"status": "bogus"}],
    "RuntimeProfile": [
        {"binding_version": "1"},
        {"binding_version": 0},
        {"binding_version": None},
        {"session_epoch": 0},
        {"runtime_profile_id": ""},
        {"runtime_profile_id": "x" * 129},
        {"expires_at": "not-a-date"},
        {"issued_at": "2026-08-09"},
        {"issued_at": "2026-08-09T10:00:00"},
        {"expires_at": "yesterday"},
        {"issued_at": 0},
        {"issued_at": 1691575200},
        {"issued_at": True},
        {"issued_at": "2026-08-09 10:00:00+09:00"},
        {"expires_at": "2026-08-09T10:00:00+9:00"},
        {"capabilities": ["chat", "chat"]},
        {"obligations": ["DO_NOT_PERSIST", "DO_NOT_PERSIST"]},
        {"capabilities": ["chat", "bogus_capability"]},
        {"speaker_confidence": 1.5},
        {"speaker_confidence": float("nan")},
        {"speaker_confidence": float("inf")},
    ],
    "RuntimeProfileSigned": [{"signature": "zz"}, {"signature": "x" * 65}],
    "RuntimeProfileV2": [
        {"binding_version": "1"},
        {"binding_version": 0},
        {"binding_version": None},
        {"session_epoch": 0},
        {"runtime_profile_id": ""},
        {"runtime_profile_id": "x" * 129},
        {"expires_at": "not-a-date"},
        {"issued_at": "2026-08-09"},
        {"issued_at": "2026-08-09T10:00:00"},
        {"expires_at": "yesterday"},
        {"issued_at": 0},
        {"issued_at": 1691575200},
        {"issued_at": True},
        {"issued_at": "2026-08-09 10:00:00+09:00"},
        {"expires_at": "2026-08-09T10:00:00+9:00"},
        {"capabilities": ["chat", "chat"]},
        {
            "obligations": [
                _structured_obligation("DO_NOT_PERSIST"),
                _structured_obligation("DO_NOT_PERSIST"),
            ]
        },
        {"obligations": [_structured_obligation("NOT_CANONICAL")]},
        {"capabilities": ["chat", "bogus_capability"]},
        {"speaker_confidence": 1.5},
        {"speaker_confidence": float("nan")},
        {"speaker_confidence": float("inf")},
    ],
    "RuntimeProfileSignedV2": [{"signature": "zz"}, {"signature": "x" * 65}],
    "PolicyDecision": [{"effect": "bogus"}, {"obligations": ["bogus_obligation"]}, {"context_hash": "zz"}],
    "PolicyReceipt": [
        {"binding_version": 0},
        {"binding_version": "1"},
        {"session_epoch": 0},
        {"context_hash": "zz"},
        {"context_hash": "z" * 64},
        {"context_hash": "C" * 64},
        {"capability": "bogus_capability"},
    ],
    "SubjectResolution": [
        {"resolution": "bogus"},
        {"temporary_service_mode": "bogus"},
        {"candidate_subjects": [{"person_id": "p", "display_name": "n", "confidence": 1.5}]},
    ],
    "MemoryWriteFence": [
        {"epoch": 0},
        {"binding_version": 0},
        {"binding_version": "1"},
        {"fingerprint": "xyz"},
        {"fingerprint": "z" * 64},
        {"binding_role": "bogus"},
    ],
    "MemoryEvent": [
        {"session_epoch": 0},
        {"binding_version": "1"},
        {"event_sequence": -1},
        {"scope": "bogus"},
        {"event_type": "bogus"},
    ],
    "NotificationFence": [
        {"epoch": 0},
        {"binding_version": "1"},
        {"binding_version": 0},
        {"fingerprint": "xyz"},
        {"fingerprint": "z" * 64},
        {"session_id": ""},
        {"session_id": "x" * 129},
    ],
    "RelationshipSnapshot": [{"status": "bogus"}, {"role": "bogus"}],
    "RecipientSnapshot": [
        {"status": "bogus"},
        {"role": "bogus"},
        {"channel_index": -1},
        {"channels": ["sms", "sms"]},
    ],
    "NotificationIntent": [
        {"intent_kind": "bogus"},
        {"status": "bogus"},
        {"reason_code": "bogus"},
        {"template_key": "bogus"},
        {"script_version": "bad script!"},
        {"idempotency_key": ""},
    ],
    "SessionEpochFence": [
        {"session_epoch": 0},
        {"generation_id": -1},
        {"fingerprint": "xyz"},
    ],
    "SessionEvent": [{"event_type": "bogus"}, {"event_sequence": -1}],
}


# ---------------------------------------------------------------------------
# Schema structure
# ---------------------------------------------------------------------------


def test_objects_section_structural_integrity() -> None:
    data = _schema()
    objects = data["objects"]
    props = objects["properties"]
    assert props["objects_version"]["const"] == 2
    assert tuple(entry["name"] for entry in props["contracts"]["const"]) == gen.REQUIRED_OBJECT_CONTRACTS
    defs = _object_defs()
    assert set(defs) >= set(gen.REQUIRED_OBJECT_CONTRACTS)
    assert data["properties"]["objects"]["$ref"] == "#/objects"


def test_every_definition_fail_closed_and_refs_resolvable() -> None:
    defs = _object_defs()
    for name, schema in defs.items():
        assert schema["title"] == name
        if "enum" in schema:
            assert schema["type"] == "string"
            assert len(schema["enum"]) == len(set(schema["enum"]))
            continue
        if "allOf" in schema:
            ref = schema["allOf"][0]["$ref"]
            assert ref.startswith(("#/$defs/", "#/objects/properties/definitions/properties/"))
            assert schema["unevaluatedProperties"] is False
            assert "signature" in schema["required"]
            continue
        assert schema.get("additionalProperties") is False or schema.get("unevaluatedProperties") is False
        required = set(schema["required"])
        assert required and required <= set(schema["properties"])


def test_binding_version_and_epoch_are_canonical_integers() -> None:
    defs = _object_defs()
    for name in ("NotificationFence", "MemoryWriteFence", "MemoryEvent"):
        prop = defs[name]["properties"]["binding_version"]
        assert prop["type"] == "integer" and prop["minimum"] == 1
    assert defs["RuntimeProfile"]["properties"]["binding_version"]["minimum"] == 1
    assert defs["PolicyReceipt"]["properties"]["binding_version"]["minimum"] == 1
    relationship_binding = defs["RelationshipSnapshot"]["properties"]["binding_version"]
    assert relationship_binding["type"] == "integer" and relationship_binding["minimum"] == 1
    for name in ("NotificationFence", "MemoryWriteFence", "PolicyReceipt", "RuntimeProfile", "MemoryEvent", "SessionEpochFence"):
        props = defs[name]["properties"]
        key = "epoch" if name in ("NotificationFence", "MemoryWriteFence") else "session_epoch"
        assert props[key]["minimum"] == 1, f"{name}.{key} must be >= 1"
    # SessionEvent keeps 0 for pre-issuance lifecycle, with the no-authorization note.
    assert defs["SessionEvent"]["properties"]["session_epoch"]["minimum"] == 0
    assert "不得授权任何敏感行为" in defs["SessionEvent"]["properties"]["session_epoch"]["description"]


# ---------------------------------------------------------------------------
# Pydantic v2 models: fail-closed matrix
# ---------------------------------------------------------------------------


def test_runtime_profile_models_validate_and_fail_closed() -> None:
    from pydantic import ValidationError

    RuntimeProfile = CONTRACTS_MODULE.RuntimeProfileV2
    RuntimeProfileSigned = CONTRACTS_MODULE.RuntimeProfileSignedV2
    payload = _wire_payload(confirmed=True)
    model = RuntimeProfile.model_validate(payload)
    assert model.service_mode == "student_minor"
    assert model.persona.version == 4
    assert model.session_epoch == 1
    # roundtrip (json mode) is stable
    again = RuntimeProfile.model_validate(model.model_dump(mode="json"))
    assert again.model_dump(mode="json") == model.model_dump(mode="json")
    # unknown-safe null profile validates
    unknown = RuntimeProfile.model_validate(_wire_payload(confirmed=False))
    assert unknown.active_subject_id is None and unknown.speaker_confidence is None
    assert unknown.service_mode == "unknown_safe"
    # signed envelope
    RuntimeProfileSigned.model_validate(_signed_wire_payload())
    for bad in (
        {"binding_version": "1"},
        {"binding_version": 0},
        {"binding_version": None},
        {"session_epoch": 0},
        {"service_mode": "not-a-mode"},
        {"subject_category": "adult_typo"},
        {"speaker_confidence": 1.5},
        {"extra_key": 1},
    ):
        with pytest.raises(ValidationError):
            RuntimeProfile.model_validate({**payload, **bad})
    with pytest.raises(ValidationError):
        RuntimeProfile.model_validate({key: value for key, value in payload.items() if key != "service_mode"})
    with pytest.raises(ValidationError):
        RuntimeProfileSigned.model_validate(payload)  # missing signature
    with pytest.raises(ValidationError):
        RuntimeProfileSigned.model_validate({**payload, "signature": "zz"})  # not hex64


def test_python_date_time_is_wire_strict() -> None:
    """date-time fields must reject non-string/coercible wire values (int->Unix,
    bool, float, bytes), date-only, no-offset, natural language, space-separated
    and non-padded offsets; RFC3339-permitted lowercase t/z is accepted and
    normalized. Both model_validate and model_validate_json are covered."""
    import json as _json

    from pydantic import ValidationError

    RuntimeProfileSigned = CONTRACTS_MODULE.RuntimeProfileSignedV2
    payload = _signed_wire_payload()
    for bad_value in (
        0,
        1691575200,
        True,
        1.5,
        b"2026-08-09T10:00:00Z",
        datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
    ):
        with pytest.raises(ValidationError):
            RuntimeProfileSigned.model_validate({**payload, "issued_at": bad_value})
    for bad in (
        "2026-08-09",
        "2026-08-09T10:00:00",
        "yesterday",
        "2026-08-09 10:00:00+09:00",
        "2026-08-09T10:00:00+9:00",
        "2026-02-30T10:00:00+09:00",
    ):
        with pytest.raises(ValidationError):
            RuntimeProfileSigned.model_validate({**payload, "issued_at": bad})
    with pytest.raises(ValidationError):
        RuntimeProfileSigned.model_validate_json(_json.dumps({**payload, "issued_at": 0}))
    ok = RuntimeProfileSigned.model_validate_json(_json.dumps(payload))
    assert ok.issued_at.utcoffset() is not None
    # RFC3339 §5.6 permits lowercase t/z; all three languages accept it.
    lowered = RuntimeProfileSigned.model_validate({**payload, "issued_at": "2026-08-09t10:00:00z"})
    assert lowered.issued_at.utcoffset() is not None
    with pytest.raises(ValidationError):
        RuntimeProfileSigned.model_validate({**payload, "issued_at": "2026-08-09 10:00:00+09:00"})


def test_fence_receipt_epoch_and_binding_version_fail_closed() -> None:
    from pydantic import ValidationError

    for model_name, good, bad_cases in (
        ("NotificationFence", _fence_payload(), [{"epoch": 0}, {"binding_version": "1"}, {"binding_version": 0}, {"binding_version": None}]),
        ("MemoryWriteFence", _write_fence_payload(), [{"epoch": 0}, {"binding_version": "1"}, {"binding_version": 0}, {"binding_version": None}]),
        ("PolicyReceipt", _policy_receipt_payload(), [{"session_epoch": 0}, {"binding_version": 0}, {"binding_version": "1"}, {"binding_version": None}]),
    ):
        model = getattr(CONTRACTS_MODULE, model_name)
        model.model_validate(good)
        for bad in bad_cases:
            with pytest.raises(ValidationError):
                model.model_validate({**good, **bad})
    # SessionEvent may express pre-issuance lifecycle at epoch 0
    CONTRACTS_MODULE.SessionEvent.model_validate(_session_event_payload())
    CONTRACTS_MODULE.SessionEvent.model_validate(
        _session_event_payload(
            session_epoch=3,
            event_type="subject_switched",
            device_id="dev_1",
            binding_id="bind_1",
            binding_version=2,
            generation_id=5,
            turn_id=3,
            tool_epoch=1,
            runtime_profile_id="rp_1",
            actor_id="person_a",
            active_subject_id="person_s",
            subject_revision=2,
        )
    )
    with pytest.raises(ValidationError):
        CONTRACTS_MODULE.SessionEvent.model_validate(_session_event_payload(event_type="subject_switched", event_sequence=-1))


def test_every_object_valid_missing_unknown_boundary() -> None:
    """Python: every object contract accepts its valid fixture and rejects
    unknown keys, every missing required key, and invalid boundaries."""
    from pydantic import ValidationError

    defs = _object_defs()
    for name, payload in OBJECT_FIXTURES.items():
        model = getattr(CONTRACTS_MODULE, name)
        model.model_validate(payload)  # valid
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "unknown_key": 1})
        required = defs[name]["required"]
        for key in required:
            with pytest.raises(ValidationError):
                model.model_validate({k: v for k, v in payload.items() if k != key})
        for bad in OBJECT_BOUNDARY_CASES.get(name, []):
            with pytest.raises(ValidationError):
                model.model_validate({**payload, **bad})


# ---------------------------------------------------------------------------
# Golden test: Control runtime_profile_wire_payload vs schema
# ---------------------------------------------------------------------------


def test_golden_control_wire_payload_matches_schema() -> None:
    from pydantic import ValidationError

    RuntimeProfile = CONTRACTS_MODULE.RuntimeProfileV2
    for confirmed in (True, False):
        payload = _wire_payload(confirmed)
        schema_props = _object_defs()["RuntimeProfileV2"]["properties"]
        # field set matches the schema exactly
        assert set(payload) == set(schema_props)
        # and the generated model
        assert set(payload) == set(RuntimeProfile.model_fields)
        # nested persona shape matches the schema
        assert set(payload["persona"]) == set(_object_defs()["PersonaSnapshot"]["properties"])
        # validates
        RuntimeProfile.model_validate(payload)
    # extra field fails
    with pytest.raises(ValidationError):
        RuntimeProfile.model_validate({**_wire_payload(True), "tampered": 1})
    # missing field fails
    for missing in ("service_mode", "signature_schema", "persona", "expires_at"):
        with pytest.raises(ValidationError):
            RuntimeProfile.model_validate({k: v for k, v in _wire_payload(True).items() if k != missing})


def test_runtime_profile_signed_enforced_by_real_json_schema_validator() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    doc = _schema()
    registry = Registry().with_resource(gen.SCHEMA_ID, Resource.from_contents(doc))

    def validator_for(def_name: str) -> Draft202012Validator:
        return Draft202012Validator(
            {"$ref": f"{gen.SCHEMA_ID}#/objects/properties/definitions/properties/{def_name}"},
            registry=registry,
        )

    signed_validator = validator_for("RuntimeProfileSignedV2")
    payload_validator = validator_for("RuntimeProfileV2")
    signed_validator.validate(_signed_wire_payload())
    payload_validator.validate(_wire_payload(False))
    for bad in (
        _wire_payload(False),  # missing signature
        {**_signed_wire_payload(), "tampered": 1},  # unknown property (unevaluatedProperties false)
        {**_signed_wire_payload(), "signature": "short"},
        {**_signed_wire_payload(), "binding_version": "1"},
        {**_signed_wire_payload(), "binding_version": 0},
        {**_signed_wire_payload(), "binding_version": None},
        {**_signed_wire_payload(), "session_epoch": 0},
        {**_signed_wire_payload(), "service_mode": "made_up"},
    ):
        with pytest.raises(jsonschema.ValidationError):
            signed_validator.validate(bad)
    # binding_version constraints are also enforced on the unsigned model
    for bad in ({"binding_version": "1"}, {"binding_version": 0}, {"binding_version": None}, {"session_epoch": 0}):
        with pytest.raises(jsonschema.ValidationError):
            payload_validator.validate({**_wire_payload(True), **bad})


# ---------------------------------------------------------------------------
# Determinism, source hash, artifacts
# ---------------------------------------------------------------------------


def test_generation_deterministic_and_embeds_source_hash(tmp_path: Path) -> None:
    schema_hash = hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
    outputs: list[dict[Path, str]] = []
    for index in range(2):
        root = tmp_path / str(index)
        schema_tmp = root / gen.SCHEMA_REL_PATH
        schema_tmp.parent.mkdir(parents=True)
        schema_tmp.write_bytes(SCHEMA_PATH.read_bytes())
        outputs.append(gen.build_outputs(root))
    for (_, a), (_, b) in zip(outputs[0].items(), outputs[1].items(), strict=True):
        assert a == b, "regeneration must be byte-identical"
    python_text = outputs[0][tmp_path / "0/packages/contracts/generated/python/multi_subject_contracts.py"]
    assert f"sha256:{schema_hash}" in python_text




# ---------------------------------------------------------------------------
# Go artifact: gofmt + real compilation with the Validate seam
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# TypeScript artifact: syntax + runtime guards/validators (no zod dependency)
# ---------------------------------------------------------------------------


