"""Pure contracts for multi-subject identity.

Implements the Person / Relationship / versioned Device Binding domain from
the 2026-08-09 multi-subject remediation plan:

- ``PersonSubject`` defaults to ``unknown`` and never silently to ``adult``;
- directed ``Relationship`` with a six-state lifecycle, permission sets,
  two-party confirmation and delegation metadata;
- versioned ``DeviceBinding`` with monotonic per-device versions, supersede
  chains, role grants and a serializable ``BindingManifest``;
- an explicit role/permission matrix in which the payer (``account_owner``)
  never derives content-read rights.

This module has no framework or persistence dependencies so the same rules
are reusable by the in-memory, SQLite and PostgreSQL adapters.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    AgeEvidenceStatusValue,
    BindingRoleValue,
    DeviceDeclaredModeValue,
    RelationshipStatusValue,
    SubjectCategoryValue,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingManifest as CanonicalBindingManifest,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole as BindingRoleContract,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceDeclaredMode as DeviceDeclaredModeContract,
)
from packages.contracts.generated.python.multi_subject_contracts import (
    RelationshipStatus as RelationshipStatusContract,
)

type SubjectCategory = SubjectCategoryValue
type AgeBand = AgeBandValue
type AgeEvidenceStatus = AgeEvidenceStatusValue
PersonStatus = Literal["active", "disabled"]

type DeviceDeclaredMode = DeviceDeclaredModeValue
BindingStatus = Literal["active", "superseded", "revoked", "expired"]
BindingReason = Literal["create", "supersede", "transfer", "unbind", "expire"]

type BindingRole = BindingRoleValue

Permission = Literal[
    "binding.manage",
    "device.manage",
    "device.status.view",
    "billing.manage",
    "content.read",
    "memory.private.read",
    "memory.guardian.summary",
    "consent.manage",
    "crisis.notify",
    "emergency.notify",
    "legacy.access",
    "family.shared.read",
    "family.shared.write",
    "delegate.access",
]

RelationType = Literal[
    "self",
    "parent_of",
    "child_of",
    "guardian_of",
    "ward_of",
    "spouse_of",
    "sibling_of",
    "caregiver_of",
    "emergency_contact_for",
    "delegate_for",
    "beneficiary_of",
    "co_subject_of",
    "family_member_of",
]
type RelationshipStatus = RelationshipStatusValue
TransferIntentStatus = Literal["pending", "accepted", "cancelled", "expired", "conflicted"]
IdempotencyOperation = Literal[
    "transfer.create", "transfer.accept", "transfer.cancel", "persona.create"
]

AGE_BAND_ADULT = "adult"
MAX_DELEGATION_DEPTH = 3

#: Default permission set per binding role. Content-reading and private-memory
#: rights belong to the primary subject only; the payer/device administrator
#: never derives them from payment or device management (D-06, section 5.4).
ROLE_DEFAULT_PERMISSIONS: dict[BindingRole, frozenset[Permission]] = {
    "account_owner": frozenset({"binding.manage", "billing.manage", "device.status.view"}),
    "device_admin": frozenset({"device.manage", "device.status.view"}),
    "primary_subject": frozenset({"content.read", "memory.private.read"}),
    "guardian": frozenset(
        {"memory.guardian.summary", "consent.manage", "crisis.notify", "device.status.view"}
    ),
    "delegate": frozenset({"delegate.access"}),
    "emergency_contact": frozenset({"emergency.notify", "device.status.view"}),
    "member": frozenset({"family.shared.read", "family.shared.write"}),
}

ALL_PERMISSIONS = frozenset(Permission.__args__)  # type: ignore[attr-defined]

RELATIONSHIP_TRANSITIONS: dict[RelationshipStatus, frozenset[RelationshipStatus]] = {
    "pending": frozenset({"active", "revoked", "expired", "disputed"}),
    "active": frozenset({"suspended", "revoked", "expired", "disputed"}),
    "suspended": frozenset({"active", "revoked", "expired", "disputed"}),
    "disputed": frozenset({"active", "revoked", "expired"}),
    "revoked": frozenset(),
    "expired": frozenset(),
}

TRANSFER_INTENT_TRANSITIONS: dict[TransferIntentStatus, frozenset[TransferIntentStatus]] = {
    "pending": frozenset({"accepted", "cancelled", "expired", "conflicted"}),
    "accepted": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
    "conflicted": frozenset(),
}

ALL_RELATION_TYPES = frozenset(RelationType.__args__)  # type: ignore[attr-defined]
ALL_BINDING_ROLES = frozenset(BindingRoleContract.values())
ALL_DECLARED_MODES = frozenset(DeviceDeclaredModeContract.values())
ALL_BINDING_STATUSES = frozenset(BindingStatus.__args__)  # type: ignore[attr-defined]
ALL_BINDING_REASONS = frozenset(BindingReason.__args__)  # type: ignore[attr-defined]
ALL_RELATIONSHIP_STATUSES = frozenset(RelationshipStatusContract.values())
ALL_TRANSFER_INTENT_STATUSES = frozenset(TRANSFER_INTENT_TRANSITIONS)


def permission_set(values: Iterable[object]) -> frozenset[Permission]:
    """Validate and normalize a permission collection (fail closed)."""
    result: set[Permission] = set()
    for value in values:
        if not isinstance(value, str) or value not in ALL_PERMISSIONS:
            raise ValueError(f"unknown permission {value!r}")
        result.add(cast(Permission, value))
    return frozenset(result)


class IdentityNotFoundError(LookupError):
    """The requested identity resource does not exist."""


class IdentityConflictError(RuntimeError):
    """An identity mutation violates a lifecycle or concurrency rule."""


class IdentityAccessDeniedError(PermissionError):
    """The actor is not allowed to perform this identity mutation."""


class AgeEvidenceError(ValueError):
    """An age declaration contradicts the evidence rules (never default adult)."""


class RelationshipLifecycleError(IdentityConflictError):
    """A relationship transition violates the lifecycle state machine."""


class RoleConstraintError(IdentityConflictError):
    """A role grant violates the declared-mode role matrix."""


class ModeConstraintError(IdentityConflictError):
    """A binding does not satisfy its declared mode constraints."""


class TransferLifecycleError(IdentityConflictError):
    """A transfer intent violates its lifecycle, evidence or actor rules."""


class BindingVersionConflictError(IdentityConflictError):
    """A binding version collided; per-device versions must be monotonic."""


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, *, field: str, maximum: int = 128) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return normalized


def _optional_datetime(value: object, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value, field=field)
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value), field=field)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    raise ValueError(f"{field} must be an ISO-8601 timestamp or None")


def derive_subject_category(
    *, age_band: AgeBand, age_evidence_status: AgeEvidenceStatus
) -> SubjectCategory:
    """Fail-closed derivation: adult only with verified adult evidence."""
    if age_band == "adult" and age_evidence_status == "verified":
        return "adult"
    if age_band in ("under_14", "14_17"):
        return "minor"
    return "unknown"


def validate_age_declaration(
    *,
    subject_category: SubjectCategory,
    age_band: AgeBand,
    age_evidence_status: AgeEvidenceStatus,
) -> None:
    if subject_category == "adult" and not (
        age_band == "adult" and age_evidence_status == "verified"
    ):
        raise AgeEvidenceError(
            "adult category requires age_band=adult and verified age evidence; "
            "unknown must never default to adult"
        )
    if subject_category == "minor" and age_band not in ("under_14", "14_17"):
        raise AgeEvidenceError("minor category requires an under-age age band")
    if subject_category == "unknown" and (
        age_band in {"under_14", "14_17"}
        or (age_band == "adult" and age_evidence_status == "verified")
    ):
        raise AgeEvidenceError(
            "unknown category may retain only unknown or unverified adult age evidence"
        )


@dataclass(frozen=True)
class PersonSubject:
    """A natural person being served, recorded and remembered.

    The default is ``unknown`` / ``unverified`` on purpose: an unverified
    person must never be treated as an adult (sections 5.1, 8.1, PR-02).
    """

    person_id: str
    display_name: str
    subject_category: SubjectCategory = "unknown"
    age_band: AgeBand = "unknown"
    age_evidence_status: AgeEvidenceStatus = "unverified"
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    status: PersonStatus = "active"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_id", _bounded(self.person_id, field="person_id"))
        object.__setattr__(
            self, "display_name", _bounded(self.display_name, field="display_name", maximum=128)
        )
        object.__setattr__(self, "locale", _bounded(self.locale, field="locale", maximum=32))
        object.__setattr__(self, "timezone", _bounded(self.timezone, field="timezone", maximum=64))
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, field="updated_at"))
        validate_age_declaration(
            subject_category=self.subject_category,
            age_band=self.age_band,
            age_evidence_status=self.age_evidence_status,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "person_id": self.person_id,
            "display_name": self.display_name,
            "subject_category": self.subject_category,
            "age_band": self.age_band,
            "age_evidence_status": self.age_evidence_status,
            "locale": self.locale,
            "timezone": self.timezone,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def person_from_dict(value: dict[str, object]) -> PersonSubject:
    try:
        return PersonSubject(
            person_id=str(value["person_id"]),
            display_name=str(value["display_name"]),
            subject_category=cast(SubjectCategory, str(value["subject_category"])),
            age_band=cast(AgeBand, str(value["age_band"])),
            age_evidence_status=cast(AgeEvidenceStatus, str(value["age_evidence_status"])),
            locale=str(value.get("locale", "zh-CN")),
            timezone=str(value["timezone"]),
            status=cast(PersonStatus, str(value.get("status", "active"))),
            created_at=cast(datetime, _optional_datetime(value["created_at"], field="created_at")),
            updated_at=cast(datetime, _optional_datetime(value["updated_at"], field="updated_at")),
        )
    except KeyError as exc:
        raise ValueError(f"person payload is missing {exc.args[0]}") from exc


@dataclass(frozen=True)
class Relationship:
    """A directed, stateful link between two persons.

    Lifecycle: pending -> active (after both-party confirmation when required)
    -> suspended / disputed / revoked / expired. Delegation is metadata-only:
    ``can_delegate`` plus ``delegated_from_relationship_id`` /
    ``delegation_depth`` capped at ``MAX_DELEGATION_DEPTH``.
    """

    relationship_id: str
    source_person_id: str
    target_person_id: str
    relation_type: RelationType
    status: RelationshipStatus = "pending"
    valid_from: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    established_evidence_id: str = ""
    confirmed_by_source_at: datetime | None = None
    confirmed_by_target_at: datetime | None = None
    requires_confirmation: bool = True
    can_delegate: bool = False
    delegated_from_relationship_id: str | None = None
    delegation_depth: int = 0
    permissions: frozenset[Permission] = frozenset()
    dispute_reason: str | None = None
    dispute_resolution_acked_by_source_at: datetime | None = None
    dispute_resolution_acked_by_target_at: datetime | None = None
    revoked_at: datetime | None = None
    revocation_evidence_id: str | None = None
    #: True when this relationship was auto-suspended because its delegation
    #: parent is suspended / disputed.  Only meaningful while status is
    #: ``suspended``; explicit endpoint actions clear the flag.
    auto_suspended: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "relationship_id", _bounded(self.relationship_id, field="relationship_id")
        )
        object.__setattr__(
            self,
            "source_person_id",
            _bounded(self.source_person_id, field="source_person_id"),
        )
        object.__setattr__(
            self,
            "target_person_id",
            _bounded(self.target_person_id, field="target_person_id"),
        )
        if self.relation_type not in ALL_RELATION_TYPES:
            raise ValueError(f"unknown relation_type {self.relation_type!r}")
        if self.status not in ALL_RELATIONSHIP_STATUSES:
            raise ValueError(f"unknown relationship status {self.status!r}")
        object.__setattr__(self, "valid_from", _utc(self.valid_from, field="valid_from"))
        object.__setattr__(
            self,
            "valid_until",
            _utc(self.valid_until, field="valid_until")
            if self.valid_until is not None
            else None,
        )
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.relation_type == "self":
            if self.source_person_id != self.target_person_id:
                raise ValueError("self relationship must have identical endpoints")
        elif self.source_person_id == self.target_person_id:
            raise ValueError("directed relationship endpoints must differ")
        if self.relation_type != "self" and not self.requires_confirmation:
            raise ValueError(
                "non-self relationships must require two-party confirmation; "
                "requires_confirmation=False is only meaningful for self"
            )
        if self.delegation_depth < 0 or self.delegation_depth > MAX_DELEGATION_DEPTH:
            raise ValueError(f"delegation_depth must be within 0..{MAX_DELEGATION_DEPTH}")
        if self.delegated_from_relationship_id is None and self.delegation_depth != 0:
            raise ValueError("delegation_depth requires delegated_from_relationship_id")
        if self.can_delegate and not self.requires_confirmation:
            raise ValueError("delegatable relationships must require confirmation")
        if (
            self.dispute_resolution_acked_by_source_at is not None
            or self.dispute_resolution_acked_by_target_at is not None
        ) and self.status != "disputed":
            raise ValueError(
                "dispute resolution acknowledgements require status=disputed"
            )
        if self.auto_suspended and self.status != "suspended":
            raise ValueError("auto_suspended requires status=suspended")
        object.__setattr__(
            self,
            "dispute_resolution_acked_by_source_at",
            _utc(self.dispute_resolution_acked_by_source_at, field="dispute_resolution_acked_by_source_at")
            if self.dispute_resolution_acked_by_source_at is not None
            else None,
        )
        object.__setattr__(
            self,
            "dispute_resolution_acked_by_target_at",
            _utc(self.dispute_resolution_acked_by_target_at, field="dispute_resolution_acked_by_target_at")
            if self.dispute_resolution_acked_by_target_at is not None
            else None,
        )
        object.__setattr__(self, "permissions", permission_set(self.permissions))
        object.__setattr__(
            self,
            "established_evidence_id",
            _bounded(self.established_evidence_id, field="established_evidence_id"),
        )
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, field="updated_at"))

    def to_dict(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "source_person_id": self.source_person_id,
            "target_person_id": self.target_person_id,
            "relation_type": self.relation_type,
            "status": self.status,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "established_evidence_id": self.established_evidence_id,
            "confirmed_by_source_at": (
                self.confirmed_by_source_at.isoformat() if self.confirmed_by_source_at else None
            ),
            "confirmed_by_target_at": (
                self.confirmed_by_target_at.isoformat() if self.confirmed_by_target_at else None
            ),
            "requires_confirmation": self.requires_confirmation,
            "can_delegate": self.can_delegate,
            "delegated_from_relationship_id": self.delegated_from_relationship_id,
            "delegation_depth": self.delegation_depth,
            "permissions": sorted(self.permissions),
            "dispute_reason": self.dispute_reason,
            "dispute_resolution_acked_by_source_at": (
                self.dispute_resolution_acked_by_source_at.isoformat()
                if self.dispute_resolution_acked_by_source_at
                else None
            ),
            "dispute_resolution_acked_by_target_at": (
                self.dispute_resolution_acked_by_target_at.isoformat()
                if self.dispute_resolution_acked_by_target_at
                else None
            ),
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revocation_evidence_id": self.revocation_evidence_id,
            "auto_suspended": self.auto_suspended,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class DeviceBindingRole:
    """A role grant on a specific binding version (append-only per version)."""

    binding_id: str
    person_id: str
    role: BindingRole
    status: Literal["active", "superseded", "revoked", "expired"] = "active"
    permissions: frozenset[Permission] = frozenset()
    granted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _bounded(self.binding_id, field="binding_id"))
        object.__setattr__(self, "person_id", _bounded(self.person_id, field="person_id"))
        if self.role not in ALL_BINDING_ROLES:
            raise ValueError(f"unknown binding role {self.role!r}")
        if self.status not in ALL_BINDING_STATUSES:
            raise ValueError(f"unknown role status {self.status!r}")
        object.__setattr__(self, "granted_at", _utc(self.granted_at, field="granted_at"))
        object.__setattr__(
            self, "ended_at", _utc(self.ended_at, field="ended_at") if self.ended_at else None
        )
        if (self.status == "active") != (self.ended_at is None):
            raise ValueError("role status active requires ended_at=None and vice versa")
        object.__setattr__(self, "permissions", permission_set(self.permissions))


@dataclass(frozen=True)
class TransferIntent:
    """A formal, two-party device ownership transfer.

    The current account owner creates a ``pending`` intent with a step-up
    evidence id and a policy receipt (never a bare ``binding.manage`` call);
    the *target* owner must explicitly accept.  Accept atomically issues the
    next binding version, invalidates the old one and inherits no roles.
    ``conflicted`` records an intent that could no longer be completed because
    the device binding changed underneath it (section 2.4 / PR-03 / 13.1).
    """

    transfer_id: str
    device_id: str
    from_account_owner_person_id: str
    to_account_owner_person_id: str
    status: TransferIntentStatus = "pending"
    step_up_evidence_id: str = ""
    policy_receipt_id: str = ""
    #: Client-supplied replay key; one pending intent per (device, key).
    idempotency_key: str = ""
    #: sha256 of the verified signed evidence ticket (never the ticket itself).
    evidence_hash: str = ""
    supersedes_binding_id: str | None = None
    resulting_binding_id: str | None = None
    created_by_person_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    accepted_at: datetime | None = None
    cancelled_at: datetime | None = None
    cancelled_by_person_id: str | None = None
    cancel_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "transfer_id", _bounded(self.transfer_id, field="transfer_id"))
        object.__setattr__(self, "device_id", _bounded(self.device_id, field="device_id"))
        object.__setattr__(
            self,
            "from_account_owner_person_id",
            _bounded(
                self.from_account_owner_person_id,
                field="from_account_owner_person_id",
            ),
        )
        object.__setattr__(
            self,
            "to_account_owner_person_id",
            _bounded(self.to_account_owner_person_id, field="to_account_owner_person_id"),
        )
        if self.status not in ALL_TRANSFER_INTENT_STATUSES:
            raise ValueError(f"unknown transfer intent status {self.status!r}")
        object.__setattr__(
            self,
            "step_up_evidence_id",
            _bounded(self.step_up_evidence_id, field="step_up_evidence_id"),
        )
        object.__setattr__(
            self,
            "policy_receipt_id",
            _bounded(
                self.policy_receipt_id,
                field="policy_receipt_id",
                maximum=128,
            ),
        )
        idempotency_key = self.idempotency_key.strip()
        if len(idempotency_key) > 64:
            raise ValueError("idempotency_key must be at most 64 characters")
        object.__setattr__(self, "idempotency_key", idempotency_key)
        evidence_hash = self.evidence_hash.strip()
        if evidence_hash and (
            len(evidence_hash) != 64
            or any(character not in "0123456789abcdef" for character in evidence_hash)
        ):
            raise ValueError("evidence_hash must be a 64-char sha256 hex digest")
        object.__setattr__(self, "evidence_hash", evidence_hash)
        if self.from_account_owner_person_id == self.to_account_owner_person_id:
            raise ValueError("transfer requires different account owners")
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, field="updated_at"))
        object.__setattr__(
            self,
            "valid_until",
            _utc(self.valid_until, field="valid_until")
            if self.valid_until is not None
            else None,
        )
        if self.valid_until is not None and self.valid_until <= self.created_at:
            raise ValueError("valid_until must be later than created_at")
        object.__setattr__(
            self,
            "accepted_at",
            _utc(self.accepted_at, field="accepted_at")
            if self.accepted_at is not None
            else None,
        )
        object.__setattr__(
            self,
            "cancelled_at",
            _utc(self.cancelled_at, field="cancelled_at")
            if self.cancelled_at is not None
            else None,
        )
        if (self.status == "accepted") != (
            self.accepted_at is not None and self.resulting_binding_id is not None
        ):
            raise ValueError(
                "accepted transfer requires accepted_at and resulting_binding_id"
            )
        if (self.status == "cancelled") != (
            self.cancelled_at is not None and self.cancelled_by_person_id is not None
        ):
            raise ValueError(
                "cancelled transfer requires cancelled_at and cancelled_by_person_id"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "transfer_id": self.transfer_id,
            "device_id": self.device_id,
            "from_account_owner_person_id": self.from_account_owner_person_id,
            "to_account_owner_person_id": self.to_account_owner_person_id,
            "status": self.status,
            "step_up_evidence_id": self.step_up_evidence_id,
            "policy_receipt_id": self.policy_receipt_id,
            "idempotency_key": self.idempotency_key,
            "evidence_hash": self.evidence_hash,
            "supersedes_binding_id": self.supersedes_binding_id,
            "resulting_binding_id": self.resulting_binding_id,
            "created_by_person_id": self.created_by_person_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "cancelled_by_person_id": self.cancelled_by_person_id,
            "cancel_reason": self.cancel_reason,
        }


def transfer_from_dict(value: dict[str, object]) -> TransferIntent:
    try:
        return TransferIntent(
            transfer_id=str(value["transfer_id"]),
            device_id=str(value["device_id"]),
            from_account_owner_person_id=str(value["from_account_owner_person_id"]),
            to_account_owner_person_id=str(value["to_account_owner_person_id"]),
            status=cast(TransferIntentStatus, str(value["status"])),
            step_up_evidence_id=str(value["step_up_evidence_id"]),
            policy_receipt_id=str(value["policy_receipt_id"]),
            idempotency_key=str(value.get("idempotency_key", "")),
            evidence_hash=str(value.get("evidence_hash", "")),
            supersedes_binding_id=(
                str(value["supersedes_binding_id"])
                if value["supersedes_binding_id"] is not None
                else None
            ),
            resulting_binding_id=(
                str(value["resulting_binding_id"])
                if value["resulting_binding_id"] is not None
                else None
            ),
            created_by_person_id=(
                str(value["created_by_person_id"])
                if value["created_by_person_id"] is not None
                else None
            ),
            created_at=cast(
                datetime, _optional_datetime(value["created_at"], field="created_at")
            ),
            updated_at=cast(
                datetime, _optional_datetime(value["updated_at"], field="updated_at")
            ),
            valid_until=_optional_datetime(value["valid_until"], field="valid_until"),
            accepted_at=_optional_datetime(value["accepted_at"], field="accepted_at"),
            cancelled_at=_optional_datetime(value["cancelled_at"], field="cancelled_at"),
            cancelled_by_person_id=(
                str(value["cancelled_by_person_id"])
                if value["cancelled_by_person_id"] is not None
                else None
            ),
            cancel_reason=(
                str(value["cancel_reason"]) if value["cancel_reason"] is not None else None
            ),
        )
    except KeyError as exc:
        raise ValueError(f"transfer payload is missing {exc.args[0]}") from exc


@dataclass(frozen=True)
class IdempotencyRecord:
    """Store-authoritative command idempotency (first-write-wins).

    ``scope_key`` + ``idempotency_key`` are the unique command identity;
    ``content_hash`` is the canonical request digest.  Replays must compare
    the digest and return the immutable ``result_payload`` — identical
    content replays across terminal states, different content conflicts.
    """

    scope_key: str
    idempotency_key: str
    operation: IdempotencyOperation
    content_hash: str
    result_payload: dict[str, object]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope_key", _bounded(self.scope_key, field="scope_key", maximum=256))
        object.__setattr__(
            self, "idempotency_key", _bounded(self.idempotency_key, field="idempotency_key", maximum=64)
        )
        if self.operation not in {
            "transfer.create",
            "transfer.accept",
            "transfer.cancel",
            "persona.create",
        }:
            raise ValueError(f"unknown idempotency operation {self.operation!r}")
        if not self.content_hash or len(self.content_hash) != 64:
            raise ValueError("content_hash must be a 64-char sha256 hex digest")
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))


@dataclass(frozen=True)
class DeviceBinding:
    """One immutable version of a device's binding contract.

    Versions are monotonic per device; superseding, transfer and unbind keep
    the previous versions untouched for audit (sections 2.3, 8.2, PR-03).
    """

    binding_id: str
    device_id: str
    declared_mode: DeviceDeclaredMode
    account_owner_person_id: str
    primary_subject_ids: tuple[str, ...]
    binding_version: int
    status: BindingStatus = "active"
    reason: BindingReason = "create"
    family_space_id: str | None = None
    supersedes_binding_id: str | None = None
    valid_from: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid_until: datetime | None = None
    roles: tuple[DeviceBindingRole, ...] = ()
    service_profile_version: str = "default-v1"
    policy_bundle_version: str = "policy-default-v1"
    consent_snapshot_id: str | None = None
    persona_assignment_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _bounded(self.binding_id, field="binding_id"))
        object.__setattr__(self, "device_id", _bounded(self.device_id, field="device_id"))
        object.__setattr__(
            self,
            "account_owner_person_id",
            _bounded(self.account_owner_person_id, field="account_owner_person_id"),
        )
        if self.declared_mode not in ALL_DECLARED_MODES:
            raise ValueError(f"unknown declared mode {self.declared_mode!r}")
        if self.status not in ALL_BINDING_STATUSES:
            raise ValueError(f"unknown binding status {self.status!r}")
        if self.reason not in ALL_BINDING_REASONS:
            raise ValueError(f"unknown binding reason {self.reason!r}")
        if self.binding_version < 1:
            raise ValueError("binding_version must be >= 1")
        if not self.primary_subject_ids:
            raise ValueError("a binding requires at least one primary subject")
        object.__setattr__(self, "valid_from", _utc(self.valid_from, field="valid_from"))
        object.__setattr__(
            self,
            "valid_until",
            _utc(self.valid_until, field="valid_until")
            if self.valid_until is not None
            else None,
        )
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        if self.supersedes_binding_id == self.binding_id:
            raise ValueError("a binding cannot supersede itself")
        object.__setattr__(
            self,
            "service_profile_version",
            _bounded(self.service_profile_version, field="service_profile_version", maximum=64),
        )
        object.__setattr__(
            self,
            "policy_bundle_version",
            _bounded(self.policy_bundle_version, field="policy_bundle_version", maximum=64),
        )
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))

    def role_ids(self, role: BindingRole) -> tuple[str, ...]:
        return tuple(
            sorted(
                grant.person_id
                for grant in self.roles
                if grant.role == role and grant.status == "active"
            )
        )


@dataclass(frozen=True)
class ManifestRole:
    """Serializable role entry inside a BindingManifest."""

    person_id: str
    role: BindingRole
    permissions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "person_id": self.person_id,
            "role": self.role,
            "permissions": sorted(self.permissions),
        }


@dataclass(frozen=True)
class BindingManifest:
    """The versioned contract returned by ``IdentityService``.

    Serialization is explicit (``to_dict`` / ``to_json`` / ``manifest_from_dict``)
    so Control API can persist, sign or replay manifests without re-deriving
    roles from any payer relationship.
    """

    binding_id: str
    device_id: str
    declared_mode: DeviceDeclaredMode
    binding_version: int
    status: BindingStatus
    reason: BindingReason
    supersedes_binding_id: str | None
    family_space_id: str | None
    account_owner_id: str
    device_admin_ids: tuple[str, ...]
    primary_subject_ids: tuple[str, ...]
    guardian_ids: tuple[str, ...]
    delegate_ids: tuple[str, ...]
    emergency_contact_ids: tuple[str, ...]
    member_ids: tuple[str, ...]
    roles: tuple[ManifestRole, ...]
    service_profile_version: str
    policy_bundle_version: str
    consent_snapshot_id: str | None
    persona_assignment_id: str | None
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "device_id": self.device_id,
            "declared_mode": self.declared_mode,
            "binding_version": self.binding_version,
            "status": self.status,
            "reason": self.reason,
            "supersedes_binding_id": self.supersedes_binding_id,
            "family_space_id": self.family_space_id,
            "account_owner_id": self.account_owner_id,
            "device_admin_ids": sorted(self.device_admin_ids),
            "primary_subject_ids": sorted(self.primary_subject_ids),
            "guardian_ids": sorted(self.guardian_ids),
            "delegate_ids": sorted(self.delegate_ids),
            "emergency_contact_ids": sorted(self.emergency_contact_ids),
            "member_ids": sorted(self.member_ids),
            "roles": [role.to_dict() for role in self.roles],
            "service_profile_version": self.service_profile_version,
            "policy_bundle_version": self.policy_bundle_version,
            "consent_snapshot_id": self.consent_snapshot_id,
            "persona_assignment_id": self.persona_assignment_id,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "created_at": self.created_at.isoformat(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def manifest_from_binding(binding: DeviceBinding) -> BindingManifest:
    """Derive the serializable manifest for a persisted binding version."""
    return BindingManifest(
        binding_id=binding.binding_id,
        device_id=binding.device_id,
        declared_mode=binding.declared_mode,
        binding_version=binding.binding_version,
        status=binding.status,
        reason=binding.reason,
        supersedes_binding_id=binding.supersedes_binding_id,
        family_space_id=binding.family_space_id,
        account_owner_id=binding.account_owner_person_id,
        device_admin_ids=binding.role_ids("device_admin"),
        primary_subject_ids=binding.primary_subject_ids,
        guardian_ids=binding.role_ids("guardian"),
        delegate_ids=binding.role_ids("delegate"),
        emergency_contact_ids=binding.role_ids("emergency_contact"),
        member_ids=binding.role_ids("member"),
        roles=tuple(
            ManifestRole(
                person_id=grant.person_id,
                role=grant.role,
                permissions=tuple(sorted(grant.permissions)),
            )
            for grant in sorted(binding.roles, key=lambda item: (item.person_id, item.role))
            if grant.status == "active"
        ),
        service_profile_version=binding.service_profile_version,
        policy_bundle_version=binding.policy_bundle_version,
        consent_snapshot_id=binding.consent_snapshot_id,
        persona_assignment_id=binding.persona_assignment_id,
        valid_from=binding.valid_from,
        valid_until=binding.valid_until,
        created_at=binding.created_at,
    )


def manifest_from_dict(value: dict[str, object]) -> BindingManifest:
    """Round-trip a manifest produced by ``BindingManifest.to_dict``."""

    def _string_list(key: str) -> tuple[str, ...]:
        raw = value.get(key)
        if not isinstance(raw, list):
            raise ValueError(f"manifest payload field {key} must be a list")
        return tuple(sorted(str(item) for item in raw))

    try:
        raw_roles = value["roles"]
        if not isinstance(raw_roles, list):
            raise ValueError("manifest payload field roles must be a list")
        roles = tuple(
            ManifestRole(
                person_id=str(role.get("person_id")),
                role=cast(BindingRole, str(role.get("role"))),
                permissions=tuple(sorted(permission_set(role.get("permissions") or ()))),
            )
            for role in raw_roles
            if isinstance(role, dict)
        )
        return BindingManifest(
            binding_id=str(value["binding_id"]),
            device_id=str(value["device_id"]),
            declared_mode=cast(DeviceDeclaredMode, str(value["declared_mode"])),
            binding_version=int(str(value["binding_version"])),
            status=cast(BindingStatus, str(value["status"])),
            reason=cast(BindingReason, str(value["reason"])),
            supersedes_binding_id=(
                str(value["supersedes_binding_id"])
                if value["supersedes_binding_id"] is not None
                else None
            ),
            family_space_id=str(value["family_space_id"]) if value["family_space_id"] else None,
            account_owner_id=str(value["account_owner_id"]),
            device_admin_ids=_string_list("device_admin_ids"),
            primary_subject_ids=_string_list("primary_subject_ids"),
            guardian_ids=_string_list("guardian_ids"),
            delegate_ids=_string_list("delegate_ids"),
            emergency_contact_ids=_string_list("emergency_contact_ids"),
            member_ids=_string_list("member_ids"),
            roles=roles,
            service_profile_version=str(value["service_profile_version"]),
            policy_bundle_version=str(value["policy_bundle_version"]),
            consent_snapshot_id=(
                str(value["consent_snapshot_id"])
                if value["consent_snapshot_id"] is not None
                else None
            ),
            persona_assignment_id=(
                str(value["persona_assignment_id"])
                if value["persona_assignment_id"] is not None
                else None
            ),
            valid_from=cast(datetime, _optional_datetime(value["valid_from"], field="valid_from")),
            valid_until=_optional_datetime(value["valid_until"], field="valid_until"),
            created_at=cast(datetime, _optional_datetime(value["created_at"], field="created_at")),
        )
    except KeyError as exc:
        raise ValueError(f"manifest payload is missing {exc.args[0]}") from exc


_MAX_ASSIGNMENT_ID = 64
_MAX_PERSONA_ID = 32


def canonical_persona_assignment_id(persona_id: str, persona_version: int) -> str:
    """Return the one canonical assignment id form ``"{persona_id}:v{n}"``.

    The same shape is used by the in-memory authority, the signed Runtime
    Profile payload, and the persisted binding default, so a persona never
    has two spellings.
    """
    normalized = persona_id.strip()
    if not normalized or len(normalized) > _MAX_PERSONA_ID:
        raise ValueError("persona_id must be a bounded non-empty string")
    if persona_version < 1:
        raise ValueError("persona_version must be >= 1")
    return f"{normalized}:v{persona_version}"


@dataclass(frozen=True, slots=True)
class PersonaAssignmentRecord:
    """One subject-level persona override on a device binding.

    ``(binding_id, subject_id)`` is the primary key: a person holds at most
    one persona on a binding.  ``assignment_id`` is the canonical
    ``"{persona_id}:v{version}"`` snapshot consumed by the Runtime Profile.
    A missing row means "no override": the binding default applies.
    """

    binding_id: str
    subject_id: str
    assignment_id: str
    persona_id: str
    persona_version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _bounded(self.binding_id, field="binding_id"))
        object.__setattr__(self, "subject_id", _bounded(self.subject_id, field="subject_id"))
        object.__setattr__(
            self,
            "assignment_id",
            _bounded(self.assignment_id, field="assignment_id", maximum=_MAX_ASSIGNMENT_ID),
        )
        object.__setattr__(
            self, "persona_id", _bounded(self.persona_id, field="persona_id", maximum=_MAX_PERSONA_ID)
        )
        if self.persona_version < 1:
            raise ValueError("persona_version must be >= 1")
        if self.assignment_id != canonical_persona_assignment_id(
            self.persona_id, self.persona_version
        ):
            raise ValueError(
                "assignment_id must equal '{persona_id}:v{persona_version}'"
            )
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, field="updated_at"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PersonaAssignmentRecord:
        """Build a validated record from a persisted row/mapping."""
        try:
            return cls(
                binding_id=str(value["binding_id"]),
                subject_id=str(value["subject_id"]),
                assignment_id=str(value["assignment_id"]),
                persona_id=str(value["persona_id"]),
                persona_version=int(str(value["persona_version"])),
                created_at=cast(
                    datetime, _optional_datetime(value["created_at"], field="created_at")
                ),
                updated_at=cast(
                    datetime, _optional_datetime(value["updated_at"], field="updated_at")
                ),
            )
        except KeyError as exc:
            raise ValueError(f"persona assignment payload is missing {exc.args[0]}") from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "subject_id": self.subject_id,
            "assignment_id": self.assignment_id,
            "persona_id": self.persona_id,
            "persona_version": self.persona_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def effective_permissions(manifest: BindingManifest, person_id: str) -> frozenset[str]:
    """Union of active role permissions for a person within a manifest."""
    return frozenset(
        {
            permission
            for role in manifest.roles
            if role.person_id == person_id
            for permission in role.permissions
        }
    )


def has_permission(manifest: BindingManifest, person_id: str, permission: str) -> bool:
    return permission in effective_permissions(manifest, person_id)


def validate_manifest_wire(manifest: BindingManifest) -> None:
    """Boundary-validate a manifest against the canonical generated contract.

    Raises ``ValueError`` when the serialized manifest deviates from the
    generated ``BindingManifest`` schema (unknown enums, missing/extra fields,
    malformed timestamps, invalid identifiers).  Consumer drift is a release
    blocker (section 9.6), so every API boundary re-checks before emitting.
    """
    CanonicalBindingManifest.model_validate(manifest.to_dict())


# ---------------------------------------------------------------------------
# Custom personas (increment two): immutable per-account persona entities
# ---------------------------------------------------------------------------
#
# A custom persona is an account-level, create-once entity: it owns a
# ``cu_``-prefixed id inside the SAME persona-id namespace as the built-in
# catalogue (``starlight`` ...).  Its structured fields mirror the controlled
# allowlist in ``services/persona/custom_persona_fields.py`` and the CHECK
# constraints of ``identity_custom_personas``; this module is the last line of
# fail-closed defence before an adapter persists a row.

CUSTOM_PERSONA_ID_PREFIX = "cu_"
MAX_CUSTOM_PERSONAS_PER_OWNER = 5
MAX_CUSTOM_PERSONA_DISPLAY_NAME = 16
MAX_STYLE_DESCRIPTION = 60
MAX_WELCOME_TEXT = 60
MAX_CONVERSATION_INSTRUCTION = 200
MAX_VOICE_INSTRUCTION = 120
MIN_DEFAULT_VOICE_RATE = 0.90
MAX_DEFAULT_VOICE_RATE = 1.10

CUSTOM_PERSONA_WARMTH: frozenset[str] = frozenset(
    {"warm", "bright", "soft", "calm", "reserved"}
)
CUSTOM_PERSONA_DIRECTNESS: frozenset[str] = frozenset({"gentle", "direct"})
CUSTOM_PERSONA_RESPONSE_LENGTH: frozenset[str] = frozenset({"brief", "balanced"})
CUSTOM_PERSONA_QUESTION_FREQUENCY: frozenset[str] = frozenset(
    {"rare", "occasional", "frequent"}
)
CUSTOM_PERSONA_INTERVIEW_DEPTH: frozenset[str] = frozenset(
    {"light", "structured", "on_explicit_invitation"}
)
CUSTOM_PERSONA_VOICE_EMOTION: frozenset[str] = frozenset({"neutral", "happy"})
CUSTOM_PERSONA_SOURCES: frozenset[str] = frozenset({"user_created", "bio_migration"})

_CUSTOM_PERSONA_ID_PATTERN = re.compile(r"^cu_[0-9a-f]{16,29}$")


def new_custom_persona_id() -> str:
    """``cu_`` + 26 hex uuid4 chars: opaque, globally unique, at most 32 chars.

    The 3-char ``cu_`` prefix plus 26 opaque hex chars (104 bits of entropy)
    stays within the 32-char persona-id ceiling shared with the built-ins and
    matches the ``^cu_[0-9a-f]{16,29}$`` database check.
    """
    return f"{CUSTOM_PERSONA_ID_PREFIX}{uuid.uuid4().hex[:26]}"


def is_custom_persona_id(persona_id: object) -> bool:
    """Whether ``persona_id`` lives in the ``cu_`` custom namespace."""
    return isinstance(persona_id, str) and persona_id.startswith(
        CUSTOM_PERSONA_ID_PREFIX
    )


def _custom_text(value: str, *, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return value


@dataclass(frozen=True, slots=True)
class CustomPersonaRecord:
    """One immutable account-level custom persona (``v1`` forever).

    The structured field allowlist is fixed: the eleven controlled style
    fields plus ``fallback_designed_voice`` (an approved built-in companion id
    used when the persona has no bound clone).  ``owner_person_id`` is the
    account owner; a persona may be referenced by several bindings/subjects of
    that account.  Renaming is impossible -- to change anything, create anew.
    """

    persona_id: str
    owner_person_id: str
    display_name: str
    style_description: str
    warmth: str
    directness: str
    response_length: str
    question_frequency: str
    interview_depth: str
    welcome_text: str
    conversation_instruction: str
    voice_instruction: str
    default_voice_emotion: str
    default_voice_rate: float
    fallback_designed_voice: str
    persona_version: int
    source: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not _CUSTOM_PERSONA_ID_PATTERN.match(self.persona_id) or len(
            self.persona_id
        ) > _MAX_PERSONA_ID:
            raise ValueError(
                "persona_id must be 'cu_' + 16..29 lower-case hex (<= 32 chars)"
            )
        object.__setattr__(
            self,
            "owner_person_id",
            _bounded(self.owner_person_id, field="owner_person_id"),
        )
        if not (1 <= len(self.display_name) <= MAX_CUSTOM_PERSONA_DISPLAY_NAME):
            raise ValueError(
                "display_name must be 1..16 characters"
            )
        object.__setattr__(
            self,
            "style_description",
            _custom_text(
                self.style_description,
                field="style_description",
                maximum=MAX_STYLE_DESCRIPTION,
            ),
        )
        if self.warmth not in CUSTOM_PERSONA_WARMTH:
            raise ValueError(f"warmth must be one of {sorted(CUSTOM_PERSONA_WARMTH)}")
        if self.directness not in CUSTOM_PERSONA_DIRECTNESS:
            raise ValueError(
                f"directness must be one of {sorted(CUSTOM_PERSONA_DIRECTNESS)}"
            )
        if self.response_length not in CUSTOM_PERSONA_RESPONSE_LENGTH:
            raise ValueError(
                "response_length must be one of "
                f"{sorted(CUSTOM_PERSONA_RESPONSE_LENGTH)}"
            )
        if self.question_frequency not in CUSTOM_PERSONA_QUESTION_FREQUENCY:
            raise ValueError(
                "question_frequency must be one of "
                f"{sorted(CUSTOM_PERSONA_QUESTION_FREQUENCY)}"
            )
        if self.interview_depth not in CUSTOM_PERSONA_INTERVIEW_DEPTH:
            raise ValueError(
                "interview_depth must be one of "
                f"{sorted(CUSTOM_PERSONA_INTERVIEW_DEPTH)}"
            )
        object.__setattr__(
            self,
            "welcome_text",
            _custom_text(
                self.welcome_text, field="welcome_text", maximum=MAX_WELCOME_TEXT
            ),
        )
        object.__setattr__(
            self,
            "conversation_instruction",
            _custom_text(
                self.conversation_instruction,
                field="conversation_instruction",
                maximum=MAX_CONVERSATION_INSTRUCTION,
            ),
        )
        object.__setattr__(
            self,
            "voice_instruction",
            _custom_text(
                self.voice_instruction,
                field="voice_instruction",
                maximum=MAX_VOICE_INSTRUCTION,
            ),
        )
        if self.default_voice_emotion not in CUSTOM_PERSONA_VOICE_EMOTION:
            raise ValueError(
                "default_voice_emotion must be one of "
                f"{sorted(CUSTOM_PERSONA_VOICE_EMOTION)}"
            )
        if not isinstance(self.default_voice_rate, (int, float)) or isinstance(
            self.default_voice_rate, bool
        ):
            raise ValueError("default_voice_rate must be a number")
        rate = float(self.default_voice_rate)
        if not (MIN_DEFAULT_VOICE_RATE <= rate <= MAX_DEFAULT_VOICE_RATE):
            raise ValueError(
                "default_voice_rate must be within "
                f"[{MIN_DEFAULT_VOICE_RATE}, {MAX_DEFAULT_VOICE_RATE}]"
            )
        object.__setattr__(self, "default_voice_rate", rate)
        object.__setattr__(
            self,
            "fallback_designed_voice",
            _bounded(
                self.fallback_designed_voice,
                field="fallback_designed_voice",
                maximum=_MAX_PERSONA_ID,
            ),
        )
        if self.persona_version != 1:
            raise ValueError("custom personas are immutable and always version 1")
        if self.source not in CUSTOM_PERSONA_SOURCES:
            raise ValueError(f"source must be one of {sorted(CUSTOM_PERSONA_SOURCES)}")
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CustomPersonaRecord:
        """Build a validated record from a persisted row/mapping."""
        try:
            return cls(
                persona_id=str(value["persona_id"]),
                owner_person_id=str(value["owner_person_id"]),
                display_name=str(value["display_name"]),
                style_description=str(value["style_description"]),
                warmth=str(value["warmth"]),
                directness=str(value["directness"]),
                response_length=str(value["response_length"]),
                question_frequency=str(value["question_frequency"]),
                interview_depth=str(value["interview_depth"]),
                welcome_text=str(value["welcome_text"]),
                conversation_instruction=str(value["conversation_instruction"]),
                voice_instruction=str(value["voice_instruction"]),
                default_voice_emotion=str(value["default_voice_emotion"]),
                default_voice_rate=float(str(value["default_voice_rate"])),
                fallback_designed_voice=str(value["fallback_designed_voice"]),
                persona_version=int(str(value["persona_version"])),
                source=str(value["source"]),
                created_at=cast(
                    datetime,
                    _optional_datetime(value["created_at"], field="created_at"),
                ),
            )
        except KeyError as exc:
            raise ValueError(
                f"custom persona payload is missing {exc.args[0]}"
            ) from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "persona_id": self.persona_id,
            "owner_person_id": self.owner_person_id,
            "display_name": self.display_name,
            "style_description": self.style_description,
            "warmth": self.warmth,
            "directness": self.directness,
            "response_length": self.response_length,
            "question_frequency": self.question_frequency,
            "interview_depth": self.interview_depth,
            "welcome_text": self.welcome_text,
            "conversation_instruction": self.conversation_instruction,
            "voice_instruction": self.voice_instruction,
            "default_voice_emotion": self.default_voice_emotion,
            "default_voice_rate": self.default_voice_rate,
            "fallback_designed_voice": self.fallback_designed_voice,
            "persona_version": self.persona_version,
            "source": self.source,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class CustomPersonaDeleteOutcome:
    """Result of deleting one custom persona in a single transaction.

    ``drifted_subjects`` counts the ``identity_persona_assignments`` rows that
    referenced the persona and were dropped by the same transaction; those
    subjects deterministically fall back to the binding default.
    """

    persona_id: str
    deleted: bool
    drifted_subjects: int


class CustomPersonaLimitError(IdentityConflictError):
    """Raised when an account already owns ``limit`` custom personas."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"custom persona limit reached (limit={limit})")
        self.limit = limit


@dataclass(frozen=True, slots=True)
class StructuredPersonaFields:
    """The eleven controlled style fields accepted by ``create_custom_persona``.

    This is a plain transport container: the authoritative validation lives in
    ``CustomPersonaRecord.__post_init__`` (which mirrors the DB CHECKs), so a
    malformed field fails closed before any adapter writes a row.
    """

    style_description: str = ""
    warmth: str = "warm"
    directness: str = "gentle"
    response_length: str = "balanced"
    question_frequency: str = "occasional"
    interview_depth: str = "light"
    welcome_text: str = ""
    conversation_instruction: str = ""
    voice_instruction: str = ""
    default_voice_emotion: str = "neutral"
    default_voice_rate: float = 1.0
