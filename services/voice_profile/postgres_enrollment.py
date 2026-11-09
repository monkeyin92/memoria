"""Voice-profile enrollment persistence mixin.

``PostgresVoiceEnrollmentMixin`` owns the enrollment operation state machine
(create/accept/complete, sample and provider-voice persistence, operation
finalization).  Pools, object store, provider client and consent stay owned by
``PostgresVoiceProfileManager`` and are declared here for type checking only.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import asyncpg

from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.archive.object_store import ObjectRef, ObjectStore
from services.voice_profile.clone_authority import (
    VoiceCloneAuthority,
    clone_consent_valid,
    require_clone_consent,
)
from services.voice_profile.domain import (
    ProviderVoice,
    VoiceConsentRequiredError,
    VoiceEnrollmentOperationState,
    VoiceEnrollmentProvider,
    VoiceEnrollmentReconciliationRequiredError,
    VoiceEnrollmentRequest,
    VoiceProfile,
    VoiceSampleRejectedError,
)
from services.voice_profile.enrollment_records import profile_from_row
from services.voice_profile.sample_validation import (
    VoiceSampleValidation,
    validate_voice_sample,
)

if TYPE_CHECKING:
    pass


class PostgresVoiceEnrollmentMixin:
    """Persist enrollment operations for the PostgreSQL voice profile manager."""

    if TYPE_CHECKING:
        _object_store: ObjectStore
        _provider: VoiceEnrollmentProvider
        _provider_name: str
        _provider_region: str
        _clone: VoiceCloneAuthority
        _target_model: str
        _sample_url_factory: Callable[[str], str]

        async def _ready_pool(self) -> asyncpg.Pool: ...

        @staticmethod
        async def _scope(connection: asyncpg.Connection, account_id: str) -> None: ...

        def _event(
            self, account_id: str, event_type: str, payload: dict[str, object]
        ) -> EvidenceEvent: ...

        async def _insert_evidence(
            self, connection: asyncpg.Connection, event: EvidenceEvent
        ) -> None: ...

        async def _profile_for_operation(
            self, operation: asyncpg.Record
        ) -> VoiceProfile: ...

        async def revoke_profile(
            self, *, account_id: str, profile_id: str
        ) -> VoiceProfile: ...


    async def enroll(self, request: VoiceEnrollmentRequest) -> VoiceProfile:
        """Accept and complete an enrollment in one call.

        Lab clients and tests use this. The consumer path calls
        ``accept_enrollment`` and ``complete_enrollment`` separately so the
        upload request can return while the provider is still cloning.
        """
        profile = await self.accept_enrollment(request)
        if profile.status != "enrolling":
            return profile
        return await self.complete_enrollment(
            account_id=request.account_id, profile_id=profile.profile_id
        )

    async def accept_enrollment(self, request: VoiceEnrollmentRequest) -> VoiceProfile:
        """Measure, store and register one enrollment; no provider call.

        Returns the profile in ``enrolling``. Raises
        ``VoiceSampleRejectedError`` when the recording cannot be cloned --
        before anything is written, so a refused sample leaves no trace.
        """
        self._clone.admit()
        # Measure first: a sample that cannot be cloned must not create an
        # operation row, a stored object or a profile. Nothing can be cloned
        # from audio that does not decode, so this refusal is unconditional;
        # ``require_sample_validation`` only decides whether a *passing*
        # measurement is what admits the profile for delivery.
        validation = validate_voice_sample(
            request.audio, admit_on_pass=request.require_sample_validation
        )
        if not validation.passed:
            raise VoiceSampleRejectedError(validation)

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
                await self._persist_uploaded_sample(operation, request, reference, validation)
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

        if state in {"sample_uploaded", "provider_created"}:
            # Already accepted: the provider half is ``complete_enrollment``'s
            # job, and it is safely retryable from either state.
            profile = await self._profile_for_operation(operation)
            if profile.status == "enrolling":
                return profile
        raise VoiceEnrollmentReconciliationRequiredError(
            "voice enrollment is not safely retryable"
        )

    async def complete_enrollment(
        self, *, account_id: str, profile_id: str
    ) -> VoiceProfile:
        """Drive an accepted enrollment through the provider and finalize it."""
        self._clone.admit()
        operation = await self._operation_for_profile(account_id, profile_id)
        if operation is None or str(operation["account_id"]) != account_id:
            raise EvidenceNotFoundError(profile_id)
        state = str(operation["state"])
        if state == "completed":
            return await self._profile_for_operation(operation)
        if state == "revoked":
            raise VoiceConsentRequiredError("voice enrollment was revoked")
        if state == "provider_created":
            return await self._finalize_enrollment(operation)
        if state != "sample_uploaded":
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice enrollment is not safely retryable"
            )

        await self._set_operation_state(
            account_id,
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
                account_id,
                operation["operation_id"],
                state="reconciliation_required",
                last_error=type(exc).__name__,
            )
            raise
        try:
            await self._persist_provider_voice(
                account_id=account_id,
                operation_id=operation["operation_id"],
                voice=provider_voice,
            )
        except Exception as exc:
            # The provider may already have created the voice. Do not reissue it;
            # leave a needs-human state the profile poll can show.
            await self._set_operation_state(
                account_id,
                operation["operation_id"],
                state="reconciliation_required",
                last_error=type(exc).__name__,
            )
            raise
        operation = await self._operation_for_profile(account_id, profile_id)
        assert operation is not None, "a submitted enrollment always has an operation"
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
            await require_clone_consent(connection, request.account_id, for_update=True)
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
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10, $11, 'intent', $12, $12
                ) RETURNING *
                """,
                operation_id,
                enrollment_key,
                request.account_id,
                profile_id,
                sample_id,
                version,
                self._provider_name,
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
        validation: VoiceSampleValidation,
    ) -> None:
        now = datetime.now(UTC)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            await require_clone_consent(connection, request.account_id, for_update=True)
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
                    sample_validation_status, custom_persona_id,
                    created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7,
                    'enrolling', $8, $9, $10, $10
                )
                """,
                operation["profile_id"],
                request.account_id,
                operation["sample_id"],
                int(operation["version_number"]),
                self._provider_name,
                self._provider_region,
                self._target_model,
                "passed" if validation.admitted else "pending",
                request.custom_persona_id,
                now,
            )
            await self._insert_sample_validation(
                connection, operation, request, validation, now
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

    async def _insert_sample_validation(
        self,
        connection: asyncpg.Connection,
        operation: asyncpg.Record,
        request: VoiceEnrollmentRequest,
        validation: VoiceSampleValidation,
        now: datetime,
    ) -> None:
        """Store the measurement next to the profile it admitted or refused."""
        metrics = validation.metrics
        assert metrics is not None, "a sample without metrics never reaches storage"
        await connection.execute(
            """
            INSERT INTO voice_sample_validations (
                validation_id, account_id, profile_id, sample_id, status,
                duration_ms, sample_rate, channels, rms_dbfs, peak_dbfs,
                clipped_ratio, silence_ratio, speech_ms, dc_offset,
                reasons_json, created_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                $14, $15::jsonb, $16
            )
            """,
            uuid.uuid5(
                cast(uuid.UUID, operation["operation_id"]), "sample-validation"
            ),
            request.account_id,
            operation["profile_id"],
            operation["sample_id"],
            "passed" if validation.passed else "failed",
            metrics.duration_ms,
            metrics.sample_rate,
            metrics.channels,
            metrics.rms_dbfs,
            metrics.peak_dbfs,
            metrics.clipped_ratio,
            metrics.silence_ratio,
            metrics.speech_ms,
            metrics.dc_offset,
            json.dumps(list(validation.reasons)),
            now,
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
            raise VoiceEnrollmentReconciliationRequiredError("voice provider result is unavailable")
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
            row = None
            if await clone_consent_valid(
                connection, str(operation["account_id"]), for_update=True
            ):
                row = await connection.fetchrow(
                    """
                    UPDATE voice_profiles
                    SET provider_voice_id = $1, provider_expires_at = $2,
                        status = 'candidate', updated_at = $3
                    WHERE profile_id = $4 AND account_id = $5 AND status = 'enrolling'
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
            return profile_from_row(row)
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

    async def _operation_for_profile(self, account_id: str, profile_id: str) -> asyncpg.Record | None:
        """The enrollment operation that produced one of the account's profiles."""
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)  # FORCE RLS: unscoped reads see no row
            return await connection.fetchrow(
                """
                SELECT * FROM voice_enrollment_operations
                WHERE profile_id = $1::uuid
                ORDER BY created_at DESC LIMIT 1
                """,
                profile_id,
            )

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
