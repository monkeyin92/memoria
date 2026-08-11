"""Immutable structured consent payloads with canonical tamper detection.

The canonical hash covers the complete stored field set and detects mutation;
it is not a signature. Subject, binding, and relationship authority comes from
an Identity-owned resolver/adapter, not from constructing these values locally.

All datetimes are normalized to UTC and must be timezone-aware (naive datetimes
are rejected).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    ALL_CAPABILITY_VALUES,
    ALL_DEVICE_DECLARED_MODE_VALUES,
    ALL_PURPOSE_VALUES,
    ALL_RELATIONSHIP_STATUS_VALUES,
    ALL_RELATIONSHIP_TYPE_VALUES,
    OBJECT_INVARIANTS,
    CapabilityValue,
    DeviceDeclaredModeValue,
    PurposeValue,
    RelationshipStatusValue,
    RelationshipTypeValue,
)

type ConsentStatus = Literal["active", "revoked", "expired", "disputed", "superseded"]
type ConsentOfferStatus = Literal["active", "revoked", "expired", "superseded"]
type ActorKind = Literal["subject", "guardian", "family_admin", "service", "other"]
# Wire seam dependency: Identity owns these directed values today. Move this
# alias to packages/contracts when the cross-domain relationship enum converges.
RelationTypeValue = RelationshipTypeValue
ALL_RELATION_TYPE_VALUES: frozenset[str] = frozenset(
    item.value for item in ALL_RELATIONSHIP_TYPE_VALUES
)
ALL_CAPABILITY_VALUES_SET: frozenset[str] = frozenset(c.value for c in ALL_CAPABILITY_VALUES)
ALL_DECLARED_MODE_VALUES_SET: frozenset[str] = frozenset(
    m.value for m in ALL_DEVICE_DECLARED_MODE_VALUES
)
ALL_RELATIONSHIP_STATUS_VALUES_SET: frozenset[str] = frozenset(
    s.value for s in ALL_RELATIONSHIP_STATUS_VALUES
)
ALL_CONSENT_STATUS_VALUES: frozenset[str] = frozenset(
    {"active", "revoked", "expired", "disputed", "superseded"}
)
ALL_ACTOR_KIND_VALUES: frozenset[str] = frozenset(
    {"subject", "guardian", "family_admin", "service", "other"}
)
ALL_CONSENT_OFFER_STATUS_VALUES: frozenset[str] = frozenset(
    {"active", "revoked", "expired", "superseded"}
)
# Canonical cross-domain wire value. Identity authority still emits the legacy
# ``device_ownership_transfer`` purpose and must switch to this value when its
# transfer receipt seam is connected to Consent.
DEVICE_TRANSFER_PURPOSE = "device_transfer"
ALLOWED_PURPOSES: frozenset[str] = frozenset(
    item.value for item in ALL_PURPOSE_VALUES
)


def _generated_memory_capability_purpose_pairs() -> frozenset[tuple[str, str]]:
    """Read the bidirectional v2 pairs from the generated object invariant."""
    for rule in OBJECT_INVARIANTS["PolicyReceiptV2"]:
        if rule.get("kind") != "value_pairs":
            continue
        pairs = rule.get("pairs")
        if not isinstance(pairs, dict):
            raise RuntimeError("generated PolicyReceiptV2 value_pairs must be an object")
        return frozenset((str(capability), str(purpose)) for capability, purpose in pairs.items())
    raise RuntimeError("generated PolicyReceiptV2 lacks capability/purpose value_pairs")


CANONICAL_MEMORY_CAPABILITY_PURPOSE_PAIRS = _generated_memory_capability_purpose_pairs()
_CANONICAL_MEMORY_PURPOSE_BY_CAPABILITY = dict(CANONICAL_MEMORY_CAPABILITY_PURPOSE_PAIRS)
_CANONICAL_MEMORY_CAPABILITY_BY_PURPOSE = {
    purpose: capability for capability, purpose in CANONICAL_MEMORY_CAPABILITY_PURPOSE_PAIRS
}


def _validate_capability_purpose(capability: str, purpose: str) -> None:
    if purpose not in ALLOWED_PURPOSES:
        raise ValueError(f"unknown consent purpose {purpose!r}")
    expected_purpose = _CANONICAL_MEMORY_PURPOSE_BY_CAPABILITY.get(capability)
    paired_capability = _CANONICAL_MEMORY_CAPABILITY_BY_PURPOSE.get(purpose)
    if (
        expected_purpose is not None
        and purpose != expected_purpose
        or paired_capability is not None
        and capability != paired_capability
    ):
        raise ValueError(
            f"canonical capability/purpose mismatch: {capability!r}/{purpose!r}"
        )

# Canonical serialization order per type (fixed fields first; params/extras last).
# The ``canonical_hash`` field is excluded because it is derived from the rest.
CANONICAL_FIELD_ORDER_CONSENT: tuple[str, ...] = (
    "consent_id",
    "version",
    "snapshot_id",
    "status",
    "subject_id",
    "resource_owner_id",
    "actor_id",
    "actor_kind",
    "device_id",
    "binding_id",
    "binding_version",
    "capability",
    "purpose",
    "policy_version",
    "evidence_id",
    "offer_id",
    "offer_version",
    "offer_hash",
    "idempotency_key",
    "params",
    "valid_from",
    "valid_until",
    "supersedes_consent_id",
    "superseded_by_consent_id",
)
CANONICAL_FIELD_ORDER_RELATIONSHIP: tuple[str, ...] = (
    "relationship_id",
    "snapshot_id",
    "revision",
    "relation_type",
    "status",
    "source_person_id",
    "target_person_id",
    "binding_id",
    "valid_from",
    "valid_until",
)
CANONICAL_FIELD_ORDER_BINDING: tuple[str, ...] = (
    "binding_id",
    "version",
    "device_id",
    "status",
    "declared_mode",
    "valid_from",
    "valid_until",
)
CANONICAL_FIELD_ORDER_SNAPSHOT: tuple[str, ...] = (
    "snapshot_id",
    "version",
    "subject_id",
    "binding_id",
    "binding_version",
    "policy_version",
    "created_at",
    "grants",
    "relationships",
    "binding",
)
CANONICAL_FIELD_ORDER_PARAMS: tuple[str, ...] = (
    "max_session_seconds",
    "retention_ttl_seconds",
    "quiet_hours",
    "extras",
)
CANONICAL_FIELD_ORDER_OFFER: tuple[str, ...] = (
    "offer_id",
    "version",
    "status",
    "capability",
    "subject_id",
    "actor_id",
    "resource_owner_id",
    "purpose",
    "params",
    "valid_from",
    "valid_until",
    "created_at",
    "issuer",
    "policy_version",
    "supersedes_offer_id",
)

_QUIET_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


def canonical_json(value: object) -> str:
    """Deterministic compact JSON (sorted keys, no spaces)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def compute_canonical_hash(value: object) -> str:
    """sha256 hex over the canonical JSON projection of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, field_name: str, maximum: int = 128) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must be a bounded non-empty string")
    return normalized


def _positive(value: int, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be an integer > 0")
    return value


def _validate_hash(canonical_hash: str, computed: str) -> str:
    if not _HASH_RE.fullmatch(canonical_hash):
        raise ValueError("canonical_hash must be a 64-char lowercase hex sha256")
    if canonical_hash != computed:
        raise ValueError("canonical_hash does not match the authoritative fields")
    return canonical_hash


@dataclass(frozen=True, slots=True)
class ConsentParams:
    """Auditable grant parameters. Constructing with invalid values raises ValueError."""

    max_session_seconds: int | None = None
    retention_ttl_seconds: int | None = None
    quiet_hours: tuple[str, str] | None = None
    extras: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if self.max_session_seconds is not None:
            object.__setattr__(
                self,
                "max_session_seconds",
                _positive(self.max_session_seconds, "max_session_seconds"),
            )
        if self.retention_ttl_seconds is not None:
            object.__setattr__(
                self,
                "retention_ttl_seconds",
                _positive(self.retention_ttl_seconds, "retention_ttl_seconds"),
            )
        if self.quiet_hours is not None:
            if len(self.quiet_hours) != 2 or not all(
                _QUIET_HOURS_RE.fullmatch(hour) for hour in self.quiet_hours
            ):
                raise ValueError("quiet_hours must be a pair of HH:MM 24-hour strings")
        merged: dict[str, str] = {}
        for key, value in self.extras:
            _bounded(key, "extras key")
            _bounded(value, "extras value")
            merged[key] = value
        object.__setattr__(self, "extras", tuple(sorted(merged.items())))

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "max_session_seconds": self.max_session_seconds,
            "retention_ttl_seconds": self.retention_ttl_seconds,
            "quiet_hours": list(self.quiet_hours) if self.quiet_hours is not None else None,
            "extras": [[key, value] for key, value in self.extras],
        }


def _params_from_dict(data: dict[str, object], *, field_name: str) -> ConsentParams:
    _require_exact_fields(data, CANONICAL_FIELD_ORDER_PARAMS, f"{field_name} params")
    quiet_hours: tuple[str, str] | None = None
    raw_quiet = data["quiet_hours"]
    if raw_quiet is not None:
        if type(raw_quiet) is not list or len(raw_quiet) != 2:
            raise ValueError(f"{field_name}: quiet_hours must be a pair")
        quiet_hours = (
            _as_str(raw_quiet[0], "quiet_hours[0]"),
            _as_str(raw_quiet[1], "quiet_hours[1]"),
        )
    extras: tuple[tuple[str, str], ...] = ()
    raw_extras = data["extras"]
    if type(raw_extras) is not list:
        raise ValueError(f"{field_name}: extras must be a list")
    decoded_extras: list[tuple[str, str]] = []
    for index, pair in enumerate(raw_extras):
        if type(pair) is not list or len(pair) != 2:
            raise ValueError(f"{field_name}: extras[{index}] must be a two-item list")
        decoded_extras.append(
            (
                _as_str(pair[0], f"extras[{index}][0]"),
                _as_str(pair[1], f"extras[{index}][1]"),
            )
        )
    extras = tuple(decoded_extras)
    return ConsentParams(
        max_session_seconds=(
            _as_int(data["max_session_seconds"], "max_session_seconds")
            if data["max_session_seconds"] is not None
            else None
        ),
        retention_ttl_seconds=(
            _as_int(data["retention_ttl_seconds"], "retention_ttl_seconds")
            if data["retention_ttl_seconds"] is not None
            else None
        ),
        quiet_hours=quiet_hours,
        extras=extras,
    )


@dataclass(frozen=True, slots=True)
class ConsentEvidence:
    """Immutable, versioned grant/revoke/dispute/expire row (append-only)."""

    consent_id: str
    version: int
    snapshot_id: str
    status: ConsentStatus
    subject_id: str
    resource_owner_id: str
    actor_id: str
    actor_kind: ActorKind
    device_id: str | None
    binding_id: str
    binding_version: int
    capability: CapabilityValue
    purpose: PurposeValue
    policy_version: str
    evidence_id: str
    offer_id: str
    idempotency_key: str | None
    params: ConsentParams
    valid_from: datetime
    valid_until: datetime
    supersedes_consent_id: str | None
    superseded_by_consent_id: str | None
    offer_version: int = 1
    offer_hash: str = ""
    canonical_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "consent_id",
            "snapshot_id",
            "subject_id",
            "resource_owner_id",
            "actor_id",
            "binding_id",
            "evidence_id",
            "offer_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _bounded(getattr(self, field_name), field_name=field_name),
            )
        if self.device_id is not None:
            object.__setattr__(self, "device_id", _bounded(self.device_id, "device_id"))
        for field_name in ("purpose", "policy_version"):
            maximum = 256 if field_name == "purpose" else 128
            object.__setattr__(
                self,
                field_name,
                _bounded(getattr(self, field_name), field_name=field_name, maximum=maximum),
            )
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                _bounded(self.idempotency_key, "idempotency_key"),
            )
        if self.supersedes_consent_id is not None:
            object.__setattr__(
                self,
                "supersedes_consent_id",
                _bounded(self.supersedes_consent_id, "supersedes_consent_id"),
            )
        if self.superseded_by_consent_id is not None:
            object.__setattr__(
                self,
                "superseded_by_consent_id",
                _bounded(self.superseded_by_consent_id, "superseded_by_consent_id"),
            )
        object.__setattr__(self, "version", _positive(self.version, "version"))
        object.__setattr__(
            self, "binding_version", _positive(self.binding_version, "binding_version")
        )
        object.__setattr__(self, "offer_version", _positive(self.offer_version, "offer_version"))
        if self.offer_hash and not _HASH_RE.fullmatch(self.offer_hash):
            raise ValueError("offer_hash must be a sha256 hex digest")
        if self.status not in ALL_CONSENT_STATUS_VALUES:
            raise ValueError(f"unknown consent status {self.status!r}")
        if self.capability not in ALL_CAPABILITY_VALUES_SET:
            raise ValueError(f"unknown capability {self.capability!r}")
        _validate_capability_purpose(self.capability, self.purpose)
        if self.actor_kind not in ALL_ACTOR_KIND_VALUES:
            raise ValueError(f"unknown actor_kind {self.actor_kind!r}")
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "valid_until", _utc(self.valid_until, "valid_until"))
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        computed = compute_canonical_hash(self._canonical_payload())
        if self.canonical_hash:
            object.__setattr__(
                self, "canonical_hash", _validate_hash(self.canonical_hash, computed)
            )
        else:
            object.__setattr__(self, "canonical_hash", computed)

    def _canonical_payload(self) -> dict[str, object]:
        return {
            "consent_id": self.consent_id,
            "version": self.version,
            "snapshot_id": self.snapshot_id,
            "status": self.status,
            "subject_id": self.subject_id,
            "resource_owner_id": self.resource_owner_id,
            "actor_id": self.actor_id,
            "actor_kind": self.actor_kind,
            "device_id": self.device_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "capability": self.capability,
            "purpose": self.purpose,
            "policy_version": self.policy_version,
            "evidence_id": self.evidence_id,
            "offer_id": self.offer_id,
            "offer_version": self.offer_version,
            "offer_hash": self.offer_hash,
            "idempotency_key": self.idempotency_key,
            "params": self.params.to_canonical_dict(),
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "supersedes_consent_id": self.supersedes_consent_id,
            "superseded_by_consent_id": self.superseded_by_consent_id,
        }

    def to_canonical_dict(self) -> dict[str, object]:
        payload = self._canonical_payload()
        payload["canonical_hash"] = self.canonical_hash
        return payload

    @classmethod
    def from_canonical_dict(cls, data: dict[str, object]) -> ConsentEvidence:
        _require_exact_fields(
            data,
            (*CANONICAL_FIELD_ORDER_CONSENT, "canonical_hash"),
            "consent evidence",
        )
        try:
            return cls(
                consent_id=_as_str(data["consent_id"], "consent_id"),
                version=_as_int(data["version"], "version"),
                snapshot_id=_as_str(data["snapshot_id"], "snapshot_id"),
                status=cast(ConsentStatus, _as_str(data["status"], "status")),
                subject_id=_as_str(data["subject_id"], "subject_id"),
                resource_owner_id=_as_str(
                    data["resource_owner_id"], "resource_owner_id"
                ),
                actor_id=_as_str(data["actor_id"], "actor_id"),
                actor_kind=cast(ActorKind, _as_str(data["actor_kind"], "actor_kind")),
                device_id=_as_optional_str(data["device_id"], "device_id"),
                binding_id=_as_str(data["binding_id"], "binding_id"),
                binding_version=_as_int(data["binding_version"], "binding_version"),
                capability=cast(
                    CapabilityValue, _as_str(data["capability"], "capability")
                ),
                purpose=cast(PurposeValue, _as_str(data["purpose"], "purpose")),
                policy_version=_as_str(data["policy_version"], "policy_version"),
                evidence_id=_as_str(data["evidence_id"], "evidence_id"),
                offer_id=_as_str(data["offer_id"], "offer_id"),
                offer_version=_as_int(data["offer_version"], "offer_version"),
                offer_hash=_as_str(data["offer_hash"], "offer_hash"),
                idempotency_key=_as_optional_str(
                    data["idempotency_key"], "idempotency_key"
                ),
                params=_params_from_dict(
                    _require_dict(data["params"], "params"), field_name="params"
                ),
                valid_from=_datetime_from_iso(data["valid_from"], "valid_from"),
                valid_until=_datetime_from_iso(data["valid_until"], "valid_until"),
                supersedes_consent_id=_as_optional_str(
                    data["supersedes_consent_id"], "supersedes_consent_id"
                ),
                superseded_by_consent_id=_as_optional_str(
                    data["superseded_by_consent_id"], "superseded_by_consent_id"
                ),
                canonical_hash=_as_str(data["canonical_hash"], "canonical_hash"),
            )
        except KeyError as exc:
            raise ValueError(f"consent evidence missing {exc.args[0]}") from exc

    def is_effective_at(self, now: datetime) -> bool:
        _utc(now, "now")
        return self.status == "active" and self.valid_from <= now < self.valid_until

    def matches(
        self,
        subject_id: str,
        binding_id: str,
        binding_version: int,
        capability: CapabilityValue,
    ) -> bool:
        return (
            self.subject_id == subject_id
            and self.binding_id == binding_id
            and self.binding_version == binding_version
            and self.capability == capability
        )


@dataclass(frozen=True, slots=True)
class RelationshipEvidence:
    """Directed Identity-authority payload: source person -> target person.

    Relation types are owned by Identity until packages/contracts exposes the
    shared enum. Generic contacts must never be encoded in endpoint role fields.
    """

    relationship_id: str
    snapshot_id: str
    revision: int
    relation_type: RelationTypeValue
    status: RelationshipStatusValue
    source_person_id: str
    target_person_id: str
    binding_id: str
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "relationship_id",
            "snapshot_id",
            "source_person_id",
            "target_person_id",
            "binding_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _bounded(getattr(self, field_name), field_name=field_name),
            )
        object.__setattr__(self, "revision", _positive(self.revision, "revision"))
        if self.relation_type not in ALL_RELATION_TYPE_VALUES:
            raise ValueError(f"unknown relation_type {self.relation_type!r}")
        if self.status not in ALL_RELATIONSHIP_STATUS_VALUES_SET:
            raise ValueError(f"unknown relationship status {self.status!r}")
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "valid_until", _utc(self.valid_until, "valid_until"))
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        computed = compute_canonical_hash(self._canonical_payload())
        if self.canonical_hash:
            object.__setattr__(
                self, "canonical_hash", _validate_hash(self.canonical_hash, computed)
            )
        else:
            object.__setattr__(self, "canonical_hash", computed)

    def _canonical_payload(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "snapshot_id": self.snapshot_id,
            "revision": self.revision,
            "relation_type": self.relation_type,
            "status": self.status,
            "source_person_id": self.source_person_id,
            "target_person_id": self.target_person_id,
            "binding_id": self.binding_id,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
        }

    def to_canonical_dict(self) -> dict[str, object]:
        payload = self._canonical_payload()
        payload["canonical_hash"] = self.canonical_hash
        return payload

    @classmethod
    def from_canonical_dict(cls, data: dict[str, object]) -> RelationshipEvidence:
        _require_exact_fields(
            data,
            (*CANONICAL_FIELD_ORDER_RELATIONSHIP, "canonical_hash"),
            "relationship",
        )
        try:
            return cls(
                relationship_id=_as_str(data["relationship_id"], "relationship_id"),
                snapshot_id=_as_str(data["snapshot_id"], "snapshot_id"),
                revision=_as_int(data["revision"], "revision"),
                relation_type=cast(
                    RelationTypeValue, _as_str(data["relation_type"], "relation_type")
                ),
                status=cast(
                    RelationshipStatusValue, _as_str(data["status"], "status")
                ),
                source_person_id=_as_str(data["source_person_id"], "source_person_id"),
                target_person_id=_as_str(data["target_person_id"], "target_person_id"),
                binding_id=_as_str(data["binding_id"], "binding_id"),
                valid_from=_datetime_from_iso(data["valid_from"], "valid_from"),
                valid_until=_datetime_from_iso(data["valid_until"], "valid_until"),
                canonical_hash=_as_str(data["canonical_hash"], "canonical_hash"),
            )
        except KeyError as exc:
            raise ValueError(f"relationship evidence missing {exc.args[0]}") from exc

    def is_active_at(self, now: datetime) -> bool:
        _utc(now, "now")
        return self.status == "active" and self.valid_from <= now < self.valid_until


def is_guardian_of(evidence: RelationshipEvidence, *, guardian_id: str, subject_id: str) -> bool:
    """Project only Identity ``guardian_of`` in its canonical direction."""
    return (
        evidence.relation_type == "guardian_of"
        and evidence.source_person_id == guardian_id
        and evidence.target_person_id == subject_id
    )


def is_parent_of(evidence: RelationshipEvidence, *, parent_id: str, subject_id: str) -> bool:
    """Project only Identity ``parent_of`` in its canonical direction."""
    return (
        evidence.relation_type == "parent_of"
        and evidence.source_person_id == parent_id
        and evidence.target_person_id == subject_id
    )


@dataclass(frozen=True, slots=True)
class BindingEvidence:
    """Structured binding payload verified by an authority resolver."""

    binding_id: str
    version: int
    device_id: str
    status: Literal["active", "superseded", "revoked", "expired"]
    declared_mode: DeviceDeclaredModeValue
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "binding_id", _bounded(self.binding_id, "binding_id"))
        object.__setattr__(self, "device_id", _bounded(self.device_id, "device_id"))
        object.__setattr__(self, "version", _positive(self.version, "version"))
        if self.status not in {"active", "superseded", "revoked", "expired"}:
            raise ValueError(f"unknown binding status {self.status!r}")
        if self.declared_mode not in ALL_DECLARED_MODE_VALUES_SET:
            raise ValueError(f"unknown declared_mode {self.declared_mode!r}")
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "valid_until", _utc(self.valid_until, "valid_until"))
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        computed = compute_canonical_hash(self._canonical_payload())
        if self.canonical_hash:
            object.__setattr__(
                self, "canonical_hash", _validate_hash(self.canonical_hash, computed)
            )
        else:
            object.__setattr__(self, "canonical_hash", computed)

    def _canonical_payload(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id,
            "version": self.version,
            "device_id": self.device_id,
            "status": self.status,
            "declared_mode": self.declared_mode,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
        }

    def to_canonical_dict(self) -> dict[str, object]:
        payload = self._canonical_payload()
        payload["canonical_hash"] = self.canonical_hash
        return payload

    @classmethod
    def from_canonical_dict(cls, data: dict[str, object]) -> BindingEvidence:
        _require_exact_fields(
            data,
            (*CANONICAL_FIELD_ORDER_BINDING, "canonical_hash"),
            "binding",
        )
        try:
            return cls(
                binding_id=_as_str(data["binding_id"], "binding_id"),
                version=_as_int(data["version"], "version"),
                device_id=_as_str(data["device_id"], "device_id"),
                status=cast(
                    Literal["active", "superseded", "revoked", "expired"],
                    _as_str(data["status"], "status"),
                ),
                declared_mode=cast(
                    DeviceDeclaredModeValue,
                    _as_str(data["declared_mode"], "declared_mode"),
                ),
                valid_from=_datetime_from_iso(data["valid_from"], "valid_from"),
                valid_until=_datetime_from_iso(data["valid_until"], "valid_until"),
                canonical_hash=_as_str(data["canonical_hash"], "canonical_hash"),
            )
        except KeyError as exc:
            raise ValueError(f"binding evidence missing {exc.args[0]}") from exc

    def is_active_at(self, now: datetime) -> bool:
        _utc(now, "now")
        return self.status == "active" and self.valid_from <= now < self.valid_until


@dataclass(frozen=True, slots=True)
class ConsentSnapshot:
    """Immutable, versioned consent snapshot bound to a binding version."""

    snapshot_id: str
    version: int
    subject_id: str
    binding_id: str
    binding_version: int
    policy_version: str
    created_at: datetime
    grants: tuple[ConsentEvidence, ...]
    relationships: tuple[RelationshipEvidence, ...]
    binding: BindingEvidence
    canonical_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _bounded(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "subject_id", _bounded(self.subject_id, "subject_id"))
        object.__setattr__(self, "binding_id", _bounded(self.binding_id, "binding_id"))
        object.__setattr__(
            self,
            "policy_version",
            _bounded(self.policy_version, "policy_version"),
        )
        object.__setattr__(self, "version", _positive(self.version, "version"))
        object.__setattr__(
            self, "binding_version", _positive(self.binding_version, "binding_version")
        )
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if not isinstance(self.grants, tuple):
            raise ValueError("grants must be a tuple")
        if not isinstance(self.relationships, tuple):
            raise ValueError("relationships must be a tuple")
        if self.binding.binding_id != self.binding_id:
            raise ValueError("snapshot binding must match binding_id")
        if self.binding.version != self.binding_version:
            raise ValueError("snapshot binding version must match binding_version")
        for grant in self.grants:
            if grant.subject_id != self.subject_id:
                raise ValueError("grant subject must match snapshot subject")
            if grant.binding_id != self.binding_id:
                raise ValueError("grant binding must match snapshot binding")
            if grant.binding_version != self.binding_version:
                raise ValueError("grant binding version must match snapshot binding version")
        for relationship in self.relationships:
            if relationship.binding_id != self.binding_id:
                raise ValueError("relationship binding must match snapshot binding")
        computed = compute_canonical_hash(self._canonical_payload())
        if self.canonical_hash:
            object.__setattr__(
                self, "canonical_hash", _validate_hash(self.canonical_hash, computed)
            )
        else:
            object.__setattr__(self, "canonical_hash", computed)

    def _canonical_payload(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "subject_id": self.subject_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "policy_version": self.policy_version,
            "created_at": self.created_at.isoformat(),
            "grants": [grant._canonical_payload() for grant in self.grants],
            "relationships": [
                relationship._canonical_payload() for relationship in self.relationships
            ],
            "binding": self.binding._canonical_payload(),
        }

    def to_canonical_dict(self) -> dict[str, object]:
        payload = self._canonical_payload()
        payload["canonical_hash"] = self.canonical_hash
        return payload

    @classmethod
    def from_canonical_dict(cls, data: dict[str, object]) -> ConsentSnapshot:
        _require_exact_fields(
            data,
            (*CANONICAL_FIELD_ORDER_SNAPSHOT, "canonical_hash"),
            "snapshot",
        )
        try:
            grants_data = _require_list(data["grants"], "grants")
            relationships_data = _require_list(data["relationships"], "relationships")
            return cls(
                snapshot_id=_as_str(data["snapshot_id"], "snapshot_id"),
                version=_as_int(data["version"], "version"),
                subject_id=_as_str(data["subject_id"], "subject_id"),
                binding_id=_as_str(data["binding_id"], "binding_id"),
                binding_version=_as_int(data["binding_version"], "binding_version"),
                policy_version=_as_str(data["policy_version"], "policy_version"),
                created_at=_datetime_from_iso(data["created_at"], "created_at"),
                grants=tuple(
                    ConsentEvidence.from_canonical_dict(
                        _canonical_projection_with_hash(
                            _require_dict(g, "grants[]"),
                            CANONICAL_FIELD_ORDER_CONSENT,
                            "snapshot grant",
                        )
                    )
                    for g in grants_data
                ),
                relationships=tuple(
                    RelationshipEvidence.from_canonical_dict(
                        _canonical_projection_with_hash(
                            _require_dict(r, "relationships[]"),
                            CANONICAL_FIELD_ORDER_RELATIONSHIP,
                            "snapshot relationship",
                        )
                    )
                    for r in relationships_data
                ),
                binding=BindingEvidence.from_canonical_dict(
                    _canonical_projection_with_hash(
                        _require_dict(data["binding"], "binding"),
                        CANONICAL_FIELD_ORDER_BINDING,
                        "snapshot binding",
                    )
                ),
                canonical_hash=_as_str(data["canonical_hash"], "canonical_hash"),
            )
        except KeyError as exc:
            raise ValueError(f"snapshot missing {exc.args[0]}") from exc

    @property
    def checksum(self) -> str:
        """Compatibility alias for the canonical hash."""
        return self.canonical_hash


@dataclass(frozen=True, slots=True)
class ConsentOffer:
    """Immutable, versioned offer loaded from a ConsentStore before grant."""

    offer_id: str
    capability: CapabilityValue
    subject_id: str
    actor_id: str
    resource_owner_id: str
    purpose: PurposeValue
    params: ConsentParams
    valid_from: datetime
    valid_until: datetime
    created_at: datetime
    policy_version: str
    version: int = 1
    status: ConsentOfferStatus = "active"
    issuer: str = "consent_authority"
    supersedes_offer_id: str | None = None
    canonical_hash: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "offer_id", _bounded(self.offer_id, "offer_id"))
        object.__setattr__(self, "subject_id", _bounded(self.subject_id, "subject_id"))
        object.__setattr__(self, "actor_id", _bounded(self.actor_id, "actor_id"))
        object.__setattr__(
            self, "resource_owner_id", _bounded(self.resource_owner_id, "resource_owner_id")
        )
        object.__setattr__(
            self,
            "purpose",
            _bounded(self.purpose, "purpose", maximum=256),
        )
        object.__setattr__(
            self,
            "policy_version",
            _bounded(self.policy_version, "policy_version"),
        )
        object.__setattr__(self, "issuer", _bounded(self.issuer, "issuer"))
        object.__setattr__(self, "version", _positive(self.version, "version"))
        if self.status not in ALL_CONSENT_OFFER_STATUS_VALUES:
            raise ValueError(f"unknown consent offer status {self.status!r}")
        if self.supersedes_offer_id is not None:
            object.__setattr__(
                self,
                "supersedes_offer_id",
                _bounded(self.supersedes_offer_id, "supersedes_offer_id"),
            )
        if self.version == 1 and self.supersedes_offer_id is not None:
            raise ValueError("version 1 offers cannot supersede another offer")
        if self.version > 1 and self.supersedes_offer_id is None:
            raise ValueError("versioned offers require supersedes_offer_id")
        if self.capability not in ALL_CAPABILITY_VALUES_SET:
            raise ValueError(f"unknown capability {self.capability!r}")
        _validate_capability_purpose(self.capability, self.purpose)
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        object.__setattr__(self, "valid_until", _utc(self.valid_until, "valid_until"))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be later than valid_from")
        computed = compute_canonical_hash(self._canonical_payload())
        if self.canonical_hash:
            object.__setattr__(
                self,
                "canonical_hash",
                _validate_hash(self.canonical_hash, computed),
            )
        else:
            object.__setattr__(self, "canonical_hash", computed)

    def _canonical_payload(self) -> dict[str, object]:
        return {
            "offer_id": self.offer_id,
            "version": self.version,
            "status": self.status,
            "capability": self.capability,
            "subject_id": self.subject_id,
            "actor_id": self.actor_id,
            "resource_owner_id": self.resource_owner_id,
            "purpose": self.purpose,
            "params": self.params.to_canonical_dict(),
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "created_at": self.created_at.isoformat(),
            "issuer": self.issuer,
            "policy_version": self.policy_version,
            "supersedes_offer_id": self.supersedes_offer_id,
        }

    def to_canonical_dict(self) -> dict[str, object]:
        payload = self._canonical_payload()
        payload["canonical_hash"] = self.canonical_hash
        return payload

    @classmethod
    def from_canonical_dict(cls, data: dict[str, object]) -> ConsentOffer:
        _require_exact_fields(
            data,
            (*CANONICAL_FIELD_ORDER_OFFER, "canonical_hash"),
            "offer",
        )
        try:
            return cls(
                offer_id=_as_str(data["offer_id"], "offer_id"),
                version=_as_int(data["version"], "version"),
                status=cast(
                    ConsentOfferStatus, _as_str(data["status"], "status")
                ),
                capability=cast(
                    CapabilityValue, _as_str(data["capability"], "capability")
                ),
                subject_id=_as_str(data["subject_id"], "subject_id"),
                actor_id=_as_str(data["actor_id"], "actor_id"),
                resource_owner_id=_as_str(
                    data["resource_owner_id"], "resource_owner_id"
                ),
                purpose=cast(PurposeValue, _as_str(data["purpose"], "purpose")),
                params=_params_from_dict(
                    _require_dict(data["params"], "params"), field_name="params"
                ),
                valid_from=_datetime_from_iso(data["valid_from"], "valid_from"),
                valid_until=_datetime_from_iso(data["valid_until"], "valid_until"),
                created_at=_datetime_from_iso(data["created_at"], "created_at"),
                issuer=_as_str(data["issuer"], "issuer"),
                policy_version=_as_str(data["policy_version"], "policy_version"),
                supersedes_offer_id=_as_optional_str(
                    data["supersedes_offer_id"], "supersedes_offer_id"
                ),
                canonical_hash=_as_str(data["canonical_hash"], "canonical_hash"),
            )
        except KeyError as exc:
            raise ValueError(f"offer missing {exc.args[0]}") from exc

    def canonical_content(self) -> str:
        """Deterministic content projection used for idempotency fingerprinting."""
        return canonical_json(self._canonical_payload())


def _require_dict(value: object, field_name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{field_name} must be an object")
    return value


def _require_list(value: object, field_name: str) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{field_name} must be a list")
    return value


def _datetime_from_iso(value: object, field_name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be an ISO datetime string")
    return _utc(datetime.fromisoformat(value), field_name)


def _as_int(value: object, field_name: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field_name} must be an integer")
    return value


def _as_str(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string")
    return value


def _as_optional_str(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise ValueError(f"{field_name} must be a string or null")
    return value


def _require_exact_fields(
    data: dict[str, object], expected: tuple[str, ...], label: str
) -> None:
    actual = set(data)
    required = set(expected)
    unknown = actual - required
    if unknown:
        raise ValueError(f"unknown {label} keys {sorted(unknown)}")
    missing = required - actual
    if missing:
        raise ValueError(f"{label} missing {sorted(missing)[0]}")


def _canonical_projection_with_hash(
    data: dict[str, object], expected: tuple[str, ...], label: str
) -> dict[str, object]:
    """Decode a nested hashless projection covered by its snapshot hash."""
    _require_exact_fields(data, expected, label)
    result = dict(data)
    result["canonical_hash"] = compute_canonical_hash(data)
    return result


__all__ = [
    "ActorKind",
    "ALLOWED_PURPOSES",
    "ALL_ACTOR_KIND_VALUES",
    "ALL_CAPABILITY_VALUES_SET",
    "ALL_CONSENT_STATUS_VALUES",
    "ALL_CONSENT_OFFER_STATUS_VALUES",
    "ALL_DECLARED_MODE_VALUES_SET",
    "ALL_RELATION_TYPE_VALUES",
    "ALL_RELATIONSHIP_STATUS_VALUES_SET",
    "BindingEvidence",
    "CANONICAL_FIELD_ORDER_BINDING",
    "CANONICAL_FIELD_ORDER_CONSENT",
    "CANONICAL_FIELD_ORDER_OFFER",
    "CANONICAL_FIELD_ORDER_PARAMS",
    "CANONICAL_FIELD_ORDER_RELATIONSHIP",
    "CANONICAL_FIELD_ORDER_SNAPSHOT",
    "ConsentEvidence",
    "ConsentOffer",
    "ConsentOfferStatus",
    "ConsentParams",
    "ConsentSnapshot",
    "ConsentStatus",
    "DEVICE_TRANSFER_PURPOSE",
    "RelationTypeValue",
    "RelationshipEvidence",
    "RelationshipStatusValue",
    "canonical_json",
    "compute_canonical_hash",
    "is_guardian_of",
    "is_parent_of",
]
