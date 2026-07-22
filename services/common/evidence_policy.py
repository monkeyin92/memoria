"""One conservative policy for evidence that may shape the owner's record."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from services.archive.domain import EvidenceEvent

EvidenceWeight = Literal["strong", "normal", "weak"]

_POSITIVE_ACTIONS = frozenset({"reflection", "choice", "decision_review", "correction"})
_NEGATIVE_ACTIONS = frozenset({"not_me", "would_not_say"})
_PROMPT_WEIGHTS: dict[str, tuple[EvidenceWeight, float]] = {
    "spontaneous": ("strong", 1.0),
    "open": ("normal", 0.7),
    "structured": ("weak", 0.35),
    "leading": ("weak", 0.35),
}
_LEADING_PATTERNS = (
    "是不是",
    "难道",
    "对不对",
    "对吗",
    "对吧",
    "是吧",
    "你应该",
)
_STRUCTURED_PATTERNS = (
    "还是",
    "二选一",
    "选择哪个",
    "选一个",
    "请选择",
)
_OPEN_PATTERNS = (
    "什么",
    "如何",
    "怎么",
    "为什么",
    "哪",
    "多少",
    "何时",
    "谁",
    "请说",
    "说说",
    "聊聊",
    "讲讲",
    "回想",
    "描述",
    "告诉我",
)


@dataclass(frozen=True, slots=True)
class Contribution:
    accepted: bool
    reason: str
    weight: EvidenceWeight = "weak"
    factor: float = 0.0


def classify_prompt_kind(text: str) -> str:
    value = text.strip()
    if any(pattern in value for pattern in _LEADING_PATTERNS):
        return "leading"
    if any(pattern in value for pattern in _STRUCTURED_PATTERNS):
        return "structured"
    if value.endswith(("?", "？")) or any(
        pattern in value for pattern in _OPEN_PATTERNS
    ):
        return "open"
    return "spontaneous"


def prompt_weight_for(payload: Mapping[str, object]) -> tuple[EvidenceWeight, float]:
    return _PROMPT_WEIGHTS.get(
        str(payload.get("prompt_kind") or "spontaneous"),
        ("weak", 0.35),
    )


def contribution_for(event: EvidenceEvent) -> Contribution:
    """Return the sole positive-learning decision for an immutable ledger event."""
    payload = event.payload
    if event.speaker_class != "owner":
        return Contribution(False, "not_owner")
    if payload.get("simulated_output") is True or payload.get("interaction_mode") not in {
        None,
        "companion",
    }:
        return Contribution(False, "simulated_or_non_companion")
    if payload.get("owner_projection_eligible") is not True:
        return Contribution(False, "owner_projection_ineligible")
    if event.event_type == "speech.utterance_finalized":
        kind, factor = prompt_weight_for(payload)
        return Contribution(True, "accepted", kind, factor)
    if event.event_type == "owner.action_recorded":
        action = str(payload.get("action_type") or "")
        if action in _NEGATIVE_ACTIONS:
            return Contribution(False, "negative_action")
        if action not in _POSITIVE_ACTIONS:
            return Contribution(False, "action_not_whitelisted")
        kind, factor = prompt_weight_for(
            {
                **payload,
                "prompt_kind": payload.get("prompt_kind") or "structured",
            }
        )
        return Contribution(True, "accepted", kind, factor)
    return Contribution(False, "unsupported_event_type")


def confirmed_projection_contribution_for(event: EvidenceEvent) -> Contribution:
    """Preserve confirmed pre-policy projections without weakening new ingestion."""

    contribution = contribution_for(event)
    if contribution.accepted or contribution.reason != "owner_projection_ineligible":
        return contribution
    payload = event.payload
    if (
        "owner_projection_eligible" in payload
        or event.speaker_class != "owner"
        or event.event_type != "speech.utterance_finalized"
        or payload.get("simulated_output") is True
        or payload.get("interaction_mode") not in {None, "companion"}
    ):
        return contribution
    return Contribution(True, "legacy_confirmed_projection", "normal", 0.7)
