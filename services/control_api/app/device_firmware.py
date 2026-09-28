"""Signed firmware releases for device over-the-air updates.

A release is published by ``firmware/esp32/scripts/publish_firmware_release.py``
into a directory this service reads (``MEMORIA_FIRMWARE_RELEASE_DIR``, by
default ``firmware-releases`` next to ``MEMORIA_DB_PATH``: in production the
existing ``/data`` bind of ``/var/lib/memoria``)::

    <root>/<board>/current.json          the signed release document
    <root>/<board>/<build>/app.bin       the application image it describes

The document is signed offline with the firmware release key; the device holds
the public key and is the authority that accepts or refuses an image.  This
service verifies the same signature before offering a release, so a broken or
foreign ``current.json`` is never served, and it only serves the image of the
current release.  Requests are device-signed exactly like the activation
manifest (``{certificate_id, device_id, method, path}``); unlike the display
poll, an unbound device may update too.  Everything here is read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from services.device_fleet.bootstrap_domain import (
    DeviceLifecycle,
    DeviceNotFound,
    DeviceRecord,
    DeviceRevoked,
    InvalidDeviceProof,
    OnboardingError,
    b64url_decode,
    public_key_from_bytes,
    verify_signed_payload,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService

FIRMWARE_BOARD = "memoria-esp-vocat"
RELEASE_SCHEMA_VERSION = 1
RELEASE_SIGNING_DOMAIN = b"memoria-firmware-release-v1\n"
#: Public half of the firmware release key; the firmware embeds the same bytes
#: (overlay/files/main/memoria/memoria_firmware_release.h).
RELEASE_PUBLIC_KEY = bytes.fromhex(
    "b9fc4ad5dea748e4f67341b155417ab65e06c37c16c95a2696493461ae6b009a"
)
#: The ota_0/ota_1 slots in partitions/v2/32m.csv.
MAX_IMAGE_BYTES = 0x3F0000

_RELEASE_KEYS = frozenset(
    {"schema_version", "board", "build", "version", "size", "sha256", "signature"}
)
_VERSION = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InvalidFirmwareRelease(ValueError):
    """``current.json`` is malformed, unsigned or does not match its image."""


def canonical_release_payload(release: Mapping[str, object]) -> bytes:
    """The bytes the release key signs: domain + sorted compact JSON sans signature."""
    unsigned = {key: value for key, value in release.items() if key != "signature"}
    return RELEASE_SIGNING_DOMAIN + json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def release_request_payload(*, device_id: str, certificate_id: str, path: str) -> dict[str, object]:
    return {
        "method": "GET",
        "path": path,
        "device_id": device_id,
        "certificate_id": certificate_id,
    }


def release_path(device_id: str) -> str:
    return f"/v1/devices/{device_id}/firmware-release"


def image_path(device_id: str, build: int) -> str:
    return f"/v1/devices/{device_id}/firmware-release/{build}/image"


def validate_release(document: object, *, public_key: bytes = RELEASE_PUBLIC_KEY) -> dict[str, object]:
    """Strict shape and signature check of a release document."""
    if not isinstance(document, dict) or set(document) != _RELEASE_KEYS:
        raise InvalidFirmwareRelease("release keys are invalid")
    release = cast(dict[str, object], document)
    build = release["build"]
    size = release["size"]
    if release["schema_version"] != RELEASE_SCHEMA_VERSION:
        raise InvalidFirmwareRelease("unsupported release schema")
    if release["board"] != FIRMWARE_BOARD:
        raise InvalidFirmwareRelease("release is for another board")
    if not isinstance(build, int) or isinstance(build, bool) or not 0 < build < 2**31:
        raise InvalidFirmwareRelease("release build is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_IMAGE_BYTES:
        raise InvalidFirmwareRelease("release size is invalid")
    if not isinstance(release["version"], str) or not _VERSION.fullmatch(release["version"]):
        raise InvalidFirmwareRelease("release version is invalid")
    if not isinstance(release["sha256"], str) or not _SHA256.fullmatch(release["sha256"]):
        raise InvalidFirmwareRelease("release sha256 is invalid")
    signature = release["signature"]
    if not isinstance(signature, str):
        raise InvalidFirmwareRelease("release signature is invalid")
    try:
        raw_signature = b64url_decode(signature, field="signature", exact_length=64)
        Ed25519PublicKey.from_public_bytes(public_key).verify(
            raw_signature, canonical_release_payload(release)
        )
    except (InvalidSignature, OnboardingError, ValueError) as exc:
        raise InvalidFirmwareRelease("release signature does not verify") from exc
    return release


@dataclass(frozen=True, slots=True)
class FirmwareRelease:
    document: dict[str, object]
    image: Path

    @property
    def build(self) -> int:
        return cast(int, self.document["build"])


class FirmwareReleaseDirectory:
    """Reads the published release; re-read on every request (tiny files)."""

    def __init__(self, root: Path, *, public_key: bytes = RELEASE_PUBLIC_KEY) -> None:
        self.root = root
        self._public_key = public_key
        self._verified: tuple[tuple[int, int, int], FirmwareRelease] | None = None

    def current(self, board: str = FIRMWARE_BOARD) -> FirmwareRelease | None:
        manifest = self.root / board / "current.json"
        try:
            raw = manifest.read_bytes()
        except FileNotFoundError:
            return None
        release = validate_release(json.loads(raw), public_key=self._public_key)
        image = self.root / board / str(release["build"]) / "app.bin"
        try:
            stat = image.stat()
        except FileNotFoundError as exc:
            raise InvalidFirmwareRelease("release image is missing") from exc
        if stat.st_size != release["size"]:
            raise InvalidFirmwareRelease("release image size does not match")
        # Hash the image once per (build, size, mtime), not on every poll.
        fingerprint = (cast(int, release["build"]), stat.st_size, stat.st_mtime_ns)
        if self._verified is not None and self._verified[0] == fingerprint:
            return self._verified[1]
        digest = hashlib.sha256()
        with image.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 16), b""):
                digest.update(chunk)
        if digest.hexdigest() != release["sha256"]:
            raise InvalidFirmwareRelease("release image hash does not match")
        verified = FirmwareRelease(document=release, image=image)
        self._verified = (fingerprint, verified)
        return verified


def release_directory_for(memoria_db_path: str) -> FirmwareReleaseDirectory:
    configured = os.environ.get("MEMORIA_FIRMWARE_RELEASE_DIR", "").strip()
    return FirmwareReleaseDirectory(
        Path(configured) if configured else Path(memoria_db_path).parent / "firmware-releases"
    )


def authenticate_device_get(
    service: DeviceOnboardingService,
    *,
    device_id: str,
    certificate_id: str,
    request_signature: bytes | None,
    path: str,
) -> DeviceRecord:
    """Manufactured certificate, not revoked, signature over this exact path."""
    device = service.store.get_device(device_id)
    if device is None:
        raise DeviceNotFound()
    if device.certificate_id != certificate_id:
        raise InvalidDeviceProof("certificate does not belong to device")
    if device.lifecycle_status is DeviceLifecycle.REVOKED:
        raise DeviceRevoked()
    if request_signature is None and not service.offline_mock:
        raise InvalidDeviceProof("device request signature is required")
    if request_signature is not None:
        verify_signed_payload(
            public_key=public_key_from_bytes(device.public_key),
            payload=release_request_payload(
                device_id=device_id, certificate_id=certificate_id, path=path
            ),
            signature=request_signature,
        )
    return device
