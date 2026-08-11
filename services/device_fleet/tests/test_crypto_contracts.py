from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceAttestation,
    DeviceCapability,
    DeviceCertificateStatus,
    DeviceLifecycleStatus,
    OtaAssignmentStatus,
    OtaBootStatus,
    OtaSlotName,
    PhysicalMuteState,
    PrivacyLightState,
    SimLifecycleStatus,
)

from services.device_fleet.crypto import (
    sign_device_attestation,
    sign_ota_assignment,
    verify_device_attestation_signature,
    verify_ota_assignment_artifact,
)


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _attestation_payload(now: datetime) -> dict[str, object]:
    capabilities = (
        DeviceCapability.DEVICE_CAPABILITY_DEVICE_CERTIFICATE,
        DeviceCapability.DEVICE_CAPABILITY_SECURE_ELEMENT,
        DeviceCapability.DEVICE_CAPABILITY_PHYSICAL_MICROPHONE_CUT,
        DeviceCapability.DEVICE_CAPABILITY_HARDWARE_PRIVACY_LIGHT,
        DeviceCapability.DEVICE_CAPABILITY_AB_OTA,
        DeviceCapability.DEVICE_CAPABILITY_ANTI_ROLLBACK,
        DeviceCapability.DEVICE_CAPABILITY_REMOTE_ATTESTATION,
    )
    return {
        "attestation_schema": "device-attestation-v1",
        "attestation_id": "attestation-1",
        "device_id": "device-1",
        "certificate_id": "certificate-1",
        "certificate_status": DeviceCertificateStatus.DEVICE_CERTIFICATE_STATUS_ACTIVE,
        "device_lifecycle_status": DeviceLifecycleStatus.DEVICE_LIFECYCLE_STATUS_BOUND,
        "binding_id": "binding-1",
        "binding_version": 1,
        "firmware_version": "1.0.0",
        "firmware_security_version": 10,
        "firmware_sha256": "1" * 64,
        "bootloader_version": "1.0.0",
        "capability_manifest_hash": "2" * 64,
        "capabilities": capabilities,
        "attested_capabilities": capabilities,
        "physical_mute_state": PhysicalMuteState.PHYSICAL_MUTE_STATE_ENGAGED,
        "privacy_light_state": PrivacyLightState.PRIVACY_LIGHT_STATE_ON,
        "sim_status": SimLifecycleStatus.SIM_LIFECYCLE_STATUS_ACTIVE,
        "active_ota_slot": OtaSlotName.OTA_SLOT_NAME_A,
        "ota_boot_status": OtaBootStatus.OTA_BOOT_STATUS_CONFIRMED,
        "anti_rollback_floor_version": "1.0.0",
        "anti_rollback_floor_security_version": 10,
        "monotonic_counter": 1,
        "last_command_sequence": 0,
        "nonce": "nonce-0123456789abcdef",
        "occurred_at": _rfc3339(now),
        "expires_at": _rfc3339(now + timedelta(minutes=2)),
        "signer_key_id": "certificate-1",
        "signature_algorithm": "ed25519",
    }


def test_device_attestation_uses_generated_contract_and_detects_tampering() -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    private_key = Ed25519PrivateKey.generate()

    signed = sign_device_attestation(_attestation_payload(now), private_key)

    assert type(signed) is DeviceAttestation
    assert verify_device_attestation_signature(signed, private_key.public_key())
    tampered = signed.model_copy(update={"firmware_security_version": 11})
    assert not verify_device_attestation_signature(tampered, private_key.public_key())


def test_generated_ota_assignment_verifies_exact_artifact_digest_and_size() -> None:
    now = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    private_key = Ed25519PrivateKey.generate()
    artifact = b"signed firmware image"
    assignment = sign_ota_assignment(
        {
            "assignment_id": "assignment-1",
            "device_id": "device-1",
            "target_slot": OtaSlotName.OTA_SLOT_NAME_B,
            "target_version": "2.0.0",
            "target_firmware_security_version": 20,
            "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
            "artifact_size_bytes": len(artifact),
            "min_bootloader_version": "1.0.0",
            "anti_rollback_floor_version": "1.0.0",
            "anti_rollback_floor_security_version": 10,
            "channel": "stable",
            "status": OtaAssignmentStatus.OTA_ASSIGNMENT_STATUS_PENDING,
            "assigned_at": _rfc3339(now),
            "expires_at": _rfc3339(now + timedelta(hours=1)),
            "signer_key_id": "ota-key-1",
            "signature_algorithm": "ed25519",
        },
        private_key,
    )

    assert verify_ota_assignment_artifact(assignment, artifact)
    assert not verify_ota_assignment_artifact(assignment, artifact + b"tampered")
