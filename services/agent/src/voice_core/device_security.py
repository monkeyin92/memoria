"""Per-device identity, signed challenge, and OTA verification primitives.

The control plane owns enrolment and policy.  Devices only receive short-lived
session claims and signed firmware metadata.  Ed25519 is used through the
already-declared cryptography dependency; no shared device API key is needed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("encoded value is required")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid base64 value") from exc


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    public_key_b64: str
    firmware_channel: str = "stable"

    def __post_init__(self) -> None:
        if not self.device_id.strip() or len(self.device_id) > 128:
            raise ValueError("device_id must be a short non-empty string")
        public_key = _unb64(self.public_key_b64)
        if len(public_key) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        if self.firmware_channel not in {"stable", "canary", "lab"}:
            raise ValueError("unsupported firmware channel")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_unb64(self.public_key_b64)).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {
            "device_id": self.device_id,
            "public_key": self.public_key_b64,
            "fingerprint": self.fingerprint,
            "firmware_channel": self.firmware_channel,
        }


@dataclass(frozen=True, slots=True)
class ProvisionedDevice:
    identity: DeviceIdentity
    private_key_b64: str

    def private_key(self) -> Ed25519PrivateKey:
        raw = _unb64(self.private_key_b64)
        if len(raw) != 32:
            raise ValueError("Ed25519 private key must be 32 bytes")
        return Ed25519PrivateKey.from_private_bytes(raw)


def provision_device(device_id: str, *, firmware_channel: str = "stable") -> ProvisionedDevice:
    """Create a unique device keypair once during manufacturing/provisioning."""

    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    public_raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,  # type: ignore[arg-type]
        format=serialization.PublicFormat.Raw,  # type: ignore[arg-type]
    )
    private_raw = private.private_bytes(
        encoding=serialization.Encoding.Raw,  # type: ignore[arg-type]
        format=serialization.PrivateFormat.Raw,  # type: ignore[arg-type]
        encryption_algorithm=serialization.NoEncryption(),
    )
    return ProvisionedDevice(
        identity=DeviceIdentity(
            device_id=device_id,
            public_key_b64=_b64(public_raw),
            firmware_channel=firmware_channel,
        ),
        private_key_b64=_b64(private_raw),
    )


@dataclass(frozen=True, slots=True)
class SignedChallenge:
    device_id: str
    nonce: str
    issued_at_ms: int
    signature_b64: str

    def signing_bytes(self) -> bytes:
        return _canonical(
            {
                "device_id": self.device_id,
                "nonce": self.nonce,
                "issued_at_ms": self.issued_at_ms,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "nonce": self.nonce,
            "issued_at_ms": self.issued_at_ms,
            "signature": self.signature_b64,
        }


def sign_challenge(
    provisioned: ProvisionedDevice,
    *,
    nonce: str,
    issued_at_ms: int | None = None,
) -> SignedChallenge:
    if not nonce or len(nonce) > 256:
        raise ValueError("challenge nonce must be a short non-empty string")
    timestamp = int(time.time() * 1000) if issued_at_ms is None else issued_at_ms
    if timestamp < 0:
        raise ValueError("issued_at_ms must be non-negative")
    unsigned = SignedChallenge(
        device_id=provisioned.identity.device_id,
        nonce=nonce,
        issued_at_ms=timestamp,
        signature_b64="pending",
    )
    signature = provisioned.private_key().sign(unsigned.signing_bytes())
    return SignedChallenge(
        device_id=unsigned.device_id,
        nonce=unsigned.nonce,
        issued_at_ms=unsigned.issued_at_ms,
        signature_b64=_b64(signature),
    )


def verify_challenge(
    identity: DeviceIdentity,
    challenge: SignedChallenge,
    *,
    now_ms: int | None = None,
    max_age_ms: int = 120_000,
) -> bool:
    if challenge.device_id != identity.device_id or max_age_ms <= 0:
        return False
    current = int(time.time() * 1000) if now_ms is None else now_ms
    if current < 0 or challenge.issued_at_ms > current:
        return False
    if current - challenge.issued_at_ms > max_age_ms:
        return False
    try:
        signature = _unb64(challenge.signature_b64)
        Ed25519PublicKey.from_public_bytes(_unb64(identity.public_key_b64)).verify(
            signature,
            challenge.signing_bytes(),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


@dataclass(frozen=True, slots=True)
class OtaManifest:
    version: str
    artifact_url: str
    sha256: str
    size_bytes: int
    min_bootloader: str
    channel: str = "stable"
    rollout_percent: int = 100
    signature_b64: str = ""

    def __post_init__(self) -> None:
        if not self.version.strip() or len(self.version) > 64:
            raise ValueError("OTA version must be a short non-empty string")
        if not self.artifact_url.startswith("https://"):
            raise ValueError("OTA artifact URL must use HTTPS")
        if len(self.sha256) != 64 or any(char not in "0123456789abcdef" for char in self.sha256.lower()):
            raise ValueError("OTA sha256 must be a hexadecimal digest")
        if self.size_bytes <= 0:
            raise ValueError("OTA artifact size must be positive")
        if not self.min_bootloader.strip():
            raise ValueError("min_bootloader is required")
        if self.channel not in {"stable", "canary", "lab"}:
            raise ValueError("unsupported OTA channel")
        if not 0 <= self.rollout_percent <= 100:
            raise ValueError("rollout_percent must be between 0 and 100")

    def signing_bytes(self) -> bytes:
        return _canonical(
            {
                "version": self.version,
                "artifact_url": self.artifact_url,
                "sha256": self.sha256.lower(),
                "size_bytes": self.size_bytes,
                "min_bootloader": self.min_bootloader,
                "channel": self.channel,
                "rollout_percent": self.rollout_percent,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "artifact_url": self.artifact_url,
            "sha256": self.sha256.lower(),
            "size_bytes": self.size_bytes,
            "min_bootloader": self.min_bootloader,
            "channel": self.channel,
            "rollout_percent": self.rollout_percent,
            "signature": self.signature_b64,
        }


def sign_ota_manifest(manifest: OtaManifest, signer: Ed25519PrivateKey) -> OtaManifest:
    return OtaManifest(
        version=manifest.version,
        artifact_url=manifest.artifact_url,
        sha256=manifest.sha256,
        size_bytes=manifest.size_bytes,
        min_bootloader=manifest.min_bootloader,
        channel=manifest.channel,
        rollout_percent=manifest.rollout_percent,
        signature_b64=_b64(signer.sign(manifest.signing_bytes())),
    )


def verify_ota_manifest(manifest: OtaManifest, signer: Ed25519PublicKey) -> bool:
    try:
        signer.verify(_unb64(manifest.signature_b64), manifest.signing_bytes())
    except (InvalidSignature, ValueError):
        return False
    return True


def verify_artifact_digest(payload: bytes, manifest: OtaManifest) -> bool:
    return len(payload) == manifest.size_bytes and hashlib.sha256(payload).hexdigest() == manifest.sha256.lower()


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "DeviceIdentity",
    "OtaManifest",
    "ProvisionedDevice",
    "SignedChallenge",
    "provision_device",
    "sign_challenge",
    "sign_ota_manifest",
    "verify_artifact_digest",
    "verify_challenge",
    "verify_ota_manifest",
]
