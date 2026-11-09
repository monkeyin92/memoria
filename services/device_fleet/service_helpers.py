"""Small payload helpers shared by ``DeviceFleetService`` and its seams.

The facade owns wiring and policy ordering; these functions only decode
database scalars, canonicalize command parameters and clamp version strings.
They live outside the facade so the trust seam and the remaining lifecycle
code share one definition.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packages.contracts.generated.python.multi_subject_contracts import (
    OtaSlotName,
    OtaSlotState,
    OtaStateReceipt,
    RemoteDeviceCommandType,
)

from services.device_fleet.domain import DeviceFleetConflict, OtaRejected


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
