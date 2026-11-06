"""Row decoders for the device bootstrap PostgreSQL store.

The store and its sync-over-async facade decode the same record shapes. They
live in this mixin module so the store keeps the transaction and authorization
flow and the wire mapping stays in one place.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import cast

import asyncpg

from services.device_fleet.bootstrap_domain import (
    ActivationRecord,
    ActivationStatus,
    BindingInitialization,
    BindingRecord,
    BootstrapPurpose,
    BootstrapSession,
    BootstrapState,
    ClaimReservation,
    ClaimStatus,
    DeviceChallenge,
    DeviceLifecycle,
    DeviceMediaChallenge,
    DeviceRecord,
    b64url_decode,
    now_utc,
    validate_activation_manifest,
)


def _timestamp(value: datetime) -> datetime:
    normalized = now_utc(value)
    return normalized


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, object]:
    parsed: object
    if isinstance(value, str):
        parsed = json.loads(value)
    else:
        parsed = value
    if not isinstance(parsed, Mapping):
        raise RuntimeError("corrupt onboarding JSON object")
    return {str(key): item for key, item in parsed.items()}


def _db_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RuntimeError("corrupt onboarding timestamp")
        return _timestamp(parsed)
    raise RuntimeError("corrupt onboarding timestamp")


def _required_time(value: object) -> datetime:
    parsed = _db_time(value)
    if parsed is None:
        raise RuntimeError("corrupt onboarding timestamp")
    return parsed


def _row_value(row: asyncpg.Record, key: str) -> object:
    return row[key]


def _int_value(row: asyncpg.Record, key: str) -> int:
    return int(cast(int, _row_value(row, key)))


def _optional_text(row: asyncpg.Record, key: str) -> str | None:
    value = _row_value(row, key)
    return str(value) if value is not None else None


class BootstrapRowsMixin:
    """Decode bootstrap records from asyncpg rows."""

    @staticmethod
    def _device(row: asyncpg.Record) -> DeviceRecord:
        return DeviceRecord(
            device_id=str(_row_value(row, "device_id")),
            certificate_id=str(_row_value(row, "certificate_id")),
            public_key=b64url_decode(
                str(_row_value(row, "public_key_b64")), field="public_key", exact_length=32
            ),
            product_model=str(_row_value(row, "product_model")),
            hardware_revision=str(_row_value(row, "hardware_revision")),
            firmware_version=str(_row_value(row, "firmware_version")),
            firmware_security_version=_int_value(row, "firmware_security_version"),
            capability_manifest_hash=str(_row_value(row, "capability_manifest_hash")),
            minimum_firmware_security_version=_int_value(
                row, "minimum_firmware_security_version"
            ),
            lifecycle_status=DeviceLifecycle(str(_row_value(row, "lifecycle_status"))),
            last_monotonic_counter=_int_value(row, "last_monotonic_counter"),
            binding_id=_optional_text(row, "binding_id"),
            binding_version=(
                _int_value(row, "binding_version")
                if _row_value(row, "binding_version") is not None
                else None
            ),
            actor_id=_optional_text(row, "actor_id"),
            activation_version=_int_value(row, "activation_version"),
            last_activation_counter=_int_value(row, "last_activation_counter"),
        )

    @staticmethod
    def _session(row: asyncpg.Record) -> BootstrapSession:
        return BootstrapSession(
            onboarding_session_id=str(_row_value(row, "onboarding_session_id")),
            device_id=str(_row_value(row, "device_id")),
            actor_id=str(_row_value(row, "actor_id")),
            client_onboarding_id=str(_row_value(row, "client_onboarding_id")),
            qr_nonce_hash=str(_row_value(row, "qr_nonce_hash")),
            pop_hash=str(_row_value(row, "pop_hash")),
            mobile_nonce_hash=str(_row_value(row, "mobile_nonce_hash")),
            protocol_version=_int_value(row, "protocol_version"),
            ble_name=str(_row_value(row, "ble_name")),
            ble_service_uuid=str(_row_value(row, "ble_service_uuid")),
            state=BootstrapState(str(_row_value(row, "state"))),
            state_version=_int_value(row, "state_version"),
            first_seen_at=_required_time(_row_value(row, "first_seen_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            proximity_verified_at=_db_time(_row_value(row, "proximity_verified_at")),
            wifi_connected_at=_db_time(_row_value(row, "wifi_connected_at")),
            device_online_at=_db_time(_row_value(row, "device_online_at")),
            cancelled_at=_db_time(_row_value(row, "cancelled_at")),
            consumed_at=_db_time(_row_value(row, "consumed_at")),
            failure_code=_optional_text(row, "failure_code"),
            purpose=BootstrapPurpose(str(_row_value(row, "purpose"))),
        )

    @staticmethod
    def _challenge(row: asyncpg.Record) -> DeviceChallenge:
        return DeviceChallenge(
            challenge_id=str(_row_value(row, "challenge_id")),
            onboarding_session_id=str(_row_value(row, "onboarding_session_id")),
            device_id=str(_row_value(row, "device_id")),
            nonce_hash=str(_row_value(row, "nonce_hash")),
            issued_at=_required_time(_row_value(row, "issued_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            used_at=_db_time(_row_value(row, "used_at")),
        )

    @staticmethod
    def _media_challenge(row: asyncpg.Record) -> DeviceMediaChallenge:
        return DeviceMediaChallenge(
            challenge_id=str(_row_value(row, "challenge_id")),
            device_id=str(_row_value(row, "device_id")),
            certificate_id=str(_row_value(row, "certificate_id")),
            client_id=str(_row_value(row, "client_id")),
            nonce_hash=str(_row_value(row, "nonce_hash")),
            issued_at=_required_time(_row_value(row, "issued_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            used_at=_db_time(_row_value(row, "used_at")),
        )

    @staticmethod
    def _claim(row: asyncpg.Record) -> ClaimReservation:
        return ClaimReservation(
            claim_id=str(_row_value(row, "claim_id")),
            onboarding_session_id=str(_row_value(row, "onboarding_session_id")),
            device_id=str(_row_value(row, "device_id")),
            actor_id=str(_row_value(row, "actor_id")),
            status=ClaimStatus(str(_row_value(row, "status"))),
            idempotency_key=str(_row_value(row, "idempotency_key")),
            reserved_at=_required_time(_row_value(row, "reserved_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            binding_id=_optional_text(row, "binding_id"),
            binding_version=(
                _int_value(row, "binding_version")
                if _row_value(row, "binding_version") is not None
                else None
            ),
            committed_at=_db_time(_row_value(row, "committed_at")),
            released_at=_db_time(_row_value(row, "released_at")),
        )

    @staticmethod
    def _binding(row: asyncpg.Record) -> BindingRecord:
        return BindingRecord(
            binding_id=str(_row_value(row, "binding_id")),
            claim_id=str(_row_value(row, "claim_id")),
            device_id=str(_row_value(row, "device_id")),
            actor_id=str(_row_value(row, "actor_id")),
            binding_version=_int_value(row, "binding_version"),
            status=str(_row_value(row, "status")),
            initialization=BindingInitialization.from_mapping(
                _json_object(_row_value(row, "initialization_json"))
            ),
            created_at=_required_time(_row_value(row, "created_at")),
            committed_at=_db_time(_row_value(row, "committed_at")),
        )

    @staticmethod
    def _activation(row: asyncpg.Record) -> ActivationRecord:
        manifest = _json_object(_row_value(row, "manifest_json"))
        validate_activation_manifest(manifest)
        return ActivationRecord(
            activation_id=str(_row_value(row, "activation_id")),
            device_id=str(_row_value(row, "device_id")),
            claim_id=str(_row_value(row, "claim_id")),
            binding_id=str(_row_value(row, "binding_id")),
            binding_version=_int_value(row, "binding_version"),
            activation_version=_int_value(row, "activation_version"),
            manifest=manifest,
            manifest_hash=str(_row_value(row, "manifest_hash")),
            status=ActivationStatus(str(_row_value(row, "status"))),
            issued_at=_required_time(_row_value(row, "issued_at")),
            expires_at=_required_time(_row_value(row, "expires_at")),
            downloaded_at=_db_time(_row_value(row, "downloaded_at")),
            applied_at=_db_time(_row_value(row, "applied_at")),
            acknowledged_at=_db_time(_row_value(row, "acknowledged_at")),
            ack_counter=(
                _int_value(row, "ack_counter")
                if _row_value(row, "ack_counter") is not None
                else None
            ),
        )

    # ------------------------------------------------------------------
