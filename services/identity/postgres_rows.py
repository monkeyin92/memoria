"""Decode PostgreSQL identity rows into identity-domain objects.

The store's SQL stays in ``postgres_store.py``; this module only maps asyncpg
records to the frozen domain types.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, cast

import asyncpg
from services.identity.domain import (
    AgeBand,
    AgeEvidenceStatus,
    BindingReason,
    BindingRole,
    BindingStatus,
    CustomPersonaRecord,
    DeviceBinding,
    DeviceBindingRole,
    DeviceDeclaredMode,
    Permission,
    PersonaAssignmentRecord,
    PersonStatus,
    PersonSubject,
    Relationship,
    RelationshipStatus,
    RelationType,
    SubjectCategory,
    TransferIntent,
    permission_set,
    transfer_from_dict,
)


def _from_db(value: object) -> datetime | None:
    if value is None:
        return None
    return cast(datetime, value)


def _jsonb_list(value: object, *, field: str) -> list[object]:
    """Strict asyncpg jsonb decoding: asyncpg may return ``str`` or ``list``.
    Verifies the top-level type and every element type (fail closed)."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSONB") from exc
    else:
        decoded = value
    if not isinstance(decoded, list):
        raise ValueError(f"{field} must be a JSONB array")
    if not all(isinstance(item, str) for item in decoded):
        raise ValueError(f"{field} array items must be strings")
    return decoded


def _jsonb_object(value: object, *, field: str) -> dict[str, object]:
    """Strict asyncpg jsonb object decoding (str or dict, fail closed)."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSONB") from exc
    else:
        decoded = value
    if not isinstance(decoded, dict):
        raise ValueError(f"{field} must be a JSONB object")
    return decoded


def _permissions(value: object, *, field: str) -> frozenset[Permission]:
    return permission_set(_jsonb_list(value, field=field))


def _person(row: asyncpg.Record) -> PersonSubject:
    return PersonSubject(
        person_id=str(row["person_id"]),
        display_name=str(row["display_name"]),
        subject_category=cast(SubjectCategory, str(row["subject_category"])),
        age_band=cast(AgeBand, str(row["age_band"])),
        age_evidence_status=cast(AgeEvidenceStatus, str(row["age_evidence_status"])),
        locale=str(row["locale"]),
        timezone=str(row["timezone"]),
        status=cast(PersonStatus, str(row["status"])),
        created_at=_from_db(row["created_at"]) or datetime.now(UTC),
        updated_at=_from_db(row["updated_at"]) or datetime.now(UTC),
    )


def _relationship(row: asyncpg.Record) -> Relationship:
    return Relationship(
        relationship_id=str(row["relationship_id"]),
        source_person_id=str(row["source_person_id"]),
        target_person_id=str(row["target_person_id"]),
        relation_type=cast(RelationType, str(row["relation_type"])),
        status=cast(RelationshipStatus, str(row["status"])),
        valid_from=_from_db(row["valid_from"]) or datetime.now(UTC),
        valid_until=_from_db(row["valid_until"]),
        established_evidence_id=str(row["established_evidence_id"]),
        confirmed_by_source_at=_from_db(row["confirmed_by_source_at"]),
        confirmed_by_target_at=_from_db(row["confirmed_by_target_at"]),
        dispute_resolution_acked_by_source_at=_from_db(
            row["dispute_resolution_acked_by_source_at"]
        ),
        dispute_resolution_acked_by_target_at=_from_db(
            row["dispute_resolution_acked_by_target_at"]
        ),
        requires_confirmation=bool(row["requires_confirmation"]),
        can_delegate=bool(row["can_delegate"]),
        delegated_from_relationship_id=(
            str(row["delegated_from_relationship_id"])
            if row["delegated_from_relationship_id"] is not None
            else None
        ),
        delegation_depth=int(row["delegation_depth"]),
        permissions=_permissions(
            row["permissions_json"], field="permissions_json"
        ),
        dispute_reason=(
            str(row["dispute_reason"]) if row["dispute_reason"] is not None else None
        ),
        auto_suspended=bool(row["auto_suspended"]),
        revoked_at=_from_db(row["revoked_at"]),
        revocation_evidence_id=(
            str(row["revocation_evidence_id"])
            if row["revocation_evidence_id"] is not None
            else None
        ),
        created_at=_from_db(row["created_at"]) or datetime.now(UTC),
        updated_at=_from_db(row["updated_at"]) or datetime.now(UTC),
    )


def _persona_assignment(row: asyncpg.Record) -> PersonaAssignmentRecord:
    return PersonaAssignmentRecord(
        binding_id=str(row["binding_id"]),
        subject_id=str(row["subject_id"]),
        assignment_id=str(row["assignment_id"]),
        persona_id=str(row["persona_id"]),
        persona_version=int(row["persona_version"]),
        created_at=cast(datetime, _from_db(row["created_at"])),
        updated_at=cast(datetime, _from_db(row["updated_at"])),
    )


def _custom_persona(row: asyncpg.Record) -> CustomPersonaRecord:
    return CustomPersonaRecord(
        persona_id=str(row["persona_id"]),
        owner_person_id=str(row["owner_person_id"]),
        display_name=str(row["display_name"]),
        style_description=str(row["style_description"]),
        warmth=str(row["warmth"]),
        directness=str(row["directness"]),
        response_length=str(row["response_length"]),
        question_frequency=str(row["question_frequency"]),
        interview_depth=str(row["interview_depth"]),
        welcome_text=str(row["welcome_text"]),
        conversation_instruction=str(row["conversation_instruction"]),
        voice_instruction=str(row["voice_instruction"]),
        default_voice_emotion=str(row["default_voice_emotion"]),
        default_voice_rate=float(row["default_voice_rate"]),
        fallback_designed_voice=str(row["fallback_designed_voice"]),
        persona_version=int(row["persona_version"]),
        source=str(row["source"]),
        created_at=cast(datetime, _from_db(row["created_at"])),
    )


def _transfer_intent(row: asyncpg.Record) -> TransferIntent:
    return transfer_from_dict(
        {
            "transfer_id": str(row["transfer_id"]),
            "device_id": str(row["device_id"]),
            "from_account_owner_person_id": str(row["from_account_owner_person_id"]),
            "to_account_owner_person_id": str(row["to_account_owner_person_id"]),
            "status": str(row["status"]),
            "step_up_evidence_id": str(row["step_up_evidence_id"]),
            "policy_receipt_id": str(row["policy_receipt_id"]),
            "idempotency_key": str(row["idempotency_key"] or ""),
            "evidence_hash": str(row["evidence_hash"] or ""),
            "supersedes_binding_id": row["supersedes_binding_id"],
            "resulting_binding_id": row["resulting_binding_id"],
            "created_by_person_id": row["created_by_person_id"],
            "created_at": (
                row["created_at"].isoformat() if row["created_at"] is not None else None
            ),
            "updated_at": (
                row["updated_at"].isoformat() if row["updated_at"] is not None else None
            ),
            "valid_until": row["valid_until"],
            "accepted_at": row["accepted_at"],
            "cancelled_at": row["cancelled_at"],
            "cancelled_by_person_id": row["cancelled_by_person_id"],
            "cancel_reason": row["cancel_reason"],
        }
    )


def _binding(binding_row: asyncpg.Record, role_rows: list[asyncpg.Record]) -> DeviceBinding:
    primary_ids = tuple(
        sorted(
            str(row["person_id"])
            for row in role_rows
            if str(row["role"]) == "primary_subject"
        )
    )
    return DeviceBinding(
        binding_id=str(binding_row["binding_id"]),
        device_id=str(binding_row["device_id"]),
        declared_mode=cast(DeviceDeclaredMode, str(binding_row["declared_mode"])),
        account_owner_person_id=str(binding_row["account_owner_person_id"]),
        primary_subject_ids=primary_ids,
        binding_version=int(binding_row["binding_version"]),
        status=cast(BindingStatus, str(binding_row["status"])),
        reason=cast(BindingReason, str(binding_row["reason"])),
        family_space_id=(
            str(binding_row["family_space_id"])
            if binding_row["family_space_id"] is not None
            else None
        ),
        supersedes_binding_id=(
            str(binding_row["supersedes_binding_id"])
            if binding_row["supersedes_binding_id"] is not None
            else None
        ),
        valid_from=_from_db(binding_row["valid_from"]) or datetime.now(UTC),
        valid_until=_from_db(binding_row["valid_until"]),
        roles=tuple(_role(row) for row in role_rows),
        service_profile_version=str(binding_row["service_profile_version"]),
        policy_bundle_version=str(binding_row["policy_bundle_version"]),
        consent_snapshot_id=(
            str(binding_row["consent_snapshot_id"])
            if binding_row["consent_snapshot_id"] is not None
            else None
        ),
        persona_assignment_id=(
            str(binding_row["persona_assignment_id"])
            if binding_row["persona_assignment_id"] is not None
            else None
        ),
        created_at=_from_db(binding_row["created_at"]) or datetime.now(UTC),
    )


def _role(row: asyncpg.Record) -> DeviceBindingRole:
    return DeviceBindingRole(
        binding_id=str(row["binding_id"]),
        person_id=str(row["person_id"]),
        role=cast(BindingRole, str(row["role"])),
        status=cast(Literal["active", "superseded", "revoked", "expired"], str(row["status"])),
        permissions=_permissions(
            row["permissions_json"], field="permissions_json"
        ),
        granted_at=_from_db(row["granted_at"]) or datetime.now(UTC),
        ended_at=_from_db(row["ended_at"]),
    )
