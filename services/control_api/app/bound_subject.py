"""Device-bound speaker claims are never trusted for private authority (A06).

A signed Runtime Profile proves device↔subject config and consent-derived
capabilities. It does not prove the current speaker is that subject. Any
``owner`` claim with :data:`DEVICE_BOUND_SUBJECT_REASON` is demoted at Control
boundaries until a stronger confirm exists (product-defined; not voiceprint).
Every other speaker decision passes through unchanged.
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
    """Bind/config profile never counts as turn-level speaker trust for private ops."""

    del profile, subject_id
    return False
