"""How the Session Runtime reads the Device trust authority's answer.

``action_device_lock_trust`` answers with a trust level and, additively, the
fact ``device_bound``: onboarding activated this device with its own key and it
is currently bound to exactly this binding, but no hardware attestation backs
that.  ``device_bound`` is not a trust level.  Policy tiers the levels:

* ``verified`` - a fresh, signed hardware attestation.  Every sensitive
  capability may rely on it.
* ``trusted`` - the bound link alone.  Only the memory capabilities in
  ``services.policy.engine.BOUND_DEVICE_CAPABILITIES`` may rely on it.

The engine treats ``trusted`` as the weaker tier, so it must never reach code
that predates that rule.  The authority therefore never answers ``trusted``
itself: this module upgrades ``untrusted`` + ``device_bound`` to ``trusted``
only when the deployment opted in (``MEMORIA_BOUND_DEVICE_TRUST_ENABLED``).
Nothing that predates the opt-in sees ``trusted``, and turning it off is an
environment change with no schema step.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

TRUST_LEVELS: Final = frozenset({"trusted", "verified", "offline", "untrusted", "revoked"})
BOUND_DEVICE_REASON: Final = "device_bound_no_attestation"


@dataclass(frozen=True, slots=True)
class DeviceTrustSnapshot:
    available: bool
    trust: str
    reason_code: str


def snapshot_from_authority(
    payload: Mapping[str, object], *, accept_bound_device: bool
) -> DeviceTrustSnapshot:
    """Validate the authority's answer and apply the opt-in bound-device tier.

    Raises ``ValueError`` when the answer is not one the contract allows, so
    the caller can fail closed.  Only a strict JSON ``true`` counts as
    ``device_bound``; an absent or malformed fact never upgrades anything.
    """

    available = payload.get("available")
    trust = payload.get("device_trust")
    if not isinstance(available, bool) or trust not in TRUST_LEVELS:
        raise ValueError("Device trust authority is invalid")
    reason = payload.get("reason_code")
    if (
        accept_bound_device
        and available
        and trust == "untrusted"
        and payload.get("device_bound") is True
    ):
        return DeviceTrustSnapshot(available=True, trust="trusted", reason_code=BOUND_DEVICE_REASON)
    return DeviceTrustSnapshot(
        available=available,
        trust=str(trust),
        reason_code=reason if isinstance(reason, str) else "unknown",
    )
