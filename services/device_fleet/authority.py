"""Callback-only authority seam for sensitive Device Fleet writes.

No authenticated principal, role grant, receipt or reusable allow boolean is
created by Device Fleet.  A caller-owned transaction supplies an authority
adapter; the adapter locks its own principal/binding/Policy heads and invokes
the write callback only while those locks remain held.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2,
    RemoteDeviceCommandType,
)

from services.policy.context import PolicyContext

T = TypeVar("T")


class CommandAuthorizationRejected(PermissionError):
    """Current locked authority does not authorize the device action."""


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class DeviceCommandAction:
    """Exact action resource presented to an external authority adapter."""

    device_id: str
    certificate_id: str
    binding_id: str
    binding_version: int
    monotonic_counter: int
    firmware_security_version: int
    command_type: RemoteDeviceCommandType
    parameters_hash: str
    idempotency_key: str
    action_resource_id: str
    action_revision: int
    allowed_binding_roles: frozenset[str]
    canonical_hash: str

    def __post_init__(self) -> None:
        expected = _digest(self.canonical_payload())
        if self.canonical_hash != expected:
            raise ValueError("device command action canonical hash mismatch")
        if self.action_resource_id != f"device-action-{expected}":
            raise ValueError("device command action resource id mismatch")
        if self.action_revision != self.binding_version:
            raise ValueError("device command action revision must equal binding version")
        if not self.allowed_binding_roles:
            raise ValueError("device command action requires binding roles")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "certificate_id": self.certificate_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "monotonic_counter": self.monotonic_counter,
            "firmware_security_version": self.firmware_security_version,
            "command_type": self.command_type.value,
            "parameters_hash": self.parameters_hash,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, slots=True)
class DeviceLifecycleAction:
    """Exact non-command lifecycle resource authorized in one callback."""

    device_id: str
    certificate_id: str
    binding_id: str
    binding_version: int
    monotonic_counter: int
    firmware_security_version: int
    action_type: str
    parameters_hash: str
    idempotency_key: str
    action_resource_id: str
    action_revision: int
    allowed_binding_roles: frozenset[str]
    canonical_hash: str

    def __post_init__(self) -> None:
        expected = _digest(self.canonical_payload())
        if self.canonical_hash != expected:
            raise ValueError("device lifecycle action canonical hash mismatch")
        if self.action_resource_id != f"device-action-{expected}":
            raise ValueError("device lifecycle action resource id mismatch")
        if self.action_revision != self.binding_version:
            raise ValueError("device lifecycle action revision must equal binding version")
        if not self.allowed_binding_roles:
            raise ValueError("device lifecycle action requires binding roles")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "certificate_id": self.certificate_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "monotonic_counter": self.monotonic_counter,
            "firmware_security_version": self.firmware_security_version,
            "action_type": self.action_type,
            "parameters_hash": self.parameters_hash,
            "idempotency_key": self.idempotency_key,
        }


type DeviceSensitiveAction = DeviceCommandAction | DeviceLifecycleAction


def build_device_command_action(
    *,
    device_id: str,
    certificate_id: str,
    binding_id: str,
    binding_version: int,
    monotonic_counter: int,
    firmware_security_version: int,
    command_type: RemoteDeviceCommandType,
    parameters_hash: str,
    idempotency_key: str,
    allowed_binding_roles: frozenset[str],
) -> DeviceCommandAction:
    payload = {
        "device_id": device_id,
        "certificate_id": certificate_id,
        "binding_id": binding_id,
        "binding_version": binding_version,
        "monotonic_counter": monotonic_counter,
        "firmware_security_version": firmware_security_version,
        "command_type": command_type.value,
        "parameters_hash": parameters_hash,
        "idempotency_key": idempotency_key,
    }
    canonical_hash = _digest(payload)
    return DeviceCommandAction(
        device_id=device_id,
        certificate_id=certificate_id,
        binding_id=binding_id,
        binding_version=binding_version,
        monotonic_counter=monotonic_counter,
        firmware_security_version=firmware_security_version,
        command_type=command_type,
        parameters_hash=parameters_hash,
        idempotency_key=idempotency_key,
        action_resource_id=f"device-action-{canonical_hash}",
        action_revision=binding_version,
        allowed_binding_roles=allowed_binding_roles,
        canonical_hash=canonical_hash,
    )


def build_device_lifecycle_action(
    *,
    device_id: str,
    certificate_id: str,
    binding_id: str,
    binding_version: int,
    monotonic_counter: int,
    firmware_security_version: int,
    action_type: str,
    parameters_hash: str,
    idempotency_key: str,
    allowed_binding_roles: frozenset[str],
) -> DeviceLifecycleAction:
    if not action_type or len(action_type) > 128:
        raise ValueError("device lifecycle action type is required")
    payload = {
        "device_id": device_id,
        "certificate_id": certificate_id,
        "binding_id": binding_id,
        "binding_version": binding_version,
        "monotonic_counter": monotonic_counter,
        "firmware_security_version": firmware_security_version,
        "action_type": action_type,
        "parameters_hash": parameters_hash,
        "idempotency_key": idempotency_key,
    }
    canonical_hash = _digest(payload)
    return DeviceLifecycleAction(
        device_id=device_id,
        certificate_id=certificate_id,
        binding_id=binding_id,
        binding_version=binding_version,
        monotonic_counter=monotonic_counter,
        firmware_security_version=firmware_security_version,
        action_type=action_type,
        parameters_hash=parameters_hash,
        idempotency_key=idempotency_key,
        action_resource_id=f"device-action-{canonical_hash}",
        action_revision=binding_version,
        allowed_binding_roles=allowed_binding_roles,
        canonical_hash=canonical_hash,
    )


@dataclass(frozen=True, slots=True)
class DeviceCommandAuthorization:
    """Ephemeral evidence passed only inside an authority-held callback."""

    action: DeviceSensitiveAction
    actor_id: str
    binding_role: str
    authority_receipt_id: str
    action_fence_hash: str

    def __post_init__(self) -> None:
        if not self.actor_id or not self.authority_receipt_id or not self.action_fence_hash:
            raise ValueError("device command authorization evidence is incomplete")
        if self.binding_role not in self.action.allowed_binding_roles:
            raise CommandAuthorizationRejected


type DeviceCommandWriteCallback[T] = Callable[
    [asyncpg.Connection, DeviceCommandAuthorization], Awaitable[T]
]


class DeviceCommandAuthorityPort(Protocol):
    async def execute_authorized(
        self,
        connection: asyncpg.Connection,
        action: DeviceSensitiveAction,
        authority_input: object,
        callback: DeviceCommandWriteCallback[T],
    ) -> T: ...


class RejectingDeviceCommandAuthority:
    """Default fail-closed adapter used when integration is absent."""

    async def execute_authorized(
        self,
        connection: asyncpg.Connection,
        action: DeviceSensitiveAction,
        authority_input: object,
        callback: DeviceCommandWriteCallback[T],
    ) -> T:
        del connection, action, authority_input, callback
        raise CommandAuthorizationRejected("device command authority is not configured")


class DeviceBindingRoleAuthorityPort(Protocol):
    async def lock_current_role(
        self,
        connection: asyncpg.Connection,
        *,
        actor_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
    ) -> str: ...


class SensitiveWritePort(Protocol):
    async def execute(
        self,
        connection: asyncpg.Connection,
        context: PolicyContext,
        write_callback: Callable[
            [asyncpg.Connection, PolicyReceiptV2], T | Awaitable[T]
        ],
    ) -> T: ...


@dataclass(frozen=True, slots=True)
class PolicyDeviceCommandInput:
    """Integration input consumed by the SensitiveWriteService adapter."""

    context: PolicyContext


class PolicySensitiveWriteDeviceCommandAuthority:
    """Adapt Policy SensitiveWriteService to DeviceCommandAuthorityPort.

    The supplied ``PolicyContext`` must use a currently generated canonical
    capability/purpose.  Dedicated device-command capabilities do not exist in
    the current generated contract, so selecting that mapping remains an
    integration decision; unsupported mappings are denied by Policy rather
    than guessed here.
    """

    def __init__(
        self,
        sensitive_write: SensitiveWritePort,
        binding_roles: DeviceBindingRoleAuthorityPort,
    ) -> None:
        self._sensitive_write = sensitive_write
        self._binding_roles = binding_roles

    async def execute_authorized(
        self,
        connection: asyncpg.Connection,
        action: DeviceSensitiveAction,
        authority_input: object,
        callback: DeviceCommandWriteCallback[T],
    ) -> T:
        if not isinstance(authority_input, PolicyDeviceCommandInput):
            raise CommandAuthorizationRejected("PolicyContext authority input is required")
        context = authority_input.context
        fence = context.action_resource_fence
        if (
            context.device_id != action.device_id
            or context.binding_id != action.binding_id
            or context.binding_version != action.binding_version
            or fence is None
            or fence.action_resource_id != action.action_resource_id
            or fence.action_revision != action.action_revision
        ):
            raise CommandAuthorizationRejected("Policy action resource mismatch")

        async def authorized(
            active: asyncpg.Connection,
            receipt: PolicyReceiptV2,
        ) -> T:
            if (
                receipt.actor_id != context.actor_id
                or receipt.device_id != action.device_id
                or receipt.binding_id != action.binding_id
                or receipt.binding_version != action.binding_version
                or receipt.action_resource_fence.action_resource_id
                != action.action_resource_id
                or receipt.action_resource_fence.action_revision
                != action.action_revision
            ):
                raise CommandAuthorizationRejected("locked Policy receipt mismatch")
            role = await self._binding_roles.lock_current_role(
                active,
                actor_id=receipt.actor_id,
                device_id=action.device_id,
                binding_id=action.binding_id,
                binding_version=action.binding_version,
            )
            authorization = DeviceCommandAuthorization(
                action=action,
                actor_id=receipt.actor_id,
                binding_role=role,
                authority_receipt_id=receipt.receipt_id,
                action_fence_hash=receipt.action_fence_hash,
            )
            result = callback(active, authorization)
            if inspect.isawaitable(result):
                return await result
            return result

        try:
            return await self._sensitive_write.execute(connection, context, authorized)
        except CommandAuthorizationRejected:
            raise
        except (PermissionError, ValueError) as exc:
            raise CommandAuthorizationRejected(str(exc)) from exc


__all__ = [
    "CommandAuthorizationRejected",
    "DeviceBindingRoleAuthorityPort",
    "DeviceCommandAction",
    "DeviceCommandAuthorization",
    "DeviceCommandAuthorityPort",
    "DeviceCommandWriteCallback",
    "DeviceLifecycleAction",
    "DeviceSensitiveAction",
    "PolicyDeviceCommandInput",
    "PolicySensitiveWriteDeviceCommandAuthority",
    "RejectingDeviceCommandAuthority",
    "build_device_command_action",
    "build_device_lifecycle_action",
]
