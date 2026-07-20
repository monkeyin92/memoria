"""PostgreSQL/RLS adapter for the voice-profile lifecycle."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

import asyncpg

from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.object_store import ObjectRef, ObjectStore
from services.voice_profile.domain import (
    EvaluationRequiredError,
    ProviderSample,
    ProviderVoice,
    VoiceBlindPreviewTarget,
    VoiceBlindSlot,
    VoiceBlindTrial,
    VoiceConsent,
    VoiceConsentRequiredError,
    VoiceDeletionStatus,
    VoiceEnrollmentOperation,
    VoiceEnrollmentOperationState,
    VoiceEnrollmentProvider,
    VoiceEnrollmentReconciliationRequiredError,
    VoiceEnrollmentRequest,
    VoiceEvaluation,
    VoiceEvaluationRequest,
    VoicePreviewTarget,
    VoiceProfile,
    VoiceProfileStatus,
    VoiceQualityMeasurement,
    VoiceQualityMeasurementRequest,
    VoiceResolution,
    objective_voice_quality_passes,
    subjective_voice_evaluation_passes,
)


class PostgresVoiceProfileManager:
    def __init__(
        self,
        dsn: str,
        *,
        object_store: ObjectStore,
        provider: VoiceEnrollmentProvider,
        sample_url_factory: Callable[[str], str],
        provider_region: str,
        target_model: str,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("voice profile DSN must use PostgreSQL")
        self._dsn = dsn
        self._object_store = object_store
        self._provider = provider
        self._sample_url_factory = sample_url_factory
        self._provider_region = provider_region
        self._target_model = target_model
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=10,
            command_timeout=15,
        )
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL voice profile pool")
        archive_schema = (Path(__file__).parents[1] / "archive" / "postgres_schema.sql").read_text(
            encoding="utf-8"
        )
        voice_schema = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(archive_schema)
            await connection.execute(voice_schema)
        self._pool = pool

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL voice profile manager is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    async def grant_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
    ) -> VoiceConsent:
        if not account_id.strip() or not policy_version.strip():
            raise ValueError("voice consent requires account_id and policy_version")
        now = datetime.now(UTC)
        event = self._event(
            account_id,
            "voice_clone.consent_granted",
            {"policy_version": policy_version},
        )
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._insert_evidence(connection, event)
            await connection.execute(
                """
                INSERT INTO voice_clone_consents (
                    account_id, policy_version, granted_at, grant_event_id
                ) VALUES ($1, $2, $3, $4)
                ON CONFLICT(account_id) DO UPDATE SET
                    policy_version = excluded.policy_version,
                    granted_at = excluded.granted_at,
                    revoked_at = NULL,
                    grant_event_id = excluded.grant_event_id,
                    revoke_event_id = NULL
                """,
                account_id,
                policy_version,
                now,
                event.event_id,
            )
        return VoiceConsent(account_id, policy_version, now)

    async def revoke_consent(self, *, account_id: str) -> VoiceConsent:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM voice_clone_consents
                WHERE account_id = $1
                FOR UPDATE
                """,
                account_id,
            )
            if row is None:
                raise VoiceConsentRequiredError("active voice-clone consent is required")
            profile_ids = [
                str(item["profile_id"])
                for item in await connection.fetch(
                    """
                    SELECT profile_id FROM voice_profiles
                    WHERE account_id = $1 AND deletion_status != 'completed'
                    UNION
                    SELECT operation.profile_id
                    FROM voice_enrollment_operations AS operation
                    LEFT JOIN voice_profiles AS profile
                      ON profile.profile_id = operation.profile_id
                    WHERE operation.account_id = $1 AND profile.profile_id IS NULL
                      AND operation.state NOT IN ('completed', 'revoked')
                    """,
                    account_id,
                )
            ]
            if row["revoked_at"] is None:
                revoked_at = datetime.now(UTC)
                event = self._event(account_id, "voice_clone.consent_revoked", {})
                await self._insert_evidence(connection, event)
                await connection.execute(
                    """
                    UPDATE voice_clone_consents
                    SET revoked_at = $1, revoke_event_id = $2
                    WHERE account_id = $3
                    """,
                    revoked_at,
                    event.event_id,
                    account_id,
                )
            else:
                revoked_at = cast(datetime, row["revoked_at"])
        deletion_incomplete = False
        for profile_id in profile_ids:
            try:
                profile = await self.revoke_profile(
                    account_id=account_id,
                    profile_id=profile_id,
                )
            except Exception:
                deletion_incomplete = True
            else:
                deletion_incomplete = deletion_incomplete or profile.deletion_status != "completed"
        if deletion_incomplete:
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice profile deletion incomplete; retry consent revocation"
            )
        return VoiceConsent(
            account_id=account_id,
            policy_version=str(row["policy_version"]),
            granted_at=cast(datetime, row["granted_at"]),
            revoked_at=revoked_at,
        )

    async def consent(self, *, account_id: str) -> VoiceConsent | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                "SELECT * FROM voice_clone_consents WHERE account_id = $1",
                account_id,
            )
        if row is None:
            return None
        return VoiceConsent(
            account_id=account_id,
            policy_version=str(row["policy_version"]),
            granted_at=cast(datetime, row["granted_at"]),
            revoked_at=cast(datetime | None, row["revoked_at"]),
        )

    async def enroll(self, request: VoiceEnrollmentRequest) -> VoiceProfile:
        operation = await self._ensure_enrollment_operation(request)
        state = str(operation["state"])
        if state == "completed":
            return await self._profile_for_operation(operation)
        if state == "revoked":
            raise VoiceConsentRequiredError("voice enrollment was revoked")
        if state in {
            "upload_submitted",
            "provider_submitted",
            "reconciliation_required",
        }:
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice enrollment requires provider or object-store reconciliation"
            )

        if state == "intent":
            await self._set_operation_state(
                str(operation["account_id"]),
                operation["operation_id"],
                state="upload_submitted",
            )
            try:
                reference = await self._object_store.put(
                    account_id=request.account_id,
                    purpose=str(operation["sample_purpose"]),
                    data=request.audio,
                    media_type=request.media_type,
                )
            except Exception as exc:
                await self._set_operation_state(
                    request.account_id,
                    operation["operation_id"],
                    state="reconciliation_required",
                    last_error=type(exc).__name__,
                )
                raise
            try:
                await self._persist_uploaded_sample(operation, request, reference)
            except Exception:
                try:
                    await self._object_store.delete(reference)
                except Exception as cleanup_exc:
                    await self._set_operation_state(
                        request.account_id,
                        operation["operation_id"],
                        state="reconciliation_required",
                        last_error=type(cleanup_exc).__name__,
                    )
                else:
                    await self._set_operation_state(
                        request.account_id,
                        operation["operation_id"],
                        state="intent",
                    )
                raise
            operation = await self._operation(
                account_id=request.account_id,
                enrollment_key=str(operation["enrollment_key"]),
            )
            state = str(operation["state"])

        if state == "provider_created":
            return await self._finalize_enrollment(operation)
        if state != "sample_uploaded":
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice enrollment is not safely retryable"
            )

        await self._set_operation_state(
            request.account_id,
            operation["operation_id"],
            state="provider_submitted",
        )
        try:
            provider_voice = await self._provider.create_voice(
                target_model=self._target_model,
                prefix=str(operation["provider_prefix"]),
                sample_url=self._sample_url_factory(str(operation["sample_id"])),
            )
            if provider_voice.target_model != self._target_model:
                raise RuntimeError("provider returned an unexpected voice model")
        except Exception as exc:
            await self._set_operation_state(
                request.account_id,
                operation["operation_id"],
                state="reconciliation_required",
                last_error=type(exc).__name__,
            )
            raise
        await self._persist_provider_voice(
            account_id=request.account_id,
            operation_id=operation["operation_id"],
            voice=provider_voice,
        )
        operation = await self._operation(
            account_id=request.account_id,
            enrollment_key=str(operation["enrollment_key"]),
        )
        return await self._finalize_enrollment(operation)

    async def _ensure_enrollment_operation(
        self,
        request: VoiceEnrollmentRequest,
    ) -> asyncpg.Record:
        enrollment_key = self._enrollment_key(request)
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:voice-enrollment:{request.account_id}:{enrollment_key}",
        )
        profile_id = uuid.uuid5(operation_id, "profile")
        sample_id = uuid.uuid5(operation_id, "sample")
        now = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            await self._require_consent(connection, request.account_id, for_update=True)
            existing = await connection.fetchrow(
                """
                SELECT * FROM voice_enrollment_operations
                WHERE account_id = $1 AND enrollment_key = $2
                """,
                request.account_id,
                enrollment_key,
            )
            if existing is not None:
                return existing
            version = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1 FROM (
                        SELECT version_number FROM voice_profiles WHERE account_id = $1
                        UNION ALL
                        SELECT version_number FROM voice_enrollment_operations
                        WHERE account_id = $1
                    ) AS versions
                    """,
                    request.account_id,
                )
            )
            created = await connection.fetchrow(
                """
                INSERT INTO voice_enrollment_operations (
                    operation_id, enrollment_key, account_id, profile_id, sample_id,
                    version_number, provider, provider_region, target_model,
                    provider_prefix, sample_purpose, state, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, 'alibaba_model_studio', $7, $8,
                    $9, $10, 'intent', $11, $11
                ) RETURNING *
                """,
                operation_id,
                enrollment_key,
                request.account_id,
                profile_id,
                sample_id,
                version,
                self._provider_region,
                self._target_model,
                f"m{profile_id.hex[:9]}",
                f"voice-{operation_id.hex[:32]}",
                now,
            )
        assert created is not None
        return created

    async def _persist_uploaded_sample(
        self,
        operation: asyncpg.Record,
        request: VoiceEnrollmentRequest,
        reference: ObjectRef,
    ) -> None:
        now = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            await self._require_consent(connection, request.account_id, for_update=True)
            await connection.execute(
                """
                INSERT INTO voice_samples (
                    sample_id, account_id, object_key, media_type, byte_count,
                    content_sha256, encryption_key_version, object_backend,
                    duration_ms, sample_rate, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                operation["sample_id"],
                request.account_id,
                reference.object_key,
                reference.media_type,
                reference.byte_count,
                reference.content_sha256,
                reference.encryption_key_version,
                reference.backend,
                request.duration_ms,
                request.sample_rate,
                now,
            )
            await connection.execute(
                """
                INSERT INTO voice_profiles (
                    profile_id, account_id, sample_id, version_number,
                    provider, provider_region, target_model, status,
                    created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, 'alibaba_model_studio', $5, $6,
                    'enrolling', $7, $7
                )
                """,
                operation["profile_id"],
                request.account_id,
                operation["sample_id"],
                int(operation["version_number"]),
                self._provider_region,
                self._target_model,
                now,
            )
            await connection.execute(
                """
                UPDATE voice_enrollment_operations
                SET state = 'sample_uploaded', last_error = NULL, updated_at = $1
                WHERE operation_id = $2 AND account_id = $3
                  AND state = 'upload_submitted'
                """,
                now,
                operation["operation_id"],
                request.account_id,
            )

    async def _persist_provider_voice(
        self,
        *,
        account_id: str,
        operation_id: uuid.UUID,
        voice: ProviderVoice,
    ) -> None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                UPDATE voice_enrollment_operations
                SET provider_voice_id = $1, provider_expires_at = $2,
                    state = 'provider_created', last_error = NULL, updated_at = $3
                WHERE operation_id = $4 AND account_id = $5
                  AND state IN ('provider_submitted', 'reconciliation_required')
                RETURNING operation_id
                """,
                voice.voice_id,
                voice.expires_at,
                datetime.now(UTC),
                operation_id,
                account_id,
            )
        if row is None:
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice provider result could not be attached to its enrollment"
            )

    async def _finalize_enrollment(self, operation: asyncpg.Record) -> VoiceProfile:
        provider_voice_id = str(operation["provider_voice_id"] or "")
        if not provider_voice_id:
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice provider result is unavailable"
            )
        now = datetime.now(UTC)
        event = self._event(
            str(operation["account_id"]),
            "voice_profile.enrolled",
            {
                "profile_id": str(operation["profile_id"]),
                "sample_id": str(operation["sample_id"]),
                "version_number": int(operation["version_number"]),
                "enrollment_key": str(operation["enrollment_key"]),
            },
        )
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, str(operation["account_id"]))
            row = await connection.fetchrow(
                """
                UPDATE voice_profiles
                SET provider_voice_id = $1, provider_expires_at = $2,
                    status = 'candidate', updated_at = $3
                WHERE profile_id = $4 AND account_id = $5 AND status = 'enrolling'
                  AND EXISTS (
                      SELECT 1 FROM voice_clone_consents
                      WHERE account_id = $5 AND revoked_at IS NULL
                  )
                RETURNING *
                """,
                provider_voice_id,
                operation["provider_expires_at"],
                now,
                operation["profile_id"],
                str(operation["account_id"]),
            )
            if row is not None:
                await self._insert_evidence(connection, event)
                await connection.execute(
                    """
                    UPDATE voice_enrollment_operations
                    SET state = 'completed', updated_at = $1
                    WHERE operation_id = $2 AND account_id = $3
                    """,
                    now,
                    operation["operation_id"],
                    str(operation["account_id"]),
                )
        if row is not None:
            return self._profile(row)
        revoked = await self.revoke_profile(
            account_id=str(operation["account_id"]),
            profile_id=str(operation["profile_id"]),
        )
        if revoked.deletion_status != "completed":
            raise RuntimeError("late provider voice deletion failed")
        raise VoiceConsentRequiredError("voice-clone consent was revoked during enrollment")

    @staticmethod
    def _enrollment_key(request: VoiceEnrollmentRequest) -> str:
        if request.enrollment_key is not None:
            return request.enrollment_key.strip()
        digest = hashlib.sha256(request.audio).hexdigest()
        material = (
            f"v1:{request.account_id}:{request.media_type}:{request.duration_ms}:"
            f"{request.sample_rate}:{digest}"
        )
        return f"auto-{hashlib.sha256(material.encode()).hexdigest()}"

    async def _operation(
        self,
        *,
        account_id: str,
        enrollment_key: str,
    ) -> asyncpg.Record:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM voice_enrollment_operations
                WHERE account_id = $1 AND enrollment_key = $2
                """,
                account_id,
                enrollment_key,
            )
        if row is None:
            raise EvidenceNotFoundError(enrollment_key)
        return row

    async def _set_operation_state(
        self,
        account_id: str,
        operation_id: uuid.UUID,
        *,
        state: VoiceEnrollmentOperationState,
        last_error: str | None = None,
    ) -> None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                """
                UPDATE voice_enrollment_operations
                SET state = $1, last_error = $2, updated_at = $3
                WHERE operation_id = $4 AND account_id = $5
                """,
                state,
                last_error,
                datetime.now(UTC),
                operation_id,
                account_id,
            )

    async def provider_sample(self, *, sample_id: str) -> ProviderSample:
        try:
            sample_uuid = uuid.UUID(sample_id)
        except ValueError as exc:
            raise VoiceConsentRequiredError("voice sample is unavailable") from exc
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('app.voice_sample_id', $1, true)",
                str(sample_uuid),
            )
            row = await connection.fetchrow(
                """
                SELECT * FROM voice_samples
                WHERE sample_id = $1 AND deleted_at IS NULL
                """,
                sample_uuid,
            )
            if row is None:
                raise VoiceConsentRequiredError("voice sample is unavailable")
            account_id = str(row["account_id"])
            await self._scope(connection, account_id)
            allowed = await connection.fetchval(
                """
                SELECT 1 FROM voice_profiles AS profile
                JOIN voice_clone_consents AS consent
                  ON consent.account_id = profile.account_id
                WHERE profile.sample_id = $1
                  AND profile.account_id = $2
                  AND consent.revoked_at IS NULL
                  AND profile.status IN ('enrolling', 'candidate', 'active')
                """,
                sample_uuid,
                account_id,
            )
        if allowed is None:
            raise VoiceConsentRequiredError("voice sample is unavailable")
        return ProviderSample(
            data=await self._object_store.get(self._reference(row)),
            media_type=str(row["media_type"]),
        )

    async def create_blind_trial(
        self,
        *,
        account_id: str,
        profile_id: str,
    ) -> VoiceBlindTrial:
        await self.preview_target(account_id=account_id, profile_id=profile_id)
        try:
            profile_uuid = uuid.UUID(profile_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(profile_id) from exc
        trial_id = uuid.uuid4()
        now = datetime.now(UTC)
        candidate_slot: VoiceBlindSlot = secrets.choice(("A", "B"))
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._require_consent(connection, account_id)
            await connection.execute(
                """
                INSERT INTO voice_blind_trials (
                    trial_id, account_id, profile_id, candidate_slot, created_at
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                trial_id,
                account_id,
                profile_uuid,
                candidate_slot,
                now,
            )
        return VoiceBlindTrial(
            trial_id=str(trial_id),
            profile_id=profile_id,
            slots=("A", "B"),
            created_at=now,
        )

    async def blind_preview_target(
        self,
        *,
        account_id: str,
        trial_id: str,
        slot: VoiceBlindSlot,
        text: str,
    ) -> VoiceBlindPreviewTarget:
        if slot not in {"A", "B"}:
            raise ValueError("blind voice slot must be A or B")
        if not 1 <= len(text.strip()) <= 120:
            raise ValueError("blind voice preview text must contain 1..120 characters")
        try:
            trial_uuid = uuid.UUID(trial_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(trial_id) from exc
        normalized_text = text.strip()
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._require_consent(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT trial.*, profile.target_model, profile.provider_voice_id,
                       profile.status, profile.provider_expires_at
                FROM voice_blind_trials AS trial
                JOIN voice_profiles AS profile ON profile.profile_id = trial.profile_id
                WHERE trial.trial_id = $1 AND trial.account_id = $2
                FOR UPDATE OF trial
                """,
                trial_uuid,
                account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(trial_id)
            if row["status"] not in {"candidate", "active"} or not row["provider_voice_id"]:
                raise EvaluationRequiredError("blind trial candidate is unavailable")
            expires_at = cast(datetime | None, row["provider_expires_at"])
            if expires_at is not None and expires_at <= datetime.now(UTC):
                raise EvaluationRequiredError("blind trial candidate has expired")
            if row["preview_text"] is not None and str(row["preview_text"]) != normalized_text:
                raise EvaluationRequiredError("both blind trial slots must use the same text")
            preview_column = "previewed_a" if slot == "A" else "previewed_b"
            await connection.execute(
                f"""
                UPDATE voice_blind_trials
                SET preview_text = COALESCE(preview_text, $1), {preview_column} = TRUE
                WHERE trial_id = $2 AND account_id = $3
                """,
                normalized_text,
                trial_uuid,
                account_id,
            )
        if str(row["candidate_slot"]) == slot:
            return VoiceBlindPreviewTarget(
                model=str(row["target_model"]),
                voice_id=str(row["provider_voice_id"]),
            )
        return VoiceBlindPreviewTarget(model=None, voice_id=None)

    async def resolve_blind_preference(
        self,
        *,
        account_id: str,
        profile_id: str,
        trial_id: str,
        preferred_slot: VoiceBlindSlot,
    ) -> bool:
        if preferred_slot not in {"A", "B"}:
            raise ValueError("preferred blind voice slot must be A or B")
        try:
            trial_uuid = uuid.UUID(trial_id)
            profile_uuid = uuid.UUID(profile_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(trial_id) from exc
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._require_consent(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT candidate_slot, previewed_a, previewed_b
                FROM voice_blind_trials
                WHERE trial_id = $1 AND account_id = $2 AND profile_id = $3
                """,
                trial_uuid,
                account_id,
                profile_uuid,
            )
        if row is None:
            raise EvidenceNotFoundError(trial_id)
        if not bool(row["previewed_a"]) or not bool(row["previewed_b"]):
            raise EvaluationRequiredError("both blind trial slots must be previewed")
        return str(row["candidate_slot"]) == preferred_slot

    async def evaluate(self, request: VoiceEvaluationRequest) -> VoiceEvaluation:
        passed = subjective_voice_evaluation_passes(
            candidate_preferred=request.candidate_preferred,
            similarity=request.similarity,
            naturalness=request.naturalness,
            accent_similarity=request.accent_similarity,
            emotion_adherence=request.emotion_adherence,
            instruction_adherence=request.instruction_adherence,
            uncanny=request.uncanny,
        )
        status: Literal["passed", "failed"] = "passed" if passed else "failed"
        evaluation_id = uuid.uuid4()
        now = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            await self._require_consent(connection, request.account_id)
            row = await connection.fetchrow(
                """
                SELECT status FROM voice_profiles
                WHERE profile_id = $1::uuid AND account_id = $2
                FOR UPDATE
                """,
                request.profile_id,
                request.account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(request.profile_id)
            if row["status"] not in {"candidate", "active"}:
                raise EvaluationRequiredError("only a usable candidate can be evaluated")
            await connection.execute(
                """
                INSERT INTO voice_evaluations (
                    evaluation_id, account_id, profile_id, similarity,
                    naturalness, accent_similarity, emotion_adherence,
                    instruction_adherence, uncanny, candidate_preferred,
                    first_audio_ms, cancel_tail_ms, timestamp_error_ms,
                    notes, status, created_at
                ) VALUES (
                    $1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10,
                    0, 0, 0, $11, $12, $13
                )
                """,
                evaluation_id,
                request.account_id,
                request.profile_id,
                request.similarity,
                request.naturalness,
                request.accent_similarity,
                request.emotion_adherence,
                request.instruction_adherence,
                request.uncanny,
                request.candidate_preferred,
                request.notes,
                status,
                now,
            )
            await connection.execute(
                """
                UPDATE voice_profiles SET evaluation_status = $1, updated_at = $2
                WHERE profile_id = $3::uuid AND account_id = $4
                """,
                status,
                now,
                request.profile_id,
                request.account_id,
            )
        return VoiceEvaluation(str(evaluation_id), request.profile_id, status, now)

    async def record_quality_measurement(
        self,
        request: VoiceQualityMeasurementRequest,
    ) -> VoiceQualityMeasurement:
        passed = objective_voice_quality_passes(
            first_audio_ms=request.first_audio_ms,
            cancel_tail_ms=request.cancel_tail_ms,
            timestamp_error_ms=request.timestamp_error_ms,
            long_sentence_chars=request.long_sentence_chars,
            long_sentence_completion_ratio=request.long_sentence_completion_ratio,
        )
        status: Literal["passed", "failed"] = "passed" if passed else "failed"
        measurement_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:voice-quality:{request.account_id}:{request.source_run_id}",
        )
        now = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            await self._require_consent(connection, request.account_id)
            profile = await connection.fetchrow(
                """
                SELECT status FROM voice_profiles
                WHERE profile_id = $1::uuid AND account_id = $2
                """,
                request.profile_id,
                request.account_id,
            )
            if profile is None:
                raise EvidenceNotFoundError(request.profile_id)
            if profile["status"] not in {"candidate", "active"}:
                raise EvaluationRequiredError(
                    "only a usable candidate can receive a quality measurement"
                )
            await connection.execute(
                """
                INSERT INTO voice_quality_measurements (
                    measurement_id, account_id, profile_id, source_run_id,
                    first_audio_ms, cancel_tail_ms, timestamp_error_ms,
                    long_sentence_chars, long_sentence_completion_ratio,
                    status, created_at
                ) VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT(account_id, source_run_id) DO NOTHING
                """,
                measurement_id,
                request.account_id,
                request.profile_id,
                request.source_run_id,
                request.first_audio_ms,
                request.cancel_tail_ms,
                request.timestamp_error_ms,
                request.long_sentence_chars,
                request.long_sentence_completion_ratio,
                status,
                now,
            )
            stored = await connection.fetchrow(
                """
                SELECT * FROM voice_quality_measurements
                WHERE account_id = $1 AND source_run_id = $2
                """,
                request.account_id,
                request.source_run_id,
            )
            assert stored is not None
            if str(stored["profile_id"]) != request.profile_id:
                raise ValueError("quality source_run_id belongs to another voice profile")
            await connection.execute(
                """
                UPDATE voice_profiles SET quality_status = $1, updated_at = $2
                WHERE profile_id = $3::uuid AND account_id = $4
                """,
                str(stored["status"]),
                now,
                request.profile_id,
                request.account_id,
            )
        return VoiceQualityMeasurement(
            measurement_id=str(stored["measurement_id"]),
            profile_id=request.profile_id,
            source_run_id=request.source_run_id,
            status=cast(Literal["passed", "failed"], stored["status"]),
            created_at=cast(datetime, stored["created_at"]),
        )

    async def account_id_for_profile(self, *, profile_id: str) -> str:
        try:
            profile_uuid = uuid.UUID(profile_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(profile_id) from exc
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT set_config('app.voice_profile_id', $1, true)",
                str(profile_uuid),
            )
            account_id = await connection.fetchval(
                "SELECT account_id FROM voice_profiles WHERE profile_id = $1",
                profile_uuid,
            )
        if account_id is None:
            raise EvidenceNotFoundError(profile_id)
        return str(account_id)

    async def activate(self, *, account_id: str, profile_id: str) -> VoiceProfile:
        now = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._require_consent(connection, account_id, for_update=True)
            row = await connection.fetchrow(
                """
                SELECT * FROM voice_profiles
                WHERE profile_id = $1::uuid AND account_id = $2
                FOR UPDATE
                """,
                profile_id,
                account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(profile_id)
            if row["status"] not in {"candidate", "active"} or row["evaluation_status"] != "passed":
                raise EvaluationRequiredError("a passed candidate evaluation is required")
            if row["quality_status"] != "passed":
                raise EvaluationRequiredError("a passed provider quality measurement is required")
            event = self._event(
                account_id,
                "voice_profile.activated",
                {
                    "profile_id": profile_id,
                    "version_number": int(row["version_number"]),
                },
            )
            await self._insert_evidence(connection, event)
            await connection.execute(
                """
                UPDATE voice_profiles SET status = 'candidate', activated_at = NULL
                WHERE account_id = $1 AND status = 'active'
                """,
                account_id,
            )
            updated = await connection.fetchrow(
                """
                UPDATE voice_profiles
                SET status = 'active', activated_at = $1, updated_at = $1
                WHERE profile_id = $2::uuid AND account_id = $3
                RETURNING *
                """,
                now,
                profile_id,
                account_id,
            )
        assert updated is not None
        return self._profile(updated)

    async def resolve(self, *, account_id: str) -> VoiceResolution:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            consent = await connection.fetchval(
                """
                SELECT 1 FROM voice_clone_consents
                WHERE account_id = $1 AND revoked_at IS NULL
                """,
                account_id,
            )
            row = await connection.fetchrow(
                """
                SELECT * FROM voice_profiles
                WHERE account_id = $1 AND status = 'active'
                """,
                account_id,
            )
        if consent is None or row is None or row["provider_voice_id"] is None:
            return VoiceResolution(mode="fallback")
        expires = cast(datetime | None, row["provider_expires_at"])
        if expires is not None and expires <= datetime.now(UTC):
            return VoiceResolution(mode="fallback")
        return VoiceResolution(
            mode="active",
            profile_id=str(row["profile_id"]),
            model=str(row["target_model"]),
            voice_id=str(row["provider_voice_id"]),
        )

    async def profiles(self, *, account_id: str) -> tuple[VoiceProfile, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM voice_profiles
                WHERE account_id = $1 ORDER BY version_number DESC
                """,
                account_id,
            )
            operations = await connection.fetch(
                """
                SELECT operation.* FROM voice_enrollment_operations AS operation
                LEFT JOIN voice_profiles AS profile
                  ON profile.profile_id = operation.profile_id
                WHERE operation.account_id = $1 AND profile.profile_id IS NULL
                  AND operation.state NOT IN ('completed', 'revoked')
                ORDER BY operation.version_number DESC
                """,
                account_id,
            )
        combined = [self._profile(row) for row in rows]
        combined.extend(self._synthetic_profile(operation) for operation in operations)
        return tuple(sorted(combined, key=lambda profile: profile.version_number, reverse=True))

    async def pending_enrollments(
        self,
        *,
        account_id: str,
    ) -> tuple[VoiceEnrollmentOperation, ...]:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM voice_enrollment_operations
                WHERE account_id = $1 AND state NOT IN ('completed', 'revoked')
                ORDER BY version_number
                """,
                account_id,
            )
        return tuple(self._enrollment_operation(row) for row in rows)

    async def reconcile_enrollment(
        self,
        *,
        account_id: str,
        enrollment_key: str,
        provider_voice: ProviderVoice | None = None,
        provider_asset_absent: bool = False,
        sample_asset_absent: bool = False,
    ) -> VoiceProfile:
        if sum(
            (
                provider_voice is not None,
                provider_asset_absent,
                sample_asset_absent,
            )
        ) != 1:
            raise ValueError("exactly one enrollment reconciliation outcome is required")
        operation = await self._operation(
            account_id=account_id,
            enrollment_key=enrollment_key.strip(),
        )
        if provider_voice is not None:
            if provider_voice.target_model != str(operation["target_model"]):
                raise ValueError("reconciled provider voice has an unexpected target model")
            await self._persist_provider_voice(
                account_id=account_id,
                operation_id=operation["operation_id"],
                voice=provider_voice,
            )
            refreshed = await self._operation(
                account_id=account_id,
                enrollment_key=enrollment_key.strip(),
            )
            return await self._finalize_enrollment(refreshed)
        if provider_asset_absent:
            if str(operation["state"]) not in {
                "provider_submitted",
                "reconciliation_required",
            }:
                raise ValueError("enrollment has no ambiguous provider submission")
            await self._set_operation_state(
                account_id,
                operation["operation_id"],
                state="sample_uploaded",
            )
        else:
            if str(operation["state"]) not in {
                "upload_submitted",
                "reconciliation_required",
            } or await self._profile_row(account_id, operation["profile_id"]) is not None:
                raise ValueError("enrollment has no ambiguous sample upload")
            await self._set_operation_state(
                account_id,
                operation["operation_id"],
                state="intent",
            )
        refreshed = await self._operation(
            account_id=account_id,
            enrollment_key=enrollment_key.strip(),
        )
        return await self._profile_for_operation(refreshed)

    async def preview_target(
        self,
        *,
        account_id: str,
        profile_id: str,
    ) -> VoicePreviewTarget:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._require_consent(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM voice_profiles
                WHERE profile_id = $1::uuid AND account_id = $2
                """,
                profile_id,
                account_id,
            )
        if row is None:
            raise EvidenceNotFoundError(profile_id)
        if row["status"] not in {"candidate", "active"} or row["provider_voice_id"] is None:
            raise EvaluationRequiredError("only a usable voice profile can be previewed")
        expires = cast(datetime | None, row["provider_expires_at"])
        if expires is not None and expires <= datetime.now(UTC):
            raise EvaluationRequiredError("expired voice profile cannot be previewed")
        return VoicePreviewTarget(
            model=str(row["target_model"]),
            voice_id=str(row["provider_voice_id"]),
        )

    async def revoke_profile(self, *, account_id: str, profile_id: str) -> VoiceProfile:
        now = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT profile.*, sample.object_key, sample.media_type,
                       sample.byte_count, sample.content_sha256,
                       sample.encryption_key_version, sample.object_backend,
                       operation.operation_id AS enrollment_operation_id,
                       operation.state AS enrollment_state,
                       operation.provider_voice_id AS enrollment_provider_voice_id
                FROM voice_profiles AS profile
                JOIN voice_samples AS sample ON sample.sample_id = profile.sample_id
                LEFT JOIN voice_enrollment_operations AS operation
                  ON operation.profile_id = profile.profile_id
                WHERE profile.profile_id = $1::uuid AND profile.account_id = $2
                FOR UPDATE OF profile
                """,
                profile_id,
                account_id,
            )
            if row is None:
                operation = await connection.fetchrow(
                    """
                    SELECT * FROM voice_enrollment_operations
                    WHERE profile_id = $1::uuid AND account_id = $2
                    FOR UPDATE
                    """,
                    profile_id,
                    account_id,
                )
                if operation is None:
                    raise EvidenceNotFoundError(profile_id)
                if operation["state"] == "intent":
                    refreshed = await connection.fetchrow(
                        """
                        UPDATE voice_enrollment_operations
                        SET state = 'revoked', updated_at = $1
                        WHERE operation_id = $2 AND account_id = $3
                        RETURNING *
                        """,
                        now,
                        operation["operation_id"],
                        account_id,
                    )
                    assert refreshed is not None
                    return self._synthetic_profile(refreshed)
                refreshed = await connection.fetchrow(
                    """
                    UPDATE voice_enrollment_operations
                    SET state = 'reconciliation_required',
                        last_error = 'account_deletion_requires_asset_reconciliation',
                        updated_at = $1
                    WHERE operation_id = $2 AND account_id = $3
                    RETURNING *
                    """,
                    now,
                    operation["operation_id"],
                    account_id,
                )
                assert refreshed is not None
                return self._synthetic_profile(refreshed)
            if row["status"] == "revoked" and row["deletion_status"] == "completed":
                return self._profile(row)
            if row["status"] != "revoked":
                event = self._event(
                    account_id,
                    "voice_profile.revoked",
                    {"profile_id": profile_id},
                )
                await self._insert_evidence(connection, event)
                await connection.execute(
                    """
                    UPDATE voice_profiles
                    SET status = 'revoked', deletion_status = 'pending',
                        revoked_at = $1, updated_at = $1
                    WHERE profile_id = $2::uuid AND account_id = $3
                    """,
                    now,
                    profile_id,
                    account_id,
                )
            reference = self._reference(row)
            voice_id = (
                str(row["provider_voice_id"] or row["enrollment_provider_voice_id"])
                if row["provider_voice_id"] or row["enrollment_provider_voice_id"]
                else None
            )
            provider_result_ambiguous = (
                row["enrollment_state"]
                in {"provider_submitted", "reconciliation_required"}
                and voice_id is None
            )
        await self._object_store.delete(reference)
        deletion_status: VoiceDeletionStatus = (
            "failed" if provider_result_ambiguous else "completed"
        )
        try:
            if voice_id is not None:
                await self._provider.delete_voice(voice_id=voice_id)
        except Exception:
            deletion_status = "failed"
        completed_at = datetime.now(UTC)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await connection.execute(
                """
                UPDATE voice_samples SET deleted_at = COALESCE(deleted_at, $1)
                WHERE sample_id = $2 AND account_id = $3
                """,
                completed_at,
                row["sample_id"],
                account_id,
            )
            updated = await connection.fetchrow(
                """
                UPDATE voice_profiles
                SET provider_voice_id = COALESCE(provider_voice_id, $1),
                    deletion_status = $2, provider_deleted_at = $3, updated_at = $4
                WHERE profile_id = $5::uuid AND account_id = $6
                RETURNING *
                """,
                voice_id,
                deletion_status,
                completed_at if deletion_status == "completed" else None,
                completed_at,
                profile_id,
                account_id,
            )
            if row["enrollment_operation_id"] is not None:
                await connection.execute(
                    """
                    UPDATE voice_enrollment_operations
                    SET state = $1, last_error = $2, updated_at = $3
                    WHERE operation_id = $4 AND account_id = $5
                    """,
                    "revoked" if deletion_status == "completed" else "reconciliation_required",
                    (
                        None
                        if deletion_status == "completed"
                        else "provider_asset_deletion_incomplete"
                    ),
                    completed_at,
                    row["enrollment_operation_id"],
                    account_id,
                )
        assert updated is not None
        return self._profile(updated)

    @staticmethod
    async def _require_consent(
        connection: asyncpg.Connection,
        account_id: str,
        *,
        for_update: bool = False,
    ) -> None:
        suffix = " FOR UPDATE" if for_update else ""
        row = await connection.fetchval(
            """
            SELECT 1 FROM voice_clone_consents
            WHERE account_id = $1 AND revoked_at IS NULL
            """
            + suffix,
            account_id,
        )
        if row is None:
            raise VoiceConsentRequiredError("active voice-clone consent is required")

    @staticmethod
    def _reference(row: asyncpg.Record) -> ObjectRef:
        return ObjectRef(
            account_id=str(row["account_id"]),
            object_key=str(row["object_key"]),
            media_type=str(row["media_type"]),
            byte_count=int(row["byte_count"]),
            content_sha256=str(row["content_sha256"]),
            encryption_key_version=str(row["encryption_key_version"]),
            backend=str(row["object_backend"]),
        )

    async def _profile_row(
        self,
        account_id: str,
        profile_id: uuid.UUID,
    ) -> asyncpg.Record | None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            return await connection.fetchrow(
                """
                SELECT * FROM voice_profiles
                WHERE profile_id = $1 AND account_id = $2
                """,
                profile_id,
                account_id,
            )

    async def _profile_for_operation(self, operation: asyncpg.Record) -> VoiceProfile:
        row = await self._profile_row(
            str(operation["account_id"]),
            operation["profile_id"],
        )
        return self._profile(row) if row is not None else self._synthetic_profile(operation)

    @staticmethod
    def _synthetic_profile(operation: asyncpg.Record) -> VoiceProfile:
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
        )

    @staticmethod
    def _enrollment_operation(row: asyncpg.Record) -> VoiceEnrollmentOperation:
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
                str(row["provider_voice_id"])
                if row["provider_voice_id"] is not None
                else None
            ),
            last_error=str(row["last_error"]) if row["last_error"] is not None else None,
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    @staticmethod
    def _profile(row: asyncpg.Record) -> VoiceProfile:
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
            provider_expires_at=cast(datetime | None, row["provider_expires_at"]),
            created_at=cast(datetime, row["created_at"]),
            activated_at=cast(datetime | None, row["activated_at"]),
            revoked_at=cast(datetime | None, row["revoked_at"]),
        )

    @staticmethod
    def _event(account_id: str, event_type: str, payload: dict[str, object]) -> EvidenceEvent:
        return EvidenceEvent(
            event_id=str(uuid.uuid4()),
            account_id=account_id,
            event_type=event_type,
            occurred_at=datetime.now(UTC),
            speaker_class="system",
            source="voice_profile.manager",
            payload=payload,
        )

    @staticmethod
    async def _insert_evidence(
        connection: asyncpg.Connection,
        event: EvidenceEvent,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO archive_evidence_events (
                event_id, account_id, session_id, turn_id, generation_id,
                event_type, schema_version, occurred_at, speaker_identity_id,
                speaker_class, source, consent_grant_id, payload,
                content_sha256, supersedes_event_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                $13::jsonb, $14, $15
            )
            """,
            event.event_id,
            event.account_id,
            event.session_id,
            event.turn_id,
            event.generation_id,
            event.event_type,
            event.schema_version,
            event.occurred_at,
            event.speaker_identity_id,
            event.speaker_class,
            event.source,
            event.consent_grant_id,
            json.dumps(dict(event.payload), ensure_ascii=False, separators=(",", ":")),
            event.content_sha256,
            event.supersedes_event_id,
        )
        await connection.execute(
            """
            INSERT INTO archive_processing_outbox (
                outbox_id, account_id, event_id, task_type
            ) VALUES ($1, $2, $3, 'compile_evidence')
            """,
            uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:outbox:{event.event_id}"),
            event.account_id,
            event.event_id,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
