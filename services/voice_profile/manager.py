"""SQLite voice-profile lifecycle with encrypted samples and provider cleanup."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from services.archive.domain import (
    EvidenceEvent,
    EvidenceNotFoundError,
    canonical_payload,
)
from services.archive.life_archive import LifeArchive
from services.archive.object_store import ObjectRef, ObjectStore
from services.voice_profile.domain import (
    EvaluationRequiredError,
    ProviderSample,
    ProviderVoice,
    ProviderVoiceDeletionUnsupportedError,
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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voice_clone_consents (
    account_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    grant_event_id TEXT NOT NULL,
    revoke_event_id TEXT
);

CREATE TABLE IF NOT EXISTS voice_samples (
    sample_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count > 0),
    content_sha256 TEXT NOT NULL,
    encryption_key_version TEXT NOT NULL,
    object_backend TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    sample_rate INTEGER NOT NULL CHECK (sample_rate >= 16000),
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS voice_enrollment_operations (
    operation_id TEXT PRIMARY KEY,
    enrollment_key TEXT NOT NULL,
    account_id TEXT NOT NULL,
    profile_id TEXT NOT NULL UNIQUE,
    sample_id TEXT NOT NULL UNIQUE,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    provider TEXT NOT NULL,
    provider_region TEXT NOT NULL,
    target_model TEXT NOT NULL,
    provider_prefix TEXT NOT NULL,
    sample_purpose TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'intent', 'upload_submitted', 'sample_uploaded',
            'provider_submitted', 'provider_created', 'completed',
            'reconciliation_required', 'revoked'
        )
    ),
    provider_voice_id TEXT,
    provider_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, enrollment_key),
    UNIQUE (account_id, version_number),
    FOREIGN KEY (account_id) REFERENCES voice_clone_consents(account_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_profiles (
    profile_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    provider TEXT NOT NULL,
    provider_region TEXT NOT NULL,
    target_model TEXT NOT NULL,
    provider_voice_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('enrolling', 'candidate', 'active', 'failed', 'revoked')
    ),
    evaluation_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (evaluation_status IN ('pending', 'passed', 'failed')),
    quality_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (quality_status IN ('pending', 'passed', 'failed')),
    deletion_status TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (deletion_status IN ('not_requested', 'pending', 'completed', 'failed')),
    provider_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    activated_at TEXT,
    revoked_at TEXT,
    provider_deleted_at TEXT,
    UNIQUE (account_id, version_number),
    FOREIGN KEY (sample_id) REFERENCES voice_samples(sample_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_one_active
ON voice_profiles(account_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS voice_blind_trials (
    trial_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    candidate_slot TEXT NOT NULL CHECK (candidate_slot IN ('A', 'B')),
    preview_text TEXT,
    previewed_a INTEGER NOT NULL DEFAULT 0 CHECK (previewed_a IN (0, 1)),
    previewed_b INTEGER NOT NULL DEFAULT 0 CHECK (previewed_b IN (0, 1)),
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES voice_profiles(profile_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    similarity REAL NOT NULL,
    naturalness REAL NOT NULL,
    accent_similarity REAL NOT NULL CHECK (accent_similarity BETWEEN 1 AND 5),
    emotion_adherence REAL NOT NULL CHECK (emotion_adherence BETWEEN 1 AND 5),
    instruction_adherence REAL NOT NULL CHECK (instruction_adherence BETWEEN 1 AND 5),
    uncanny REAL NOT NULL,
    candidate_preferred INTEGER,
    first_audio_ms INTEGER NOT NULL DEFAULT 0,
    cancel_tail_ms INTEGER NOT NULL DEFAULT 0,
    timestamp_error_ms INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES voice_profiles(profile_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_quality_measurements (
    measurement_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    source_run_id TEXT NOT NULL,
    first_audio_ms INTEGER NOT NULL CHECK (first_audio_ms >= 0),
    cancel_tail_ms INTEGER NOT NULL CHECK (cancel_tail_ms >= 0),
    timestamp_error_ms INTEGER NOT NULL CHECK (timestamp_error_ms >= 0),
    long_sentence_chars INTEGER NOT NULL CHECK (long_sentence_chars > 0),
    long_sentence_completion_ratio REAL NOT NULL CHECK (
        long_sentence_completion_ratio BETWEEN 0 AND 1
    ),
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    created_at TEXT NOT NULL,
    UNIQUE (account_id, source_run_id),
    FOREIGN KEY (profile_id) REFERENCES voice_profiles(profile_id) ON DELETE CASCADE
);
"""


class VoiceProfileManager:
    def __init__(
        self,
        sqlite_path: Path,
        *,
        object_store: ObjectStore,
        provider: VoiceEnrollmentProvider,
        sample_url_factory: Callable[[str], str],
        provider_region: str,
        target_model: str,
        provider_name: str = "alibaba_model_studio",
    ) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._object_store = object_store
        self._provider = provider
        self._sample_url_factory = sample_url_factory
        self._provider_region = provider_region
        self._target_model = target_model
        if not provider_name.strip():
            raise ValueError("voice provider name is required")
        self._provider_name = provider_name
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        **kwargs: object,
    ) -> VoiceProfileManager:
        return cls(Path(path), **kwargs)  # type: ignore[arg-type]

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            LifeArchive.sqlite(self._path).initialize()
            with sqlite3.connect(self._path, timeout=5) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_SCHEMA)
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(voice_profiles)")
                }
                if "quality_status" not in columns:
                    connection.execute(
                        "ALTER TABLE voice_profiles ADD COLUMN quality_status TEXT NOT NULL DEFAULT 'pending'"
                    )
                evaluation_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(voice_evaluations)")
                }
                if "candidate_preferred" not in evaluation_columns:
                    connection.execute(
                        "ALTER TABLE voice_evaluations ADD COLUMN candidate_preferred INTEGER"
                    )
                for name in (
                    "accent_similarity",
                    "emotion_adherence",
                    "instruction_adherence",
                ):
                    if name not in evaluation_columns:
                        connection.execute(
                            f"ALTER TABLE voice_evaluations ADD COLUMN {name} REAL NOT NULL DEFAULT 0"
                        )
                quality_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(voice_quality_measurements)")
                }
                if "long_sentence_chars" not in quality_columns:
                    connection.execute(
                        "ALTER TABLE voice_quality_measurements ADD COLUMN "
                        "long_sentence_chars INTEGER NOT NULL DEFAULT 0"
                    )
                if "long_sentence_completion_ratio" not in quality_columns:
                    connection.execute(
                        "ALTER TABLE voice_quality_measurements ADD COLUMN "
                        "long_sentence_completion_ratio REAL NOT NULL DEFAULT 0"
                    )
                connection.execute(
                    """
                    UPDATE voice_profiles
                    SET evaluation_status = 'pending',
                        status = CASE WHEN status = 'active' THEN 'candidate' ELSE status END,
                        activated_at = CASE WHEN status = 'active' THEN NULL ELSE activated_at END
                    WHERE evaluation_status = 'passed' AND EXISTS (
                        SELECT 1 FROM voice_evaluations evaluation
                        WHERE evaluation.profile_id = voice_profiles.profile_id
                          AND (
                              evaluation.accent_similarity = 0
                              OR evaluation.emotion_adherence = 0
                              OR evaluation.instruction_adherence = 0
                          )
                    )
                    """
                )
                connection.execute(
                    """
                    UPDATE voice_profiles
                    SET quality_status = 'pending',
                        status = CASE WHEN status = 'active' THEN 'candidate' ELSE status END,
                        activated_at = CASE WHEN status = 'active' THEN NULL ELSE activated_at END
                    WHERE quality_status = 'passed' AND EXISTS (
                        SELECT 1 FROM voice_quality_measurements measurement
                        WHERE measurement.profile_id = voice_profiles.profile_id
                          AND (
                              measurement.long_sentence_chars = 0
                              OR measurement.long_sentence_completion_ratio = 0
                          )
                    )
                    """
                )
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

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
        with self._connect() as connection:
            self._insert_evidence(connection, event)
            connection.execute(
                """
                INSERT INTO voice_clone_consents (
                    account_id, policy_version, granted_at, grant_event_id
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    policy_version = excluded.policy_version,
                    granted_at = excluded.granted_at,
                    revoked_at = NULL,
                    grant_event_id = excluded.grant_event_id,
                    revoke_event_id = NULL
                """,
                (account_id, policy_version, now.isoformat(), event.event_id),
            )
        return VoiceConsent(account_id, policy_version, now)

    async def revoke_consent(self, *, account_id: str) -> VoiceConsent:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_clone_consents WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if row is None:
                raise VoiceConsentRequiredError("active voice-clone consent is required")
            profile_ids = [
                str(item["profile_id"])
                for item in connection.execute(
                    """
                    SELECT profile_id FROM voice_profiles
                    WHERE account_id = ? AND deletion_status != 'completed'
                    UNION
                    SELECT operation.profile_id
                    FROM voice_enrollment_operations AS operation
                    LEFT JOIN voice_profiles AS profile
                      ON profile.profile_id = operation.profile_id
                    WHERE operation.account_id = ? AND profile.profile_id IS NULL
                      AND operation.state NOT IN ('completed', 'revoked')
                    """,
                    (account_id, account_id),
                ).fetchall()
            ]
            if row["revoked_at"] is None:
                revoked_at = datetime.now(UTC)
                event = self._event(account_id, "voice_clone.consent_revoked", {})
                self._insert_evidence(connection, event)
                connection.execute(
                    """
                    UPDATE voice_clone_consents
                    SET revoked_at = ?, revoke_event_id = ? WHERE account_id = ?
                    """,
                    (revoked_at.isoformat(), event.event_id, account_id),
                )
            else:
                revoked_at = datetime.fromisoformat(str(row["revoked_at"]))
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
                deletion_incomplete = deletion_incomplete or profile.deletion_status == "failed"
        if deletion_incomplete:
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice profile deletion incomplete; retry consent revocation"
            )
        return VoiceConsent(
            account_id=account_id,
            policy_version=str(row["policy_version"]),
            granted_at=datetime.fromisoformat(str(row["granted_at"])),
            revoked_at=revoked_at,
        )

    async def consent(self, *, account_id: str) -> VoiceConsent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM voice_clone_consents WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return VoiceConsent(
            account_id=account_id,
            policy_version=str(row["policy_version"]),
            granted_at=datetime.fromisoformat(str(row["granted_at"])),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
        )

    async def enroll(self, request: VoiceEnrollmentRequest) -> VoiceProfile:
        operation = self._ensure_enrollment_operation(request)
        state = str(operation["state"])
        if state == "completed":
            return self._profile_for_operation(operation)
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
            self._set_operation_state(
                str(operation["operation_id"]),
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
                self._set_operation_state(
                    str(operation["operation_id"]),
                    state="reconciliation_required",
                    last_error=type(exc).__name__,
                )
                raise
            try:
                self._persist_uploaded_sample(operation, request, reference)
            except Exception:
                try:
                    await self._object_store.delete(reference)
                except Exception as cleanup_exc:
                    self._set_operation_state(
                        str(operation["operation_id"]),
                        state="reconciliation_required",
                        last_error=type(cleanup_exc).__name__,
                    )
                else:
                    self._set_operation_state(
                        str(operation["operation_id"]),
                        state="intent",
                    )
                raise
            operation = self._operation(
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

        self._set_operation_state(
            str(operation["operation_id"]),
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
            self._set_operation_state(
                str(operation["operation_id"]),
                state="reconciliation_required",
                last_error=type(exc).__name__,
            )
            raise
        self._persist_provider_voice(str(operation["operation_id"]), provider_voice)
        operation = self._operation(
            account_id=request.account_id,
            enrollment_key=str(operation["enrollment_key"]),
        )
        return await self._finalize_enrollment(operation)

    def _ensure_enrollment_operation(self, request: VoiceEnrollmentRequest) -> sqlite3.Row:
        enrollment_key = self._enrollment_key(request)
        operation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:voice-enrollment:{request.account_id}:{enrollment_key}",
        )
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_consent(connection, request.account_id)
            existing = connection.execute(
                """
                SELECT * FROM voice_enrollment_operations
                WHERE account_id = ? AND enrollment_key = ?
                """,
                (request.account_id, enrollment_key),
            ).fetchone()
            if existing is not None:
                return cast(sqlite3.Row, existing)
            version = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(version_number), 0) + 1 FROM (
                        SELECT version_number FROM voice_profiles WHERE account_id = ?
                        UNION ALL
                        SELECT version_number FROM voice_enrollment_operations
                        WHERE account_id = ?
                    )
                    """,
                    (request.account_id, request.account_id),
                ).fetchone()[0]
            )
            profile_id = uuid.uuid5(operation_id, "profile")
            sample_id = uuid.uuid5(operation_id, "sample")
            connection.execute(
                """
                INSERT INTO voice_enrollment_operations (
                    operation_id, enrollment_key, account_id, profile_id, sample_id,
                    version_number, provider, provider_region, target_model,
                    provider_prefix, sample_purpose, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'intent', ?, ?)
                """,
                (
                    str(operation_id),
                    enrollment_key,
                    request.account_id,
                    str(profile_id),
                    str(sample_id),
                    version,
                    self._provider_name,
                    self._provider_region,
                    self._target_model,
                    f"m{profile_id.hex[:9]}",
                    f"voice-{operation_id.hex[:32]}",
                    now,
                    now,
                ),
            )
            created = connection.execute(
                "SELECT * FROM voice_enrollment_operations WHERE operation_id = ?",
                (str(operation_id),),
            ).fetchone()
        assert created is not None
        return cast(sqlite3.Row, created)

    def _persist_uploaded_sample(
        self,
        operation: sqlite3.Row,
        request: VoiceEnrollmentRequest,
        reference: ObjectRef,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            self._require_consent(connection, request.account_id)
            connection.execute(
                """
                INSERT INTO voice_samples (
                    sample_id, account_id, object_key, media_type, byte_count,
                    content_sha256, encryption_key_version, object_backend,
                    duration_ms, sample_rate, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(operation["sample_id"]),
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
                ),
            )
            connection.execute(
                """
                INSERT INTO voice_profiles (
                    profile_id, account_id, sample_id, version_number,
                    provider, provider_region, target_model, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'enrolling', ?, ?)
                """,
                (
                    str(operation["profile_id"]),
                    request.account_id,
                    str(operation["sample_id"]),
                    int(operation["version_number"]),
                    self._provider_name,
                    self._provider_region,
                    self._target_model,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE voice_enrollment_operations
                SET state = 'sample_uploaded', last_error = NULL, updated_at = ?
                WHERE operation_id = ? AND state = 'upload_submitted'
                """,
                (now, str(operation["operation_id"])),
            )

    def _persist_provider_voice(self, operation_id: str, voice: ProviderVoice) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_enrollment_operations
                SET provider_voice_id = ?, provider_expires_at = ?,
                    state = 'provider_created', last_error = NULL, updated_at = ?
                WHERE operation_id = ?
                  AND state IN ('provider_submitted', 'reconciliation_required')
                """,
                (
                    voice.voice_id,
                    voice.expires_at.isoformat() if voice.expires_at is not None else None,
                    datetime.now(UTC).isoformat(),
                    operation_id,
                ),
            )
        if cursor.rowcount != 1:
            raise VoiceEnrollmentReconciliationRequiredError(
                "voice provider result could not be attached to its enrollment"
            )

    async def _finalize_enrollment(self, operation: sqlite3.Row) -> VoiceProfile:
        provider_voice_id = str(operation["provider_voice_id"] or "")
        if not provider_voice_id:
            raise VoiceEnrollmentReconciliationRequiredError("voice provider result is unavailable")
        now = datetime.now(UTC).isoformat()
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
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_profiles
                SET provider_voice_id = ?, provider_expires_at = ?,
                    status = 'candidate', updated_at = ?
                WHERE profile_id = ? AND account_id = ? AND status = 'enrolling'
                  AND EXISTS (
                      SELECT 1 FROM voice_clone_consents
                      WHERE account_id = ? AND revoked_at IS NULL
                  )
                """,
                (
                    provider_voice_id,
                    operation["provider_expires_at"],
                    now,
                    str(operation["profile_id"]),
                    str(operation["account_id"]),
                    str(operation["account_id"]),
                ),
            )
            row = (
                connection.execute(
                    "SELECT * FROM voice_profiles WHERE profile_id = ?",
                    (str(operation["profile_id"]),),
                ).fetchone()
                if cursor.rowcount == 1
                else None
            )
            if row is not None:
                self._insert_evidence(connection, event)
                connection.execute(
                    """
                    UPDATE voice_enrollment_operations
                    SET state = 'completed', updated_at = ? WHERE operation_id = ?
                    """,
                    (now, str(operation["operation_id"])),
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

    def _operation(self, *, account_id: str, enrollment_key: str) -> sqlite3.Row:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM voice_enrollment_operations
                WHERE account_id = ? AND enrollment_key = ?
                """,
                (account_id, enrollment_key),
            ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(enrollment_key)
        return cast(sqlite3.Row, row)

    def _set_operation_state(
        self,
        operation_id: str,
        *,
        state: VoiceEnrollmentOperationState,
        last_error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE voice_enrollment_operations
                SET state = ?, last_error = ?, updated_at = ? WHERE operation_id = ?
                """,
                (state, last_error, datetime.now(UTC).isoformat(), operation_id),
            )

    async def provider_sample(self, *, sample_id: str) -> ProviderSample:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sample.* FROM voice_samples AS sample
                JOIN voice_profiles AS profile ON profile.sample_id = sample.sample_id
                JOIN voice_clone_consents AS consent ON consent.account_id = sample.account_id
                WHERE sample.sample_id = ?
                  AND sample.deleted_at IS NULL
                  AND consent.revoked_at IS NULL
                  AND profile.status IN ('enrolling', 'candidate', 'active')
                """,
                (sample_id,),
            ).fetchone()
        if row is None:
            raise VoiceConsentRequiredError("voice sample is unavailable")
        reference = self._reference(row)
        return ProviderSample(
            data=await self._object_store.get(reference),
            media_type=str(row["media_type"]),
        )

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
        status = "passed" if passed else "failed"
        evaluation_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        with self._connect() as connection:
            self._require_consent(connection, request.account_id)
            row = connection.execute(
                "SELECT status FROM voice_profiles WHERE profile_id = ? AND account_id = ?",
                (request.profile_id, request.account_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(request.profile_id)
            if row["status"] not in {"candidate", "active"}:
                raise EvaluationRequiredError("only a usable candidate can be evaluated")
            connection.execute(
                """
                INSERT INTO voice_evaluations (
                    evaluation_id, account_id, profile_id, similarity,
                    naturalness, accent_similarity, emotion_adherence,
                    instruction_adherence, uncanny, candidate_preferred,
                    first_audio_ms, cancel_tail_ms, timestamp_error_ms,
                    notes, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    request.account_id,
                    request.profile_id,
                    request.similarity,
                    request.naturalness,
                    request.accent_similarity,
                    request.emotion_adherence,
                    request.instruction_adherence,
                    request.uncanny,
                    (
                        None
                        if request.candidate_preferred is None
                        else int(request.candidate_preferred)
                    ),
                    request.notes,
                    status,
                    now.isoformat(),
                ),
            )
            connection.execute(
                "UPDATE voice_profiles SET evaluation_status = ?, updated_at = ? WHERE profile_id = ?",
                (status, now.isoformat(), request.profile_id),
            )
        return VoiceEvaluation(evaluation_id, request.profile_id, status, now)  # type: ignore[arg-type]

    async def create_blind_trial(
        self,
        *,
        account_id: str,
        profile_id: str,
    ) -> VoiceBlindTrial:
        await self.preview_target(account_id=account_id, profile_id=profile_id)
        trial_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        candidate_slot: VoiceBlindSlot = secrets.choice(("A", "B"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO voice_blind_trials (
                    trial_id, account_id, profile_id, candidate_slot, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (trial_id, account_id, profile_id, candidate_slot, now.isoformat()),
            )
        return VoiceBlindTrial(
            trial_id=trial_id,
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
        normalized_text = text.strip()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT trial.*, profile.target_model, profile.provider_voice_id,
                       profile.status, profile.provider_expires_at
                FROM voice_blind_trials AS trial
                JOIN voice_profiles AS profile ON profile.profile_id = trial.profile_id
                JOIN voice_clone_consents AS consent ON consent.account_id = trial.account_id
                WHERE trial.trial_id = ? AND trial.account_id = ?
                  AND consent.revoked_at IS NULL
                """,
                (trial_id, account_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(trial_id)
            if row["status"] not in {"candidate", "active"} or not row["provider_voice_id"]:
                raise EvaluationRequiredError("blind trial candidate is unavailable")
            expires_at = (
                datetime.fromisoformat(str(row["provider_expires_at"]))
                if row["provider_expires_at"] is not None
                else None
            )
            if expires_at is not None and expires_at <= datetime.now(UTC):
                raise EvaluationRequiredError("blind trial candidate has expired")
            if row["preview_text"] is not None and str(row["preview_text"]) != normalized_text:
                raise EvaluationRequiredError("both blind trial slots must use the same text")
            preview_column = "previewed_a" if slot == "A" else "previewed_b"
            connection.execute(
                f"""
                UPDATE voice_blind_trials
                SET preview_text = COALESCE(preview_text, ?), {preview_column} = 1
                WHERE trial_id = ? AND account_id = ?
                """,
                (normalized_text, trial_id, account_id),
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
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT candidate_slot, previewed_a, previewed_b
                FROM voice_blind_trials
                WHERE trial_id = ? AND account_id = ? AND profile_id = ?
                """,
                (trial_id, account_id, profile_id),
            ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(trial_id)
        if not bool(row["previewed_a"]) or not bool(row["previewed_b"]):
            raise EvaluationRequiredError("both blind trial slots must be previewed")
        return str(row["candidate_slot"]) == preferred_slot

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
        measurement_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"memoria:voice-quality:{request.account_id}:{request.source_run_id}",
            )
        )
        now = datetime.now(UTC)
        with self._connect() as connection:
            self._require_consent(connection, request.account_id)
            profile = connection.execute(
                """
                SELECT status FROM voice_profiles
                WHERE profile_id = ? AND account_id = ?
                """,
                (request.profile_id, request.account_id),
            ).fetchone()
            if profile is None:
                raise EvidenceNotFoundError(request.profile_id)
            if profile["status"] not in {"candidate", "active"}:
                raise EvaluationRequiredError(
                    "only a usable candidate can receive a quality measurement"
                )
            connection.execute(
                """
                INSERT INTO voice_quality_measurements (
                    measurement_id, account_id, profile_id, source_run_id,
                    first_audio_ms, cancel_tail_ms, timestamp_error_ms,
                    long_sentence_chars, long_sentence_completion_ratio,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, source_run_id) DO NOTHING
                """,
                (
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
                    now.isoformat(),
                ),
            )
            stored = connection.execute(
                """
                SELECT * FROM voice_quality_measurements
                WHERE account_id = ? AND source_run_id = ?
                """,
                (request.account_id, request.source_run_id),
            ).fetchone()
            assert stored is not None
            if str(stored["profile_id"]) != request.profile_id:
                raise ValueError("quality source_run_id belongs to another voice profile")
            connection.execute(
                """
                UPDATE voice_profiles SET quality_status = ?, updated_at = ?
                WHERE profile_id = ? AND account_id = ?
                """,
                (
                    str(stored["status"]),
                    now.isoformat(),
                    request.profile_id,
                    request.account_id,
                ),
            )
        return VoiceQualityMeasurement(
            measurement_id=str(stored["measurement_id"]),
            profile_id=request.profile_id,
            source_run_id=request.source_run_id,
            status=cast(Literal["passed", "failed"], stored["status"]),
            created_at=datetime.fromisoformat(str(stored["created_at"])),
        )

    async def account_id_for_profile(self, *, profile_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT account_id FROM voice_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(profile_id)
        return str(row["account_id"])

    async def activate(self, *, account_id: str, profile_id: str) -> VoiceProfile:
        now = datetime.now(UTC)
        with self._connect() as connection:
            self._require_consent(connection, account_id)
            row = connection.execute(
                "SELECT * FROM voice_profiles WHERE profile_id = ? AND account_id = ?",
                (profile_id, account_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(profile_id)
            if row["status"] not in {"candidate", "active"} or row["evaluation_status"] != "passed":
                raise EvaluationRequiredError("a passed candidate evaluation is required")
            if row["quality_status"] != "passed":
                raise EvaluationRequiredError("a passed provider quality measurement is required")
            if row["provider"] == "volcengine_doubao":
                expires_at = (
                    datetime.fromisoformat(str(row["provider_expires_at"]))
                    if row["provider_expires_at"] is not None
                    else None
                )
                if (
                    not row["provider_voice_id"]
                    or expires_at is None
                    or expires_at.utcoffset() is None
                    or expires_at <= now
                ):
                    raise EvaluationRequiredError("an unexpired Doubao provider voice is required")
            event = self._event(
                account_id,
                "voice_profile.activated",
                {"profile_id": profile_id, "version_number": int(row["version_number"])},
            )
            self._insert_evidence(connection, event)
            connection.execute(
                "UPDATE voice_profiles SET status = 'candidate', activated_at = NULL WHERE account_id = ? AND status = 'active'",
                (account_id,),
            )
            connection.execute(
                """
                UPDATE voice_profiles
                SET status = 'active', activated_at = ?, updated_at = ?
                WHERE profile_id = ? AND account_id = ?
                """,
                (now.isoformat(), now.isoformat(), profile_id, account_id),
            )
            updated = connection.execute(
                "SELECT * FROM voice_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        assert updated is not None
        return self._profile(updated)

    async def resolve(self, *, account_id: str) -> VoiceResolution:
        with self._connect() as connection:
            consent = connection.execute(
                "SELECT 1 FROM voice_clone_consents WHERE account_id = ? AND revoked_at IS NULL",
                (account_id,),
            ).fetchone()
            row = connection.execute(
                """
                SELECT * FROM voice_profiles
                WHERE account_id = ? AND status = 'active'
                  AND evaluation_status = 'passed'
                  AND quality_status = 'passed'
                """,
                (account_id,),
            ).fetchone()
        if consent is None or row is None or row["provider_voice_id"] is None:
            return VoiceResolution(mode="fallback")
        expires = (
            datetime.fromisoformat(str(row["provider_expires_at"]))
            if row["provider_expires_at"] is not None
            else None
        )
        if row["provider"] == "volcengine_doubao" and expires is None:
            return VoiceResolution(mode="fallback")
        if expires is not None and expires <= datetime.now(UTC):
            return VoiceResolution(mode="fallback")
        return VoiceResolution(
            mode="active",
            profile_id=str(row["profile_id"]),
            version_number=int(row["version_number"]),
            provider=str(row["provider"]),
            voice_kind="personal",
            model=str(row["target_model"]),
            resource_id=str(row["target_model"]),
            voice_id=str(row["provider_voice_id"]),
            provider_expires_at=expires,
        )

    async def profiles(self, *, account_id: str) -> tuple[VoiceProfile, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM voice_profiles WHERE account_id = ? ORDER BY version_number DESC",
                (account_id,),
            ).fetchall()
            operations = connection.execute(
                """
                SELECT operation.* FROM voice_enrollment_operations AS operation
                LEFT JOIN voice_profiles AS profile
                  ON profile.profile_id = operation.profile_id
                WHERE operation.account_id = ? AND profile.profile_id IS NULL
                  AND operation.state NOT IN ('completed', 'revoked')
                ORDER BY operation.version_number DESC
                """,
                (account_id,),
            ).fetchall()
        combined = [self._profile(row) for row in rows]
        combined.extend(self._synthetic_profile(operation) for operation in operations)
        return tuple(sorted(combined, key=lambda profile: profile.version_number, reverse=True))

    async def pending_enrollments(
        self,
        *,
        account_id: str,
    ) -> tuple[VoiceEnrollmentOperation, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM voice_enrollment_operations
                WHERE account_id = ? AND state NOT IN ('completed', 'revoked')
                ORDER BY version_number
                """,
                (account_id,),
            ).fetchall()
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
        if (
            sum(
                (
                    provider_voice is not None,
                    provider_asset_absent,
                    sample_asset_absent,
                )
            )
            != 1
        ):
            raise ValueError("exactly one enrollment reconciliation outcome is required")
        operation = self._operation(
            account_id=account_id,
            enrollment_key=enrollment_key.strip(),
        )
        if provider_voice is not None:
            if provider_voice.target_model != str(operation["target_model"]):
                raise ValueError("reconciled provider voice has an unexpected target model")
            self._persist_provider_voice(str(operation["operation_id"]), provider_voice)
            refreshed = self._operation(
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
            self._set_operation_state(
                str(operation["operation_id"]),
                state="sample_uploaded",
            )
        else:
            if (
                str(operation["state"])
                not in {
                    "upload_submitted",
                    "reconciliation_required",
                }
                or self._profile_row(str(operation["profile_id"])) is not None
            ):
                raise ValueError("enrollment has no ambiguous sample upload")
            self._set_operation_state(
                str(operation["operation_id"]),
                state="intent",
            )
        refreshed = self._operation(
            account_id=account_id,
            enrollment_key=enrollment_key.strip(),
        )
        return self._profile_for_operation(refreshed)

    async def preview_target(
        self,
        *,
        account_id: str,
        profile_id: str,
    ) -> VoicePreviewTarget:
        with self._connect() as connection:
            self._require_consent(connection, account_id)
            row = connection.execute(
                """
                SELECT * FROM voice_profiles
                WHERE profile_id = ? AND account_id = ?
                """,
                (profile_id, account_id),
            ).fetchone()
        if row is None:
            raise EvidenceNotFoundError(profile_id)
        if row["status"] not in {"candidate", "active"} or row["provider_voice_id"] is None:
            raise EvaluationRequiredError("only a usable voice profile can be previewed")
        expires = (
            datetime.fromisoformat(str(row["provider_expires_at"]))
            if row["provider_expires_at"] is not None
            else None
        )
        if expires is not None and expires <= datetime.now(UTC):
            raise EvaluationRequiredError("expired voice profile cannot be previewed")
        return VoicePreviewTarget(
            model=str(row["target_model"]),
            voice_id=str(row["provider_voice_id"]),
        )

    async def revoke_profile(self, *, account_id: str, profile_id: str) -> VoiceProfile:
        now = datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
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
                WHERE profile.profile_id = ? AND profile.account_id = ?
                """,
                (profile_id, account_id),
            ).fetchone()
            if row is None:
                operation = connection.execute(
                    """
                    SELECT * FROM voice_enrollment_operations
                    WHERE profile_id = ? AND account_id = ?
                    """,
                    (profile_id, account_id),
                ).fetchone()
                if operation is None:
                    raise EvidenceNotFoundError(profile_id)
                if operation["state"] == "intent":
                    connection.execute(
                        """
                        UPDATE voice_enrollment_operations
                        SET state = 'revoked', updated_at = ? WHERE operation_id = ?
                        """,
                        (now.isoformat(), str(operation["operation_id"])),
                    )
                    refreshed = connection.execute(
                        "SELECT * FROM voice_enrollment_operations WHERE operation_id = ?",
                        (str(operation["operation_id"]),),
                    ).fetchone()
                    assert refreshed is not None
                    return self._synthetic_profile(refreshed)
                connection.execute(
                    """
                    UPDATE voice_enrollment_operations
                    SET state = 'reconciliation_required',
                        last_error = 'account_deletion_requires_asset_reconciliation',
                        updated_at = ? WHERE operation_id = ?
                    """,
                    (now.isoformat(), str(operation["operation_id"])),
                )
                refreshed = connection.execute(
                    "SELECT * FROM voice_enrollment_operations WHERE operation_id = ?",
                    (str(operation["operation_id"]),),
                ).fetchone()
                assert refreshed is not None
                return self._synthetic_profile(refreshed)
            if row["status"] == "revoked" and row["deletion_status"] in {"completed", "pending"}:
                return self._profile(row)
            if row["status"] != "revoked":
                event = self._event(
                    account_id,
                    "voice_profile.revoked",
                    {"profile_id": profile_id},
                )
                self._insert_evidence(connection, event)
                connection.execute(
                    """
                    UPDATE voice_profiles
                    SET status = 'revoked', deletion_status = 'pending',
                        revoked_at = ?, updated_at = ?
                    WHERE profile_id = ?
                    """,
                    (now.isoformat(), now.isoformat(), profile_id),
                )
            reference = self._reference(row)
            voice_id = (
                str(row["provider_voice_id"] or row["enrollment_provider_voice_id"])
                if row["provider_voice_id"] or row["enrollment_provider_voice_id"]
                else None
            )
            provider_result_ambiguous = (
                row["enrollment_state"] in {"provider_submitted", "reconciliation_required"}
                and voice_id is None
            )
        await self._object_store.delete(reference)
        deletion_status: VoiceDeletionStatus = (
            "failed" if provider_result_ambiguous else "completed"
        )
        cleanup_error: str | None = None
        try:
            if voice_id is not None:
                await self._provider.delete_voice(voice_id=voice_id)
        except ProviderVoiceDeletionUnsupportedError:
            deletion_status = "pending"
            cleanup_error = "provider_deletion_unsupported_manual_cleanup"
        except Exception:
            deletion_status = "failed"
            cleanup_error = "provider_asset_deletion_incomplete"
        completed_at = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                "UPDATE voice_samples SET deleted_at = COALESCE(deleted_at, ?) WHERE sample_id = ?",
                (completed_at.isoformat(), str(row["sample_id"])),
            )
            connection.execute(
                """
                UPDATE voice_profiles
                SET provider_voice_id = COALESCE(provider_voice_id, ?),
                    deletion_status = ?, provider_deleted_at = ?, updated_at = ?
                WHERE profile_id = ?
                """,
                (
                    voice_id,
                    deletion_status,
                    completed_at.isoformat() if deletion_status == "completed" else None,
                    completed_at.isoformat(),
                    profile_id,
                ),
            )
            if row["enrollment_operation_id"] is not None:
                connection.execute(
                    """
                    UPDATE voice_enrollment_operations
                    SET state = ?, last_error = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (
                        "revoked"
                        if deletion_status in {"completed", "pending"}
                        else "reconciliation_required",
                        cleanup_error,
                        completed_at.isoformat(),
                        str(row["enrollment_operation_id"]),
                    ),
                )
            updated = connection.execute(
                "SELECT * FROM voice_profiles WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        assert updated is not None
        return self._profile(updated)

    async def confirm_provider_deletion(
        self,
        *,
        account_id: str,
        profile_id: str,
        evidence_reference: str,
    ) -> VoiceProfile:
        """Record an operator-confirmed provider deletion without storing raw voice IDs."""

        reference = evidence_reference.strip()
        if not reference:
            raise ValueError("provider deletion evidence reference is required")
        now = datetime.now(UTC)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM voice_profiles
                WHERE profile_id = ? AND account_id = ?
                """,
                (profile_id, account_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(profile_id)
            if row["status"] != "revoked":
                raise EvaluationRequiredError(
                    "only a revoked voice profile can confirm provider deletion"
                )
            if row["deletion_status"] == "completed":
                return self._profile(row)
            if row["deletion_status"] not in {"pending", "failed"}:
                raise EvaluationRequiredError(
                    "provider deletion must be pending or failed before confirmation"
                )
            provider_voice_id = (
                str(row["provider_voice_id"]) if row["provider_voice_id"] is not None else ""
            )
            event = self._event(
                account_id,
                "voice_profile.provider_deletion_confirmed",
                {
                    "profile_id": profile_id,
                    "provider": str(row["provider"]),
                    "provider_voice_sha256": (
                        hashlib.sha256(provider_voice_id.encode("utf-8")).hexdigest()
                        if provider_voice_id
                        else None
                    ),
                    "evidence_reference": reference,
                },
            )
            self._insert_evidence(connection, event)
            connection.execute(
                """
                UPDATE voice_profiles
                SET deletion_status = 'completed', provider_deleted_at = ?, updated_at = ?
                WHERE profile_id = ? AND account_id = ?
                """,
                (now.isoformat(), now.isoformat(), profile_id, account_id),
            )
            connection.execute(
                """
                UPDATE voice_enrollment_operations
                SET state = 'revoked', last_error = NULL, updated_at = ?
                WHERE profile_id = ? AND account_id = ?
                """,
                (now.isoformat(), profile_id, account_id),
            )
            updated = connection.execute(
                "SELECT * FROM voice_profiles WHERE profile_id = ? AND account_id = ?",
                (profile_id, account_id),
            ).fetchone()
        assert updated is not None
        return self._profile(updated)

    @staticmethod
    def _require_consent(connection: sqlite3.Connection, account_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM voice_clone_consents WHERE account_id = ? AND revoked_at IS NULL",
            (account_id,),
        ).fetchone()
        if row is None:
            raise VoiceConsentRequiredError("active voice-clone consent is required")

    @staticmethod
    def _reference(row: sqlite3.Row) -> ObjectRef:
        return ObjectRef(
            account_id=str(row["account_id"]),
            object_key=str(row["object_key"]),
            media_type=str(row["media_type"]),
            byte_count=int(row["byte_count"]),
            content_sha256=str(row["content_sha256"]),
            encryption_key_version=str(row["encryption_key_version"]),
            backend=str(row["object_backend"]),
        )

    def _profile_row(self, profile_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM voice_profiles WHERE profile_id = ?",
                    (profile_id,),
                ).fetchone(),
            )

    def _profile_for_operation(self, operation: sqlite3.Row) -> VoiceProfile:
        row = self._profile_row(str(operation["profile_id"]))
        return self._profile(row) if row is not None else self._synthetic_profile(operation)

    @staticmethod
    def _synthetic_profile(operation: sqlite3.Row) -> VoiceProfile:
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
            provider_expires_at=(
                datetime.fromisoformat(str(operation["provider_expires_at"]))
                if operation["provider_expires_at"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(operation["created_at"])),
            revoked_at=(
                datetime.fromisoformat(str(operation["updated_at"])) if state == "revoked" else None
            ),
        )

    @staticmethod
    def _enrollment_operation(row: sqlite3.Row) -> VoiceEnrollmentOperation:
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
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _profile(row: sqlite3.Row) -> VoiceProfile:
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
                Literal["pending", "passed", "failed"], row["evaluation_status"]
            ),
            quality_status=cast(Literal["pending", "passed", "failed"], row["quality_status"]),
            deletion_status=cast(VoiceDeletionStatus, row["deletion_status"]),
            provider_expires_at=(
                datetime.fromisoformat(str(row["provider_expires_at"]))
                if row["provider_expires_at"] is not None
                else None
            ),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            activated_at=(
                datetime.fromisoformat(str(row["activated_at"]))
                if row["activated_at"] is not None
                else None
            ),
            revoked_at=(
                datetime.fromisoformat(str(row["revoked_at"]))
                if row["revoked_at"] is not None
                else None
            ),
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
    def _insert_evidence(connection: sqlite3.Connection, event: EvidenceEvent) -> None:
        recorded_at = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO evidence_events (
                event_id, account_id, session_id, turn_id, generation_id,
                event_type, schema_version, occurred_at, recorded_at,
                speaker_identity_id, speaker_class, source, consent_grant_id,
                payload_json, content_sha256, supersedes_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.account_id,
                event.session_id,
                event.turn_id,
                event.generation_id,
                event.event_type,
                event.schema_version,
                event.occurred_at.isoformat(),
                recorded_at,
                event.speaker_identity_id,
                event.speaker_class,
                event.source,
                event.consent_grant_id,
                canonical_payload(event.payload),
                event.content_sha256,
                event.supersedes_event_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO processing_outbox (
                outbox_id, account_id, event_id, task_type, available_at, created_at
            ) VALUES (?, ?, ?, 'compile_evidence', ?, ?)
            """,
            (
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:outbox:{event.event_id}")),
                event.account_id,
                event.event_id,
                recorded_at,
                recorded_at,
            ),
        )
