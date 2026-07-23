"""SQLite Digital Self registry used by local and single-node deployments."""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from services.archive.domain import EvidenceEvent, SpeakerClass
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.common.evidence_policy import confirmed_projection_contribution_for
from services.digital_self.compiler import (
    DEFAULT_COMPILER_VERSION,
    DEFAULT_POLICY_VERSION,
    build_manifest,
    cognitive_entry,
    decision_entry,
    decode_manifest,
    memory_entry,
    persona_entry,
    relationship_entry,
)
from services.digital_self.domain import (
    DigitalSelfVersion,
    InvalidVersionTransitionError,
    ManifestEntry,
    SourceSnapshotConflictError,
    VersionNotFoundError,
    VersionStatus,
    VoiceProfileManifestRef,
)
from services.persona.domain import LEGACY_COGNITIVE_TRAIT_CATEGORIES
from services.persona.engine import PersonaEngine
from services.self_model.domain import (
    CognitiveClaim,
    CognitiveClaimType,
    DecisionCase,
    DecisionKind,
    ItemStatus,
    RelationshipProfile,
    RelationshipProfileStatus,
    SelfModelItemKind,
    SelfModelSource,
    SourceRelation,
)
from services.self_model.policy import is_effective
from services.self_model.registry import SelfModelRegistry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS digital_self_versions (
    version_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'testing', 'approved', 'frozen', 'revoked')
    ),
    manifest_json TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    source_summary_sha256 TEXT NOT NULL CHECK (length(source_summary_sha256) = 64),
    parent_version_id TEXT,
    rollback_target_version_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (account_id, version_number),
    UNIQUE (account_id, version_id),
    FOREIGN KEY (account_id, parent_version_id)
        REFERENCES digital_self_versions(account_id, version_id),
    FOREIGN KEY (account_id, rollback_target_version_id)
        REFERENCES digital_self_versions(account_id, version_id)
);

CREATE INDEX IF NOT EXISTS idx_digital_self_account_status
ON digital_self_versions(account_id, status, version_number DESC);

CREATE TRIGGER IF NOT EXISTS digital_self_manifest_immutable
BEFORE UPDATE OF account_id, version_number, manifest_json, manifest_sha256,
                 source_summary_sha256, parent_version_id,
                 rollback_target_version_id, created_at
ON digital_self_versions
BEGIN
    SELECT RAISE(ABORT, 'digital self manifest is immutable');
END;

CREATE TABLE IF NOT EXISTS digital_self_lifecycle_audit_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    actor_account_id TEXT NOT NULL CHECK (actor_account_id = account_id),
    action TEXT NOT NULL CHECK (
        action IN ('build', 'begin_testing', 'approve', 'freeze', 'revoke', 'rollback')
    ),
    version_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    from_status TEXT CHECK (
        from_status IS NULL OR from_status IN ('draft', 'testing', 'approved', 'frozen', 'revoked')
    ),
    to_status TEXT NOT NULL CHECK (
        to_status IN ('draft', 'testing', 'approved', 'frozen', 'revoked')
    ),
    target_version_id TEXT,
    new_version_id TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digital_self_lifecycle_audit_account_occurred
ON digital_self_lifecycle_audit_events(account_id, occurred_at);

CREATE TRIGGER IF NOT EXISTS digital_self_lifecycle_audit_immutable
BEFORE UPDATE ON digital_self_lifecycle_audit_events
BEGIN
    SELECT RAISE(ABORT, 'digital self lifecycle audit events are immutable');
END;
"""

_TRANSITIONS: dict[str, tuple[VersionStatus, VersionStatus]] = {
    "begin_testing": ("draft", "testing"),
    "approve": ("testing", "approved"),
    "freeze": ("approved", "frozen"),
    "revoke": ("frozen", "revoked"),
}
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")


def _optional_datetime(value: object) -> datetime | None:
    return datetime.fromisoformat(str(value)) if value is not None else None


def _json_strings(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list):
        raise SourceSnapshotConflictError("self model list field is invalid")
    return tuple(str(item) for item in decoded)


class DigitalSelfRegistry:
    def __init__(
        self,
        sqlite_path: Path,
        *,
        compiler_version: str = DEFAULT_COMPILER_VERSION,
        policy_version: str = DEFAULT_POLICY_VERSION,
    ) -> None:
        if not compiler_version.strip() or not policy_version.strip():
            raise ValueError("digital self compiler and policy versions are required")
        self._path = sqlite_path.expanduser().resolve()
        self._compiler_version = compiler_version
        self._policy_version = policy_version
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(
        cls,
        path: str | Path,
        *,
        compiler_version: str = DEFAULT_COMPILER_VERSION,
        policy_version: str = DEFAULT_POLICY_VERSION,
    ) -> DigitalSelfRegistry:
        return cls(
            Path(path),
            compiler_version=compiler_version,
            policy_version=policy_version,
        )

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            SelfModelRegistry.sqlite(self._path).initialize()
            PersonaEngine.sqlite(self._path).initialize()
            MemoryCatalog.sqlite(
                self._path,
                extractor=RuleBasedMemoryExtractor(),
            ).initialize()
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

    async def build(
        self,
        *,
        account_id: str,
        parent_version_id: str | None = None,
        expected_source_summary_sha256: str | None = None,
    ) -> DigitalSelfVersion:
        self._require_account(account_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = self._parent_row(connection, account_id, parent_version_id)
            entries, persona_version_id, voice_profile = self._source_entries(
                connection, account_id
            )
            manifest, manifest_bytes, manifest_sha256 = build_manifest(
                entries,
                compiler_version=self._compiler_version,
                policy_version=self._policy_version,
                persona_version_id=persona_version_id,
                parent_version_id=(str(parent["version_id"]) if parent is not None else None),
                expected_source_summary_sha256=expected_source_summary_sha256,
                voice_profile=voice_profile,
            )
            version = self._insert(
                connection,
                account_id=account_id,
                manifest_bytes=manifest_bytes,
                manifest_sha256=manifest_sha256,
                source_summary_sha256=manifest.source_summary.source_summary_sha256,
                parent_version_id=manifest.parent_version_id,
                rollback_target_version_id=None,
            )
            self._append_lifecycle_audit(
                connection,
                account_id=account_id,
                action="build",
                version=version,
                from_status=None,
                to_status="draft",
                new_version_id=version.version_id,
            )
            return version

    async def get(self, *, account_id: str, version_id: str) -> DigitalSelfVersion:
        self._require_account(account_id)
        with self._connect() as connection:
            return self._version_from_row(self._required_row(connection, account_id, version_id))

    async def list(self, *, account_id: str) -> tuple[DigitalSelfVersion, ...]:
        self._require_account(account_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM digital_self_versions
                WHERE account_id = ? ORDER BY version_number DESC
                """,
                (account_id,),
            ).fetchall()
            return tuple(self._version_from_row(row) for row in rows)

    async def begin_testing(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return self._transition(
            account_id=account_id,
            version_id=version_id,
            action="begin_testing",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def approve(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return self._transition(
            account_id=account_id,
            version_id=version_id,
            action="approve",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def freeze(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return self._transition(
            account_id=account_id,
            version_id=version_id,
            action="freeze",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def revoke(
        self,
        *,
        account_id: str,
        version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        return self._transition(
            account_id=account_id,
            version_id=version_id,
            action="revoke",
            expected_manifest_sha256=expected_manifest_sha256,
        )

    async def rollback(
        self,
        *,
        account_id: str,
        target_version_id: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        self._require_account(account_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = self._version_from_row(
                self._required_row(connection, account_id, target_version_id)
            )
            self._check_digest(target, expected_manifest_sha256)
            parent_row = self._parent_row(connection, account_id, None)
            parent_id = str(parent_row["version_id"]) if parent_row is not None else None
            manifest, manifest_bytes, manifest_sha256 = build_manifest(
                target.manifest.entries,
                compiler_version=self._compiler_version,
                policy_version=self._policy_version,
                persona_version_id=target.manifest.source_summary.persona_version_id,
                parent_version_id=parent_id,
                rollback_target_version_id=target.version_id,
                voice_profile=target.manifest.source_summary.voice_profile,
            )
            version = self._insert(
                connection,
                account_id=account_id,
                manifest_bytes=manifest_bytes,
                manifest_sha256=manifest_sha256,
                source_summary_sha256=manifest.source_summary.source_summary_sha256,
                parent_version_id=parent_id,
                rollback_target_version_id=target.version_id,
            )
            self._append_lifecycle_audit(
                connection,
                account_id=account_id,
                action="rollback",
                version=version,
                from_status=None,
                to_status=version.status,
                target_version_id=target.version_id,
                new_version_id=version.version_id,
            )
            return version

    def _transition(
        self,
        *,
        account_id: str,
        version_id: str,
        action: str,
        expected_manifest_sha256: str,
    ) -> DigitalSelfVersion:
        self._require_account(account_id)
        source_status, target_status = _TRANSITIONS[action]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._version_from_row(self._required_row(connection, account_id, version_id))
            self._check_digest(current, expected_manifest_sha256)
            if current.status != source_status:
                raise InvalidVersionTransitionError(
                    f"cannot {action} a digital self version in {current.status}"
                )
            connection.execute(
                """
                UPDATE digital_self_versions SET status = ?
                WHERE account_id = ? AND version_id = ?
                """,
                (target_status, account_id, version_id),
            )
            version = self._version_from_row(self._required_row(connection, account_id, version_id))
            self._append_lifecycle_audit(
                connection,
                account_id=account_id,
                action=action,
                version=version,
                from_status=source_status,
                to_status=target_status,
            )
            return version

    def _source_entries(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> tuple[
        builtins.list[ManifestEntry],
        str | None,
        VoiceProfileManifestRef | None,
    ]:
        voice_profile = self._voice_profile_ref(connection, account_id)
        negative_targets = self._negative_targets(connection, account_id)
        memory_rows = connection.execute(
            """
            SELECT claim.*, source.event_type AS source_type,
                   source.speaker_class AS source_speaker_class,
                   source.source AS source_origin, source.occurred_at AS source_occurred_at,
                   source.payload_json AS source_payload_json
            FROM memory_claims AS claim
            JOIN evidence_events AS source
              ON source.event_id = claim.source_event_id
             AND source.account_id = claim.account_id
            WHERE claim.account_id = ?
              AND claim.status = 'confirmed'
              AND source.speaker_class = 'owner'
            ORDER BY claim.claim_id
            """,
            (account_id,),
        ).fetchall()
        entries: builtins.list[ManifestEntry] = []
        for row in memory_rows:
            source_event_id = str(row["source_event_id"])
            if ("memory_claim", str(row["claim_id"])) in negative_targets or (
                "source_event",
                source_event_id,
            ) in negative_targets:
                continue
            if confirmed_projection_contribution_for(
                EvidenceEvent(
                    event_id=source_event_id,
                    account_id=account_id,
                    event_type=str(row["source_type"]),
                    occurred_at=datetime.fromisoformat(str(row["source_occurred_at"])),
                    speaker_class=cast(SpeakerClass, row["source_speaker_class"]),
                    source=str(row["source_origin"]),
                    payload=json.loads(str(row["source_payload_json"])),
                )
            ).accepted:
                entries.append(memory_entry(dict(row)))

        persona_row = connection.execute(
            """
            SELECT version_id, snapshot_json
            FROM persona_versions
            WHERE account_id = ? AND status = 'active'
            """,
            (account_id,),
        ).fetchone()
        if persona_row is None:
            entries.extend(self._self_model_entries(connection, account_id))
            return entries, None, voice_profile
        persona_version_id = str(persona_row["version_id"])
        try:
            snapshot = json.loads(str(persona_row["snapshot_json"]))
        except json.JSONDecodeError as exc:
            raise SourceSnapshotConflictError("active persona snapshot is invalid") from exc
        if not isinstance(snapshot, list):
            raise SourceSnapshotConflictError("active persona snapshot is invalid")
        seen: set[str] = set()
        for raw_item in snapshot:
            if not isinstance(raw_item, dict):
                raise SourceSnapshotConflictError("active persona snapshot is invalid")
            item = cast(dict[str, object], raw_item)
            trait_id = str(item.get("trait_id") or "")
            if not trait_id or trait_id in seen:
                raise SourceSnapshotConflictError("active persona snapshot has duplicate traits")
            seen.add(trait_id)
            trait = connection.execute(
                """
                SELECT category, description, counterexample
                FROM persona_traits
                WHERE account_id = ? AND trait_id = ? AND status = 'confirmed'
                """,
                (account_id, trait_id),
            ).fetchone()
            if trait is None:
                continue
            if (
                str(trait["category"]) != str(item.get("category") or "")
                or str(trait["description"]) != str(item.get("description") or "")
                or str(trait["counterexample"]) != str(item.get("counterexample") or "")
            ):
                raise SourceSnapshotConflictError(
                    "active persona trait conflicts with its snapshot"
                )
            if str(trait["category"]) in LEGACY_COGNITIVE_TRAIT_CATEGORIES:
                continue
            entry = persona_entry(item, persona_version_id=persona_version_id)
            if not entry.source_event_ids:
                continue
            if ("persona_trait", entry.trait_id) in negative_targets or any(
                ("source_event", source_event_id) in negative_targets
                for source_event_id in entry.source_event_ids
            ):
                continue
            placeholders = ",".join("?" for _ in entry.source_event_ids)
            evidence = connection.execute(
                f"""
                SELECT event_id, speaker_class, event_type, source, occurred_at, payload_json
                FROM evidence_events
                WHERE account_id = ? AND event_id IN ({placeholders})
                """,
                (account_id, *entry.source_event_ids),
            ).fetchall()
            if len(evidence) != len(entry.source_event_ids) or any(
                not confirmed_projection_contribution_for(
                    EvidenceEvent(
                        event_id=str(row["event_id"]),
                        account_id=account_id,
                        event_type=str(row["event_type"]),
                        occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                        speaker_class=cast(SpeakerClass, row["speaker_class"]),
                        source=str(row["source"]),
                        payload=json.loads(str(row["payload_json"])),
                    )
                ).accepted
                for row in evidence
            ):
                continue
            entries.append(entry)
        entries.extend(self._self_model_entries(connection, account_id))
        return entries, persona_version_id, voice_profile

    @staticmethod
    def _voice_profile_ref(
        connection: sqlite3.Connection,
        account_id: str,
    ) -> VoiceProfileManifestRef | None:
        tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name IN ('voice_clone_consents', 'voice_profiles')
                """
            ).fetchall()
        }
        if tables != {"voice_clone_consents", "voice_profiles"}:
            return None
        row = connection.execute(
            """
            SELECT profile_id, version_number, provider, target_model,
                   provider_voice_id, provider_expires_at
            FROM voice_profiles
            WHERE account_id = ?
              AND status = 'active'
              AND evaluation_status = 'passed'
              AND quality_status = 'passed'
              AND provider = 'volcengine_doubao'
              AND target_model = 'seed-icl-2.0'
              AND provider_voice_id IS NOT NULL
              AND provider_expires_at IS NOT NULL
              AND provider_expires_at > ?
              AND EXISTS (
                  SELECT 1 FROM voice_clone_consents
                  WHERE account_id = ? AND revoked_at IS NULL
              )
            """,
            (account_id, datetime.now(UTC).isoformat(), account_id),
        ).fetchone()
        if row is None:
            return None
        try:
            expires_at = datetime.fromisoformat(str(row["provider_expires_at"])).isoformat()
            return VoiceProfileManifestRef(
                profile_id=str(row["profile_id"]),
                version_number=int(row["version_number"]),
                provider=str(row["provider"]),
                target_model=str(row["target_model"]),
                resource_id=str(row["target_model"]),
                provider_expires_at=expires_at,
                speaker_sha256=hashlib.sha256(
                    str(row["provider_voice_id"]).encode("utf-8")
                ).hexdigest(),
            )
        except (TypeError, ValueError):
            return None

    def _self_model_entries(
        self,
        connection: sqlite3.Connection,
        account_id: str,
    ) -> builtins.list[ManifestEntry]:
        entries: builtins.list[ManifestEntry] = []
        claim_rows = connection.execute(
            """
            SELECT * FROM self_model_cognitive_claims
            WHERE account_id = ? ORDER BY claim_id
            """,
            (account_id,),
        ).fetchall()
        for row in claim_rows:
            claim_id = str(row["claim_id"])
            claim = CognitiveClaim(
                claim_id=claim_id,
                account_id=account_id,
                claim_type=cast(CognitiveClaimType, str(row["claim_type"])),
                statement=str(row["statement"]),
                context=str(row["context"]),
                confidence=float(row["confidence"]),
                sharing_scope=str(row["sharing_scope"]),
                status=cast(ItemStatus, str(row["status"])),
                unresolved_conflict=bool(row["unresolved_conflict"]),
                sources=self._self_model_sources(
                    connection, account_id, "cognitive_claim", claim_id, 0
                ),
                owner_reviewed_at=_optional_datetime(row["owner_reviewed_at"]),
                step_up_verified=bool(row["step_up_verified"]),
                version=int(row["version"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            if is_effective(claim):
                entries.append(cognitive_entry(claim))

        decision_rows = connection.execute(
            """
            SELECT * FROM self_model_decision_cases
            WHERE account_id = ? ORDER BY case_id
            """,
            (account_id,),
        ).fetchall()
        for row in decision_rows:
            case_id = str(row["case_id"])
            decision = DecisionCase(
                case_id=case_id,
                account_id=account_id,
                kind=cast(DecisionKind, str(row["kind"])),
                context=str(row["context"]),
                options=_json_strings(row["options_json"]),
                constraints=_json_strings(row["constraints_json"]),
                chosen_option=str(row["chosen_option"]),
                rejected_options=_json_strings(row["rejected_options_json"]),
                outcome=str(row["outcome"]),
                reflection=str(row["reflection"]),
                still_endorsed=bool(row["still_endorsed"]),
                sharing_scope=str(row["sharing_scope"]),
                status=cast(ItemStatus, str(row["status"])),
                unresolved_conflict=bool(row["unresolved_conflict"]),
                sources=self._self_model_sources(
                    connection, account_id, "decision_case", case_id, 0
                ),
                owner_reviewed_at=_optional_datetime(row["owner_reviewed_at"]),
                step_up_verified=bool(row["step_up_verified"]),
                version=int(row["version"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
            )
            if is_effective(decision):
                entries.append(decision_entry(decision))

        profile_rows = connection.execute(
            """
            SELECT * FROM self_model_relationship_profiles
            WHERE account_id = ? ORDER BY profile_id, version_number
            """,
            (account_id,),
        ).fetchall()
        for row in profile_rows:
            profile_id = str(row["profile_id"])
            version_number = int(row["version_number"])
            profile = RelationshipProfile(
                profile_id=profile_id,
                account_id=account_id,
                version_number=version_number,
                person_id=str(row["person_id"]),
                relationship_id=str(row["relationship_id"]),
                salutation=str(row["salutation"]),
                tone=str(row["tone"]),
                advice_style=str(row["advice_style"]),
                sharing_scope=str(row["sharing_scope"]),
                boundaries=_json_strings(row["boundaries_json"]),
                status=cast(RelationshipProfileStatus, str(row["status"])),
                unresolved_conflict=bool(row["unresolved_conflict"]),
                sources=self._self_model_sources(
                    connection,
                    account_id,
                    "relationship_profile",
                    profile_id,
                    version_number,
                ),
                owner_reviewed_at=_optional_datetime(row["owner_reviewed_at"]),
                step_up_verified=bool(row["step_up_verified"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
            )
            if is_effective(profile):
                entries.append(relationship_entry(profile))
        return entries

    @staticmethod
    def _self_model_sources(
        connection: sqlite3.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        item_version: int,
    ) -> tuple[SelfModelSource, ...]:
        rows = connection.execute(
            """
            SELECT source.source_event_id, source.relation, source.adopted,
                   source.negative, evidence.speaker_class, evidence.occurred_at
            FROM self_model_sources AS source
            JOIN evidence_events AS evidence
              ON evidence.event_id = source.source_event_id
             AND evidence.account_id = source.account_id
            WHERE source.account_id = ? AND source.item_kind = ?
              AND source.item_id = ? AND source.item_version = ?
            ORDER BY source.source_event_id, source.relation
            """,
            (account_id, item_kind, item_id, item_version),
        ).fetchall()
        return tuple(
            SelfModelSource(
                source_event_id=str(row["source_event_id"]),
                relation=cast(SourceRelation, str(row["relation"])),
                adopted=bool(row["adopted"]),
                negative=bool(row["negative"]),
                speaker_class=cast(SpeakerClass, str(row["speaker_class"])),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            )
            for row in rows
        )

    @staticmethod
    def _negative_targets(
        connection: sqlite3.Connection,
        account_id: str,
    ) -> set[tuple[str, str]]:
        rows = connection.execute(
            """
            SELECT payload_json FROM evidence_events
            WHERE account_id = ? AND event_type = 'owner.action_recorded'
            """,
            (account_id,),
        ).fetchall()
        targets: set[tuple[str, str]] = set()
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if payload.get("action_type") not in {
                "not_me",
                "would_not_say",
                "not_like_me",
                "correction",
            }:
                continue
            target_kind = str(payload.get("target_kind") or "")
            target_id = str(payload.get("target_id") or "")
            if target_kind and target_id:
                targets.add((target_kind, target_id))
            for source_event_id in payload.get("target_source_event_ids", ()):
                if isinstance(source_event_id, str) and source_event_id:
                    targets.add(("source_event", source_event_id))
        return targets

    def _parent_row(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        parent_version_id: str | None,
    ) -> sqlite3.Row | None:
        if parent_version_id is not None:
            row = self._required_row(connection, account_id, parent_version_id)
            self._version_from_row(row)
            return row
        latest_row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT * FROM digital_self_versions
                WHERE account_id = ? ORDER BY version_number DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone(),
        )
        if latest_row is not None:
            self._version_from_row(latest_row)
        return latest_row

    def _insert(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        manifest_bytes: bytes,
        manifest_sha256: str,
        source_summary_sha256: str,
        parent_version_id: str | None,
        rollback_target_version_id: str | None,
    ) -> DigitalSelfVersion:
        version_id = str(uuid.uuid4())
        version_number = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM digital_self_versions WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()[0]
        )
        created_at = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO digital_self_versions (
                version_id, account_id, version_number, status, manifest_json,
                manifest_sha256, source_summary_sha256, parent_version_id,
                rollback_target_version_id, created_at
            ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                account_id,
                version_number,
                manifest_bytes.decode("utf-8"),
                manifest_sha256,
                source_summary_sha256,
                parent_version_id,
                rollback_target_version_id,
                created_at,
            ),
        )
        return self._version_from_row(self._required_row(connection, account_id, version_id))

    @staticmethod
    def _append_lifecycle_audit(
        connection: sqlite3.Connection,
        *,
        account_id: str,
        action: str,
        version: DigitalSelfVersion,
        from_status: VersionStatus | None,
        to_status: VersionStatus,
        target_version_id: str | None = None,
        new_version_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO digital_self_lifecycle_audit_events (
                event_id, account_id, actor_account_id, action, version_id,
                manifest_sha256, from_status, to_status, target_version_id,
                new_version_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                account_id,
                account_id,
                action,
                version.version_id,
                version.manifest_sha256,
                from_status,
                to_status,
                target_version_id,
                new_version_id,
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _required_row(
        connection: sqlite3.Connection,
        account_id: str,
        version_id: str,
    ) -> sqlite3.Row:
        row = cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT * FROM digital_self_versions
                WHERE account_id = ? AND version_id = ?
                """,
                (account_id, version_id),
            ).fetchone(),
        )
        if row is None:
            raise VersionNotFoundError(version_id)
        return row

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> DigitalSelfVersion:
        parent_id = str(row["parent_version_id"]) if row["parent_version_id"] else None
        rollback_id = (
            str(row["rollback_target_version_id"]) if row["rollback_target_version_id"] else None
        )
        manifest = decode_manifest(
            str(row["manifest_json"]).encode("utf-8"),
            expected_manifest_sha256=str(row["manifest_sha256"]),
            expected_source_summary_sha256=str(row["source_summary_sha256"]),
            expected_parent_version_id=parent_id,
            expected_rollback_target_version_id=rollback_id,
        )
        return DigitalSelfVersion(
            version_id=str(row["version_id"]),
            account_id=str(row["account_id"]),
            version_number=int(row["version_number"]),
            status=cast(VersionStatus, str(row["status"])),
            manifest=manifest,
            manifest_sha256=str(row["manifest_sha256"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _check_digest(
        version: DigitalSelfVersion,
        expected_manifest_sha256: str,
    ) -> None:
        if not isinstance(expected_manifest_sha256, str) or not _SHA256_HEX.fullmatch(
            expected_manifest_sha256
        ):
            raise ValueError("expected_manifest_sha256 must be exactly 64 hexadecimal characters")
        if expected_manifest_sha256 != version.manifest_sha256:
            raise SourceSnapshotConflictError("manifest changed before transition")

    @staticmethod
    def _require_account(account_id: str) -> None:
        if not account_id.strip():
            raise ValueError("digital self account_id is required")
