from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from services.agent.src.voice_core.device_security import OtaManifest, sign_ota_manifest
from services.agent.src.voice_core.ota import OtaUpdateManager


def _manifest(private: Ed25519PrivateKey, *, version: str = "1.1.0", payload: bytes = b"firmware") -> OtaManifest:
    unsigned = OtaManifest(
        version=version,
        artifact_url="https://updates.example.test/memoria.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        min_bootloader="1.0.0",
    )
    return sign_ota_manifest(unsigned, private)


def test_ota_ab_flow_confirms_new_slot_after_signed_digest_check() -> None:
    private = Ed25519PrivateKey.generate()
    manager = OtaUpdateManager(
        verify_key=private.public_key(), bootloader_version="1.0.0", active_version="1.0.0"
    )
    target = manager.stage(_manifest(private), b"firmware")
    assert target == "b"
    assert manager.activate() == "b"
    assert manager.boot() == "b"
    assert manager.confirm_boot("1.1.0") == "b"
    assert manager.active_slot == "b"
    assert manager.current_slot.status == "confirmed"
    assert manager.pending_slot is None


def test_ota_ab_flow_falls_back_after_bounded_boot_failures() -> None:
    private = Ed25519PrivateKey.generate()
    manager = OtaUpdateManager(
        verify_key=private.public_key(),
        bootloader_version="1.0.0",
        active_version="1.0.0",
        max_boot_attempts=2,
    )
    manager.stage(_manifest(private), b"firmware")
    manager.activate()
    assert manager.boot() == "b"
    assert manager.mark_boot_failure() == "b"
    assert manager.boot() == "b"
    assert manager.mark_boot_failure() == "a"
    assert manager.active_slot == "a"
    assert manager.slot("b").status == "failed"


def test_ota_rejects_tampered_old_or_incompatible_artifacts() -> None:
    private = Ed25519PrivateKey.generate()
    manager = OtaUpdateManager(
        verify_key=private.public_key(), bootloader_version="1.0.0", active_version="1.0.0"
    )
    with pytest.raises(ValueError, match="digest"):
        manager.stage(_manifest(private), b"tampered")
    with pytest.raises(ValueError, match="not newer"):
        manager.stage(_manifest(private, version="1.0.0"), b"firmware")
    with pytest.raises(ValueError, match="bootloader"):
        newer_bootloader = OtaManifest(
            version="1.2.0",
            artifact_url="https://updates.example.test/memoria.bin",
            sha256=hashlib.sha256(b"firmware").hexdigest(),
            size_bytes=len(b"firmware"),
            min_bootloader="2.0.0",
        )
        manager.stage(sign_ota_manifest(newer_bootloader, private), b"firmware")
