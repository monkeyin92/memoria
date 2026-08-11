"""Canonical multi-subject object contract v2 acceptance tests.

The public seams are the canonical JSON Schema and its generated language
artifacts.  These tests deliberately avoid service-internal implementation
details; production wire functions are used only as independent golden
producers where compatibility is part of the contract.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts import generate_multi_subject_contracts as gen

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / gen.SCHEMA_REL_PATH
PYTHON_ARTIFACT = REPO_ROOT / "packages/contracts/generated/python/multi_subject_contracts.py"
TYPESCRIPT_ARTIFACT = REPO_ROOT / "packages/contracts/generated/typescript/multi_subject_contracts.ts"
GO_ARTIFACT = REPO_ROOT / "packages/contracts/generated/go/multi_subject_contracts.go"


def _schema() -> dict[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_generated_python() -> object:
    name = "multi_subject_contracts_v2_acceptance"
    spec = importlib.util.spec_from_file_location(name, PYTHON_ARTIFACT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _obligation(code: str = "WRITE_POLICY_RECEIPT") -> dict[str, object]:
    return {
        "code": code,
        "params": {
            "max_session_seconds": None,
            "retention_ttl_seconds": None,
            "quiet_hours": None,
            "extras": [],
        },
    }


def _memory_fence(actor: str = "person-a") -> dict[str, object]:
    return {
        "session_id": "session-1",
        "epoch": 2,
        "device_id": "device-1",
        "binding_id": "binding-1",
        "binding_role": "primary_subject",
        "runtime_profile_id": "profile-2",
        "actor_subject_id": actor,
        "active_subject_id": actor,
        "subject_revision": 3,
        "binding_version": 4,
        "family_space_id": "family-1",
        "generation_id": 7,
        "turn_id": 5,
        "tool_epoch": 1,
        "issued_at": "2026-08-09T10:00:00Z",
        "valid_until": "2026-08-09T10:10:00Z",
        "fingerprint": "f" * 64,
    }


def _policy_action_fence(
    capability: str = "memory_promotion", *, voter: str | None = None
) -> dict[str, object]:
    family = capability.startswith("family_shared_memory_")
    proposal = capability == "family_shared_memory_proposal"
    approval = capability == "family_shared_memory_approval"
    promotion = capability == "family_shared_memory_promotion"
    approval_snapshots = (
        [
            {
                "subject_id": "person-a",
                "snapshot_id": "approval-snapshot-person-a",
                "revision": 2,
                "canonical_hash": "a" * 64,
            },
            {
                "subject_id": "person-b",
                "snapshot_id": "approval-snapshot-person-b",
                "revision": 2,
                "canonical_hash": "b" * 64,
            },
        ]
        if promotion
        else []
    )
    return {
        "action_fence_schema": "policy-action-resource-fence-v1",
        "capability": capability,
        "purpose": capability,
        "action_resource_id": "proposal-1" if family else "memory-candidate-1",
        "action_revision": 1,
        "action_evidence_hash": "9" * 64,
        "canonical_hash": "8" * 64,
        "family_space_id": "family-1" if family else None,
        "family_owner_subject_id": "person-a" if family else None,
        "proposal_id": "proposal-1" if family else None,
        "proposal_revision": 1 if family else None,
        "voter_subject_id": voter if approval else None,
        "approval_decision": "confirm" if approval else None,
        "required_approval_subject_ids": ["person-a", "person-b"] if family else [],
        "approval_snapshots": approval_snapshots,
        "capture_evidence_ids": ["evidence-1"] if proposal else [],
        "capture_evidence_hash": "7" * 64 if proposal else None,
        "consent_snapshot_id": "consent-family-1" if family else None,
        "consent_snapshot_revision": 3 if family else None,
        "consent_snapshot_hash": "6" * 64 if family else None,
        "membership_snapshot_id": "membership-family-1" if family else None,
        "membership_snapshot_revision": 4 if family else None,
        "membership_snapshot_hash": "5" * 64 if family else None,
        "generation_id": 7,
        "turn_id": 5,
        "tool_epoch": 1,
        "issued_at": "2026-08-09T10:00:00Z",
        "valid_until": "2026-08-09T10:08:00Z",
    }


def _runtime_profile_v2() -> dict[str, object]:
    return {
        "signature_schema": "runtime-profile-v2",
        "runtime_profile_id": "profile-2",
        "device_id": "device-1",
        "session_id": "session-1",
        "actor_id": "person-a",
        "binding_id": "binding-1",
        "binding_version": 4,
        "active_subject_id": "person-a",
        "subject_revision": 3,
        "subject_category": "adult",
        "age_band": "adult",
        "speaker_state": "confirmed",
        "speaker_confidence": 0.98,
        "service_mode": "adult_companion",
        "persona_assignment_id": "persona-assignment-1",
        "persona": {
            "persona_id": "persona-miya",
            "version": 2,
            "relationship_stage": "familiar",
        },
        "policy_bundle_version": "policy-v2",
        "capabilities": ["chat", "memory_promotion"],
        "obligations": [_obligation()],
        "policy_receipt_ids": ["receipt-runtime-1"],
        "session_epoch": 2,
        "issued_at": "2026-08-09T10:00:00Z",
        "expires_at": "2026-08-09T10:10:00Z",
    }


def _legacy_runtime_profile() -> dict[str, object]:
    payload = _runtime_profile_v2()
    payload["signature_schema"] = "runtime-profile-v1"
    payload["obligations"] = ["DO_NOT_PERSIST", "WRITE_POLICY_RECEIPT"]
    return payload


def _legacy_policy_receipt() -> dict[str, object]:
    return {
        "receipt_id": "legacy-receipt-1",
        "actor_id": "person-a",
        "subject_id": "person-a",
        "device_id": "device-1",
        "capability": "memory_capture",
        "effect": "allow_with_obligations",
        "reason_code": "legacy_migration_read",
        "obligations": ["DO_NOT_PERSIST"],
        "policy_version": "policy-v1",
        "context_hash": "c" * 64,
        "binding_id": "binding-1",
        "binding_version": 4,
        "session_id": "session-1",
        "session_epoch": 2,
        "runtime_profile_id": "profile-1",
        "subject_revision": 3,
        "created_at": "2026-08-09T10:00:00Z",
        "expires_at": "2026-08-09T10:05:00Z",
    }


def _policy_receipt_v2(
    capability: str = "memory_promotion", *, voter: str | None = None
) -> dict[str, object]:
    action_fence = _policy_action_fence(capability, voter=voter)
    actor = voter or ("compiler-1" if capability == "family_shared_memory_promotion" else "person-a")
    receipt_suffix = voter or capability
    return {
        "receipt_id": f"receipt-{receipt_suffix}",
        "actor_id": actor,
        "subject_id": voter or "person-a",
        "resource_owner_id": "person-a",
        "device_id": "device-1",
        "capability": capability,
        "purpose": capability,
        "effect": "allow_with_obligations",
        "reason_code": "subject_confirmed",
        "obligations": [_obligation()],
        "policy_version": "policy-v2",
        "context_hash": "c" * 64,
        "action_resource_fence": action_fence,
        "action_fence_hash": action_fence["canonical_hash"],
        "consent_snapshot_ids": ["consent-1"],
        "consent_snapshot_revisions": [2],
        "relationship_snapshot_ids": [],
        "relationship_snapshot_revisions": [],
        "binding_id": "binding-1",
        "binding_version": 4,
        "binding_canonical_hash": "b" * 64,
        "session_id": "session-1",
        "session_epoch": 2,
        "runtime_profile_id": "profile-2",
        "subject_revision": 3,
        "device_trust": "verified",
        "data_classification": "private",
        "safety_state": "normal",
        "jurisdiction": "CN",
        "created_at": "2026-08-09T10:00:00Z",
        "expires_at": "2026-08-09T10:09:00Z",
        "exact_fence": True,
    }


def _confirmation(subject: str) -> dict[str, object]:
    approval_receipt = _policy_receipt_v2(
        "family_shared_memory_approval", voter=subject
    )
    action_fence = _memory_fence(subject)
    action_fence["fingerprint"] = "d" * 64
    return {
        "evidence_id": f"approval-{subject}",
        "proposal_id": "proposal-1",
        "proposal_revision": 1,
        "subject_id": subject,
        "decision": "confirm",
        "authentication_evidence_id": f"auth-{subject}",
        "authentication_evidence_hash": "e" * 64,
        "action_evidence_hash": "d" * 64,
        "action_fence": action_fence,
        "approval_snapshot_id": f"approval-snapshot-{subject}",
        "approval_snapshot_revision": 2,
        "approval_snapshot_hash": ("a" if subject == "person-a" else "b") * 64,
        "approval_policy_receipt_id": approval_receipt["receipt_id"],
        "approval_policy_receipt": approval_receipt,
        "approval_fence": deepcopy(approval_receipt["action_resource_fence"]),
        "proposal_status_before": "pending",
        "proposal_status_after": "approvals_complete" if subject == "person-b" else "pending",
        "occurred_at": "2026-08-09T10:02:00Z",
    }


def _objection(subject: str = "person-b") -> dict[str, object]:
    action_fence = _memory_fence(subject)
    action_fence["fingerprint"] = "1" * 64
    return {
        "evidence_id": f"objection-{subject}",
        "proposal_id": "proposal-1",
        "proposal_revision": 1,
        "subject_id": subject,
        "decision": "object",
        "authentication_evidence_id": f"auth-{subject}",
        "authentication_evidence_hash": "2" * 64,
        "action_evidence_hash": "1" * 64,
        "action_fence": action_fence,
        "approval_snapshot_id": None,
        "approval_snapshot_revision": None,
        "approval_snapshot_hash": None,
        "approval_policy_receipt_id": None,
        "approval_policy_receipt": None,
        "approval_fence": None,
        "proposal_status_before": "approvals_complete",
        "proposal_status_after": "frozen",
        "occurred_at": "2026-08-09T10:02:00Z",
    }


def _shared_proposal() -> dict[str, object]:
    proposal_receipt = _policy_receipt_v2("family_shared_memory_proposal")
    return {
        "proposal_id": "proposal-1",
        "proposal_revision": 1,
        "family_space_id": "family-1",
        "family_owner_subject_id": "person-a",
        "proposer_subject_id": "person-a",
        "co_subject_ids": ["person-b"],
        "required_approval_subject_ids": ["person-a", "person-b"],
        "source_evidence_ids": ["evidence-1"],
        "capture_evidence_hash": "7" * 64,
        "consent_snapshot_id": "consent-family-1",
        "consent_snapshot_revision": 3,
        "consent_snapshot_hash": "6" * 64,
        "membership_snapshot_id": "membership-family-1",
        "membership_snapshot_revision": 4,
        "membership_snapshot_hash": "5" * 64,
        "proposal_policy_receipt_id": proposal_receipt["receipt_id"],
        "proposal_policy_receipt": proposal_receipt,
        "proposal_fence": _memory_fence("person-a"),
        "status": "pending",
        "created_at": "2026-08-09T10:00:00Z",
        "expires_at": "2026-08-09T11:00:00Z",
        "payload": {"title": "family story"},
    }


def _shared_promotion() -> dict[str, object]:
    promotion_receipt = _policy_receipt_v2("family_shared_memory_promotion")
    return {
        "promotion_id": "promotion-1",
        "proposal_id": "proposal-1",
        "proposal_revision": 1,
        "family_space_id": "family-1",
        "proposal_policy_receipt_id": "receipt-family_shared_memory_proposal",
        "promotion_policy_receipt_id": promotion_receipt["receipt_id"],
        "promotion_policy_receipt": promotion_receipt,
        "promotion_fence": _memory_fence("person-a"),
        "proposal_status_before": "approvals_complete",
        "proposal_status_after": "promoted",
        "required_approval_subject_ids": ["person-a", "person-b"],
        "confirmation_evidence": [_confirmation("person-a"), _confirmation("person-b")],
        "current_consent_snapshot_id": "consent-family-1",
        "current_consent_snapshot_revision": 3,
        "current_consent_snapshot_hash": "6" * 64,
        "current_membership_snapshot_id": "membership-family-1",
        "current_membership_snapshot_revision": 4,
        "current_membership_snapshot_hash": "5" * 64,
        "promoted_record_id": "memory-1",
        "promoted_at": "2026-08-09T10:05:00Z",
    }


def _device_attestation() -> dict[str, object]:
    return {
        "attestation_schema": "device-attestation-v1",
        "attestation_id": "attestation-1",
        "device_id": "device-1",
        "certificate_id": "certificate-1",
        "certificate_status": "active",
        "device_lifecycle_status": "bound",
        "binding_id": "binding-1",
        "binding_version": 4,
        "firmware_version": "1.2.3",
        "firmware_security_version": 12,
        "firmware_sha256": "d" * 64,
        "bootloader_version": "1.0.0",
        "capability_manifest_hash": "e" * 64,
        "capabilities": [
            "secure_element",
            "physical_microphone_cut",
            "hardware_privacy_light",
            "ab_ota",
        ],
        "attested_capabilities": [
            "physical_microphone_cut",
            "hardware_privacy_light",
        ],
        "physical_mute_state": "engaged",
        "privacy_light_state": "on",
        "sim_status": "active",
        "active_ota_slot": "a",
        "ota_boot_status": "confirmed",
        "anti_rollback_floor_version": "1.2.3",
        "anti_rollback_floor_security_version": 10,
        "monotonic_counter": 8,
        "last_command_sequence": 3,
        "nonce": "nonce_0123456789abcdef",
        "occurred_at": "2026-08-09T10:00:00Z",
        "expires_at": "2026-08-09T10:02:00Z",
        "signer_key_id": "device-key-1",
        "signature_algorithm": "ed25519",
        "signature": "A" * 86,
    }


def _ota_assignment() -> dict[str, object]:
    return {
        "assignment_id": "ota-assignment-1",
        "device_id": "device-1",
        "target_slot": "b",
        "target_version": "1.3.0",
        "target_firmware_security_version": 13,
        "artifact_sha256": "a" * 64,
        "artifact_size_bytes": 1024,
        "min_bootloader_version": "1.0.0",
        "anti_rollback_floor_version": "1.2.3",
        "anti_rollback_floor_security_version": 10,
        "channel": "stable",
        "status": "pending",
        "assigned_at": "2026-08-09T10:00:00Z",
        "expires_at": "2026-08-10T10:00:00Z",
        "signer_key_id": "ota-signer-1",
        "signature_algorithm": "ed25519",
        "signature": "B" * 86,
    }


def _ota_state_receipt() -> dict[str, object]:
    return {
        "receipt_id": "ota-receipt-1",
        "device_id": "device-1",
        "certificate_id": "certificate-1",
        "assignment_id": "ota-assignment-1",
        "highest_accepted_version": "1.3.0",
        "highest_accepted_firmware_security_version": 13,
        "anti_rollback_floor_version": "1.2.3",
        "anti_rollback_floor_security_version": 10,
        "active_slot": "a",
        "pending_slot": "b",
        "boot_status": "booting",
        "slots": [
            {
                "slot": "a",
                "boot_status": "confirmed",
                "firmware_version": "1.2.3",
                "firmware_security_version": 12,
                "artifact_sha256": "a" * 64,
                "boot_attempts": 0,
                "confirmed_at": "2026-08-09T09:00:00Z",
                "last_error_code": None,
            },
            {
                "slot": "b",
                "boot_status": "booting",
                "firmware_version": "1.3.0",
                "firmware_security_version": 13,
                "artifact_sha256": "b" * 64,
                "boot_attempts": 1,
                "confirmed_at": None,
                "last_error_code": None,
            },
        ],
        "max_boot_attempts": 2,
        "rollback_from_version": None,
        "rollback_to_version": None,
        "monotonic_counter": 9,
        "occurred_at": "2026-08-09T10:01:00Z",
        "signer_key_id": "device-key-1",
        "signature_algorithm": "ed25519",
        "signature": "C" * 86,
    }


def _remote_command() -> dict[str, object]:
    return {
        "command_id": "command-1",
        "idempotency_key": "command-key-1",
        "device_id": "device-1",
        "actor_id": "operator-1",
        "command_type": "collect_diagnostics",
        "reason_code": "support_case",
        "ota_assignment_id": None,
        "parameters_hash": "0" * 64,
        "signer_key_id": "fleet-command-key-1",
        "signature_algorithm": "ed25519",
        "target_certificate_id": "certificate-1",
        "expected_binding_id": "binding-1",
        "expected_binding_version": 4,
        "expected_monotonic_counter": 8,
        "expected_firmware_security_version": 12,
        "command_sequence": 4,
        "issued_at": "2026-08-09T10:00:00Z",
        "expires_at": "2026-08-09T10:02:00Z",
        "signature": "D" * 86,
    }


def _remote_execution() -> dict[str, object]:
    return {
        "execution_id": "execution-1",
        "command": _remote_command(),
        "current_attestation": _device_attestation(),
        "accepted_at": "2026-08-09T10:01:00Z",
    }


def _withdrawal() -> dict[str, object]:
    action_fence = _memory_fence("person-a")
    action_fence["fingerprint"] = "4" * 64
    return {
        "withdrawal_evidence_id": "withdrawal-1",
        "proposal_id": "proposal-1",
        "proposal_revision": 1,
        "subject_id": "person-a",
        "authentication_evidence_id": "auth-person-a",
        "authentication_evidence_hash": "3" * 64,
        "action_evidence_hash": "4" * 64,
        "action_fence": action_fence,
        "proposal_status_before": "approvals_complete",
        "proposal_status_after": "withdrawn",
        "reason_code": "subject_withdrawn",
        "occurred_at": "2026-08-09T10:02:00Z",
    }


EXPECTED_V2_ENUMS: dict[str, tuple[str, ...]] = {
    "RelationshipType": (
        "self",
        "parent_of",
        "child_of",
        "guardian_of",
        "ward_of",
        "spouse_of",
        "sibling_of",
        "caregiver_of",
        "emergency_contact_for",
        "delegate_for",
        "beneficiary_of",
        "co_subject_of",
        "family_member_of",
    ),
    "Purpose": (
        "user_request",
        "runtime_profile_issue",
        "runtime_sensitive_action",
        "voice_profile",
        "voice_clone",
        "digital_self",
        "legacy",
        "payment",
        "raw_audio",
        "model_training",
        "device_transfer",
        "memory_capture",
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
        "memory_recall",
        "guardian_summary",
        "crisis_response",
    ),
    "DeviceTrust": ("trusted", "verified", "offline", "untrusted", "revoked"),
    "SafetyState": ("normal", "concern", "elevated_risk", "self_crisis"),
    "DataClassification": (
        "ephemeral",
        "public",
        "private",
        "biometric",
        "aggregate",
        "safety_minimum",
        "study_progress",
    ),
    "BindingStatus": ("active", "superseded", "revoked", "expired"),
    "BindingReason": ("create", "supersede", "transfer", "unbind", "expire"),
    "IntentKind": ("crisis_safety", "emergency", "care_alert"),
    "RecipientRole": ("guardian", "emergency_contact", "delegate"),
    "Channel": ("wechat_subscription", "sms", "phone_call"),
    "TemplateKey": ("crisis_safety_notice", "emergency_notice", "care_alert_notice"),
    "ReasonCode": ("safety_concern", "emergency_alert", "care_reminder"),
    "IntentStatus": ("pending", "in_progress", "delivered", "dead_lettered", "cancelled"),
    "RecipientStatus": (
        "pending",
        "in_progress",
        "delivered",
        "failed",
        "dead_lettered",
        "cancelled",
    ),
    "CancelReason": (
        "wrong_contact",
        "relationship_revoked",
        "relationship_expired",
        "relationship_disputed",
        "authorization_revoked",
        "authorization_expired",
        "operator_override",
        "user_request",
    ),
    "ResolutionKind": ("confirmed", "confirmation_required"),
    "ConfirmationMethod": ("voice_question", "app_confirm"),
    "MemoryEventType": (
        "memory_captured",
        "memory_proposed",
        "memory_promoted",
        "memory_confirmed",
        "memory_withdrawn",
        "memory_revoked",
        "scope_resolved",
    ),
    "SessionEventType": (
        "subject_resolved",
        "subject_switched",
        "profile_rotated",
        "epoch_bumped",
        "reconnected",
        "degraded_offline",
        "session_closed",
        "session_failed",
    ),
    "SharedMemoryProposalStatus": (
        "pending",
        "approvals_complete",
        "frozen",
        "withdrawn",
        "promoted",
    ),
    "SharedMemoryConfirmationDecision": ("confirm", "object"),
    "DeviceCertificateStatus": ("unknown", "pending", "active", "revoked", "expired"),
    "DeviceLifecycleStatus": (
        "unknown",
        "manufactured",
        "provisioned",
        "bound",
        "suspended",
        "revoked",
        "wipe_pending",
        "wiped",
        "retired",
    ),
    "DeviceCapability": (
        "device_certificate",
        "secure_element",
        "audio_capture",
        "audio_playback",
        "acoustic_echo_cancellation",
        "physical_microphone_cut",
        "hardware_privacy_light",
        "wifi",
        "lte",
        "ab_ota",
        "anti_rollback",
        "remote_attestation",
        "secure_wipe",
    ),
    "PhysicalMuteState": ("unknown", "unattested", "engaged", "disengaged", "mismatch"),
    "PrivacyLightState": ("unknown", "unattested", "on", "off", "mismatch"),
    "SimLifecycleStatus": (
        "unknown",
        "absent",
        "inactive",
        "active",
        "suspended",
        "revoked",
        "expired",
    ),
    "OtaAssignmentStatus": (
        "unknown",
        "pending",
        "downloading",
        "staged",
        "activated",
        "cancelled",
        "failed",
        "superseded",
    ),
    "OtaBootStatus": (
        "unknown",
        "empty",
        "staged",
        "booting",
        "confirmed",
        "failed",
        "rollback_pending",
        "rolled_back",
    ),
    "OtaSlotName": ("unknown", "a", "b"),
    "RemoteDeviceCommandType": (
        "force_mute",
        "revoke_device",
        "secure_wipe",
        "assign_ota",
        "collect_diagnostics",
    ),
}


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_v2_enum_vocabulary_is_single_canonical_source() -> None:
    schema = _schema()
    assert schema["properties"]["schema_version"]["const"] == 2  # type: ignore[index]
    assert schema["objects"]["properties"]["objects_version"]["const"] == 2  # type: ignore[index]
    defs = schema["$defs"]  # type: ignore[assignment]
    assert isinstance(defs, dict)

    for name, expected in EXPECTED_V2_ENUMS.items():
        assert tuple(defs[name]["enum"]) == expected
        assert schema["properties"][name]["$ref"] == f"#/$defs/{name}"  # type: ignore[index]

    capability_values = tuple(defs["Capability"]["enum"])
    assert "memory_capture" in capability_values
    assert "memory_promotion" in capability_values
    assert "family_shared_memory_proposal" in capability_values
    assert "family_shared_memory_approval" in capability_values
    assert "family_shared_memory_promotion" in capability_values

    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    assert isinstance(object_defs, dict)
    assert all("enum" not in node for node in _walk(object_defs)), (
        "object contracts must reference canonical $defs (or use const for a "
        "version literal), never embed a second enum"
    )
    assert tuple(defs) == gen.REQUIRED_ENUMS


def test_contract_lifecycle_machine_metadata_forbids_v1_new_producers() -> None:
    schema = _schema()
    entries = schema["objects"]["properties"]["contracts"]["const"]  # type: ignore[index]
    lifecycle = {entry["name"]: entry for entry in entries}
    assert tuple(lifecycle) == gen.REQUIRED_OBJECT_CONTRACTS

    replacements = {
        "RuntimeProfile": "RuntimeProfileV2",
        "RuntimeProfileSigned": "RuntimeProfileSignedV2",
        "PolicyReceipt": "PolicyReceiptV2",
    }
    for name, replacement in replacements.items():
        assert lifecycle[name] == {
            "name": name,
            "lifecycle": "deprecated",
            "consumer_mode": "migration_only",
            "new_producer": "forbidden",
            "replacement": replacement,
        }
    for name in ("RuntimeProfileV2", "RuntimeProfileSignedV2", "PolicyReceiptV2"):
        assert lifecycle[name] == {
            "name": name,
            "lifecycle": "current",
            "consumer_mode": "canonical",
            "new_producer": "allowed",
            "replacement": None,
        }
    assert {
        name
        for name, entry in lifecycle.items()
        if entry["new_producer"] == "forbidden"
    } == set(replacements)


def test_relationship_snapshot_is_directed_versioned_and_binding_fenced() -> None:
    schema = _schema()
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    relationship = object_defs["RelationshipSnapshot"]
    properties = relationship["properties"]

    assert set(relationship["required"]) == {
        "relationship_id",
        "snapshot_id",
        "revision",
        "relation_type",
        "status",
        "source_person_id",
        "target_person_id",
        "binding_id",
        "binding_version",
        "binding_canonical_hash",
        "canonical_hash",
        "valid_from",
        "valid_until",
    }
    assert properties["relation_type"]["$ref"] == "#/$defs/RelationshipType"
    assert properties["status"]["$ref"] == "#/$defs/RelationshipStatus"
    assert properties["binding_version"] == {"type": "integer", "minimum": 1}
    assert properties["binding_canonical_hash"]["pattern"] == "^[a-f0-9]{64}$"
    assert properties["canonical_hash"]["pattern"] == "^[a-f0-9]{64}$"
    assert {"subject_person_id", "person_id", "role"}.isdisjoint(properties)


def test_policy_receipt_v2_and_runtime_profile_v2_share_structured_obligations() -> None:
    schema = _schema()
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    receipt = object_defs["PolicyReceiptV2"]
    receipt_properties = receipt["properties"]

    assert set(receipt["required"]) == {
        "receipt_id",
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
        "action_resource_fence",
        "action_fence_hash",
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
    assert receipt_properties["receipt_id"]["maxLength"] == 192
    assert receipt_properties["purpose"]["$ref"] == "#/$defs/Purpose"
    assert receipt_properties["device_trust"]["$ref"] == "#/$defs/DeviceTrust"
    assert receipt_properties["safety_state"]["$ref"] == "#/$defs/SafetyState"
    assert receipt_properties["data_classification"]["$ref"] == "#/$defs/DataClassification"
    obligation_ref = "#/objects/properties/definitions/properties/PolicyObligationSpec"
    assert receipt_properties["obligations"]["items"]["$ref"] == obligation_ref

    obligation = object_defs["PolicyObligationSpec"]
    assert obligation["required"] == ["code", "params"]
    assert obligation["properties"]["code"]["$ref"] == "#/$defs/PolicyObligation"
    assert obligation["properties"]["params"]["$ref"].endswith("/ObligationParams")

    runtime_v1 = object_defs["RuntimeProfile"]
    runtime_v2 = object_defs["RuntimeProfileV2"]
    assert set(runtime_v2["required"]) == set(runtime_v1["required"])
    assert runtime_v2["properties"]["signature_schema"]["const"] == "runtime-profile-v2"
    assert runtime_v2["properties"]["obligations"]["items"]["$ref"] == obligation_ref
    assert runtime_v1["properties"]["obligations"]["items"]["$ref"] == "#/$defs/PolicyObligation"

    assert receipt["x-memoria-paired-arrays"] == [
        ["consent_snapshot_ids", "consent_snapshot_revisions"],
        ["relationship_snapshot_ids", "relationship_snapshot_revisions"],
    ]
    memory_pairs = receipt["x-memoria-value-pairs"][0]
    assert memory_pairs["pairs"] == {
        "memory_capture": "memory_capture",
        "memory_promotion": "memory_promotion",
        "family_shared_memory_proposal": "family_shared_memory_proposal",
        "family_shared_memory_approval": "family_shared_memory_approval",
        "family_shared_memory_promotion": "family_shared_memory_promotion",
    }
    assert memory_pairs["bidirectional"] is True


def test_shared_memory_proposal_confirmation_and_promotion_have_distinct_authority() -> None:
    schema = _schema()
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    proposal = object_defs["SharedMemoryProposal"]
    confirmation = object_defs["SharedMemoryConfirmationEvidence"]
    promotion = object_defs["SharedMemoryPromotion"]
    withdrawal = object_defs["SharedMemoryWithdrawalEvidence"]

    assert {
        "proposal_policy_receipt_id",
        "proposal_fence",
        "required_approval_subject_ids",
        "consent_snapshot_id",
        "consent_snapshot_revision",
    } <= set(proposal["required"])
    assert "policy_receipt_id" not in proposal["properties"]
    assert proposal["properties"]["proposal_fence"]["$ref"].endswith("/MemoryWriteFence")

    assert {
        "evidence_id",
        "proposal_id",
        "proposal_revision",
        "subject_id",
        "decision",
        "authentication_evidence_id",
        "authentication_evidence_hash",
        "action_evidence_hash",
        "action_fence",
        "approval_snapshot_id",
        "approval_snapshot_revision",
        "approval_snapshot_hash",
        "approval_policy_receipt_id",
        "approval_policy_receipt",
        "approval_fence",
        "proposal_status_before",
        "proposal_status_after",
    } <= set(confirmation["required"])
    for field in (
        "approval_snapshot_id",
        "approval_snapshot_revision",
        "approval_snapshot_hash",
        "approval_policy_receipt_id",
        "approval_policy_receipt",
        "approval_fence",
    ):
        assert "null" in json.dumps(confirmation["properties"][field])
    decision_rules = confirmation["x-memoria-conditional-fields"]
    confirm_rule = next(rule for rule in decision_rules if rule["when"] == {"path": "decision", "equals": "confirm"})
    object_rule = next(rule for rule in decision_rules if rule["when"] == {"path": "decision", "equals": "object"})
    assert "approval_policy_receipt_id" in confirm_rule["require_non_null"]
    assert "approval_fence" in confirm_rule["require_non_null"]
    assert "approval_policy_receipt_id" in object_rule["require_null"]
    assert "approval_fence" in object_rule["require_null"]

    assert {
        "withdrawal_evidence_id",
        "proposal_id",
        "proposal_revision",
        "subject_id",
        "authentication_evidence_id",
        "authentication_evidence_hash",
        "action_evidence_hash",
        "action_fence",
        "proposal_status_before",
        "proposal_status_after",
        "occurred_at",
    } <= set(withdrawal["required"])
    assert not any("policy_receipt" in name for name in withdrawal["properties"])
    assert withdrawal["properties"]["proposal_status_after"]["const"] == "withdrawn"

    assert {
        "proposal_policy_receipt_id",
        "promotion_policy_receipt_id",
        "promotion_policy_receipt",
        "promotion_fence",
        "proposal_revision",
        "proposal_status_before",
        "proposal_status_after",
        "required_approval_subject_ids",
        "confirmation_evidence",
    } <= set(promotion["required"])
    assert "policy_receipt_id" not in promotion["properties"]
    assert promotion["properties"]["proposal_status_before"]["const"] == "approvals_complete"
    assert promotion["properties"]["proposal_status_after"]["const"] == "promoted"
    assert promotion["x-memoria-distinct-fields"] == [
        ["proposal_policy_receipt_id", "promotion_policy_receipt_id"]
    ]
    assert promotion["x-memoria-confirmation-coverage"] == {
        "required_subject_ids_field": "required_approval_subject_ids",
        "evidence_field": "confirmation_evidence",
        "evidence_subject_field": "subject_id",
        "evidence_decision_field": "decision",
        "accepted_decision": "confirm",
        "evidence_receipt_field": "approval_policy_receipt_id",
        "evidence_receipts_distinct_from_fields": [
            "proposal_policy_receipt_id",
            "promotion_policy_receipt_id",
        ],
        "require_unique_evidence_receipts": True,
    }


def test_policy_v2_binds_family_authority_to_exact_action_resource_fence() -> None:
    schema = _schema()
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    fence = object_defs["PolicyActionResourceFence"]
    approval = object_defs["PolicyApprovalSnapshotFence"]

    assert set(approval["required"]) == {
        "subject_id",
        "snapshot_id",
        "revision",
        "canonical_hash",
    }
    assert {
        "action_fence_schema",
        "capability",
        "purpose",
        "action_resource_id",
        "action_revision",
        "action_evidence_hash",
        "canonical_hash",
        "family_space_id",
        "family_owner_subject_id",
        "proposal_id",
        "proposal_revision",
        "voter_subject_id",
        "approval_decision",
        "required_approval_subject_ids",
        "approval_snapshots",
        "capture_evidence_ids",
        "capture_evidence_hash",
        "consent_snapshot_id",
        "consent_snapshot_revision",
        "consent_snapshot_hash",
        "membership_snapshot_id",
        "membership_snapshot_revision",
        "membership_snapshot_hash",
        "generation_id",
        "turn_id",
        "tool_epoch",
        "issued_at",
        "valid_until",
    } == set(fence["required"])
    assert fence["properties"]["action_fence_schema"]["const"] == "policy-action-resource-fence-v1"
    assert fence["properties"]["valid_until"] == {"type": "string", "format": "date-time"}

    for name in ("PolicyDecision", "PolicyReceiptV2"):
        contract = object_defs[name]
        assert {"capability", "purpose", "action_resource_fence", "action_fence_hash"} <= set(
            contract["required"]
        )
        assert contract["properties"]["action_resource_fence"]["$ref"].endswith(
            "/PolicyActionResourceFence"
        )
        assert contract["properties"]["action_fence_hash"]["pattern"] == "^[a-f0-9]{64}$"
        assert ["action_fence_hash", "action_resource_fence.canonical_hash"] in contract[
            "x-memoria-equal-paths"
        ]
        assert "action_resource_fence" in contract["x-memoria-context-hash-inputs"]

    family_rules = fence["x-memoria-conditional-fields"]
    by_capability = {
        rule["when"]["equals"]: rule
        for rule in family_rules
        if rule["when"].get("path") == "capability" and "equals" in rule["when"]
    }
    proposal_rule = by_capability["family_shared_memory_proposal"]
    approval_rule = by_capability["family_shared_memory_approval"]
    promotion_rule = by_capability["family_shared_memory_promotion"]
    assert {"proposal_id", "family_owner_subject_id", "capture_evidence_hash"} <= set(
        proposal_rule["require_non_null"]
    )
    assert {"voter_subject_id", "approval_decision", "proposal_revision"} <= set(
        approval_rule["require_non_null"]
    )
    assert approval_rule["require_equals"]["approval_decision"] == "confirm"
    assert "approval_snapshots" in promotion_rule["require_non_empty"]
    assert "required_approval_subject_ids" in promotion_rule["require_non_empty"]


def test_device_fleet_v2_contract_is_attested_fail_closed_and_has_no_remote_unmute() -> None:
    schema = _schema()
    defs = schema["$defs"]  # type: ignore[assignment]
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]

    for name in (
        "DeviceCertificateStatus",
        "DeviceLifecycleStatus",
        "PhysicalMuteState",
        "PrivacyLightState",
        "SimLifecycleStatus",
        "OtaAssignmentStatus",
        "OtaBootStatus",
        "OtaSlotName",
    ):
        assert defs[name]["x-memoria"]["default"] == "unknown"
        assert defs[name]["x-memoria"]["missing_value_policy"] == "fail_closed"

    commands = set(defs["RemoteDeviceCommandType"]["enum"])
    assert commands == {
        "force_mute",
        "revoke_device",
        "secure_wipe",
        "assign_ota",
        "collect_diagnostics",
    }
    assert not any("unmute" in value for value in commands)

    attestation = object_defs["DeviceAttestation"]
    assert {
        "device_id",
        "certificate_id",
        "certificate_status",
        "binding_id",
        "binding_version",
        "firmware_version",
        "firmware_security_version",
        "bootloader_version",
        "capability_manifest_hash",
        "capabilities",
        "attested_capabilities",
        "physical_mute_state",
        "privacy_light_state",
        "sim_status",
        "anti_rollback_floor_version",
        "anti_rollback_floor_security_version",
        "monotonic_counter",
        "last_command_sequence",
        "occurred_at",
        "expires_at",
        "signer_key_id",
        "signature",
    } <= set(attestation["required"])
    assert attestation["properties"]["physical_mute_state"]["$ref"] == "#/$defs/PhysicalMuteState"
    assert attestation["properties"]["privacy_light_state"]["$ref"] == "#/$defs/PrivacyLightState"
    assert attestation["properties"]["capabilities"]["items"]["$ref"] == "#/$defs/DeviceCapability"
    assert attestation["properties"]["attested_capabilities"]["items"]["$ref"] == "#/$defs/DeviceCapability"
    assert attestation["properties"]["monotonic_counter"]["minimum"] == 1
    assert "null" in attestation["properties"]["binding_id"]["type"]
    assert "null" in attestation["properties"]["binding_version"]["type"]
    lifecycle_rules = attestation["x-memoria-conditional-fields"]
    assert any(
        rule["when"] == {
            "path": "device_lifecycle_status",
            "in": ["manufactured", "provisioned", "unknown", "wiped", "retired"],
        }
        and set(rule["require_null"]) >= {"binding_id", "binding_version"}
        for rule in lifecycle_rules
    )
    assert any(
        rule["when"] == {
            "path": "device_lifecycle_status",
            "in": ["bound", "suspended", "revoked", "wipe_pending"],
        }
        and set(rule["require_non_null"]) >= {"binding_id", "binding_version"}
        for rule in lifecycle_rules
    )

    assignment = object_defs["OtaAssignment"]
    assert {"target_slot", "target_version", "target_firmware_security_version", "anti_rollback_floor_version", "anti_rollback_floor_security_version", "signer_key_id", "signature_algorithm", "status"} <= set(
        assignment["required"]
    )
    receipt = object_defs["OtaStateReceipt"]
    assert {
        "highest_accepted_version",
        "highest_accepted_firmware_security_version",
        "anti_rollback_floor_version",
        "anti_rollback_floor_security_version",
        "active_slot",
        "pending_slot",
        "slots",
        "max_boot_attempts",
        "monotonic_counter",
        "certificate_id",
        "signer_key_id",
        "signature_algorithm",
    } <= set(receipt["required"])
    assert receipt["properties"]["slots"]["minItems"] == 2
    assert receipt["properties"]["slots"]["maxItems"] == 2

    remote = object_defs["RemoteDeviceCommand"]
    assert remote["properties"]["command_type"]["$ref"] == "#/$defs/RemoteDeviceCommandType"
    assert {
        "signer_key_id",
        "signature_algorithm",
        "target_certificate_id",
        "expected_binding_id",
        "expected_binding_version",
        "expected_monotonic_counter",
        "expected_firmware_security_version",
        "command_sequence",
    } <= set(remote["required"])
    execution = object_defs["RemoteDeviceCommandExecution"]
    assert {"execution_id", "command", "current_attestation", "accepted_at"} == set(
        execution["required"]
    )
    equal_paths = execution["x-memoria-equal-paths"]
    assert ["command.target_certificate_id", "current_attestation.certificate_id"] in equal_paths
    assert ["command.expected_binding_version", "current_attestation.binding_version"] in equal_paths
    assert ["command.expected_monotonic_counter", "current_attestation.monotonic_counter"] in equal_paths


def test_sensitive_fences_are_bounded_and_expire_after_issue() -> None:
    schema = _schema()
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]

    for name in ("MemoryWriteFence", "NotificationFence", "SessionEpochFence"):
        contract = object_defs[name]
        assert {"issued_at", "valid_until"} <= set(contract["required"])
        assert contract["properties"]["issued_at"]["type"] == "string"
        assert contract["properties"]["issued_at"]["format"] == "date-time"
        assert contract["properties"]["valid_until"]["type"] == "string"
        assert contract["properties"]["valid_until"]["format"] == "date-time"
        assert {
            "earlier_path": "issued_at",
            "later_path": "valid_until",
            "strict": True,
        } in contract["x-memoria-time-order"]

    action_fence = object_defs["PolicyActionResourceFence"]
    assert {
        "earlier_path": "issued_at",
        "later_path": "valid_until",
        "strict": True,
    } in action_fence["x-memoria-time-order"]
    receipt = object_defs["PolicyReceiptV2"]
    assert {
        "earlier_path": "created_at",
        "later_path": "expires_at",
        "strict": True,
    } in receipt["x-memoria-time-order"]


def test_fences_and_session_events_carry_complete_identity_with_epoch_zero_isolation() -> None:
    schema = _schema()
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]

    for name in ("MemoryWriteFence", "NotificationFence"):
        contract = object_defs[name]
        assert {
            "session_id",
            "device_id",
            "binding_id",
            "binding_version",
            "runtime_profile_id",
            "subject_revision",
            "generation_id",
            "turn_id",
            "tool_epoch",
            "fingerprint",
        } <= set(contract["required"])
        assert contract["properties"]["binding_version"] == {
            "type": "integer",
            "minimum": 1,
        } or contract["properties"]["binding_version"]["minimum"] == 1

    session_fence = object_defs["SessionEpochFence"]
    assert {
        "device_id",
        "binding_id",
        "binding_version",
        "actor_id",
        "subject_revision",
    } <= set(session_fence["required"])

    session_event = object_defs["SessionEvent"]
    assert {
        "device_id",
        "binding_id",
        "binding_version",
        "subject_revision",
        "tool_epoch",
    } <= set(session_event["required"])
    semantics = session_event["x-memoria-epoch-semantics"]
    assert semantics["epoch_field"] == "session_epoch"
    assert set(semantics["zero_event_values"]) == {
        "reconnected",
        "degraded_offline",
        "session_closed",
        "session_failed",
    }
    assert set(semantics["zero_null_fields"]) >= {
        "generation_id",
        "turn_id",
        "tool_epoch",
        "active_subject_id",
        "runtime_profile_id",
        "actor_id",
        "device_id",
        "binding_id",
        "binding_version",
        "subject_revision",
    }
    assert set(semantics["positive_non_null_fields"]) >= {
        "generation_id",
        "turn_id",
        "tool_epoch",
        "runtime_profile_id",
        "actor_id",
        "device_id",
        "binding_id",
        "binding_version",
        "subject_revision",
    }


def test_notification_recipient_is_pinned_to_relationship_snapshot_revision() -> None:
    schema = _schema()
    object_defs = schema["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    recipient = object_defs["RecipientSnapshot"]
    assert {"relationship_snapshot_id", "relationship_revision"} <= set(
        recipient["required"]
    )
    assert recipient["properties"]["relationship_snapshot_id"]["maxLength"] == 128
    assert recipient["properties"]["relationship_revision"] == {
        "type": "integer",
        "minimum": 1,
    }
    intent = object_defs["NotificationIntent"]
    assert intent["properties"]["policy_receipt_id"]["maxLength"] == 192


def test_generated_python_v2_objects_validate_and_roundtrip_fail_closed() -> None:
    contracts = _load_generated_python()
    fixtures: dict[str, dict[str, object]] = {
        "RuntimeProfileV2": _runtime_profile_v2(),
        "RuntimeProfileSignedV2": {**_runtime_profile_v2(), "signature": "a" * 64},
        "PolicyReceiptV2": _policy_receipt_v2(),
        "SharedMemoryConfirmationEvidence": _confirmation("person-a"),
        "SharedMemoryWithdrawalEvidence": _withdrawal(),
        "SharedMemoryProposal": _shared_proposal(),
        "SharedMemoryPromotion": _shared_promotion(),
        "DeviceAttestation": _device_attestation(),
        "OtaAssignment": _ota_assignment(),
        "OtaStateReceipt": _ota_state_receipt(),
        "RemoteDeviceCommand": _remote_command(),
        "RemoteDeviceCommandExecution": _remote_execution(),
    }

    for name, payload in fixtures.items():
        model = getattr(contracts, name)
        parsed = model.model_validate(payload)
        assert model.model_validate_json(json.dumps(payload)).model_dump(mode="json") == parsed.model_dump(
            mode="json"
        )
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "unknown_contract_field": True})
        missing = deepcopy(payload)
        del missing[next(iter(_schema()["objects"]["properties"]["definitions"]["properties"][name]["required"]))]  # type: ignore[index]
        with pytest.raises(ValidationError):
            model.model_validate(missing)


def test_generated_contract_lifecycle_guards_parse_v1_but_forbid_v1_producers() -> None:
    contracts = _load_generated_python()
    contracts.RuntimeProfile.model_validate(_legacy_runtime_profile())
    contracts.RuntimeProfileSigned.model_validate(
        {**_legacy_runtime_profile(), "signature": "a" * 64}
    )
    contracts.PolicyReceipt.model_validate(_legacy_policy_receipt())

    replacements = {
        "RuntimeProfile": "RuntimeProfileV2",
        "RuntimeProfileSigned": "RuntimeProfileSignedV2",
        "PolicyReceipt": "PolicyReceiptV2",
    }
    assert set(contracts.MIGRATION_ONLY_CONTRACTS) == set(replacements)
    for name, replacement in replacements.items():
        assert not contracts.is_new_producer_contract(name)
        assert contracts.CONTRACT_LIFECYCLE[name]["replacement"] == replacement
        with pytest.raises(ValueError, match="forbidden for new producers"):
            contracts.require_new_producer_contract(name)
    for name in ("RuntimeProfileV2", "RuntimeProfileSignedV2", "PolicyReceiptV2"):
        assert contracts.is_new_producer_contract(name)
        contracts.require_new_producer_contract(name)
    assert not contracts.is_new_producer_contract("UnknownContract")
    with pytest.raises(ValueError):
        contracts.require_new_producer_contract("UnknownContract")
    assert "action_resource_fence" in contracts.CONTEXT_HASH_INPUTS["PolicyReceiptV2"]


def test_firmware_compact_carries_lifecycle_and_cross_field_rule_parity() -> None:
    compact_path = REPO_ROOT / "packages/contracts/generated/firmware/multi_subject_contracts.compact.json"
    compact = json.loads(compact_path.read_text(encoding="utf-8"))
    schema = _schema()
    assert compact["schema_version"] == 2
    assert compact["objects_version"] == 2
    assert compact["source_hash"] == hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest()
    assert compact["contract_lifecycle"] == schema["objects"]["properties"]["contracts"]["const"]  # type: ignore[index]
    assert set(compact["migration_only_contracts"]) == {
        "RuntimeProfile",
        "RuntimeProfileSigned",
        "PolicyReceipt",
    }
    assert {"RuntimeProfileV2", "RuntimeProfileSignedV2", "PolicyReceiptV2"} <= set(
        compact["canonical_producer_contracts"]
    )

    objects = {entry["name"]: entry for entry in compact["objects"]}
    confirmation_rules = objects["SharedMemoryConfirmationEvidence"]["invariants"]
    assert any(
        rule["kind"] == "conditional"
        and rule["when"] == {"path": "decision", "equals": "object"}
        and "approval_policy_receipt_id" in rule["require_null"]
        for rule in confirmation_rules
    )
    assert any(
        rule["kind"] == "record_set_equal"
        for rule in objects["SharedMemoryPromotion"]["invariants"]
    )
    assert any(
        rule["kind"] == "integer_order"
        and rule["left_path"] == "firmware_security_version"
        for rule in objects["DeviceAttestation"]["invariants"]
    )
    assert any(
        rule["kind"] == "required_values"
        for rule in objects["RemoteDeviceCommandExecution"]["invariants"]
    )
    memory_fields = {
        field["name"]: field for field in objects["MemoryWriteFence"]["properties"]
    }
    assert memory_fields["valid_until"]["nullable"] is False
    assert memory_fields["valid_until"]["format"] == "date-time"
    assert "action_resource_fence" in objects["PolicyReceiptV2"]["context_hash_inputs"]


def test_generated_python_v2_cross_field_and_hardware_boundaries_fail_closed() -> None:
    contracts = _load_generated_python()

    profile = _runtime_profile_v2()
    with pytest.raises(ValidationError):
        contracts.RuntimeProfileV2.model_validate({**profile, "signature_schema": "runtime-profile-v1"})
    bad_params = deepcopy(profile)
    bad_params["obligations"][0]["params"]["quiet_hours"] = ["9:00", "06:30"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.RuntimeProfileV2.model_validate(bad_params)
    bad_extra_pair = deepcopy(profile)
    bad_extra_pair["obligations"][0]["params"]["extras"] = [["scope"]]  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.RuntimeProfileV2.model_validate(bad_extra_pair)
    duplicate_obligations = deepcopy(profile)
    duplicate_obligations["obligations"] = [
        deepcopy(duplicate_obligations["obligations"][0]),
        deepcopy(duplicate_obligations["obligations"][0]),
    ]
    with pytest.raises(ValidationError):
        contracts.RuntimeProfileV2.model_validate(duplicate_obligations)
    same_code_distinct_params = deepcopy(profile)
    same_code_distinct_params["obligations"].append(  # type: ignore[union-attr]
        deepcopy(same_code_distinct_params["obligations"][0])
    )
    same_code_distinct_params["obligations"][1]["params"]["quiet_hours"] = ["08:00", "09:00"]  # type: ignore[index]
    contracts.RuntimeProfileV2.model_validate(same_code_distinct_params)

    receipt = _policy_receipt_v2()
    with pytest.raises(ValidationError):
        contracts.PolicyReceiptV2.model_validate(
            {**receipt, "purpose": "family_shared_memory_promotion"}
        )
    with pytest.raises(ValidationError):
        contracts.PolicyReceiptV2.model_validate(
            {**receipt, "consent_snapshot_revisions": []}
        )
    with pytest.raises(ValidationError):
        contracts.PolicyReceiptV2.model_validate(
            {**receipt, "binding_canonical_hash": None, "exact_fence": True}
        )
    family_receipt = _policy_receipt_v2("family_shared_memory_promotion")
    with pytest.raises(ValidationError):
        contracts.PolicyReceiptV2.model_validate(
            {**family_receipt, "exact_fence": False}
        )

    proposal = _shared_proposal()
    with pytest.raises(ValidationError):
        contracts.SharedMemoryProposal.model_validate(
            {**proposal, "required_approval_subject_ids": ["person-a", "person-c"]}
        )
    promotion = _shared_promotion()
    with pytest.raises(ValidationError):
        contracts.SharedMemoryPromotion.model_validate(
            {**promotion, "promotion_policy_receipt_id": "receipt-proposal-1"}
        )
    missing_vote = deepcopy(promotion)
    missing_vote["confirmation_evidence"] = [missing_vote["confirmation_evidence"][0]]  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.SharedMemoryPromotion.model_validate(missing_vote)
    objected = deepcopy(promotion)
    objected["confirmation_evidence"][1]["decision"] = "object"  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.SharedMemoryPromotion.model_validate(objected)
    reused_proposal_receipt = deepcopy(promotion)
    reused_proposal_receipt["confirmation_evidence"][0][
        "approval_policy_receipt_id"
    ] = promotion["proposal_policy_receipt_id"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.SharedMemoryPromotion.model_validate(reused_proposal_receipt)

    contracts.SharedMemoryConfirmationEvidence.model_validate(_objection())
    objection_with_allow = _objection()
    objection_with_allow["approval_policy_receipt_id"] = "receipt-illegal-allow"
    with pytest.raises(ValidationError):
        contracts.SharedMemoryConfirmationEvidence.model_validate(objection_with_allow)
    confirm_without_allow = _confirmation("person-a")
    confirm_without_allow["approval_policy_receipt_id"] = None
    confirm_without_allow["approval_policy_receipt"] = None
    confirm_without_allow["approval_fence"] = None
    with pytest.raises(ValidationError):
        contracts.SharedMemoryConfirmationEvidence.model_validate(confirm_without_allow)
    contracts.SharedMemoryWithdrawalEvidence.model_validate(_withdrawal())

    wrong_proposal = deepcopy(promotion)
    wrong_proposal["proposal_id"] = "proposal-2"
    with pytest.raises(ValidationError):
        contracts.SharedMemoryPromotion.model_validate(wrong_proposal)
    replaced_revision = deepcopy(promotion)
    replaced_revision["confirmation_evidence"][0]["approval_snapshot_revision"] = 3  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.SharedMemoryPromotion.model_validate(replaced_revision)
    replayed = deepcopy(promotion)
    replayed["promoted_at"] = "2026-08-09T10:10:00Z"
    with pytest.raises(ValidationError):
        contracts.SharedMemoryPromotion.model_validate(replayed)
    direct_promote_vote = _confirmation("person-b")
    direct_promote_vote["proposal_status_after"] = "promoted"
    with pytest.raises(ValidationError):
        contracts.SharedMemoryConfirmationEvidence.model_validate(direct_promote_vote)

    attestation = _device_attestation()
    with pytest.raises(ValidationError):
        contracts.DeviceAttestation.model_validate(
            {**attestation, "physical_mute_state": True}
        )
    with pytest.raises(ValidationError):
        contracts.DeviceAttestation.model_validate(
            {**attestation, "attestation_schema": "device-attestation-v0"}
        )
    manufactured = deepcopy(attestation)
    manufactured.update(
        {
            "device_lifecycle_status": "manufactured",
            "binding_id": None,
            "binding_version": None,
            "attested_capabilities": [],
        }
    )
    contracts.DeviceAttestation.model_validate(manufactured)
    with pytest.raises(ValidationError):
        contracts.DeviceAttestation.model_validate(
            {**manufactured, "binding_id": "stale-binding", "binding_version": 1}
        )
    no_physical_capability = deepcopy(attestation)
    no_physical_capability["capabilities"].remove("physical_microphone_cut")  # type: ignore[union-attr]
    no_physical_capability["attested_capabilities"].remove("physical_microphone_cut")  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        contracts.DeviceAttestation.model_validate(no_physical_capability)
    unattested_hardware = deepcopy(attestation)
    unattested_hardware["physical_mute_state"] = "unattested"
    with pytest.raises(ValidationError):
        contracts.DeviceAttestation.model_validate(unattested_hardware)
    revoked = deepcopy(attestation)
    revoked["certificate_status"] = "revoked"
    with pytest.raises(ValidationError):
        contracts.DeviceAttestation.model_validate(revoked)
    with pytest.raises(ValidationError):
        contracts.OtaAssignment.model_validate({**_ota_assignment(), "target_slot": "unknown"})
    lexical_trap = {
        **_ota_assignment(),
        "target_version": "1.10.0",
        "anti_rollback_floor_version": "1.2.0",
    }
    contracts.OtaAssignment.model_validate(lexical_trap)
    with pytest.raises(ValidationError):
        contracts.OtaAssignment.model_validate(
            {**lexical_trap, "target_firmware_security_version": 9}
        )
    duplicate_slots = deepcopy(_ota_state_receipt())
    duplicate_slots["slots"][1]["slot"] = "a"  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.OtaStateReceipt.model_validate(duplicate_slots)
    old_active_slot = deepcopy(_ota_state_receipt())
    old_active_slot["slots"][0]["firmware_security_version"] = 9  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.OtaStateReceipt.model_validate(old_active_slot)
    remote = {**_remote_command(), "command_type": "remote_unmute"}
    with pytest.raises(ValidationError):
        contracts.RemoteDeviceCommand.model_validate(remote)

    execution = _remote_execution()
    contracts.RemoteDeviceCommandExecution.model_validate(execution)
    transferred = deepcopy(execution)
    transferred["current_attestation"]["binding_id"] = "binding-2"  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.RemoteDeviceCommandExecution.model_validate(transferred)
    stale_counter = deepcopy(execution)
    stale_counter["command"]["expected_monotonic_counter"] = 7  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.RemoteDeviceCommandExecution.model_validate(stale_counter)
    stale_sequence = deepcopy(execution)
    stale_sequence["command"]["command_sequence"] = 3  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.RemoteDeviceCommandExecution.model_validate(stale_sequence)
    revoked_execution = deepcopy(execution)
    revoked_execution["current_attestation"]["certificate_status"] = "revoked"  # type: ignore[index]
    revoked_execution["current_attestation"]["attested_capabilities"] = []  # type: ignore[index]
    with pytest.raises(ValidationError):
        contracts.RemoteDeviceCommandExecution.model_validate(revoked_execution)

    epoch_zero = {
        "event_id": "event-0",
        "event_type": "degraded_offline",
        "session_id": "session-1",
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
        "occurred_at": "2026-08-09T10:00:00Z",
        "payload": {},
    }
    contracts.SessionEvent.model_validate(epoch_zero)
    with pytest.raises(ValidationError):
        contracts.SessionEvent.model_validate({**epoch_zero, "event_type": "subject_switched"})
    with pytest.raises(ValidationError):
        contracts.SessionEvent.model_validate({**epoch_zero, "device_id": "device-1"})
    business = {
        **epoch_zero,
        "event_type": "subject_resolved",
        "session_epoch": 1,
        "device_id": "device-1",
        "binding_id": "binding-1",
        "binding_version": 1,
        "generation_id": 0,
        "turn_id": 0,
        "tool_epoch": 0,
        "runtime_profile_id": "profile-1",
        "actor_id": "person-a",
        "subject_revision": 1,
    }
    contracts.SessionEvent.model_validate(business)
    with pytest.raises(ValidationError):
        contracts.SessionEvent.model_validate({**business, "binding_version": None})


def test_generated_typescript_v2_validators_enforce_cross_domain_fences(
    tmp_path: Path,
) -> None:
    if shutil.which("node") is None:
        pytest.skip("node unavailable")
    fixtures = {
        "RuntimeProfileV2": _runtime_profile_v2(),
        "PolicyReceiptV2": _policy_receipt_v2(),
        "SharedMemoryConfirmationEvidence": _confirmation("person-a"),
        "SharedMemoryWithdrawalEvidence": _withdrawal(),
        "SharedMemoryProposal": _shared_proposal(),
        "SharedMemoryPromotion": _shared_promotion(),
        "DeviceAttestation": _device_attestation(),
        "OtaAssignment": _ota_assignment(),
        "OtaStateReceipt": _ota_state_receipt(),
        "RemoteDeviceCommand": _remote_command(),
        "RemoteDeviceCommandExecution": _remote_execution(),
    }
    object_defs = _schema()["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    required = {name: object_defs[name]["required"] for name in fixtures}
    harness = tmp_path / "v2-contracts.mts"
    harness.write_text(
        f"""
import * as contracts from {json.dumps(str(TYPESCRIPT_ARTIFACT))};

const fixtures: Record<string, Record<string, unknown>> = {json.dumps(fixtures)};
const required: Record<string, string[]> = {json.dumps(required)};
const fail = (message: string): never => {{ throw new Error(message); }};
const expectValid = (name: string, value: unknown): void => {{
  const validate = contracts[`validate${{name}}`] as (payload: unknown) => string[];
  const errors = validate(value);
  if (errors.length) fail(`${{name}} unexpectedly rejected: ${{errors.join(" | ")}}`);
}};
const expectInvalid = (name: string, value: unknown, label: string): void => {{
  const validate = contracts[`validate${{name}}`] as (payload: unknown) => string[];
  if (validate(value).length === 0) fail(`${{name}} accepted ${{label}}`);
  const guard = contracts[`is${{name}}`] as (payload: unknown) => boolean;
  if (guard(value)) fail(`${{name}} guard accepted ${{label}}`);
}};

for (const [name, fixture] of Object.entries(fixtures)) {{
  expectValid(name, fixture);
  expectInvalid(name, {{ ...fixture, unknown_contract_field: true }}, "unknown field");
  const missing = {{ ...fixture }};
  delete missing[required[name][0]];
  expectInvalid(name, missing, `missing ${{required[name][0]}}`);
}}

const legacyRuntime = {json.dumps(_legacy_runtime_profile())};
expectValid("RuntimeProfile", legacyRuntime);
if (contracts.isNewProducerContract("RuntimeProfile")) fail("v1 runtime profile producer must be forbidden");
if (!contracts.isNewProducerContract("RuntimeProfileV2")) fail("v2 runtime profile must be a current producer");
let legacyGuardRejected = false;
try {{ contracts.requireNewProducerContract("RuntimeProfile"); }} catch {{ legacyGuardRejected = true; }}
if (!legacyGuardRejected) fail("v1 producer guard did not reject");
contracts.requireNewProducerContract("RuntimeProfileV2");
if (!contracts.CONTEXT_HASH_INPUTS.PolicyReceiptV2.includes("action_resource_fence")) fail("TS context hash inputs lost action fence");
const duplicateObligations = structuredClone(fixtures.RuntimeProfileV2);
duplicateObligations.obligations = [
  structuredClone(duplicateObligations.obligations[0]),
  structuredClone(duplicateObligations.obligations[0]),
];
expectInvalid("RuntimeProfileV2", duplicateObligations, "duplicate structured obligations");
const sameCodeDistinctParams = structuredClone(fixtures.RuntimeProfileV2);
sameCodeDistinctParams.obligations = [
  structuredClone(sameCodeDistinctParams.obligations[0]),
  structuredClone(sameCodeDistinctParams.obligations[0]),
];
(sameCodeDistinctParams.obligations as Array<Record<string, unknown>>)[1].params = {{
  ...((sameCodeDistinctParams.obligations as Array<Record<string, unknown>>)[1].params as Record<string, unknown>),
  quiet_hours: ["08:00", "09:00"],
}};
expectValid("RuntimeProfileV2", sameCodeDistinctParams);

const objection = {json.dumps(_objection())};
expectValid("SharedMemoryConfirmationEvidence", objection);
expectInvalid("SharedMemoryConfirmationEvidence", {{ ...objection, approval_policy_receipt_id: "illegal" }}, "object with allow receipt");
const confirmation = structuredClone(fixtures.SharedMemoryConfirmationEvidence);
confirmation.approval_policy_receipt_id = null;
confirmation.approval_policy_receipt = null;
confirmation.approval_fence = null;
expectInvalid("SharedMemoryConfirmationEvidence", confirmation, "confirm without approval receipt");

const promotion = structuredClone(fixtures.SharedMemoryPromotion);
promotion.proposal_id = "proposal-2";
expectInvalid("SharedMemoryPromotion", promotion, "wrong proposal receipt");
const missingVote = structuredClone(fixtures.SharedMemoryPromotion);
missingVote.confirmation_evidence = (missingVote.confirmation_evidence as unknown[]).slice(0, 1);
expectInvalid("SharedMemoryPromotion", missingVote, "missing required vote");
const replacedRevision = structuredClone(fixtures.SharedMemoryPromotion);
(replacedRevision.confirmation_evidence as Array<Record<string, unknown>>)[0].approval_snapshot_revision = 3;
expectInvalid("SharedMemoryPromotion", replacedRevision, "replaced approval revision");
const replay = structuredClone(fixtures.SharedMemoryPromotion);
replay.promoted_at = "2026-08-09T10:10:00Z";
expectInvalid("SharedMemoryPromotion", replay, "expired promotion receipt replay");
const directPromoteVote = structuredClone(fixtures.SharedMemoryConfirmationEvidence);
directPromoteVote.proposal_status_after = "promoted";
expectInvalid("SharedMemoryConfirmationEvidence", directPromoteVote, "vote directly promoted proposal");
const inexactFamilyReceipt = structuredClone((fixtures.SharedMemoryPromotion as Record<string, unknown>).promotion_policy_receipt);
inexactFamilyReceipt.exact_fence = false;
expectInvalid("PolicyReceiptV2", inexactFamilyReceipt, "inexact family receipt");

const expiredFence = structuredClone((fixtures.SharedMemoryProposal as Record<string, unknown>).proposal_fence);
expiredFence.valid_until = expiredFence.issued_at;
expectInvalid("MemoryWriteFence", expiredFence, "zero-length sensitive fence");

const attestation = structuredClone(fixtures.DeviceAttestation);
attestation.certificate_status = "revoked";
expectInvalid("DeviceAttestation", attestation, "revoked certificate with attested capabilities");
const noCapability = structuredClone(fixtures.DeviceAttestation);
noCapability.capabilities = (noCapability.capabilities as unknown[]).filter((value) => value !== "physical_microphone_cut");
noCapability.attested_capabilities = (noCapability.attested_capabilities as unknown[]).filter((value) => value !== "physical_microphone_cut");
expectInvalid("DeviceAttestation", noCapability, "engaged mute without hardware capability");
const unbound = structuredClone(fixtures.DeviceAttestation);
unbound.device_lifecycle_status = "manufactured";
unbound.binding_id = null;
unbound.binding_version = null;
unbound.attested_capabilities = [];
expectValid("DeviceAttestation", unbound);
expectInvalid("DeviceAttestation", {{ ...unbound, binding_id: "stale", binding_version: 1 }}, "manufactured device with stale binding");

const oldBinding = structuredClone(fixtures.RemoteDeviceCommandExecution);
(oldBinding.current_attestation as Record<string, unknown>).binding_id = "binding-2";
expectInvalid("RemoteDeviceCommandExecution", oldBinding, "old transferred binding");
const staleCounter = structuredClone(fixtures.RemoteDeviceCommandExecution);
(staleCounter.command as Record<string, unknown>).expected_monotonic_counter = 7;
expectInvalid("RemoteDeviceCommandExecution", staleCounter, "stale attestation counter");
const staleSequence = structuredClone(fixtures.RemoteDeviceCommandExecution);
(staleSequence.command as Record<string, unknown>).command_sequence = 3;
expectInvalid("RemoteDeviceCommandExecution", staleSequence, "replayed command sequence");

expectValid("OtaAssignment", {{ ...fixtures.OtaAssignment, target_version: "1.10.0", anti_rollback_floor_version: "1.2.0" }});
expectInvalid("OtaAssignment", {{ ...fixtures.OtaAssignment, target_version: "9.0.0", target_firmware_security_version: 9 }}, "semver lexical anti-rollback bypass");
const oldActiveSlot = structuredClone(fixtures.OtaStateReceipt);
(oldActiveSlot.slots as Array<Record<string, unknown>>)[0].firmware_security_version = 9;
expectInvalid("OtaStateReceipt", oldActiveSlot, "active slot below anti-rollback floor");
expectInvalid("RemoteDeviceCommand", {{ ...fixtures.RemoteDeviceCommand, command_type: "remote_unmute" }}, "remote unmute");
console.log("typescript-v2-contracts-ok");
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(harness)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "typescript-v2-contracts-ok" in result.stdout


def test_generated_go_v2_validators_enforce_cross_domain_fences(
    tmp_path: Path,
) -> None:
    if shutil.which("go") is None:
        pytest.skip("go unavailable")
    package = tmp_path / "multi_subject"
    package.mkdir()
    shutil.copy2(GO_ARTIFACT, package / "multi_subject_contracts.go")
    (package / "go.mod").write_text(
        "module memoria.local/contracts\n\ngo 1.22\n", encoding="utf-8"
    )
    fixtures = {
        "RuntimeProfileV2": _runtime_profile_v2(),
        "PolicyReceiptV2": _policy_receipt_v2(),
        "SharedMemoryConfirmationEvidence": _confirmation("person-a"),
        "SharedMemoryWithdrawalEvidence": _withdrawal(),
        "SharedMemoryProposal": _shared_proposal(),
        "SharedMemoryPromotion": _shared_promotion(),
        "DeviceAttestation": _device_attestation(),
        "OtaAssignment": _ota_assignment(),
        "OtaStateReceipt": _ota_state_receipt(),
        "RemoteDeviceCommand": _remote_command(),
        "RemoteDeviceCommandExecution": _remote_execution(),
        "MemoryWriteFence": _memory_fence(),
    }
    object_defs = _schema()["objects"]["properties"]["definitions"]["properties"]  # type: ignore[index]
    required = {name: object_defs[name]["required"] for name in fixtures}
    (package / "contracts_v2_test.go").write_text(
        f'''package multi_subject

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"testing"
)

var fixtureJSON = []byte(`{json.dumps(fixtures, separators=(",", ":"))}`)
var requiredJSON = []byte(`{json.dumps(required, separators=(",", ":"))}`)

func decodeByName(name string, data []byte) error {{
	switch name {{
	case "PolicyReceiptV2":
		_, err := DecodePolicyReceiptV2Strict(data); return err
	case "RuntimeProfileV2":
		var value RuntimeProfileV2
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&value); err != nil {{ return err }}
		if err := decoder.Decode(&struct{{}}{{}}); err != io.EOF {{ return fmt.Errorf("unexpected trailing data: %v", err) }}
		return value.Validate()
	case "SharedMemoryConfirmationEvidence":
		_, err := DecodeSharedMemoryConfirmationEvidenceStrict(data); return err
	case "SharedMemoryWithdrawalEvidence":
		_, err := DecodeSharedMemoryWithdrawalEvidenceStrict(data); return err
	case "SharedMemoryProposal":
		_, err := DecodeSharedMemoryProposalStrict(data); return err
	case "SharedMemoryPromotion":
		_, err := DecodeSharedMemoryPromotionStrict(data); return err
	case "DeviceAttestation":
		_, err := DecodeDeviceAttestationStrict(data); return err
	case "OtaAssignment":
		_, err := DecodeOtaAssignmentStrict(data); return err
	case "OtaStateReceipt":
		_, err := DecodeOtaStateReceiptStrict(data); return err
	case "RemoteDeviceCommand":
		_, err := DecodeRemoteDeviceCommandStrict(data); return err
	case "RemoteDeviceCommandExecution":
		_, err := DecodeRemoteDeviceCommandExecutionStrict(data); return err
	case "MemoryWriteFence":
		_, err := DecodeMemoryWriteFenceStrict(data); return err
	default:
		return fmt.Errorf("missing decoder %s", name)
	}}
}}

func cloneFixture(t *testing.T, fixtures map[string]json.RawMessage, name string) map[string]any {{
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(fixtures[name], &out); err != nil {{ t.Fatal(err) }}
	return out
}}

func encoded(t *testing.T, value any) []byte {{
	t.Helper()
	out, err := json.Marshal(value)
	if err != nil {{ t.Fatal(err) }}
	return out
}}

func expectInvalid(t *testing.T, name string, value any, label string) {{
	t.Helper()
	if err := decodeByName(name, encoded(t, value)); err == nil {{
		t.Fatalf("%s accepted %s", name, label)
	}}
}}

func TestGeneratedV2Contracts(t *testing.T) {{
	var fixtures map[string]json.RawMessage
	var required map[string][]string
	if err := json.Unmarshal(fixtureJSON, &fixtures); err != nil {{ t.Fatal(err) }}
	if err := json.Unmarshal(requiredJSON, &required); err != nil {{ t.Fatal(err) }}
	var legacy RuntimeProfile
	if err := json.Unmarshal([]byte(`{json.dumps(_legacy_runtime_profile(), separators=(",", ":"))}`), &legacy); err != nil {{ t.Fatal(err) }}
	if err := legacy.Validate(); err != nil {{ t.Fatalf("legacy consumer must still parse RuntimeProfile v1: %v", err) }}
	if IsNewProducerContract("RuntimeProfile") {{ t.Fatal("v1 runtime profile producer must be forbidden") }}
	if err := RequireNewProducerContract("RuntimeProfile"); err == nil {{ t.Fatal("v1 producer guard did not reject") }}
	if !IsNewProducerContract("RuntimeProfileV2") {{ t.Fatal("v2 runtime profile must be current producer") }}
	if err := RequireNewProducerContract("RuntimeProfileV2"); err != nil {{ t.Fatalf("v2 producer guard rejected: %v", err) }}
	foundActionFence := false
	for _, value := range ContextHashInputsByContract["PolicyReceiptV2"] {{ if value == "action_resource_fence" {{ foundActionFence = true }} }}
	if !foundActionFence {{ t.Fatal("Go context hash inputs lost action fence") }}
	runtimeProfile := cloneFixture(t, fixtures, "RuntimeProfileV2")
	obligations := runtimeProfile["obligations"].([]any)
	runtimeDuplicate := cloneFixture(t, fixtures, "RuntimeProfileV2")
	runtimeDuplicate["obligations"] = []any{{obligations[0], obligations[0]}}
	expectInvalid(t, "RuntimeProfileV2", runtimeDuplicate, "duplicate structured obligations")
	runtimeSameCodeDistinct := cloneFixture(t, fixtures, "RuntimeProfileV2")
	runtimeSameCodeDistinctObligations := runtimeSameCodeDistinct["obligations"].([]any)
	variant := cloneFixture(t, fixtures, "RuntimeProfileV2")
	variantObligation := variant["obligations"].([]any)[0].(map[string]any)
	variantParams := variantObligation["params"].(map[string]any)
	variantParams["quiet_hours"] = []any{{"08:00", "09:00"}}
	runtimeSameCodeDistinct["obligations"] = []any{{runtimeSameCodeDistinctObligations[0], variantObligation}}
	if err := decodeByName("RuntimeProfileV2", encoded(t, runtimeSameCodeDistinct)); err != nil {{ t.Fatalf("RuntimeProfileV2 same code distinct params must validate: %v", err) }}
	for name, raw := range fixtures {{
		if err := decodeByName(name, raw); err != nil {{ t.Fatalf("%s valid fixture: %v", name, err) }}
		extra := cloneFixture(t, fixtures, name)
		extra["unknown_contract_field"] = true
		expectInvalid(t, name, extra, "unknown field")
		missing := cloneFixture(t, fixtures, name)
		delete(missing, required[name][0])
		expectInvalid(t, name, missing, "missing required field")
	}}

	var objection map[string]any
	if err := json.Unmarshal([]byte(`{json.dumps(_objection(), separators=(",", ":"))}`), &objection); err != nil {{ t.Fatal(err) }}
	if err := decodeByName("SharedMemoryConfirmationEvidence", encoded(t, objection)); err != nil {{ t.Fatalf("objection must not need allow receipt: %v", err) }}
	objection["approval_policy_receipt_id"] = "illegal"
	expectInvalid(t, "SharedMemoryConfirmationEvidence", objection, "object with allow receipt")

	promotion := cloneFixture(t, fixtures, "SharedMemoryPromotion")
	promotion["proposal_id"] = "proposal-2"
	expectInvalid(t, "SharedMemoryPromotion", promotion, "wrong proposal")
	promotion = cloneFixture(t, fixtures, "SharedMemoryPromotion")
	promotion["confirmation_evidence"] = promotion["confirmation_evidence"].([]any)[:1]
	expectInvalid(t, "SharedMemoryPromotion", promotion, "missing required vote")
	promotion = cloneFixture(t, fixtures, "SharedMemoryPromotion")
	promotion["confirmation_evidence"].([]any)[0].(map[string]any)["approval_snapshot_revision"] = float64(3)
	expectInvalid(t, "SharedMemoryPromotion", promotion, "replaced vote revision")
	promotion = cloneFixture(t, fixtures, "SharedMemoryPromotion")
	promotion["promoted_at"] = "2026-08-09T10:10:00Z"
	expectInvalid(t, "SharedMemoryPromotion", promotion, "expired receipt replay")
	confirmation := cloneFixture(t, fixtures, "SharedMemoryConfirmationEvidence")
	confirmation["proposal_status_after"] = "promoted"
	expectInvalid(t, "SharedMemoryConfirmationEvidence", confirmation, "vote directly promoted proposal")
	promotion = cloneFixture(t, fixtures, "SharedMemoryPromotion")
	inexactReceipt := promotion["promotion_policy_receipt"].(map[string]any)
	inexactReceipt["exact_fence"] = false
	expectInvalid(t, "PolicyReceiptV2", inexactReceipt, "inexact family receipt")

	fence := cloneFixture(t, fixtures, "MemoryWriteFence")
	fence["valid_until"] = fence["issued_at"]
	expectInvalid(t, "MemoryWriteFence", fence, "zero-length sensitive fence")

	attestation := cloneFixture(t, fixtures, "DeviceAttestation")
	attestation["certificate_status"] = "revoked"
	expectInvalid(t, "DeviceAttestation", attestation, "revoked certificate with attested capabilities")
	noHardwareCapability := cloneFixture(t, fixtures, "DeviceAttestation")
	filteredCapabilities := []any{{}}
	for _, value := range noHardwareCapability["capabilities"].([]any) {{
		if value != "physical_microphone_cut" {{ filteredCapabilities = append(filteredCapabilities, value) }}
	}}
	filteredAttested := []any{{}}
	for _, value := range noHardwareCapability["attested_capabilities"].([]any) {{
		if value != "physical_microphone_cut" {{ filteredAttested = append(filteredAttested, value) }}
	}}
	noHardwareCapability["capabilities"] = filteredCapabilities
	noHardwareCapability["attested_capabilities"] = filteredAttested
	expectInvalid(t, "DeviceAttestation", noHardwareCapability, "engaged mute without hardware capability")
	unbound := cloneFixture(t, fixtures, "DeviceAttestation")
	unbound["device_lifecycle_status"] = "manufactured"
	unbound["binding_id"] = nil
	unbound["binding_version"] = nil
	unbound["attested_capabilities"] = []any{{}}
	if err := decodeByName("DeviceAttestation", encoded(t, unbound)); err != nil {{ t.Fatalf("unbound lifecycle must allow null binding: %v", err) }}
	unbound["binding_id"] = "stale-binding"
	unbound["binding_version"] = float64(1)
	expectInvalid(t, "DeviceAttestation", unbound, "manufactured device stale binding")

	execution := cloneFixture(t, fixtures, "RemoteDeviceCommandExecution")
	execution["current_attestation"].(map[string]any)["binding_id"] = "binding-2"
	expectInvalid(t, "RemoteDeviceCommandExecution", execution, "old transferred binding")
	execution = cloneFixture(t, fixtures, "RemoteDeviceCommandExecution")
	execution["command"].(map[string]any)["expected_monotonic_counter"] = float64(7)
	expectInvalid(t, "RemoteDeviceCommandExecution", execution, "stale monotonic counter")
	execution = cloneFixture(t, fixtures, "RemoteDeviceCommandExecution")
	execution["command"].(map[string]any)["command_sequence"] = float64(3)
	expectInvalid(t, "RemoteDeviceCommandExecution", execution, "replayed command sequence")

	assignment := cloneFixture(t, fixtures, "OtaAssignment")
	assignment["target_version"] = "1.10.0"
	assignment["anti_rollback_floor_version"] = "1.2.0"
	if err := decodeByName("OtaAssignment", encoded(t, assignment)); err != nil {{ t.Fatalf("semver display strings must not control security order: %v", err) }}
	assignment["target_firmware_security_version"] = float64(9)
	expectInvalid(t, "OtaAssignment", assignment, "integer anti-rollback floor")
	otaState := cloneFixture(t, fixtures, "OtaStateReceipt")
	otaState["slots"].([]any)[0].(map[string]any)["firmware_security_version"] = float64(9)
	expectInvalid(t, "OtaStateReceipt", otaState, "active slot below anti-rollback floor")

	command := cloneFixture(t, fixtures, "RemoteDeviceCommand")
	command["command_type"] = "remote_unmute"
	expectInvalid(t, "RemoteDeviceCommand", command, "remote unmute")
}}
''',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["GOCACHE"] = str(tmp_path / "gocache")
    env["GOPATH"] = str(tmp_path / "gopath")
    result = subprocess.run(
        ["go", "test", "./..."],
        cwd=package,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
