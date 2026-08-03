"""Fail-closed A/B firmware update state machine.

The device security module verifies a signed manifest and artifact digest.  This
module owns the independent boot-control state: staging always targets the
inactive slot, activation requires a verified artifact, and an unconfirmed
slot is automatically abandoned after bounded boot attempts.  It is an
in-memory/reference state machine; the production device must persist the
snapshot in its bootloader's redundant metadata sector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from services.agent.src.voice_core.device_security import (
    OtaManifest,
    verify_artifact_digest,
    verify_ota_manifest,
)

SlotName = Literal["a", "b"]
SlotStatus = Literal["empty", "staged", "booting", "confirmed", "failed"]
_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("firmware versions must use numeric semver components")
    return tuple(int(part or "0") for part in match.groups())  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class OtaSlot:
    name: SlotName
    status: SlotStatus = "empty"
    version: str = ""
    sha256: str = ""
    boot_attempts: int = 0

    def __post_init__(self) -> None:
        if self.name not in {"a", "b"}:
            raise ValueError("OTA slot must be a or b")
        if self.boot_attempts < 0:
            raise ValueError("OTA boot attempts must be non-negative")
        if self.status == "empty" and (self.version or self.sha256):
            raise ValueError("empty OTA slot cannot carry an artifact")
        if self.status != "empty" and (not self.version or len(self.sha256) != 64):
            raise ValueError("non-empty OTA slot requires version and sha256")


@dataclass(slots=True)
class OtaUpdateManager:
    """Reference A/B boot policy with signature and anti-rollback checks."""

    verify_key: Ed25519PublicKey
    bootloader_version: str
    active_version: str
    active_slot: SlotName = "a"
    max_boot_attempts: int = 2
    _slots: dict[SlotName, OtaSlot] = field(init=False)
    _pending_slot: SlotName | None = field(default=None, init=False)
    _highest_version: tuple[int, int, int] = field(init=False)

    def __post_init__(self) -> None:
        _version_key(self.bootloader_version)
        _version_key(self.active_version)
        if self.active_slot not in {"a", "b"}:
            raise ValueError("active OTA slot must be a or b")
        if self.max_boot_attempts <= 0:
            raise ValueError("max_boot_attempts must be positive")
        self._highest_version = _version_key(self.active_version)
        self._slots = {
            "a": OtaSlot(
                name="a", status="confirmed", version=self.active_version, sha256="0" * 64
            ),
            "b": OtaSlot(name="b"),
        }

    @property
    def inactive_slot(self) -> SlotName:
        return "b" if self.active_slot == "a" else "a"

    @property
    def pending_slot(self) -> SlotName | None:
        return self._pending_slot

    @property
    def current_slot(self) -> OtaSlot:
        return self._slots[self.active_slot]

    def slot(self, name: SlotName) -> OtaSlot:
        return self._slots[name]

    def stage(
        self,
        manifest: OtaManifest,
        artifact: bytes,
        *,
        channel: str = "stable",
    ) -> SlotName:
        """Verify and stage a newer artifact in the inactive slot."""

        if manifest.channel != channel:
            raise ValueError("OTA manifest channel is not allowed for this device")
        if self._pending_slot is not None:
            raise ValueError("an OTA update is already pending confirmation")
        if not verify_ota_manifest(manifest, self.verify_key):
            raise ValueError("OTA manifest signature is invalid")
        if not verify_artifact_digest(artifact, manifest):
            raise ValueError("OTA artifact digest or size is invalid")
        if _version_key(manifest.min_bootloader) > _version_key(self.bootloader_version):
            raise ValueError("device bootloader is too old for this OTA")
        candidate_version = _version_key(manifest.version)
        if candidate_version <= self._highest_version:
            raise ValueError("OTA version is not newer than the accepted firmware")
        target = self.inactive_slot
        self._slots[target] = OtaSlot(
            name=target,
            status="staged",
            version=manifest.version,
            sha256=manifest.sha256.lower(),
        )
        self._highest_version = candidate_version
        self._pending_slot = None
        return target

    def activate(self, slot: SlotName | None = None) -> SlotName:
        """Mark a verified staged slot as the next boot target."""

        target = self.inactive_slot if slot is None else slot
        if target == self.active_slot or self._slots[target].status != "staged":
            raise ValueError("only the inactive staged OTA slot can be activated")
        staged = self._slots[target]
        self._slots[target] = OtaSlot(
            name=target,
            status="booting",
            version=staged.version,
            sha256=staged.sha256,
            boot_attempts=0,
        )
        self._pending_slot = target
        return target

    def boot(self) -> SlotName:
        """Select a boot slot and count an unconfirmed attempt.

        Once the pending slot exhausts attempts, it is marked failed and the
        last confirmed slot is selected.  This makes power loss before
        ``confirm_boot`` safe and deterministic.
        """

        pending = self._pending_slot
        if pending is None:
            return self.active_slot
        candidate = self._slots[pending]
        if candidate.status != "booting":
            self._pending_slot = None
            return self.active_slot
        if candidate.boot_attempts >= self.max_boot_attempts:
            self._slots[pending] = OtaSlot(
                name=pending,
                status="failed",
                version=candidate.version,
                sha256=candidate.sha256,
                boot_attempts=candidate.boot_attempts,
            )
            self._pending_slot = None
            return self.active_slot
        self._slots[pending] = OtaSlot(
            name=pending,
            status="booting",
            version=candidate.version,
            sha256=candidate.sha256,
            boot_attempts=candidate.boot_attempts + 1,
        )
        return pending

    def confirm_boot(self, version: str) -> SlotName:
        """Commit the pending slot only after the running image is healthy."""

        pending = self._pending_slot
        if pending is None or self._slots[pending].status != "booting":
            raise ValueError("there is no booting OTA slot to confirm")
        if version != self._slots[pending].version:
            raise ValueError("boot confirmation version does not match staged image")
        previous = self.active_slot
        candidate = self._slots[pending]
        self._slots[pending] = OtaSlot(
            name=pending,
            status="confirmed",
            version=candidate.version,
            sha256=candidate.sha256,
            boot_attempts=candidate.boot_attempts,
        )
        self._slots[previous] = OtaSlot(
            name=previous,
            status="confirmed",
            version=self._slots[previous].version,
            sha256=self._slots[previous].sha256,
            boot_attempts=0,
        )
        self.active_slot = pending
        self.active_version = candidate.version
        self._pending_slot = None
        return pending

    def mark_boot_failure(self) -> SlotName:
        """Record a health-check failure and fall back when bounded."""

        pending = self._pending_slot
        if pending is None:
            return self.active_slot
        candidate = self._slots[pending]
        # ``boot`` increments the attempt before control reaches the health
        # check.  Do not increment twice or max_boot_attempts would permit
        # only one actual boot.
        attempts = candidate.boot_attempts
        if attempts >= self.max_boot_attempts:
            self._slots[pending] = OtaSlot(
                name=pending,
                status="failed",
                version=candidate.version,
                sha256=candidate.sha256,
                boot_attempts=attempts,
            )
            self._pending_slot = None
            return self.active_slot
        self._slots[pending] = OtaSlot(
            name=pending,
            status="booting",
            version=candidate.version,
            sha256=candidate.sha256,
            boot_attempts=attempts,
        )
        return pending

    def snapshot(self) -> dict[str, object]:
        """Return the bounded metadata a bootloader store must persist."""

        return {
            "active_slot": self.active_slot,
            "active_version": self.active_version,
            "pending_slot": self._pending_slot,
            "slots": {
                name: {
                    "status": slot.status,
                    "version": slot.version,
                    "sha256": slot.sha256,
                    "boot_attempts": slot.boot_attempts,
                }
                for name, slot in self._slots.items()
            },
        }


__all__ = ["OtaSlot", "OtaUpdateManager", "SlotName", "SlotStatus"]
