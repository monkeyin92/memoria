"""Remote command lifecycle seam of ``DeviceFleetService``.

``DeviceFleetRemoteCommandMixin`` is composed into ``DeviceFleetService``; the
store handle, command signer, signer key id and command-authority adapter stay
owned by the facade and are only declared here for type checking, together with
the trust helpers the command path reuses.  The module exists so command issue,
acceptance, dispatch readiness, claiming and reconciliation can be read on their
own, without the certificate, binding, SIM and OTA flows around them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceTrust,
    RemoteDeviceCommand,
    RemoteDeviceCommandExecution,
    RemoteDeviceCommandType,
)
from pydantic import ValidationError

from services.device_fleet.authority import (
    CommandAuthorizationRejected,
    DeviceCommandAuthorization,
    build_device_command_action,
)
from services.device_fleet.crypto import (
    sign_remote_device_command,
    verify_remote_device_command_signature,
)
from services.device_fleet.domain import (
    CommandDispatchReadiness,
    CommandReconcileResult,
    DeviceFleetConflict,
    DeviceFleetContext,
    RemoteCommandDispatch,
    RemoteCommandRejected,
    RemoteCommandState,
)
from services.device_fleet.service_helpers import (
    _allowed_binding_roles,
    _canonical_parameters,
    _json_value,
    _rfc3339,
    _validate_remote_command_parameters,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from packages.contracts.generated.python.multi_subject_contracts import (
        DeviceCapability,
    )

    from services.device_fleet.authority import DeviceCommandAuthorityPort
    from services.device_fleet.domain import DeviceTrustAuthorityFacts
    from services.device_fleet.postgres_store import PostgresDeviceFleetStore


class DeviceFleetRemoteCommandMixin:
    """Issue, dispatch and reconcile one signed remote device command."""

    if TYPE_CHECKING:
        # Shared state and helpers owned by the DeviceFleetService facade and
        # composed mixins.
        store: PostgresDeviceFleetStore
        _command_signing_key: Ed25519PrivateKey
        _command_signer_key_id: str
        _command_authority: DeviceCommandAuthorityPort

        @property
        def command_verification_key(self) -> Ed25519PublicKey: ...

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
        ) -> DeviceTrustAuthorityFacts: ...

        async def resolve_device_trust_on_connection(
            self,
            connection: asyncpg.Connection,
            context: DeviceFleetContext,
            *,
            now: datetime,
            required_capabilities: Sequence[DeviceCapability] = (),
        ) -> DeviceTrustAuthorityFacts | None: ...

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
