"""Shared minor memory-retention ceiling for runtime and archive consumers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def memory_retention_allowed(
    *,
    subject_category: object,
    active_consent: bool,
) -> bool:
    if subject_category == "adult":
        return True
    if subject_category == "minor":
        return active_consent
    return False


def apply_memory_retention_ceiling(
    context: Mapping[str, Any],
    *,
    allowed: bool,
) -> dict[str, Any]:
    result = dict(context)
    if allowed:
        return result
    capabilities = dict(result.get("capabilities") or {})
    for key in (
        "private_memory",
        "persona",
        "persona_low_sensitivity",
        "history",
        "learning",
    ):
        capabilities[key] = False
    result.update(
        {
            "history_eligible": False,
            "owner_projection_eligible": False,
            "capabilities": capabilities,
            "memory_retention": "ephemeral_only",
        }
    )
    return result


__all__ = ["apply_memory_retention_ceiling", "memory_retention_allowed"]
