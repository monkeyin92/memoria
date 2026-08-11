"""Authoritative acceptance snapshots for initial device binding.

This module deliberately does not issue standing capability grants.  It
records which server-owned offers and service preferences were accepted when
Identity created an immutable binding version.  Sensitive capabilities still
flow through :mod:`services.consent.authority` and Policy receipts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import asyncpg
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRoleValue,
    DeviceDeclaredModeValue,
)

BINDING_CONSENT_SCHEMA_VERSION = "binding-consent-snapshot-v1"
BINDING_OFFER_CATALOG_VERSION = "binding-offers-v1"


class BindingConsentValidationError(ValueError):
    """A binding-acceptance command does not satisfy the server catalog."""


class BindingConsentConflictError(RuntimeError):
    """An immutable binding/version already has different acceptance facts."""


class BindingConsentNotFoundError(LookupError):
    """The requested binding acceptance snapshot does not exist."""


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BindingConsentValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded(value: str, field: str, maximum: int = 128) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise BindingConsentValidationError(
            f"{field} must be a bounded non-empty string"
        )
    return normalized


def _json_value(value: object, *, field: str) -> object:
    """Normalize caller data to strict, finite JSON values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise BindingConsentValidationError(f"{field} must contain finite JSON")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise BindingConsentValidationError(
                    f"{field} keys must be strings"
                )
            normalized[key] = _json_value(item, field=f"{field}.{key}")
        return dict(sorted(normalized.items()))
    if isinstance(value, (list, tuple)):
        return [_json_value(item, field=field) for item in value]
    raise BindingConsentValidationError(f"{field} must contain JSON values")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _snapshot_json(value: object) -> Mapping[str, object]:
    """Normalize SQLite text and asyncpg JSONB values through one strict seam."""
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise BindingConsentValidationError(
            "binding consent snapshot must be a JSON object"
        )
    if any(not isinstance(key, str) for key in decoded):
        raise BindingConsentValidationError(
            "binding consent snapshot keys must be strings"
        )
    return cast(Mapping[str, object], decoded)


@dataclass(frozen=True, slots=True)
class BindingConsentRole:
    person_id: str
    role: BindingRoleValue
    permissions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "person_id", _bounded(self.person_id, "person_id"))
        if not self.role:
            raise BindingConsentValidationError("role must be non-empty")
        permissions = tuple(sorted(dict.fromkeys(self.permissions)))
        if any(not item.strip() or len(item) > 128 for item in permissions):
            raise BindingConsentValidationError(
                "permissions must contain bounded non-empty strings"
            )
        object.__setattr__(self, "permissions", permissions)

    def to_dict(self) -> dict[str, object]:
        return {
            "person_id": self.person_id,
            "role": self.role,
            "permissions": list(self.permissions),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> BindingConsentRole:
        if set(value) != {"person_id", "role", "permissions"}:
            raise BindingConsentValidationError("binding role has unknown fields")
        permissions = value["permissions"]
        if not isinstance(permissions, list) or not all(
            isinstance(item, str) for item in permissions
        ):
            raise BindingConsentValidationError("role permissions must be strings")
        return cls(
            person_id=str(value["person_id"]),
            role=cast(BindingRoleValue, str(value["role"])),
            permissions=tuple(permissions),
        )


@dataclass(frozen=True, slots=True)
class BindingRelationshipEvidence:
    relationship_id: str
    source_person_id: str
    target_person_id: str
    relation_type: str
    status: str
    established_evidence_id: str
    revision_at: datetime
    valid_from: datetime
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        for field in (
            "relationship_id",
            "source_person_id",
            "target_person_id",
            "relation_type",
            "status",
            "established_evidence_id",
        ):
            object.__setattr__(self, field, _bounded(str(getattr(self, field)), field))
        object.__setattr__(self, "revision_at", _utc(self.revision_at, "revision_at"))
        object.__setattr__(self, "valid_from", _utc(self.valid_from, "valid_from"))
        if self.valid_until is not None:
            object.__setattr__(
                self,
                "valid_until",
                _utc(self.valid_until, "valid_until"),
            )
            if self.valid_until <= self.valid_from:
                raise BindingConsentValidationError(
                    "relationship valid_until must be later than valid_from"
                )

    def is_active_at(self, at: datetime) -> bool:
        timestamp = _utc(at, "at")
        return (
            self.status == "active"
            and self.valid_from <= timestamp
            and (self.valid_until is None or timestamp < self.valid_until)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "relationship_id": self.relationship_id,
            "source_person_id": self.source_person_id,
            "target_person_id": self.target_person_id,
            "relation_type": self.relation_type,
            "status": self.status,
            "established_evidence_id": self.established_evidence_id,
            "revision_at": self.revision_at.isoformat(),
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object]
    ) -> BindingRelationshipEvidence:
        expected = {
            "relationship_id",
            "source_person_id",
            "target_person_id",
            "relation_type",
            "status",
            "established_evidence_id",
            "revision_at",
            "valid_from",
            "valid_until",
        }
        if set(value) != expected:
            raise BindingConsentValidationError(
                "relationship evidence has unknown fields"
            )
        valid_until = value["valid_until"]
        return cls(
            relationship_id=str(value["relationship_id"]),
            source_person_id=str(value["source_person_id"]),
            target_person_id=str(value["target_person_id"]),
            relation_type=str(value["relation_type"]),
            status=str(value["status"]),
            established_evidence_id=str(value["established_evidence_id"]),
            revision_at=datetime.fromisoformat(str(value["revision_at"])),
            valid_from=datetime.fromisoformat(str(value["valid_from"])),
            valid_until=(
                datetime.fromisoformat(str(valid_until))
                if valid_until is not None
                else None
            ),
        )


class BindingOfferCatalog:
    """Single server-owned offer and preference catalog for initial binding."""

    def __init__(self) -> None:
        self.version = BINDING_OFFER_CATALOG_VERSION
        self.offers: dict[DeviceDeclaredModeValue, frozenset[str]] = {
            "parent_for_child": frozenset(
                {
                    "offer_minor_voice_session_v1",
                    "offer_minor_memory_retention_v1",
                    "offer_guardian_weekly_summary_v1",
                    "offer_emergency_contact_v1",
                }
            ),
            "self_use": frozenset(
                {
                    "offer_self_memory_retention_v1",
                    "offer_self_voice_profile_v1",
                    "offer_self_raw_audio_v1",
                    "offer_self_voice_clone_v1",
                    "offer_self_digital_self_v1",
                    "offer_self_legacy_v1",
                }
            ),
            "child_for_parent": frozenset(
                {
                    "offer_admin_device_management_v1",
                    "offer_senior_health_reminder_v1",
                    "offer_senior_anti_fraud_v1",
                    "offer_senior_emergency_contact_v1",
                }
            ),
            "family_shared": frozenset(
                {
                    "offer_family_space_v1",
                    "offer_family_shared_memory_v1",
                    "offer_family_admin_v1",
                }
            ),
        }
        self.preference_keys: dict[DeviceDeclaredModeValue, frozenset[str]] = {
            "parent_for_child": frozenset(
                {
                    "tutor_enabled",
                    "english_practice_enabled",
                    "memory_level",
                    "max_session_minutes",
                    "quiet_hours",
                }
            ),
            "self_use": frozenset({"memory_level", "interview_frequency"}),
            "child_for_parent": frozenset(
                {"speech_speed", "memory_level", "admin_visibility"}
            ),
            "family_shared": frozenset(
                {"memory_level", "shared_persona_enabled"}
            ),
        }

    @staticmethod
    def _memory_enabled(preferences: Mapping[str, object]) -> bool:
        value = preferences.get("memory_level")
        return value not in {None, "", "none", "off", "ephemeral", "disabled"}

    def required_offers(
        self,
        *,
        declared_mode: DeviceDeclaredModeValue,
        service_preferences: Mapping[str, object],
    ) -> frozenset[str]:
        required: set[str] = set()
        if declared_mode == "parent_for_child":
            required.add("offer_minor_voice_session_v1")
            if self._memory_enabled(service_preferences):
                required.add("offer_minor_memory_retention_v1")
        elif declared_mode == "self_use":
            if self._memory_enabled(service_preferences):
                required.add("offer_self_memory_retention_v1")
        elif declared_mode == "child_for_parent":
            required.add("offer_admin_device_management_v1")
        elif declared_mode == "family_shared":
            required.add("offer_family_space_v1")
        else:
            raise BindingConsentValidationError(
                f"unknown declared mode {declared_mode!r}"
            )
        return frozenset(required)

    def validate(
        self,
        *,
        declared_mode: DeviceDeclaredModeValue,
        consent_offer_ids: tuple[str, ...],
        service_preferences: Mapping[str, object],
    ) -> None:
        if declared_mode not in self.offers:
            raise BindingConsentValidationError(
                f"unknown declared mode {declared_mode!r}"
            )
        normalized_offers = tuple(
            _bounded(item, "consent_offer_id") for item in consent_offer_ids
        )
        selected = set(normalized_offers)
        if len(selected) != len(normalized_offers):
            raise BindingConsentValidationError(
                "consent_offer_ids must be unique"
            )
        unknown_offers = selected - self.offers[declared_mode]
        if unknown_offers:
            raise BindingConsentValidationError(
                f"consent offers are not valid: {sorted(unknown_offers)}"
            )
        unknown_preferences = (
            set(service_preferences) - self.preference_keys[declared_mode]
        )
        if unknown_preferences:
            raise BindingConsentValidationError(
                "service preferences are not allowed: "
                f"{sorted(unknown_preferences)}"
            )
        required = self.required_offers(
            declared_mode=declared_mode,
            service_preferences=service_preferences,
        )
        missing = required - selected
        if missing:
            raise BindingConsentValidationError(
                f"required consent offers are missing: {sorted(missing)}"
            )
        _json_value(service_preferences, field="service_preferences")


BINDING_OFFER_CATALOG = BindingOfferCatalog()


@dataclass(frozen=True, slots=True)
class BindingConsentCommand:
    actor_person_id: str
    account_owner_person_id: str
    primary_subject_ids: tuple[str, ...]
    device_id: str
    binding_id: str
    binding_version: int
    declared_mode: DeviceDeclaredModeValue
    roles: tuple[BindingConsentRole, ...]
    consent_offer_ids: tuple[str, ...]
    service_preferences: dict[str, object]
    relationship_evidence: tuple[BindingRelationshipEvidence, ...]
    service_profile_version: str
    policy_bundle_version: str
    issued_at: datetime

    def __post_init__(self) -> None:
        for field in (
            "actor_person_id",
            "account_owner_person_id",
            "device_id",
            "binding_id",
            "service_profile_version",
            "policy_bundle_version",
        ):
            object.__setattr__(self, field, _bounded(str(getattr(self, field)), field))
        if self.binding_version < 1:
            raise BindingConsentValidationError("binding_version must be >= 1")
        primary_subject_ids = tuple(
            dict.fromkeys(_bounded(item, "primary_subject_id") for item in self.primary_subject_ids)
        )
        if not primary_subject_ids:
            raise BindingConsentValidationError(
                "primary_subject_ids must not be empty"
            )
        object.__setattr__(self, "primary_subject_ids", primary_subject_ids)
        object.__setattr__(
            self,
            "consent_offer_ids",
            tuple(_bounded(item, "consent_offer_id") for item in self.consent_offer_ids),
        )
        normalized_preferences = cast(
            dict[str, object],
            _json_value(self.service_preferences, field="service_preferences"),
        )
        object.__setattr__(self, "service_preferences", normalized_preferences)
        object.__setattr__(self, "issued_at", _utc(self.issued_at, "issued_at"))
        if not any(
            role.person_id == self.account_owner_person_id
            and role.role == "account_owner"
            for role in self.roles
        ):
            raise BindingConsentValidationError(
                "roles must contain the authoritative account_owner"
            )
        for subject_id in self.primary_subject_ids:
            if not any(
                role.person_id == subject_id and role.role == "primary_subject"
                for role in self.roles
            ):
                raise BindingConsentValidationError(
                    f"roles must contain primary_subject {subject_id}"
                )


@dataclass(frozen=True, slots=True)
class BindingConsentSnapshot:
    snapshot_id: str
    actor_person_id: str
    account_owner_person_id: str
    primary_subject_ids: tuple[str, ...]
    device_id: str
    binding_id: str
    binding_version: int
    declared_mode: DeviceDeclaredModeValue
    roles: tuple[BindingConsentRole, ...]
    consent_offer_ids: tuple[str, ...]
    service_preferences: dict[str, object]
    relationship_evidence: tuple[BindingRelationshipEvidence, ...]
    service_profile_version: str
    policy_bundle_version: str
    catalog_version: str
    issued_at: datetime
    canonical_hash: str
    schema_version: str = BINDING_CONSENT_SCHEMA_VERSION

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "actor_person_id": self.actor_person_id,
            "account_owner_person_id": self.account_owner_person_id,
            "primary_subject_ids": list(self.primary_subject_ids),
            "device_id": self.device_id,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "declared_mode": self.declared_mode,
            "roles": [role.to_dict() for role in self.roles],
            "consent_offer_ids": list(self.consent_offer_ids),
            "service_preferences": self.service_preferences,
            "relationship_evidence": [
                item.to_dict() for item in self.relationship_evidence
            ],
            "service_profile_version": self.service_profile_version,
            "policy_bundle_version": self.policy_bundle_version,
            "catalog_version": self.catalog_version,
            "issued_at": self.issued_at.isoformat(),
            "canonical_hash": self.canonical_hash,
        }

    @classmethod
    def from_canonical_dict(
        cls, value: Mapping[str, object]
    ) -> BindingConsentSnapshot:
        expected = {
            "schema_version",
            "snapshot_id",
            "actor_person_id",
            "account_owner_person_id",
            "primary_subject_ids",
            "device_id",
            "binding_id",
            "binding_version",
            "declared_mode",
            "roles",
            "consent_offer_ids",
            "service_preferences",
            "relationship_evidence",
            "service_profile_version",
            "policy_bundle_version",
            "catalog_version",
            "issued_at",
            "canonical_hash",
        }
        if set(value) != expected:
            raise BindingConsentValidationError(
                "binding consent snapshot has unknown fields"
            )
        if value["schema_version"] != BINDING_CONSENT_SCHEMA_VERSION:
            raise BindingConsentValidationError(
                "unsupported binding consent schema_version"
            )
        roles = value["roles"]
        relationships = value["relationship_evidence"]
        subjects = value["primary_subject_ids"]
        offers = value["consent_offer_ids"]
        preferences = value["service_preferences"]
        if not isinstance(roles, list) or not all(
            isinstance(item, dict) for item in roles
        ):
            raise BindingConsentValidationError("snapshot roles must be objects")
        if not isinstance(relationships, list) or not all(
            isinstance(item, dict) for item in relationships
        ):
            raise BindingConsentValidationError(
                "snapshot relationship evidence must be objects"
            )
        if not isinstance(subjects, list) or not all(
            isinstance(item, str) for item in subjects
        ):
            raise BindingConsentValidationError(
                "snapshot primary_subject_ids must be strings"
            )
        if not isinstance(offers, list) or not all(
            isinstance(item, str) for item in offers
        ):
            raise BindingConsentValidationError(
                "snapshot consent_offer_ids must be strings"
            )
        if not isinstance(preferences, dict):
            raise BindingConsentValidationError(
                "snapshot service_preferences must be an object"
            )
        snapshot = cls(
            snapshot_id=str(value["snapshot_id"]),
            actor_person_id=str(value["actor_person_id"]),
            account_owner_person_id=str(value["account_owner_person_id"]),
            primary_subject_ids=tuple(subjects),
            device_id=str(value["device_id"]),
            binding_id=str(value["binding_id"]),
            binding_version=int(cast(int, value["binding_version"])),
            declared_mode=cast(DeviceDeclaredModeValue, str(value["declared_mode"])),
            roles=tuple(BindingConsentRole.from_dict(item) for item in roles),
            consent_offer_ids=tuple(offers),
            service_preferences=cast(
                dict[str, object],
                _json_value(preferences, field="service_preferences"),
            ),
            relationship_evidence=tuple(
                BindingRelationshipEvidence.from_dict(item) for item in relationships
            ),
            service_profile_version=str(value["service_profile_version"]),
            policy_bundle_version=str(value["policy_bundle_version"]),
            catalog_version=str(value["catalog_version"]),
            issued_at=datetime.fromisoformat(str(value["issued_at"])),
            canonical_hash=str(value["canonical_hash"]),
            schema_version=str(value["schema_version"]),
        )
        if snapshot.canonical_hash != _snapshot_hash(snapshot):
            raise BindingConsentValidationError(
                "binding consent snapshot canonical hash mismatch"
            )
        return snapshot


def _snapshot_payload(
    command: BindingConsentCommand,
    *,
    snapshot_id: str,
) -> dict[str, object]:
    return {
        "schema_version": BINDING_CONSENT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "actor_person_id": command.actor_person_id,
        "account_owner_person_id": command.account_owner_person_id,
        "primary_subject_ids": list(command.primary_subject_ids),
        "device_id": command.device_id,
        "binding_id": command.binding_id,
        "binding_version": command.binding_version,
        "declared_mode": command.declared_mode,
        "roles": [role.to_dict() for role in command.roles],
        "consent_offer_ids": list(command.consent_offer_ids),
        "service_preferences": command.service_preferences,
        "relationship_evidence": [
            item.to_dict() for item in command.relationship_evidence
        ],
        "service_profile_version": command.service_profile_version,
        "policy_bundle_version": command.policy_bundle_version,
        "catalog_version": BINDING_OFFER_CATALOG_VERSION,
        "issued_at": command.issued_at.isoformat(),
    }


def _snapshot_hash(snapshot: BindingConsentSnapshot) -> str:
    payload = snapshot.to_canonical_dict()
    payload.pop("canonical_hash")
    return _canonical_hash(payload)


class BindingConsentStorePort(Protocol):
    async def initialize(self) -> None: ...

    async def save(
        self, snapshot: BindingConsentSnapshot
    ) -> BindingConsentSnapshot: ...

    async def get(self, snapshot_id: str) -> BindingConsentSnapshot | None: ...

    async def close(self) -> None: ...


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS binding_consent_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL,
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    actor_person_id TEXT NOT NULL,
    account_owner_person_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    declared_mode TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    UNIQUE (binding_id, binding_version)
);
"""


class SqliteBindingConsentStore:
    """Local-development acceptance store; never a production authority."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def initialize_sync(self) -> None:
        if self._conn is not None:
            return
        if self._path != ":memory:":
            Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(_SQLITE_SCHEMA)
        self._conn = connection

    async def initialize(self) -> None:
        self.initialize_sync()

    def _ready(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteBindingConsentStore not initialized")
        return self._conn

    async def save(
        self, snapshot: BindingConsentSnapshot
    ) -> BindingConsentSnapshot:
        connection = self._ready()
        payload = _canonical_json(snapshot.to_canonical_dict())
        with self._lock:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT snapshot_json FROM binding_consent_snapshot
                    WHERE binding_id = ? AND binding_version = ?
                    """,
                    (snapshot.binding_id, snapshot.binding_version),
                ).fetchone()
                if existing is not None:
                    replay = BindingConsentSnapshot.from_canonical_dict(
                        _snapshot_json(existing["snapshot_json"])
                    )
                    if replay.canonical_hash != snapshot.canonical_hash:
                        raise BindingConsentConflictError(
                            "binding consent snapshot content conflict"
                        )
                    connection.commit()
                    return replay
                connection.execute(
                    """
                    INSERT INTO binding_consent_snapshot (
                        snapshot_id, binding_id, binding_version,
                        actor_person_id, account_owner_person_id, device_id,
                        declared_mode, catalog_version, canonical_hash,
                        snapshot_json, issued_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.binding_id,
                        snapshot.binding_version,
                        snapshot.actor_person_id,
                        snapshot.account_owner_person_id,
                        snapshot.device_id,
                        snapshot.declared_mode,
                        snapshot.catalog_version,
                        snapshot.canonical_hash,
                        payload,
                        snapshot.issued_at.isoformat(),
                    ),
                )
                connection.commit()
                return snapshot
            except BaseException:
                connection.rollback()
                raise

    async def get(self, snapshot_id: str) -> BindingConsentSnapshot | None:
        connection = self._ready()
        with self._lock:
            row = connection.execute(
                """
                SELECT snapshot_json FROM binding_consent_snapshot
                WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return BindingConsentSnapshot.from_canonical_dict(
            _snapshot_json(row["snapshot_json"])
        )

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class PostgresBindingConsentStore:
    """Production append-only acceptance store using the consent service role."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
        )

    def _ready(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresBindingConsentStore not initialized")
        return self._pool

    async def save(
        self, snapshot: BindingConsentSnapshot
    ) -> BindingConsentSnapshot:
        pool = self._ready()
        payload = snapshot.to_canonical_dict()
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"{snapshot.binding_id}:{snapshot.binding_version}",
                )
                row = await connection.fetchrow(
                    """
                    SELECT snapshot_json FROM binding_consent_snapshot
                    WHERE binding_id = $1 AND binding_version = $2
                    """,
                    snapshot.binding_id,
                    snapshot.binding_version,
                )
                if row is not None:
                    replay = BindingConsentSnapshot.from_canonical_dict(
                        _snapshot_json(row["snapshot_json"])
                    )
                    if replay.canonical_hash != snapshot.canonical_hash:
                        raise BindingConsentConflictError(
                            "binding consent snapshot content conflict"
                        )
                    return replay
                await connection.execute(
                    """
                    INSERT INTO binding_consent_snapshot (
                        snapshot_id, binding_id, binding_version,
                        actor_person_id, account_owner_person_id, device_id,
                        declared_mode, catalog_version, canonical_hash,
                        snapshot_json, issued_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11
                    )
                    """,
                    snapshot.snapshot_id,
                    snapshot.binding_id,
                    snapshot.binding_version,
                    snapshot.actor_person_id,
                    snapshot.account_owner_person_id,
                    snapshot.device_id,
                    snapshot.declared_mode,
                    snapshot.catalog_version,
                    snapshot.canonical_hash,
                    json.dumps(payload, ensure_ascii=False),
                    snapshot.issued_at,
                )
                return snapshot

    async def get(self, snapshot_id: str) -> BindingConsentSnapshot | None:
        pool = self._ready()
        async with pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT snapshot_json FROM binding_consent_snapshot
                WHERE snapshot_id = $1
                """,
                snapshot_id,
            )
        if row is None:
            return None
        return BindingConsentSnapshot.from_canonical_dict(
            _snapshot_json(row["snapshot_json"])
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class BindingConsentAuthority:
    """Validate the catalog again, freeze evidence, persist, and return its id."""

    def __init__(
        self,
        store: BindingConsentStorePort,
        *,
        catalog: BindingOfferCatalog = BINDING_OFFER_CATALOG,
    ) -> None:
        self._store = store
        self._catalog = catalog

    async def resolve(self, *, command: BindingConsentCommand) -> str:
        self._catalog.validate(
            declared_mode=command.declared_mode,
            consent_offer_ids=command.consent_offer_ids,
            service_preferences=command.service_preferences,
        )
        participants = {
            command.actor_person_id,
            command.account_owner_person_id,
            *command.primary_subject_ids,
            *(role.person_id for role in command.roles),
        }
        for relationship in command.relationship_evidence:
            if not relationship.is_active_at(command.issued_at):
                raise BindingConsentValidationError(
                    f"relationship {relationship.relationship_id} is not active"
                )
            if (
                relationship.source_person_id not in participants
                or relationship.target_person_id not in participants
            ):
                raise BindingConsentValidationError(
                    "relationship evidence must be bound to binding participants"
                )
        provisional_payload = _snapshot_payload(command, snapshot_id="")
        identity_hash = _canonical_hash(provisional_payload)
        snapshot_id = (
            f"binding-consent:{command.binding_id}:v{command.binding_version}:"
            f"{identity_hash[:24]}"
        )
        payload = _snapshot_payload(command, snapshot_id=snapshot_id)
        snapshot = BindingConsentSnapshot(
            snapshot_id=snapshot_id,
            actor_person_id=command.actor_person_id,
            account_owner_person_id=command.account_owner_person_id,
            primary_subject_ids=command.primary_subject_ids,
            device_id=command.device_id,
            binding_id=command.binding_id,
            binding_version=command.binding_version,
            declared_mode=command.declared_mode,
            roles=command.roles,
            consent_offer_ids=command.consent_offer_ids,
            service_preferences=command.service_preferences,
            relationship_evidence=command.relationship_evidence,
            service_profile_version=command.service_profile_version,
            policy_bundle_version=command.policy_bundle_version,
            catalog_version=self._catalog.version,
            issued_at=command.issued_at,
            canonical_hash=_canonical_hash(payload),
        )
        persisted = await self._store.save(snapshot)
        return persisted.snapshot_id

    async def get(self, snapshot_id: str) -> BindingConsentSnapshot:
        snapshot = await self._store.get(snapshot_id)
        if snapshot is None:
            raise BindingConsentNotFoundError(snapshot_id)
        return snapshot


class RejectingBindingConsentAuthority:
    """Fail-closed resolver used until the real store is initialized."""

    async def resolve(self, *, command: BindingConsentCommand) -> None:
        del command
        return None


class DeterministicBindingConsentAuthority:
    """TEST-ONLY resolver that binds ids to the full server command."""

    async def resolve(self, *, command: BindingConsentCommand) -> str:
        payload = _snapshot_payload(command, snapshot_id="")
        digest = _canonical_hash(payload)[:32]
        return (
            f"consent:test:v1:{command.declared_mode}:"
            f"{command.binding_id}:{digest}"
        )


async def initialize_binding_consent_store(
    store: BindingConsentStorePort,
) -> BindingConsentStorePort:
    """Small helper for wiring sites and tests."""
    await store.initialize()
    return store


__all__ = [
    "BINDING_CONSENT_SCHEMA_VERSION",
    "BINDING_OFFER_CATALOG",
    "BINDING_OFFER_CATALOG_VERSION",
    "BindingConsentAuthority",
    "BindingConsentCommand",
    "BindingConsentConflictError",
    "BindingConsentNotFoundError",
    "BindingConsentRole",
    "BindingConsentSnapshot",
    "BindingConsentStorePort",
    "BindingConsentValidationError",
    "BindingOfferCatalog",
    "BindingRelationshipEvidence",
    "DeterministicBindingConsentAuthority",
    "PostgresBindingConsentStore",
    "RejectingBindingConsentAuthority",
    "SqliteBindingConsentStore",
    "initialize_binding_consent_store",
]
