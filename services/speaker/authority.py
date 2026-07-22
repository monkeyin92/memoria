"""Encrypted SQLite implementation of the SpeakerAuthority seam."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from services.speaker.domain import (
    EnrollmentQualityError,
    EnrollmentRequest,
    EnrollmentResult,
    RevokeSpeakerProfile,
    SpeakerDecision,
    SpeakerEmbeddingAdapter,
    SpeakerEvaluation,
    SpeakerProfileNotFoundError,
    SpeakerProfileSummary,
    SpeakerSample,
    permissions_for_speaker,
)
from services.speaker.policy import (
    classification_quality_reason,
    cosine_similarity,
    embedding_quality_reason,
    normalize_embedding,
    require_enrollment_quality,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS speaker_identities (
    identity_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    identity_type TEXT NOT NULL CHECK (identity_type IN ('owner', 'guest')),
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, identity_type, label)
);

CREATE TABLE IF NOT EXISTS speaker_profiles (
    profile_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    template_version INTEGER NOT NULL,
    template_ciphertext BLOB,
    owner_threshold REAL NOT NULL,
    guest_threshold REAL NOT NULL,
    consent_grant_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('shadow', 'active', 'revoked')),
    evaluation_ref TEXT,
    evaluation_sample_count INTEGER,
    far REAL,
    frr REAL,
    eer REAL,
    unknown_rejection REAL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    FOREIGN KEY (identity_id) REFERENCES speaker_identities(identity_id),
    UNIQUE (account_id, template_version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_speaker_one_active_owner
ON speaker_profiles(account_id)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS speaker_enrollment_samples (
    sample_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    speech_ms INTEGER NOT NULL,
    snr_db REAL NOT NULL,
    quality_score REAL NOT NULL,
    replay_risk REAL NOT NULL,
    synthetic_risk REAL NOT NULL DEFAULT 1.0,
    risk_assessment TEXT NOT NULL DEFAULT 'unavailable'
        CHECK (risk_assessment IN ('verified', 'unavailable')),
    device TEXT NOT NULL,
    scene TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (profile_id) REFERENCES speaker_profiles(profile_id) ON DELETE CASCADE
);
"""


class SpeakerAuthority:
    def __init__(
        self,
        path: Path,
        *,
        template_key: str,
        adapter: SpeakerEmbeddingAdapter,
        owner_threshold: float,
        guest_threshold: float,
        classify_timeout_s: float = 1.5,
    ) -> None:
        if not 0 <= guest_threshold < owner_threshold <= 1:
            raise ValueError("speaker thresholds must satisfy guest < owner")
        self._path = path.expanduser().resolve()
        self._fernet = Fernet(template_key.encode("ascii"))
        self._adapter = adapter
        self._owner_threshold = owner_threshold
        self._guest_threshold = guest_threshold
        self._classify_timeout_s = classify_timeout_s
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        template_key: str,
        adapter: SpeakerEmbeddingAdapter,
        owner_threshold: float = 0.78,
        guest_threshold: float = 0.45,
        classify_timeout_s: float = 1.5,
    ) -> SpeakerAuthority:
        return cls(
            Path(path),
            template_key=template_key,
            adapter=adapter,
            owner_threshold=owner_threshold,
            guest_threshold=guest_threshold,
            classify_timeout_s=classify_timeout_s,
        )

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._path, timeout=5) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(_SCHEMA)
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(speaker_profiles)")
                }
                for name, sql_type in (
                    ("evaluation_sample_count", "INTEGER"),
                    ("far", "REAL"),
                    ("frr", "REAL"),
                    ("eer", "REAL"),
                    ("unknown_rejection", "REAL"),
                ):
                    if name not in columns:
                        connection.execute(
                            f"ALTER TABLE speaker_profiles ADD COLUMN {name} {sql_type}"
                        )
                sample_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(speaker_enrollment_samples)")
                }
                if "synthetic_risk" not in sample_columns:
                    connection.execute(
                        "ALTER TABLE speaker_enrollment_samples "
                        "ADD COLUMN synthetic_risk REAL NOT NULL DEFAULT 1.0"
                    )
                if "risk_assessment" not in sample_columns:
                    connection.execute(
                        "ALTER TABLE speaker_enrollment_samples ADD COLUMN "
                        "risk_assessment TEXT NOT NULL DEFAULT 'unavailable'"
                    )
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    async def enroll(self, request: EnrollmentRequest) -> EnrollmentResult:
        embedded = [
            await self._adapter.embed(sample.pcm, sample_rate=sample.sample_rate)
            for sample in request.samples
        ]
        for result in embedded:
            require_enrollment_quality(result)
        dimensions = {len(result.vector) for result in embedded}
        if len(dimensions) != 1:
            raise EnrollmentQualityError("speaker embeddings have inconsistent dimensions")
        template = normalize_embedding(
            tuple(
                sum(result.vector[index] for result in embedded) / len(embedded)
                for index in range(len(embedded[0].vector))
            )
        )
        now = datetime.now(UTC).isoformat()
        profile_id = str(uuid.uuid4())
        identity_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:speaker-owner:{request.account_id}")
        )
        ciphertext = self._fernet.encrypt(json.dumps(template, separators=(",", ":")).encode())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO speaker_identities (
                    identity_id, account_id, identity_type, label, created_at
                ) VALUES (?, ?, 'owner', '账户主人', ?)
                """,
                (identity_id, request.account_id, now),
            )
            row = connection.execute(
                "SELECT COALESCE(MAX(template_version), 0) + 1 FROM speaker_profiles "
                "WHERE account_id = ?",
                (request.account_id,),
            ).fetchone()
            template_version = int(row[0])
            connection.execute(
                """
                INSERT INTO speaker_profiles (
                    profile_id, account_id, identity_id, model_version,
                    template_version, template_ciphertext, owner_threshold,
                    guest_threshold, consent_grant_id, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'shadow', ?)
                """,
                (
                    profile_id,
                    request.account_id,
                    identity_id,
                    self._adapter.model_version,
                    template_version,
                    ciphertext,
                    self._owner_threshold,
                    self._guest_threshold,
                    request.consent_grant_id,
                    now,
                ),
            )
            for sample, result in zip(request.samples, embedded, strict=True):
                connection.execute(
                    """
                    INSERT INTO speaker_enrollment_samples (
                        sample_id, profile_id, account_id, content_sha256,
                        speech_ms, snr_db, quality_score, replay_risk,
                        synthetic_risk, risk_assessment, device, scene, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        profile_id,
                        request.account_id,
                        hashlib.sha256(sample.pcm).hexdigest(),
                        result.speech_ms,
                        result.snr_db,
                        result.quality_score,
                        result.replay_risk,
                        result.synthetic_risk,
                        result.risk_assessment,
                        sample.device,
                        sample.scene,
                        now,
                    ),
                )
        return EnrollmentResult(
            profile_id=profile_id,
            identity_id=identity_id,
            template_version=template_version,
            model_version=self._adapter.model_version,
            sample_count=len(request.samples),
            status="shadow",
            consent_grant_id=request.consent_grant_id,
        )

    async def activate(
        self,
        profile_id: str,
        *,
        account_id: str,
        evaluation: SpeakerEvaluation,
    ) -> None:
        if not account_id.strip():
            raise ValueError("account_id must not be blank")
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT account_id, status FROM speaker_profiles "
                "WHERE profile_id = ? AND account_id = ?",
                (profile_id, account_id),
            ).fetchone()
            if row is None:
                raise SpeakerProfileNotFoundError(profile_id)
            if row["status"] == "revoked":
                raise ValueError("revoked speaker profile cannot be activated")
            unavailable_risk_count = int(
                connection.execute(
                    "SELECT count(*) FROM speaker_enrollment_samples "
                    "WHERE profile_id = ? AND risk_assessment != 'verified'",
                    (profile_id,),
                ).fetchone()[0]
            )
            if unavailable_risk_count:
                raise ValueError("speaker profile anti-spoof assessment is unavailable")
            connection.execute(
                "UPDATE speaker_profiles SET status = 'shadow', activated_at = NULL "
                "WHERE account_id = ? AND status = 'active'",
                (row["account_id"],),
            )
            connection.execute(
                """
                UPDATE speaker_profiles
                SET status = 'active', evaluation_ref = ?, evaluation_sample_count = ?,
                    far = ?, frr = ?, eer = ?, unknown_rejection = ?, activated_at = ?
                WHERE profile_id = ?
                """,
                (
                    evaluation.report_ref,
                    evaluation.sample_count,
                    evaluation.far,
                    evaluation.frr,
                    evaluation.eer,
                    evaluation.unknown_rejection,
                    now,
                    profile_id,
                ),
            )

    async def classify(self, sample: SpeakerSample) -> SpeakerDecision:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM speaker_profiles
                WHERE account_id = ? AND status IN ('active', 'shadow')
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                         template_version DESC
                LIMIT 1
                """,
                (sample.account_id,),
            ).fetchone()
        if row is None:
            return self._uncertain("no_active_profile")
        try:
            result = await asyncio.wait_for(
                self._adapter.embed(sample.pcm, sample_rate=sample.sample_rate),
                timeout=self._classify_timeout_s,
            )
        except TimeoutError:
            return self._uncertain("model_timeout", row=row)
        except Exception:
            return self._uncertain("model_unavailable", row=row)
        ciphertext = row["template_ciphertext"]
        if ciphertext is None:
            return self._uncertain("profile_revoked", row=row, quality=result.quality_score)
        try:
            template = tuple(json.loads(self._fernet.decrypt(bytes(ciphertext))))
        except (InvalidToken, TypeError, json.JSONDecodeError):
            return self._uncertain("template_unavailable", row=row, quality=result.quality_score)
        if len(result.vector) != len(template):
            return self._uncertain(
                "embedding_dimension_mismatch",
                row=row,
                quality=result.quality_score,
            )
        score = cosine_similarity(
            tuple(result.vector), tuple(float(value) for value in template)
        )
        if row["status"] == "shadow":
            # Shadow output never changes authority. Keep its candidate score even
            # when the short conversational sample fails authority-quality gates,
            # so the realtime target-speaker focus can reject a clear bystander.
            effective_guest_threshold = min(
                float(row["guest_threshold"]), self._guest_threshold
            )
            if score >= float(row["owner_threshold"]):
                reason = "shadow_owner_candidate"
            elif score <= effective_guest_threshold:
                reason = "shadow_guest_candidate"
            else:
                reason = "shadow_ambiguous_candidate"
            return self._uncertain(
                reason,
                row=row,
                quality=result.quality_score,
                score=score,
            )
        quality_reason = embedding_quality_reason(result)
        if quality_reason is not None:
            return self._uncertain(quality_reason, row=row, quality=result.quality_score)
        risk_reason = classification_quality_reason(result)
        if risk_reason is not None:
            return self._uncertain(
                risk_reason,
                row=row,
                quality=result.quality_score,
            )
        if score >= float(row["owner_threshold"]):
            classification = "owner"
            reason = "owner_match"
        elif score <= float(row["guest_threshold"]):
            classification = "guest"
            reason = "owner_mismatch"
        else:
            classification = "uncertain"
            reason = "ambiguous_score"
        return SpeakerDecision(
            classification=classification,  # type: ignore[arg-type]
            score=score,
            quality_score=result.quality_score,
            reason_code=reason,
            model_version=str(row["model_version"]),
            template_version=int(row["template_version"]),
            profile_id=str(row["profile_id"]),
            permissions=permissions_for_speaker(classification),  # type: ignore[arg-type]
        )

    async def profiles(self, account_id: str) -> tuple[SpeakerProfileSummary, ...]:
        if not account_id.strip():
            raise ValueError("account_id must not be blank")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.*, count(s.sample_id) AS sample_count
                FROM speaker_profiles p
                LEFT JOIN speaker_enrollment_samples s ON s.profile_id = p.profile_id
                WHERE p.account_id = ?
                GROUP BY p.profile_id
                ORDER BY p.template_version DESC
                """,
                (account_id,),
            ).fetchall()
        return tuple(
            SpeakerProfileSummary(
                profile_id=str(row["profile_id"]),
                identity_id=str(row["identity_id"]),
                template_version=int(row["template_version"]),
                model_version=str(row["model_version"]),
                sample_count=int(row["sample_count"]),
                status=row["status"],
                evaluation_ref=row["evaluation_ref"],
                evaluation_sample_count=row["evaluation_sample_count"],
                far=row["far"],
                frr=row["frr"],
                eer=row["eer"],
                unknown_rejection=row["unknown_rejection"],
                created_at=str(row["created_at"]),
                activated_at=row["activated_at"],
                revoked_at=row["revoked_at"],
            )
            for row in rows
        )

    async def revoke(self, request: RevokeSpeakerProfile) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE speaker_profiles
                SET status = 'revoked', template_ciphertext = NULL,
                    revoked_at = ?, revoke_reason = ?
                WHERE profile_id = ? AND account_id = ? AND status != 'revoked'
                """,
                (now, request.reason, request.profile_id, request.account_id),
            )
        if cursor.rowcount != 1:
            raise SpeakerProfileNotFoundError(request.profile_id)

    def _uncertain(
        self,
        reason: str,
        *,
        row: sqlite3.Row | None = None,
        quality: float = 0.0,
        score: float | None = None,
    ) -> SpeakerDecision:
        return SpeakerDecision(
            classification="uncertain",
            score=score,
            quality_score=quality,
            reason_code=reason,
            model_version=(
                str(row["model_version"]) if row is not None else self._adapter.model_version
            ),
            template_version=int(row["template_version"]) if row is not None else None,
            profile_id=str(row["profile_id"]) if row is not None else None,
            permissions=permissions_for_speaker("uncertain"),
        )
