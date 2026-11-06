"""Authoritative Device Fleet lifecycle and signed-attestation module."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import asyncpg
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceCapability,
    DeviceTrust,
    OtaAssignment,
    OtaAssignmentStatus,
    OtaBootStatus,
    OtaSlotName,
    OtaStateReceipt,
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
    verify_ota_assignment_signature,
    verify_ota_state_receipt_signature,
)
from services.device_fleet.domain import (
    DeviceFleetConflict,
    DeviceFleetContext,
    DeviceLifecycleAuthorityFacts,
    OtaRejected,
)
from services.device_fleet.postgres_store import PostgresDeviceFleetStore
from services.device_fleet.remote_commands import DeviceFleetRemoteCommandMixin
from services.device_fleet.service_helpers import (
    _allowed_binding_roles,
    _canonical_parameters,
    _int_value,
    _json_value,
    _ota_slot,
    _public_key,
    _rfc3339,
    _version_key,
)
from services.device_fleet.sim_authority import DeviceFleetSimAuthorityMixin
from services.device_fleet.trust import DeviceFleetTrustMixin


class DeviceFleetService(DeviceFleetTrustMixin, DeviceFleetSimAuthorityMixin, DeviceFleetRemoteCommandMixin):
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
