"""Device attestation and trust-fact seam of ``DeviceFleetService``.

``DeviceFleetTrustMixin`` is composed into ``DeviceFleetService``; the store
handle stays owned by the facade and is only declared here for type checking.
The module exists so the nonce, attestation and trust-projection path can be
read on its own, without the lifecycle, command and OTA code around it.
"""

from __future__ import annotations

import inspect
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceAttestation,
    DeviceCapability,
    DeviceCertificateStatus,
    DeviceLifecycleStatus,
    DeviceTrust,
)
from pydantic import ValidationError

from services.device_fleet.crypto import verify_device_attestation_signature
from services.device_fleet.domain import (
    AttestationRejected,
    DeviceFleetContext,
    DeviceTrustAuthorityFacts,
)
from services.device_fleet.service_helpers import (
    _datetime_value,
    _int_value,
    _json_value,
    _public_key,
)

if TYPE_CHECKING:
    from services.device_fleet.postgres_store import PostgresDeviceFleetStore

T = TypeVar("T")


class DeviceFleetTrustMixin:
    """Issue attestation challenges and project current Device trust facts."""

    if TYPE_CHECKING:
        # Shared state owned by the DeviceFleetService facade.
        store: PostgresDeviceFleetStore

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
