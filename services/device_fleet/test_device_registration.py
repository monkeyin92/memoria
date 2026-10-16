"""Register one public key in the test onboarding registry.

The device table is not unique on the public key, because manufacturing rows
may already share one. This command still refuses to insert a second row for
a key it can already see, and it never updates the row it found. The audit
table records the fingerprint and the first registration time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from services.device_fleet.bootstrap_domain import (
    ClaimConflict,
    DeviceRecord,
    InvalidOnboardingRequest,
    OnboardingError,
    b64url_encode,
    now_utc,
    sha256_hex,
)
from services.device_fleet.bootstrap_port import BootstrapStorePort
from services.device_fleet.bootstrap_postgres_store import PostgresBootstrapStore, _Scope
from services.device_fleet.bootstrap_store import (
    SQLiteBootstrapStore,
    _parse_timestamp,
    _timestamp,
)

TEST_ENVIRONMENT_ID = "memoria-test"
PRODUCTION_DEPLOYMENT_PROFILE = "cn_self_hosted"
REGISTRATION_ACTION = "device.public_key_registered"

_AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS device_onboarding_registration_audits (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) BETWEEN 1 AND 160),
    action TEXT NOT NULL CHECK (action = 'device.public_key_registered'),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    public_key_sha256 TEXT NOT NULL CHECK (length(public_key_sha256) = 64),
    environment_id TEXT NOT NULL CHECK (environment_id = 'memoria-test'),
    created_at TEXT NOT NULL,
    UNIQUE (public_key_sha256)
)
"""


class TestRegistrationRejected(OnboardingError):
    code = "TEST_ENVIRONMENT_REQUIRED"
    status_code = 403


@dataclass(frozen=True, slots=True)
class TestEnvironment:
    """The only environment id this registration path can hold."""

    environment_id: str

    def __post_init__(self) -> None:
        if self.environment_id != TEST_ENVIRONMENT_ID:
            raise TestRegistrationRejected("registration requires the test environment")


@dataclass(frozen=True, slots=True)
class RegistrationAudit:
    event_id: str
    action: str
    device_id: str
    public_key_sha256: str
    environment_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TestDeviceRegistration:
    device_id: str
    certificate_id: str
    public_key_sha256: str
    created: bool
    audit: RegistrationAudit


def require_test_environment(
    *,
    environment: str,
    process_environment: Mapping[str, str],
) -> TestEnvironment:
    """Accept only the test id, and refuse the production deployment profile."""

    profile = process_environment.get("DEPLOYMENT_PROFILE", "")
    declared = process_environment.get("MEMORIA_ENVIRONMENT", "")
    if (
        profile == PRODUCTION_DEPLOYMENT_PROFILE
        or environment != TEST_ENVIRONMENT_ID
        or declared != TEST_ENVIRONMENT_ID
    ):
        raise TestRegistrationRejected("registration requires the test environment")
    return TestEnvironment(TEST_ENVIRONMENT_ID)


def apply_test_device_registration(
    store: BootstrapStorePort,
    *,
    environment: TestEnvironment,
    record: DeviceRecord,
    now: datetime,
) -> TestDeviceRegistration:
    if not isinstance(environment, TestEnvironment):
        raise TestRegistrationRejected("registration requires the test environment")
    registered_at = now_utc(now)
    if isinstance(store, SQLiteBootstrapStore):
        return _apply_sqlite(store, environment, record, registered_at)
    if isinstance(store, PostgresBootstrapStore):
        return _apply_postgres(store, environment, record, registered_at)
    raise TestRegistrationRejected("registration requires the test environment")


def _fingerprint(public_key: bytes) -> str:
    return sha256_hex(public_key)


def _event_id(fingerprint: str) -> str:
    return f"{REGISTRATION_ACTION}:{fingerprint}"


def _result(
    device: DeviceRecord,
    audit: RegistrationAudit,
    *,
    created: bool,
) -> TestDeviceRegistration:
    return TestDeviceRegistration(
        device_id=device.device_id,
        certificate_id=device.certificate_id,
        public_key_sha256=audit.public_key_sha256,
        created=created,
        audit=audit,
    )


def _ensure_sqlite_audit_table(store: SQLiteBootstrapStore) -> None:
    with store._write() as connection:
        connection.execute(_AUDIT_DDL)


def _sqlite_find(store: SQLiteBootstrapStore, public_key: bytes) -> DeviceRecord | None:
    with store._read() as connection:
        row = connection.execute(
            """
            SELECT * FROM device_onboarding_devices
            WHERE public_key_b64 = ?
            ORDER BY device_id
            LIMIT 1
            """,
            (b64url_encode(public_key),),
        ).fetchone()
    if row is None:
        return None
    return SQLiteBootstrapStore._device(row)


def _sqlite_audit(
    store: SQLiteBootstrapStore,
    *,
    device_id: str,
    public_key: bytes,
    environment: TestEnvironment,
    now: datetime,
) -> RegistrationAudit:
    fingerprint = _fingerprint(public_key)
    with store._write() as connection:
        connection.execute(
            """
            INSERT INTO device_onboarding_registration_audits (
                event_id, action, device_id, public_key_sha256,
                environment_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (public_key_sha256) DO NOTHING
            """,
            (
                _event_id(fingerprint),
                REGISTRATION_ACTION,
                device_id,
                fingerprint,
                environment.environment_id,
                _timestamp(now),
            ),
        )
        row = connection.execute(
            """
            SELECT event_id, action, device_id, public_key_sha256,
                   environment_id, created_at
            FROM device_onboarding_registration_audits
            WHERE public_key_sha256 = ?
            """,
            (fingerprint,),
        ).fetchone()
    if row is None:
        raise RuntimeError("registration audit was not stored")
    created_at = _parse_timestamp(str(row["created_at"]))
    if created_at is None:
        raise RuntimeError("registration audit timestamp is missing")
    return RegistrationAudit(
        event_id=str(row["event_id"]),
        action=str(row["action"]),
        device_id=str(row["device_id"]),
        public_key_sha256=str(row["public_key_sha256"]),
        environment_id=str(row["environment_id"]),
        created_at=created_at,
    )


def _apply_sqlite(
    store: SQLiteBootstrapStore,
    environment: TestEnvironment,
    record: DeviceRecord,
    now: datetime,
) -> TestDeviceRegistration:
    _ensure_sqlite_audit_table(store)
    existing = _sqlite_find(store, record.public_key)
    if existing is not None:
        audit = _sqlite_audit(
            store,
            device_id=existing.device_id,
            public_key=existing.public_key,
            environment=environment,
            now=now,
        )
        return _result(existing, audit, created=False)
    if store.get_device(record.device_id) is not None:
        raise ClaimConflict("device identity already exists")
    try:
        stored = store.register_manufactured_device(record)
    except ClaimConflict:
        raced = _sqlite_find(store, record.public_key)
        if raced is not None and raced.public_key == record.public_key:
            audit = _sqlite_audit(
                store,
                device_id=raced.device_id,
                public_key=raced.public_key,
                environment=environment,
                now=now,
            )
            return _result(raced, audit, created=False)
        raise
    audit = _sqlite_audit(
        store,
        device_id=stored.device_id,
        public_key=stored.public_key,
        environment=environment,
        now=now,
    )
    return _result(stored, audit, created=True)


def _apply_postgres(
    store: PostgresBootstrapStore,
    environment: TestEnvironment,
    record: DeviceRecord,
    now: datetime,
) -> TestDeviceRegistration:
    if store.role != "maintenance":
        raise InvalidOnboardingRequest(
            "manufactured device registration requires the maintenance role"
        )
    existing = _postgres_find(store, record.public_key)
    if existing is not None:
        audit = _postgres_audit(
            store,
            device_id=existing.device_id,
            public_key=existing.public_key,
            environment=environment,
            now=now,
        )
        return _result(existing, audit, created=False)
    if store.get_device(record.device_id) is not None:
        raise ClaimConflict("device identity already exists")
    try:
        stored = store.register_manufactured_device(record)
    except ClaimConflict:
        raced = _postgres_find(store, record.public_key)
        if raced is not None and raced.public_key == record.public_key:
            audit = _postgres_audit(
                store,
                device_id=raced.device_id,
                public_key=raced.public_key,
                environment=environment,
                now=now,
            )
            return _result(raced, audit, created=False)
        raise
    audit = _postgres_audit(
        store,
        device_id=stored.device_id,
        public_key=stored.public_key,
        environment=environment,
        now=now,
    )
    return _result(stored, audit, created=True)


def _postgres_find(store: PostgresBootstrapStore, public_key: bytes) -> DeviceRecord | None:
    encoded = b64url_encode(public_key)

    async def operation(connection: asyncpg.Connection) -> DeviceRecord | None:
        row = await connection.fetchrow(
            """
            SELECT * FROM device_onboarding_devices
            WHERE public_key_b64 = $1
            ORDER BY device_id
            LIMIT 1
            """,
            encoded,
        )
        if row is None:
            return None
        return PostgresBootstrapStore._device(row)

    return store._call(store._transaction(_Scope(), operation))


def _postgres_audit(
    store: PostgresBootstrapStore,
    *,
    device_id: str,
    public_key: bytes,
    environment: TestEnvironment,
    now: datetime,
) -> RegistrationAudit:
    fingerprint = _fingerprint(public_key)
    event_id = _event_id(fingerprint)

    async def operation(connection: asyncpg.Connection) -> asyncpg.Record:
        await connection.execute(
            """
            INSERT INTO device_onboarding_registration_audits (
                event_id, action, device_id, public_key_sha256,
                environment_id, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (public_key_sha256) DO NOTHING
            """,
            event_id,
            REGISTRATION_ACTION,
            device_id,
            fingerprint,
            environment.environment_id,
            now,
        )
        row = await connection.fetchrow(
            """
            SELECT event_id, action, device_id, public_key_sha256,
                   environment_id, created_at
            FROM device_onboarding_registration_audits
            WHERE public_key_sha256 = $1
            """,
            fingerprint,
        )
        if row is None:
            raise RuntimeError("registration audit was not stored")
        return row

    row = store._call(store._transaction(_Scope(), operation))
    created_at = row["created_at"]
    if not isinstance(created_at, datetime):
        raise RuntimeError("registration audit timestamp is missing")
    return RegistrationAudit(
        event_id=str(row["event_id"]),
        action=str(row["action"]),
        device_id=str(row["device_id"]),
        public_key_sha256=str(row["public_key_sha256"]),
        environment_id=str(row["environment_id"]),
        created_at=now_utc(created_at),
    )


__all__ = [
    "PRODUCTION_DEPLOYMENT_PROFILE",
    "REGISTRATION_ACTION",
    "TEST_ENVIRONMENT_ID",
    "RegistrationAudit",
    "TestDeviceRegistration",
    "TestEnvironment",
    "TestRegistrationRejected",
    "apply_test_device_registration",
    "require_test_environment",
]
