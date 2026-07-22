"""PostgreSQL/RLS PersonaEngine implementation."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import asyncpg

from services.archive.domain import EvidenceEvent, EvidenceNotFoundError
from services.common.evidence_policy import contribution_for, prompt_weight_for
from services.persona.domain import (
    ObservationResult,
    PersonaCapsule,
    PersonaCapsuleEntry,
    PersonaConsent,
    PersonaEvidence,
    PersonaRequest,
    PersonaReview,
    PersonaTrait,
    PersonaTraitCategory,
    PersonaTraitStatus,
    PersonaVersion,
    require_persona_counterexample,
)
from services.persona.rules import (
    CATEGORY_ORDER,
    EXCLUSIVE_STYLE_CATEGORIES,
    TICS,
    ExclusiveBucketObservation,
    PersonaExtractor,
    RuleBasedPersonaExtractor,
    exclusive_auto_promote_target,
    safe_confirmed_style_description,
    should_auto_promote,
    trusted_uncertain_profile,
)

_CONTAMINATION = frozenset(
    {"assistant", "synthetic_audio", "guest", "echo", "overlap", "low_quality", "replay"}
)


def _stable_uuid(kind: str, *values: object) -> uuid.UUID:
    key = ":".join(str(value) for value in values)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{kind}:{key}")


class PostgresPersonaEngine:
    def __init__(self, dsn: str, *, extractor: PersonaExtractor | None = None) -> None:
        if not dsn.startswith(("postgresql://", "postgres://")):
            raise ValueError("persona DSN must use PostgreSQL")
        self._dsn = dsn
        self._extractor = extractor or RuleBasedPersonaExtractor()
        self._pool: asyncpg.Pool | None = None

    async def initialize(self) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=10, command_timeout=15)
        if pool is None:  # pragma: no cover
            raise RuntimeError("failed to create PostgreSQL persona pool")
        archive_schema = (Path(__file__).parents[1] / "archive" / "postgres_schema.sql").read_text(
            encoding="utf-8"
        )
        persona_schema = Path(__file__).with_name("postgres_schema.sql").read_text(encoding="utf-8")
        async with pool.acquire() as connection:
            await connection.execute(archive_schema)
            await connection.execute(persona_schema)
        self._pool = pool

    async def _ready_pool(self) -> asyncpg.Pool:
        await self.initialize()
        if self._pool is None:  # pragma: no cover
            raise RuntimeError("PostgreSQL persona engine is not initialized")
        return self._pool

    @staticmethod
    async def _scope(connection: asyncpg.Connection, account_id: str) -> None:
        await connection.execute("SELECT set_config('app.account_id', $1, true)", account_id)

    @staticmethod
    async def _lock_account(connection: asyncpg.Connection, account_id: str) -> None:
        """Serialize Persona mutations for one account inside the transaction."""

        await connection.fetchval(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            f"memoria-persona:{account_id}",
        )

    @staticmethod
    async def _duplicate_observation(
        connection: asyncpg.Connection,
        evidence: PersonaEvidence,
    ) -> ObservationResult | None:
        receipt = await connection.fetchval(
            """
            SELECT source_event_id FROM persona_observation_receipts
            WHERE source_event_id = $1 AND account_id = $2
            """,
            evidence.source_event_id,
            evidence.account_id,
        )
        if receipt is None:
            return None
        trait_rows = await connection.fetch(
            """
            SELECT trait_id FROM persona_evidence
            WHERE source_event_id = $1 AND account_id = $2
            ORDER BY trait_id
            """,
            evidence.source_event_id,
            evidence.account_id,
        )
        return ObservationResult(
            True,
            "duplicate_evidence",
            tuple(str(item["trait_id"]) for item in trait_rows),
        )

    @staticmethod
    async def _learning_allowed(connection: asyncpg.Connection, account_id: str) -> bool:
        return (
            await connection.fetchval(
                """
                SELECT 1 FROM persona_learning_consents
                WHERE account_id = $1 AND revoked_at IS NULL
                """,
                account_id,
            )
            is not None
        )

    async def observe(self, evidence: PersonaEvidence) -> ObservationResult:
        if not evidence.learning_allowed:
            return ObservationResult(False, "learning_not_authorized")
        if set(evidence.contamination_flags) & _CONTAMINATION:
            return ObservationResult(False, "contaminated_evidence")
        if evidence.quality_score is not None and evidence.quality_score < 0.5:
            return ObservationResult(False, "low_quality_evidence")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, evidence.account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM archive_evidence_events
                WHERE event_id = $1 AND account_id = $2
                """,
                evidence.source_event_id,
                evidence.account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(evidence.source_event_id)
            if str(row["event_type"]).startswith("assistant.") or row["speaker_class"] in {
                "assistant",
                "system",
            }:
                return ObservationResult(False, "assistant_or_synthetic_evidence")
            speaker_class = str(row["speaker_class"])
            if speaker_class not in {"owner", "uncertain"}:
                return ObservationResult(False, "speaker_not_owner")
            if row["event_type"] != "speech.utterance_finalized":
                return ObservationResult(False, "unsupported_evidence_type")
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            text = str(payload.get("text") or "").strip()
            if not text:
                return ObservationResult(False, "empty_evidence")
            if payload.get("persona_eligible") is not True:
                return ObservationResult(False, "persona_ineligible_turn")
            if speaker_class == "owner":
                contribution = contribution_for(
                    EvidenceEvent(
                        event_id=str(row["event_id"]),
                        account_id=evidence.account_id,
                        event_type=str(row["event_type"]),
                        occurred_at=cast(datetime, row["occurred_at"]),
                        speaker_class="owner",
                        source=str(row["source"]),
                        payload=payload,
                    )
                )
                if not contribution.accepted:
                    return ObservationResult(False, contribution.reason)
                prompt_weight = contribution.weight
                prompt_factor = contribution.factor
            else:
                prompt_weight, prompt_factor = prompt_weight_for(payload)
            uncertain_provenance = (
                trusted_uncertain_profile(payload) if speaker_class == "uncertain" else None
            )
            if speaker_class == "uncertain" and uncertain_provenance is None:
                return ObservationResult(False, "untrusted_uncertain_speaker")
            duplicate = await self._duplicate_observation(connection, evidence)
            if duplicate is not None:
                return duplicate
            occurred_at = row["occurred_at"]

        if speaker_class == "uncertain":
            assert uncertain_provenance is not None
            extraction_evidence = replace(
                evidence,
                speech_duration_ms=None,
                pause_ratio=None,
                quality_score=uncertain_provenance[1] * prompt_factor,
            )
        else:
            extraction_evidence = replace(
                evidence,
                quality_score=(evidence.quality_score or 1.0) * prompt_factor,
            )
        candidates = await self._extractor.extract(text, extraction_evidence)
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, evidence.account_id)
            await self._lock_account(connection, evidence.account_id)
            if not await self._learning_allowed(connection, evidence.account_id):
                return ObservationResult(False, "learning_not_authorized")
            duplicate = await self._duplicate_observation(connection, evidence)
            if duplicate is not None:
                return duplicate
            trait_ids: list[str] = []
            version_changed = False
            if speaker_class == "owner" and prompt_weight == "strong":
                tic_counts = {tic: text.count(tic) for tic in TICS if tic in text}
                await self._update_style_stats(connection, evidence, text, tic_counts)
            for candidate in candidates:
                trait_uuid = _stable_uuid(
                    "persona-trait",
                    evidence.account_id,
                    candidate.category,
                    candidate.normalized_key,
                )
                current = await connection.fetchrow(
                    """
                    SELECT status, EXISTS (
                        SELECT 1
                        FROM persona_evidence AS pe
                        JOIN archive_evidence_events AS ae
                          ON ae.event_id = pe.source_event_id
                         AND ae.account_id = pe.account_id
                        WHERE pe.trait_id = persona_traits.trait_id
                          AND ae.speaker_class = 'owner'
                    ) AS has_owner_evidence
                    FROM persona_traits
                    WHERE trait_id = $1 AND account_id = $2
                    """,
                    trait_uuid,
                    evidence.account_id,
                )
                if current is not None and str(current["status"]) == "disabled":
                    continue
                if (
                    speaker_class == "uncertain"
                    and current is not None
                    and (
                        str(current["status"]) != "candidate" or bool(current["has_owner_evidence"])
                    )
                ):
                    continue
                trait_ids.append(str(trait_uuid))
                await connection.execute(
                    """
                    INSERT INTO persona_traits (
                        trait_id, account_id, category, normalized_key,
                        description, context, counterexample, confidence
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, 0.55)
                    ON CONFLICT (account_id, category, normalized_key) DO NOTHING
                    """,
                    trait_uuid,
                    evidence.account_id,
                    candidate.category,
                    candidate.normalized_key,
                    candidate.description,
                    candidate.context,
                    candidate.counterexample,
                )
                inserted = await connection.fetchval(
                    """
                    INSERT INTO persona_evidence (
                        trait_id, account_id, source_event_id, scene, weight, occurred_at
                    ) VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT DO NOTHING
                    RETURNING 1
                    """,
                    trait_uuid,
                    evidence.account_id,
                    evidence.source_event_id,
                    evidence.scene,
                    extraction_evidence.quality_score
                    if extraction_evidence.quality_score is not None
                    else 1.0,
                    occurred_at,
                )
                if inserted:
                    counts = await connection.fetchrow(
                        """
                        SELECT
                            COUNT(*) AS total_count,
                            SUM(pe.weight) AS total_weight,
                            SUM(pe.weight) FILTER (
                                WHERE ae.speaker_class = 'owner'
                            ) AS owner_weight,
                            SUM(pe.weight) FILTER (
                                WHERE ae.speaker_class = 'uncertain'
                                  AND ae.payload @> '{
                                      "persona_eligible": true,
                                      "speaker_reason_code": "shadow_owner_candidate"
                                  }'::jsonb
                                  AND COALESCE(ae.payload->>'speaker_profile_id', '') = $2
                            )
                                AS uncertain_weight,
                            COUNT(DISTINCT ae.session_id)
                                FILTER (
                                    WHERE ae.speaker_class = 'uncertain'
                                      AND ae.payload @> '{
                                          "persona_eligible": true,
                                          "speaker_reason_code": "shadow_owner_candidate"
                                      }'::jsonb
                                      AND COALESCE(
                                          ae.payload->>'speaker_profile_id', ''
                                      ) = $2
                                ) AS uncertain_session_count,
                            COUNT(DISTINCT ae.payload->>'speaker_profile_id')
                                FILTER (
                                    WHERE ae.speaker_class = 'uncertain'
                                      AND ae.payload @> '{
                                          "persona_eligible": true,
                                          "speaker_reason_code": "shadow_owner_candidate"
                                      }'::jsonb
                                      AND COALESCE(
                                          ae.payload->>'speaker_profile_id', ''
                                      ) = $2
                                ) AS uncertain_profile_count
                        FROM persona_evidence AS pe
                        JOIN archive_evidence_events AS ae
                          ON ae.event_id = pe.source_event_id
                         AND ae.account_id = pe.account_id
                        WHERE pe.trait_id = $1
                        """,
                        trait_uuid,
                        uncertain_provenance[0] if uncertain_provenance is not None else "",
                    )
                    assert counts is not None
                    count = int(counts["total_count"])
                    total_weight = float(counts["total_weight"] or 0)
                    owner_weight = float(counts["owner_weight"] or 0)
                    uncertain_weight = float(counts["uncertain_weight"] or 0)
                    uncertain_session_count = int(counts["uncertain_session_count"])
                    uncertain_profile_count = int(counts["uncertain_profile_count"])
                    current = await connection.fetchrow(
                        "SELECT status FROM persona_traits WHERE trait_id = $1",
                        trait_uuid,
                    )
                    status = str(current["status"]) if current is not None else "candidate"
                    if candidate.category not in EXCLUSIVE_STYLE_CATEGORIES:
                        if should_auto_promote(
                            category=candidate.category,
                            status=status,
                            owner_weight=owner_weight,
                            uncertain_weight=uncertain_weight,
                            uncertain_session_count=uncertain_session_count,
                            uncertain_profile_count=uncertain_profile_count,
                        ):
                            status = "confirmed"
                            version_changed = (
                                version_changed
                                or current is None
                                or current["status"] != "confirmed"
                            )
                    await connection.execute(
                        """
                        UPDATE persona_traits
                        SET observation_count = $1, confidence = $2,
                            status = $3, updated_at = now()
                        WHERE trait_id = $4
                        """,
                        count,
                        min(0.95, 0.45 + 0.13 * total_weight),
                        status,
                        trait_uuid,
                    )
                    if candidate.category in EXCLUSIVE_STYLE_CATEGORIES:
                        version_changed = (
                            await self._reconcile_exclusive_category(
                                connection,
                                account_id=evidence.account_id,
                                category=candidate.category,
                                uncertain_profile_id=(
                                    uncertain_provenance[0]
                                    if uncertain_provenance is not None
                                    else None
                                ),
                            )
                            or version_changed
                        )
            await connection.execute(
                """
                INSERT INTO persona_observation_receipts (
                    source_event_id, account_id
                ) VALUES ($1, $2)
                """,
                evidence.source_event_id,
                evidence.account_id,
            )
            version = (
                await self._publish_version(
                    connection,
                    evidence.account_id,
                    reason="automatic_style_learning_v1",
                )
                if version_changed
                else None
            )
        return ObservationResult(
            accepted=True,
            reason="candidate_observed" if speaker_class == "uncertain" else "observed",
            candidate_trait_ids=tuple(trait_ids),
            published_version_id=version.version_id if version is not None else None,
        )

    @staticmethod
    async def _reconcile_exclusive_category(
        connection: asyncpg.Connection,
        *,
        account_id: str,
        category: PersonaTraitCategory,
        uncertain_profile_id: str | None = None,
    ) -> bool:
        rows = await connection.fetch(
            """
            SELECT
                pt.trait_id, pt.status, pt.review_event_id, pt.updated_at,
                ae.speaker_class, ae.session_id, ae.payload, pe.weight
            FROM persona_traits AS pt
            LEFT JOIN persona_evidence AS pe
              ON pe.trait_id = pt.trait_id
             AND pe.account_id = pt.account_id
            LEFT JOIN archive_evidence_events AS ae
              ON ae.event_id = pe.source_event_id
             AND ae.account_id = pe.account_id
            WHERE pt.account_id = $1 AND pt.category = $2
              AND pt.status <> 'disabled'
            ORDER BY pt.trait_id, pe.occurred_at, pe.source_event_id
            """,
            account_id,
            category,
        )
        observations: list[ExclusiveBucketObservation] = []
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            observations.append(
                ExclusiveBucketObservation(
                    trait_id=str(row["trait_id"]),
                    status=str(row["status"]),
                    review_event_id=(
                        str(row["review_event_id"]) if row["review_event_id"] is not None else None
                    ),
                    updated_at=str(row["updated_at"]),
                    speaker_class=(
                        str(row["speaker_class"]) if row["speaker_class"] is not None else None
                    ),
                    session_id=(str(row["session_id"]) if row["session_id"] is not None else None),
                    payload=payload or {},
                    weight=float(row["weight"] or 0),
                )
            )
        target = exclusive_auto_promote_target(
            category,
            tuple(observations),
            uncertain_profile_id=uncertain_profile_id,
        )
        changed = False
        seen: set[str] = set()
        for row in rows:
            trait_id = str(row["trait_id"])
            if trait_id in seen:
                continue
            seen.add(trait_id)
            status = str(row["status"])
            desired = "confirmed" if trait_id == target else "candidate"
            if status == desired:
                continue
            await connection.execute(
                """
                UPDATE persona_traits
                SET status = $1, updated_at = now()
                WHERE trait_id = $2 AND account_id = $3 AND status <> 'disabled'
                """,
                desired,
                uuid.UUID(trait_id),
                account_id,
            )
            changed = True
        return changed

    @staticmethod
    async def _update_style_stats(
        connection: asyncpg.Connection,
        evidence: PersonaEvidence,
        text: str,
        tic_counts: dict[str, int],
    ) -> None:
        current = await connection.fetchval(
            """
            SELECT tic_counts FROM speech_style_stats
            WHERE account_id = $1 AND scene = $2
            """,
            evidence.account_id,
            evidence.scene,
        )
        if isinstance(current, str):
            current = json.loads(current)
        merged = dict(current or {})
        for tic, count in tic_counts.items():
            merged[tic] = int(merged.get(tic, 0)) + count
        await connection.execute(
            """
            INSERT INTO speech_style_stats (
                account_id, scene, utterance_count, char_count,
                speech_duration_ms, pause_ratio_sum, pause_sample_count,
                tic_counts, updated_at
            ) VALUES ($1, $2, 1, $3, $4, $5, $6, $7::jsonb, now())
            ON CONFLICT(account_id, scene) DO UPDATE SET
                utterance_count = speech_style_stats.utterance_count + 1,
                char_count = speech_style_stats.char_count + excluded.char_count,
                speech_duration_ms = speech_style_stats.speech_duration_ms
                    + excluded.speech_duration_ms,
                pause_ratio_sum = speech_style_stats.pause_ratio_sum
                    + excluded.pause_ratio_sum,
                pause_sample_count = speech_style_stats.pause_sample_count
                    + excluded.pause_sample_count,
                tic_counts = excluded.tic_counts,
                updated_at = now()
            """,
            evidence.account_id,
            evidence.scene,
            len(text),
            evidence.speech_duration_ms or 0,
            evidence.pause_ratio or 0.0,
            1 if evidence.pause_ratio is not None else 0,
            json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
        )

    async def review(self, command: PersonaReview) -> PersonaTrait:
        try:
            trait_id = uuid.UUID(command.trait_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(command.trait_id) from exc
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, command.account_id)
            await self._lock_account(connection, command.account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM persona_traits
                WHERE trait_id = $1 AND account_id = $2
                FOR UPDATE
                """,
                trait_id,
                command.account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(command.trait_id)
            description = (
                command.corrected_description.strip()
                if command.corrected_description is not None
                else str(row["description"])
            )
            status: PersonaTraitStatus = "disabled" if command.action == "disable" else "confirmed"
            counterexample = (
                command.counterexample.strip()
                if command.counterexample is not None
                else str(row["counterexample"])
            )
            require_persona_counterexample(
                category=cast(PersonaTraitCategory, str(row["category"])),
                action=command.action,
                counterexample=counterexample,
            )
            review_event = EvidenceEvent(
                event_id=str(uuid.uuid4()),
                account_id=command.account_id,
                event_type="persona.trait_reviewed",
                occurred_at=datetime.now(UTC),
                speaker_class="system",
                source="user.persona_review",
                payload={
                    "target_id": command.trait_id,
                    "action": command.action,
                    "previous_description": str(row["description"]),
                    **(
                        {"corrected_description": description}
                        if command.action == "correct"
                        else {}
                    ),
                    **(
                        {"counterexample": counterexample}
                        if command.counterexample is not None
                        else {}
                    ),
                },
            )
            await self._insert_evidence(connection, review_event)
            if status == "confirmed" and str(row["category"]) in EXCLUSIVE_STYLE_CATEGORIES:
                await connection.execute(
                    """
                    UPDATE persona_traits
                    SET status = 'candidate', updated_at = now()
                    WHERE account_id = $1 AND category = $2 AND trait_id <> $3
                      AND status = 'confirmed'
                    """,
                    command.account_id,
                    row["category"],
                    trait_id,
                )
            await connection.execute(
                """
                UPDATE persona_traits
                SET description = $1, counterexample = $2, status = $3,
                    review_event_id = $4, updated_at = now()
                WHERE trait_id = $5 AND account_id = $6
                """,
                description,
                counterexample,
                status,
                review_event.event_id,
                trait_id,
                command.account_id,
            )
            version = await self._publish_version(
                connection,
                command.account_id,
                reason=f"trait_{command.action}",
            )
            updated = await connection.fetchrow(
                "SELECT * FROM persona_traits WHERE trait_id = $1",
                trait_id,
            )
            assert updated is not None
            return await self._trait_from_row(
                connection,
                updated,
                version_id=version.version_id,
            )

    async def _publish_version(
        self,
        connection: asyncpg.Connection,
        account_id: str,
        *,
        reason: str,
    ) -> PersonaVersion:
        rows = await connection.fetch(
            """
            SELECT * FROM persona_traits
            WHERE account_id = $1 AND status = 'confirmed'
            """,
            account_id,
        )
        ordered = sorted(
            rows,
            key=lambda row: (CATEGORY_ORDER[str(row["category"])], str(row["trait_id"])),
        )
        snapshot: list[dict[str, Any]] = []
        for row in ordered:
            evidence = await connection.fetch(
                """
                SELECT pe.source_event_id, pe.scene, pe.weight, ae.speaker_class
                FROM persona_evidence AS pe
                JOIN archive_evidence_events AS ae
                  ON ae.event_id = pe.source_event_id
                 AND ae.account_id = pe.account_id
                WHERE pe.trait_id = $1
                ORDER BY pe.occurred_at, pe.source_event_id
                """,
                row["trait_id"],
            )
            owner_evidence = [item for item in evidence if item["speaker_class"] == "owner"]
            selected_evidence = owner_evidence or evidence
            selected_weight = sum(float(item["weight"]) for item in selected_evidence)
            snapshot.append(
                {
                    "trait_id": str(row["trait_id"]),
                    "category": str(row["category"]),
                    "description": str(row["description"]),
                    "context": (
                        str(selected_evidence[0]["scene"])
                        if selected_evidence
                        else str(row["context"])
                    ),
                    "counterexample": str(row["counterexample"]),
                    "confidence": (
                        min(0.95, 0.45 + 0.13 * selected_weight)
                        if owner_evidence
                        else float(row["confidence"])
                    ),
                    "source_event_ids": [
                        str(item["source_event_id"]) for item in selected_evidence
                    ],
                }
            )
        active = await connection.fetchrow(
            """
            SELECT version_id FROM persona_versions
            WHERE account_id = $1 AND status = 'active'
            """,
            account_id,
        )
        version_number = int(
            await connection.fetchval(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM persona_versions WHERE account_id = $1
                """,
                account_id,
            )
        )
        version_id = uuid.uuid4()
        created_at = datetime.now(UTC)
        await connection.execute(
            """
            UPDATE persona_versions SET status = 'superseded'
            WHERE account_id = $1 AND status = 'active'
            """,
            account_id,
        )
        await connection.execute(
            """
            INSERT INTO persona_versions (
                version_id, account_id, version_number, status, reason,
                snapshot, parent_version_id, created_at
            ) VALUES ($1, $2, $3, 'active', $4, $5::jsonb, $6, $7)
            """,
            version_id,
            account_id,
            version_number,
            reason,
            json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            active["version_id"] if active is not None else None,
            created_at,
        )
        return PersonaVersion(
            version_id=str(version_id),
            version_number=version_number,
            status="active",
            reason=reason,
            trait_ids=tuple(str(row["trait_id"]) for row in ordered),
            created_at=created_at,
        )

    async def rollback(self, *, account_id: str, version_id: str) -> PersonaVersion:
        if not account_id.strip() or not version_id.strip():
            raise ValueError("persona rollback requires account_id and version_id")
        try:
            version_uuid = uuid.UUID(version_id)
        except ValueError as exc:
            raise EvidenceNotFoundError(version_id) from exc
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            row = await connection.fetchrow(
                """
                SELECT * FROM persona_versions
                WHERE version_id = $1 AND account_id = $2
                FOR UPDATE
                """,
                version_uuid,
                account_id,
            )
            if row is None:
                raise EvidenceNotFoundError(version_id)
            snapshot = row["snapshot"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            await connection.execute(
                "UPDATE persona_traits SET status = 'disabled' WHERE account_id = $1",
                account_id,
            )
            for item in snapshot:
                await connection.execute(
                    """
                    UPDATE persona_traits
                    SET description = $1, context = $2, counterexample = $3,
                        confidence = $4, status = 'confirmed', updated_at = now()
                    WHERE trait_id = $5 AND account_id = $6
                    """,
                    item["description"],
                    item["context"],
                    item["counterexample"],
                    item["confidence"],
                    uuid.UUID(str(item["trait_id"])),
                    account_id,
                )
            await connection.execute(
                """
                UPDATE persona_versions SET status = 'superseded'
                WHERE account_id = $1 AND status = 'active'
                """,
                account_id,
            )
            await connection.execute(
                """
                UPDATE persona_versions SET status = 'active', reason = 'rollback'
                WHERE version_id = $1
                """,
                version_uuid,
            )
        return PersonaVersion(
            version_id=version_id,
            version_number=int(row["version_number"]),
            status="active",
            reason="rollback",
            trait_ids=tuple(sorted(str(item["trait_id"]) for item in snapshot)),
            created_at=cast(datetime, row["created_at"]),
        )

    async def capsule(self, request: PersonaRequest) -> PersonaCapsule:
        confirmed_style_only = request.speaker_class == "uncertain" and request.confirmed_style_only
        if not request.enabled or (request.speaker_class != "owner" and not confirmed_style_only):
            return PersonaCapsule()
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, request.account_id)
            await self._lock_account(connection, request.account_id)
            row = await connection.fetchrow(
                """
                SELECT persona_versions.*
                FROM persona_versions
                JOIN persona_learning_consents USING (account_id)
                WHERE persona_versions.account_id = $1
                  AND persona_versions.status = 'active'
                  AND persona_learning_consents.revoked_at IS NULL
                """,
                request.account_id,
            )
        if row is None:
            return PersonaCapsule()
        snapshot = row["snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        if confirmed_style_only:
            safe_snapshot: list[dict[str, Any]] = []
            for item in snapshot:
                description = safe_confirmed_style_description(str(item.get("description") or ""))
                if description is None:
                    continue
                safe_snapshot.append(
                    {
                        **item,
                        "description": description,
                        "context": "",
                        "counterexample": "",
                        "source_event_ids": [],
                    }
                )
            snapshot = safe_snapshot
        ranked = sorted(snapshot, key=lambda item: self._capsule_rank(item, request.topic))
        prefix = (
            f"[已确认表达风格 v{row['version_number']}] "
            "仅调整表达方式，不推断或透露账户主人的身份、经历、价值观和决定。"
            if confirmed_style_only
            else f"[人格胶囊 v{row['version_number']}] "
            "仅在自然且相关时参考，不机械复读口头禅；不得声称你就是账户主人。"
        )
        lines = [prefix]
        entries: list[PersonaCapsuleEntry] = []
        for item in ranked:
            line = f"- {item['description']}"
            if item["context"] and item["context"] != "conversation":
                line += f"（适用：{item['context']}）"
            if item["counterexample"]:
                line += f"（例外：{item['counterexample']}）"
            if len("\n".join((*lines, line))) > request.max_chars:
                continue
            lines.append(line)
            entries.append(
                PersonaCapsuleEntry(
                    trait_id=str(item["trait_id"]),
                    category=cast(PersonaTraitCategory, item["category"]),
                    description=str(item["description"]),
                    context=str(item["context"]),
                    counterexample=str(item["counterexample"]),
                    confidence=float(item["confidence"]),
                    source_event_ids=tuple(str(value) for value in item["source_event_ids"]),
                )
            )
        if not entries:
            return PersonaCapsule()
        return PersonaCapsule(
            version_id=str(row["version_id"]),
            version_number=int(row["version_number"]),
            entries=tuple(entries),
            prompt_fragment="\n".join(lines),
            delivery_rate=self._delivery_rate(entries),
        )

    @staticmethod
    def _capsule_rank(item: dict[str, Any], topic: str) -> tuple[int, int, str]:
        description = str(item["description"])
        context = str(item["context"])
        compact = re.sub(r"\s+", "", topic)
        tokens = {compact}
        tokens.update(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
        relevant = bool(
            topic and any(token in description or token in context for token in tokens if token)
        )
        category = str(item["category"])
        return (0 if relevant else 1, CATEGORY_ORDER[category], str(item["trait_id"]))

    @staticmethod
    def _delivery_rate(entries: list[PersonaCapsuleEntry]) -> float:
        descriptions = " ".join(
            item.description for item in entries if item.category == "speech_rate"
        )
        if "偏从容" in descriptions:
            return 0.95
        if "偏快" in descriptions:
            return 1.05
        return 1.0

    async def traits(self, *, account_id: str) -> tuple[PersonaTrait, ...]:
        if not account_id.strip():
            raise ValueError("persona traits require account_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM persona_traits
                WHERE account_id = $1
                ORDER BY (status = 'candidate') DESC, updated_at DESC, trait_id
                """,
                account_id,
            )
            return tuple([await self._trait_from_row(connection, row) for row in rows])

    async def versions(self, *, account_id: str) -> tuple[PersonaVersion, ...]:
        if not account_id.strip():
            raise ValueError("persona versions require account_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            rows = await connection.fetch(
                """
                SELECT * FROM persona_versions
                WHERE account_id = $1 ORDER BY version_number DESC
                """,
                account_id,
            )
        versions: list[PersonaVersion] = []
        for row in rows:
            snapshot = row["snapshot"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            versions.append(
                PersonaVersion(
                    version_id=str(row["version_id"]),
                    version_number=int(row["version_number"]),
                    status=cast(Any, row["status"]),
                    reason=str(row["reason"]),
                    trait_ids=tuple(str(item["trait_id"]) for item in snapshot),
                    created_at=cast(datetime, row["created_at"]),
                )
            )
        return tuple(versions)

    async def grant_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
    ) -> PersonaConsent:
        if not account_id.strip() or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", policy_version):
            raise ValueError("persona consent requires account_id and a versioned policy")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            current = await connection.fetchrow(
                "SELECT * FROM persona_learning_consents WHERE account_id = $1",
                account_id,
            )
            if (
                current is not None
                and current["revoked_at"] is None
                and current["policy_version"] == policy_version
            ):
                return PersonaConsent(
                    account_id=account_id,
                    policy_version=policy_version,
                    granted_at=cast(datetime, current["granted_at"]),
                )
            granted_at = datetime.now(UTC)
            event = EvidenceEvent(
                event_id=str(uuid.uuid4()),
                account_id=account_id,
                event_type="consent.granted",
                occurred_at=granted_at,
                speaker_class="system",
                source="user.persona_consent",
                payload={"purpose": "persona_learning", "policy_version": policy_version},
            )
            await self._insert_evidence(connection, event)
            await connection.execute(
                """
                INSERT INTO persona_learning_consents (
                    account_id, policy_version, granted_at, revoked_at,
                    grant_event_id, revoke_event_id
                ) VALUES ($1, $2, $3, NULL, $4, NULL)
                ON CONFLICT(account_id) DO UPDATE SET
                    policy_version = excluded.policy_version,
                    granted_at = excluded.granted_at,
                    revoked_at = NULL,
                    grant_event_id = excluded.grant_event_id,
                    revoke_event_id = NULL
                """,
                account_id,
                policy_version,
                granted_at,
                event.event_id,
            )
        return PersonaConsent(
            account_id=account_id,
            policy_version=policy_version,
            granted_at=granted_at,
        )

    async def revoke_consent(self, *, account_id: str) -> PersonaConsent:
        if not account_id.strip():
            raise ValueError("persona consent revocation requires account_id")
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            await self._lock_account(connection, account_id)
            current = await connection.fetchrow(
                "SELECT * FROM persona_learning_consents WHERE account_id = $1",
                account_id,
            )
            if current is None:
                raise EvidenceNotFoundError(account_id)
            granted_at = cast(datetime, current["granted_at"])
            if current["revoked_at"] is not None:
                return PersonaConsent(
                    account_id=account_id,
                    policy_version=str(current["policy_version"]),
                    granted_at=granted_at,
                    revoked_at=cast(datetime, current["revoked_at"]),
                )
            revoked_at = datetime.now(UTC)
            event = EvidenceEvent(
                event_id=str(uuid.uuid4()),
                account_id=account_id,
                event_type="consent.revoked",
                occurred_at=revoked_at,
                speaker_class="system",
                source="user.persona_consent",
                payload={
                    "purpose": "persona_learning",
                    "policy_version": str(current["policy_version"]),
                },
            )
            await self._insert_evidence(connection, event)
            await connection.execute(
                """
                UPDATE persona_learning_consents
                SET revoked_at = $1, revoke_event_id = $2
                WHERE account_id = $3
                """,
                revoked_at,
                event.event_id,
                account_id,
            )
        return PersonaConsent(
            account_id=account_id,
            policy_version=str(current["policy_version"]),
            granted_at=granted_at,
            revoked_at=revoked_at,
        )

    async def learning_allowed(self, *, account_id: str) -> bool:
        if not account_id.strip():
            return False
        pool = await self._ready_pool()
        async with pool.acquire() as connection, connection.transaction():
            await self._scope(connection, account_id)
            value = await connection.fetchval(
                """
                SELECT 1 FROM persona_learning_consents
                WHERE account_id = $1 AND revoked_at IS NULL
                """,
                account_id,
            )
        return value is not None

    @staticmethod
    async def _trait_from_row(
        connection: asyncpg.Connection,
        row: asyncpg.Record,
        *,
        version_id: str | None = None,
    ) -> PersonaTrait:
        evidence = await connection.fetch(
            """
            SELECT source_event_id FROM persona_evidence
            WHERE trait_id = $1 ORDER BY occurred_at, source_event_id
            """,
            row["trait_id"],
        )
        return PersonaTrait(
            trait_id=str(row["trait_id"]),
            category=cast(PersonaTraitCategory, row["category"]),
            description=str(row["description"]),
            context=str(row["context"]),
            counterexample=str(row["counterexample"]),
            confidence=float(row["confidence"]),
            status=cast(PersonaTraitStatus, row["status"]),
            observation_count=int(row["observation_count"]),
            source_event_ids=tuple(str(item["source_event_id"]) for item in evidence),
            updated_at=cast(datetime, row["updated_at"]),
            version_id=version_id,
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
            _stable_uuid("outbox", event.event_id),
            event.account_id,
            event.event_id,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
