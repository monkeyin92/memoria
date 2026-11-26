"""Wire codecs and locked-evidence types for the Session Runtime boundary.

Canonical hashes, signatures and authority payloads are decoded here, where
untyped JSON from the PostgreSQL functions becomes typed evidence, and a
malformed payload fails closed as ``PersistentSessionUnavailable``.  The
orchestration service only sees the values these helpers return.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from packages.contracts.generated.python.multi_subject_contracts import (
    DeviceDeclaredModeValue,
    RuntimeProfileSignedV2,
)

from services.consent.evidence import (
    RelationshipEvidence,
    RelationshipStatusValue,
    RelationTypeValue,
)
from services.policy.context import SubjectPresence
from services.policy.evidence import BindingStatusValue, RelationshipEvidencePort
from services.session_runtime.errors import PersistentSessionUnavailable
from services.session_runtime.subject_resolver import BindingSnapshot

if TYPE_CHECKING:
    from services.session_runtime.commands import CommitToolEffectCommand

_NO_EXPIRY_SENTINEL = datetime(9999, 12, 31, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _BindingRole:
    person_id: str
    role: str
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _BindingSubject:
    person_id: str
    subject_category: str
    age_band: str
    revision: int


@dataclass(frozen=True, slots=True)
class _LockedBindingEvidence:
    binding_id: str
    version: int
    device_id: str
    status: BindingStatusValue
    declared_mode: DeviceDeclaredModeValue
    valid_from: datetime
    valid_until: datetime
    canonical_hash: str

    def is_active_at(self, now: datetime) -> bool:
        return self.status == "active" and self.valid_from <= now < self.valid_until


#: Bindings without subject overrides share one immutable empty mapping, so a
#: frozen ``_LockedBinding`` never needs a mutable default.
_NO_PERSONA_ASSIGNMENTS: Mapping[str, str] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class _LockedBinding:
    evidence: _LockedBindingEvidence
    roles: tuple[_BindingRole, ...]
    subjects: tuple[_BindingSubject, ...]
    relationships: tuple[RelationshipEvidencePort, ...]
    account_owner_person_id: str
    policy_bundle_version: str
    persona_assignment_id: str
    persona_assignments: Mapping[str, str] = _NO_PERSONA_ASSIGNMENTS

    def persona_for(self, subject_id: str | None) -> str:
        """Subject override first, binding default second (never fail-open).

        The order is fixed here and nowhere else: every reader of a locked
        binding resolves a persona through this one method.
        """
        if subject_id is not None:
            override = self.persona_assignments.get(subject_id)
            if override is not None:
                return override
        return self.persona_assignment_id

    @property
    def snapshot(self) -> BindingSnapshot:
        members = tuple(item.person_id for item in self.subjects)
        primary = tuple(
            sorted({item.person_id for item in self.roles if item.role == "primary_subject"})
        )
        return BindingSnapshot(
            binding_id=self.evidence.binding_id,
            device_id=self.evidence.device_id,
            binding_version=self.evidence.version,
            declared_mode=self.evidence.declared_mode,
            primary_subject_ids=primary,
            member_subject_ids=members,
        )


def _canonical_json(value: object, *, allow_nan: bool = True) -> str:
    return json.dumps(
        value,
        allow_nan=allow_nan,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _subject_presence_from_profile(
    profile: RuntimeProfileSignedV2,
    *,
    actor_id: str,
) -> SubjectPresence:
    """Recover device-bound presence from a signed profile at action time.

    Policy grants private recall and tutoring for another subject only to
    that subject's bound device. Either signed capability preserves this
    evidence without requiring retention consent for basic tutoring.
    """

    subject_id = profile.active_subject_id
    if (
        subject_id is not None
        and subject_id != actor_id
        and profile.speaker_state.value == "confirmed"
        and any(item.value in {"memory_recall_private", "tutor"} for item in profile.capabilities)
    ):
        return "device_bound"
    return "resolved"


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _tool_effect_fence_fingerprint(command: CommitToolEffectCommand) -> str:
    return _digest(
        {
            "session_id": command.session_id,
            "turn_id": command.turn_id,
            "generation_id": command.generation_id,
            "tool_epoch": command.tool_epoch,
            "session_epoch": command.session_epoch,
        }
    )


def _decode_json_object(value: object, *, name: str) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise PersistentSessionUnavailable(f"{name} is not a JSON object")
    return {str(key): item for key, item in decoded.items()}


def _required_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistentSessionUnavailable(f"{name} is unavailable")
    return value


def _required_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PersistentSessionUnavailable(f"{name} is unavailable")
    return value


def _aware_datetime(value: object, *, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PersistentSessionUnavailable(f"{name} is invalid") from exc
    else:
        raise PersistentSessionUnavailable(f"{name} is unavailable")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistentSessionUnavailable(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_permissions(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PersistentSessionUnavailable("binding role permissions are invalid")
    return tuple(value)


def _parse_locked_binding(value: object) -> _LockedBinding:
    raw = _decode_json_object(value, name="Identity binding authority")
    binding_id = _required_string(raw.get("binding_id"), name="binding_id")
    device_id = _required_string(raw.get("device_id"), name="device_id")
    version = _required_int(raw.get("binding_version"), name="binding_version")
    declared_mode_raw = _required_string(raw.get("declared_mode"), name="declared_mode")
    if declared_mode_raw not in {
        "parent_for_child",
        "self_use",
        "child_for_parent",
        "family_shared",
    }:
        raise PersistentSessionUnavailable("declared_mode is invalid")
    declared_mode = cast(DeviceDeclaredModeValue, declared_mode_raw)
    valid_from = _aware_datetime(raw.get("valid_from"), name="valid_from")
    valid_until_raw = raw.get("valid_until")
    valid_until = (
        _NO_EXPIRY_SENTINEL
        if valid_until_raw is None
        else _aware_datetime(valid_until_raw, name="valid_until")
    )
    evidence_payload = {
        "binding_id": binding_id,
        "version": version,
        "device_id": device_id,
        "status": "active",
        "declared_mode": declared_mode,
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat(),
    }
    roles_raw = raw.get("roles")
    if not isinstance(roles_raw, list):
        raise PersistentSessionUnavailable("binding roles are unavailable")
    roles: list[_BindingRole] = []
    for item in roles_raw:
        if not isinstance(item, dict):
            raise PersistentSessionUnavailable("binding role is invalid")
        roles.append(
            _BindingRole(
                person_id=_required_string(item.get("person_id"), name="role person"),
                role=_required_string(item.get("role"), name="binding role"),
                permissions=_parse_permissions(item.get("permissions")),
            )
        )
    subjects_raw = raw.get("subjects")
    if not isinstance(subjects_raw, list):
        raise PersistentSessionUnavailable("binding subjects are unavailable")
    subjects: list[_BindingSubject] = []
    for item in subjects_raw:
        if not isinstance(item, dict):
            raise PersistentSessionUnavailable("binding subject is invalid")
        category = _required_string(item.get("subject_category"), name="subject category")
        age_band = _required_string(item.get("age_band"), name="subject age band")
        if category not in {"unknown", "minor", "adult"} or age_band not in {
            "unknown",
            "under_14",
            "14_17",
            "adult",
        }:
            raise PersistentSessionUnavailable("subject classification is invalid")
        subjects.append(
            _BindingSubject(
                person_id=_required_string(item.get("person_id"), name="subject person"),
                subject_category=category,
                age_band=age_band,
                revision=_required_int(item.get("revision"), name="subject revision"),
            )
        )
    relationships_raw = raw.get("relationships")
    if not isinstance(relationships_raw, list):
        raise PersistentSessionUnavailable("binding relationships are unavailable")
    relationships: list[RelationshipEvidencePort] = []
    for item in relationships_raw:
        if not isinstance(item, dict):
            raise PersistentSessionUnavailable("binding relationship is invalid")
        updated_at = _aware_datetime(item.get("updated_at"), name="relationship update")
        valid_from = _aware_datetime(item.get("valid_from"), name="relationship start")
        valid_until_raw = item.get("valid_until")
        valid_until = (
            _NO_EXPIRY_SENTINEL
            if valid_until_raw is None
            else _aware_datetime(valid_until_raw, name="relationship end")
        )
        relationship_id = _required_string(item.get("relationship_id"), name="relationship id")
        revision = max(1, int(updated_at.timestamp() * 1_000_000))
        relationships.append(
            RelationshipEvidence(
                relationship_id=relationship_id,
                snapshot_id=f"{relationship_id}:v{revision}",
                revision=revision,
                relation_type=cast(
                    RelationTypeValue,
                    _required_string(item.get("relation_type"), name="relationship type"),
                ),
                status=cast(
                    RelationshipStatusValue,
                    _required_string(item.get("status"), name="relationship status"),
                ),
                source_person_id=_required_string(
                    item.get("source_person_id"), name="relationship source"
                ),
                target_person_id=_required_string(
                    item.get("target_person_id"), name="relationship target"
                ),
                binding_id=binding_id,
                valid_from=valid_from,
                valid_until=valid_until,
            )
        )
    return _LockedBinding(
        evidence=_LockedBindingEvidence(
            binding_id=binding_id,
            version=version,
            device_id=device_id,
            status="active",
            declared_mode=declared_mode,
            valid_from=valid_from,
            valid_until=valid_until,
            canonical_hash=_digest(evidence_payload),
        ),
        roles=tuple(roles),
        subjects=tuple(subjects),
        relationships=tuple(relationships),
        account_owner_person_id=_required_string(
            raw.get("account_owner_person_id"), name="account owner"
        ),
        policy_bundle_version=_required_string(
            raw.get("policy_bundle_version"), name="policy bundle version"
        ),
        persona_assignment_id=_required_string(
            raw.get("persona_assignment_id"), name="persona assignment"
        ),
        persona_assignments=_parse_persona_assignments(raw),
    )


def _parse_persona_assignments(raw: Mapping[str, object]) -> dict[str, str]:
    """Subject overrides from the lock port; absent/legacy payloads are empty.

    A binding built before the subject-level table existed has no
    ``persona_assignments`` key at all, which is exactly "no overrides": the
    binding default still applies, so rolling the schema forward cannot
    change an existing device's persona.
    """
    value = raw.get("persona_assignments")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PersistentSessionUnavailable("persona assignments are unavailable")
    parsed: dict[str, str] = {}
    for subject_id, assignment_id in value.items():
        parsed[str(subject_id)] = _required_string(
            assignment_id, name="persona assignment"
        )
    return parsed
