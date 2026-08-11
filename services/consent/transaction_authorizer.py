"""Transaction-bound exact-fence authorization for production PostgreSQL.

This module deliberately does not answer a reusable authorization boolean.  A
caller supplies an already-open :class:`asyncpg.Connection`; the adapter locks
current Consent authority heads in deterministic order, validates private
facts, and immediately invokes the caller's operation on that same connection.
Only the operation result escapes.  The adapter never acquires a pool
connection and never starts, commits, or rolls back a transaction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypeVar

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    CapabilityValue,
    PurposeValue,
)

from services.consent.evidence import (
    ConsentEvidence,
    ConsentSnapshot,
    _validate_capability_purpose,
)

_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
T = TypeVar("T")


class ConsentFenceMismatchError(PermissionError):
    """Current authority facts do not exactly match the caller's expected fence."""

    def __init__(self, field: str, message: str | None = None) -> None:
        super().__init__(message or f"consent fence mismatch: {field}")
        self.field = field


def _required_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return normalized


def _positive(value: int, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{field} must be an integer >= 1")
    return value


def _hash(value: str, field: str) -> str:
    if not _HASH_RE.fullmatch(value):
        raise ValueError(f"{field} must be a sha256 hex digest")
    return value


@dataclass(frozen=True, slots=True)
class ExpectedConsentFence:
    """Exact evidence head plus current-global snapshot fence from Policy.

    ``ConsentEvidence.snapshot_id`` records the historical snapshot that
    produced that immutable evidence revision.  The ``current_snapshot_*``
    fields fence the subject/binding's current global aggregate and therefore
    may identify a later snapshot containing that same evidence revision.
    """

    request_actor_id: str
    evidence_actor_id: str
    consent_id: str
    revision: int
    canonical_hash: str
    current_snapshot_id: str
    current_snapshot_revision: int
    current_snapshot_hash: str
    subject_id: str
    binding_id: str
    binding_version: int
    capability: CapabilityValue
    purpose: PurposeValue

    def __post_init__(self) -> None:
        for field in (
            "request_actor_id",
            "evidence_actor_id",
            "consent_id",
            "current_snapshot_id",
            "subject_id",
            "binding_id",
        ):
            object.__setattr__(self, field, _required_text(getattr(self, field), field))
        object.__setattr__(self, "revision", _positive(self.revision, "revision"))
        object.__setattr__(
            self,
            "current_snapshot_revision",
            _positive(self.current_snapshot_revision, "current_snapshot_revision"),
        )
        object.__setattr__(
            self,
            "binding_version",
            _positive(self.binding_version, "binding_version"),
        )
        object.__setattr__(
            self,
            "canonical_hash",
            _hash(self.canonical_hash, "canonical_hash"),
        )
        object.__setattr__(
            self,
            "current_snapshot_hash",
            _hash(self.current_snapshot_hash, "current_snapshot_hash"),
        )
        _validate_capability_purpose(self.capability, self.purpose)

    @property
    def lock_key(self) -> tuple[str, str, str, str, int, str, str]:
        return (
            self.request_actor_id,
            self.evidence_actor_id,
            self.subject_id,
            self.binding_id,
            self.binding_version,
            self.capability,
            self.purpose,
        )

    @property
    def consent_head_key(self) -> tuple[str, str, str, int, str, str]:
        return (
            self.evidence_actor_id,
            self.subject_id,
            self.binding_id,
            self.binding_version,
            self.capability,
            self.purpose,
        )

    @property
    def snapshot_head_key(self) -> tuple[str, str, int]:
        return (self.subject_id, self.binding_id, self.binding_version)


@dataclass(frozen=True, slots=True)
class _LockedConsentFacts:
    """Validated facts; the lock/fact authority ends with the caller transaction."""

    evidence: ConsentEvidence
    snapshot: ConsentSnapshot
    lock_lifetime: Literal["caller_transaction"] = "caller_transaction"


def _row_mapping(row: asyncpg.Record | Mapping[str, object]) -> Mapping[str, object]:
    return row


def _json_object(value: object, field: str) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ConsentFenceMismatchError(field, f"{field} is not a JSON object")
    return {str(key): item for key, item in decoded.items()}


def _expect_equal(field: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise ConsentFenceMismatchError(field)


class TransactionBoundConsentAuthorizer:
    """Lock and validate current heads without owning transaction lifecycle."""

    async def execute_with_authority(
        self,
        conn: asyncpg.Connection,
        *,
        expected: tuple[ExpectedConsentFence, ...],
        now: datetime,
        operation: Callable[[asyncpg.Connection], Awaitable[T]],
    ) -> T:
        await self._lock_current(conn, expected=expected, now=now)
        return await operation(conn)

    async def _lock_current(
        self,
        conn: asyncpg.Connection,
        *,
        expected: tuple[ExpectedConsentFence, ...],
        now: datetime,
    ) -> tuple[_LockedConsentFacts, ...]:
        if not conn.is_in_transaction():
            raise RuntimeError("an already-open caller transaction is required")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        checked_at = now.astimezone(UTC)
        ordered = tuple(sorted(expected, key=lambda item: item.lock_key))
        if len({item.consent_head_key for item in ordered}) != len(ordered):
            raise ValueError("duplicate expected consent authority head")

        locked_consent_heads: list[tuple[ExpectedConsentFence, Mapping[str, object]]] = []
        for fence in ordered:
            head = await conn.fetchrow(
                "SELECT * FROM consent_lock_authority_head($1,$2,$3,$4,$5,$6,$7)",
                fence.request_actor_id,
                fence.evidence_actor_id,
                fence.subject_id,
                fence.binding_id,
                fence.binding_version,
                fence.capability,
                fence.purpose,
            )
            if head is None:
                raise ConsentFenceMismatchError("authority_head", "consent authority head missing")
            locked_consent_heads.append((fence, _row_mapping(head)))

        snapshot_expectations: dict[
            tuple[str, str, int], tuple[ExpectedConsentFence, tuple[str, int, str]]
        ] = {}
        for fence in ordered:
            expected_snapshot = (
                fence.current_snapshot_id,
                fence.current_snapshot_revision,
                fence.current_snapshot_hash,
            )
            existing = snapshot_expectations.get(fence.snapshot_head_key)
            if existing is not None and existing[1] != expected_snapshot:
                raise ConsentFenceMismatchError(
                    "snapshot_fence", "inconsistent expected global snapshot head"
                )
            if existing is None:
                snapshot_expectations[fence.snapshot_head_key] = (fence, expected_snapshot)

        locked_snapshot_heads: dict[tuple[str, str, int], Mapping[str, object]] = {}
        for snapshot_key in sorted(snapshot_expectations):
            fence = snapshot_expectations[snapshot_key][0]
            snapshot_head = await conn.fetchrow(
                "SELECT * FROM consent_lock_snapshot_head($1,$2,$3,$4)",
                fence.request_actor_id,
                fence.subject_id,
                fence.binding_id,
                fence.binding_version,
            )
            if snapshot_head is None:
                raise ConsentFenceMismatchError("snapshot_head", "consent snapshot head missing")
            locked_snapshot_heads[snapshot_key] = _row_mapping(snapshot_head)

        facts: list[_LockedConsentFacts] = []
        for fence, head_values in locked_consent_heads:
            for field, actual, wanted in (
                ("evidence_actor_id", head_values["actor_id"], fence.evidence_actor_id),
                ("subject_id", head_values["subject_id"], fence.subject_id),
                ("binding_id", head_values["binding_id"], fence.binding_id),
                ("binding_version", head_values["binding_version"], fence.binding_version),
                ("capability", head_values["capability"], fence.capability),
                ("purpose", head_values["purpose"], fence.purpose),
                ("consent_id", head_values["current_consent_id"], fence.consent_id),
                ("revision", head_values["current_revision"], fence.revision),
                ("canonical_hash", head_values["current_hash"], fence.canonical_hash),
            ):
                _expect_equal(field, actual, wanted)
            snapshot_head_values = locked_snapshot_heads[fence.snapshot_head_key]
            for field, actual, wanted in (
                (
                    "current_snapshot_id",
                    snapshot_head_values["current_snapshot_id"],
                    fence.current_snapshot_id,
                ),
                (
                    "current_snapshot_revision",
                    snapshot_head_values["current_revision"],
                    fence.current_snapshot_revision,
                ),
                (
                    "current_snapshot_hash",
                    snapshot_head_values["current_hash"],
                    fence.current_snapshot_hash,
                ),
            ):
                _expect_equal(field, actual, wanted)

            evidence = ConsentEvidence.from_canonical_dict(
                _json_object(head_values["evidence_json"], "evidence_json")
            )
            snapshot = ConsentSnapshot.from_canonical_dict(
                _json_object(snapshot_head_values["snapshot_json"], "snapshot_json")
            )
            for field, actual, wanted in (
                ("consent_id", evidence.consent_id, fence.consent_id),
                ("revision", evidence.version, fence.revision),
                ("canonical_hash", evidence.canonical_hash, fence.canonical_hash),
                ("status", evidence.status, "active"),
                ("evidence_actor_id", evidence.actor_id, fence.evidence_actor_id),
                ("subject_id", evidence.subject_id, fence.subject_id),
                ("binding_id", evidence.binding_id, fence.binding_id),
                ("binding_version", evidence.binding_version, fence.binding_version),
                ("capability", evidence.capability, fence.capability),
                ("purpose", evidence.purpose, fence.purpose),
                (
                    "current_snapshot_id",
                    snapshot.snapshot_id,
                    fence.current_snapshot_id,
                ),
                (
                    "current_snapshot_revision",
                    snapshot.version,
                    fence.current_snapshot_revision,
                ),
                (
                    "current_snapshot_hash",
                    snapshot.canonical_hash,
                    fence.current_snapshot_hash,
                ),
                ("subject_id", snapshot.subject_id, fence.subject_id),
                ("binding_id", snapshot.binding_id, fence.binding_id),
                ("binding_version", snapshot.binding_version, fence.binding_version),
            ):
                _expect_equal(field, actual, wanted)
            if not evidence.is_effective_at(checked_at):
                raise ConsentFenceMismatchError("validity", "consent evidence is not currently valid")
            if not snapshot.binding.is_active_at(checked_at):
                raise ConsentFenceMismatchError("binding_status", "snapshot binding is not active")
            if not any(
                grant.consent_id == evidence.consent_id
                and grant.version == evidence.version
                and grant.canonical_hash == evidence.canonical_hash
                for grant in snapshot.grants
            ):
                raise ConsentFenceMismatchError("snapshot_grant", "snapshot lacks exact consent fact")
            facts.append(_LockedConsentFacts(evidence=evidence, snapshot=snapshot))
        return tuple(facts)
