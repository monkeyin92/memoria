"""Conservative promotion policy for explicit owner memory requests."""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import MemoryExtraction
from services.common.evidence_policy import contribution_for
from services.common.explicit_memory import (
    contains_sensitive_text,
    explicit_remember_content,
    low_risk_self_fact_predicate,
    minor_sensitive_context,
)

EXPLICIT_MEMORY_POLICY_VERSION = "explicit-memory-v2"
EXPLICIT_MEMORY_CONFIRM_REASON = "explicit-memory-low-risk"
EXPLICIT_MEMORY_INTENT = {
    "kind": "explicit_remember",
    "policy_version": EXPLICIT_MEMORY_POLICY_VERSION,
}
POLICY_CONFIRMATION_SOURCE = "system.memory_write_policy"
SINGLE_VALUE_PREDICATES = frozenset({"age", "birth_date", "birth_place"})
LOW_RISK_AUTO_CONFIRM_PREDICATES = frozenset({"preference", "habit"})
MINOR_LONG_TERM_DOMAIN_ALLOWLIST = frozenset(
    {"study_progress", "learning_preference", "daily_life"}
)
#: What the extractor's own confidence must reach before a request confirms itself.
AUTO_CONFIRM_MIN_CONFIDENCE = 0.9


def has_exact_explicit_memory_intent(payload: Mapping[str, object]) -> bool:
    raw = payload.get("memory_write_intent")
    return isinstance(raw, Mapping) and dict(raw) == EXPLICIT_MEMORY_INTENT


def is_policy_confirmation_event(event: EvidenceEvent) -> bool:
    payload = event.payload
    return bool(
        event.event_type == "memory.claim_reviewed"
        and event.source == POLICY_CONFIRMATION_SOURCE
        and payload.get("action") == "confirm"
        and payload.get("policy_version") == EXPLICIT_MEMORY_POLICY_VERSION
        and payload.get("reason") == EXPLICIT_MEMORY_CONFIRM_REASON
        and str(payload.get("target_id") or "").strip()
        and str(payload.get("source_event_id") or "").strip()
    )


def _normalized_fact(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())


def _traceable_to(value: str, source: str) -> bool:
    fact = _normalized_fact(value)
    original = _normalized_fact(source)
    return len(fact) >= 2 and bool(original) and (fact in original or original in fact)


def filter_extraction_for_subject(
    event: EvidenceEvent,
    extraction: MemoryExtraction,
    *,
    subject_category: str | None,
) -> MemoryExtraction:
    """Keep the first-release minor long-term projection deliberately narrow."""

    if subject_category != "minor":
        return extraction
    text = str(event.payload.get("text") or "").strip()
    if not text or contains_sensitive_text(text) or minor_sensitive_context(text):
        return MemoryExtraction(
            extractor_version=extraction.extractor_version,
            usage=extraction.usage,
        )
    low_risk_predicate = low_risk_self_fact_predicate(explicit_remember_content(text) or text)
    low_risk_daily = low_risk_predicate is not None
    claims = tuple(
        claim
        for claim in extraction.claims
        if claim.subject_key == "self"
        and not claim.entity_keys
        and claim.domain_category in MINOR_LONG_TERM_DOMAIN_ALLOWLIST
        and (
            claim.domain_category != "daily_life"
            or (low_risk_daily and claim.predicate in {low_risk_predicate, "daily_life"})
        )
        and str(claim.sensitive_domain).casefold() in {"public", "personal"}
        and not contains_sensitive_text(claim.value)
        and not minor_sensitive_context(claim.value)
    )
    timeline = tuple(
        item
        for item in extraction.timeline
        if not item.participant_keys
        and item.domain_category in MINOR_LONG_TERM_DOMAIN_ALLOWLIST
        and (item.domain_category != "daily_life" or low_risk_daily)
        and item.sensitivity in {"public", "personal"}
        and not contains_sensitive_text(item.title)
        and not minor_sensitive_context(item.title)
    )
    knowledge = tuple(
        item
        for item in extraction.knowledge
        if not item.entity_keys
        and item.domain_category in {"study_progress", "learning_preference"}
        and item.sensitivity in {"public", "personal"}
        and not contains_sensitive_text(item.question)
        and not contains_sensitive_text(item.answer)
        and not minor_sensitive_context(item.question)
        and not minor_sensitive_context(item.answer)
    )
    return MemoryExtraction(
        claims=claims,
        timeline=timeline,
        knowledge=knowledge,
        extractor_version=extraction.extractor_version,
        usage=extraction.usage,
    )


def _complete_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True, slots=True)
class MemoryWriteDecision:
    confirmed: bool
    reason: str
    content: str | None = None

    def confirmation_event(
        self,
        *,
        source: EvidenceEvent,
        claim_id: str,
        claim_value: str,
    ) -> EvidenceEvent:
        if not self.confirmed:
            raise ValueError("candidate memory decisions cannot create confirmation evidence")
        event_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    "memoria:memory-write-policy:"
                    f"{EXPLICIT_MEMORY_POLICY_VERSION}:{source.event_id}:{claim_id}"
                ),
            )
        )
        return EvidenceEvent(
            event_id=event_id,
            account_id=source.account_id,
            session_id=source.session_id,
            turn_id=source.turn_id,
            generation_id=source.generation_id,
            event_type="memory.claim_reviewed",
            occurred_at=source.occurred_at,
            speaker_class="system",
            source=POLICY_CONFIRMATION_SOURCE,
            # The confirmation is evidence of the same speaker.  Leaving this
            # blank makes the review an unclaimed source on the claim's search
            # document, so a later speaker's self-claim looks merged with
            # nobody and can no longer confirm.
            subject_id=source.subject_id,
            payload={
                "target_id": claim_id,
                "action": "confirm",
                "previous_value": claim_value,
                "source_event_id": source.event_id,
                "policy_version": EXPLICIT_MEMORY_POLICY_VERSION,
                "reason": self.reason,
                "tool_epoch": source.payload.get("tool_epoch"),
            },
        )


class MemoryWritePolicy:
    """Promote only explicit, attributable, low-risk single-owner facts."""

    def decide(
        self,
        event: EvidenceEvent,
        extraction: MemoryExtraction,
        *,
        existing_values: Collection[str] = (),
        subject_category: str | None = None,
    ) -> MemoryWriteDecision:
        if not has_exact_explicit_memory_intent(event.payload):
            return MemoryWriteDecision(False, "explicit_intent_missing")
        contribution = contribution_for(event)
        if (
            not contribution.accepted
            or event.event_type != "speech.utterance_finalized"
            or not (event.session_id or "").strip()
            or not _complete_nonnegative_int(event.turn_id)
            or not _complete_nonnegative_int(event.generation_id)
            or not _complete_nonnegative_int(event.payload.get("tool_epoch"))
        ):
            return MemoryWriteDecision(False, "untrusted_owner_fence")
        content = explicit_remember_content(event.payload.get("text"))
        if content is None:
            return MemoryWriteDecision(False, "invalid_explicit_command")
        if subject_category == "minor" and contains_sensitive_text(content):
            return MemoryWriteDecision(False, "minor_long_term_boundary", content)
        low_risk_predicate = low_risk_self_fact_predicate(content)
        if low_risk_predicate is None:
            return MemoryWriteDecision(False, "auto_confirm_not_allowlisted", content)
        if len(extraction.claims) != 1:
            return MemoryWriteDecision(False, "single_claim_required", content)
        claim = extraction.claims[0]
        if (
            claim.subject_key != "self"
            or claim.entity_keys
            or extraction.people
            or extraction.relationships
            or any(item.participant_keys for item in extraction.timeline)
            or any(item.entity_keys for item in extraction.knowledge)
        ):
            return MemoryWriteDecision(False, "self_claim_required", content)
        if (
            claim.predicate not in LOW_RISK_AUTO_CONFIRM_PREDICATES
            or claim.predicate != low_risk_predicate
        ):
            return MemoryWriteDecision(False, "auto_confirm_predicate_mismatch", content)
        # The child's own "帮我记住…" is a command, not an answer to the robot's last line, so the
        # prompt-kind discount (open 0.7, structured and leading 0.35) does not apply to it: with
        # it every request after the first turn scored 0.63 and stayed a candidate (2026-10-02).
        if claim.confidence < AUTO_CONFIRM_MIN_CONFIDENCE:
            return MemoryWriteDecision(False, "insufficient_confidence", content)
        if not _traceable_to(claim.value, content):
            return MemoryWriteDecision(False, "claim_not_traceable", content)
        sensitivity_values = (
            claim.sensitive_domain,
            *(item.sensitivity for item in extraction.timeline),
            *(item.sensitivity for item in extraction.knowledge),
        )
        text_values = (
            str(event.payload.get("text") or ""),
            content,
            claim.value,
            *(item.title for item in extraction.timeline),
            *(item.question for item in extraction.knowledge),
            *(item.answer for item in extraction.knowledge),
        )
        if any(
            str(value).strip().casefold() not in {"public", "personal"}
            for value in sensitivity_values
        ):
            return MemoryWriteDecision(False, "sensitive_or_unknown_domain", content)
        if any(contains_sensitive_text(value) for value in text_values):
            return MemoryWriteDecision(False, "sensitive_content", content)
        if claim.predicate in SINGLE_VALUE_PREDICATES and any(
            str(value) != claim.value for value in existing_values
        ):
            return MemoryWriteDecision(False, "conflicting_value", content)
        return MemoryWriteDecision(True, EXPLICIT_MEMORY_CONFIRM_REASON, content)
