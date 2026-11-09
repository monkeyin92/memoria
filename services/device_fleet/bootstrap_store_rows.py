"""SQLite row decoders for the in-process device bootstrap store.

``SQLiteBootstrapRowsMixin`` is composed into ``SQLiteBootstrapStore``; the
store keeps the write/read transaction flow and these static decoders stay in
one place.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import UTC, datetime

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
    b64url_encode,
    validate_activation_manifest,
)


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("corrupt onboarding timestamp")
    return parsed.astimezone(UTC)



def _mapping(value: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeError("corrupt onboarding JSON")
    return {str(key): item for key, item in parsed.items()}




class SQLiteBootstrapRowsMixin:
    """Decode bootstrap records from SQLite rows."""

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
            purpose=BootstrapPurpose(str(row["purpose"])),
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
