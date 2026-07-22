"""SQLite registry for evidence-backed self-model records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from services.archive.domain import SpeakerClass
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.self_model.domain import (
    CognitiveClaim,
    CognitiveClaimType,
    DecisionCase,
    DecisionKind,
    InvalidSelfModelTransitionError,
    ItemStatus,
    RelationshipProfile,
    RelationshipProfileStatus,
    RelationshipReferenceError,
    SelfModelIdempotencyConflictError,
    SelfModelItem,
    SelfModelItemKind,
    SelfModelNotFoundError,
    SelfModelSource,
    SelfModelVersionConflictError,
    SourceInput,
    SourceRelation,
    UntrustedSelfModelSourceError,
)
from services.self_model.policy import HIGH_SENSITIVITY_CLAIM_TYPES, is_effective

_SCHEMA = """
CREATE TABLE IF NOT EXISTS self_model_cognitive_claims (
    claim_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    claim_type TEXT NOT NULL CHECK (
        claim_type IN (
            'belief', 'preference', 'value', 'decision_rule', 'red_line',
            'uncertainty', 'conflict', 'support'
        )
    ),
    statement TEXT NOT NULL,
    context TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    sharing_scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted', 'superseded')
    ),
    unresolved_conflict INTEGER NOT NULL DEFAULT 0 CHECK (unresolved_conflict IN (0, 1)),
    owner_reviewed_at TEXT,
    step_up_verified INTEGER NOT NULL DEFAULT 0 CHECK (step_up_verified IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, claim_id)
);

CREATE INDEX IF NOT EXISTS idx_self_model_claim_account_status
ON self_model_cognitive_claims(account_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS self_model_decision_cases (
    case_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('real', 'hypothetical')),
    context TEXT NOT NULL,
    options_json TEXT NOT NULL,
    constraints_json TEXT NOT NULL,
    chosen_option TEXT NOT NULL,
    rejected_options_json TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reflection TEXT NOT NULL,
    still_endorsed INTEGER NOT NULL CHECK (still_endorsed IN (0, 1)),
    sharing_scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted', 'superseded')
    ),
    unresolved_conflict INTEGER NOT NULL DEFAULT 0 CHECK (unresolved_conflict IN (0, 1)),
    owner_reviewed_at TEXT,
    step_up_verified INTEGER NOT NULL DEFAULT 0 CHECK (step_up_verified IN (0, 1)),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (account_id, case_id)
);

CREATE INDEX IF NOT EXISTS idx_self_model_decision_account_status
ON self_model_decision_cases(account_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS self_model_relationship_profiles (
    profile_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    person_id TEXT NOT NULL,
    relationship_id TEXT NOT NULL,
    salutation TEXT NOT NULL,
    tone TEXT NOT NULL,
    advice_style TEXT NOT NULL,
    sharing_scope TEXT NOT NULL,
    boundaries_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'approved', 'revoked', 'superseded')
    ),
    unresolved_conflict INTEGER NOT NULL DEFAULT 0 CHECK (unresolved_conflict IN (0, 1)),
    owner_reviewed_at TEXT,
    step_up_verified INTEGER NOT NULL DEFAULT 0 CHECK (step_up_verified IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, version_number),
    UNIQUE (account_id, profile_id, version_number),
    FOREIGN KEY (person_id) REFERENCES person_entities(person_id) ON DELETE CASCADE,
    FOREIGN KEY (relationship_id) REFERENCES relationships(relationship_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_self_model_relationship_account_status
ON self_model_relationship_profiles(account_id, status, created_at DESC);

CREATE TRIGGER IF NOT EXISTS self_model_relationship_content_immutable
BEFORE UPDATE OF account_id, version_number, person_id, relationship_id,
                 salutation, tone, advice_style, sharing_scope,
                 boundaries_json, created_at
ON self_model_relationship_profiles
BEGIN
    SELECT RAISE(ABORT, 'relationship profile content is immutable');
END;

CREATE TABLE IF NOT EXISTS self_model_sources (
    item_kind TEXT NOT NULL CHECK (
        item_kind IN ('cognitive_claim', 'decision_case', 'relationship_profile')
    ),
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL DEFAULT 0 CHECK (item_version >= 0),
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('support', 'counterexample')),
    adopted INTEGER NOT NULL DEFAULT 0 CHECK (adopted IN (0, 1)),
    negative INTEGER NOT NULL DEFAULT 0 CHECK (negative IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        item_kind, item_id, item_version, source_event_id, relation
    ),
    FOREIGN KEY (source_event_id) REFERENCES evidence_events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_self_model_sources_account_item
ON self_model_sources(account_id, item_kind, item_id, item_version);

CREATE TABLE IF NOT EXISTS self_model_audit_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    actor_account_id TEXT NOT NULL CHECK (actor_account_id = account_id),
    event_type TEXT NOT NULL,
    item_kind TEXT NOT NULL CHECK (
        item_kind IN ('cognitive_claim', 'decision_case', 'relationship_profile')
    ),
    item_id TEXT NOT NULL,
    item_version INTEGER NOT NULL CHECK (item_version > 0),
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_self_model_audit_account_occurred
ON self_model_audit_events(account_id, occurred_at);

CREATE TRIGGER IF NOT EXISTS self_model_audit_immutable
BEFORE UPDATE ON self_model_audit_events
BEGIN
    SELECT RAISE(ABORT, 'self model audit events are immutable');
END;

CREATE TABLE IF NOT EXISTS self_model_command_receipts (
    account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_type TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    result_kind TEXT NOT NULL CHECK (
        result_kind IN ('cognitive_claim', 'decision_case', 'relationship_profile')
    ),
    result_id TEXT NOT NULL,
    result_version INTEGER NOT NULL CHECK (result_version > 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, idempotency_key)
);
"""

_ITEM_TRANSITIONS: dict[ItemStatus, frozenset[ItemStatus]] = {
    "candidate": frozenset({"confirmed", "disputed", "retracted", "superseded"}),
    "confirmed": frozenset({"disputed", "retracted", "superseded"}),
    "disputed": frozenset({"confirmed", "retracted", "superseded"}),
    "retracted": frozenset(),
    "superseded": frozenset(),
}


def _required(value: str, name: str, *, maximum: int = 4000) -> str:
    result = value.strip()
    if not result or len(result) > maximum:
        raise ValueError(f"{name} must contain 1..{maximum} characters")
    return result


def _optional(value: str, name: str, *, maximum: int = 4000) -> str:
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{name} must not exceed {maximum} characters")
    return result


def _strings(values: tuple[str, ...], name: str, *, required: bool = False) -> tuple[str, ...]:
    result = tuple(_required(value, name, maximum=1000) for value in values)
    if required and not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _command_digest(command_type: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"command_type": command_type, **payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _source_inputs(sources: tuple[SourceInput, ...]) -> tuple[SourceInput, ...]:
    normalized: list[SourceInput] = []
    seen: set[str] = set()
    for source in sources:
        source_event_id = _required(source.source_event_id, "source_event_id", maximum=128)
        if source.relation not in {"support", "counterexample"}:
            raise ValueError("source relation is invalid")
        if source.adopted and source.relation != "support":
            raise ValueError("only supporting evidence can be adopted")
        if source_event_id in seen:
            raise ValueError("duplicate source")
        seen.add(source_event_id)
        normalized.append(
            SourceInput(
                source_event_id=source_event_id,
                relation=source.relation,
                adopted=source.adopted,
                negative=source.negative,
            )
        )
    return tuple(normalized)


def _source_payloads(sources: tuple[SourceInput, ...]) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "source_event_id": source.source_event_id,
            "relation": source.relation,
            "adopted": source.adopted,
            "negative": source.negative,
        }
        for source in sources
    )


class SelfModelRegistry:
    def __init__(self, sqlite_path: Path) -> None:
        self._path = sqlite_path.expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @classmethod
    def sqlite(cls, path: str | Path) -> SelfModelRegistry:
        return cls(Path(path))

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            LifeArchive.sqlite(self._path).initialize()
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

    async def create_cognitive_claim(
        self,
        *,
        account_id: str,
        claim_type: CognitiveClaimType,
        statement: str,
        confidence: float,
        idempotency_key: str,
        context: str = "",
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> CognitiveClaim:
        self._validate_common(account_id, idempotency_key, confidence, sharing_scope)
        statement = _required(statement, "statement")
        context = _optional(context, "context")
        sources = _source_inputs(sources)
        payload: dict[str, object] = {
            "claim_type": claim_type,
            "statement": statement,
            "context": context,
            "confidence": confidence,
            "sharing_scope": sharing_scope,
            "unresolved_conflict": unresolved_conflict,
        }
        if sources:
            payload["sources"] = _source_payloads(sources)
        digest = _command_digest("create_cognitive_claim", payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(connection, account_id, idempotency_key, digest)
            if duplicate is not None:
                return cast(CognitiveClaim, duplicate)
            now = datetime.now(UTC).isoformat()
            claim_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO self_model_cognitive_claims (
                    claim_id, account_id, claim_type, statement, context,
                    confidence, sharing_scope, unresolved_conflict, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_id,
                    account_id,
                    claim_type,
                    statement,
                    context,
                    confidence,
                    sharing_scope,
                    unresolved_conflict,
                    now,
                    now,
                ),
            )
            self._append_audit(
                connection,
                account_id,
                "cognitive_claim.created",
                "cognitive_claim",
                claim_id,
                1,
                payload,
                now,
            )
            result_version = 1
            for source in sources:
                result_version = self._add_source_in_transaction(
                    connection,
                    account_id=account_id,
                    item_kind="cognitive_claim",
                    item_id=claim_id,
                    source=source,
                    expected_version=result_version,
                    occurred_at=now,
                )
            self._record_receipt(
                connection,
                account_id,
                idempotency_key,
                "create_cognitive_claim",
                digest,
                "cognitive_claim",
                claim_id,
                result_version,
                now,
            )
            return self._claim(connection, account_id, claim_id)

    async def create_decision_case(
        self,
        *,
        account_id: str,
        kind: DecisionKind,
        context: str,
        options: tuple[str, ...],
        constraints: tuple[str, ...],
        chosen_option: str,
        rejected_options: tuple[str, ...],
        outcome: str,
        reflection: str,
        still_endorsed: bool,
        idempotency_key: str,
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> DecisionCase:
        self._validate_common(account_id, idempotency_key, 1.0, sharing_scope)
        context = _required(context, "context")
        options = _strings(options, "option", required=True)
        constraints = _strings(constraints, "constraint")
        chosen_option = _required(chosen_option, "chosen_option", maximum=1000)
        rejected_options = _strings(rejected_options, "rejected_option")
        outcome = _optional(outcome, "outcome")
        reflection = _optional(reflection, "reflection")
        sources = _source_inputs(sources)
        if chosen_option not in options:
            raise ValueError("chosen_option must be one of options")
        payload: dict[str, object] = {
            "kind": kind,
            "context": context,
            "options": options,
            "constraints": constraints,
            "chosen_option": chosen_option,
            "rejected_options": rejected_options,
            "outcome": outcome,
            "reflection": reflection,
            "still_endorsed": still_endorsed,
            "sharing_scope": sharing_scope,
            "unresolved_conflict": unresolved_conflict,
        }
        if sources:
            payload["sources"] = _source_payloads(sources)
        digest = _command_digest("create_decision_case", payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(connection, account_id, idempotency_key, digest)
            if duplicate is not None:
                return cast(DecisionCase, duplicate)
            now = datetime.now(UTC).isoformat()
            case_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO self_model_decision_cases (
                    case_id, account_id, kind, context, options_json,
                    constraints_json, chosen_option, rejected_options_json,
                    outcome, reflection, still_endorsed, sharing_scope,
                    unresolved_conflict, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    case_id,
                    account_id,
                    kind,
                    context,
                    json.dumps(options, ensure_ascii=False),
                    json.dumps(constraints, ensure_ascii=False),
                    chosen_option,
                    json.dumps(rejected_options, ensure_ascii=False),
                    outcome,
                    reflection,
                    still_endorsed,
                    sharing_scope,
                    unresolved_conflict,
                    now,
                    now,
                ),
            )
            self._append_audit(
                connection,
                account_id,
                "decision_case.created",
                "decision_case",
                case_id,
                1,
                payload,
                now,
            )
            result_version = 1
            for source in sources:
                result_version = self._add_source_in_transaction(
                    connection,
                    account_id=account_id,
                    item_kind="decision_case",
                    item_id=case_id,
                    source=source,
                    expected_version=result_version,
                    occurred_at=now,
                )
            self._record_receipt(
                connection,
                account_id,
                idempotency_key,
                "create_decision_case",
                digest,
                "decision_case",
                case_id,
                result_version,
                now,
            )
            return self._decision(connection, account_id, case_id)

    async def create_relationship_profile(
        self,
        *,
        account_id: str,
        person_id: str,
        relationship_id: str,
        salutation: str,
        tone: str,
        advice_style: str,
        boundaries: tuple[str, ...],
        idempotency_key: str,
        sharing_scope: str = "private",
        unresolved_conflict: bool = False,
        sources: tuple[SourceInput, ...] = (),
    ) -> RelationshipProfile:
        self._validate_common(account_id, idempotency_key, 1.0, sharing_scope)
        person_id = _required(person_id, "person_id", maximum=128)
        relationship_id = _required(relationship_id, "relationship_id", maximum=128)
        salutation = _required(salutation, "salutation", maximum=256)
        tone = _required(tone, "tone", maximum=1000)
        advice_style = _required(advice_style, "advice_style", maximum=1000)
        boundaries = _strings(boundaries, "boundary")
        sources = _source_inputs(sources)
        payload: dict[str, object] = {
            "person_id": person_id,
            "relationship_id": relationship_id,
            "salutation": salutation,
            "tone": tone,
            "advice_style": advice_style,
            "boundaries": boundaries,
            "sharing_scope": sharing_scope,
            "unresolved_conflict": unresolved_conflict,
        }
        if sources:
            payload["sources"] = _source_payloads(sources)
        digest = _command_digest("create_relationship_profile", payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(connection, account_id, idempotency_key, digest)
            if duplicate is not None:
                return cast(RelationshipProfile, duplicate)
            self._require_relationship_refs(connection, account_id, person_id, relationship_id)
            now = datetime.now(UTC).isoformat()
            profile_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO self_model_relationship_profiles (
                    profile_id, account_id, version_number, person_id,
                    relationship_id, salutation, tone, advice_style,
                    sharing_scope, boundaries_json, unresolved_conflict, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    account_id,
                    person_id,
                    relationship_id,
                    salutation,
                    tone,
                    advice_style,
                    sharing_scope,
                    json.dumps(boundaries, ensure_ascii=False),
                    unresolved_conflict,
                    now,
                ),
            )
            self._append_audit(
                connection,
                account_id,
                "relationship_profile.created",
                "relationship_profile",
                profile_id,
                1,
                payload,
                now,
            )
            for source in sources:
                self._add_source_in_transaction(
                    connection,
                    account_id=account_id,
                    item_kind="relationship_profile",
                    item_id=profile_id,
                    source=source,
                    expected_version=1,
                    occurred_at=now,
                )
            self._record_receipt(
                connection,
                account_id,
                idempotency_key,
                "create_relationship_profile",
                digest,
                "relationship_profile",
                profile_id,
                1,
                now,
            )
            return self._profile(connection, account_id, profile_id, 1)

    async def add_source(
        self,
        *,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        source_event_id: str,
        relation: SourceRelation,
        adopted: bool,
        negative: bool,
        expected_version: int,
        idempotency_key: str,
    ) -> SelfModelItem:
        self._validate_ids(account_id, idempotency_key)
        if expected_version < 1:
            raise ValueError("expected_version must be positive")
        source = _source_inputs(
            (
                SourceInput(
                    source_event_id=source_event_id,
                    relation=relation,
                    adopted=adopted,
                    negative=negative,
                ),
            )
        )[0]
        payload: dict[str, object] = {
            "item_kind": item_kind,
            "item_id": item_id,
            **_source_payloads((source,))[0],
            "expected_version": expected_version,
        }
        digest = _command_digest("add_source", payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(connection, account_id, idempotency_key, digest)
            if duplicate is not None:
                return duplicate
            now = datetime.now(UTC).isoformat()
            result_version = self._add_source_in_transaction(
                connection,
                account_id=account_id,
                item_kind=item_kind,
                item_id=item_id,
                source=source,
                expected_version=expected_version,
                occurred_at=now,
            )
            self._record_receipt(
                connection,
                account_id,
                idempotency_key,
                "add_source",
                digest,
                item_kind,
                item_id,
                result_version,
                now,
            )
            return self._item(connection, account_id, item_kind, item_id, result_version)

    async def review_cognitive_claim(
        self,
        *,
        account_id: str,
        claim_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> CognitiveClaim:
        return cast(
            CognitiveClaim,
            self._review_item(
                account_id=account_id,
                item_kind="cognitive_claim",
                item_id=claim_id,
                status=status,
                expected_version=expected_version,
                step_up_verified=step_up_verified,
                idempotency_key=idempotency_key,
            ),
        )

    async def review_decision_case(
        self,
        *,
        account_id: str,
        case_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> DecisionCase:
        return cast(
            DecisionCase,
            self._review_item(
                account_id=account_id,
                item_kind="decision_case",
                item_id=case_id,
                status=status,
                expected_version=expected_version,
                step_up_verified=step_up_verified,
                idempotency_key=idempotency_key,
            ),
        )

    def _review_item(
        self,
        *,
        account_id: str,
        item_kind: Literal["cognitive_claim", "decision_case"],
        item_id: str,
        status: ItemStatus,
        expected_version: int,
        step_up_verified: bool,
        idempotency_key: str,
    ) -> SelfModelItem:
        self._validate_ids(account_id, idempotency_key)
        payload: dict[str, object] = {
            "item_kind": item_kind,
            "item_id": item_id,
            "status": status,
            "expected_version": expected_version,
            "step_up_verified": step_up_verified,
        }
        digest = _command_digest("review_item", payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(connection, account_id, idempotency_key, digest)
            if duplicate is not None:
                return duplicate
            item = self._item(connection, account_id, item_kind, item_id, expected_version)
            current_status = item.status
            if status not in _ITEM_TRANSITIONS[cast(ItemStatus, current_status)]:
                raise InvalidSelfModelTransitionError(
                    f"cannot transition {current_status} to {status}"
                )
            if (
                isinstance(item, CognitiveClaim)
                and item.claim_type in HIGH_SENSITIVITY_CLAIM_TYPES
                and status == "confirmed"
            ):
                if not step_up_verified:
                    raise InvalidSelfModelTransitionError(
                        "high-sensitivity claims require owner step-up review"
                    )
                if not any(
                    source.speaker_class == "owner"
                    and source.relation == "counterexample"
                    and not source.negative
                    for source in item.sources
                ):
                    raise InvalidSelfModelTransitionError(
                        "high-sensitivity claims require an owner counterexample"
                    )
            now = datetime.now(UTC).isoformat()
            table, id_column = self._mutable_table(item_kind)
            cursor = connection.execute(
                f"""
                UPDATE {table}
                SET status = ?, owner_reviewed_at = ?, step_up_verified = ?,
                    version = version + 1, updated_at = ?
                WHERE account_id = ? AND {id_column} = ? AND version = ?
                """,
                (
                    status,
                    now,
                    step_up_verified,
                    now,
                    account_id,
                    item_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise SelfModelVersionConflictError(item_id)
            result_version = expected_version + 1
            self._append_audit(
                connection,
                account_id,
                f"{item_kind}.reviewed",
                item_kind,
                item_id,
                result_version,
                payload,
                now,
            )
            self._record_receipt(
                connection,
                account_id,
                idempotency_key,
                "review_item",
                digest,
                item_kind,
                item_id,
                result_version,
                now,
            )
            return self._item(connection, account_id, item_kind, item_id, result_version)

    async def review_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        version_number: int,
        status: Literal["approved", "revoked"],
        expected_status: Literal["candidate", "approved"],
        step_up_verified: bool,
        idempotency_key: str,
    ) -> RelationshipProfile:
        self._validate_ids(account_id, idempotency_key)
        if status == "approved" and not step_up_verified:
            raise InvalidSelfModelTransitionError(
                "relationship approval requires owner step-up review"
            )
        if (expected_status, status) not in {
            ("candidate", "approved"),
            ("approved", "revoked"),
        }:
            raise InvalidSelfModelTransitionError(
                f"cannot transition {expected_status} to {status}"
            )
        payload: dict[str, object] = {
            "profile_id": profile_id,
            "version_number": version_number,
            "status": status,
            "expected_status": expected_status,
            "step_up_verified": step_up_verified,
        }
        digest = _command_digest("review_relationship_profile", payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(connection, account_id, idempotency_key, digest)
            if duplicate is not None:
                return cast(RelationshipProfile, duplicate)
            self._profile(connection, account_id, profile_id, version_number)
            now = datetime.now(UTC).isoformat()
            cursor = connection.execute(
                """
                UPDATE self_model_relationship_profiles
                SET status = ?, owner_reviewed_at = ?, step_up_verified = ?
                WHERE account_id = ? AND profile_id = ? AND version_number = ?
                  AND status = ?
                """,
                (
                    status,
                    now,
                    step_up_verified,
                    account_id,
                    profile_id,
                    version_number,
                    expected_status,
                ),
            )
            if cursor.rowcount != 1:
                raise SelfModelVersionConflictError(profile_id)
            self._append_audit(
                connection,
                account_id,
                f"relationship_profile.{status}",
                "relationship_profile",
                profile_id,
                version_number,
                payload,
                now,
            )
            self._record_receipt(
                connection,
                account_id,
                idempotency_key,
                "review_relationship_profile",
                digest,
                "relationship_profile",
                profile_id,
                version_number,
                now,
            )
            return self._profile(connection, account_id, profile_id, version_number)

    async def revise_relationship_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
        expected_version: int,
        salutation: str,
        tone: str,
        advice_style: str,
        boundaries: tuple[str, ...],
        idempotency_key: str,
        sharing_scope: str,
        unresolved_conflict: bool = False,
    ) -> RelationshipProfile:
        self._validate_common(account_id, idempotency_key, 1.0, sharing_scope)
        salutation = _required(salutation, "salutation", maximum=256)
        tone = _required(tone, "tone", maximum=1000)
        advice_style = _required(advice_style, "advice_style", maximum=1000)
        boundaries = _strings(boundaries, "boundary")
        payload: dict[str, object] = {
            "profile_id": profile_id,
            "expected_version": expected_version,
            "salutation": salutation,
            "tone": tone,
            "advice_style": advice_style,
            "boundaries": boundaries,
            "sharing_scope": sharing_scope,
            "unresolved_conflict": unresolved_conflict,
        }
        digest = _command_digest("revise_relationship_profile", payload)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._duplicate_result(connection, account_id, idempotency_key, digest)
            if duplicate is not None:
                return cast(RelationshipProfile, duplicate)
            current = self._profile(connection, account_id, profile_id, expected_version)
            latest = connection.execute(
                """
                SELECT MAX(version_number) AS version_number
                FROM self_model_relationship_profiles
                WHERE account_id = ? AND profile_id = ?
                """,
                (account_id, profile_id),
            ).fetchone()
            if latest is None or int(latest["version_number"]) != expected_version:
                raise SelfModelVersionConflictError(profile_id)
            next_version = expected_version + 1
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                UPDATE self_model_relationship_profiles
                SET status = 'superseded'
                WHERE account_id = ? AND profile_id = ? AND version_number = ?
                """,
                (account_id, profile_id, expected_version),
            )
            connection.execute(
                """
                INSERT INTO self_model_relationship_profiles (
                    profile_id, account_id, version_number, person_id,
                    relationship_id, salutation, tone, advice_style,
                    sharing_scope, boundaries_json, unresolved_conflict, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    account_id,
                    next_version,
                    current.person_id,
                    current.relationship_id,
                    salutation,
                    tone,
                    advice_style,
                    sharing_scope,
                    json.dumps(boundaries, ensure_ascii=False),
                    unresolved_conflict,
                    now,
                ),
            )
            self._append_audit(
                connection,
                account_id,
                "relationship_profile.revised",
                "relationship_profile",
                profile_id,
                next_version,
                payload,
                now,
            )
            self._record_receipt(
                connection,
                account_id,
                idempotency_key,
                "revise_relationship_profile",
                digest,
                "relationship_profile",
                profile_id,
                next_version,
                now,
            )
            return self._profile(connection, account_id, profile_id, next_version)

    async def get_cognitive_claim(self, *, account_id: str, claim_id: str) -> CognitiveClaim:
        with self._connect() as connection:
            return self._claim(connection, account_id, claim_id)

    async def get_decision_case(self, *, account_id: str, case_id: str) -> DecisionCase:
        with self._connect() as connection:
            return self._decision(connection, account_id, case_id)

    async def get_relationship_profile(
        self, *, account_id: str, profile_id: str, version_number: int | None = None
    ) -> RelationshipProfile:
        with self._connect() as connection:
            return self._profile(connection, account_id, profile_id, version_number)

    async def cognitive_claims(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[CognitiveClaim, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT claim_id FROM self_model_cognitive_claims
                WHERE account_id = ? ORDER BY created_at, claim_id
                """,
                (account_id,),
            ).fetchall()
            values = tuple(
                self._claim(connection, account_id, str(row["claim_id"])) for row in rows
            )
            return tuple(value for value in values if not effective_only or is_effective(value))

    async def decision_cases(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[DecisionCase, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT case_id FROM self_model_decision_cases
                WHERE account_id = ? ORDER BY created_at, case_id
                """,
                (account_id,),
            ).fetchall()
            values = tuple(
                self._decision(connection, account_id, str(row["case_id"])) for row in rows
            )
            return tuple(value for value in values if not effective_only or is_effective(value))

    async def relationship_profiles(
        self, *, account_id: str, effective_only: bool = False
    ) -> tuple[RelationshipProfile, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT profile_id, version_number
                FROM self_model_relationship_profiles
                WHERE account_id = ?
                ORDER BY created_at, profile_id, version_number
                """,
                (account_id,),
            ).fetchall()
            values = tuple(
                self._profile(
                    connection,
                    account_id,
                    str(row["profile_id"]),
                    int(row["version_number"]),
                )
                for row in rows
            )
            return tuple(value for value in values if not effective_only or is_effective(value))

    async def export_account(self, account_id: str) -> dict[str, list[dict[str, object]]]:
        _required(account_id, "account_id", maximum=128)
        tables = (
            "self_model_cognitive_claims",
            "self_model_decision_cases",
            "self_model_relationship_profiles",
            "self_model_sources",
            "self_model_audit_events",
            "self_model_command_receipts",
        )
        exported: dict[str, list[dict[str, object]]] = {}
        with self._connect() as connection:
            for table in tables:
                rows = connection.execute(
                    f"SELECT * FROM {table} WHERE account_id = ? ORDER BY rowid",
                    (account_id,),
                ).fetchall()
                exported[table] = [dict(row) for row in rows]
        return exported

    async def delete_account(self, account_id: str) -> dict[str, int]:
        _required(account_id, "account_id", maximum=128)
        tables = (
            "self_model_command_receipts",
            "self_model_audit_events",
            "self_model_sources",
            "self_model_relationship_profiles",
            "self_model_decision_cases",
            "self_model_cognitive_claims",
        )
        deleted: dict[str, int] = {}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table in tables:
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE account_id = ?",
                    (account_id,),
                )
                deleted[table] = cursor.rowcount
        return deleted

    @staticmethod
    def _validate_ids(account_id: str, idempotency_key: str) -> None:
        _required(account_id, "account_id", maximum=128)
        _required(idempotency_key, "idempotency_key", maximum=256)

    @classmethod
    def _validate_common(
        cls,
        account_id: str,
        idempotency_key: str,
        confidence: float,
        sharing_scope: str,
    ) -> None:
        cls._validate_ids(account_id, idempotency_key)
        if not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        _required(sharing_scope, "sharing_scope", maximum=128)

    @staticmethod
    def _mutable_table(
        item_kind: Literal["cognitive_claim", "decision_case"],
    ) -> tuple[str, str]:
        if item_kind == "cognitive_claim":
            return "self_model_cognitive_claims", "claim_id"
        return "self_model_decision_cases", "case_id"

    def _add_source_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        source: SourceInput,
        expected_version: int,
        occurred_at: str,
    ) -> int:
        item = self._item(connection, account_id, item_kind, item_id, expected_version)
        if (
            isinstance(item, RelationshipProfile)
            and item.status != "candidate"
            and not source.negative
        ):
            raise InvalidSelfModelTransitionError(
                "only negative evidence can be added after relationship approval"
            )
        self._require_source(connection, account_id, source.source_event_id)
        source_item_version = (
            item.version_number if isinstance(item, RelationshipProfile) else 0
        )
        try:
            connection.execute(
                """
                INSERT INTO self_model_sources (
                    item_kind, item_id, item_version, account_id, source_event_id,
                    relation, adopted, negative, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_kind,
                    item_id,
                    source_item_version,
                    account_id,
                    source.source_event_id,
                    source.relation,
                    source.adopted,
                    source.negative,
                    occurred_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: self_model_sources" in str(exc):
                raise ValueError("duplicate source") from exc
            raise
        result_version = expected_version
        if not isinstance(item, RelationshipProfile):
            result_version += 1
            mutable_kind = cast(Literal["cognitive_claim", "decision_case"], item_kind)
            table, id_column = self._mutable_table(mutable_kind)
            cursor = connection.execute(
                f"""
                UPDATE {table}
                SET version = version + 1, updated_at = ?
                WHERE account_id = ? AND {id_column} = ? AND version = ?
                """,
                (occurred_at, account_id, item_id, expected_version),
            )
            if cursor.rowcount != 1:
                raise SelfModelVersionConflictError(item_id)
        payload: dict[str, object] = {
            "item_kind": item_kind,
            "item_id": item_id,
            **_source_payloads((source,))[0],
            "expected_version": expected_version,
        }
        self._append_audit(
            connection,
            account_id,
            f"{item_kind}.source_added",
            item_kind,
            item_id,
            result_version,
            payload,
            occurred_at,
        )
        return result_version

    def _item(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        expected_version: int,
    ) -> SelfModelItem:
        if item_kind == "cognitive_claim":
            claim = self._claim(connection, account_id, item_id)
            if claim.version != expected_version:
                raise SelfModelVersionConflictError(item_id)
            return claim
        if item_kind == "decision_case":
            decision = self._decision(connection, account_id, item_id)
            if decision.version != expected_version:
                raise SelfModelVersionConflictError(item_id)
            return decision
        return self._profile(connection, account_id, item_id, expected_version)

    def _claim(
        self, connection: sqlite3.Connection, account_id: str, claim_id: str
    ) -> CognitiveClaim:
        row = connection.execute(
            """
            SELECT * FROM self_model_cognitive_claims
            WHERE account_id = ? AND claim_id = ?
            """,
            (account_id, claim_id),
        ).fetchone()
        if row is None:
            raise SelfModelNotFoundError(claim_id)
        return CognitiveClaim(
            claim_id=str(row["claim_id"]),
            account_id=str(row["account_id"]),
            claim_type=cast(CognitiveClaimType, str(row["claim_type"])),
            statement=str(row["statement"]),
            context=str(row["context"]),
            confidence=float(row["confidence"]),
            sharing_scope=str(row["sharing_scope"]),
            status=cast(ItemStatus, str(row["status"])),
            unresolved_conflict=bool(row["unresolved_conflict"]),
            sources=self._sources(connection, account_id, "cognitive_claim", claim_id, 0),
            owner_reviewed_at=self._datetime(row["owner_reviewed_at"]),
            step_up_verified=bool(row["step_up_verified"]),
            version=int(row["version"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def _decision(
        self, connection: sqlite3.Connection, account_id: str, case_id: str
    ) -> DecisionCase:
        row = connection.execute(
            """
            SELECT * FROM self_model_decision_cases
            WHERE account_id = ? AND case_id = ?
            """,
            (account_id, case_id),
        ).fetchone()
        if row is None:
            raise SelfModelNotFoundError(case_id)
        return DecisionCase(
            case_id=str(row["case_id"]),
            account_id=str(row["account_id"]),
            kind=cast(DecisionKind, str(row["kind"])),
            context=str(row["context"]),
            options=tuple(cast(list[str], json.loads(str(row["options_json"])))),
            constraints=tuple(cast(list[str], json.loads(str(row["constraints_json"])))),
            chosen_option=str(row["chosen_option"]),
            rejected_options=tuple(cast(list[str], json.loads(str(row["rejected_options_json"])))),
            outcome=str(row["outcome"]),
            reflection=str(row["reflection"]),
            still_endorsed=bool(row["still_endorsed"]),
            sharing_scope=str(row["sharing_scope"]),
            status=cast(ItemStatus, str(row["status"])),
            unresolved_conflict=bool(row["unresolved_conflict"]),
            sources=self._sources(connection, account_id, "decision_case", case_id, 0),
            owner_reviewed_at=self._datetime(row["owner_reviewed_at"]),
            step_up_verified=bool(row["step_up_verified"]),
            version=int(row["version"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def _profile(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        profile_id: str,
        version_number: int | None,
    ) -> RelationshipProfile:
        if version_number is None:
            row = connection.execute(
                """
                SELECT * FROM self_model_relationship_profiles
                WHERE account_id = ? AND profile_id = ?
                ORDER BY version_number DESC LIMIT 1
                """,
                (account_id, profile_id),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT * FROM self_model_relationship_profiles
                WHERE account_id = ? AND profile_id = ? AND version_number = ?
                """,
                (account_id, profile_id, version_number),
            ).fetchone()
        if row is None:
            raise SelfModelNotFoundError(profile_id)
        actual_version = int(row["version_number"])
        return RelationshipProfile(
            profile_id=str(row["profile_id"]),
            account_id=str(row["account_id"]),
            version_number=actual_version,
            person_id=str(row["person_id"]),
            relationship_id=str(row["relationship_id"]),
            salutation=str(row["salutation"]),
            tone=str(row["tone"]),
            advice_style=str(row["advice_style"]),
            sharing_scope=str(row["sharing_scope"]),
            boundaries=tuple(cast(list[str], json.loads(str(row["boundaries_json"])))),
            status=cast(RelationshipProfileStatus, str(row["status"])),
            unresolved_conflict=bool(row["unresolved_conflict"]),
            sources=self._sources(
                connection,
                account_id,
                "relationship_profile",
                profile_id,
                actual_version,
            ),
            owner_reviewed_at=self._datetime(row["owner_reviewed_at"]),
            step_up_verified=bool(row["step_up_verified"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _datetime(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value is not None else None

    @staticmethod
    def _sources(
        connection: sqlite3.Connection,
        account_id: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        item_version: int,
    ) -> tuple[SelfModelSource, ...]:
        rows = connection.execute(
            """
            SELECT s.*, e.speaker_class, e.occurred_at
            FROM self_model_sources AS s
            JOIN evidence_events AS e
              ON e.event_id = s.source_event_id
             AND e.account_id = s.account_id
            WHERE s.account_id = ? AND s.item_kind = ?
              AND s.item_id = ? AND s.item_version = ?
            ORDER BY e.occurred_at, s.source_event_id, s.relation
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
    def _require_source(
        connection: sqlite3.Connection, account_id: str, source_event_id: str
    ) -> None:
        row = connection.execute(
            """
            SELECT event_type, speaker_class, payload_json
            FROM evidence_events WHERE account_id = ? AND event_id = ?
            """,
            (account_id, source_event_id),
        ).fetchone()
        if row is None:
            raise UntrustedSelfModelSourceError("source event is not in the account")
        payload = cast(dict[str, object], json.loads(str(row["payload_json"])))
        if (
            row["speaker_class"] != "owner"
            or payload.get("owner_projection_eligible") is not True
            or payload.get("simulated_output") is True
            or payload.get("interaction_mode") not in {None, "companion"}
            or row["event_type"] not in {"speech.utterance_finalized", "owner.action_recorded"}
        ):
            raise UntrustedSelfModelSourceError(
                "only eligible owner evidence from a real companion interaction is allowed"
            )

    @staticmethod
    def _require_relationship_refs(
        connection: sqlite3.Connection,
        account_id: str,
        person_id: str,
        relationship_id: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1
            FROM person_entities AS p
            JOIN relationships AS r
              ON r.person_id = p.person_id AND r.account_id = p.account_id
            WHERE p.account_id = ? AND p.person_id = ?
              AND r.relationship_id = ?
            """,
            (account_id, person_id, relationship_id),
        ).fetchone()
        if row is None:
            raise RelationshipReferenceError(
                "person_id and relationship_id must exist in the same account"
            )

    def _duplicate_result(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        idempotency_key: str,
        payload_sha256: str,
    ) -> SelfModelItem | None:
        row = connection.execute(
            """
            SELECT * FROM self_model_command_receipts
            WHERE account_id = ? AND idempotency_key = ?
            """,
            (account_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if row["payload_sha256"] != payload_sha256:
            raise SelfModelIdempotencyConflictError(idempotency_key)
        result_kind = cast(SelfModelItemKind, str(row["result_kind"]))
        result_id = str(row["result_id"])
        if result_kind == "cognitive_claim":
            return self._claim(connection, account_id, result_id)
        if result_kind == "decision_case":
            return self._decision(connection, account_id, result_id)
        return self._profile(
            connection,
            account_id,
            result_id,
            int(row["result_version"]),
        )

    @staticmethod
    def _record_receipt(
        connection: sqlite3.Connection,
        account_id: str,
        idempotency_key: str,
        command_type: str,
        payload_sha256: str,
        result_kind: SelfModelItemKind,
        result_id: str,
        result_version: int,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO self_model_command_receipts (
                account_id, idempotency_key, command_type, payload_sha256,
                result_kind, result_id, result_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                idempotency_key,
                command_type,
                payload_sha256,
                result_kind,
                result_id,
                result_version,
                created_at,
            ),
        )

    @staticmethod
    def _append_audit(
        connection: sqlite3.Connection,
        account_id: str,
        event_type: str,
        item_kind: SelfModelItemKind,
        item_id: str,
        item_version: int,
        payload: dict[str, object],
        occurred_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO self_model_audit_events (
                event_id, account_id, actor_account_id, event_type,
                item_kind, item_id, item_version, payload_json, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                account_id,
                account_id,
                event_type,
                item_kind,
                item_id,
                item_version,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                occurred_at,
            ),
        )
