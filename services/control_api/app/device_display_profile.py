"""Which mascot a bound device should show, for its idle display poll.

The answer is the persona the device's *next* session would speak as for the
binding's primary subject -- subject override first, binding default second,
the same order the Session runtime locks -- mapped onto the built-in companions
that ship mascot art.  Anything without art (a custom ``cu_*`` persona, a tutor
persona, an unknown id) shows its base built-in companion when it has one, else
the account's own companion, else the shipped default.

Every read here is a plain read: the firmware polls this every ~20 s, so it
writes nothing and audits nothing.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

from services.common.companions import DEFAULT_COMPANION_ID
from services.control_api.app.multi_subject_runtime import _persona_assignment_for
from services.control_api.app.session_companion import CUSTOM_PERSONA_PREFIX
from services.device_fleet.bootstrap_domain import (
    BindingConflict,
    BindingRecord,
    DeviceLifecycle,
    DeviceNotFound,
    DeviceRecord,
    DeviceRevoked,
    InvalidDeviceProof,
    public_key_from_bytes,
    verify_signed_payload,
)
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.identity.domain import (
    IdentityAccessDeniedError,
    IdentityNotFoundError,
)
from services.identity.service import IdentityService

#: Built-in companions that ship on-device mascot art.  The tutor personas
#: (zhiyao, yanxi) and custom personas have none, so a device display falls
#: back to one of these.  Kept here rather than in services/common so the
#: Control component can ship without touching code other images import.
MASCOT_COMPANION_IDS: frozenset[str] = frozenset(
    {"starlight", "taoxi", "mianmian", "axu", "xuanmo"}
)

DISPLAY_PROFILE_SCHEMA_VERSION = 1
_DISPLAY_VERSION_DOMAIN = b"memoria-device-display-profile-v1\0"


class DisplayBindingUnavailable(LookupError):
    """The device is bound in the fleet but Identity has no active binding."""


def display_request_payload(*, device_id: str, certificate_id: str) -> dict[str, object]:
    """The object a device signs for the poll: the manifest's shape, this path.

    The signature binds method, exact path, device and certificate, so a
    manifest signature never authenticates this endpoint or the reverse.
    """
    return {
        "method": "GET",
        "path": f"/v1/devices/{device_id}/display-profile",
        "device_id": device_id,
        "certificate_id": certificate_id,
    }


def display_binding(
    service: DeviceOnboardingService,
    *,
    device_id: str,
    certificate_id: str,
    request_signature: bytes | None,
) -> dict[str, str]:
    """Authenticate a display poll and say who the bound device serves.

    Same checks, in the same order, as
    ``DeviceOnboardingService.get_activation_manifest`` (certificate, lifecycle,
    signature) but strictly read-only: no download mark, no counter, no audit
    row, so a device may poll it every few seconds.
    """
    store = service.store
    device = cast(DeviceRecord | None, store.get_device(device_id))  # type: ignore[attr-defined]
    if device is None:
        raise DeviceNotFound()
    if device.certificate_id != certificate_id:
        raise InvalidDeviceProof("certificate does not belong to device")
    if device.lifecycle_status is DeviceLifecycle.REVOKED:
        raise DeviceRevoked()
    if device.lifecycle_status is not DeviceLifecycle.BOUND:
        raise BindingConflict("device is not bound")
    if request_signature is None and not service.offline_mock:
        raise InvalidDeviceProof("device request signature is required")
    if request_signature is not None:
        verify_signed_payload(
            public_key=public_key_from_bytes(device.public_key),
            payload=display_request_payload(device_id=device_id, certificate_id=certificate_id),
            signature=request_signature,
        )
    if device.actor_id is None or device.binding_id is None:
        raise BindingConflict("device has no active binding")
    binding = cast(BindingRecord | None, store.get_binding(device.binding_id))  # type: ignore[attr-defined]
    subject_id = (
        str(binding.initialization.primary_subject.get("person_id", ""))
        if binding is not None
        else ""
    )
    return {
        "device_id": device.device_id,
        "actor_id": device.actor_id,
        "binding_id": device.binding_id,
        "subject_id": subject_id,
    }


class _ProfileReader(Protocol):
    def get_subject_profile(self, *, user_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class DeviceDisplayProfile:
    device_id: str
    binding_id: str
    persona_id: str
    companion_id: str

    @property
    def display_version(self) -> str:
        """Short, opaque, and different whenever the shown companion changes."""
        digest = hashlib.sha256(
            _DISPLAY_VERSION_DOMAIN + f"{self.binding_id}\0{self.companion_id}".encode()
        ).hexdigest()
        return digest[:16]

    def to_wire(self) -> dict[str, object]:
        return {
            "schema_version": DISPLAY_PROFILE_SCHEMA_VERSION,
            "device_id": self.device_id,
            "companion_id": self.companion_id,
            "display_version": self.display_version,
        }


def _mascot_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value in MASCOT_COMPANION_IDS else None


async def mascot_for_persona(
    persona_id: str,
    *,
    identity: IdentityService | None,
    profiles: _ProfileReader | None,
    owner_id: str,
    actor_id: str,
) -> str:
    """Map a resolved persona onto a companion that has mascot art."""
    builtin = _mascot_or_none(persona_id)
    if builtin is not None:
        return builtin
    if persona_id.startswith(CUSTOM_PERSONA_PREFIX) and identity is not None:
        try:
            record = await identity.get_custom_persona(
                persona_id,
                owner_person_id=owner_id,
                actor_person_id=actor_id,
            )
        except (IdentityNotFoundError, IdentityAccessDeniedError):
            record = None
        base = _mascot_or_none(record.fallback_designed_voice if record is not None else None)
        if base is not None:
            return base
    if profiles is not None:
        row = profiles.get_subject_profile(user_id=owner_id)
        account = _mascot_or_none((row or {}).get("companion_id"))
        if account is not None:
            return account
    return DEFAULT_COMPANION_ID


async def resolve_device_display_profile(
    *,
    identity: IdentityService,
    profiles: _ProfileReader | None,
    device_id: str,
    actor_id: str,
    subject_id: str,
    now: datetime,
) -> DeviceDisplayProfile:
    """The companion the device's next session would show for its subject."""
    manifest = await identity.get_active_manifest(device_id, now, actor_person_id=actor_id)
    if manifest is None:
        raise DisplayBindingUnavailable(device_id)
    primary = tuple(sorted(manifest.primary_subject_ids))
    subject = subject_id if subject_id in primary else (primary[0] if primary else None)
    overrides: dict[str, str] = {}
    if subject is not None:
        record = await identity.get_persona_assignment(
            binding_id=manifest.binding_id,
            subject_id=subject,
            actor_person_id=actor_id,
        )
        if record is not None:
            overrides[subject] = record.assignment_id
    assignment_id = _persona_assignment_for(
        binding_default=manifest.persona_assignment_id,
        overrides=overrides,
        subject_id=subject,
    )
    persona_id = assignment_id.partition(":v")[0]
    companion_id = await mascot_for_persona(
        persona_id,
        identity=identity,
        profiles=profiles,
        owner_id=manifest.account_owner_id,
        actor_id=actor_id,
    )
    return DeviceDisplayProfile(
        device_id=device_id,
        binding_id=manifest.binding_id,
        persona_id=persona_id,
        companion_id=companion_id,
    )


__all__ = [
    "DISPLAY_PROFILE_SCHEMA_VERSION",
    "DeviceDisplayProfile",
    "DisplayBindingUnavailable",
    "mascot_for_persona",
    "resolve_device_display_profile",
]
