"""Test-environment public key registration.

The device rows and the audit rows are the observable registry. Expected
fingerprints come from hashlib, not from the registration helper.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from scripts.register_test_device import main

from services.device_fleet.binding_release import release_committed_binding
from services.device_fleet.bootstrap_domain import (
    ClaimConflict,
    DevelopmentRegistrationDisabled,
    DeviceLifecycle,
    DeviceRecord,
    FirmwareBlocked,
    InvalidOnboardingRequest,
    sha256_hex,
)
from services.device_fleet.bootstrap_postgres_store import PostgresBootstrapStore
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.device_fleet.bootstrap_store import SQLiteBootstrapStore
from services.device_fleet.test_device_registration import (
    REGISTRATION_ACTION,
    TEST_ENVIRONMENT_ID,
    delete_registration_audits,
    delete_registration_audits_for_account,
)
from services.device_fleet.test_device_registration import (
    TestEnvironment as RegistrationEnvironment,
)
from services.device_fleet.test_device_registration import (
    TestRegistrationRejected as RegistrationRejected,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
AUDIT_COLUMNS = {
    "action",
    "created_at",
    "device_id",
    "public_key_sha256",
}
ADMIN_DSN = os.getenv(
    "MEMORIA_TEST_POSTGRES_DSN",
    "postgresql://memoria_test:memoria_test_local@127.0.0.1:55439/postgres",
)
ROLE_PASSWORD = "memoria_device_onboarding_local_test"


def _public_key() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return seed, public


def _fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def _write_public_key(path: Path, public_key: bytes) -> None:
    path.write_bytes(public_key)


def _test_environment(**overrides: str) -> dict[str, str]:
    environment = {"MEMORIA_ENVIRONMENT": TEST_ENVIRONMENT_ID}
    environment.update(overrides)
    return environment


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _device_rows(path: Path) -> list[sqlite3.Row]:
    connection = _connect(path)
    try:
        return list(
            connection.execute("SELECT * FROM device_onboarding_devices ORDER BY device_id")
        )
    finally:
        connection.close()


def _activation_manifests(path: Path) -> list[dict[str, object]]:
    connection = _connect(path)
    try:
        rows = connection.execute(
            "SELECT manifest_json FROM device_onboarding_activations ORDER BY activation_version"
        ).fetchall()
    finally:
        connection.close()
    return [json.loads(str(row["manifest_json"])) for row in rows]


def _audit_rows(path: Path) -> list[sqlite3.Row]:
    connection = _connect(path)
    try:
        return list(
            connection.execute(
                "SELECT * FROM device_onboarding_registration_audits ORDER BY public_key_sha256"
            )
        )
    finally:
        connection.close()


def _service(
    path: Path,
    now: datetime = NOW,
) -> tuple[DeviceOnboardingService, SQLiteBootstrapStore]:
    store = SQLiteBootstrapStore(path)
    return (
        DeviceOnboardingService(store, offline_mock=False, now_fn=lambda: now),
        store,
    )


def _register(
    service: DeviceOnboardingService,
    *,
    public_key: bytes,
    device_id: str = "dev_synth_01",
    certificate_id: str = "cert_synth_01",
    process_environment: Mapping[str, str] | None = None,
    firmware_security_version: int = 1,
    minimum_firmware_security_version: int = 1,
) -> object:
    return service.register_test_device_public_key(
        environment=TEST_ENVIRONMENT_ID,
        process_environment=process_environment or _test_environment(),
        device_id=device_id,
        certificate_id=certificate_id,
        public_key=public_key,
        product_model="memoria-esp32s3-devkit",
        hardware_revision="rev-a",
        firmware_version="0.1.0",
        firmware_security_version=firmware_security_version,
        capability_manifest_hash=sha256_hex(b"synthetic-capabilities"),
        minimum_firmware_security_version=minimum_firmware_security_version,
    )


def _cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    public_key: bytes,
    *,
    device_id: str = "dev_synth_01",
    certificate_id: str = "cert_synth_01",
    environment: str = TEST_ENVIRONMENT_ID,
    memoria_environment: str | None = TEST_ENVIRONMENT_ID,
    profile: str | None = None,
) -> tuple[int, Path]:
    if memoria_environment is None:
        monkeypatch.delenv("MEMORIA_ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("MEMORIA_ENVIRONMENT", memoria_environment)
    if profile is None:
        monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    else:
        monkeypatch.setenv("DEPLOYMENT_PROFILE", profile)
    key_path = tmp_path / f"{device_id}.pub"
    _write_public_key(key_path, public_key)
    database = tmp_path / "registry.sqlite"
    code = main(
        [
            "--environment",
            environment,
            "--database",
            str(database),
            "--device-id",
            device_id,
            "--certificate-id",
            certificate_id,
            "--public-key-in",
            str(key_path),
            "--capability-manifest-hash",
            sha256_hex(b"synthetic-capabilities"),
        ]
    )
    return code, database


@pytest.mark.parametrize(
    ("environment", "memoria_environment", "profile"),
    [
        ("production", TEST_ENVIRONMENT_ID, None),
        (TEST_ENVIRONMENT_ID, None, None),
        (TEST_ENVIRONMENT_ID, "production", None),
        (TEST_ENVIRONMENT_ID, "cn_self_hosted", None),
        (" memoria-test", TEST_ENVIRONMENT_ID, None),
        ("MEMORIA-TEST", TEST_ENVIRONMENT_ID, None),
        ("offline-mock", TEST_ENVIRONMENT_ID, None),
        ("", TEST_ENVIRONMENT_ID, None),
    ],
)
def test_rejected_environment_creates_no_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: str,
    memoria_environment: str | None,
    profile: str | None,
) -> None:
    _seed, public_key = _public_key()
    code, database = _cli(
        tmp_path,
        monkeypatch,
        public_key,
        environment=environment,
        memoria_environment=memoria_environment,
        profile=profile,
    )

    assert code == 1
    assert "test environment" in capsys.readouterr().err
    assert not database.exists()


def test_cn_self_hosted_with_memoria_test_registers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed, public_key = _public_key()
    code, database = _cli(
        tmp_path,
        monkeypatch,
        public_key,
        memoria_environment=TEST_ENVIRONMENT_ID,
        profile="cn_self_hosted",
    )

    assert code == 0
    assert len(_device_rows(database)) == 1
    audits = _audit_rows(database)
    assert len(audits) == 1
    assert set(audits[0].keys()) == AUDIT_COLUMNS


def test_deleting_registration_audits_reports_only_count_and_time(tmp_path: Path) -> None:
    _seed_a, key_a = _public_key()
    _seed_b, key_b = _public_key()
    database = tmp_path / "registry.sqlite"
    service, store = _service(database, NOW)
    try:
        _register(service, public_key=key_a)
        _register(
            service,
            public_key=key_b,
            device_id="dev_synth_02",
            certificate_id="cert_synth_02",
        )
        receipt = delete_registration_audits(store, device_id="dev_synth_01", now=LATER)
        again = delete_registration_audits(store, device_id="dev_synth_01", now=LATER)
    finally:
        store.close()

    assert {field.name for field in fields(type(receipt))} == {"deleted_count", "deleted_at"}
    assert receipt.deleted_count == 1
    assert receipt.deleted_at == LATER
    assert again.deleted_count == 0
    remaining = _audit_rows(database)
    assert [row["device_id"] for row in remaining] == ["dev_synth_02"]
    assert len(_device_rows(database)) == 2


def _bind_device(
    database: Path,
    *,
    device_id: str,
    account_id: str,
    status: str,
) -> None:
    stamp = NOW.isoformat().replace("+00:00", "Z")
    later = LATER.isoformat().replace("+00:00", "Z")
    digest = "a" * 64
    connection = _connect(database)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO device_onboarding_sessions (
                onboarding_session_id, device_id, actor_id, client_onboarding_id,
                qr_nonce_hash, pop_hash, mobile_nonce_hash, protocol_version,
                ble_name, ble_service_uuid, state, first_seen_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'synth', 'synth-service', 'consumed', ?, ?)
            """,
            (
                f"sess_{device_id}",
                device_id,
                account_id,
                f"client_{device_id}",
                digest,
                digest,
                digest,
                stamp,
                later,
            ),
        )
        connection.execute(
            """
            INSERT INTO device_onboarding_claims (
                claim_id, onboarding_session_id, device_id, actor_id, status,
                idempotency_key, reserved_at, expires_at
            ) VALUES (?, ?, ?, ?, 'committed', ?, ?, ?)
            """,
            (
                f"claim_{device_id}",
                f"sess_{device_id}",
                device_id,
                account_id,
                f"idem_{device_id}",
                stamp,
                later,
            ),
        )
        connection.execute(
            """
            INSERT INTO device_onboarding_bindings (
                binding_id, claim_id, device_id, actor_id, binding_version,
                status, initialization_json, idempotency_key, created_at
            ) VALUES (?, ?, ?, ?, 1, ?, '{}', ?, ?)
            """,
            (
                f"bind_{device_id}",
                f"claim_{device_id}",
                device_id,
                account_id,
                status,
                f"idem_bind_{device_id}",
                stamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_account_deletion_removes_only_that_accounts_bound_device_audits(
    tmp_path: Path,
) -> None:
    _seed_a, key_a = _public_key()
    _seed_b, key_b = _public_key()
    _seed_c, key_c = _public_key()
    database = tmp_path / "registry.sqlite"
    service, store = _service(database, NOW)
    try:
        _register(service, public_key=key_a, device_id="dev_synth_01", certificate_id="cert_synth_01")
        _register(service, public_key=key_b, device_id="dev_synth_02", certificate_id="cert_synth_02")
        _register(service, public_key=key_c, device_id="dev_synth_03", certificate_id="cert_synth_03")
        _bind_device(database, device_id="dev_synth_01", account_id="acct_synth_a", status="released")
        _bind_device(database, device_id="dev_synth_02", account_id="acct_synth_b", status="committed")
        _bind_device(database, device_id="dev_synth_03", account_id="acct_synth_a", status="draft")
        receipt = delete_registration_audits_for_account(
            store, account_id="acct_synth_a", now=LATER
        )
    finally:
        store.close()

    assert {field.name for field in fields(type(receipt))} == {"deleted_count", "deleted_at"}
    assert receipt.deleted_count == 1
    assert receipt.deleted_at == LATER
    remaining = {row["device_id"] for row in _audit_rows(database)}
    assert remaining == {"dev_synth_02", "dev_synth_03"}


@pytest.mark.asyncio
async def test_postgres_account_deletion_keeps_the_other_accounts_audit(
    postgres_registration: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str, str],
) -> None:
    maintenance, api, admin_dsn, _maintenance_dsn = postgres_registration
    _seed_a, key_a = _public_key()
    _seed_b, key_b = _public_key()
    service = DeviceOnboardingService(maintenance, offline_mock=False, now_fn=lambda: NOW)
    _register(service, public_key=key_a, device_id="dev_synth_01", certificate_id="cert_synth_01")
    _register(service, public_key=key_b, device_id="dev_synth_02", certificate_id="cert_synth_02")
    connection = await asyncpg.connect(admin_dsn)
    try:
        for device_id, account_id, status in (
            ("dev_synth_01", "acct_synth_a", "released"),
            ("dev_synth_02", "acct_synth_b", "committed"),
        ):
            digest = "ab" * 32
            await connection.execute(
                """
                INSERT INTO device_onboarding_sessions (
                    onboarding_session_id, device_id, actor_id, client_onboarding_id,
                    qr_nonce_hash, pop_hash, mobile_nonce_hash, protocol_version,
                    ble_name, ble_service_uuid, state, first_seen_at, expires_at
                ) VALUES ($1, $2, $3, $4, $5, $5, $5, 1, 'synth', 'synth-service',
                          'consumed', $6, $7)
                """,
                f"sess_{device_id}",
                device_id,
                account_id,
                f"client_{device_id}",
                digest,
                NOW,
                LATER,
            )
            await connection.execute(
                """
                INSERT INTO device_onboarding_claims (
                    claim_id, onboarding_session_id, device_id, actor_id, status,
                    idempotency_key, reserved_at, expires_at
                ) VALUES ($1, $2, $3, $4, 'committed', $5, $6, $7)
                """,
                f"claim_{device_id}",
                f"sess_{device_id}",
                device_id,
                account_id,
                f"idem_{device_id}",
                NOW,
                LATER,
            )
            await connection.execute(
                """
                INSERT INTO device_onboarding_bindings (
                    binding_id, claim_id, device_id, actor_id, binding_version,
                    status, initialization_json, idempotency_key, created_at
                ) VALUES ($1, $2, $3, $4, 1, $5, '{}'::jsonb, $6, $7)
                """,
                f"bind_{device_id}",
                f"claim_{device_id}",
                device_id,
                account_id,
                status,
                f"idem_bind_{device_id}",
                NOW,
            )
    finally:
        await connection.close()

    api_service = DeviceOnboardingService(api, offline_mock=False, now_fn=lambda: LATER)
    receipt = api_service.delete_account_registration_audits(account_id="acct_synth_a")
    _devices, audits, _columns = await _postgres_tables(admin_dsn)

    assert {field.name for field in fields(type(receipt))} == {"deleted_count", "deleted_at"}
    assert receipt.deleted_count == 1
    assert receipt.deleted_at == LATER
    assert [row["device_id"] for row in audits] == ["dev_synth_02"]


def test_unbinding_the_device_deletes_its_registration_audit(tmp_path: Path) -> None:
    _seed, public_key = _public_key()
    database = tmp_path / "registry.sqlite"
    service, store = _service(database, NOW)
    try:
        _register(service, public_key=public_key)
        connection = _connect(database)
        connection.execute(
            """
            UPDATE device_onboarding_devices
            SET lifecycle_status = 'bound', binding_id = ?, binding_version = 1,
                actor_id = ?
            WHERE device_id = ?
            """,
            ("bind_synth_01", "actor_synth_01", "dev_synth_01"),
        )
        connection.commit()
        connection.close()
        released = release_committed_binding(
            store,
            device_id="dev_synth_01",
            binding_id="bind_synth_01",
            reason_code="user_unbound",
            now=LATER,
        )
    finally:
        store.close()

    devices = _device_rows(database)
    assert released is True
    assert _audit_rows(database) == []
    assert devices[0]["lifecycle_status"] == "provisioned"
    assert devices[0]["binding_id"] is None


def test_unbinding_clears_the_display_name_from_the_activation_manifest(
    tmp_path: Path,
) -> None:
    _seed, public_key = _public_key()
    database = tmp_path / "registry.sqlite"
    service, store = _service(database, NOW)
    manifest = {
        "schema_version": 1,
        "display": {"robot_name": "robot", "primary_subject_display_name": "kept-name"},
    }
    try:
        _register(service, public_key=public_key)
        connection = _connect(database)
        connection.execute(
            """
            UPDATE device_onboarding_devices
            SET lifecycle_status = 'bound', binding_id = ?, binding_version = 1,
                actor_id = ?
            WHERE device_id = ?
            """,
            ("bind_synth_01", "actor_synth_01", "dev_synth_01"),
        )
        connection.execute(
            """
            INSERT INTO device_onboarding_activations (
                activation_id, device_id, claim_id, binding_id, binding_version,
                activation_version, manifest_json, manifest_hash, status, issued_at,
                expires_at
            ) VALUES (?, ?, ?, ?, 1, 1, ?, ?, 'manifest_ready', ?, ?)
            """,
            (
                "act_synth_01",
                "dev_synth_01",
                "claim_synth_01",
                "bind_synth_01",
                json.dumps(manifest),
                "a" * 64,
                NOW.isoformat(),
                LATER.isoformat(),
            ),
        )
        connection.commit()
        connection.close()
        released = release_committed_binding(
            store,
            device_id="dev_synth_01",
            binding_id="bind_synth_01",
            reason_code="user_unbound",
            now=LATER,
        )
    finally:
        store.close()

    manifests = _activation_manifests(database)
    assert released is True
    assert manifests == [
        {
            "schema_version": 1,
            "display": {"robot_name": "robot", "primary_subject_display_name": ""},
        }
    ]


def test_command_rejects_seed_and_private_key_arguments() -> None:
    for flag in ("--seed-in", "--private-key-in", "--private-key-out"):
        with pytest.raises(SystemExit) as raised:
            main([flag, "secret.bin"])
        assert raised.value.code == 2


def test_command_rejects_a_public_key_that_is_not_32_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MEMORIA_ENVIRONMENT", TEST_ENVIRONMENT_ID)
    monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    key_path = tmp_path / "short.pub"
    key_path.write_bytes(b"\x01" * 31)
    database = tmp_path / "registry.sqlite"

    code = main(
        [
            "--environment",
            TEST_ENVIRONMENT_ID,
            "--database",
            str(database),
            "--device-id",
            "dev_synth_01",
            "--certificate-id",
            "cert_synth_01",
            "--public-key-in",
            str(key_path),
        ]
    )

    assert code == 1
    assert "32 raw bytes" in capsys.readouterr().err
    assert not database.exists()


def test_command_registers_the_public_key_and_audit_without_the_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed, public_key = _public_key()
    monkeypatch.setenv("MEMORIA_ENVIRONMENT", TEST_ENVIRONMENT_ID)
    monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    key_path = tmp_path / "device.pub"
    _write_public_key(key_path, public_key)
    database = tmp_path / "registry.sqlite"

    code = main(
        [
            "--environment",
            TEST_ENVIRONMENT_ID,
            "--database",
            str(database),
            "--device-id",
            "dev_synth_01",
            "--certificate-id",
            "cert_synth_01",
            "--public-key-in",
            str(key_path),
            "--capability-manifest-hash",
            sha256_hex(b"synthetic-capabilities"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    devices = _device_rows(database)
    audits = _audit_rows(database)
    stored_key = base64.urlsafe_b64decode(str(devices[0]["public_key_b64"]) + "==")

    assert code == 0
    assert payload == {
        "audit_created_at": audits[0]["created_at"],
        "certificate_id": "cert_synth_01",
        "created": True,
        "device_id": "dev_synth_01",
        "environment_id": TEST_ENVIRONMENT_ID,
        "private_key_stored": False,
        "public_key_sha256": _fingerprint(public_key),
        "requested_device_id": "dev_synth_01",
    }
    assert seed not in database.read_bytes()
    assert stored_key == public_key
    assert len(devices) == 1
    assert len(audits) == 1
    connection = _connect(database)
    try:
        columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(device_onboarding_registration_audits)"
            )
        }
    finally:
        connection.close()
    assert columns == AUDIT_COLUMNS
    assert audits[0]["action"] == REGISTRATION_ACTION
    assert audits[0]["public_key_sha256"] == _fingerprint(public_key)
    assert audits[0]["device_id"] == "dev_synth_01"
    assert audits[0]["created_at"]


def test_repeat_registration_does_not_change_the_device_or_the_audit(
    tmp_path: Path,
) -> None:
    _seed, public_key = _public_key()
    database = tmp_path / "registry.sqlite"
    clocks = iter((NOW, LATER))
    service, store = _service(database, NOW)
    service.now_fn = lambda: next(clocks)
    try:
        first = _register(service, public_key=public_key)
        first_devices = [tuple(row) for row in _device_rows(database)]
        first_audits = [tuple(row) for row in _audit_rows(database)]
        second = _register(
            service,
            public_key=public_key,
            device_id="dev_synth_other",
            certificate_id="cert_synth_other",
        )
        second_devices = [tuple(row) for row in _device_rows(database)]
        second_audits = [tuple(row) for row in _audit_rows(database)]
    finally:
        store.close()

    assert first.created is True
    assert second.created is False
    assert second.device_id == "dev_synth_01"
    assert second.certificate_id == "cert_synth_01"
    assert first_devices == second_devices
    assert first_audits == second_audits
    assert second.audit.created_at == NOW
    assert len(second_devices) == 1
    assert len(second_audits) == 1


def test_existing_device_id_with_a_different_key_is_left_unchanged(
    tmp_path: Path,
) -> None:
    _seed, first_key = _public_key()
    _other_seed, second_key = _public_key()
    database = tmp_path / "registry.sqlite"
    service, store = _service(database)
    try:
        _register(service, public_key=first_key)
        before = [tuple(row) for row in _device_rows(database)]
        before_audits = [tuple(row) for row in _audit_rows(database)]
        with pytest.raises(ClaimConflict):
            _register(service, public_key=second_key, certificate_id="cert_synth_02")
        after = [tuple(row) for row in _device_rows(database)]
        after_audits = [tuple(row) for row in _audit_rows(database)]
    finally:
        store.close()

    assert before == after
    assert before_audits == after_audits
    assert _fingerprint(first_key) in after_audits[0]
    assert _fingerprint(second_key) not in after_audits[0]


def test_missing_audit_is_added_without_changing_the_device_row(tmp_path: Path) -> None:
    _seed, public_key = _public_key()
    database = tmp_path / "registry.sqlite"
    store = SQLiteBootstrapStore(database)
    store.register_manufactured_device(
        DeviceRecord(
            device_id="dev_synth_01",
            certificate_id="cert_synth_01",
            public_key=public_key,
            product_model="memoria-esp32s3-devkit",
            hardware_revision="rev-a",
            firmware_version="0.1.0",
            firmware_security_version=1,
            capability_manifest_hash=sha256_hex(b"synthetic-capabilities"),
            minimum_firmware_security_version=1,
            lifecycle_status=DeviceLifecycle.MANUFACTURED,
            last_monotonic_counter=0,
            binding_id=None,
            binding_version=None,
            actor_id=None,
            activation_version=0,
            last_activation_counter=0,
        )
    )
    before = [tuple(row) for row in _device_rows(database)]
    service = DeviceOnboardingService(store, offline_mock=False, now_fn=lambda: NOW)
    try:
        result = _register(service, public_key=public_key)
        after = [tuple(row) for row in _device_rows(database)]
        audits = _audit_rows(database)
    finally:
        store.close()

    assert result.created is False
    assert before == after
    assert len(audits) == 1
    assert audits[0]["public_key_sha256"] == _fingerprint(public_key)
    assert audits[0]["created_at"] == NOW.isoformat().replace("+00:00", "Z")


def test_registration_stays_fail_closed_for_offline_mock(tmp_path: Path) -> None:
    _seed, public_key = _public_key()
    service, store = _service(tmp_path / "registry.sqlite")
    try:
        with pytest.raises(DevelopmentRegistrationDisabled):
            service.register_offline_mock_device(
                device_id="dev_synth_01",
                certificate_id="cert_synth_01",
                public_key_b64="AAAA",
                product_model="memoria-esp32s3-devkit",
                hardware_revision="rev-a",
                firmware_version="0.1.0",
                firmware_security_version=1,
                capability_manifest_hash=sha256_hex(b"synthetic-capabilities"),
            )
        with pytest.raises(FirmwareBlocked):
            _register(
                service,
                public_key=public_key,
                firmware_security_version=0,
                minimum_firmware_security_version=1,
            )
        assert _device_rows(tmp_path / "registry.sqlite") == []
    finally:
        store.close()


def test_production_environment_value_cannot_be_constructed() -> None:
    with pytest.raises(RegistrationRejected):
        RegistrationEnvironment("production")
    with pytest.raises(RegistrationRejected):
        RegistrationEnvironment("cn_self_hosted")


def _dsn_with(dsn: str, *, database: str, user: str, password: str) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit(
        (parsed.scheme, f"{quote(user)}:{quote(password)}@{host}", f"/{database}", "", "")
    )


@pytest.fixture
async def postgres_registration() -> tuple[PostgresBootstrapStore, PostgresBootstrapStore, str, str]:
    database = f"device_registration_{uuid.uuid4().hex[:10]}"
    try:
        admin = await asyncpg.connect(ADMIN_DSN)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL integration DSN unavailable: {exc}")
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    admin_db_dsn = urlunsplit(
        (urlsplit(ADMIN_DSN).scheme, urlsplit(ADMIN_DSN).netloc, f"/{database}", "", "")
    )
    maintenance_dsn = _dsn_with(
        admin_db_dsn,
        database=database,
        user="memoria_device_onboarding_maintenance",
        password=ROLE_PASSWORD,
    )
    maintenance = PostgresBootstrapStore(maintenance_dsn, role="maintenance")
    api = PostgresBootstrapStore(
        _dsn_with(
            admin_db_dsn,
            database=database,
            user="memoria_device_onboarding_api",
            password=ROLE_PASSWORD,
        )
    )
    try:
        maintenance.initialize(bootstrap_dsn=admin_db_dsn, role_password=ROLE_PASSWORD)
        api.initialize()
        yield maintenance, api, admin_db_dsn, maintenance_dsn
    finally:
        api.close()
        maintenance.close()
        admin = await asyncpg.connect(ADMIN_DSN)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            await admin.close()


async def _postgres_tables(
    dsn: str,
) -> tuple[list[dict[str, object]], list[dict[str, object]], set[str]]:
    connection = await asyncpg.connect(dsn)
    try:
        devices = [
            dict(row)
            for row in await connection.fetch(
                "SELECT * FROM device_onboarding_devices ORDER BY device_id"
            )
        ]
        audits = [
            dict(row)
            for row in await connection.fetch(
                "SELECT * FROM device_onboarding_registration_audits ORDER BY public_key_sha256"
            )
        ]
        columns = {
            str(name)
            for name in await connection.fetchval(
                """
                SELECT array_agg(column_name)
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'device_onboarding_registration_audits'
                """
            )
            or []
        }
    finally:
        await connection.close()
    return devices, audits, columns


@pytest.mark.asyncio
async def test_postgres_repeat_keeps_the_device_row_and_audit(
    postgres_registration: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str, str],
) -> None:
    maintenance, _api, admin_dsn, _maintenance_dsn = postgres_registration
    _seed, public_key = _public_key()
    clocks = iter((NOW, LATER))
    service = DeviceOnboardingService(maintenance, offline_mock=False, now_fn=lambda: next(clocks))
    first = _register(service, public_key=public_key)
    devices, audits, columns = await _postgres_tables(admin_dsn)
    second = _register(
        service,
        public_key=public_key,
        device_id="dev_synth_other",
        certificate_id="cert_synth_other",
    )
    again_devices, again_audits, _columns = await _postgres_tables(admin_dsn)

    assert first.created is True
    assert second.created is False
    assert second.device_id == "dev_synth_01"
    assert devices == again_devices
    assert audits == again_audits
    assert columns == AUDIT_COLUMNS
    assert len(again_devices) == 1
    assert len(again_audits) == 1
    assert again_audits[0]["public_key_sha256"] == _fingerprint(public_key)
    assert again_audits[0]["action"] == REGISTRATION_ACTION
    assert "person" not in columns
    assert "seed" not in columns


@pytest.mark.asyncio
async def test_postgres_api_role_cannot_register(
    postgres_registration: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str, str],
) -> None:
    _maintenance, api, admin_dsn, _maintenance_dsn = postgres_registration
    _seed, public_key = _public_key()
    service = DeviceOnboardingService(api, offline_mock=False, now_fn=lambda: NOW)

    with pytest.raises(InvalidOnboardingRequest):
        _register(service, public_key=public_key)
    devices, audits, _columns = await _postgres_tables(admin_dsn)

    assert devices == []
    assert audits == []


@pytest.mark.asyncio
async def test_command_registers_on_postgres_and_a_repeat_is_a_no_op(
    postgres_registration: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _maintenance, _api, admin_dsn, maintenance_dsn = postgres_registration
    _seed, public_key = _public_key()
    key_path = tmp_path / "device.pub"
    _write_public_key(key_path, public_key)
    monkeypatch.setenv("MEMORIA_ENVIRONMENT", TEST_ENVIRONMENT_ID)
    monkeypatch.delenv("DEPLOYMENT_PROFILE", raising=False)
    argv = [
        "--environment",
        TEST_ENVIRONMENT_ID,
        "--dsn",
        maintenance_dsn,
        "--device-id",
        "dev_synth_01",
        "--certificate-id",
        "cert_synth_01",
        "--public-key-in",
        str(key_path),
        "--capability-manifest-hash",
        sha256_hex(b"synthetic-capabilities"),
    ]

    first = main(argv)
    first_payload = json.loads(capsys.readouterr().out)
    devices, audits, _columns = await _postgres_tables(admin_dsn)
    second = main(argv)
    second_payload = json.loads(capsys.readouterr().out)
    again_devices, again_audits, _columns = await _postgres_tables(admin_dsn)

    assert first == 0
    assert second == 0
    assert first_payload["created"] is True
    assert second_payload["created"] is False
    assert second_payload["device_id"] == "dev_synth_01"
    assert second_payload["private_key_stored"] is False
    assert devices == again_devices
    assert audits == again_audits
    assert len(again_audits) == 1


@pytest.mark.asyncio
async def test_postgres_api_unbind_deletes_the_registration_audit(
    postgres_registration: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str, str],
) -> None:
    maintenance, api, admin_dsn, _maintenance_dsn = postgres_registration
    _seed, public_key = _public_key()
    service = DeviceOnboardingService(maintenance, offline_mock=False, now_fn=lambda: NOW)
    _register(service, public_key=public_key)
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(
            """
            UPDATE device_onboarding_devices
            SET lifecycle_status = 'bound', binding_id = $2, binding_version = 1,
                actor_id = $3
            WHERE device_id = $1
            """,
            "dev_synth_01",
            "bind_synth_01",
            "actor_synth_01",
        )
    finally:
        await connection.close()

    api_reader = await asyncpg.connect(
        _dsn_with(
            admin_dsn,
            database=urlsplit(admin_dsn).path.lstrip("/"),
            user="memoria_device_onboarding_api",
            password=ROLE_PASSWORD,
        )
    )
    try:
        visible = await api_reader.fetch(
            "SELECT public_key_sha256 FROM device_onboarding_registration_audits"
        )
    finally:
        await api_reader.close()
    assert visible == []
    released = release_committed_binding(
        api,
        device_id="dev_synth_01",
        binding_id="bind_synth_01",
        reason_code="user_unbound",
        now=LATER,
    )
    _devices, audits, columns = await _postgres_tables(admin_dsn)

    assert released is True
    assert audits == []


@pytest.mark.asyncio
async def test_postgres_unbind_clears_the_display_name_from_the_activation_manifest(
    postgres_registration: tuple[PostgresBootstrapStore, PostgresBootstrapStore, str, str],
) -> None:
    maintenance, api, admin_dsn, _maintenance_dsn = postgres_registration
    _seed, public_key = _public_key()
    service = DeviceOnboardingService(maintenance, offline_mock=False, now_fn=lambda: NOW)
    _register(service, public_key=public_key)
    manifest = {
        "schema_version": 1,
        "display": {"robot_name": "robot", "primary_subject_display_name": "kept-name"},
    }
    connection = await asyncpg.connect(admin_dsn)
    try:
        await connection.execute(
            """
            UPDATE device_onboarding_devices
            SET lifecycle_status = 'bound', binding_id = $2, binding_version = 1,
                actor_id = $3
            WHERE device_id = $1
            """,
            "dev_synth_01",
            "bind_synth_01",
            "actor_synth_01",
        )
        await connection.execute(
            """
            INSERT INTO device_onboarding_activations (
                activation_id, device_id, actor_id, claim_id, binding_id, binding_version,
                activation_version, manifest_json, manifest_hash, status, issued_at, expires_at
            ) VALUES ($1, $2, $3, $4, $5, 1, 1, $6::jsonb, $7, 'manifest_ready', $8, $9)
            """,
            "act_synth_01",
            "dev_synth_01",
            "actor_synth_01",
            "claim_synth_01",
            "bind_synth_01",
            json.dumps(manifest),
            "a" * 64,
            NOW,
            LATER,
        )
    finally:
        await connection.close()

    released = release_committed_binding(
        api,
        device_id="dev_synth_01",
        binding_id="bind_synth_01",
        reason_code="user_unbound",
        now=LATER,
    )
    reader = await asyncpg.connect(admin_dsn)
    try:
        stored = await reader.fetchval(
            "SELECT manifest_json FROM device_onboarding_activations WHERE activation_id = $1",
            "act_synth_01",
        )
    finally:
        await reader.close()

    assert released is True
    assert json.loads(stored)["display"]["primary_subject_display_name"] == ""
