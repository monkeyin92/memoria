"""Canonical Ed25519 signing for generated Device Fleet contracts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceAttestation,
    OtaAssignment,
    OtaStateReceipt,
    RemoteDeviceCommand,
)
from pydantic import BaseModel

_SIGNATURE_PLACEHOLDER = "A" * 86


def _encode_signature(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_signature(value: str) -> bytes:
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid signature encoding") from exc
    if len(decoded) != 64:
        raise ValueError("invalid Ed25519 signature length")
    return decoded


def _canonical_unsigned(model: BaseModel) -> bytes:
    payload = model.model_dump(mode="json", exclude={"signature"})
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sign_device_attestation(
    payload: Mapping[str, object],
    private_key: Ed25519PrivateKey,
) -> DeviceAttestation:
    """Validate and sign the canonical generated attestation contract."""

    unsigned = DeviceAttestation.model_validate(
        {**payload, "signature": _SIGNATURE_PLACEHOLDER}
    )
    signature = _encode_signature(private_key.sign(_canonical_unsigned(unsigned)))
    return DeviceAttestation.model_validate({**payload, "signature": signature})


def verify_device_attestation_signature(
    attestation: DeviceAttestation,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify a generated attestation without weakening contract validation."""

    try:
        public_key.verify(
            _decode_signature(attestation.signature),
            _canonical_unsigned(attestation),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def sign_remote_device_command(
    payload: Mapping[str, object],
    private_key: Ed25519PrivateKey,
) -> RemoteDeviceCommand:
    """Validate and sign the canonical generated remote-command contract."""

    unsigned = RemoteDeviceCommand.model_validate(
        {**payload, "signature": _SIGNATURE_PLACEHOLDER}
    )
    signature = _encode_signature(private_key.sign(_canonical_unsigned(unsigned)))
    return RemoteDeviceCommand.model_validate({**payload, "signature": signature})


def verify_remote_device_command_signature(
    command: RemoteDeviceCommand,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify the server signature on a generated remote command."""

    try:
        public_key.verify(
            _decode_signature(command.signature),
            _canonical_unsigned(command),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def sign_ota_assignment(
    payload: Mapping[str, object],
    private_key: Ed25519PrivateKey,
) -> OtaAssignment:
    """Validate and sign the canonical generated OTA assignment."""

    unsigned = OtaAssignment.model_validate(
        {**payload, "signature": _SIGNATURE_PLACEHOLDER}
    )
    signature = _encode_signature(private_key.sign(_canonical_unsigned(unsigned)))
    return OtaAssignment.model_validate({**payload, "signature": signature})


def verify_ota_assignment_signature(
    assignment: OtaAssignment,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify the server signature on a generated OTA assignment."""

    try:
        public_key.verify(
            _decode_signature(assignment.signature),
            _canonical_unsigned(assignment),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_ota_assignment_artifact(
    assignment: OtaAssignment,
    artifact: bytes,
) -> bool:
    """Verify downloaded bytes before any inactive-slot staging is reported."""

    return len(artifact) == assignment.artifact_size_bytes and hmac.compare_digest(
        hashlib.sha256(artifact).hexdigest(), assignment.artifact_sha256
    )


def sign_ota_state_receipt(
    payload: Mapping[str, object],
    private_key: Ed25519PrivateKey,
) -> OtaStateReceipt:
    """Validate and sign the generated device OTA state receipt."""

    unsigned = OtaStateReceipt.model_validate(
        {**payload, "signature": _SIGNATURE_PLACEHOLDER}
    )
    signature = _encode_signature(private_key.sign(_canonical_unsigned(unsigned)))
    return OtaStateReceipt.model_validate({**payload, "signature": signature})


def verify_ota_state_receipt_signature(
    receipt: OtaStateReceipt,
    public_key: Ed25519PublicKey,
) -> bool:
    """Verify a device-signed generated OTA state receipt."""

    try:
        public_key.verify(
            _decode_signature(receipt.signature),
            _canonical_unsigned(receipt),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


__all__ = [
    "sign_device_attestation",
    "sign_ota_assignment",
    "sign_ota_state_receipt",
    "sign_remote_device_command",
    "verify_device_attestation_signature",
    "verify_ota_assignment_signature",
    "verify_ota_assignment_artifact",
    "verify_ota_state_receipt_signature",
    "verify_remote_device_command_signature",
]
