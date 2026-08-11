"""Authoritative Device Fleet lifecycle and signed-attestation module."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast
from urllib.parse import urlsplit

import asyncpg
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceAttestation,
    DeviceCapability,
    DeviceCertificateStatus,
    DeviceLifecycleStatus,
    DeviceTrust,
    OtaAssignment,
    OtaAssignmentStatus,
    OtaBootStatus,
    OtaSlotName,
    OtaSlotState,
    OtaStateReceipt,
    RemoteDeviceCommand,
    RemoteDeviceCommandExecution,
    RemoteDeviceCommandType,
)
from pydantic import ValidationError

from services.device_fleet.authority import (
    CommandAuthorizationRejected,
    DeviceCommandAuthorityPort,
    DeviceCommandAuthorization,
    RejectingDeviceCommandAuthority,
    build_device_command_action,
    build_device_lifecycle_action,
)
from services.device_fleet.crypto import (
    sign_ota_assignment,
    sign_remote_device_command,
    verify_device_attestation_signature,
    verify_ota_assignment_signature,
    verify_ota_state_receipt_signature,
    verify_remote_device_command_signature,
)
from services.device_fleet.domain import (
    AttestationRejected,
    CommandDispatchReadiness,
    CommandReconcileResult,
    DeviceFleetConflict,
    DeviceFleetContext,
    DeviceLifecycleAuthorityFacts,
    DeviceTrustAuthorityFacts,
    OtaRejected,
    RemoteCommandDispatch,
    RemoteCommandRejected,
    RemoteCommandState,
    SimAuthorityFacts,
)
from services.device_fleet.postgres_store import PostgresDeviceFleetStore

T = TypeVar("T")


def _json_value(value: object) -> object:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) != 32:
            raise ValueError
        return Ed25519PublicKey.from_public_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise DeviceFleetConflict("invalid Ed25519 device public key") from exc


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _datetime_value(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise ValueError("expected timestamp")


def _int_value(value: object) -> int:
    """Validate a database scalar before using it as an integer."""

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("expected integer")
    return int(value)


def _canonical_parameters(parameters: Mapping[str, object]) -> tuple[str, str]:
    try:
        encoded = json.dumps(
            dict(parameters),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("remote command parameters must be canonical JSON") from exc
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _allowed_binding_roles(command_type: RemoteDeviceCommandType) -> frozenset[str]:
    if command_type is RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE:
        return frozenset({"account_owner", "device_admin", "guardian"})
    return frozenset({"account_owner", "device_admin"})


def _validate_remote_command_parameters(
    command_type: RemoteDeviceCommandType,
    parameters: Mapping[str, object],
) -> None:
    value = dict(parameters)
    valid = False
    if command_type is RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_FORCE_MUTE:
        valid = value == {"muted": True}
    elif command_type in {
        RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_REVOKE_DEVICE,
        RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_SECURE_WIPE,
    }:
        valid = value == {}
    elif (
        command_type
        is RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_COLLECT_DIAGNOSTICS
    ):
        valid = value == {"scope": "minimal"}
    elif command_type is RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_ASSIGN_OTA:
        valid = (
            set(value)
            == {
                "assignment_id",
                "artifact_url",
                "artifact_sha256",
                "artifact_size_bytes",
            }
            and isinstance(value.get("assignment_id"), str)
            and isinstance(value.get("artifact_url"), str)
            and isinstance(value.get("artifact_sha256"), str)
            and isinstance(value.get("artifact_size_bytes"), int)
            and not isinstance(value.get("artifact_size_bytes"), bool)
        )
    if not valid:
        raise ValueError("remote command parameters do not match the closed schema")


def _version_key(value: str) -> tuple[int, int, int]:
    core = value.removeprefix("v").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if not 1 <= len(parts) <= 3 or any(not part.isdigit() for part in parts):
        raise OtaRejected
    padded = [int(part) for part in parts] + [0] * (3 - len(parts))
    return padded[0], padded[1], padded[2]


def _ota_slot(receipt: OtaStateReceipt, name: OtaSlotName) -> OtaSlotState:
    for slot in receipt.slots:
        if slot.slot is name:
            return slot
    raise OtaRejected


class DeviceFleetService:
    """Deep module for lifecycle, attestation and Policy fact projection."""

    def __init__(
        self,
        store: PostgresDeviceFleetStore,
        *,
        command_signing_key: Ed25519PrivateKey,
        command_signer_key_id: str,
        command_authority: DeviceCommandAuthorityPort | None = None,
    ) -> None:
        if not command_signer_key_id or len(command_signer_key_id) > 128:
            raise ValueError("command_signer_key_id must be a short non-empty string")
        self.store = store
        self._command_signing_key = command_signing_key
        self._command_signer_key_id = command_signer_key_id
        self._command_authority = command_authority or RejectingDeviceCommandAuthority()

    @property
    def command_verification_key(self) -> Ed25519PublicKey:
        """Public command key distributed to provisioned device firmware."""

        return self._command_signing_key.public_key()

    async def provision_device(
        self,
        *,
        device_id: str,
        capability_manifest_hash: str,
        capabilities: Sequence[DeviceCapability],
        firmware_version: str,
        firmware_security_version: int,
        firmware_sha256: str,
        bootloader_version: str,
        anti_rollback_floor_version: str,
        anti_rollback_floor_security_version: int,
        now: datetime | None = None,
    ) -> None:
        self.store.require_role("maintenance")
        timestamp = now or datetime.now(UTC)
        if firmware_security_version < anti_rollback_floor_security_version:
            raise ValueError("firmware security version is below anti-rollback floor")
        if len(capability_manifest_hash) != 64 or len(firmware_sha256) != 64:
            raise ValueError("firmware and capability hashes must be sha256 hex")
        capability_values = [capability.value for capability in capabilities]
        if len(capability_values) != len(set(capability_values)):
            raise ValueError("device capabilities must be unique")
        async with self.store.transaction() as connection:
            try:
                await connection.execute(
                    """
                    INSERT INTO device_fleet_devices (
                        device_id, lifecycle_status, capability_manifest_hash,
                        capabilities, firmware_version, firmware_security_version,
                        firmware_sha256, bootloader_version,
                        anti_rollback_floor_version,
                        anti_rollback_floor_security_version, created_at, updated_at
                    ) VALUES ($1, 'provisioned', $2, $3::jsonb, $4, $5, $6, $7, $8, $9, $10, $10)
                    """,
                    device_id,
                    capability_manifest_hash,
                    json.dumps(capability_values),
                    firmware_version,
                    firmware_security_version,
                    firmware_sha256,
                    bootloader_version,
                    anti_rollback_floor_version,
                    anti_rollback_floor_security_version,
                    timestamp,
                )
            except asyncpg.UniqueViolationError as exc:
                raise DeviceFleetConflict("device already exists") from exc

    async def issue_certificate(
        self,
        *,
        device_id: str,
        certificate_id: str,
        public_key_b64: str,
        valid_until: datetime,
        now: datetime | None = None,
    ) -> None:
        self.store.require_role("maintenance")
        timestamp = now or datetime.now(UTC)
        _public_key(public_key_b64)
        if valid_until <= timestamp:
            raise ValueError("certificate must expire in the future")
        async with self.store.transaction() as connection:
            device = await connection.fetchrow(
                "SELECT device_id, current_certificate_id FROM device_fleet_devices "
                "WHERE device_id = $1 FOR UPDATE",
                device_id,
            )
            if device is None:
                raise DeviceFleetConflict("device does not exist")
            if device["current_certificate_id"] is not None:
                raise DeviceFleetConflict("device already has an active certificate")
            await connection.execute(
                """
                INSERT INTO device_fleet_certificates (
                    certificate_id, device_id, public_key_b64, key_algorithm,
                    status, valid_from, valid_until, created_at
                ) VALUES ($1, $2, $3, 'ed25519', 'active', $4, $5, $4)
                """,
                certificate_id,
                device_id,
                public_key_b64,
                timestamp,
                valid_until,
            )
            await connection.execute(
                "UPDATE device_fleet_devices SET current_certificate_id = $2, "
                "state_version = state_version + 1, updated_at = $3 WHERE device_id = $1",
                device_id,
                certificate_id,
                timestamp,
            )

    async def bind_device(
        self,
        *,
        device_id: str,
        family_space_id: str,
        binding_id: str,
        binding_version: int,
        now: datetime | None = None,
    ) -> DeviceFleetContext:
        self.store.require_role("maintenance")
        timestamp = now or datetime.now(UTC)
        if binding_version < 1:
            raise ValueError("binding_version must be positive")
        context = DeviceFleetContext(device_id, binding_id, binding_version, family_space_id)
        async with self.store.transaction() as connection:
            device = await connection.fetchrow(
                "SELECT lifecycle_status, current_certificate_id, binding_version_floor "
                "FROM device_fleet_devices "
                "WHERE device_id = $1 FOR UPDATE",
                device_id,
            )
            if device is None or device["current_certificate_id"] is None:
                raise DeviceFleetConflict("provisioned device with active certificate is required")
            if device["lifecycle_status"] != "provisioned":
                raise DeviceFleetConflict("device is not bindable")
            if binding_version != int(device["binding_version_floor"]) + 1:
                raise DeviceFleetConflict("binding version conflict")
            await connection.execute(
                """
                UPDATE device_fleet_devices SET family_space_id = $2, binding_id = $3,
                    binding_version = $4, lifecycle_status = 'bound',
                    binding_version_floor = $4,
                    state_version = state_version + 1, updated_at = $5
                WHERE device_id = $1
                """,
                device_id,
                family_space_id,
                binding_id,
                binding_version,
                timestamp,
            )
            await connection.execute(
                """
                UPDATE device_fleet_certificates SET family_space_id = $2,
                    binding_id = $3, binding_version = $4
                WHERE device_id = $1 AND status = 'active'
                """,
                device_id,
                family_space_id,
                binding_id,
                binding_version,
            )
        return context

    async def provision_sim_authority(
        self,
        context: DeviceFleetContext,
        *,
        sim_id: str,
        provider: str,
        profile_kind: str,
        provider_status: str,
        iccid: str | None = None,
        esim_profile_id: str | None = None,
        now: datetime | None = None,
    ) -> SimAuthorityFacts:
        """Bootstrap one server-owned SIM/eSIM profile for a bound device.

        This maintenance-only entry point is for carrier provisioning.  User
        lifecycle changes use the callback-authorized action path instead.
        Device attestations never write this authority.
        """

        self.store.require_role("maintenance")
        timestamp = now or datetime.now(UTC)
        if profile_kind not in {"physical", "esim"}:
            raise ValueError("SIM profile kind must be physical or esim")
        if provider_status not in {
            "unknown",
            "inactive",
            "active",
            "suspended",
            "revoked",
            "expired",
        }:
            raise ValueError("invalid provider SIM status")
        if not sim_id or not provider:
            raise ValueError("SIM id and provider are required")
        if profile_kind == "physical":
            iccid = iccid or sim_id
            esim_profile_id = None
        else:
            esim_profile_id = esim_profile_id or sim_id
            iccid = None
        server_status = provider_status if provider_status != "unknown" else "inactive"
        async with self.store.transaction(context) as connection:
            device = await connection.fetchrow(
                "SELECT * FROM device_fleet_devices WHERE device_id = $1 FOR UPDATE",
                context.device_id,
            )
            if (
                device is None
                or device["lifecycle_status"] != "bound"
                or device["current_sim_id"] is not None
                or int(device["sim_authority_revision"]) != 0
            ):
                raise DeviceFleetConflict("SIM authority already provisioned")
            try:
                await connection.execute(
                    """
                    INSERT INTO device_fleet_sim_profiles (
                        sim_id, device_id, family_space_id, binding_id,
                        binding_version, iccid, esim_profile_id, provider,
                        profile_kind, provider_status, server_status,
                        authority_revision, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                              $11, 1, $12, $12)
                    """,
                    sim_id,
                    context.device_id,
                    context.family_space_id,
                    context.binding_id,
                    context.binding_version,
                    iccid,
                    esim_profile_id,
                    provider,
                    profile_kind,
                    provider_status,
                    server_status,
                    timestamp,
                )
                await connection.execute(
                    """
                    UPDATE device_fleet_devices SET current_sim_id = $2,
                        sim_authority_revision = 1,
                        state_version = state_version + 1, updated_at = $3
                    WHERE device_id = $1
                    """,
                    context.device_id,
                    sim_id,
                    timestamp,
                )
                await connection.execute(
                    """
                    INSERT INTO device_fleet_sim_events (
                        event_id, device_id, family_space_id, binding_id,
                        binding_version, sim_id, authority_revision, action,
                        previous_server_status, server_status, provider_status,
                        actor_id, authority_receipt_id, action_resource_id,
                        action_resource_hash, action_fence_hash, occurred_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, 1, 'provision',
                              NULL, $7, $8, 'maintenance-bootstrap',
                              'maintenance-bootstrap', 'sim-bootstrap', $9, $9, $10)
                    """,
                    f"sim-event-{uuid.uuid4().hex}",
                    context.device_id,
                    context.family_space_id,
                    context.binding_id,
                    context.binding_version,
                    sim_id,
                    server_status,
                    provider_status,
                    "0" * 64,
                    timestamp,
                )
            except asyncpg.IntegrityConstraintViolationError as exc:
                raise DeviceFleetConflict("SIM authority conflict") from exc
        return SimAuthorityFacts(
            sim_id=sim_id,
            provider=provider,
            profile_kind=profile_kind,
            provider_status=provider_status,
            status=server_status,
            revision=1,
            iccid=iccid,
            esim_profile_id=esim_profile_id,
        )

    async def transition_sim_authority(
        self,
        context: DeviceFleetContext,
        *,
        authority_input: object,
        expected_sim_id: str,
        expected_revision: int,
        action: str,
        replacement_sim_id: str | None = None,
        replacement_provider: str | None = None,
        replacement_profile_kind: str | None = None,
        now: datetime | None = None,
    ) -> SimAuthorityFacts:
        """Apply one callback-authorized server SIM lifecycle CAS."""

        self.store.require_role("action_executor")
        timestamp = now or datetime.now(UTC)
        if action not in {"suspend", "revoke", "expire", "replace"}:
            raise ValueError("unsupported SIM lifecycle action")
        if action == "replace" and (
            not replacement_sim_id
            or not replacement_provider
            or replacement_profile_kind not in {"physical", "esim"}
        ):
            raise ValueError("replacement SIM profile is required")
        semantic_parameters = {
            "expected_sim_id": expected_sim_id,
            "expected_revision": expected_revision,
            "action": action,
            "replacement_sim_id": replacement_sim_id,
            "replacement_provider": replacement_provider,
            "replacement_profile_kind": replacement_profile_kind,
        }
        _, parameters_hash = _canonical_parameters(semantic_parameters)
        async with self.store.transaction(context) as connection:
            snapshot_value = await connection.fetchval(
                "SELECT device_fleet_action_lock_snapshot($1, $2, $3, $4, $5, $6)",
                context.device_id,
                context.binding_id,
                context.binding_version,
                context.family_space_id,
                None,
                None,
            )
            snapshot = _json_value(snapshot_value)
            if not isinstance(snapshot, dict):
                raise DeviceFleetConflict("SIM revision conflict")
            device = snapshot.get("device")
            current = snapshot.get("sim")
            if (
                not isinstance(device, dict)
                or not isinstance(current, dict)
                or device.get("current_sim_id") != expected_sim_id
                or int(device.get("sim_authority_revision", 0)) != expected_revision
                or int(current.get("authority_revision", 0)) != expected_revision
            ):
                raise DeviceFleetConflict("SIM revision conflict")
            certificate_id = device.get("current_certificate_id")
            if not isinstance(certificate_id, str):
                raise DeviceFleetConflict("SIM certificate fence missing")
            lifecycle_action = build_device_lifecycle_action(
                device_id=context.device_id,
                certificate_id=certificate_id,
                binding_id=context.binding_id,
                binding_version=context.binding_version,
                monotonic_counter=int(device["last_attestation_counter"]),
                firmware_security_version=int(device["firmware_security_version"]),
                action_type=f"sim.{action}",
                parameters_hash=parameters_hash,
                idempotency_key=(
                    f"sim-{action}-{expected_sim_id}-{expected_revision + 1}"
                ),
                allowed_binding_roles=frozenset({"account_owner", "device_admin"}),
            )

            async def authorized_write(
                active: asyncpg.Connection,
                authorization: DeviceCommandAuthorization,
            ) -> SimAuthorityFacts:
                if authorization.action != lifecycle_action:
                    raise CommandAuthorizationRejected("authorized SIM action mismatch")
                record = {
                    "event_id": f"sim-event-{uuid.uuid4().hex}",
                    "device_id": context.device_id,
                    "family_space_id": context.family_space_id,
                    "binding_id": context.binding_id,
                    "binding_version": context.binding_version,
                    **semantic_parameters,
                    "target_certificate_id": lifecycle_action.certificate_id,
                    "expected_monotonic_counter": (
                        lifecycle_action.monotonic_counter
                    ),
                    "expected_firmware_security_version": (
                        lifecycle_action.firmware_security_version
                    ),
                    "occurred_at": _rfc3339(timestamp),
                }
                authority_record = {
                    "actor_id": authorization.actor_id,
                    "authority_receipt_id": authorization.authority_receipt_id,
                    "action_resource_id": lifecycle_action.action_resource_id,
                    "action_resource_hash": lifecycle_action.canonical_hash,
                    "action_fence_hash": authorization.action_fence_hash,
                }
                try:
                    committed = await active.fetchval(
                        "SELECT device_fleet_action_transition_sim("
                        "$1::jsonb, $2::jsonb)",
                        json.dumps(record, separators=(",", ":")),
                        json.dumps(authority_record, separators=(",", ":")),
                    )
                except asyncpg.PostgresError as exc:
                    raise DeviceFleetConflict("SIM revision conflict") from exc
                committed_value = _json_value(committed)
                if not isinstance(committed_value, dict):
                    raise DeviceFleetConflict("SIM lifecycle commit failed")
                return SimAuthorityFacts(
                    sim_id=str(committed_value["sim_id"]),
                    provider=str(committed_value["provider"]),
                    profile_kind=str(committed_value["profile_kind"]),
                    provider_status=str(committed_value["provider_status"]),
                    status=str(committed_value["status"]),
                    revision=int(committed_value["revision"]),
                    iccid=(
                        str(committed_value["iccid"])
                        if committed_value.get("iccid") is not None
                        else None
                    ),
                    esim_profile_id=(
                        str(committed_value["esim_profile_id"])
                        if committed_value.get("esim_profile_id") is not None
                        else None
                    ),
                )

            return await self._command_authority.execute_authorized(
                connection,
                lifecycle_action,
                authority_input,
                authorized_write,
            )

    async def rotate_certificate(
        self,
        *,
        device_id: str,
        certificate_id: str,
        public_key_b64: str,
        valid_until: datetime,
        now: datetime | None = None,
    ) -> None:
        """Atomically revoke the current key and activate a replacement."""

        self.store.require_role("maintenance")
        timestamp = now or datetime.now(UTC)
        _public_key(public_key_b64)
        if valid_until <= timestamp:
            raise ValueError("certificate must expire in the future")
        async with self.store.transaction() as connection:
            device = await connection.fetchrow(
                "SELECT * FROM device_fleet_devices WHERE device_id = $1 FOR UPDATE",
                device_id,
            )
            if (
                device is None
                or device["lifecycle_status"] != "bound"
                or device["current_certificate_id"] is None
            ):
                raise DeviceFleetConflict("bound device with active certificate is required")
            result = await connection.execute(
                """
                UPDATE device_fleet_certificates SET status = 'revoked',
                    revoked_at = $2, revocation_reason_code = 'certificate_rotated'
                WHERE certificate_id = $1 AND status = 'active'
                """,
                device["current_certificate_id"],
                timestamp,
            )
            if result != "UPDATE 1":
                raise DeviceFleetConflict("current certificate is not active")
            try:
                await connection.execute(
                    """
                    INSERT INTO device_fleet_certificates (
                        certificate_id, device_id, family_space_id, binding_id,
                        binding_version, public_key_b64, key_algorithm, status,
                        valid_from, valid_until, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, 'ed25519', 'active', $7, $8, $7)
                    """,
                    certificate_id,
                    device_id,
                    device["family_space_id"],
                    device["binding_id"],
                    device["binding_version"],
                    public_key_b64,
                    timestamp,
                    valid_until,
                )
            except asyncpg.UniqueViolationError as exc:
                raise DeviceFleetConflict("replacement certificate already exists") from exc
            await connection.execute(
                """
                UPDATE device_fleet_devices SET current_certificate_id = $2,
                    state_version = state_version + 1, updated_at = $3
                WHERE device_id = $1
                """,
                device_id,
                certificate_id,
                timestamp,
            )

    async def transfer_device_binding(
        self,
        context: DeviceFleetContext,
        *,
        authority_input: object,
        new_family_space_id: str,
        new_binding_id: str,
        new_binding_version: int,
        new_certificate_id: str,
        new_public_key_b64: str,
        certificate_valid_until: datetime,
        now: datetime | None = None,
    ) -> DeviceFleetContext:
        """Transfer one binding only inside the Policy/Identity callback."""

        self.store.require_role("action_executor")
        timestamp = now or datetime.now(UTC)
        _public_key(new_public_key_b64)
        if certificate_valid_until <= timestamp:
            raise ValueError("replacement certificate must expire in the future")
        if new_binding_version != context.binding_version + 1:
            raise DeviceFleetConflict("binding version conflict")
        new_context = DeviceFleetContext(
            context.device_id,
            new_binding_id,
            new_binding_version,
            new_family_space_id,
        )
        semantic_parameters = {
            "expected_family_space_id": context.family_space_id,
            "expected_binding_id": context.binding_id,
            "expected_binding_version": context.binding_version,
            "new_family_space_id": new_family_space_id,
            "new_binding_id": new_binding_id,
            "new_binding_version": new_binding_version,
            "new_certificate_id": new_certificate_id,
            "new_public_key_sha256": hashlib.sha256(
                new_public_key_b64.encode("ascii")
            ).hexdigest(),
            "certificate_valid_until": _rfc3339(certificate_valid_until),
        }
        _, parameters_hash = _canonical_parameters(semantic_parameters)
        async with self.store.transaction(context) as connection:
            snapshot_value = await connection.fetchval(
                "SELECT device_fleet_action_lock_snapshot($1, $2, $3, $4, $5, $6)",
                context.device_id,
                context.binding_id,
                context.binding_version,
                context.family_space_id,
                None,
                None,
            )
            snapshot = _json_value(snapshot_value)
            if not isinstance(snapshot, dict):
                raise DeviceFleetConflict("binding version conflict")
            device = snapshot.get("device")
            if (
                not isinstance(device, dict)
                or device.get("lifecycle_status") != "bound"
                or device.get("binding_id") != context.binding_id
                or int(device.get("binding_version", 0)) != context.binding_version
                or int(device.get("binding_version_floor", 0))
                != context.binding_version
            ):
                raise DeviceFleetConflict("binding version conflict")
            certificate_id = device.get("current_certificate_id")
            if not isinstance(certificate_id, str):
                raise DeviceFleetConflict("current binding has no certificate")
            action = build_device_lifecycle_action(
                device_id=context.device_id,
                certificate_id=certificate_id,
                binding_id=context.binding_id,
                binding_version=context.binding_version,
                monotonic_counter=int(device["last_attestation_counter"]),
                firmware_security_version=int(device["firmware_security_version"]),
                action_type="binding.transfer",
                parameters_hash=parameters_hash,
                idempotency_key=f"binding-transfer-{new_binding_id}-{new_binding_version}",
                allowed_binding_roles=frozenset({"account_owner"}),
            )

            async def authorized_write(
                active: asyncpg.Connection,
                authorization: DeviceCommandAuthorization,
            ) -> DeviceFleetContext:
                if authorization.action != action:
                    raise CommandAuthorizationRejected(
                        "authorized binding action mismatch"
                    )
                record = {
                    "event_id": f"binding-event-{uuid.uuid4().hex}",
                    "device_id": context.device_id,
                    "expected_family_space_id": context.family_space_id,
                    "expected_binding_id": context.binding_id,
                    "expected_binding_version": context.binding_version,
                    "new_family_space_id": new_family_space_id,
                    "new_binding_id": new_binding_id,
                    "new_binding_version": new_binding_version,
                    "new_certificate_id": new_certificate_id,
                    "new_public_key_b64": new_public_key_b64,
                    "certificate_valid_until": _rfc3339(certificate_valid_until),
                    "target_certificate_id": action.certificate_id,
                    "expected_monotonic_counter": action.monotonic_counter,
                    "expected_firmware_security_version": (
                        action.firmware_security_version
                    ),
                    "occurred_at": _rfc3339(timestamp),
                }
                authority_record = {
                    "actor_id": authorization.actor_id,
                    "authority_receipt_id": authorization.authority_receipt_id,
                    "action_resource_id": action.action_resource_id,
                    "action_resource_hash": action.canonical_hash,
                    "action_fence_hash": authorization.action_fence_hash,
                }
                try:
                    committed = await active.fetchval(
                        "SELECT device_fleet_action_transfer_binding("
                        "$1::jsonb, $2::jsonb)",
                        json.dumps(record, separators=(",", ":")),
                        json.dumps(authority_record, separators=(",", ":")),
                    )
                except asyncpg.PostgresError as exc:
                    raise DeviceFleetConflict("binding version conflict") from exc
                committed_value = _json_value(committed)
                if not isinstance(committed_value, dict):
                    raise DeviceFleetConflict("binding transfer commit failed")
                if committed_value != {
                    "device_id": new_context.device_id,
                    "family_space_id": new_context.family_space_id,
                    "binding_id": new_context.binding_id,
                    "binding_version": new_context.binding_version,
                }:
                    raise DeviceFleetConflict("binding transfer commit mismatch")
                return new_context

            return await self._command_authority.execute_authorized(
                connection,
                action,
                authority_input,
                authorized_write,
            )

    async def emergency_revoke_device(
        self,
        *,
        device_id: str,
        reason_code: str,
        now: datetime | None = None,
    ) -> None:
        """Disaster-operations revocation, outside normal product authorization.

        Normal owner/device-admin revocation is the signed ``revoke_device``
        remote-command flow and becomes authoritative only after a signed
        device ACK.  This maintenance seam exists solely for compromised-key
        containment when the normal action path cannot be used.
        """

        self.store.require_role("maintenance")
        timestamp = now or datetime.now(UTC)
        if not reason_code or len(reason_code) > 128:
            raise ValueError("reason_code must be a short non-empty string")
        async with self.store.transaction() as connection:
            device = await connection.fetchrow(
                "SELECT * FROM device_fleet_devices WHERE device_id = $1 FOR UPDATE",
                device_id,
            )
            if device is None:
                raise DeviceFleetConflict("device does not exist")
            if device["lifecycle_status"] == "revoked":
                return
            if device["current_certificate_id"] is not None:
                await connection.execute(
                    """
                    UPDATE device_fleet_certificates SET status = 'revoked',
                        revoked_at = $2, revocation_reason_code = $3
                    WHERE certificate_id = $1 AND status = 'active'
                    """,
                    device["current_certificate_id"],
                    timestamp,
                    reason_code,
                )
            await connection.execute(
                """
                UPDATE device_fleet_devices SET lifecycle_status = 'revoked',
                    lifecycle_reason_code = $2, state_version = state_version + 1,
                    updated_at = $3 WHERE device_id = $1
                """,
                device_id,
                reason_code,
                timestamp,
            )

    async def issue_attestation_nonce(
        self,
        context: DeviceFleetContext,
        *,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=2),
    ) -> str:
        self.store.require_role("api")
        timestamp = now or datetime.now(UTC)
        if ttl <= timedelta(0) or ttl > timedelta(minutes=5):
            raise ValueError("attestation nonce TTL must be within five minutes")
        nonce = secrets.token_urlsafe(24)
        async with self.store.transaction(context) as connection:
            device = await connection.fetchrow(
                """
                SELECT d.lifecycle_status, d.current_certificate_id,
                       c.status AS certificate_status, c.valid_from, c.valid_until
                FROM device_fleet_devices d
                LEFT JOIN device_fleet_certificates c
                  ON c.certificate_id = d.current_certificate_id
                WHERE d.device_id = $1
                """,
                context.device_id,
            )
            if (
                device is None
                or device["lifecycle_status"] != "bound"
                or device["current_certificate_id"] is None
                or device["certificate_status"] != "active"
                or device["valid_from"] > timestamp
                or device["valid_until"] <= timestamp
            ):
                raise AttestationRejected
            await connection.execute(
                """
                INSERT INTO device_fleet_attestation_challenges (
                    nonce, device_id, family_space_id, binding_id, binding_version,
                    issued_at, expires_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                nonce,
                context.device_id,
                context.family_space_id,
                context.binding_id,
                context.binding_version,
                timestamp,
                timestamp + ttl,
            )
        return nonce

    async def accept_attestation(
        self,
        context: DeviceFleetContext,
        attestation: DeviceAttestation,
        *,
        now: datetime | None = None,
    ) -> DeviceTrustAuthorityFacts:
        self.store.require_role("api")
        timestamp = now or datetime.now(UTC)
        async with self.store.transaction(context) as connection:
            return await self.accept_attestation_on_connection(
                connection, context, attestation, now=timestamp
            )

    async def accept_attestation_on_connection(
        self,
        connection: asyncpg.Connection,
        context: DeviceFleetContext,
        attestation: DeviceAttestation,
        *,
        now: datetime,
    ) -> DeviceTrustAuthorityFacts:
        """Connection-bound attestation CAS for Session/Policy integration."""

        self.store.require_role("api")
        await self.store.set_context_on_connection(connection, context)
        device = await connection.fetchrow(
            "SELECT * FROM device_fleet_devices WHERE device_id = $1 FOR UPDATE",
            context.device_id,
        )
        certificate = await connection.fetchrow(
            "SELECT * FROM device_fleet_certificates WHERE certificate_id = $1",
            attestation.certificate_id,
        )
        challenge = await connection.fetchrow(
            "SELECT * FROM device_fleet_attestation_challenges WHERE nonce = $1 FOR UPDATE",
            attestation.nonce,
        )
        acknowledged_command: asyncpg.Record | None = None
        try:
            if device is None or certificate is None or challenge is None:
                raise AttestationRejected
            if (
                device["lifecycle_status"] != "bound"
                or device["current_certificate_id"] != attestation.certificate_id
                or certificate["status"] != "active"
                or certificate["key_algorithm"] != "ed25519"
                or certificate["device_id"] != context.device_id
                or certificate["valid_from"] > now
                or certificate["valid_until"] <= now
            ):
                raise AttestationRejected
            if (
                attestation.device_id != context.device_id
                or attestation.binding_id != context.binding_id
                or attestation.binding_version != context.binding_version
                or attestation.signer_key_id != attestation.certificate_id
                or attestation.certificate_status
                is not DeviceCertificateStatus.DEVICE_CERTIFICATE_STATUS_ACTIVE
                or attestation.device_lifecycle_status
                is not DeviceLifecycleStatus.DEVICE_LIFECYCLE_STATUS_BOUND
            ):
                raise AttestationRejected
            if (
                challenge["device_id"] != context.device_id
                or challenge["binding_id"] != context.binding_id
                or challenge["binding_version"] != context.binding_version
                or challenge["family_space_id"] != context.family_space_id
                or challenge["consumed_at"] is not None
                or challenge["expires_at"] <= now
            ):
                raise AttestationRejected
            if (
                attestation.occurred_at > now + timedelta(seconds=5)
                or now - attestation.occurred_at > timedelta(minutes=5)
                or attestation.expires_at <= now
                or attestation.expires_at - attestation.occurred_at > timedelta(minutes=5)
            ):
                raise AttestationRejected
            declared_capabilities = _json_value(device["capabilities"])
            if not isinstance(declared_capabilities, list) or not all(
                isinstance(capability, str) for capability in declared_capabilities
            ):
                raise AttestationRejected
            capabilities: tuple[str, ...] = tuple(declared_capabilities)
            reported_command_sequence = attestation.last_command_sequence
            current_command_sequence = int(device["last_command_sequence"])
            if reported_command_sequence == current_command_sequence + 1:
                acknowledged_command = await connection.fetchrow(
                    """
                    SELECT * FROM device_fleet_remote_commands
                    WHERE device_id = $1 AND command_sequence = $2
                    FOR UPDATE
                    """,
                    context.device_id,
                    reported_command_sequence,
                )
                if (
                    acknowledged_command is None
                    or acknowledged_command["status"] not in {"dispatched", "uncertain"}
                    or acknowledged_command["target_certificate_id"]
                    != attestation.certificate_id
                    or acknowledged_command["binding_id"] != context.binding_id
                    or acknowledged_command["binding_version"] != context.binding_version
                    or acknowledged_command["expected_monotonic_counter"]
                    != device["last_attestation_counter"]
                    or acknowledged_command["expected_firmware_security_version"]
                    != device["firmware_security_version"]
                ):
                    raise AttestationRejected
            elif reported_command_sequence != current_command_sequence:
                raise AttestationRejected
            if (
                attestation.capability_manifest_hash != device["capability_manifest_hash"]
                or tuple(capability.value for capability in attestation.capabilities)
                != capabilities
                or attestation.firmware_security_version
                < device["anti_rollback_floor_security_version"]
                or attestation.anti_rollback_floor_security_version
                < device["anti_rollback_floor_security_version"]
                or attestation.monotonic_counter <= device["last_attestation_counter"]
            ):
                raise AttestationRejected
            key = _public_key(certificate["public_key_b64"])
            if not verify_device_attestation_signature(attestation, key):
                raise AttestationRejected
            await connection.execute(
                """
                INSERT INTO device_fleet_attestations (
                    attestation_id, device_id, certificate_id, family_space_id,
                    binding_id, binding_version, monotonic_counter, nonce, payload,
                    occurred_at, expires_at, accepted_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12)
                """,
                attestation.attestation_id,
                context.device_id,
                attestation.certificate_id,
                context.family_space_id,
                context.binding_id,
                context.binding_version,
                attestation.monotonic_counter,
                attestation.nonce,
                json.dumps(attestation.model_dump(mode="json"), separators=(",", ":")),
                attestation.occurred_at,
                attestation.expires_at,
                now,
            )
            await connection.execute(
                "UPDATE device_fleet_attestation_challenges SET consumed_at = $2 "
                "WHERE nonce = $1",
                attestation.nonce,
                now,
            )
            await connection.execute(
                """
                UPDATE device_fleet_devices SET last_attestation_counter = $2,
                    firmware_version = $3, firmware_security_version = $4,
                    firmware_sha256 = $5, bootloader_version = $6,
                    physical_mute_state = $7, privacy_light_state = $8,
                    attested_sim_status = $9, active_ota_slot = $10, ota_boot_status = $11,
                    anti_rollback_floor_version = $12,
                    anti_rollback_floor_security_version = $13,
                    last_command_sequence = $14,
                    state_version = state_version + 1, updated_at = $15
                WHERE device_id = $1
                """,
                context.device_id,
                attestation.monotonic_counter,
                attestation.firmware_version,
                attestation.firmware_security_version,
                attestation.firmware_sha256,
                attestation.bootloader_version,
                attestation.physical_mute_state.value,
                attestation.privacy_light_state.value,
                attestation.sim_status.value,
                attestation.active_ota_slot.value,
                attestation.ota_boot_status.value,
                attestation.anti_rollback_floor_version,
                attestation.anti_rollback_floor_security_version,
                attestation.last_command_sequence,
                now,
            )
            if acknowledged_command is not None:
                hardware_capabilities = {
                    DeviceCapability.DEVICE_CAPABILITY_PHYSICAL_MICROPHONE_CUT,
                    DeviceCapability.DEVICE_CAPABILITY_HARDWARE_PRIVACY_LIGHT,
                }
                hardware_confirmed = (
                    acknowledged_command["command_type"] == "force_mute"
                    and attestation.physical_mute_state.value == "engaged"
                    and attestation.privacy_light_state.value == "on"
                    and hardware_capabilities.issubset(
                        set(attestation.attested_capabilities)
                    )
                )
                command_status = (
                    "hardware_confirmed" if hardware_confirmed else "device_acknowledged"
                )
                await connection.execute(
                    """
                    UPDATE device_fleet_remote_commands SET status = $2::text,
                        device_acknowledged_at = $3::timestamptz,
                        hardware_confirmed_at = CASE WHEN $4::boolean
                            THEN $3::timestamptz ELSE NULL END
                    WHERE command_id = $1
                    """,
                    acknowledged_command["command_id"],
                    command_status,
                    now,
                    hardware_confirmed,
                )
                await connection.execute(
                    """
                    UPDATE device_fleet_command_outbox SET status = 'acknowledged',
                        lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                        updated_at = $2 WHERE command_id = $1
                    """,
                    acknowledged_command["command_id"],
                    now,
                )
                if acknowledged_command["command_type"] == "revoke_device":
                    await connection.execute(
                        """
                        UPDATE device_fleet_certificates SET status = 'revoked',
                            revoked_at = $2, revocation_reason_code = $3
                        WHERE certificate_id = $1 AND status = 'active'
                        """,
                        acknowledged_command["target_certificate_id"],
                        now,
                        acknowledged_command["reason_code"],
                    )
                    await connection.execute(
                        """
                        UPDATE device_fleet_devices SET lifecycle_status = 'revoked',
                            lifecycle_reason_code = $2,
                            state_version = state_version + 1, updated_at = $3
                        WHERE device_id = $1
                        """,
                        context.device_id,
                        acknowledged_command["reason_code"],
                        now,
                    )
                elif acknowledged_command["command_type"] == "secure_wipe":
                    await connection.execute(
                        """
                        UPDATE device_fleet_devices SET lifecycle_status = 'wipe_pending',
                            lifecycle_reason_code = $2,
                            state_version = state_version + 1, updated_at = $3
                        WHERE device_id = $1
                        """,
                        context.device_id,
                        acknowledged_command["reason_code"],
                        now,
                    )
        except (asyncpg.IntegrityConstraintViolationError, ValueError) as exc:
            raise AttestationRejected from exc
        facts = await self.resolve_device_trust_on_connection(
            connection, context, now=now
        )
        if facts is None:
            raise AttestationRejected
        return facts

    def _project_trust_facts(
        self,
        context: DeviceFleetContext,
        *,
        now: datetime,
        device: Mapping[str, object],
        certificate: Mapping[str, object] | None,
        attestation_row: Mapping[str, object] | None,
        sim: Mapping[str, object] | None,
        required_capabilities: Sequence[DeviceCapability] = (),
    ) -> DeviceTrustAuthorityFacts:
        reasons: list[str] = []
        certificate_status = (
            str(certificate["status"]) if certificate is not None else None
        )
        revoked = (
            device["lifecycle_status"] == "revoked"
            or certificate_status == "revoked"
        )
        if device["lifecycle_status"] != "bound":
            reasons.append("device_lifecycle_not_bound")
        if certificate is None:
            reasons.append("current_certificate_missing")
        elif (
            certificate["status"] != "active"
            or _datetime_value(certificate["valid_from"]) > now
            or _datetime_value(certificate["valid_until"]) <= now
            or certificate["device_id"] != context.device_id
            or certificate["binding_id"] != context.binding_id
            or _int_value(certificate["binding_version"]) != context.binding_version
        ):
            reasons.append("current_certificate_invalid")

        attestation: DeviceAttestation | None = None
        accepted_at: datetime | None = None
        if attestation_row is None:
            reasons.append("fresh_attestation_missing")
        else:
            accepted_at = _datetime_value(attestation_row["accepted_at"])
            payload = _json_value(attestation_row["payload"])
            try:
                if not isinstance(payload, dict):
                    raise ValueError
                attestation = DeviceAttestation.model_validate(payload)
            except (ValidationError, ValueError):
                reasons.append("attestation_payload_invalid")
            if attestation is not None:
                if (
                    _datetime_value(attestation_row["expires_at"]) <= now
                    or attestation.certificate_id != device["current_certificate_id"]
                    or attestation.device_id != context.device_id
                    or attestation.binding_id != context.binding_id
                    or attestation.binding_version != context.binding_version
                    or attestation.monotonic_counter
                    != _int_value(device["last_attestation_counter"])
                ):
                    reasons.append("attestation_fence_stale")
                public_key_value = (
                    str(certificate["public_key_b64"])
                    if certificate is not None
                    else ""
                )
                if certificate is None or not verify_device_attestation_signature(
                    attestation, _public_key(public_key_value)
                ):
                    reasons.append("attestation_signature_invalid")
                declared_value = _json_value(device["capabilities"])
                declared = tuple(declared_value) if isinstance(declared_value, list) else ()
                if (
                    attestation.capability_manifest_hash
                    != device["capability_manifest_hash"]
                    or tuple(item.value for item in attestation.capabilities) != declared
                ):
                    reasons.append("capability_manifest_mismatch")
                required = {
                    DeviceCapability.DEVICE_CAPABILITY_DEVICE_CERTIFICATE,
                    DeviceCapability.DEVICE_CAPABILITY_SECURE_ELEMENT,
                    DeviceCapability.DEVICE_CAPABILITY_REMOTE_ATTESTATION,
                }
                required.update(required_capabilities)
                if not required.issubset(set(attestation.attested_capabilities)):
                    reasons.append("required_capability_missing")
                if (
                    attestation.firmware_security_version
                    < _int_value(device["anti_rollback_floor_security_version"])
                    or attestation.anti_rollback_floor_security_version
                    < _int_value(device["anti_rollback_floor_security_version"])
                ):
                    reasons.append("firmware_security_floor_drift")
                if (
                    attestation.physical_mute_state.value == "mismatch"
                    or attestation.privacy_light_state.value == "mismatch"
                    or (
                        attestation.physical_mute_state.value == "engaged"
                        and attestation.privacy_light_state.value != "on"
                    )
                ):
                    reasons.append("physical_privacy_state_mismatch")

        server_sim_status = str(sim["server_status"]) if sim is not None else "absent"
        attested_sim_status = str(device["attested_sim_status"])
        if sim is not None:
            if (
                sim["device_id"] != context.device_id
                or sim["binding_id"] != context.binding_id
                or _int_value(sim["binding_version"]) != context.binding_version
                or _int_value(sim["authority_revision"])
                != _int_value(device["sim_authority_revision"])
            ):
                reasons.append("sim_authority_fence_mismatch")
            if sim["server_status"] != "active" or sim["provider_status"] != "active":
                reasons.append("server_sim_not_active")
            if accepted_at is None or accepted_at < _datetime_value(sim["updated_at"]):
                reasons.append("sim_attestation_stale")
        if attested_sim_status != server_sim_status:
            reasons.append("sim_status_mismatch")

        trust = (
            DeviceTrust.DEVICE_TRUST_REVOKED
            if revoked
            else (
                DeviceTrust.DEVICE_TRUST_UNTRUSTED
                if reasons
                else DeviceTrust.DEVICE_TRUST_VERIFIED
            )
        )
        certificate_id_value = device["current_certificate_id"]
        return DeviceTrustAuthorityFacts(
            family_space_id=context.family_space_id,
            binding_id=context.binding_id,
            binding_version=context.binding_version,
            trust=trust,
            reasons=tuple(dict.fromkeys(reasons)),
            attestation=attestation,
            accepted_at=accepted_at,
            certificate_id=(
                str(certificate_id_value)
                if certificate_id_value is not None
                else None
            ),
            certificate_status=certificate_status,
            server_sim_status=server_sim_status,
            attested_sim_status=attested_sim_status,
        )

    async def resolve_device_trust(
        self,
        context: DeviceFleetContext,
        *,
        now: datetime | None = None,
        required_capabilities: Sequence[DeviceCapability] = (),
    ) -> DeviceTrustAuthorityFacts | None:
        self.store.require_role("api", "projector", "worker", "action_executor")
        timestamp = now or datetime.now(UTC)
        async with self.store.transaction(context) as connection:
            return await self.resolve_device_trust_on_connection(
                connection,
                context,
                now=timestamp,
                required_capabilities=required_capabilities,
            )

    async def resolve_device_trust_on_connection(
        self,
        connection: asyncpg.Connection,
        context: DeviceFleetContext,
        *,
        now: datetime,
        required_capabilities: Sequence[DeviceCapability] = (),
    ) -> DeviceTrustAuthorityFacts | None:
        """Return current locked facts on a caller-owned transaction."""

        self.store.require_role("api", "projector", "worker", "action_executor")
        await self.store.set_context_on_connection(connection, context)
        if self.store.role == "action_executor":
            snapshot_value = await connection.fetchval(
                "SELECT device_fleet_action_lock_snapshot($1, $2, $3, $4, $5, $6)",
                context.device_id,
                context.binding_id,
                context.binding_version,
                context.family_space_id,
                None,
                None,
            )
            snapshot = _json_value(snapshot_value)
            if not isinstance(snapshot, dict):
                return None
            device = snapshot.get("device")
            if not isinstance(device, dict):
                return None
            certificate_value = snapshot.get("certificate")
            attestation_value = snapshot.get("attestation")
            sim_value = snapshot.get("sim")
            return self._project_trust_facts(
                context,
                now=now,
                device=device,
                certificate=(
                    certificate_value
                    if isinstance(certificate_value, dict)
                    else None
                ),
                attestation_row=(
                    attestation_value
                    if isinstance(attestation_value, dict)
                    else None
                ),
                sim=sim_value if isinstance(sim_value, dict) else None,
                required_capabilities=required_capabilities,
            )
        # Fixed lock order: device -> current certificate -> latest
        # attestation -> binding head -> SIM authority -> command head.  Every
        # lifecycle mutator contends on the device row before changing a head.
        locked = await connection.fetchval(
            "SELECT device_fleet_lock_trust_heads($1, $2, $3, $4)",
            context.device_id,
            context.binding_id,
            context.binding_version,
            context.family_space_id,
        )
        if not locked:
            return None
        device = await connection.fetchrow(
            "SELECT * FROM device_fleet_devices WHERE device_id = $1",
            context.device_id,
        )
        if device is None:
            return None
        certificate = None
        if device["current_certificate_id"] is not None:
            certificate = await connection.fetchrow(
                "SELECT * FROM device_fleet_certificates "
                "WHERE certificate_id = $1",
                device["current_certificate_id"],
            )
        attestation_row = await connection.fetchrow(
            "SELECT * FROM device_fleet_attestations WHERE device_id = $1 "
            "ORDER BY monotonic_counter DESC LIMIT 1",
            context.device_id,
        )
        await connection.fetchrow(
            "SELECT event_id FROM device_fleet_binding_events "
            "WHERE device_id = $1 ORDER BY binding_version DESC, occurred_at DESC "
            "LIMIT 1",
            context.device_id,
        )
        sim = None
        if device["current_sim_id"] is not None:
            sim = await connection.fetchrow(
                "SELECT * FROM device_fleet_sim_profiles WHERE sim_id = $1",
                device["current_sim_id"],
            )
        await connection.fetchrow(
            "SELECT command_id FROM device_fleet_remote_commands "
            "WHERE device_id = $1 ORDER BY command_sequence DESC LIMIT 1",
            context.device_id,
        )

        return self._project_trust_facts(
            context,
            now=now,
            device=device,
            certificate=certificate,
            attestation_row=attestation_row,
            sim=sim,
            required_capabilities=required_capabilities,
        )

    async def execute_with_locked_device_trust(
        self,
        context: DeviceFleetContext,
        callback: Callable[
            [asyncpg.Connection, DeviceTrustAuthorityFacts | None],
            T | Awaitable[T],
        ],
        *,
        now: datetime | None = None,
        required_capabilities: Sequence[DeviceCapability] = (),
    ) -> T:
        """Run a Policy callback while all Device Fleet heads remain locked.

        The callback receives authority facts only; this adapter never grants
        a capability.  A concurrent revoke, transfer, certificate rotation or
        SIM authority transition cannot commit until the callback transaction
        exits.
        """

        self.store.require_role("api", "projector", "worker", "action_executor")
        timestamp = now or datetime.now(UTC)
        async with self.store.transaction(context) as connection:
            facts = await self.resolve_device_trust_on_connection(
                connection,
                context,
                now=timestamp,
                required_capabilities=required_capabilities,
            )
            result = callback(connection, facts)
            if inspect.isawaitable(result):
                return await cast(Awaitable[T], result)
            return result

    async def issue_remote_command(
        self,
        context: DeviceFleetContext,
        *,
        authority_input: object,
        command_type: RemoteDeviceCommandType,
        reason_code: str,
        parameters: Mapping[str, object],
        idempotency_key: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=2),
        ota_assignment_id: str | None = None,
    ) -> RemoteDeviceCommand:
        """Issue a short-lived signed tightening/governance command."""

        self.store.require_role("action_executor")
        timestamp = now or datetime.now(UTC)
        if not isinstance(command_type, RemoteDeviceCommandType):
            raise ValueError("command_type must be a generated RemoteDeviceCommandType")
        if not reason_code or not idempotency_key:
            raise ValueError("reason and idempotency key are required")
        if ttl <= timedelta(0) or ttl > timedelta(minutes=5):
            raise ValueError("remote command TTL must be within five minutes")
        if (
            command_type is RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_ASSIGN_OTA
        ) != (ota_assignment_id is not None):
            raise ValueError("assign_ota is the only command that binds an OTA assignment")
        _validate_remote_command_parameters(command_type, parameters)
        parameters_json, parameters_hash = _canonical_parameters(parameters)
        async with self.store.transaction(context) as connection:
            snapshot_value = await connection.fetchval(
                "SELECT device_fleet_action_lock_snapshot($1, $2, $3, $4, $5, $6)",
                context.device_id,
                context.binding_id,
                context.binding_version,
                context.family_space_id,
                idempotency_key,
                ota_assignment_id,
            )
            snapshot = _json_value(snapshot_value)
            if not isinstance(snapshot, dict):
                raise RemoteCommandRejected
            device = snapshot.get("device")
            certificate = snapshot.get("certificate")
            attestation_row = snapshot.get("attestation")
            sim = snapshot.get("sim")
            if not isinstance(device, dict) or device.get("lifecycle_status") != "bound":
                raise RemoteCommandRejected
            certificate = certificate if isinstance(certificate, dict) else None
            attestation_row = (
                attestation_row if isinstance(attestation_row, dict) else None
            )
            sim = sim if isinstance(sim, dict) else None
            if ota_assignment_id is not None:
                assignment_value = snapshot.get("assignment")
                assignment_row = (
                    assignment_value if isinstance(assignment_value, dict) else None
                )
                expected_parameters = (
                    {
                        "assignment_id": ota_assignment_id,
                        "artifact_url": assignment_row["artifact_url"],
                        "artifact_sha256": assignment_row["artifact_sha256"],
                        "artifact_size_bytes": assignment_row["artifact_size_bytes"],
                    }
                    if assignment_row is not None
                    else None
                )
                if (
                    assignment_row is None
                    or assignment_row["status"] != "pending"
                    or dict(parameters) != expected_parameters
                ):
                    raise RemoteCommandRejected
            existing_value = snapshot.get("existing_command")
            existing = (
                existing_value if isinstance(existing_value, dict) else None
            )
            existing_command: RemoteDeviceCommand | None = None
            if existing is not None:
                payload = _json_value(existing["payload"])
                if not isinstance(payload, dict):
                    raise DeviceFleetConflict("stored command payload is invalid")
                existing_command = RemoteDeviceCommand.model_validate(payload)
                if (
                    existing["parameters_hash"] != parameters_hash
                    or existing_command.command_type is not command_type
                    or existing_command.reason_code != reason_code
                    or existing_command.ota_assignment_id != ota_assignment_id
                ):
                    raise DeviceFleetConflict("idempotency key conflict")
            if existing_command is not None:
                action = build_device_command_action(
                    device_id=existing_command.device_id,
                    certificate_id=existing_command.target_certificate_id,
                    binding_id=existing_command.expected_binding_id,
                    binding_version=existing_command.expected_binding_version,
                    monotonic_counter=existing_command.expected_monotonic_counter,
                    firmware_security_version=(
                        existing_command.expected_firmware_security_version
                    ),
                    command_type=existing_command.command_type,
                    parameters_hash=existing_command.parameters_hash,
                    idempotency_key=existing_command.idempotency_key,
                    allowed_binding_roles=_allowed_binding_roles(command_type),
                )
            else:
                # The action role has no table ACL; project the already locked
                # snapshot rather than opening a second authority path.
                facts = self._project_trust_facts(
                    context,
                    now=timestamp,
                    device=device,
                    certificate=certificate,
                    attestation_row=attestation_row,
                    sim=sim,
                )
                if (
                    facts is None
                    or facts.trust is not DeviceTrust.DEVICE_TRUST_VERIFIED
                    or facts.attestation is None
                ):
                    raise RemoteCommandRejected
                action = build_device_command_action(
                    device_id=context.device_id,
                    certificate_id=facts.attestation.certificate_id,
                    binding_id=context.binding_id,
                    binding_version=context.binding_version,
                    monotonic_counter=facts.attestation.monotonic_counter,
                    firmware_security_version=facts.attestation.firmware_security_version,
                    command_type=command_type,
                    parameters_hash=parameters_hash,
                    idempotency_key=idempotency_key,
                    allowed_binding_roles=_allowed_binding_roles(command_type),
                )

            async def authorized_write(
                active: asyncpg.Connection,
                authorization: DeviceCommandAuthorization,
            ) -> RemoteDeviceCommand:
                if authorization.action != action:
                    raise CommandAuthorizationRejected("authorized action mismatch")
                if existing_command is not None:
                    if existing_command.actor_id != authorization.actor_id:
                        raise DeviceFleetConflict("idempotency key actor conflict")
                    return existing_command
                sequence = max(
                    int(snapshot.get("max_command_sequence", 0)),
                    int(device["last_command_sequence"]),
                ) + 1
                payload = {
                    "command_id": f"command-{uuid.uuid4().hex}",
                    "idempotency_key": idempotency_key,
                    "device_id": context.device_id,
                    "actor_id": authorization.actor_id,
                    "command_type": command_type,
                    "reason_code": reason_code,
                    "ota_assignment_id": ota_assignment_id,
                    "parameters_hash": parameters_hash,
                    "signer_key_id": self._command_signer_key_id,
                    "signature_algorithm": "ed25519",
                    "target_certificate_id": action.certificate_id,
                    "expected_binding_id": action.binding_id,
                    "expected_binding_version": action.binding_version,
                    "expected_monotonic_counter": action.monotonic_counter,
                    "expected_firmware_security_version": (
                        action.firmware_security_version
                    ),
                    "command_sequence": sequence,
                    "issued_at": _rfc3339(timestamp),
                    "expires_at": _rfc3339(timestamp + ttl),
                }
                command = sign_remote_device_command(payload, self._command_signing_key)
                try:
                    stored = await active.fetchval(
                        "SELECT device_fleet_action_commit_command("
                        "$1::jsonb, $2::jsonb, $3::jsonb)",
                        json.dumps(
                            command.model_dump(mode="json"), separators=(",", ":")
                        ),
                        parameters_json,
                        json.dumps(
                            {
                                "binding_role": authorization.binding_role,
                                "authority_receipt_id": (
                                    authorization.authority_receipt_id
                                ),
                                "action_resource_id": action.action_resource_id,
                                "action_resource_hash": action.canonical_hash,
                                "action_fence_hash": authorization.action_fence_hash,
                            },
                            separators=(",", ":"),
                        ),
                    )
                    stored_value = _json_value(stored)
                    if not isinstance(stored_value, dict):
                        raise DeviceFleetConflict("remote command commit failed")
                    if RemoteDeviceCommand.model_validate(stored_value) != command:
                        raise DeviceFleetConflict("remote command commit mismatch")
                except asyncpg.PostgresError as exc:
                    raise DeviceFleetConflict(
                        "remote command authority or sequence conflict"
                    ) from exc
                return command

            return await self._command_authority.execute_authorized(
                connection,
                action,
                authority_input,
                authorized_write,
            )

    async def accept_remote_command_execution(
        self,
        context: DeviceFleetContext,
        command: RemoteDeviceCommand,
        *,
        now: datetime | None = None,
    ) -> RemoteDeviceCommandExecution:
        """Record a pre-execution acceptance envelope and durable outbox row."""

        self.store.require_role("worker")
        timestamp = now or datetime.now(UTC)
        async with self.store.transaction(context) as connection:
            device = await connection.fetchrow(
                "SELECT * FROM device_fleet_devices WHERE device_id = $1 FOR UPDATE",
                context.device_id,
            )
            row = await connection.fetchrow(
                "SELECT * FROM device_fleet_remote_commands WHERE command_id = $1 FOR UPDATE",
                command.command_id,
            )
            if (
                device is None
                or device["lifecycle_status"] != "bound"
                or row is None
                or row["status"] != "pending"
                or row["expires_at"] <= timestamp
                or command.command_sequence != device["last_command_sequence"] + 1
            ):
                raise RemoteCommandRejected
            stored = _json_value(row["payload"])
            if (
                not isinstance(stored, dict)
                or RemoteDeviceCommand.model_validate(stored) != command
                or not verify_remote_device_command_signature(
                    command, self.command_verification_key
                )
            ):
                raise RemoteCommandRejected
            facts = await self.resolve_device_trust_on_connection(
                connection, context, now=timestamp
            )
            if (
                facts is None
                or facts.trust is not DeviceTrust.DEVICE_TRUST_VERIFIED
                or facts.attestation is None
            ):
                raise RemoteCommandRejected
            try:
                execution = RemoteDeviceCommandExecution.model_validate(
                    {
                        "execution_id": f"execution-{uuid.uuid4().hex}",
                        "command": command.model_dump(mode="json"),
                        "current_attestation": facts.attestation.model_dump(mode="json"),
                        "accepted_at": _rfc3339(timestamp),
                    }
                )
                await connection.execute(
                    """
                    INSERT INTO device_fleet_remote_command_executions (
                        execution_id, command_id, device_id, family_space_id,
                        binding_id, binding_version, command_sequence, payload, accepted_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                    """,
                    execution.execution_id,
                    command.command_id,
                    context.device_id,
                    context.family_space_id,
                    context.binding_id,
                    context.binding_version,
                    command.command_sequence,
                    json.dumps(execution.model_dump(mode="json"), separators=(",", ":")),
                    timestamp,
                )
                await connection.execute(
                    "UPDATE device_fleet_remote_commands SET status = 'accepted', "
                    "accepted_at = $2 WHERE command_id = $1",
                    command.command_id,
                    timestamp,
                )
                await connection.execute(
                    """
                    INSERT INTO device_fleet_command_outbox (
                        command_id, device_id, family_space_id, binding_id,
                        binding_version, status, next_attempt_at, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, 'ready', $6, $6, $6)
                    """,
                    command.command_id,
                    context.device_id,
                    context.family_space_id,
                    context.binding_id,
                    context.binding_version,
                    timestamp,
                )
            except (ValidationError, asyncpg.IntegrityConstraintViolationError) as exc:
                raise RemoteCommandRejected from exc
            return execution

    async def get_remote_command_state(
        self,
        context: DeviceFleetContext,
        command_id: str,
    ) -> RemoteCommandState | None:
        self.store.require_role("api", "projector", "worker")
        async with self.store.transaction(context) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM device_fleet_remote_commands WHERE command_id = $1",
                command_id,
            )
        if row is None:
            return None
        payload = _json_value(row["payload"])
        if not isinstance(payload, dict):
            raise RemoteCommandRejected
        return RemoteCommandState(
            command=RemoteDeviceCommand.model_validate(payload),
            status=row["status"],
            accepted_at=row["accepted_at"],
            dispatched_at=row["dispatched_at"],
            device_acknowledged_at=row["device_acknowledged_at"],
            hardware_confirmed_at=row["hardware_confirmed_at"],
            last_error_code=row["last_error_code"],
        )

    async def command_dispatch_readiness(
        self,
        context: DeviceFleetContext,
        *,
        now: datetime | None = None,
        heartbeat_ttl: timedelta = timedelta(seconds=30),
    ) -> CommandDispatchReadiness:
        """Expose accepted-but-undispatched work and dispatcher freshness."""

        self.store.require_role("worker", "projector")
        timestamp = now or datetime.now(UTC)
        async with self.store.transaction(context) as connection:
            pending = await connection.fetchval(
                "SELECT count(*) FROM device_fleet_remote_commands "
                "WHERE device_id = $1 AND status = 'accepted'",
                context.device_id,
            )
            heartbeat = await connection.fetchval(
                "SELECT MAX(heartbeat_at) FROM device_fleet_dispatcher_heartbeats "
                "WHERE device_id = $1",
                context.device_id,
            )
        ready = heartbeat is not None and heartbeat > timestamp - heartbeat_ttl
        return CommandDispatchReadiness(
            ready=ready,
            accepted_not_dispatched=int(pending),
            last_dispatcher_heartbeat_at=heartbeat,
        )

    async def claim_remote_commands(
        self,
        context: DeviceFleetContext,
        *,
        dispatcher_id: str,
        now: datetime | None = None,
        limit: int = 10,
        lease_ttl: timedelta = timedelta(seconds=20),
    ) -> tuple[RemoteCommandDispatch, ...]:
        """Lease accepted commands with SKIP LOCKED for an injected dispatcher."""

        self.store.require_role("worker")
        timestamp = now or datetime.now(UTC)
        if not dispatcher_id or len(dispatcher_id) > 128:
            raise ValueError("dispatcher_id must be a short non-empty string")
        if not 1 <= limit <= 100 or lease_ttl <= timedelta(0):
            raise ValueError("invalid command claim limits")
        dispatches: list[RemoteCommandDispatch] = []
        async with self.store.transaction(context) as connection:
            await connection.execute(
                """
                INSERT INTO device_fleet_dispatcher_heartbeats (
                    dispatcher_id, device_id, family_space_id, binding_id,
                    binding_version, heartbeat_at
                ) VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (dispatcher_id, device_id)
                DO UPDATE SET heartbeat_at = EXCLUDED.heartbeat_at,
                              family_space_id = EXCLUDED.family_space_id,
                              binding_id = EXCLUDED.binding_id,
                              binding_version = EXCLUDED.binding_version
                """,
                dispatcher_id,
                context.device_id,
                context.family_space_id,
                context.binding_id,
                context.binding_version,
                timestamp,
            )
            rows = await connection.fetch(
                """
                SELECT o.command_id, o.attempt_count, c.payload
                FROM device_fleet_command_outbox o
                JOIN device_fleet_remote_commands c ON c.command_id = o.command_id
                WHERE o.device_id = $1
                  AND c.status = 'accepted'
                  AND c.expires_at > $2
                  AND o.next_attempt_at <= $2
                  AND (o.status = 'ready'
                       OR (o.status = 'leased' AND o.lease_until <= $2))
                ORDER BY c.command_sequence
                FOR UPDATE OF o SKIP LOCKED
                LIMIT $3
                """,
                context.device_id,
                timestamp,
                limit,
            )
            for row in rows:
                token = f"lease-{uuid.uuid4().hex}"
                lease_until = timestamp + lease_ttl
                await connection.execute(
                    """
                    UPDATE device_fleet_command_outbox SET status = 'leased',
                        attempt_count = attempt_count + 1, lease_token = $2,
                        lease_owner = $3, lease_until = $4, updated_at = $5
                    WHERE command_id = $1
                    """,
                    row["command_id"],
                    token,
                    dispatcher_id,
                    lease_until,
                    timestamp,
                )
                payload = _json_value(row["payload"])
                if not isinstance(payload, dict):
                    raise RemoteCommandRejected
                dispatches.append(
                    RemoteCommandDispatch(
                        command=RemoteDeviceCommand.model_validate(payload),
                        lease_token=token,
                        lease_until=lease_until,
                        attempt_count=int(row["attempt_count"]) + 1,
                    )
                )
        return tuple(dispatches)

    async def mark_remote_command_dispatched(
        self,
        context: DeviceFleetContext,
        *,
        command_id: str,
        lease_token: str,
        now: datetime | None = None,
        ack_timeout: timedelta = timedelta(seconds=30),
    ) -> None:
        """Commit transport acceptance; this is not a device execution ACK."""

        self.store.require_role("worker")
        timestamp = now or datetime.now(UTC)
        if ack_timeout <= timedelta(0) or ack_timeout > timedelta(minutes=5):
            raise ValueError("command ACK timeout must be within five minutes")
        async with self.store.transaction(context) as connection:
            row = await connection.fetchrow(
                "SELECT * FROM device_fleet_command_outbox "
                "WHERE command_id = $1 FOR UPDATE",
                command_id,
            )
            if (
                row is None
                or row["status"] != "leased"
                or row["lease_token"] != lease_token
                or row["lease_until"] <= timestamp
            ):
                raise RemoteCommandRejected
            await connection.execute(
                """
                UPDATE device_fleet_command_outbox SET status = 'dispatched',
                    lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                    next_attempt_at = $3, updated_at = $2 WHERE command_id = $1
                """,
                command_id,
                timestamp,
                timestamp + ack_timeout,
            )
            result = await connection.execute(
                """
                UPDATE device_fleet_remote_commands SET status = 'dispatched',
                    dispatched_at = $2 WHERE command_id = $1 AND status = 'accepted'
                """,
                command_id,
                timestamp,
            )
            if result != "UPDATE 1":
                raise RemoteCommandRejected

    async def reconcile_remote_commands(
        self,
        context: DeviceFleetContext,
        *,
        now: datetime | None = None,
    ) -> CommandReconcileResult:
        """Requeue abandoned leases and mark dispatched/no-ACK work uncertain."""

        self.store.require_role("worker")
        timestamp = now or datetime.now(UTC)
        async with self.store.transaction(context) as connection:
            requeued_rows = await connection.fetch(
                """
                UPDATE device_fleet_command_outbox SET status = 'ready',
                    lease_token = NULL, lease_owner = NULL, lease_until = NULL,
                    next_attempt_at = $2, last_error_code = 'dispatch_lease_expired',
                    updated_at = $2
                WHERE device_id = $1 AND status = 'leased' AND lease_until <= $2
                RETURNING command_id
                """,
                context.device_id,
                timestamp,
            )
            uncertain_rows = await connection.fetch(
                """
                UPDATE device_fleet_command_outbox SET status = 'uncertain',
                    last_error_code = 'device_ack_timeout', updated_at = $2
                WHERE device_id = $1 AND status = 'dispatched'
                  AND next_attempt_at <= $2
                RETURNING command_id
                """,
                context.device_id,
                timestamp,
            )
            for row in uncertain_rows:
                await connection.execute(
                    """
                    UPDATE device_fleet_remote_commands SET status = 'uncertain',
                        last_error_code = 'device_ack_timeout'
                    WHERE command_id = $1 AND status = 'dispatched'
                    """,
                    row["command_id"],
                )
        return CommandReconcileResult(
            requeued=len(requeued_rows),
            uncertain=len(uncertain_rows),
        )

    async def resolve_device_lifecycle(
        self,
        context: DeviceFleetContext,
    ) -> DeviceLifecycleAuthorityFacts | None:
        """Project current lifecycle facts without converting them into permission."""

        self.store.require_role("api", "projector", "worker")
        async with self.store.transaction(context) as connection:
            row = await connection.fetchrow(
                """
                SELECT d.*, c.status AS certificate_status
                FROM device_fleet_devices d
                LEFT JOIN device_fleet_certificates c
                  ON c.certificate_id = d.current_certificate_id
                WHERE d.device_id = $1
                """,
                context.device_id,
            )
        if row is None:
            return None
        return DeviceLifecycleAuthorityFacts(
            device_id=row["device_id"],
            family_space_id=row["family_space_id"],
            binding_id=row["binding_id"],
            binding_version=row["binding_version"],
            lifecycle_status=row["lifecycle_status"],
            certificate_id=row["current_certificate_id"],
            certificate_status=row["certificate_status"],
            physical_mute_state=row["physical_mute_state"],
            privacy_light_state=row["privacy_light_state"],
        )

    async def issue_ota_assignment(
        self,
        context: DeviceFleetContext,
        *,
        authority_input: object,
        assignment_id: str,
        artifact_url: str,
        target_version: str,
        target_firmware_security_version: int,
        artifact_sha256: str,
        artifact_size_bytes: int,
        min_bootloader_version: str,
        channel: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(days=1),
    ) -> OtaAssignment:
        """Create a signed assignment for the currently inactive A/B slot."""

        self.store.require_role("action_executor")
        timestamp = now or datetime.now(UTC)
        parsed_url = urlsplit(artifact_url)
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise OtaRejected
        if ttl <= timedelta(0) or ttl > timedelta(days=7):
            raise OtaRejected
        semantic_parameters = {
            "assignment_id": assignment_id,
            "artifact_url": artifact_url,
            "target_version": target_version,
            "target_firmware_security_version": target_firmware_security_version,
            "artifact_sha256": artifact_sha256,
            "artifact_size_bytes": artifact_size_bytes,
            "min_bootloader_version": min_bootloader_version,
            "channel": channel,
        }
        _, parameters_hash = _canonical_parameters(semantic_parameters)
        async with self.store.transaction(context) as connection:
            snapshot_value = await connection.fetchval(
                "SELECT device_fleet_action_lock_snapshot($1, $2, $3, $4, $5, $6)",
                context.device_id,
                context.binding_id,
                context.binding_version,
                context.family_space_id,
                None,
                assignment_id,
            )
            snapshot = _json_value(snapshot_value)
            if not isinstance(snapshot, dict):
                raise OtaRejected
            device = snapshot.get("device")
            certificate = snapshot.get("certificate")
            attestation_row = snapshot.get("attestation")
            sim = snapshot.get("sim")
            if not isinstance(device, dict) or device.get("lifecycle_status") != "bound":
                raise OtaRejected
            certificate = certificate if isinstance(certificate, dict) else None
            attestation_row = (
                attestation_row if isinstance(attestation_row, dict) else None
            )
            sim = sim if isinstance(sim, dict) else None
            existing_value = snapshot.get("assignment")
            existing = (
                existing_value if isinstance(existing_value, dict) else None
            )
            existing_row: dict[str, object] = (
                existing if existing is not None else {}
            )
            existing_assignment: OtaAssignment | None = None
            if existing is not None:
                payload = _json_value(existing["payload"])
                if not isinstance(payload, dict):
                    raise OtaRejected
                existing_assignment = OtaAssignment.model_validate(payload)
                if (
                    existing["artifact_url"] != artifact_url
                    or existing_assignment.target_version != target_version
                    or existing_assignment.target_firmware_security_version
                    != target_firmware_security_version
                    or existing_assignment.artifact_sha256 != artifact_sha256
                    or existing_assignment.artifact_size_bytes != artifact_size_bytes
                    or existing_assignment.min_bootloader_version != min_bootloader_version
                    or existing_assignment.channel != channel
                ):
                    raise DeviceFleetConflict("OTA assignment id conflict")
            if existing_assignment is not None:
                existing_certificate_id = existing_row.get("target_certificate_id")
                existing_monotonic_counter = existing_row.get(
                    "expected_monotonic_counter"
                )
                existing_firmware_security_version = existing_row.get(
                    "expected_firmware_security_version"
                )
                existing_parameters_hash = existing_row.get("action_parameters_hash")
                if (
                    not isinstance(existing_certificate_id, str)
                    or isinstance(existing_monotonic_counter, bool)
                    or not isinstance(existing_monotonic_counter, (int, str))
                    or isinstance(existing_firmware_security_version, bool)
                    or not isinstance(existing_firmware_security_version, (int, str))
                    or not isinstance(existing_parameters_hash, str)
                ):
                    raise OtaRejected
                action = build_device_command_action(
                    device_id=context.device_id,
                    certificate_id=existing_certificate_id,
                    binding_id=context.binding_id,
                    binding_version=context.binding_version,
                    monotonic_counter=_int_value(existing_monotonic_counter),
                    firmware_security_version=_int_value(
                        existing_firmware_security_version
                    ),
                    command_type=(
                        RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_ASSIGN_OTA
                    ),
                    parameters_hash=existing_parameters_hash,
                    idempotency_key=assignment_id,
                    allowed_binding_roles=_allowed_binding_roles(
                        RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_ASSIGN_OTA
                    ),
                )
            else:
                facts = self._project_trust_facts(
                    context,
                    now=timestamp,
                    device=device,
                    certificate=certificate,
                    attestation_row=attestation_row,
                    sim=sim,
                    required_capabilities=(
                        DeviceCapability.DEVICE_CAPABILITY_AB_OTA,
                        DeviceCapability.DEVICE_CAPABILITY_ANTI_ROLLBACK,
                    ),
                )
                required_capabilities = {"ab_ota", "anti_rollback"}
                attested = (
                    {
                        capability.value
                        for capability in facts.attestation.attested_capabilities
                    }
                    if facts is not None and facts.attestation is not None
                    else set()
                )
                if (
                    facts is None
                    or facts.trust is not DeviceTrust.DEVICE_TRUST_VERIFIED
                    or facts.attestation is None
                    or not required_capabilities.issubset(attested)
                ):
                    raise OtaRejected
                if (
                    target_firmware_security_version
                    <= max(
                        int(device["firmware_security_version"]),
                        int(device["anti_rollback_floor_security_version"]),
                    )
                    or _version_key(min_bootloader_version)
                    > _version_key(device["bootloader_version"])
                    or len(artifact_sha256) != 64
                    or any(char not in "0123456789abcdef" for char in artifact_sha256)
                    or artifact_size_bytes < 1
                ):
                    raise OtaRejected
                action = build_device_command_action(
                    device_id=context.device_id,
                    certificate_id=facts.attestation.certificate_id,
                    binding_id=context.binding_id,
                    binding_version=context.binding_version,
                    monotonic_counter=facts.attestation.monotonic_counter,
                    firmware_security_version=facts.attestation.firmware_security_version,
                    command_type=(
                        RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_ASSIGN_OTA
                    ),
                    parameters_hash=parameters_hash,
                    idempotency_key=assignment_id,
                    allowed_binding_roles=_allowed_binding_roles(
                        RemoteDeviceCommandType.REMOTE_DEVICE_COMMAND_TYPE_ASSIGN_OTA
                    ),
                )

            async def authorized_write(
                active: asyncpg.Connection,
                authorization: DeviceCommandAuthorization,
            ) -> OtaAssignment:
                if authorization.action != action:
                    raise CommandAuthorizationRejected("authorized OTA action mismatch")
                if existing_assignment is not None:
                    if existing_row["actor_id"] != authorization.actor_id:
                        raise DeviceFleetConflict("OTA assignment actor conflict")
                    return existing_assignment
                target_slot = (
                    OtaSlotName.OTA_SLOT_NAME_B
                    if device["active_ota_slot"] == "a"
                    else OtaSlotName.OTA_SLOT_NAME_A
                )
                payload = {
                    "assignment_id": assignment_id,
                    "device_id": context.device_id,
                    "target_slot": target_slot,
                    "target_version": target_version,
                    "target_firmware_security_version": target_firmware_security_version,
                    "artifact_sha256": artifact_sha256,
                    "artifact_size_bytes": artifact_size_bytes,
                    "min_bootloader_version": min_bootloader_version,
                    "anti_rollback_floor_version": device[
                        "anti_rollback_floor_version"
                    ],
                    "anti_rollback_floor_security_version": device[
                        "anti_rollback_floor_security_version"
                    ],
                    "channel": channel,
                    "status": OtaAssignmentStatus.OTA_ASSIGNMENT_STATUS_PENDING,
                    "assigned_at": _rfc3339(timestamp),
                    "expires_at": _rfc3339(timestamp + ttl),
                    "signer_key_id": self._command_signer_key_id,
                    "signature_algorithm": "ed25519",
                }
                try:
                    assignment = sign_ota_assignment(payload, self._command_signing_key)
                    record = {
                        "assignment": assignment.model_dump(mode="json"),
                        "artifact_url": artifact_url,
                        "binding_id": context.binding_id,
                        "binding_version": context.binding_version,
                        "action_parameters_hash": parameters_hash,
                        "target_certificate_id": action.certificate_id,
                        "expected_monotonic_counter": action.monotonic_counter,
                        "expected_firmware_security_version": (
                            action.firmware_security_version
                        ),
                    }
                    authority_record = {
                        "actor_id": authorization.actor_id,
                        "binding_role": authorization.binding_role,
                        "authority_receipt_id": authorization.authority_receipt_id,
                        "action_resource_id": action.action_resource_id,
                        "action_resource_hash": action.canonical_hash,
                        "action_fence_hash": authorization.action_fence_hash,
                    }
                    stored = await active.fetchval(
                        "SELECT device_fleet_action_commit_ota($1::jsonb, $2::jsonb)",
                        json.dumps(record, separators=(",", ":")),
                        json.dumps(authority_record, separators=(",", ":")),
                    )
                    stored_value = _json_value(stored)
                    if not isinstance(stored_value, dict):
                        raise OtaRejected
                    if OtaAssignment.model_validate(stored_value) != assignment:
                        raise OtaRejected
                except (
                    ValidationError,
                    asyncpg.PostgresError,
                ) as exc:
                    raise OtaRejected from exc
                return assignment

            return await self._command_authority.execute_authorized(
                connection,
                action,
                authority_input,
                authorized_write,
            )

    async def accept_ota_state_receipt(
        self,
        context: DeviceFleetContext,
        receipt: OtaStateReceipt,
        *,
        now: datetime | None = None,
    ) -> OtaStateReceipt:
        """Verify and persist one monotonic device-signed A/B state transition."""

        self.store.require_role("worker")
        timestamp = now or datetime.now(UTC)
        async with self.store.transaction(context) as connection:
            device = await connection.fetchrow(
                "SELECT * FROM device_fleet_devices WHERE device_id = $1 FOR UPDATE",
                context.device_id,
            )
            certificate = await connection.fetchrow(
                "SELECT * FROM device_fleet_certificates WHERE certificate_id = $1",
                receipt.certificate_id,
            )
            assignment_row = await connection.fetchrow(
                "SELECT * FROM device_fleet_ota_assignments WHERE assignment_id = $1 FOR UPDATE",
                receipt.assignment_id,
            )
            latest_row = await connection.fetchrow(
                "SELECT payload FROM device_fleet_ota_receipts "
                "WHERE device_id = $1 ORDER BY monotonic_counter DESC LIMIT 1",
                context.device_id,
            )
            try:
                if (
                    device is None
                    or certificate is None
                    or assignment_row is None
                    or device["lifecycle_status"] != "bound"
                    or device["current_certificate_id"] != receipt.certificate_id
                    or certificate["status"] != "active"
                    or certificate["valid_until"] <= timestamp
                    or receipt.device_id != context.device_id
                    or receipt.signer_key_id != receipt.certificate_id
                    or receipt.monotonic_counter <= device["last_ota_counter"]
                    or receipt.occurred_at > timestamp + timedelta(seconds=5)
                    or timestamp - receipt.occurred_at > timedelta(minutes=5)
                    or assignment_row["expires_at"] <= timestamp
                ):
                    raise OtaRejected
                assignment_payload = _json_value(assignment_row["payload"])
                if not isinstance(assignment_payload, dict):
                    raise OtaRejected
                assignment = OtaAssignment.model_validate(assignment_payload)
                if (
                    not verify_ota_assignment_signature(
                        assignment, self.command_verification_key
                    )
                    or not verify_ota_state_receipt_signature(
                        receipt, _public_key(certificate["public_key_b64"])
                    )
                    or receipt.assignment_id != assignment.assignment_id
                    or receipt.highest_accepted_version != assignment.target_version
                    or receipt.highest_accepted_firmware_security_version
                    != assignment.target_firmware_security_version
                    or receipt.anti_rollback_floor_security_version
                    < device["anti_rollback_floor_security_version"]
                ):
                    raise OtaRejected
                target = _ota_slot(receipt, assignment.target_slot)
                if (
                    target.firmware_version != assignment.target_version
                    or target.firmware_security_version
                    != assignment.target_firmware_security_version
                    or target.artifact_sha256 != assignment.artifact_sha256
                ):
                    raise OtaRejected
                latest: OtaStateReceipt | None = None
                if latest_row is not None:
                    latest_payload = _json_value(latest_row["payload"])
                    if not isinstance(latest_payload, dict):
                        raise OtaRejected
                    latest = OtaStateReceipt.model_validate(latest_payload)
                self._validate_ota_transition(device, assignment, latest, receipt)
                await connection.execute(
                    """
                    INSERT INTO device_fleet_ota_receipts (
                        receipt_id, device_id, certificate_id, assignment_id,
                        family_space_id, binding_id, binding_version,
                        monotonic_counter, active_slot, pending_slot, boot_status,
                        highest_accepted_firmware_security_version,
                        anti_rollback_floor_security_version, payload,
                        occurred_at, accepted_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                              $11, $12, $13, $14::jsonb, $15, $16)
                    """,
                    receipt.receipt_id,
                    context.device_id,
                    receipt.certificate_id,
                    receipt.assignment_id,
                    context.family_space_id,
                    context.binding_id,
                    context.binding_version,
                    receipt.monotonic_counter,
                    receipt.active_slot.value,
                    receipt.pending_slot.value if receipt.pending_slot is not None else None,
                    receipt.boot_status.value,
                    receipt.highest_accepted_firmware_security_version,
                    receipt.anti_rollback_floor_security_version,
                    json.dumps(receipt.model_dump(mode="json"), separators=(",", ":")),
                    receipt.occurred_at,
                    timestamp,
                )
                status_by_boot = {
                    OtaBootStatus.OTA_BOOT_STATUS_STAGED: "staged",
                    OtaBootStatus.OTA_BOOT_STATUS_BOOTING: "activated",
                    OtaBootStatus.OTA_BOOT_STATUS_CONFIRMED: "activated",
                    OtaBootStatus.OTA_BOOT_STATUS_ROLLBACK_PENDING: "failed",
                    OtaBootStatus.OTA_BOOT_STATUS_ROLLED_BACK: "failed",
                }
                assignment_status = status_by_boot.get(receipt.boot_status, "failed")
                await connection.execute(
                    "UPDATE device_fleet_ota_assignments SET status = $2, "
                    "updated_at = $3 WHERE assignment_id = $1",
                    assignment.assignment_id,
                    assignment_status,
                    timestamp,
                )
                confirmed = (
                    receipt.boot_status is OtaBootStatus.OTA_BOOT_STATUS_CONFIRMED
                    and receipt.active_slot is assignment.target_slot
                )
                await connection.execute(
                    """
                    UPDATE device_fleet_devices SET last_ota_counter = $2,
                        active_ota_slot = $3, ota_boot_status = $4,
                        anti_rollback_floor_version = $5,
                        anti_rollback_floor_security_version = $6,
                        firmware_version = CASE WHEN $7 THEN $8 ELSE firmware_version END,
                        firmware_security_version = CASE WHEN $7 THEN $9 ELSE firmware_security_version END,
                        firmware_sha256 = CASE WHEN $7 THEN $10 ELSE firmware_sha256 END,
                        state_version = state_version + 1, updated_at = $11
                    WHERE device_id = $1
                    """,
                    context.device_id,
                    receipt.monotonic_counter,
                    receipt.active_slot.value,
                    receipt.boot_status.value,
                    receipt.anti_rollback_floor_version,
                    receipt.anti_rollback_floor_security_version,
                    confirmed,
                    assignment.target_version,
                    assignment.target_firmware_security_version,
                    assignment.artifact_sha256,
                    timestamp,
                )
            except (
                ValidationError,
                ValueError,
                asyncpg.IntegrityConstraintViolationError,
            ) as exc:
                if isinstance(exc, OtaRejected):
                    raise
                raise OtaRejected from exc
            return receipt

    @staticmethod
    def _validate_ota_transition(
        device: asyncpg.Record,
        assignment: OtaAssignment,
        previous: OtaStateReceipt | None,
        current: OtaStateReceipt,
    ) -> None:
        target = _ota_slot(current, assignment.target_slot)
        old_slot = (
            OtaSlotName.OTA_SLOT_NAME_A
            if assignment.target_slot is OtaSlotName.OTA_SLOT_NAME_B
            else OtaSlotName.OTA_SLOT_NAME_B
        )
        old = _ota_slot(current, old_slot)
        if previous is None:
            if (
                current.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_STAGED
                or current.active_slot.value != device["active_ota_slot"]
                or current.pending_slot is not assignment.target_slot
                or target.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_STAGED
                or target.boot_attempts != 0
            ):
                raise OtaRejected
            return
        if (
            current.monotonic_counter <= previous.monotonic_counter
            or current.max_boot_attempts != previous.max_boot_attempts
            or current.highest_accepted_firmware_security_version
            < previous.highest_accepted_firmware_security_version
            or current.anti_rollback_floor_security_version
            < previous.anti_rollback_floor_security_version
        ):
            raise OtaRejected
        if previous.boot_status is OtaBootStatus.OTA_BOOT_STATUS_STAGED:
            previous_target = _ota_slot(previous, assignment.target_slot)
            if (
                current.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_BOOTING
                or current.active_slot is not previous.active_slot
                or current.pending_slot is not assignment.target_slot
                or target.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_BOOTING
                or target.boot_attempts != previous_target.boot_attempts + 1
            ):
                raise OtaRejected
            return
        if previous.boot_status is OtaBootStatus.OTA_BOOT_STATUS_BOOTING:
            previous_target = _ota_slot(previous, assignment.target_slot)
            if current.boot_status is OtaBootStatus.OTA_BOOT_STATUS_BOOTING:
                if (
                    current.active_slot is not previous.active_slot
                    or current.pending_slot is not assignment.target_slot
                    or target.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_BOOTING
                    or target.boot_attempts != previous_target.boot_attempts + 1
                    or target.boot_attempts > current.max_boot_attempts
                ):
                    raise OtaRejected
                return
            if current.boot_status is OtaBootStatus.OTA_BOOT_STATUS_CONFIRMED:
                if (
                    current.active_slot is not assignment.target_slot
                    or current.pending_slot is not None
                    or target.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_CONFIRMED
                    or current.anti_rollback_floor_security_version
                    < assignment.target_firmware_security_version
                ):
                    raise OtaRejected
                return
            if current.boot_status is OtaBootStatus.OTA_BOOT_STATUS_ROLLBACK_PENDING:
                if (
                    previous_target.boot_attempts < current.max_boot_attempts
                    or current.active_slot is not previous.active_slot
                    or current.pending_slot is not assignment.target_slot
                    or target.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_FAILED
                ):
                    raise OtaRejected
                return
            raise OtaRejected
        if previous.boot_status is OtaBootStatus.OTA_BOOT_STATUS_ROLLBACK_PENDING:
            if (
                current.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_ROLLED_BACK
                or current.active_slot is not old_slot
                or current.pending_slot is not None
                or target.boot_status is not OtaBootStatus.OTA_BOOT_STATUS_FAILED
                or current.rollback_from_version != assignment.target_version
                or current.rollback_to_version != old.firmware_version
            ):
                raise OtaRejected
            return
        raise OtaRejected

    async def get_latest_ota_state(
        self,
        context: DeviceFleetContext,
    ) -> OtaStateReceipt | None:
        """Restore the last durable device-signed boot state after restart."""

        self.store.require_role("api", "projector", "worker")
        async with self.store.transaction(context) as connection:
            row = await connection.fetchrow(
                "SELECT payload FROM device_fleet_ota_receipts "
                "WHERE device_id = $1 ORDER BY monotonic_counter DESC LIMIT 1",
                context.device_id,
            )
        if row is None:
            return None
        payload = _json_value(row["payload"])
        if not isinstance(payload, dict):
            raise OtaRejected
        return OtaStateReceipt.model_validate(payload)


__all__ = ["DeviceFleetService"]
