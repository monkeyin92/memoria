"""Owner data authority from the device binding, checked against the signed profile.

A device serves the one person it is bound to. With no voiceprint running, the
Agent claims ``owner`` with :data:`DEVICE_BOUND_SUBJECT_REASON` for that person;
Control honours the claim only when the session's current Runtime Profile itself
confirms the bound subject and grants private recall. Every other speaker
decision passes through unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping

from services.speaker.domain import DEVICE_BOUND_SUBJECT_REASON

__all__ = [
    "DEVICE_BOUND_SUBJECT_REASON",
    "DEVICE_BOUND_SUBJECT_UNVERIFIED",
    "claims_device_bound_subject",
    "runtime_profile_trusts_bound_subject",
]

DEVICE_BOUND_SUBJECT_UNVERIFIED = "device_bound_subject_unverified"


def claims_device_bound_subject(classification: str, reason_code: str | None) -> bool:
    return classification == "owner" and reason_code == DEVICE_BOUND_SUBJECT_REASON


def runtime_profile_trusts_bound_subject(
    profile: Mapping[str, object] | None,
    *,
    subject_id: str | None = None,
) -> bool:
    """Whether this signed profile makes its active subject the device's owner."""

    if profile is None:
        return False
    active = profile.get("active_subject_id")
    capabilities = profile.get("capabilities")
    return (
        isinstance(active, str)
        and bool(active.strip())
        and active != "unknown"
        and (subject_id is None or subject_id == active)
        and profile.get("speaker_state") == "confirmed"
        and profile.get("service_mode") not in {None, "unknown_safe"}
        and isinstance(capabilities, (list, tuple))
        and "memory_recall_private" in capabilities
    )
