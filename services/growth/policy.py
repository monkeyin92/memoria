"""Growth map labels; intentionally no percentage or aggregate score."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from services.persona.domain import LEGACY_COGNITIVE_TRAIT_CATEGORIES

GrowthDimension = Literal[
    "life_chapters",
    "important_people",
    "expression",
    "decision_cases",
    "relationship_models",
    "voice",
    "legacy",
]
GrowthStatus = Literal["empty", "emerging", "supported", "conflicted"]

DIMENSIONS: tuple[GrowthDimension, ...] = (
    "life_chapters",
    "important_people",
    "expression",
    "decision_cases",
    "relationship_models",
    "voice",
    "legacy",
)
DEPENDENCY_PENDING = frozenset(
    {"voice", "legacy"}
)

def version_target_ids(
    dimension: GrowthDimension,
    current_target_ids: set[str],
    manifest: Mapping[str, Any],
) -> tuple[set[str], set[str]]:
    """Return comparable current/manifest IDs for one implemented dimension."""

    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise TypeError("manifest entries are invalid")
    if dimension == "life_chapters":
        frozen = {
            str(entry["claim_id"])
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "memory_claim"
            and "claim_id" in entry
        }
        return set(current_target_ids), frozen
    if dimension == "expression":
        frozen = {
            str(entry["trait_id"])
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "persona_trait"
            and entry.get("category") not in LEGACY_COGNITIVE_TRAIT_CATEGORIES
            and "trait_id" in entry
        }
        return set(current_target_ids), frozen
    if dimension == "decision_cases":
        frozen = {
            str(entry["claim_id"])
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "cognitive_claim"
            and "claim_id" in entry
        }
        frozen.update(
            str(entry["case_id"])
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "decision_case"
            and "case_id" in entry
        )
        return set(current_target_ids), frozen
    if dimension == "relationship_models":
        frozen = {
            f"{entry['profile_id']}@{entry['version_number']}"
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "relationship_profile"
            and "profile_id" in entry
            and "version_number" in entry
        }
        return set(current_target_ids), frozen
    return set(), set()


def apply_negative_evidence(
    dimension: GrowthDimension,
    sources: Sequence[Mapping[str, Any]],
    target_events: Mapping[str, set[str]],
    negatives: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], set[str]]:
    """Find dimension conflicts and the targets eligible for the next manifest."""

    expected_target_kinds = {
        "life_chapters": frozenset({"memory_claim"}),
        "expression": frozenset({"persona_trait"}),
        "decision_cases": frozenset({"cognitive_claim", "decision_case"}),
        "relationship_models": frozenset({"relationship_profile"}),
    }.get(dimension, frozenset())
    effective_targets = set(target_events)
    conflicts: list[Mapping[str, Any]] = []
    for negative in negatives:
        target_kind = str(negative.get("target_kind") or "")
        target_id = str(negative.get("target_id") or "")
        direct_target_match = (
            target_kind in expected_target_kinds
            and target_id in target_events
        )
        source_target_ids = {
            item_id
            for item_id, event_ids in target_events.items()
            if target_kind == "source_event" and target_id in event_ids
        }
        visible_match = any(
            (target_kind, target_id)
            == (
                str(source.get("target_kind") or ""),
                str(source.get("target_id") or ""),
            )
            or (
                target_kind == "source_event"
                and target_id == str(source.get("event_id") or "")
            )
            for source in sources
        )
        if not (direct_target_match or source_target_ids or visible_match):
            continue
        conflicts.append(negative)
        if direct_target_match:
            effective_targets.discard(target_id)
        effective_targets.difference_update(source_target_ids)
        effective_targets.difference_update(
            str(source.get("version_target_id") or "")
            for source in sources
            if (
                target_kind,
                target_id,
            )
            == (
                str(source.get("target_kind") or ""),
                str(source.get("target_id") or ""),
            )
        )
    return conflicts, effective_targets
