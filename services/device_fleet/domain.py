"""Device Fleet domain vocabulary and fail-closed public results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceAttestation,
    DeviceTrust,
    RemoteDeviceCommand,
)


class DeviceFleetError(RuntimeError):
    """Base error for authoritative Device Fleet operations."""


class DeviceFleetConflict(DeviceFleetError):
    """The requested lifecycle change conflicts with current authority state."""


class AttestationRejected(DeviceFleetError):
    """A device attestation failed a fail-closed authority check."""

    def __init__(self) -> None:
        super().__init__("attestation rejected")


class RemoteCommandRejected(DeviceFleetError):
    """A signed remote command failed an exact execution fence."""

    def __init__(self) -> None:
        super().__init__("remote command rejected")


class OtaRejected(DeviceFleetError):
    """An OTA assignment or state transition failed closed."""

    def __init__(self) -> None:
        super().__init__("OTA rejected")


@dataclass(frozen=True, slots=True)
class DeviceFleetContext:
    """Exact RLS scope for one bound device."""

    device_id: str
    binding_id: str
    binding_version: int
    family_space_id: str

    def __post_init__(self) -> None:
        for field_name in ("device_id", "binding_id", "family_space_id"):
            value = getattr(self, field_name)
            if not value or len(value) > 128:
                raise ValueError(f"{field_name} must be a short non-empty string")
        if self.binding_version < 1:
            raise ValueError("binding_version must be positive")


@dataclass(frozen=True, slots=True)
class DeviceTrustAuthorityFacts:
    """Locked device facts for Policy evaluation; never a permission grant."""

    family_space_id: str
    binding_id: str
    binding_version: int
    trust: DeviceTrust
    reasons: tuple[str, ...]
    attestation: DeviceAttestation | None
    accepted_at: datetime | None
    certificate_id: str | None
    certificate_status: str | None
    server_sim_status: str
    attested_sim_status: str


@dataclass(frozen=True, slots=True)
class SimAuthorityFacts:
    """Server-owned carrier/profile state; never derived from device claims."""

    sim_id: str
    provider: str
    profile_kind: str
    provider_status: str
    status: str
    revision: int
    iccid: str | None = None
    esim_profile_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeviceLifecycleAuthorityFacts:
    """Current lifecycle facts; hardware fields remain attestations, not proof of lab acceptance."""

    device_id: str
    family_space_id: str
    binding_id: str
    binding_version: int
    lifecycle_status: str
    certificate_id: str | None
    certificate_status: str | None
    physical_mute_state: str
    privacy_light_state: str


@dataclass(frozen=True, slots=True)
class RemoteCommandState:
    command: RemoteDeviceCommand
    status: str
    accepted_at: datetime | None
    dispatched_at: datetime | None
    device_acknowledged_at: datetime | None
    hardware_confirmed_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class RemoteCommandDispatch:
    command: RemoteDeviceCommand
    lease_token: str
    lease_until: datetime
    attempt_count: int


@dataclass(frozen=True, slots=True)
class CommandDispatchReadiness:
    ready: bool
    accepted_not_dispatched: int
    last_dispatcher_heartbeat_at: datetime | None


@dataclass(frozen=True, slots=True)
class CommandReconcileResult:
    requeued: int
    uncertain: int


__all__ = [
    "AttestationRejected",
    "DeviceFleetConflict",
    "DeviceFleetContext",
    "DeviceFleetError",
    "DeviceLifecycleAuthorityFacts",
    "CommandDispatchReadiness",
    "CommandReconcileResult",
    "DeviceTrustAuthorityFacts",
    "RemoteCommandRejected",
    "RemoteCommandDispatch",
    "RemoteCommandState",
    "SimAuthorityFacts",
    "OtaRejected",
]
