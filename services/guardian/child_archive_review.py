"""Parent confirm / correct / withdraw for child-archive whitelist facts.

Append-only: confirm and withdraw add status events. Correct inserts a new
record and revokes the old one. Display labels never call auto-extract
confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal, Mapping

from services.guardian.child_memory import ARCHIVE_FIELDS, ArchiveField

ArchiveReviewAction = Literal["confirm", "correct", "withdraw"]
ArchiveStorageStatus = Literal["candidate", "confirmed", "disputed", "revoked"]
ArchiveDisplayLifecycle = Literal[
    "auto_pending",
    "pending_confirm",
    "parent_confirmed",
    "withdrawn",
    "expired",
]
ArchivePolarity = Literal["positive", "negative", "substituted", "unknown"]

DISPLAY_LABELS: Final[dict[ArchiveDisplayLifecycle, str]] = {
    "auto_pending": "自动提取·待确认",
    "pending_confirm": "待确认",
    "parent_confirmed": "家长已确认",
    "withdrawn": "已撤回",
    "expired": "已过期",
}

PARENT_CONFIRM_REASONS: Final[frozenset[str]] = frozenset(
    {
        "parent_confirm",
        "parent_correct",
        "parent_correct_history",
        "child_archive_fact",
    }
)
AUTO_BIRTH_REASONS: Final[frozenset[str]] = frozenset({"child_archive_auto_extract"})


class ChildArchiveReviewError(ValueError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChildArchiveFactView:
    record_id: str
    field: str
    text: str
    status: ArchiveStorageStatus
    display_lifecycle: ArchiveDisplayLifecycle
    display_label: str
    provenance: str | None
    source_session_id: str | None
    rule_version: str | None
    fact_version: int
    polarity: ArchivePolarity | None
    confidence: float | None
    handled_at: datetime | None
    reason_code: str | None
    created_at: datetime
    last_mentioned_at: str | None
    supersedes_record_id: str | None
    recall_eligible: bool


@dataclass(frozen=True, slots=True)
class ChildArchiveReviewCommand:
    action: ArchiveReviewAction
    record_id: str
    actor_id: str
    text: str | None = None
    field: ArchiveField | None = None
    polarity: ArchivePolarity | None = None
    history_change: bool = False


@dataclass(frozen=True, slots=True)
class ChildArchiveReviewReceipt:
    action: ArchiveReviewAction
    record_id: str
    status: ArchiveStorageStatus
    display_lifecycle: ArchiveDisplayLifecycle
    display_label: str
    reviewed_at: datetime
    audit_event_id: str
    old_record_id: str | None = None
    new_record_id: str | None = None
    old_version: int | None = None
    new_version: int | None = None
    failure_reason: str | None = None


def display_lifecycle(
    *,
    status: str,
    reason_code: str | None,
    provenance: str | None,
    retention_expires_at: datetime | None,
    now: datetime | None = None,
) -> ArchiveDisplayLifecycle:
    current = now or datetime.now(UTC)
    if retention_expires_at is not None and retention_expires_at <= current:
        return "expired"
    if status == "revoked":
        return "withdrawn"
    if status == "confirmed":
        if reason_code in PARENT_CONFIRM_REASONS:
            return "parent_confirmed"
        if provenance == "auto_extract":
            return "auto_pending"
        return "parent_confirmed"
    if provenance == "auto_extract" or reason_code in AUTO_BIRTH_REASONS:
        return "auto_pending"
    return "pending_confirm"


def recall_eligible(lifecycle: ArchiveDisplayLifecycle) -> bool:
    """Default dialogue recall may use parent-confirmed facts only.

    Candidate / auto-pending / withdrawn / expired stay out. A05 owns proof
    that a new session and caches honor withdraw/delete.
    """

    return lifecycle == "parent_confirmed"


def build_fact_view(
    *,
    record_id: str,
    field: str,
    text: str,
    created_at: datetime,
    retention_expires_at: datetime | None,
    payload: Mapping[str, object],
    status: str,
    reason_code: str | None,
    handled_at: datetime | None,
    confidence: float | None,
    now: datetime | None = None,
) -> ChildArchiveFactView:
    provenance = _optional_str(payload.get("provenance"))
    lifecycle = display_lifecycle(
        status=status,
        reason_code=reason_code,
        provenance=provenance,
        retention_expires_at=retention_expires_at,
        now=now,
    )
    storage: ArchiveStorageStatus
    if status in {"candidate", "confirmed", "disputed", "revoked"}:
        storage = status  # type: ignore[assignment]
    else:
        storage = "candidate"
    version_raw = payload.get("fact_version")
    fact_version = int(version_raw) if isinstance(version_raw, int) and version_raw >= 1 else 1
    polarity_raw = _optional_str(payload.get("polarity"))
    polarity: ArchivePolarity | None = None
    if polarity_raw in {"positive", "negative", "substituted", "unknown"}:
        polarity = polarity_raw  # type: ignore[assignment]
    return ChildArchiveFactView(
        record_id=record_id,
        field=field,
        text=text,
        status=storage,
        display_lifecycle=lifecycle,
        display_label=DISPLAY_LABELS[lifecycle],
        provenance=provenance,
        source_session_id=_optional_str(payload.get("source_session_id"))
        or _optional_str(payload.get("session_id")),
        rule_version=_optional_str(payload.get("rule_version")),
        fact_version=fact_version,
        polarity=polarity,
        confidence=confidence,
        handled_at=handled_at,
        reason_code=reason_code,
        created_at=created_at,
        last_mentioned_at=_optional_str(payload.get("last_mentioned_at")),
        supersedes_record_id=_optional_str(payload.get("supersedes_record_id")),
        recall_eligible=recall_eligible(lifecycle),
    )


def view_to_api(view: ChildArchiveFactView) -> dict[str, object]:
    return {
        "record_id": view.record_id,
        "field": view.field,
        "text": view.text,
        "status": view.status,
        "display_lifecycle": view.display_lifecycle,
        "display_label": view.display_label,
        "provenance": view.provenance,
        "source_session_id": view.source_session_id,
        "rule_version": view.rule_version,
        "fact_version": view.fact_version,
        "polarity": view.polarity,
        "confidence": view.confidence,
        "handled_at": view.handled_at.isoformat() if view.handled_at else None,
        "reason_code": view.reason_code,
        "created_at": view.created_at.isoformat(),
        "last_mentioned_at": view.last_mentioned_at,
        "supersedes_record_id": view.supersedes_record_id,
        "recall_eligible": view.recall_eligible,
    }


def validate_correct_patch(
    *,
    text: str | None,
    field: str | None,
    polarity: str | None,
) -> tuple[str | None, ArchiveField | None, ArchivePolarity | None]:
    if text is None and field is None and polarity is None:
        raise ChildArchiveReviewError(
            "correct_requires_change",
            "correct needs text, field, or polarity",
        )
    next_field: ArchiveField | None = None
    if field is not None:
        if field not in ARCHIVE_FIELDS:
            raise ChildArchiveReviewError("invalid_archive_field")
        next_field = field  # type: ignore[assignment]
    next_polarity: ArchivePolarity | None = None
    if polarity is not None:
        if polarity not in {"positive", "negative", "substituted", "unknown"}:
            raise ChildArchiveReviewError("invalid_polarity")
        next_polarity = polarity  # type: ignore[assignment]
    next_text = text.strip() if isinstance(text, str) else None
    if next_text is not None and not next_text:
        raise ChildArchiveReviewError("invalid_archive_text")
    return next_text, next_field, next_polarity


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = [
    "ARCHIVE_FIELDS",
    "AUTO_BIRTH_REASONS",
    "ChildArchiveFactView",
    "ChildArchiveReviewCommand",
    "ChildArchiveReviewError",
    "ChildArchiveReviewReceipt",
    "DISPLAY_LABELS",
    "PARENT_CONFIRM_REASONS",
    "build_fact_view",
    "display_lifecycle",
    "recall_eligible",
    "validate_correct_patch",
    "view_to_api",
]
