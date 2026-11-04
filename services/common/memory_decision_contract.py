"""Shared memory decision, save-receipt, and promise-matrix contract.

Turn policy, session extract, catalog writes, parent display, and companion
recall must agree on these shapes. Async acceptance is not a successful save.
Auto-extract never enters confirmed, and never carries confidence/accuracy 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from services.common.explicit_memory import asks_to_remember, explicit_remember_will_save

MemoryProvenance = Literal[
    "explicit_remember",
    "auto_extract",
    "parent_entry",
    "parent_confirm",
]
MemoryLifecycle = Literal[
    "not_allowed",
    "eligible_unwritten",
    "pending_confirm",
    "confirmed",
    "save_failed",
    "withdrawn",
    "deleted",
]
Polarity = Literal["positive", "negative", "substituted", "unknown"]
WriteMode = Literal["sync_persisted", "async_accepted", "not_written"]
DisplayEligibility = Literal["hidden", "parent_review", "parent_visible", "gone"]
RecallEligibility = Literal["none", "after_confirm", "confirmed_only"]
PromiseLanguage = Literal[
    "must_not_claim_remembered",
    "may_acknowledge_only",
    "may_claim_remembered_after_sync",
]

CONTRACT_VERSION: Final = "memory-decision-v1"
_AUTO_EXTRACT_BIRTH_LIFECYCLES: Final[frozenset[MemoryLifecycle]] = frozenset(
    {
        "not_allowed",
        "eligible_unwritten",
        "pending_confirm",
        "save_failed",
    }
)


class MemoryDecisionContractError(ValueError):
    """Raised when a caller asks for an illegal memory decision or receipt."""


@dataclass(frozen=True, slots=True)
class CandidateFact:
    subject_key: str
    predicate: str
    object_text: str
    polarity: Polarity
    provenance: MemoryProvenance
    rule_version: str
    source_session_id: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        subject = self.subject_key.strip()
        predicate = self.predicate.strip()
        object_text = self.object_text.strip()
        rule_version = self.rule_version.strip()
        if not subject or not predicate or not object_text or not rule_version:
            raise MemoryDecisionContractError("candidate fact fields must be non-empty")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise MemoryDecisionContractError("confidence must be in [0, 1] or None")
        if self.provenance == "auto_extract":
            if self.confidence is not None and self.confidence >= 1.0:
                raise MemoryDecisionContractError(
                    "auto_extract must not claim confidence/accuracy=1.0"
                )
        object.__setattr__(self, "subject_key", subject)
        object.__setattr__(self, "predicate", predicate)
        object.__setattr__(self, "object_text", object_text)
        object.__setattr__(self, "rule_version", rule_version)


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    fact: CandidateFact
    lifecycle: MemoryLifecycle
    record_id: str | None = None
    fact_version: int | None = None

    def __post_init__(self) -> None:
        if self.fact.provenance == "auto_extract" and self.lifecycle == "confirmed":
            raise MemoryDecisionContractError(
                "auto_extract must not be written as confirmed"
            )
        if self.lifecycle in {"pending_confirm", "confirmed"} and not (
            self.record_id and self.record_id.strip()
        ):
            raise MemoryDecisionContractError(
                "pending_confirm and confirmed require a durable record_id"
            )
        if self.fact_version is not None and self.fact_version < 1:
            raise MemoryDecisionContractError("fact_version must be >= 1 when set")


@dataclass(frozen=True, slots=True)
class SaveReceipt:
    decision: MemoryDecision
    write_mode: WriteMode
    reason: str

    def __post_init__(self) -> None:
        reason = self.reason.strip()
        if not reason:
            raise MemoryDecisionContractError("save receipt reason must be non-empty")
        object.__setattr__(self, "reason", reason)
        lifecycle = self.decision.lifecycle
        if self.write_mode == "sync_persisted":
            if lifecycle not in {"pending_confirm", "confirmed"}:
                raise MemoryDecisionContractError(
                    "sync_persisted requires pending_confirm or confirmed"
                )
            if not self.decision.record_id:
                raise MemoryDecisionContractError(
                    "sync_persisted requires a durable record_id"
                )
            return
        if self.write_mode == "async_accepted":
            if lifecycle not in {"eligible_unwritten", "pending_confirm"}:
                raise MemoryDecisionContractError(
                    "async_accepted may only pair with eligible_unwritten or pending_confirm"
                )
            return
        if lifecycle in {"pending_confirm", "confirmed"}:
            raise MemoryDecisionContractError(
                "not_written cannot claim pending_confirm or confirmed"
            )

    def may_claim_saved(self) -> bool:
        return (
            self.write_mode == "sync_persisted"
            and self.decision.lifecycle in {"pending_confirm", "confirmed"}
            and bool(self.decision.record_id)
        )

    def may_claim_remembered_aloud(self) -> bool:
        return (
            self.write_mode == "sync_persisted"
            and self.decision.lifecycle == "confirmed"
            and self.decision.fact.provenance
            in {"explicit_remember", "parent_entry", "parent_confirm"}
        )


def decide_auto_extract(
    *,
    subject_key: str,
    predicate: str,
    object_text: str,
    polarity: Polarity,
    rule_version: str,
    lifecycle: MemoryLifecycle,
    source_session_id: str | None = None,
    confidence: float | None = None,
    record_id: str | None = None,
    fact_version: int | None = None,
) -> MemoryDecision:
    """Build a decision for rule/LLM auto-extract. Confirmed and accuracy=1.0 are illegal."""

    if lifecycle not in _AUTO_EXTRACT_BIRTH_LIFECYCLES:
        raise MemoryDecisionContractError(
            f"auto_extract lifecycle {lifecycle!r} is not allowed at birth"
        )
    fact = CandidateFact(
        subject_key=subject_key,
        predicate=predicate,
        object_text=object_text,
        polarity=polarity,
        provenance="auto_extract",
        rule_version=rule_version,
        source_session_id=source_session_id,
        confidence=confidence,
    )
    return MemoryDecision(
        fact=fact,
        lifecycle=lifecycle,
        record_id=record_id,
        fact_version=fact_version,
    )


def save_receipt(
    *,
    decision: MemoryDecision,
    write_mode: WriteMode,
    reason: str,
) -> SaveReceipt:
    return SaveReceipt(decision=decision, write_mode=write_mode, reason=reason)


@dataclass(frozen=True, slots=True)
class PromiseMatrixRow:
    example_id: str
    utterance: str
    asks: bool
    will_save: bool
    promise: PromiseLanguage
    expected_lifecycle_if_written: MemoryLifecycle
    display: DisplayEligibility
    recall: RecallEligibility
    notes: str


def _row(
    example_id: str,
    utterance: str,
    *,
    minor: bool,
    promise: PromiseLanguage,
    expected_lifecycle_if_written: MemoryLifecycle,
    display: DisplayEligibility,
    recall: RecallEligibility,
    notes: str,
) -> PromiseMatrixRow:
    asks = asks_to_remember(utterance)
    will_save = explicit_remember_will_save(utterance, minor=minor)
    return PromiseMatrixRow(
        example_id=example_id,
        utterance=utterance,
        asks=asks,
        will_save=will_save,
        promise=promise,
        expected_lifecycle_if_written=expected_lifecycle_if_written,
        display=display,
        recall=recall,
        notes=notes,
    )


# Product-visible cases from A00 §4 / A01 close criteria. Values are measured
# against explicit_remember_will_save for minors unless notes say otherwise.
PRODUCT_PROMISE_MATRIX: Final[tuple[PromiseMatrixRow, ...]] = (
    _row(
        "blue-closed-vocab",
        "帮我记住我最喜欢蓝色。",
        minor=True,
        promise="may_claim_remembered_after_sync",
        expected_lifecycle_if_written="confirmed",
        display="parent_visible",
        recall="confirmed_only",
        notes="Closed low-risk preference; will_save True. Spoken remember only after sync_persisted.",
    ),
    _row(
        "dinosaur-not-allowlisted",
        "帮我记住我喜欢恐龙。",
        minor=True,
        promise="must_not_claim_remembered",
        expected_lifecycle_if_written="not_allowed",
        display="hidden",
        recall="none",
        notes="A00 §4: will_save False; companion must inject unsaved note.",
    ),
    _row(
        "auto-extract-preference",
        "我喜欢画画。",
        minor=True,
        promise="must_not_claim_remembered",
        expected_lifecycle_if_written="pending_confirm",
        display="parent_review",
        recall="after_confirm",
        notes="No remember command. Auto-extract may land pending_confirm only, never confirmed.",
    ),
    _row(
        "async-not-saved",
        "帮我记住我最喜欢蓝色。",
        minor=True,
        promise="may_acknowledge_only",
        expected_lifecycle_if_written="eligible_unwritten",
        display="hidden",
        recall="none",
        notes="Same utterance as blue, but write_mode=async_accepted: may_claim_saved is False.",
    ),
)


def promise_matrix_by_id(example_id: str) -> PromiseMatrixRow:
    for row in PRODUCT_PROMISE_MATRIX:
        if row.example_id == example_id:
            return row
    raise KeyError(example_id)


def display_for(lifecycle: MemoryLifecycle) -> DisplayEligibility:
    if lifecycle == "pending_confirm":
        return "parent_review"
    if lifecycle == "confirmed":
        return "parent_visible"
    if lifecycle in {"withdrawn", "deleted"}:
        return "gone"
    return "hidden"


def recall_for(lifecycle: MemoryLifecycle, *, provenance: MemoryProvenance) -> RecallEligibility:
    if lifecycle == "confirmed" and provenance != "auto_extract":
        return "confirmed_only"
    if lifecycle == "pending_confirm":
        return "after_confirm"
    return "none"
