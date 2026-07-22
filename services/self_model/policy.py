"""The single activation policy for self-model material."""

from __future__ import annotations

from dataclasses import dataclass

from services.self_model.domain import (
    CognitiveClaim,
    DecisionCase,
    RelationshipProfile,
    SelfModelItem,
)

HIGH_SENSITIVITY_CLAIM_TYPES = frozenset(
    {"value", "decision_rule", "red_line", "conflict", "support"}
)


@dataclass(frozen=True, slots=True)
class ActivationDecision:
    effective: bool
    reasons: tuple[str, ...] = ()


def activation_decision(item: SelfModelItem) -> ActivationDecision:
    reasons: list[str] = []
    required_status = "approved" if isinstance(item, RelationshipProfile) else "confirmed"
    if item.status != required_status:
        reasons.append("not_approved")
    if isinstance(item, DecisionCase) and item.kind != "real":
        reasons.append("hypothetical_decision")
    if isinstance(item, DecisionCase) and not item.still_endorsed:
        reasons.append("no_longer_endorsed")
    if item.unresolved_conflict:
        reasons.append("unresolved_conflict")
    if any(source.negative for source in item.sources):
        reasons.append("negative_evidence")
    if not any(
        source.speaker_class == "owner"
        and source.relation == "support"
        and source.adopted
        and not source.negative
        for source in item.sources
    ):
        reasons.append("missing_owner_adopted_source")

    high_sensitivity = (
        isinstance(item, CognitiveClaim) and item.claim_type in HIGH_SENSITIVITY_CLAIM_TYPES
    )
    if isinstance(item, RelationshipProfile) or high_sensitivity:
        if item.owner_reviewed_at is None or not item.step_up_verified:
            reasons.append("step_up_review_required")
    if high_sensitivity and not any(
        source.speaker_class == "owner"
        and source.relation == "counterexample"
        and not source.negative
        for source in item.sources
    ):
        reasons.append("owner_counterexample_required")

    return ActivationDecision(not reasons, tuple(reasons))


def is_effective(item: SelfModelItem) -> bool:
    return activation_decision(item).effective
