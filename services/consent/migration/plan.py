"""Dry-run migration plan: statistics, deterministic mapping, and checksum.

``build_plan`` is a pure computation: it never opens, reads, or writes any
store.  Every input row maps to exactly one item — an ``offer_candidate``
(old id -> canonical consent id) or a ``quarantine`` (old id -> quarantine id).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from services.consent.migration.adapters import RawLegacyConsent
from services.consent.migration.normalizer import canonical_digest, normalize_rows


@dataclass(frozen=True, slots=True)
class PlanItem:
    """One deterministic old-id -> canonical-id mapping."""

    old_id: str
    new_id: str
    action: Literal["offer_candidate", "quarantine"]
    reason: str | None
    row_checksum: str


@dataclass(frozen=True, slots=True)
class DryRunPlan:
    """Immutable dry-run result; carrying it performs no side effects."""

    input_count: int
    provable_count: int
    quarantine_count: int
    items: tuple[PlanItem, ...]
    checksum: str


def _plan_checksum(items: list[PlanItem]) -> str:
    payload = [
        {
            "old_id": item.old_id,
            "new_id": item.new_id,
            "action": item.action,
            "reason": item.reason,
            "row_checksum": item.row_checksum,
        }
        for item in items
    ]
    return canonical_digest(payload)


def build_plan(
    rows: Iterable[RawLegacyConsent],
    *,
    now: datetime | None = None,
) -> DryRunPlan:
    """Build a dry-run plan from legacy rows (pure; no store I/O).

    The API accepts only rows — there is no store parameter at all, which makes
    the no-side-effect contract structural rather than conventional.
    """
    result = normalize_rows(rows, now=now)
    items: list[PlanItem] = [
        PlanItem(
            old_id=consent.legacy_id,
            new_id=consent.consent_id,
            action="offer_candidate",
            reason=None,
            row_checksum=consent.canonical_hash,
        )
        for consent in result.normalized
    ]
    items.extend(
        PlanItem(
            old_id=entry.legacy_id,
            new_id=entry.quarantine_id,
            action="quarantine",
            reason=entry.reason,
            row_checksum=entry.canonical_hash,
        )
        for entry in result.quarantined
    )
    items.sort(key=lambda item: item.old_id)
    return DryRunPlan(
        input_count=len(items),
        provable_count=len(result.normalized),
        quarantine_count=len(result.quarantined),
        items=tuple(items),
        checksum=_plan_checksum(items),
    )
