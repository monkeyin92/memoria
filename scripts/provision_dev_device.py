#!/usr/bin/env python3
"""Create local development device identity material.

This is intentionally limited to the backend/offline-mock registration seam.
It can generate a seed or consume an existing local seed so firmware and Fleet
share one public identity, but only the public key enters the store. It does not
flash firmware; the ESP-IDF/esptool step remains a separate operation.
"""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from services.device_fleet.bootstrap_domain import (
    BootstrapQRPayload,
    b64url_encode,
    encode_bootstrap_qr,
    sha256_hex,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.bootstrap_store import SQLiteBootstrapStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-mock", action="store_true", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--certificate-id", required=True)
    parser.add_argument("--product-model", default="memoria-esp32s3-devkit")
    parser.add_argument("--hardware-revision", default="rev-a")
    parser.add_argument("--firmware-version", default="0.1.0")
    parser.add_argument("--firmware-security-version", type=int, default=1)
    parser.add_argument("--minimum-firmware-security-version", type=int, default=1)
    parser.add_argument(
        "--capability-manifest-hash",
        default=sha256_hex(b"memoria-esp32s3-dev-capabilities-v1"),
    )
    parser.add_argument("--ble-name", required=True)
    parser.add_argument("--ble-service-uuid", required=True, type=UUID)
    private_key = parser.add_mutually_exclusive_group(required=True)
    private_key.add_argument("--private-key-in", type=Path)
    private_key.add_argument("--private-key-out", type=Path)
    parser.add_argument("--qr-out", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.offline_mock:
        raise SystemExit("--offline-mock is required; production registration is fail-closed")
    if (
        args.private_key_out is not None and args.private_key_out.exists()
    ) or args.qr_out.exists():
        raise SystemExit(
            "refusing to overwrite existing private-key-out or qr-out; choose new paths"
        )
    if args.firmware_security_version < args.minimum_firmware_security_version:
        raise SystemExit("firmware security version is below the device floor")
    if args.private_key_in is not None:
        if args.private_key_in.stat().st_mode & 0o077:
            raise SystemExit("private-key-in must not be readable by group or others")
        private_key_bytes = args.private_key_in.read_bytes()
        if len(private_key_bytes) != 32:
            raise SystemExit("private-key-in must contain exactly one raw Ed25519 seed")
        private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    else:
        private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,  # type: ignore[arg-type]
        format=serialization.PublicFormat.Raw,  # type: ignore[arg-type]
    )
    store = SQLiteBootstrapStore(args.database)
    try:
        service = DeviceOnboardingService(store, offline_mock=True)
        service.register_offline_mock_device(
            device_id=args.device_id,
            certificate_id=args.certificate_id,
            public_key_b64=b64url_encode(public_key),
            product_model=args.product_model,
            hardware_revision=args.hardware_revision,
            firmware_version=args.firmware_version,
            firmware_security_version=args.firmware_security_version,
            capability_manifest_hash=args.capability_manifest_hash,
            minimum_firmware_security_version=args.minimum_firmware_security_version,
        )
        payload = BootstrapQRPayload(
            typ="memoria-device-bootstrap",
            ver=1,
            device_id=args.device_id,
            bootstrap_nonce=b64url_encode(secrets.token_bytes(16)),
            ble_name=args.ble_name,
            ble_service_uuid=str(args.ble_service_uuid),
            certificate_id=args.certificate_id,
            provisioning_protocol="memoria-provisioning/1",
            firmware_version=args.firmware_version,
            pop=b64url_encode(secrets.token_bytes(32)),
        )
        private_bytes = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,  # type: ignore[arg-type]
            format=serialization.PrivateFormat.Raw,  # type: ignore[arg-type]
            encryption_algorithm=serialization.NoEncryption(),
        )
        private_key_file = args.private_key_in or args.private_key_out
        assert private_key_file is not None
        if args.private_key_out is not None:
            args.private_key_out.parent.mkdir(parents=True, exist_ok=True)
            args.private_key_out.write_bytes(private_bytes)
            args.private_key_out.chmod(0o600)
        args.qr_out.parent.mkdir(parents=True, exist_ok=True)
        args.qr_out.write_text(encode_bootstrap_qr(payload, private_key), encoding="utf-8")
        args.qr_out.chmod(0o600)
        print(
            json.dumps(
                {
                    "device_id": args.device_id,
                    "certificate_id": args.certificate_id,
                    "public_key": b64url_encode(public_key),
                    "private_key_file": str(private_key_file),
                    "qr_file": str(args.qr_out),
                    "private_key_printed": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
