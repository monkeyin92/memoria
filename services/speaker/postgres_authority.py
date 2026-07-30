"""PostgreSQL production adapter for the SpeakerAuthority public seam."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Any, cast

import asyncpg
from cryptography.fernet import Fernet, InvalidToken

from services.speaker.domain import (
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
    deserialize_embedding_template,
    embedding_quality_reason,
    require_enrollment_quality,
    serialize_embedding_template,
    template_similarity,
)


class PostgresSpeakerAuthority:
    def __init__(
        self,
        dsn: str,
        *,
        template_key: str,
        adapter: SpeakerEmbeddingAdapter,
        owner_threshold: float = 0.78,
        guest_threshold: float = 0.45,
        classify_timeout_s: float = 1.0,
    ) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("speaker DSN must use PostgreSQL")
        if not 0 <= guest_threshold < owner_threshold <= 1:
            raise ValueError("speaker thresholds must satisfy guest < owner")
        self._dsn = dsn
        self._fernet = Fernet(template_key.encode("ascii"))
        self._adapter = adapter
        self._owner_threshold = owner_threshold
        self._guest_threshold = guest_threshold
        self._classify_timeout_s = classify_timeout_s
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, command_timeout=10)
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL speaker pool")
        schema = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(schema)
        self._pool = pool

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL speaker authority is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    async def enroll(self, request: EnrollmentRequest) -> EnrollmentResult:
        embedded = [
            await self._adapter.embed(sample.pcm, sample_rate=sample.sample_rate)
            for sample in request.samples
        ]
        for result in embedded:
            require_enrollment_quality(result)
        template = serialize_embedding_template(result.vector for result in embedded)
        profile_id = uuid.uuid4()
        identity_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"memoria:speaker-owner:{request.account_id}",
        )
        ciphertext = self._fernet.encrypt(template)
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            await connection.execute(
                """
                INSERT INTO speaker_identities (
                    identity_id, account_id, identity_type, label
                ) VALUES ($1, $2, 'owner', '账户主人')
                ON CONFLICT (identity_id) DO NOTHING
                """,
                identity_id,
                request.account_id,
            )
            await connection.fetchrow(
                "SELECT identity_id FROM speaker_identities "
                "WHERE identity_id = $1 AND account_id = $2 FOR UPDATE",
                identity_id,
                request.account_id,
            )
            template_version = int(
                await connection.fetchval(
                    "SELECT COALESCE(MAX(template_version), 0) + 1 "
                    "FROM speaker_profiles WHERE account_id = $1",
                    request.account_id,
                )
            )
            await connection.execute(
                """
                INSERT INTO speaker_profiles (
                    profile_id, account_id, identity_id, model_version,
                    template_version, template_ciphertext, owner_threshold,
                    guest_threshold, consent_grant_id, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'shadow')
                """,
                profile_id,
                request.account_id,
                identity_id,
                self._adapter.model_version,
                template_version,
                ciphertext,
                self._owner_threshold,
                self._guest_threshold,
                request.consent_grant_id,
            )
            await connection.executemany(
                """
                INSERT INTO speaker_enrollment_samples (
                    sample_id, profile_id, account_id, content_sha256,
                    speech_ms, snr_db, quality_score, replay_risk,
                    synthetic_risk, risk_assessment, device, scene
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                [
                    (
                        uuid.uuid4(),
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
                    )
                    for sample, result in zip(request.samples, embedded, strict=True)
                ],
            )
        return EnrollmentResult(
            profile_id=str(profile_id),
            identity_id=str(identity_id),
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
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            row = await connection.fetchrow(
                "SELECT status FROM speaker_profiles "
                "WHERE profile_id = $1::uuid AND account_id = $2 FOR UPDATE",
                profile_id,
                account_id,
            )
            if row is None:
                raise SpeakerProfileNotFoundError(profile_id)
            if row["status"] == "revoked":
                raise ValueError("revoked speaker profile cannot be activated")
            unavailable_risk_count = int(
                await connection.fetchval(
                    "SELECT count(*) FROM speaker_enrollment_samples "
                    "WHERE profile_id = $1::uuid AND risk_assessment != 'verified'",
                    profile_id,
                )
            )
            if unavailable_risk_count:
                raise ValueError("speaker profile anti-spoof assessment is unavailable")
            await connection.execute(
                "UPDATE speaker_profiles SET status = 'shadow', activated_at = NULL "
                "WHERE account_id = $1 AND status = 'active'",
                account_id,
            )
            await connection.execute(
                "UPDATE speaker_profiles SET status = 'active', evaluation_ref = $1, "
                "evaluation_sample_count = $2, far = $3, frr = $4, eer = $5, "
                "unknown_rejection = $6, activated_at = now() "
                "WHERE profile_id = $7::uuid AND account_id = $8",
                evaluation.report_ref,
                evaluation.sample_count,
                evaluation.far,
                evaluation.frr,
                evaluation.eer,
                evaluation.unknown_rejection,
                profile_id,
                account_id,
            )

    async def classify(self, sample: SpeakerSample) -> SpeakerDecision:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, sample.account_id)
            row = await connection.fetchrow(
                "SELECT * FROM speaker_profiles "
                "WHERE account_id = $1 AND status IN ('active', 'shadow') "
                "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
                "template_version DESC LIMIT 1",
                sample.account_id,
            )
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
            prototypes = deserialize_embedding_template(self._fernet.decrypt(bytes(ciphertext)))
        except (InvalidToken, TypeError, ValueError):
            return self._uncertain("template_unavailable", row=row, quality=result.quality_score)
        if any(len(result.vector) != len(prototype) for prototype in prototypes):
            return self._uncertain(
                "embedding_dimension_mismatch",
                row=row,
                quality=result.quality_score,
            )
        score = template_similarity(tuple(result.vector), prototypes)
        if row["status"] == "shadow":
            # Shadow output never changes authority. Keep its candidate score even
            # when the short conversational sample fails authority-quality gates,
            # so the realtime target-speaker focus can reject a clear bystander.
            effective_guest_threshold = min(float(row["guest_threshold"]), self._guest_threshold)
            if score >= float(row["owner_threshold"]):
                reason_code = "shadow_owner_candidate"
            elif score <= effective_guest_threshold:
                reason_code = "shadow_guest_candidate"
            else:
                reason_code = "shadow_ambiguous_candidate"
            return self._uncertain(
                reason_code,
                row=row,
                quality=result.quality_score,
                score=score,
            )
        reason = embedding_quality_reason(result)
        if reason is not None:
            return self._uncertain(reason, row=row, quality=result.quality_score)
        risk_reason = classification_quality_reason(result)
        if risk_reason is not None:
            return self._uncertain(
                risk_reason,
                row=row,
                quality=result.quality_score,
            )
        if score >= float(row["owner_threshold"]):
            classification = "owner"
            reason_code = "owner_match"
        elif score <= float(row["guest_threshold"]):
            classification = "guest"
            reason_code = "owner_mismatch"
        else:
            classification = "uncertain"
            reason_code = "ambiguous_score"
        return SpeakerDecision(
            classification=cast(Any, classification),
            score=score,
            quality_score=result.quality_score,
            reason_code=reason_code,
            model_version=str(row["model_version"]),
            template_version=int(row["template_version"]),
            profile_id=str(row["profile_id"]),
            permissions=permissions_for_speaker(cast(Any, classification)),
        )

    async def profiles(self, account_id: str) -> tuple[SpeakerProfileSummary, ...]:
        if not account_id.strip():
            raise ValueError("account_id must not be blank")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT p.*, count(s.sample_id) AS sample_count
                FROM speaker_profiles p
                LEFT JOIN speaker_enrollment_samples s ON s.profile_id = p.profile_id
                WHERE p.account_id = $1
                GROUP BY p.profile_id
                ORDER BY p.template_version DESC
                """,
                account_id,
            )
        return tuple(
            SpeakerProfileSummary(
                profile_id=str(row["profile_id"]),
                identity_id=str(row["identity_id"]),
                template_version=int(row["template_version"]),
                model_version=str(row["model_version"]),
                sample_count=int(row["sample_count"]),
                status=cast(Any, row["status"]),
                evaluation_ref=row["evaluation_ref"],
                evaluation_sample_count=row["evaluation_sample_count"],
                far=row["far"],
                frr=row["frr"],
                eer=row["eer"],
                unknown_rejection=row["unknown_rejection"],
                created_at=row["created_at"].isoformat(),
                activated_at=(
                    row["activated_at"].isoformat() if row["activated_at"] is not None else None
                ),
                revoked_at=(
                    row["revoked_at"].isoformat() if row["revoked_at"] is not None else None
                ),
            )
            for row in rows
        )

    async def revoke(self, request: RevokeSpeakerProfile) -> None:
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            status = await connection.execute(
                """
                UPDATE speaker_profiles
                SET status = 'revoked', template_ciphertext = NULL,
                    revoked_at = now(), revoke_reason = $1
                WHERE profile_id = $2::uuid AND account_id = $3 AND status != 'revoked'
                """,
                request.reason,
                request.profile_id,
                request.account_id,
            )
        if status != "UPDATE 1":
            raise SpeakerProfileNotFoundError(request.profile_id)

    def _uncertain(
        self,
        reason: str,
        *,
        row: asyncpg.Record | None = None,
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

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
