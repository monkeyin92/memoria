#!/usr/bin/env python3
"""Register one device public key in the test onboarding registry.

Set MEMORIA_ENVIRONMENT=memoria-test and pass --environment memoria-test.
Any other value, including a missing MEMORIA_ENVIRONMENT, is refused.
DEPLOYMENT_PROFILE is not a gate. The public key file is 32 raw bytes, the same file
firmware/esp32/scripts/provision_identity.py takes as --activation-pubkey-in.
A seed or a private key is not an argument. Repeating the command for the
same public key does not insert or change a device row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from services.device_fleet.bootstrap_domain import (
    OnboardingError,
    public_key_from_bytes,
    sha256_hex,
)
from services.device_fleet.bootstrap_postgres_store import PostgresBootstrapStore
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.bootstrap_store import SQLiteBootstrapStore
from services.device_fleet.test_device_registration import (
    TestRegistrationRejected,
    require_test_environment,
)


class PublicKeyRejected(Exception):
    """The public key file is not 32 raw Ed25519 bytes."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--database", type=Path)
    target.add_argument("--dsn")
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--certificate-id", required=True)
    parser.add_argument("--public-key-in", type=Path, required=True)
    parser.add_argument("--product-model", default="memoria-esp32s3-devkit")
    parser.add_argument("--hardware-revision", default="rev-a")
    parser.add_argument("--firmware-version", default="0.1.0")
    parser.add_argument("--firmware-security-version", type=int, default=1)
    parser.add_argument("--minimum-firmware-security-version", type=int, default=1)
    parser.add_argument(
        "--capability-manifest-hash",
        default=sha256_hex(b"memoria-esp32s3-dev-capabilities-v1"),
    )
    return parser


def _read_public_key(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise PublicKeyRejected("public key input must be a regular file")
    value = path.read_bytes()
    if len(value) != 32:
        raise PublicKeyRejected("public key input must contain exactly 32 raw bytes")
    public_key_from_bytes(value)
    return value


def _open_store(args: argparse.Namespace) -> SQLiteBootstrapStore | PostgresBootstrapStore:
    if args.database is not None:
        return SQLiteBootstrapStore(args.database)
    store = PostgresBootstrapStore(args.dsn, role="maintenance")
    store.initialize()
    return store


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        require_test_environment(
            environment=args.environment,
            process_environment=os.environ,
        )
    except TestRegistrationRejected as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    store: SQLiteBootstrapStore | PostgresBootstrapStore | None = None
    try:
        public_key = _read_public_key(args.public_key_in)
        store = _open_store(args)
        service = DeviceOnboardingService(store, offline_mock=False)
        result = service.register_test_device_public_key(
            environment=args.environment,
            process_environment=os.environ,
            device_id=args.device_id,
            certificate_id=args.certificate_id,
            public_key=public_key,
            product_model=args.product_model,
            hardware_revision=args.hardware_revision,
            firmware_version=args.firmware_version,
            firmware_security_version=args.firmware_security_version,
            capability_manifest_hash=args.capability_manifest_hash,
            minimum_firmware_security_version=args.minimum_firmware_security_version,
        )
    except (PublicKeyRejected, OnboardingError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: device registration failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
    created_at = result.audit.created_at.isoformat().replace("+00:00", "Z")
    print(
        json.dumps(
            {
                "environment_id": args.environment,
                "device_id": result.device_id,
                "requested_device_id": args.device_id,
                "certificate_id": result.certificate_id,
                "created": result.created,
                "public_key_sha256": result.public_key_sha256,
                "audit_created_at": created_at,
                "private_key_stored": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
