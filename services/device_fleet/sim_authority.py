"""SIM authority seam of ``DeviceFleetService``.

``DeviceFleetSimAuthorityMixin`` is composed into ``DeviceFleetService``; the
store handle and the command-authority adapter stay owned by the facade and are
only declared here for type checking.  The module exists so eSIM provisioning
and its audited SIM-state transitions can be read on their own, without the
certificate, binding, remote-command and OTA code around them.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg

from services.device_fleet.authority import (
    CommandAuthorizationRejected,
    DeviceCommandAuthorization,
    build_device_lifecycle_action,
)
from services.device_fleet.domain import (
    DeviceFleetConflict,
    DeviceFleetContext,
    SimAuthorityFacts,
)
from services.device_fleet.service_helpers import (
    _canonical_parameters,
    _json_value,
    _rfc3339,
)

if TYPE_CHECKING:
    from services.device_fleet.authority import DeviceCommandAuthorityPort
    from services.device_fleet.postgres_store import PostgresDeviceFleetStore


class DeviceFleetSimAuthorityMixin:
    """Provision a SIM profile and transition its audited authority state."""

    if TYPE_CHECKING:
        # Shared state owned by the DeviceFleetService facade.
        store: PostgresDeviceFleetStore
        _command_authority: DeviceCommandAuthorityPort

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
