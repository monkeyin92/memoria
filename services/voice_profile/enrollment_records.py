"""Map voice enrollment rows onto the records clients poll.

``PostgresVoiceProfileManager`` keeps the SQL. These functions only copy a
row into ``VoiceProfile`` / ``VoiceEnrollmentOperation``, including the
operation state (``reconciliation_required`` included) that enrollment
progress reads.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Literal, cast

import asyncpg

from services.voice_profile.domain import (
    VoiceDeletionStatus,
    VoiceEnrollmentOperation,
    VoiceEnrollmentOperationState,
    VoiceProfile,
    VoiceProfileStatus,
)


def profile_for_operation(
    row: asyncpg.Record | None,
    operation: asyncpg.Record,
) -> VoiceProfile:
    if row is None:
        return synthetic_profile(operation)
    return replace(
        profile_from_row(row),
        enrollment_state=cast(VoiceEnrollmentOperationState, operation["state"]),
    )


def synthetic_profile(operation: asyncpg.Record) -> VoiceProfile:
    state = str(operation["state"])
    return VoiceProfile(
        profile_id=str(operation["profile_id"]),
        sample_id=str(operation["sample_id"]),
        version_number=int(operation["version_number"]),
        provider=str(operation["provider"]),
        provider_region=str(operation["provider_region"]),
        target_model=str(operation["target_model"]),
        provider_voice_id=(
            str(operation["provider_voice_id"])
            if operation["provider_voice_id"] is not None
            else None
        ),
        status="revoked" if state == "revoked" else "enrolling",
        evaluation_status="pending",
        quality_status="pending",
        deletion_status="completed" if state == "revoked" else "failed",
        provider_expires_at=cast(datetime | None, operation["provider_expires_at"]),
        created_at=cast(datetime, operation["created_at"]),
        revoked_at=(cast(datetime, operation["updated_at"]) if state == "revoked" else None),
        enrollment_state=cast(VoiceEnrollmentOperationState, state),
    )


def enrollment_operation_from_row(row: asyncpg.Record) -> VoiceEnrollmentOperation:
    return VoiceEnrollmentOperation(
        operation_id=str(row["operation_id"]),
        enrollment_key=str(row["enrollment_key"]),
        account_id=str(row["account_id"]),
        profile_id=str(row["profile_id"]),
        sample_id=str(row["sample_id"]),
        version_number=int(row["version_number"]),
        provider_prefix=str(row["provider_prefix"]),
        sample_purpose=str(row["sample_purpose"]),
        state=cast(VoiceEnrollmentOperationState, row["state"]),
        provider_voice_id=(
            str(row["provider_voice_id"]) if row["provider_voice_id"] is not None else None
        ),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=cast(datetime, row["created_at"]),
        updated_at=cast(datetime, row["updated_at"]),
    )


def profile_from_row(row: asyncpg.Record) -> VoiceProfile:
    return VoiceProfile(
        profile_id=str(row["profile_id"]),
        sample_id=str(row["sample_id"]),
        version_number=int(row["version_number"]),
        provider=str(row["provider"]),
        provider_region=str(row["provider_region"]),
        target_model=str(row["target_model"]),
        provider_voice_id=(
            str(row["provider_voice_id"]) if row["provider_voice_id"] is not None else None
        ),
        status=cast(VoiceProfileStatus, row["status"]),
        evaluation_status=cast(
            Literal["pending", "passed", "failed"],
            row["evaluation_status"],
        ),
        quality_status=cast(
            Literal["pending", "passed", "failed"],
            row["quality_status"],
        ),
        deletion_status=cast(VoiceDeletionStatus, row["deletion_status"]),
        sample_validation_status=cast(
            Literal["pending", "passed", "failed"],
            row["sample_validation_status"]
            if "sample_validation_status" in row.keys()
            else "pending",
        ),
        custom_persona_id=(
            str(row["custom_persona_id"])
            if "custom_persona_id" in row.keys()
            and row["custom_persona_id"] is not None
            else None
        ),
        provider_expires_at=cast(datetime | None, row["provider_expires_at"]),
        created_at=cast(datetime, row["created_at"]),
        activated_at=cast(datetime | None, row["activated_at"]),
        revoked_at=cast(datetime | None, row["revoked_at"]),
        enrollment_state=(
            cast(VoiceEnrollmentOperationState, row["enrollment_state"])
            if "enrollment_state" in row.keys() and row["enrollment_state"] is not None
            else None
        ),
    )
