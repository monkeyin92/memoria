"""Outbox and audit events emitted by the consent authority.

Both event types are immutable, JSONB-safe (sorted string pairs), and validated
on construction.  The outbox ``event_id`` is stable per idempotency key: a
replayed operation returns the stored id instead of writing a new row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

type AggregateType = Literal[
    "consent_offer",
    "consent_grant",
    "consent_revoke",
    "consent_dispute",
    "consent_expire",
    "consent_snapshot",
]

ALL_AGGREGATE_TYPES: frozenset[str] = frozenset(
    {
        "consent_offer",
        "consent_grant",
        "consent_revoke",
        "consent_dispute",
        "consent_expire",
        "consent_snapshot",
    }
)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, field_name: str, maximum: int = 128) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return normalized


def normalize_payload(
    payload: tuple[tuple[str, str], ...] | list[tuple[str, str]] | tuple[list[str], ...],
) -> tuple[tuple[str, str], ...]:
    """Sort and dedupe (keep last) string pairs; JSONB-safe and canonical.

    Keys must be non-empty; values may be empty (e.g. an optional reason) but
    are length-bounded.
    """
    merged: dict[str, str] = {}
    for pair in payload:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("payload items must be (key, value) string pairs")
        key = _bounded(str(pair[0]), "payload key")
        value = str(pair[1])
        if len(value) > 256:
            raise ValueError("payload value must be at most 256 characters")
        merged[key] = value
    return tuple(sorted(merged.items()))


def payload_to_json(payload: tuple[tuple[str, str], ...]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_from_json(raw: str) -> tuple[tuple[str, str], ...]:
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("payload must be a JSON array of pairs")
    return normalize_payload(tuple(tuple(pair) for pair in value))


@dataclass(frozen=True, slots=True)
class ConsentOutboxEvent:
    """Outbox row for downstream workers (append-only; stable id per retry)."""

    event_id: str
    aggregate_type: AggregateType
    aggregate_id: str
    version: int
    payload: tuple[tuple[str, str], ...]
    created_at: datetime
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _bounded(self.event_id, "event_id"))
        object.__setattr__(self, "aggregate_id", _bounded(self.aggregate_id, "aggregate_id"))
        if self.aggregate_type not in ALL_AGGREGATE_TYPES:
            raise ValueError(f"unknown aggregate_type {self.aggregate_type!r}")
        if self.version <= 0:
            raise ValueError("version must be >= 1")
        object.__setattr__(self, "payload", normalize_payload(self.payload))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                _bounded(self.idempotency_key, "idempotency_key"),
            )


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Immutable audit trail row written in the same transaction as the change."""

    audit_id: str
    event_id: str
    action: str
    actor_id: str
    subject_id: str
    consent_id: str | None
    snapshot_id: str | None
    payload: tuple[tuple[str, str], ...]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "audit_id", _bounded(self.audit_id, "audit_id"))
        object.__setattr__(self, "event_id", _bounded(self.event_id, "event_id"))
        object.__setattr__(self, "action", _bounded(self.action, "action", maximum=64))
        object.__setattr__(self, "actor_id", _bounded(self.actor_id, "actor_id"))
        object.__setattr__(self, "subject_id", _bounded(self.subject_id, "subject_id"))
        if self.consent_id is not None:
            object.__setattr__(self, "consent_id", _bounded(self.consent_id, "consent_id"))
        if self.snapshot_id is not None:
            object.__setattr__(self, "snapshot_id", _bounded(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "payload", normalize_payload(self.payload))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


__all__ = [
    "ALL_AGGREGATE_TYPES",
    "AggregateType",
    "AuditEntry",
    "ConsentOutboxEvent",
    "normalize_payload",
    "payload_from_json",
    "payload_to_json",
]
