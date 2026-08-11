"""Development SQLite persistence for Device Bootstrap/Claim/Activation.

The service only depends on the small operations exposed here.  The schema is
intentionally bootstrap-specific and contains hashes for QR nonce, PoP,
mobile nonce, and device challenges; it has no credential or private-key
columns.  Production uses
``services.device_fleet.bootstrap_postgres_store``; this implementation stays
limited to ``OFFLINE_MOCK`` and local tests.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from services.device_fleet.bootstrap_domain import (
    ActivationNotFound,
    ActivationRecord,
    ActivationStatus,
    ActorMismatch,
    BindingConflict,
    BindingInitialization,
    BindingRecord,
    BootstrapSession,
    BootstrapState,
    ChallengeReplay,
    ClaimConflict,
    ClaimExpired,
    ClaimNotFound,
    ClaimReservation,
    ClaimStatus,
    DeviceAlreadyBound,
    DeviceChallenge,
    DeviceLifecycle,
    DeviceMediaChallenge,
    DeviceMediaChallengeExpired,
    DeviceMediaChallengeRateLimited,
    DeviceNotFound,
    DeviceRecord,
    DeviceRevoked,
    InvalidOnboardingRequest,
    SessionExpired,
    SessionNotFound,
    StateVersionConflict,
    b64url_encode,
    now_utc,
    require_transition,
    validate_activation_manifest,
)


class BootstrapStorePort:
    """Structural store boundary shared by SQLite and PostgreSQL."""

    pass


_DEVICE_ACTIVE_CLAIMS: Final[tuple[str, ...]] = (
    ClaimStatus.RESERVED.value,
    ClaimStatus.BINDING_COMMITTING.value,
)
_TERMINAL_SESSION_STATES: Final[frozenset[str]] = frozenset(
    {
        BootstrapState.EXPIRED.value,
        BootstrapState.CANCELLED.value,
        BootstrapState.FAILED.value,
        BootstrapState.REVOKED.value,
        BootstrapState.CONFLICT.value,
        BootstrapState.ACTIVATED.value,
    }
)


def _timestamp(value: datetime) -> str:
    return now_utc(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("corrupt onboarding timestamp")
    return parsed.astimezone(UTC)


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError("corrupt onboarding JSON")
    return {str(key): item for key, item in parsed.items()}


def _in_clause(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


class SQLiteBootstrapStore(BootstrapStorePort):
    """Thread-safe SQLite store intended for ``OFFLINE_MOCK`` and local tests."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database,
            check_same_thread=False,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self.initialize()

    def initialize(self) -> None:
        with self._write() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS device_onboarding_devices (
                    device_id TEXT PRIMARY KEY,
                    certificate_id TEXT NOT NULL UNIQUE,
                    public_key_b64 TEXT NOT NULL,
                    product_model TEXT NOT NULL,
                    hardware_revision TEXT NOT NULL,
                    firmware_version TEXT NOT NULL,
                    firmware_security_version INTEGER NOT NULL,
                    capability_manifest_hash TEXT NOT NULL,
                    minimum_firmware_security_version INTEGER NOT NULL,
                    lifecycle_status TEXT NOT NULL,
                    last_monotonic_counter INTEGER NOT NULL DEFAULT 0,
                    binding_id TEXT,
                    binding_version INTEGER,
                    actor_id TEXT,
                    activation_version INTEGER NOT NULL DEFAULT 0,
                    last_activation_counter INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS device_onboarding_sessions (
                    onboarding_session_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
                    actor_id TEXT NOT NULL,
                    client_onboarding_id TEXT NOT NULL,
                    qr_nonce_hash TEXT NOT NULL,
                    pop_hash TEXT NOT NULL,
                    mobile_nonce_hash TEXT NOT NULL,
                    protocol_version INTEGER NOT NULL,
                    ble_name TEXT NOT NULL,
                    ble_service_uuid TEXT NOT NULL,
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 1,
                    first_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    proximity_verified_at TEXT,
                    wifi_connected_at TEXT,
                    device_online_at TEXT,
                    cancelled_at TEXT,
                    consumed_at TEXT,
                    failure_code TEXT,
                    UNIQUE (actor_id, client_onboarding_id),
                    UNIQUE (device_id, qr_nonce_hash)
                );

                CREATE TABLE IF NOT EXISTS device_onboarding_events (
                    event_id TEXT PRIMARY KEY,
                    onboarding_session_id TEXT NOT NULL
                        REFERENCES device_onboarding_sessions(onboarding_session_id),
                    device_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    previous_state TEXT,
                    next_state TEXT,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT,
                    reason_code TEXT,
                    payload_redacted TEXT NOT NULL DEFAULT '{}',
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS device_onboarding_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    onboarding_session_id TEXT NOT NULL
                        REFERENCES device_onboarding_sessions(onboarding_session_id),
                    device_id TEXT NOT NULL,
                    nonce_hash TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS device_media_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
                    certificate_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    nonce_hash TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS device_onboarding_claims (
                    claim_id TEXT PRIMARY KEY,
                    onboarding_session_id TEXT NOT NULL UNIQUE
                        REFERENCES device_onboarding_sessions(onboarding_session_id),
                    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
                    actor_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    binding_id TEXT,
                    binding_version INTEGER,
                    committed_at TEXT,
                    released_at TEXT,
                    failure_code TEXT
                );

                CREATE TABLE IF NOT EXISTS device_onboarding_bindings (
                    binding_id TEXT PRIMARY KEY,
                    claim_id TEXT NOT NULL UNIQUE
                        REFERENCES device_onboarding_claims(claim_id),
                    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
                    actor_id TEXT NOT NULL,
                    binding_version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    initialization_json TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    committed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS device_onboarding_activations (
                    activation_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
                    claim_id TEXT NOT NULL REFERENCES device_onboarding_claims(claim_id),
                    binding_id TEXT NOT NULL REFERENCES device_onboarding_bindings(binding_id),
                    binding_version INTEGER NOT NULL,
                    activation_version INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    downloaded_at TEXT,
                    applied_at TEXT,
                    acknowledged_at TEXT,
                    ack_counter INTEGER,
                    ack_json TEXT,
                    UNIQUE (device_id, activation_version)
                );

                CREATE INDEX IF NOT EXISTS idx_device_onboarding_claims_device
                    ON device_onboarding_claims(device_id, status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_device_onboarding_activations_device
                    ON device_onboarding_activations(device_id, activation_version DESC);
                CREATE INDEX IF NOT EXISTS idx_device_media_challenges_device
                    ON device_media_challenges(device_id, expires_at, used_at);
                """
            )
            media_challenge_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(device_media_challenges)"
                ).fetchall()
            }
            if "client_id" not in media_challenge_columns:
                connection.execute(
                    """
                    ALTER TABLE device_media_challenges
                    ADD COLUMN client_id TEXT NOT NULL DEFAULT ''
                    """
                )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._connection

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------
    # Row conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _device(row: sqlite3.Row) -> DeviceRecord:
        raw_key = base64.urlsafe_b64decode(
            str(row["public_key_b64"]) + "=" * (-len(str(row["public_key_b64"])) % 4)
        )
        if b64url_encode(raw_key) != row["public_key_b64"]:
            raise RuntimeError("corrupt device public key")
        return DeviceRecord(
            device_id=str(row["device_id"]),
            certificate_id=str(row["certificate_id"]),
            public_key=raw_key,
            product_model=str(row["product_model"]),
            hardware_revision=str(row["hardware_revision"]),
            firmware_version=str(row["firmware_version"]),
            firmware_security_version=int(row["firmware_security_version"]),
            capability_manifest_hash=str(row["capability_manifest_hash"]),
            minimum_firmware_security_version=int(row["minimum_firmware_security_version"]),
            lifecycle_status=DeviceLifecycle(str(row["lifecycle_status"])),
            last_monotonic_counter=int(row["last_monotonic_counter"]),
            binding_id=str(row["binding_id"]) if row["binding_id"] is not None else None,
            binding_version=(
                int(row["binding_version"]) if row["binding_version"] is not None else None
            ),
            actor_id=str(row["actor_id"]) if row["actor_id"] is not None else None,
            activation_version=int(row["activation_version"]),
            last_activation_counter=int(row["last_activation_counter"]),
        )

    @staticmethod
    def _session(row: sqlite3.Row) -> BootstrapSession:
        return BootstrapSession(
            onboarding_session_id=str(row["onboarding_session_id"]),
            device_id=str(row["device_id"]),
            actor_id=str(row["actor_id"]),
            client_onboarding_id=str(row["client_onboarding_id"]),
            qr_nonce_hash=str(row["qr_nonce_hash"]),
            pop_hash=str(row["pop_hash"]),
            mobile_nonce_hash=str(row["mobile_nonce_hash"]),
            protocol_version=int(row["protocol_version"]),
            ble_name=str(row["ble_name"]),
            ble_service_uuid=str(row["ble_service_uuid"]),
            state=BootstrapState(str(row["state"])),
            state_version=int(row["state_version"]),
            first_seen_at=_parse_timestamp(str(row["first_seen_at"])) or datetime.min.replace(tzinfo=UTC),
            expires_at=_parse_timestamp(str(row["expires_at"])) or datetime.min.replace(tzinfo=UTC),
            proximity_verified_at=_parse_timestamp(row["proximity_verified_at"]),
            wifi_connected_at=_parse_timestamp(row["wifi_connected_at"]),
            device_online_at=_parse_timestamp(row["device_online_at"]),
            cancelled_at=_parse_timestamp(row["cancelled_at"]),
            consumed_at=_parse_timestamp(row["consumed_at"]),
            failure_code=(str(row["failure_code"]) if row["failure_code"] is not None else None),
        )

    @staticmethod
    def _challenge(row: sqlite3.Row) -> DeviceChallenge:
        return DeviceChallenge(
            challenge_id=str(row["challenge_id"]),
            onboarding_session_id=str(row["onboarding_session_id"]),
            device_id=str(row["device_id"]),
            nonce_hash=str(row["nonce_hash"]),
            issued_at=_parse_timestamp(str(row["issued_at"])) or datetime.min.replace(tzinfo=UTC),
            expires_at=_parse_timestamp(str(row["expires_at"])) or datetime.min.replace(tzinfo=UTC),
            used_at=_parse_timestamp(row["used_at"]),
        )

    @staticmethod
    def _media_challenge(row: sqlite3.Row) -> DeviceMediaChallenge:
        return DeviceMediaChallenge(
            challenge_id=str(row["challenge_id"]),
            device_id=str(row["device_id"]),
            certificate_id=str(row["certificate_id"]),
            client_id=str(row["client_id"]),
            nonce_hash=str(row["nonce_hash"]),
            issued_at=_parse_timestamp(str(row["issued_at"]))
            or datetime.min.replace(tzinfo=UTC),
            expires_at=_parse_timestamp(str(row["expires_at"]))
            or datetime.min.replace(tzinfo=UTC),
            used_at=_parse_timestamp(row["used_at"]),
        )

    @staticmethod
    def _claim(row: sqlite3.Row) -> ClaimReservation:
        return ClaimReservation(
            claim_id=str(row["claim_id"]),
            onboarding_session_id=str(row["onboarding_session_id"]),
            device_id=str(row["device_id"]),
            actor_id=str(row["actor_id"]),
            status=ClaimStatus(str(row["status"])),
            idempotency_key=str(row["idempotency_key"]),
            reserved_at=_parse_timestamp(str(row["reserved_at"])) or datetime.min.replace(tzinfo=UTC),
            expires_at=_parse_timestamp(str(row["expires_at"])) or datetime.min.replace(tzinfo=UTC),
            binding_id=str(row["binding_id"]) if row["binding_id"] is not None else None,
            binding_version=(
                int(row["binding_version"]) if row["binding_version"] is not None else None
            ),
            committed_at=_parse_timestamp(row["committed_at"]),
            released_at=_parse_timestamp(row["released_at"]),
        )

    @staticmethod
    def _binding(row: sqlite3.Row) -> BindingRecord:
        return BindingRecord(
            binding_id=str(row["binding_id"]),
            claim_id=str(row["claim_id"]),
            device_id=str(row["device_id"]),
            actor_id=str(row["actor_id"]),
            binding_version=int(row["binding_version"]),
            status=str(row["status"]),
            initialization=BindingInitialization.from_mapping(_mapping(str(row["initialization_json"]))),
            created_at=_parse_timestamp(str(row["created_at"])) or datetime.min.replace(tzinfo=UTC),
            committed_at=_parse_timestamp(row["committed_at"]),
        )

    @staticmethod
    def _activation(row: sqlite3.Row) -> ActivationRecord:
        manifest = _mapping(str(row["manifest_json"]))
        validate_activation_manifest(manifest)
        return ActivationRecord(
            activation_id=str(row["activation_id"]),
            device_id=str(row["device_id"]),
            claim_id=str(row["claim_id"]),
            binding_id=str(row["binding_id"]),
            binding_version=int(row["binding_version"]),
            activation_version=int(row["activation_version"]),
            manifest=manifest,
            manifest_hash=str(row["manifest_hash"]),
            status=ActivationStatus(str(row["status"])),
            issued_at=_parse_timestamp(str(row["issued_at"])) or datetime.min.replace(tzinfo=UTC),
            expires_at=_parse_timestamp(str(row["expires_at"])) or datetime.min.replace(tzinfo=UTC),
            downloaded_at=_parse_timestamp(row["downloaded_at"]),
            applied_at=_parse_timestamp(row["applied_at"]),
            acknowledged_at=_parse_timestamp(row["acknowledged_at"]),
            ack_counter=(int(row["ack_counter"]) if row["ack_counter"] is not None else None),
        )

    # ------------------------------------------------------------------
    # Device identity authority
    # ------------------------------------------------------------------

    def register_manufactured_device(self, device: DeviceRecord) -> DeviceRecord:
        """Register only a public key; a private key is never a store argument."""

        if len(device.public_key) != 32:
            raise InvalidOnboardingRequest("Ed25519 public key is invalid")
        timestamp = _timestamp(datetime.now(UTC))
        from services.device_fleet.bootstrap_domain import b64url_encode

        with self._write() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO device_onboarding_devices (
                        device_id, certificate_id, public_key_b64, product_model,
                        hardware_revision, firmware_version, firmware_security_version,
                        capability_manifest_hash, minimum_firmware_security_version,
                        lifecycle_status, last_monotonic_counter, binding_id,
                        binding_version, actor_id, activation_version,
                        last_activation_counter, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        device.device_id,
                        device.certificate_id,
                        b64url_encode(device.public_key),
                        device.product_model,
                        device.hardware_revision,
                        device.firmware_version,
                        device.firmware_security_version,
                        device.capability_manifest_hash,
                        device.minimum_firmware_security_version,
                        device.lifecycle_status.value,
                        device.last_monotonic_counter,
                        device.binding_id,
                        device.binding_version,
                        device.actor_id,
                        device.activation_version,
                        device.last_activation_counter,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ClaimConflict("device identity already exists") from exc
        return device

    def get_device(self, device_id: str) -> DeviceRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        return self._device(row) if row is not None else None

    def active_claim_for_device(
        self, *, device_id: str, now: datetime
    ) -> ClaimReservation | None:
        with self._read() as connection:
            row = connection.execute(
                f"""
                SELECT * FROM device_onboarding_claims
                WHERE device_id = ? AND status IN ({_in_clause(_DEVICE_ACTIVE_CLAIMS)})
                  AND expires_at > ?
                ORDER BY reserved_at DESC LIMIT 1
                """,
                (device_id, *_DEVICE_ACTIVE_CLAIMS, _timestamp(now_utc(now))),
            ).fetchone()
        return self._claim(row) if row is not None else None

    # ------------------------------------------------------------------
    # Bootstrap sessions and state-versioned events
    # ------------------------------------------------------------------

    def create_session(self, session: BootstrapSession) -> BootstrapSession:
        with self._write() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO device_onboarding_sessions (
                        onboarding_session_id, device_id, actor_id, client_onboarding_id,
                        qr_nonce_hash, pop_hash, mobile_nonce_hash, protocol_version,
                        ble_name, ble_service_uuid, state, state_version, first_seen_at,
                        expires_at, proximity_verified_at, wifi_connected_at,
                        device_online_at, cancelled_at, consumed_at, failure_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session.onboarding_session_id,
                        session.device_id,
                        session.actor_id,
                        session.client_onboarding_id,
                        session.qr_nonce_hash,
                        session.pop_hash,
                        session.mobile_nonce_hash,
                        session.protocol_version,
                        session.ble_name,
                        session.ble_service_uuid,
                        session.state.value,
                        session.state_version,
                        _timestamp(session.first_seen_at),
                        _timestamp(session.expires_at),
                        _timestamp(session.proximity_verified_at)
                        if session.proximity_verified_at
                        else None,
                        _timestamp(session.wifi_connected_at) if session.wifi_connected_at else None,
                        _timestamp(session.device_online_at) if session.device_online_at else None,
                        _timestamp(session.cancelled_at) if session.cancelled_at else None,
                        _timestamp(session.consumed_at) if session.consumed_at else None,
                        session.failure_code,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ClaimConflict("onboarding session identity already exists") from exc
            self._event(
                connection,
                session=session,
                event_type="session_created",
                previous_state=None,
                next_state=session.state,
                actor_type="user",
                actor_id=session.actor_id,
                reason_code=None,
            )
        return session

    def get_session(self, onboarding_session_id: str) -> BootstrapSession | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (onboarding_session_id,),
            ).fetchone()
        return self._session(row) if row is not None else None

    def find_session_by_client(
        self, *, actor_id: str, client_onboarding_id: str
    ) -> BootstrapSession | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_onboarding_sessions
                WHERE actor_id = ? AND client_onboarding_id = ?
                """,
                (actor_id, client_onboarding_id),
            ).fetchone()
        return self._session(row) if row is not None else None

    def transition_session(
        self,
        onboarding_session_id: str,
        *,
        expected_state_version: int,
        target: BootstrapState,
        actor_type: str,
        actor_id: str | None,
        event_type: str,
        reason_code: str | None = None,
        fields: Mapping[str, object] | None = None,
    ) -> BootstrapSession:
        allowed_fields = {
            "proximity_verified_at",
            "wifi_connected_at",
            "device_online_at",
            "cancelled_at",
            "consumed_at",
            "failure_code",
        }
        updates = dict(fields or {})
        if not set(updates).issubset(allowed_fields):
            raise InvalidOnboardingRequest("unsupported session update field")
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (onboarding_session_id,),
            ).fetchone()
            if row is None:
                raise SessionNotFound()
            current = self._session(row)
            if current.state_version != expected_state_version:
                raise StateVersionConflict("onboarding state version changed")
            require_transition(current.state, target)
            assignments = ["state = ?", "state_version = state_version + 1"]
            values: list[object] = [target.value]
            for field, value in updates.items():
                assignments.append(f"{field} = ?")
                if isinstance(value, datetime):
                    values.append(_timestamp(value))
                else:
                    values.append(value)
            values.extend([onboarding_session_id, expected_state_version])
            connection.execute(
                f"""
                UPDATE device_onboarding_sessions
                SET {", ".join(assignments)}
                WHERE onboarding_session_id = ? AND state_version = ?
                """,
                tuple(values),
            )
            updated_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (onboarding_session_id,),
            ).fetchone()
            if updated_row is None:
                raise SessionNotFound()
            updated = self._session(updated_row)
            self._event(
                connection,
                session=updated,
                event_type=event_type,
                previous_state=current.state,
                next_state=target,
                actor_type=actor_type,
                actor_id=actor_id,
                reason_code=reason_code,
            )
        return updated

    def _event(
        self,
        connection: sqlite3.Connection,
        *,
        session: BootstrapSession,
        event_type: str,
        previous_state: BootstrapState | None,
        next_state: BootstrapState | None,
        actor_type: str,
        actor_id: str | None,
        reason_code: str | None,
    ) -> None:
        import uuid

        connection.execute(
            """
            INSERT INTO device_onboarding_events (
                event_id, onboarding_session_id, device_id, event_type,
                previous_state, next_state, actor_type, actor_id, reason_code,
                payload_redacted, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                f"evt_{uuid.uuid4().hex}",
                session.onboarding_session_id,
                session.device_id,
                event_type,
                previous_state.value if previous_state else None,
                next_state.value if next_state else None,
                actor_type,
                actor_id,
                reason_code,
                _timestamp(datetime.now(UTC)),
            ),
        )

    def expire_session(self, onboarding_session_id: str, *, now: datetime) -> BootstrapSession:
        session = self.get_session(onboarding_session_id)
        if session is None:
            raise SessionNotFound()
        if session.state in {
            BootstrapState.EXPIRED,
            BootstrapState.CANCELLED,
            BootstrapState.ACTIVATED,
        }:
            return session
        return self.transition_session(
            onboarding_session_id,
            expected_state_version=session.state_version,
            target=BootstrapState.EXPIRED,
            actor_type="system",
            actor_id=None,
            event_type="session_expired",
            reason_code="ttl",
            fields={"failure_code": "QR_SESSION_EXPIRED"},
        )

    # ------------------------------------------------------------------
    # Challenge and device proof transaction
    # ------------------------------------------------------------------

    def issue_challenge(self, challenge: DeviceChallenge) -> DeviceChallenge:
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO device_onboarding_challenges (
                    challenge_id, onboarding_session_id, device_id, nonce_hash,
                    issued_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    challenge.challenge_id,
                    challenge.onboarding_session_id,
                    challenge.device_id,
                    challenge.nonce_hash,
                    _timestamp(challenge.issued_at),
                    _timestamp(challenge.expires_at),
                ),
            )
        return challenge

    def get_challenge(self, challenge_id: str) -> DeviceChallenge | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        return self._challenge(row) if row is not None else None

    def issue_media_challenge(
        self,
        challenge: DeviceMediaChallenge,
        *,
        max_outstanding: int = 3,
    ) -> DeviceMediaChallenge:
        current_time = now_utc(challenge.issued_at)
        with self._write() as connection:
            connection.execute(
                """
                DELETE FROM device_media_challenges
                WHERE device_id = ? AND (expires_at <= ? OR used_at IS NOT NULL)
                """,
                (challenge.device_id, _timestamp(current_time)),
            )
            outstanding = connection.execute(
                """
                SELECT COUNT(*) AS count FROM device_media_challenges
                WHERE device_id = ? AND used_at IS NULL AND expires_at > ?
                """,
                (challenge.device_id, _timestamp(current_time)),
            ).fetchone()
            if outstanding is not None and int(outstanding["count"]) >= max_outstanding:
                raise DeviceMediaChallengeRateLimited()
            connection.execute(
                """
                INSERT INTO device_media_challenges (
                    challenge_id, device_id, certificate_id, client_id, nonce_hash,
                    issued_at, expires_at, used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    challenge.challenge_id,
                    challenge.device_id,
                    challenge.certificate_id,
                    challenge.client_id,
                    challenge.nonce_hash,
                    _timestamp(challenge.issued_at),
                    _timestamp(challenge.expires_at),
                ),
            )
        return challenge

    def get_media_challenge(self, challenge_id: str) -> DeviceMediaChallenge | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_media_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        return self._media_challenge(row) if row is not None else None

    def consume_media_challenge(
        self,
        *,
        challenge_id: str,
        device_id: str,
        nonce_hash: str,
        now: datetime,
    ) -> DeviceMediaChallenge:
        current_time = now_utc(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM device_media_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if row is None:
                raise ChallengeReplay("device media challenge was not found")
            challenge = self._media_challenge(row)
            if challenge.device_id != device_id or challenge.nonce_hash != nonce_hash:
                raise InvalidOnboardingRequest("device media challenge does not match")
            if challenge.used_at is not None:
                raise ChallengeReplay("device media challenge was already consumed")
            if challenge.expires_at <= current_time:
                raise DeviceMediaChallengeExpired()
            updated = connection.execute(
                """
                UPDATE device_media_challenges SET used_at = ?
                WHERE challenge_id = ? AND used_at IS NULL AND expires_at > ?
                """,
                (
                    _timestamp(current_time),
                    challenge_id,
                    _timestamp(current_time),
                ),
            )
            if updated.rowcount != 1:
                raise ChallengeReplay("device media challenge was already consumed")
            accepted = connection.execute(
                "SELECT * FROM device_media_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if accepted is None:
                raise ChallengeReplay("device media challenge was not found")
        return self._media_challenge(accepted)

    def accept_online_proof(
        self,
        *,
        challenge_id: str,
        onboarding_session_id: str,
        device_id: str,
        monotonic_counter: int,
        firmware_version: str,
        firmware_security_version: int,
        now: datetime,
    ) -> BootstrapSession:
        current_time = now_utc(now)
        with self._write() as connection:
            challenge_row = connection.execute(
                "SELECT * FROM device_onboarding_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if challenge_row is None:
                raise ChallengeReplay("device challenge not found")
            challenge = self._challenge(challenge_row)
            if challenge.onboarding_session_id != onboarding_session_id:
                raise ChallengeReplay("device challenge does not belong to session")
            if challenge.device_id != device_id:
                raise ChallengeReplay("device challenge does not belong to device")
            if challenge.used_at is not None:
                raise ChallengeReplay("device challenge was already consumed")
            if challenge.expires_at <= current_time:
                raise SessionExpired("device challenge expired")
            session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (onboarding_session_id,),
            ).fetchone()
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.expires_at <= current_time:
                raise SessionExpired()
            device_row = connection.execute(
                "SELECT * FROM device_onboarding_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            if device.lifecycle_status is DeviceLifecycle.REVOKED:
                raise DeviceRevoked()
            if device.lifecycle_status is DeviceLifecycle.BOUND:
                raise DeviceAlreadyBound()
            if monotonic_counter <= device.last_monotonic_counter:
                raise ClaimConflict("device monotonic counter is not increasing")
            timestamp = _timestamp(current_time)
            connection.execute(
                "UPDATE device_onboarding_challenges SET used_at = ? WHERE challenge_id = ?",
                (timestamp, challenge_id),
            )
            connection.execute(
                """
                UPDATE device_onboarding_devices
                SET lifecycle_status = ?, last_monotonic_counter = ?,
                    firmware_version = ?, firmware_security_version = ?, updated_at = ?
                WHERE device_id = ?
                """,
                (
                    DeviceLifecycle.PROVISIONED.value,
                    monotonic_counter,
                    firmware_version,
                    firmware_security_version,
                    timestamp,
                    device_id,
                ),
            )
            target = BootstrapState.DEVICE_ONLINE
            require_transition(session.state, target)
            connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = ?, state_version = state_version + 1,
                    proximity_verified_at = COALESCE(proximity_verified_at, ?),
                    wifi_connected_at = COALESCE(wifi_connected_at, ?),
                    device_online_at = ?
                WHERE onboarding_session_id = ? AND state_version = ?
                """,
                (
                    target.value,
                    timestamp,
                    timestamp,
                    timestamp,
                    onboarding_session_id,
                    session.state_version,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (onboarding_session_id,),
            ).fetchone()
            if updated_row is None:
                raise SessionNotFound()
            updated = self._session(updated_row)
            self._event(
                connection,
                session=updated,
                event_type="device_online_proof_accepted",
                previous_state=session.state,
                next_state=target,
                actor_type="device",
                actor_id=device_id,
                reason_code=None,
            )
        return updated

    # ------------------------------------------------------------------
    # Claim reservation
    # ------------------------------------------------------------------

    def reserve_claim(
        self,
        claim: ClaimReservation,
        *,
        expected_state_version: int,
        now: datetime,
    ) -> ClaimReservation:
        current_time = now_utc(now)
        with self._write() as connection:
            session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.state_version != expected_state_version:
                raise StateVersionConflict("onboarding state version changed")
            if session.device_id != claim.device_id or session.actor_id != claim.actor_id:
                raise ActorMismatch()
            if session.expires_at <= current_time:
                raise SessionExpired()
            if not session.proximity_verified_at or not session.device_online_at:
                raise ClaimConflict("device online proof and proximity are required")
            existing_session_row = connection.execute(
                """
                SELECT * FROM device_onboarding_claims
                WHERE onboarding_session_id = ?
                """,
                (claim.onboarding_session_id,),
            ).fetchone()
            if existing_session_row is not None:
                existing = self._claim(existing_session_row)
                if existing.actor_id != claim.actor_id:
                    raise ClaimConflict("onboarding session belongs to another actor")
                if existing.idempotency_key == claim.idempotency_key:
                    return existing
                if existing.status is ClaimStatus.COMMITTED:
                    return existing
                raise ClaimConflict("claim idempotency key conflicts")
            active_rows = connection.execute(
                f"""
                SELECT * FROM device_onboarding_claims
                WHERE device_id = ? AND status IN ({_in_clause(_DEVICE_ACTIVE_CLAIMS)})
                  AND expires_at > ?
                ORDER BY reserved_at DESC
                """,
                (claim.device_id, *_DEVICE_ACTIVE_CLAIMS, _timestamp(current_time)),
            ).fetchall()
            if active_rows:
                active = self._claim(active_rows[0])
                if active.actor_id != claim.actor_id:
                    raise ClaimConflict("device is being claimed by another actor")
                raise ClaimConflict("device already has an active claim")
            device_row = connection.execute(
                "SELECT * FROM device_onboarding_devices WHERE device_id = ?",
                (claim.device_id,),
            ).fetchone()
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            if device.lifecycle_status is DeviceLifecycle.REVOKED:
                raise DeviceRevoked()
            if device.lifecycle_status is DeviceLifecycle.BOUND:
                raise DeviceAlreadyBound()
            try:
                connection.execute(
                    """
                    INSERT INTO device_onboarding_claims (
                        claim_id, onboarding_session_id, device_id, actor_id, status,
                        idempotency_key, reserved_at, expires_at, binding_id,
                        binding_version, committed_at, released_at, failure_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
                    """,
                    (
                        claim.claim_id,
                        claim.onboarding_session_id,
                        claim.device_id,
                        claim.actor_id,
                        claim.status.value,
                        claim.idempotency_key,
                        _timestamp(claim.reserved_at),
                        _timestamp(claim.expires_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ClaimConflict("device claim already exists") from exc
            require_transition(session.state, BootstrapState.CLAIM_RESERVED)
            connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = ?, state_version = state_version + 1
                WHERE onboarding_session_id = ? AND state_version = ?
                """,
                (
                    BootstrapState.CLAIM_RESERVED.value,
                    claim.onboarding_session_id,
                    expected_state_version,
                ),
            )
            updated_session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if updated_session_row is None:
                raise SessionNotFound()
            self._event(
                connection,
                session=self._session(updated_session_row),
                event_type="claim_reserved",
                previous_state=session.state,
                next_state=BootstrapState.CLAIM_RESERVED,
                actor_type="user",
                actor_id=claim.actor_id,
                reason_code=None,
            )
        return claim

    def get_claim(self, claim_id: str) -> ClaimReservation | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
        return self._claim(row) if row is not None else None

    def get_claim_for_session(self, onboarding_session_id: str) -> ClaimReservation | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_onboarding_claims
                WHERE onboarding_session_id = ?
                """,
                (onboarding_session_id,),
            ).fetchone()
        return self._claim(row) if row is not None else None

    def expire_claim_if_needed(self, claim_id: str, *, now: datetime) -> ClaimReservation:
        current_time = now_utc(now)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if row is None:
                raise ClaimNotFound()
            claim = self._claim(row)
            if claim.status in {ClaimStatus.COMMITTED, ClaimStatus.RELEASED, ClaimStatus.EXPIRED}:
                return claim
            if claim.expires_at > current_time:
                return claim
            connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = ?, released_at = ?, failure_code = ?
                WHERE claim_id = ? AND status IN (?, ?)
                """,
                (
                    ClaimStatus.EXPIRED.value,
                    _timestamp(current_time),
                    "CLAIM_EXPIRED",
                    claim_id,
                    ClaimStatus.RESERVED.value,
                    ClaimStatus.BINDING_COMMITTING.value,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if updated is None:
                raise ClaimNotFound()
        return self._claim(updated)

    # ------------------------------------------------------------------
    # Binding Saga operations
    # ------------------------------------------------------------------

    def begin_binding(
        self,
        *,
        binding: BindingRecord,
        idempotency_key: str,
        expected_state_version: int,
        now: datetime,
    ) -> BindingRecord:
        current_time = now_utc(now)
        with self._write() as connection:
            claim_row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (binding.claim_id,),
            ).fetchone()
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != binding.actor_id:
                raise ActorMismatch()
            if claim.expires_at <= current_time:
                raise ClaimExpired()
            if claim.status is ClaimStatus.COMMITTED:
                existing = connection.execute(
                    "SELECT * FROM device_onboarding_bindings WHERE claim_id = ?",
                    (binding.claim_id,),
                ).fetchone()
                if existing is None:
                    raise BindingConflict("committed claim is missing binding")
                return self._binding(existing)
            if claim.status is ClaimStatus.BINDING_COMMITTING:
                existing = connection.execute(
                    "SELECT * FROM device_onboarding_bindings WHERE claim_id = ?",
                    (binding.claim_id,),
                ).fetchone()
                if existing is not None:
                    current = self._binding(existing)
                    if current.initialization != binding.initialization:
                        raise BindingConflict("binding initialization conflicts")
                    return current
                raise BindingConflict("binding transaction is already in progress")
            if claim.status is not ClaimStatus.RESERVED:
                raise BindingConflict("claim is not reservable")
            session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.state_version != expected_state_version:
                raise StateVersionConflict("onboarding state version changed")
            require_transition(session.state, BootstrapState.BINDING_COMMITTING)
            connection.execute(
                """
                INSERT INTO device_onboarding_bindings (
                    binding_id, claim_id, device_id, actor_id, binding_version,
                    status, initialization_json, idempotency_key, created_at, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    binding.binding_id,
                    binding.claim_id,
                    binding.device_id,
                    binding.actor_id,
                    binding.binding_version,
                    binding.status,
                    _json(binding.initialization.to_dict()),
                    idempotency_key,
                    _timestamp(binding.created_at),
                ),
            )
            connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = ?, binding_id = ?, binding_version = ?
                WHERE claim_id = ? AND status = ?
                """,
                (
                    ClaimStatus.BINDING_COMMITTING.value,
                    binding.binding_id,
                    binding.binding_version,
                    binding.claim_id,
                    ClaimStatus.RESERVED.value,
                ),
            )
            connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = ?, state_version = state_version + 1
                WHERE onboarding_session_id = ? AND state_version = ?
                """,
                (
                    BootstrapState.BINDING_COMMITTING.value,
                    claim.onboarding_session_id,
                    expected_state_version,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM device_onboarding_bindings WHERE binding_id = ?",
                (binding.binding_id,),
            ).fetchone()
            updated_session = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if updated is None or updated_session is None:
                raise BindingConflict("binding transaction could not be created")
            self._event(
                connection,
                session=self._session(updated_session),
                event_type="binding_begin",
                previous_state=session.state,
                next_state=BootstrapState.BINDING_COMMITTING,
                actor_type="user",
                actor_id=binding.actor_id,
                reason_code=None,
            )
        return self._binding(updated)

    def get_binding(self, binding_id: str) -> BindingRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        return self._binding(row) if row is not None else None

    def get_binding_for_claim(self, claim_id: str) -> BindingRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
        return self._binding(row) if row is not None else None

    def adopt_binding_authority(
        self,
        *,
        claim_id: str,
        actor_id: str,
        binding_id: str,
        binding_version: int,
    ) -> BindingRecord:
        """Replace a Fleet-local draft id with Identity's authoritative id.

        Identity creates its immutable binding asynchronously and owns the
        public id/version.  Before Activation exists, the local Fleet draft
        may safely adopt that identity in one SQLite transaction.  Replays
        with the same authority are idempotent; a different result conflicts.
        """

        with self._write() as connection:
            claim_row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != actor_id:
                raise ActorMismatch()
            if claim.status is ClaimStatus.COMMITTED:
                existing = connection.execute(
                    "SELECT * FROM device_onboarding_bindings WHERE claim_id = ?",
                    (claim_id,),
                ).fetchone()
                if existing is None:
                    raise BindingConflict("committed claim is missing binding")
                adopted = self._binding(existing)
                if (
                    adopted.binding_id != binding_id
                    or adopted.binding_version != binding_version
                ):
                    raise BindingConflict("binding authority result conflicts")
                return adopted
            if claim.status is not ClaimStatus.BINDING_COMMITTING:
                raise BindingConflict("binding is not waiting for authority")
            binding_row = connection.execute(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if binding_row is None:
                raise BindingConflict("binding begin is required")
            binding = self._binding(binding_row)
            if binding.status != "draft":
                raise BindingConflict("binding draft cannot adopt authority")
            if (
                binding.binding_id == binding_id
                and binding.binding_version == binding_version
            ):
                return binding
            try:
                connection.execute(
                    """
                    UPDATE device_onboarding_bindings
                    SET binding_id = ?, binding_version = ?
                    WHERE claim_id = ? AND status = 'draft'
                    """,
                    (binding_id, binding_version, claim_id),
                )
            except sqlite3.IntegrityError as exc:
                raise BindingConflict("binding authority id already exists") from exc
            connection.execute(
                """
                UPDATE device_onboarding_claims
                SET binding_id = ?, binding_version = ?
                WHERE claim_id = ? AND status = ?
                """,
                (
                    binding_id,
                    binding_version,
                    claim_id,
                    ClaimStatus.BINDING_COMMITTING.value,
                ),
            )
            adopted_row = connection.execute(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if adopted_row is None:
                raise BindingConflict("binding authority adoption failed")
        return self._binding(adopted_row)

    def release_binding(
        self,
        *,
        claim_id: str,
        actor_id: str,
        reason_code: str,
        now: datetime,
    ) -> ClaimReservation:
        current_time = now_utc(now)
        with self._write() as connection:
            claim_row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != actor_id:
                raise ActorMismatch()
            if claim.status is ClaimStatus.COMMITTED:
                raise BindingConflict("committed binding cannot be released")
            if claim.status in {ClaimStatus.RELEASED, ClaimStatus.EXPIRED}:
                return claim
            connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = ?, released_at = ?, failure_code = ?
                WHERE claim_id = ?
                """,
                (
                    ClaimStatus.RELEASED.value,
                    _timestamp(current_time),
                    reason_code,
                    claim_id,
                ),
            )
            connection.execute(
                "UPDATE device_onboarding_bindings SET status = ? WHERE claim_id = ?",
                ("released", claim_id),
            )
            session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            if session.state not in _TERMINAL_SESSION_STATES and session.state is not BootstrapState.DEVICE_ONLINE:
                require_transition(session.state, BootstrapState.DEVICE_ONLINE)
                connection.execute(
                    """
                    UPDATE device_onboarding_sessions
                    SET state = ?, state_version = state_version + 1
                    WHERE onboarding_session_id = ?
                    """,
                    (BootstrapState.DEVICE_ONLINE.value, claim.onboarding_session_id),
                )
                session_row = connection.execute(
                    "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                    (claim.onboarding_session_id,),
                ).fetchone()
                if session_row is None:
                    raise SessionNotFound()
                self._event(
                    connection,
                    session=self._session(session_row),
                    event_type="binding_released",
                    previous_state=session.state,
                    next_state=BootstrapState.DEVICE_ONLINE,
                    actor_type="user",
                    actor_id=actor_id,
                    reason_code=reason_code,
                )
            updated = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if updated is None:
                raise ClaimNotFound()
        return self._claim(updated)

    def commit_binding(
        self,
        *,
        claim_id: str,
        actor_id: str,
        binding_id: str,
        binding_version: int,
        manifest: Mapping[str, object],
        manifest_hash: str,
        activation_id: str,
        activation_version: int,
        activation_expires_at: datetime,
        now: datetime,
    ) -> tuple[BindingRecord, ActivationRecord]:
        current_time = now_utc(now)
        validate_activation_manifest(manifest)
        with self._write() as connection:
            claim_row = connection.execute(
                "SELECT * FROM device_onboarding_claims WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if claim_row is None:
                raise ClaimNotFound()
            claim = self._claim(claim_row)
            if claim.actor_id != actor_id:
                raise ActorMismatch()
            binding_row = connection.execute(
                "SELECT * FROM device_onboarding_bindings WHERE claim_id = ?",
                (claim_id,),
            ).fetchone()
            if binding_row is None:
                raise BindingConflict("binding begin is required")
            binding = self._binding(binding_row)
            if binding.binding_id != binding_id or binding.binding_version != binding_version:
                raise BindingConflict("binding authority result does not match reservation")
            existing_activation_row = connection.execute(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = ? AND activation_version = ?
                """,
                (claim.device_id, activation_version),
            ).fetchone()
            if claim.status is ClaimStatus.COMMITTED:
                if existing_activation_row is None:
                    raise BindingConflict("committed claim is missing activation")
                return binding, self._activation(existing_activation_row)
            if claim.status is not ClaimStatus.BINDING_COMMITTING:
                raise BindingConflict("claim is not in binding commit state")
            if binding.status == "committed":
                if existing_activation_row is None:
                    raise BindingConflict("committed binding is missing activation")
                return binding, self._activation(existing_activation_row)
            device_row = connection.execute(
                "SELECT * FROM device_onboarding_devices WHERE device_id = ?",
                (claim.device_id,),
            ).fetchone()
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            if device.lifecycle_status is DeviceLifecycle.REVOKED:
                raise DeviceRevoked()
            if device.lifecycle_status is DeviceLifecycle.BOUND:
                raise DeviceAlreadyBound()
            if activation_version != device.activation_version + 1:
                raise BindingConflict("activation version is not monotonic")
            session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if session_row is None:
                raise SessionNotFound()
            session = self._session(session_row)
            require_transition(session.state, BootstrapState.BOUND)
            timestamp = _timestamp(current_time)
            connection.execute(
                """
                UPDATE device_onboarding_bindings
                SET status = ?, committed_at = ?
                WHERE binding_id = ? AND status = 'draft'
                """,
                ("committed", timestamp, binding_id),
            )
            connection.execute(
                """
                UPDATE device_onboarding_claims
                SET status = ?, binding_id = ?, binding_version = ?, committed_at = ?
                WHERE claim_id = ? AND status = ?
                """,
                (
                    ClaimStatus.COMMITTED.value,
                    binding_id,
                    binding_version,
                    timestamp,
                    claim_id,
                    ClaimStatus.BINDING_COMMITTING.value,
                ),
            )
            connection.execute(
                """
                UPDATE device_onboarding_devices
                SET lifecycle_status = ?, binding_id = ?, binding_version = ?,
                    actor_id = ?, activation_version = ?, updated_at = ?
                WHERE device_id = ? AND lifecycle_status IN (?, ?)
                """,
                (
                    DeviceLifecycle.BOUND.value,
                    binding_id,
                    binding_version,
                    actor_id,
                    activation_version,
                    timestamp,
                    claim.device_id,
                    DeviceLifecycle.MANUFACTURED.value,
                    DeviceLifecycle.PROVISIONED.value,
                ),
            )
            connection.execute(
                """
                UPDATE device_onboarding_sessions
                SET state = ?, state_version = state_version + 1, consumed_at = ?
                WHERE onboarding_session_id = ?
                """,
                (BootstrapState.BOUND.value, timestamp, claim.onboarding_session_id),
            )
            connection.execute(
                """
                INSERT INTO device_onboarding_activations (
                    activation_id, device_id, claim_id, binding_id, binding_version,
                    activation_version, manifest_json, manifest_hash, status, issued_at,
                    expires_at, downloaded_at, applied_at, acknowledged_at, ack_counter,
                    ack_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL)
                """,
                (
                    activation_id,
                    claim.device_id,
                    claim_id,
                    binding_id,
                    binding_version,
                    activation_version,
                    _json(manifest),
                    manifest_hash,
                    ActivationStatus.MANIFEST_READY.value,
                    timestamp,
                    _timestamp(activation_expires_at),
                ),
            )
            updated_binding_row = connection.execute(
                "SELECT * FROM device_onboarding_bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
            updated_activation_row = connection.execute(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
            updated_session_row = connection.execute(
                "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                (claim.onboarding_session_id,),
            ).fetchone()
            if (
                updated_binding_row is None
                or updated_activation_row is None
                or updated_session_row is None
            ):
                raise BindingConflict("binding commit did not persist")
            self._event(
                connection,
                session=self._session(updated_session_row),
                event_type="binding_committed",
                previous_state=session.state,
                next_state=BootstrapState.BOUND,
                actor_type="user",
                actor_id=actor_id,
                reason_code=None,
            )
        return self._binding(updated_binding_row), self._activation(updated_activation_row)

    # ------------------------------------------------------------------
    # Activation and device ACK
    # ------------------------------------------------------------------

    def get_activation(self, activation_id: str) -> ActivationRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = ?",
                (activation_id,),
            ).fetchone()
        return self._activation(row) if row is not None else None

    def latest_activation_for_device(self, device_id: str) -> ActivationRecord | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = ? ORDER BY activation_version DESC LIMIT 1
                """,
                (device_id,),
            ).fetchone()
        return self._activation(row) if row is not None else None

    def mark_activation_downloaded(
        self, *, device_id: str, now: datetime
    ) -> ActivationRecord:
        current_time = now_utc(now)
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = ? ORDER BY activation_version DESC LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            if row is None:
                raise ActivationNotFound()
            activation = self._activation(row)
            if activation.status is ActivationStatus.MANIFEST_READY:
                connection.execute(
                    """
                    UPDATE device_onboarding_activations
                    SET status = ?, downloaded_at = ? WHERE activation_id = ?
                    """,
                    (
                        ActivationStatus.DEVICE_DOWNLOADING.value,
                        _timestamp(current_time),
                        activation.activation_id,
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = ?",
                (activation.activation_id,),
            ).fetchone()
            if updated is None:
                raise ActivationNotFound()
        return self._activation(updated)

    def accept_activation_ack(
        self,
        *,
        ack_payload: Mapping[str, object],
        now: datetime,
    ) -> ActivationRecord:
        current_time = now_utc(now)
        with self._write() as connection:
            activation_row = connection.execute(
                """
                SELECT * FROM device_onboarding_activations
                WHERE device_id = ? AND activation_version = ?
                """,
                (
                    str(ack_payload["device_id"]),
                    cast(int, ack_payload["activation_version"]),
                ),
            ).fetchone()
            if activation_row is None:
                raise ActivationNotFound()
            activation = self._activation(activation_row)
            if activation.status in {
                ActivationStatus.DEVICE_ACKNOWLEDGED,
                ActivationStatus.READY_FOR_CONVERSATION,
            }:
                stored_ack_raw = activation_row["ack_json"]
                if stored_ack_raw is not None and dict(ack_payload) == _mapping(
                    str(stored_ack_raw)
                ):
                    # The device may lose the HTTP response after the durable
                    # commit. Exact signed-payload retries are idempotent; any
                    # changed counter/timestamp remains a replay.
                    return activation
                raise ChallengeReplay("activation was already acknowledged")
            if activation.expires_at <= current_time:
                raise ClaimExpired("activation manifest expired")
            if (
                str(ack_payload["device_id"]) != activation.device_id
                or str(ack_payload["binding_id"]) != activation.binding_id
                or cast(int, ack_payload["binding_version"]) != activation.binding_version
                or cast(int, ack_payload["activation_version"]) != activation.activation_version
                or str(ack_payload["config_hash"]) != str(activation.manifest["config_hash"])
            ):
                raise BindingConflict("activation ACK does not match manifest")
            device_row = connection.execute(
                "SELECT * FROM device_onboarding_devices WHERE device_id = ?",
                (activation.device_id,),
            ).fetchone()
            if device_row is None:
                raise DeviceNotFound()
            device = self._device(device_row)
            counter = cast(int, ack_payload["monotonic_counter"])
            if counter <= max(device.last_monotonic_counter, device.last_activation_counter):
                raise ChallengeReplay("activation ACK counter was already consumed")
            timestamp = _timestamp(current_time)
            connection.execute(
                """
                UPDATE device_onboarding_activations
                SET status = ?, applied_at = ?, acknowledged_at = ?, ack_counter = ?,
                    ack_json = ?
                WHERE activation_id = ? AND status IN (?, ?)
                """,
                (
                    ActivationStatus.READY_FOR_CONVERSATION.value,
                    str(ack_payload["applied_at"]),
                    timestamp,
                    counter,
                    _json(ack_payload),
                    activation.activation_id,
                    ActivationStatus.MANIFEST_READY.value,
                    ActivationStatus.DEVICE_DOWNLOADING.value,
                ),
            )
            connection.execute(
                """
                UPDATE device_onboarding_devices
                SET last_monotonic_counter = ?, last_activation_counter = ?, updated_at = ?
                WHERE device_id = ?
                """,
                (counter, counter, timestamp, activation.device_id),
            )
            claim_row = connection.execute(
                "SELECT onboarding_session_id FROM device_onboarding_claims WHERE claim_id = ?",
                (activation.claim_id,),
            ).fetchone()
            if claim_row is not None:
                session_row = connection.execute(
                    "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                    (str(claim_row["onboarding_session_id"]),),
                ).fetchone()
                if session_row is not None:
                    session = self._session(session_row)
                    if session.state is BootstrapState.BOUND:
                        require_transition(session.state, BootstrapState.ACTIVATED)
                        connection.execute(
                            """
                            UPDATE device_onboarding_sessions
                            SET state = ?, state_version = state_version + 1
                            WHERE onboarding_session_id = ?
                            """,
                            (BootstrapState.ACTIVATED.value, session.onboarding_session_id),
                        )
                        updated_session_row = connection.execute(
                            "SELECT * FROM device_onboarding_sessions WHERE onboarding_session_id = ?",
                            (session.onboarding_session_id,),
                        ).fetchone()
                        if updated_session_row is not None:
                            self._event(
                                connection,
                                session=self._session(updated_session_row),
                                event_type="activation_ack_accepted",
                                previous_state=session.state,
                                next_state=BootstrapState.ACTIVATED,
                                actor_type="device",
                                actor_id=activation.device_id,
                                reason_code=None,
                            )
            updated = connection.execute(
                "SELECT * FROM device_onboarding_activations WHERE activation_id = ?",
                (activation.activation_id,),
            ).fetchone()
            if updated is None:
                raise ActivationNotFound()
        return self._activation(updated)

    def is_actor_bound_to_device(self, *, actor_id: str, device_id: str) -> bool:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM device_onboarding_devices
                WHERE device_id = ? AND lifecycle_status = ? AND actor_id = ?
                """,
                (device_id, DeviceLifecycle.BOUND.value, actor_id),
            ).fetchone()
        return row is not None


__all__ = ["BootstrapStorePort", "SQLiteBootstrapStore"]
