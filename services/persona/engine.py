"""SQLite PersonaEngine with evidence gates, review and version snapshots."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from services.archive.domain import EvidenceEvent, EvidenceNotFoundError, canonical_payload
from services.archive.life_archive import LifeArchive
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
    AUTO_PROMOTE,
    CATEGORY_ORDER,
    TICS,
    PersonaExtractor,
    RuleBasedPersonaExtractor,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS persona_traits (
    trait_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    description TEXT NOT NULL,
    context TEXT NOT NULL,
    counterexample TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'confirmed', 'disabled')),
    observation_count INTEGER NOT NULL DEFAULT 0 CHECK (observation_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    review_event_id TEXT,
    UNIQUE (account_id, category, normalized_key)
);

CREATE TABLE IF NOT EXISTS persona_evidence (
    trait_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    scene TEXT NOT NULL,
    weight REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
    occurred_at TEXT NOT NULL,
    PRIMARY KEY (trait_id, source_event_id),
    FOREIGN KEY (trait_id) REFERENCES persona_traits(trait_id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS persona_observation_receipts (
    source_event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS speech_style_stats (
    account_id TEXT NOT NULL,
    scene TEXT NOT NULL,
    utterance_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    speech_duration_ms INTEGER NOT NULL DEFAULT 0,
    pause_ratio_sum REAL NOT NULL DEFAULT 0,
    pause_sample_count INTEGER NOT NULL DEFAULT 0,
    tic_counts_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, scene)
);

CREATE TABLE IF NOT EXISTS persona_learning_consents (
    account_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    grant_event_id TEXT NOT NULL,
    revoke_event_id TEXT
);

CREATE TABLE IF NOT EXISTS persona_versions (
    version_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    reason TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    parent_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, version_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_persona_one_active_version
ON persona_versions(account_id) WHERE status = 'active';
"""

_CONTAMINATION = frozenset(
    {"assistant", "synthetic_audio", "guest", "echo", "overlap", "low_quality", "replay"}
)


def _stable_id(kind: str, *values: object) -> str:
    key = ":".join(str(value) for value in values)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:{kind}:{key}"))


class PersonaEngine:
    def __init__(self, sqlite_path: Path, *, extractor: PersonaExtractor | None = None) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._extractor = extractor or RuleBasedPersonaExtractor()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        extractor: PersonaExtractor | None = None,
    ) -> PersonaEngine:
        return cls(Path(path), extractor=extractor)

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
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _duplicate_observation(
        connection: sqlite3.Connection,
        evidence: PersonaEvidence,
    ) -> ObservationResult | None:
        receipt = connection.execute(
            """
            SELECT source_event_id FROM persona_observation_receipts
            WHERE source_event_id = ? AND account_id = ?
            """,
            (evidence.source_event_id, evidence.account_id),
        ).fetchone()
        if receipt is None:
            return None
        trait_rows = connection.execute(
            """
            SELECT trait_id FROM persona_evidence
            WHERE source_event_id = ? AND account_id = ?
            ORDER BY trait_id
            """,
            (evidence.source_event_id, evidence.account_id),
        ).fetchall()
        return ObservationResult(
            True,
            "duplicate_evidence",
            tuple(str(item["trait_id"]) for item in trait_rows),
        )

    async def observe(self, evidence: PersonaEvidence) -> ObservationResult:
        if not evidence.learning_allowed:
            return ObservationResult(False, "learning_not_authorized")
        if set(evidence.contamination_flags) & _CONTAMINATION:
            return ObservationResult(False, "contaminated_evidence")
        if evidence.quality_score is not None and evidence.quality_score < 0.5:
            return ObservationResult(False, "low_quality_evidence")

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM evidence_events
                WHERE event_id = ? AND account_id = ?
                """,
                (evidence.source_event_id, evidence.account_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(evidence.source_event_id)
            if str(row["event_type"]).startswith("assistant.") or row["speaker_class"] in {
                "assistant",
                "system",
            }:
                return ObservationResult(False, "assistant_or_synthetic_evidence")
            if row["speaker_class"] != "owner":
                return ObservationResult(False, "speaker_not_owner")
            if row["event_type"] != "speech.utterance_finalized":
                return ObservationResult(False, "unsupported_evidence_type")

            payload = json.loads(str(row["payload_json"]))
            text = str(payload.get("text") or "").strip()
            if not text:
                return ObservationResult(False, "empty_evidence")
            duplicate = self._duplicate_observation(connection, evidence)
            if duplicate is not None:
                return duplicate
            occurred_at = str(row["occurred_at"])

        candidates = await self._extractor.extract(text, evidence)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_observation(connection, evidence)
            if duplicate is not None:
                return duplicate
            now = datetime.now(UTC).isoformat()
            trait_ids: list[str] = []
            promoted = False
            tic_counts = {tic: text.count(tic) for tic in TICS if tic in text}
            self._update_style_stats(connection, evidence, text, tic_counts, now)
            for candidate in candidates:
                trait_id = _stable_id(
                    "persona-trait",
                    evidence.account_id,
                    candidate.category,
                    candidate.normalized_key,
                )
                trait_ids.append(trait_id)
                connection.execute(
                    """
                    INSERT OR IGNORE INTO persona_traits (
                        trait_id, account_id, category, normalized_key,
                        description, context, counterexample, confidence,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.55, ?, ?)
                    """,
                    (
                        trait_id,
                        evidence.account_id,
                        candidate.category,
                        candidate.normalized_key,
                        candidate.description,
                        candidate.context,
                        candidate.counterexample,
                        now,
                        now,
                    ),
                )
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO persona_evidence (
                        trait_id, account_id, source_event_id, scene, weight, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trait_id,
                        evidence.account_id,
                        evidence.source_event_id,
                        evidence.scene,
                        evidence.quality_score if evidence.quality_score is not None else 1.0,
                        occurred_at,
                    ),
                ).rowcount
                if inserted:
                    count = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM persona_evidence WHERE trait_id = ?",
                            (trait_id,),
                        ).fetchone()[0]
                    )
                    confidence = min(0.95, 0.45 + 0.13 * count)
                    previous = connection.execute(
                        "SELECT status FROM persona_traits WHERE trait_id = ?",
                        (trait_id,),
                    ).fetchone()
                    status = str(previous["status"]) if previous is not None else "candidate"
                    if candidate.category in AUTO_PROMOTE and count >= 3:
                        status = "confirmed"
                        promoted = promoted or previous is None or previous["status"] != "confirmed"
                    connection.execute(
                        """
                        UPDATE persona_traits
                        SET observation_count = ?, confidence = ?, status = ?, updated_at = ?
                        WHERE trait_id = ?
                        """,
                        (count, confidence, status, now, trait_id),
                    )
            connection.execute(
                """
                INSERT INTO persona_observation_receipts (
                    source_event_id, account_id, observed_at
                ) VALUES (?, ?, ?)
                """,
                (evidence.source_event_id, evidence.account_id, now),
            )
            version_id = (
                self._publish_version(
                    connection,
                    evidence.account_id,
                    reason="stable_style_observation",
                ).version_id
                if promoted
                else None
            )
        return ObservationResult(
            accepted=True,
            reason="observed",
            candidate_trait_ids=tuple(trait_ids),
            published_version_id=version_id,
        )

    @staticmethod
    def _update_style_stats(
        connection: sqlite3.Connection,
        evidence: PersonaEvidence,
        text: str,
        tic_counts: dict[str, int],
        now: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT * FROM speech_style_stats
            WHERE account_id = ? AND scene = ?
            """,
            (evidence.account_id, evidence.scene),
        ).fetchone()
        existing_tics = json.loads(str(row["tic_counts_json"])) if row is not None else {}
        for tic, count in tic_counts.items():
            existing_tics[tic] = int(existing_tics.get(tic, 0)) + count
        values = (
            1,
            len(text),
            evidence.speech_duration_ms or 0,
            evidence.pause_ratio or 0.0,
            1 if evidence.pause_ratio is not None else 0,
        )
        connection.execute(
            """
            INSERT INTO speech_style_stats (
                account_id, scene, utterance_count, char_count,
                speech_duration_ms, pause_ratio_sum, pause_sample_count,
                tic_counts_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, scene) DO UPDATE SET
                utterance_count = utterance_count + excluded.utterance_count,
                char_count = char_count + excluded.char_count,
                speech_duration_ms = speech_duration_ms + excluded.speech_duration_ms,
                pause_ratio_sum = pause_ratio_sum + excluded.pause_ratio_sum,
                pause_sample_count = pause_sample_count + excluded.pause_sample_count,
                tic_counts_json = excluded.tic_counts_json,
                updated_at = excluded.updated_at
            """,
            (
                evidence.account_id,
                evidence.scene,
                *values,
                json.dumps(existing_tics, ensure_ascii=False, sort_keys=True),
                now,
            ),
        )

    async def review(self, command: PersonaReview) -> PersonaTrait:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM persona_traits
                WHERE trait_id = ? AND account_id = ?
                """,
                (command.trait_id, command.account_id),
            ).fetchone()
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
            self._insert_evidence(connection, review_event)
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                UPDATE persona_traits
                SET description = ?, counterexample = ?, status = ?,
                    review_event_id = ?, updated_at = ?
                WHERE trait_id = ? AND account_id = ?
                """,
                (
                    description,
                    counterexample,
                    status,
                    review_event.event_id,
                    now,
                    command.trait_id,
                    command.account_id,
                ),
            )
            version = self._publish_version(
                connection,
                command.account_id,
                reason=f"trait_{command.action}",
            )
            updated = connection.execute(
                "SELECT * FROM persona_traits WHERE trait_id = ?",
                (command.trait_id,),
            ).fetchone()
            assert updated is not None
            return self._trait_from_row(
                connection,
                updated,
                version_id=version.version_id,
            )

    def _publish_version(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        *,
        reason: str,
    ) -> PersonaVersion:
        rows = connection.execute(
            """
            SELECT * FROM persona_traits
            WHERE account_id = ? AND status = 'confirmed'
            """,
            (account_id,),
        ).fetchall()
        ordered = sorted(rows, key=lambda row: (CATEGORY_ORDER[str(row["category"])], row["trait_id"]))
        snapshot: list[dict[str, Any]] = []
        for row in ordered:
            evidence = connection.execute(
                """
                SELECT source_event_id FROM persona_evidence
                WHERE trait_id = ? ORDER BY occurred_at, source_event_id
                """,
                (row["trait_id"],),
            ).fetchall()
            snapshot.append(
                {
                    "trait_id": str(row["trait_id"]),
                    "category": str(row["category"]),
                    "description": str(row["description"]),
                    "context": str(row["context"]),
                    "counterexample": str(row["counterexample"]),
                    "confidence": float(row["confidence"]),
                    "source_event_ids": [str(item["source_event_id"]) for item in evidence],
                }
            )
        active = connection.execute(
            """
            SELECT version_id FROM persona_versions
            WHERE account_id = ? AND status = 'active'
            """,
            (account_id,),
        ).fetchone()
        version_number = int(
            connection.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM persona_versions WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
        )
        version_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        connection.execute(
            "UPDATE persona_versions SET status = 'superseded' WHERE account_id = ? AND status = 'active'",
            (account_id,),
        )
        connection.execute(
            """
            INSERT INTO persona_versions (
                version_id, account_id, version_number, status, reason,
                snapshot_json, parent_version_id, created_at
            ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
            """,
            (
                version_id,
                account_id,
                version_number,
                reason,
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                str(active["version_id"]) if active is not None else None,
                now.isoformat(),
            ),
        )
        return PersonaVersion(
            version_id=version_id,
            version_number=version_number,
            status="active",
            reason=reason,
            trait_ids=tuple(str(row["trait_id"]) for row in ordered),
            created_at=now,
        )

    async def rollback(self, *, account_id: str, version_id: str) -> PersonaVersion:
        if not account_id.strip() or not version_id.strip():
            raise ValueError("persona rollback requires account_id and version_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM persona_versions
                WHERE version_id = ? AND account_id = ?
                """,
                (version_id, account_id),
            ).fetchone()
            if row is None:
                raise EvidenceNotFoundError(version_id)
            snapshot = json.loads(str(row["snapshot_json"]))
            snapshot_ids = {str(item["trait_id"]) for item in snapshot}
            connection.execute(
                "UPDATE persona_traits SET status = 'disabled' WHERE account_id = ?",
                (account_id,),
            )
            for item in snapshot:
                connection.execute(
                    """
                    UPDATE persona_traits
                    SET description = ?, context = ?, counterexample = ?,
                        confidence = ?, status = 'confirmed', updated_at = ?
                    WHERE trait_id = ? AND account_id = ?
                    """,
                    (
                        item["description"],
                        item["context"],
                        item["counterexample"],
                        item["confidence"],
                        datetime.now(UTC).isoformat(),
                        item["trait_id"],
                        account_id,
                    ),
                )
            connection.execute(
                "UPDATE persona_versions SET status = 'superseded' WHERE account_id = ? AND status = 'active'",
                (account_id,),
            )
            connection.execute(
                "UPDATE persona_versions SET status = 'active', reason = 'rollback' WHERE version_id = ?",
                (version_id,),
            )
            return PersonaVersion(
                version_id=version_id,
                version_number=int(row["version_number"]),
                status="active",
                reason="rollback",
                trait_ids=tuple(sorted(snapshot_ids)),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )

    async def capsule(self, request: PersonaRequest) -> PersonaCapsule:
        if not request.enabled or request.speaker_class != "owner":
            return PersonaCapsule()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM persona_versions
                WHERE account_id = ? AND status = 'active'
                """,
                (request.account_id,),
            ).fetchone()
        if row is None:
            return PersonaCapsule()
        snapshot = json.loads(str(row["snapshot_json"]))
        ranked = sorted(snapshot, key=lambda item: self._capsule_rank(item, request.topic))
        prefix = (
            f"[人格胶囊 v{row['version_number']}] "
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
            candidate_prompt = "\n".join((*lines, line))
            if len(candidate_prompt) > request.max_chars:
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
        relevant = bool(topic and any(token in description or token in context for token in PersonaEngine._topic_tokens(topic)))
        category = str(item["category"])
        return (0 if relevant else 1, CATEGORY_ORDER[category], str(item["trait_id"]))

    @staticmethod
    def _topic_tokens(topic: str) -> tuple[str, ...]:
        compact = re.sub(r"\s+", "", topic)
        tokens = {compact}
        tokens.update(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
        return tuple(token for token in tokens if token)

    @staticmethod
    def _delivery_rate(entries: list[PersonaCapsuleEntry]) -> float:
        descriptions = " ".join(item.description for item in entries if item.category == "speech_rate")
        if "偏从容" in descriptions:
            return 0.95
        if "偏快" in descriptions:
            return 1.05
        return 1.0

    async def traits(self, *, account_id: str) -> tuple[PersonaTrait, ...]:
        if not account_id.strip():
            raise ValueError("persona traits require account_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM persona_traits
                WHERE account_id = ?
                ORDER BY status = 'candidate' DESC, updated_at DESC, trait_id
                """,
                (account_id,),
            ).fetchall()
            return tuple(self._trait_from_row(connection, row) for row in rows)

    async def versions(self, *, account_id: str) -> tuple[PersonaVersion, ...]:
        if not account_id.strip():
            raise ValueError("persona versions require account_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM persona_versions
                WHERE account_id = ? ORDER BY version_number DESC
                """,
                (account_id,),
            ).fetchall()
        return tuple(
            PersonaVersion(
                version_id=str(row["version_id"]),
                version_number=int(row["version_number"]),
                status=cast(Any, row["status"]),
                reason=str(row["reason"]),
                trait_ids=tuple(
                    str(item["trait_id"]) for item in json.loads(str(row["snapshot_json"]))
                ),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            for row in rows
        )

    async def grant_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
    ) -> PersonaConsent:
        if not account_id.strip() or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", policy_version):
            raise ValueError("persona consent requires account_id and a versioned policy")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM persona_learning_consents WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if (
                current is not None
                and current["revoked_at"] is None
                and current["policy_version"] == policy_version
            ):
                return PersonaConsent(
                    account_id=account_id,
                    policy_version=policy_version,
                    granted_at=datetime.fromisoformat(str(current["granted_at"])),
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
            self._insert_evidence(connection, event)
            connection.execute(
                """
                INSERT INTO persona_learning_consents (
                    account_id, policy_version, granted_at, revoked_at,
                    grant_event_id, revoke_event_id
                ) VALUES (?, ?, ?, NULL, ?, NULL)
                ON CONFLICT(account_id) DO UPDATE SET
                    policy_version = excluded.policy_version,
                    granted_at = excluded.granted_at,
                    revoked_at = NULL,
                    grant_event_id = excluded.grant_event_id,
                    revoke_event_id = NULL
                """,
                (account_id, policy_version, granted_at.isoformat(), event.event_id),
            )
        return PersonaConsent(
            account_id=account_id,
            policy_version=policy_version,
            granted_at=granted_at,
        )

    async def revoke_consent(self, *, account_id: str) -> PersonaConsent:
        if not account_id.strip():
            raise ValueError("persona consent revocation requires account_id")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT * FROM persona_learning_consents WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if current is None:
                raise EvidenceNotFoundError(account_id)
            granted_at = datetime.fromisoformat(str(current["granted_at"]))
            if current["revoked_at"] is not None:
                return PersonaConsent(
                    account_id=account_id,
                    policy_version=str(current["policy_version"]),
                    granted_at=granted_at,
                    revoked_at=datetime.fromisoformat(str(current["revoked_at"])),
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
            self._insert_evidence(connection, event)
            connection.execute(
                """
                UPDATE persona_learning_consents
                SET revoked_at = ?, revoke_event_id = ?
                WHERE account_id = ?
                """,
                (revoked_at.isoformat(), event.event_id, account_id),
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
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM persona_learning_consents
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (account_id,),
            ).fetchone()
        return row is not None

    @staticmethod
    def _trait_from_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        version_id: str | None = None,
    ) -> PersonaTrait:
        evidence = connection.execute(
            """
            SELECT source_event_id FROM persona_evidence
            WHERE trait_id = ? ORDER BY occurred_at, source_event_id
            """,
            (row["trait_id"],),
        ).fetchall()
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
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            version_id=version_id,
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
                _stable_id("outbox", event.event_id),
                event.account_id,
                event.event_id,
                recorded_at,
                recorded_at,
            ),
        )
