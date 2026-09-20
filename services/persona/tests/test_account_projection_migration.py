"""Contract tests for the Persona account -> subject projection seam.

What this file pins
-------------------
* The seam is operator-invoked and SQLite-only. plan/dry_run never write;
  apply writes only the projected tables plus the migration
  journal/receipt/backup/audit tables; the six Persona source tables and
  evidence_events stay byte identical.
* A row reaches a subject-keyed table only when the whole chain holds: the
  account is registered in the Control store, identity_persons has it as
  active, owner evidence exists, every referenced event exists with a non-null
  subject_id, and the subjects agree. The account id is never used as the
  subject id by convention - only Control registration plus owner evidence
  prove that equality.
* Fail-closed classification: unregistered, inactive, no-owner-evidence,
  no-subject-evidence, NULL subject, mixed subject, foreign subject and
  ambiguous lineage stay out of the projected tables and carry a receipt.
* A version keeps snapshot_json byte for byte, its parent chain must be
  projectable and acyclic, and a child is refused when its parent is refused.
* Fences are receipt-scoped: content drift refuses a run, a status-only change
  is refreshed in place, rollback removes exactly the rows of its own run and
  stays idempotent.

Assumptions
-----------
* The public API is plan, dry_run, apply, rollback and PersonaProjectionError
  from services.persona.migrations, with the same report shape as the Digital
  Self seam: applied, idempotent, dry_run, migration_id, manifest_sha256,
  rows, pending_row_count, statistics["writes"], deleted_rows, restored_rows,
  status, source_path, identity_path, control_path.
* The API is still being implemented, so importing the package fails today and
  every test skips with that import error as its reason. The assertions are the
  specification: reason strings are asserted exactly where the current draft
  already encodes them (_resolve_subject / _trait_resolution); for the tables
  whose resolver is not written yet (evidence, observation, style, consent,
  version) the tests assert the fail-closed outcome and, where the rule is
  already encoded, the reason as well.
* snapshot_json items are JSON objects carrying at least trait_id
  (services/persona/engine.py reads item["trait_id"]).
* Target and support table names come from the migration draft. The style stats
  NULL-subject case encodes the handoff rule that multi-subject or only-NULL
  subject evidence must fail closed, and the version child/parent case encodes
  that a refused parent takes its child down with it.

Nothing here touches the network, PostgreSQL, the device or any live service.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from services.control_api.app.database import MemoryStore
from services.identity.sqlite_store import SqliteIdentityStore
from services.persona.engine import PersonaEngine

_projection: Any = None
_IMPORT_ERROR: str | None = None
try:  # the seam is under active development; a partial module must not break collection
    from services.persona.migrations import account_projection as _module
except ImportError as exc:  # pragma: no cover - true until the public API lands
    _IMPORT_ERROR = str(exc)
else:
    _projection = _module


def _api() -> Any:
    """Return the module under test, skipping while the public API is unfinished."""

    if _projection is None:
        pytest.skip(
            "services.persona.migrations is still being implemented "
            f"({_IMPORT_ERROR}); this file pins the P2-03 projection contract and "
            "runs once plan/dry_run/apply/rollback import cleanly"
        )
    return _projection


_ACCOUNT = "account-owner"
_OTHER_ACCOUNT = "account-elsewhere"
_MEMBER = "person-member"
_FOREIGN_SUBJECT = "person-elsewhere"
_DEVICE = "device-1"
_BINDING = "binding-1"
_OTHER_BINDING = "binding-2"
_CREATED = "2026-09-01T08:00:00+00:00"
_OCCURRED = "2026-09-01T07:59:00+00:00"

_TRAIT_TABLE = "persona_traits"
_EVIDENCE_TABLE = "persona_evidence"
_OBSERVATION_TABLE = "persona_observation_receipts"
_STYLE_TABLE = "speech_style_stats"
_CONSENT_TABLE = "persona_learning_consents"
_VERSION_TABLE = "persona_versions"
_EVENT_TABLE = "evidence_events"
_SOURCE_TABLES = (
    _TRAIT_TABLE,
    _EVIDENCE_TABLE,
    _OBSERVATION_TABLE,
    _STYLE_TABLE,
    _CONSENT_TABLE,
    _VERSION_TABLE,
)

_PROJECTED = {
    _TRAIT_TABLE: "persona_subject_traits",
    _EVIDENCE_TABLE: "persona_subject_evidence",
    _OBSERVATION_TABLE: "persona_subject_observation_receipts",
    _STYLE_TABLE: "persona_subject_style_stats",
    _CONSENT_TABLE: "persona_subject_learning_consents",
    _VERSION_TABLE: "persona_subject_versions",
}
_MIGRATIONS = "persona_projection_migrations"
_RECEIPTS = "persona_projection_receipts"
_QUARANTINE = "persona_projection_quarantine"
_BACKUPS = "persona_projection_backups"
_AUDIT = "persona_projection_audit_events"
_SUPPORT_TABLES = (_MIGRATIONS, _RECEIPTS, _QUARANTINE, _BACKUPS, _AUDIT)

_TRAIT_ID = "trait-1"
_EVIDENCE_EVENT = "event-trait-1"
_OBSERVATION_EVENT = "event-observation-1"
_CONSENT_EVENT = "event-consent-grant"
_CONSENT_REVOKE_EVENT = "event-consent-revoke"
_VERSION_ID = "version-1"
_STYLE_SCENE = "home"

_HAPPY_ROW_IDS: Mapping[str, str] = {
    _TRAIT_TABLE: _TRAIT_ID,
    _EVIDENCE_TABLE: f"{_TRAIT_ID}::{_EVIDENCE_EVENT}",
    _OBSERVATION_TABLE: _OBSERVATION_EVENT,
    _STYLE_TABLE: f"{_ACCOUNT}::{_STYLE_SCENE}",
    _CONSENT_TABLE: _ACCOUNT,
    _VERSION_TABLE: _VERSION_ID,
}


@dataclass(frozen=True, slots=True)
class _World:
    db: Path
    identity: Path
    control: Path


# -- sqlite helpers -----------------------------------------------------------


def _connect(path: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    if foreign_keys:
        connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _checkpoint(path: Path) -> None:
    with _connect(path) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, Any]) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",  # noqa: S608 - fixture
        list(values.values()),
    )


def _table_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with _read_only(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {str(row["name"]) for row in rows}


def _row_count(path: Path, table: str) -> int:
    if table not in _table_names(path):
        return 0
    with _read_only(path) as connection:
        row = connection.execute(f"SELECT count(*) AS n FROM {table}").fetchone()  # noqa: S608
    assert row is not None
    return int(row["n"])


def _rows(path: Path, table: str, order: str) -> list[dict[str, Any]]:
    with _read_only(path) as connection:
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()  # noqa: S608
    return [dict(row) for row in rows]


def _order_column(table: str) -> str:
    return {
        _TRAIT_TABLE: "trait_id",
        _EVIDENCE_TABLE: "source_event_id",
        _OBSERVATION_TABLE: "source_event_id",
        _STYLE_TABLE: "scene",
        _CONSENT_TABLE: "account_id",
        _VERSION_TABLE: "version_id",
    }[table]


def _source_digest(path: Path) -> dict[str, Any]:
    """Content of the six Persona source tables plus evidence_events."""

    payload = {table: _rows(path, table, _order_column(table)) for table in _SOURCE_TABLES}
    payload[_EVENT_TABLE] = _rows(path, _EVENT_TABLE, "event_id")
    return {
        "digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest(),
        "counts": {table: len(rows) for table, rows in payload.items()},
    }


# -- fixture builders ---------------------------------------------------------


def _build_databases(
    tmp_path: Path,
    *,
    separate_identity: bool = False,
    separate_control: bool = False,
) -> _World:
    db = tmp_path / "memoria.sqlite3"
    PersonaEngine.sqlite(db).initialize()
    MemoryStore(str(db)).initialize()
    identity = (tmp_path / "identity.sqlite3") if separate_identity else db
    SqliteIdentityStore(identity).initialize()
    control = (tmp_path / "control.sqlite3") if separate_control else db
    if control != db:
        MemoryStore(str(control)).initialize()
    return _World(db=db, identity=identity, control=control)


def _seed_account(store: Path, account_id: str = _ACCOUNT) -> None:
    with _connect(store) as connection:
        _insert(
            connection,
            "profiles",
            {
                "user_id": account_id,
                "display_name": "本人",
                "created_at": _CREATED,
                "updated_at": _CREATED,
            },
        )
        _insert(
            connection,
            "accounts",
            {
                "user_id": account_id,
                "username": account_id,
                "username_normalized": account_id,
                "password_hash": "hash",
                "created_at": _CREATED,
                "updated_at": _CREATED,
            },
        )


def _unregister_account(store: Path, account_id: str = _ACCOUNT) -> None:
    with _connect(store) as connection:
        connection.execute("DELETE FROM profiles WHERE user_id = ?", (account_id,))
        connection.execute("DELETE FROM accounts WHERE user_id = ?", (account_id,))


def _seed_person(identity: Path, person_id: str, *, status: str = "active") -> None:
    with _connect(identity) as connection:
        _insert(
            connection,
            "identity_persons",
            {
                "person_id": person_id,
                "display_name": person_id,
                "subject_category": "unknown",
                "age_band": "unknown",
                "age_evidence_status": "unverified",
                "locale": "zh-CN",
                "timezone": "Asia/Shanghai",
                "status": status,
                "created_at": _CREATED,
                "updated_at": _CREATED,
            },
        )


def _set_person_status(identity: Path, person_id: str, status: str) -> None:
    with _connect(identity) as connection:
        connection.execute(
            "UPDATE identity_persons SET status = ? WHERE person_id = ?",
            (status, person_id),
        )


def _seed_binding(
    identity: Path,
    *,
    owner: str = _ACCOUNT,
    binding_id: str = _BINDING,
    device_id: str = _DEVICE,
    status: str = "active",
) -> None:
    with _connect(identity) as connection:
        _insert(
            connection,
            "identity_device_bindings",
            {
                "binding_id": binding_id,
                "device_id": device_id,
                "declared_mode": "self_use",
                "family_space_id": None,
                "account_owner_person_id": owner,
                "binding_version": 1,
                "status": status,
                "reason": "create",
                "valid_from": _CREATED,
                "valid_until": None,
                "supersedes_binding_id": None,
                "service_profile_version": "profile-v1",
                "policy_bundle_version": "policy-v1",
                "consent_snapshot_id": None,
                "persona_assignment_id": None,
                "created_at": _CREATED,
            },
        )


def _seed_relationship(
    identity: Path,
    *,
    relationship_id: str,
    source: str,
    target: str,
    relation_type: str = "parent_of",
    status: str = "active",
) -> None:
    with _connect(identity) as connection:
        _insert(
            connection,
            "identity_relationships",
            {
                "relationship_id": relationship_id,
                "source_person_id": source,
                "target_person_id": target,
                "relation_type": relation_type,
                "status": status,
                "valid_from": _CREATED,
                "valid_until": None,
                "established_evidence_id": "evidence-established",
                "confirmed_by_source_at": None,
                "confirmed_by_target_at": None,
                "dispute_resolution_acked_by_source_at": None,
                "dispute_resolution_acked_by_target_at": None,
                "requires_confirmation": 0,
                "can_delegate": 0,
                "delegated_from_relationship_id": None,
                "delegation_depth": 0,
                "permissions_json": "[]",
                "dispute_reason": None,
                "auto_suspended": 0,
                "revoked_at": None,
                "revocation_evidence_id": None,
                "created_at": _CREATED,
                "updated_at": _CREATED,
            },
        )


def _seed_event(
    db: Path,
    event_id: str,
    *,
    account_id: str = _ACCOUNT,
    subject_id: str | None = _ACCOUNT,
    event_type: str = "speech.utterance_finalized",
    speaker_class: str = "owner",
    source: str = "test.persona-projection",
    occurred_at: str = _OCCURRED,
) -> None:
    payload_json = json.dumps(
        {"text": "历史话轮原文。", "persona_eligible": True}, ensure_ascii=False
    )
    with _connect(db) as connection:
        _insert(
            connection,
            _EVENT_TABLE,
            {
                "event_id": event_id,
                "account_id": account_id,
                "session_id": None,
                "turn_id": None,
                "generation_id": None,
                "event_type": event_type,
                "schema_version": 1,
                "occurred_at": occurred_at,
                "recorded_at": occurred_at,
                "subject_id": subject_id,
                "speaker_identity_id": None,
                "speaker_class": speaker_class,
                "source": source,
                "consent_grant_id": None,
                "payload_json": payload_json,
                "content_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                "supersedes_event_id": None,
            },
        )


def _seed_trait(
    db: Path,
    *,
    trait_id: str = _TRAIT_ID,
    account_id: str = _ACCOUNT,
    category: str = "preference",
    normalized_key: str | None = None,
    description: str = "喜欢清晨散步",
    status: str = "candidate",
    confidence: float = 0.8,
    observation_count: int = 1,
    created_at: str = _CREATED,
    updated_at: str = _CREATED,
    review_event_id: str | None = None,
) -> None:
    with _connect(db) as connection:
        _insert(
            connection,
            _TRAIT_TABLE,
            {
                "trait_id": trait_id,
                "account_id": account_id,
                "category": category,
                "normalized_key": normalized_key if normalized_key is not None else trait_id,
                "description": description,
                "context": "每天早上",
                "counterexample": "",
                "confidence": confidence,
                "status": status,
                "observation_count": observation_count,
                "created_at": created_at,
                "updated_at": updated_at,
                "review_event_id": review_event_id,
            },
        )


def _seed_evidence(
    db: Path,
    *,
    trait_id: str = _TRAIT_ID,
    event_id: str = _EVIDENCE_EVENT,
    account_id: str = _ACCOUNT,
    evidence_account_id: str | None = None,
    subject_id: str | None = _ACCOUNT,
    event_account_id: str | None = None,
    event_type: str = "speech.utterance_finalized",
    scene: str = _STYLE_SCENE,
    weight: float = 0.6,
    occurred_at: str = _OCCURRED,
    with_event: bool = True,
) -> None:
    if with_event:
        _seed_event(
            db,
            event_id,
            account_id=event_account_id if event_account_id is not None else account_id,
            subject_id=subject_id,
            event_type=event_type,
            occurred_at=occurred_at,
        )
    with _connect(db) as connection:
        _insert(
            connection,
            _EVIDENCE_TABLE,
            {
                "trait_id": trait_id,
                "account_id": evidence_account_id if evidence_account_id is not None else account_id,
                "source_event_id": event_id,
                "scene": scene,
                "weight": weight,
                "occurred_at": occurred_at,
            },
        )


def _seed_observation(
    db: Path,
    *,
    event_id: str = _OBSERVATION_EVENT,
    account_id: str = _ACCOUNT,
    subject_id: str | None = _ACCOUNT,
    observed_at: str = _OCCURRED,
    with_event: bool = True,
) -> None:
    if with_event:
        _seed_event(db, event_id, account_id=account_id, subject_id=subject_id)
    # The engine schema makes an orphan receipt impossible (foreign key plus
    # ON DELETE CASCADE), so a store that saw one was written with foreign keys
    # off - the fixture reproduces exactly that by inserting directly.
    with _connect(db, foreign_keys=with_event) as connection:
        _insert(
            connection,
            _OBSERVATION_TABLE,
            {
                "source_event_id": event_id,
                "account_id": account_id,
                "observed_at": observed_at,
            },
        )


def _seed_style(
    db: Path,
    *,
    account_id: str = _ACCOUNT,
    scene: str = _STYLE_SCENE,
    updated_at: str = _CREATED,
) -> None:
    with _connect(db) as connection:
        _insert(
            connection,
            _STYLE_TABLE,
            {
                "account_id": account_id,
                "scene": scene,
                "utterance_count": 12,
                "char_count": 240,
                "speech_duration_ms": 60000,
                "pause_ratio_sum": 1.5,
                "pause_sample_count": 10,
                "tic_counts_json": "{}",
                "updated_at": updated_at,
            },
        )


def _seed_consent(
    db: Path,
    *,
    account_id: str = _ACCOUNT,
    policy_version: str = "policy-v1",
    grant_event_id: str = _CONSENT_EVENT,
    revoke_event_id: str | None = None,
    granted_at: str = _CREATED,
    revoked_at: str | None = None,
) -> None:
    with _connect(db) as connection:
        _insert(
            connection,
            _CONSENT_TABLE,
            {
                "account_id": account_id,
                "policy_version": policy_version,
                "granted_at": granted_at,
                "revoked_at": revoked_at,
                "grant_event_id": grant_event_id,
                "revoke_event_id": revoke_event_id,
            },
        )


def _seed_version(
    db: Path,
    *,
    version_id: str = _VERSION_ID,
    account_id: str = _ACCOUNT,
    number: int = 1,
    status: str = "active",
    reason: str = "trait_review",
    snapshot: str | None = None,
    parent: str | None = None,
    created_at: str = _CREATED,
) -> None:
    snapshot_json = snapshot if snapshot is not None else '[{"trait_id": "trait-1"}]'
    with _connect(db) as connection:
        _insert(
            connection,
            _VERSION_TABLE,
            {
                "version_id": version_id,
                "account_id": account_id,
                "version_number": number,
                "status": status,
                "reason": reason,
                "snapshot_json": snapshot_json,
                "parent_version_id": parent,
                "created_at": created_at,
            },
        )


def _seed_trait_with_evidence(
    world: _World,
    *,
    trait_id: str,
    event_id: str,
    subject_id: str | None = _ACCOUNT,
    evidence_account_id: str | None = None,
    event_account_id: str | None = None,
    status: str = "candidate",
) -> None:
    _seed_trait(world.db, trait_id=trait_id, status=status)
    _seed_evidence(
        world.db,
        trait_id=trait_id,
        event_id=event_id,
        subject_id=subject_id,
        evidence_account_id=evidence_account_id,
        event_account_id=event_account_id,
    )


def _healthy_world(
    tmp_path: Path,
    *,
    separate_identity: bool = False,
    separate_control: bool = False,
    with_binding: bool = True,
) -> _World:
    """One registered owner with a trait, observation, style, consent and version."""

    world = _build_databases(
        tmp_path, separate_identity=separate_identity, separate_control=separate_control
    )
    _seed_account(world.control)
    _seed_person(world.identity, _ACCOUNT)
    if with_binding:
        _seed_binding(world.identity)
    _seed_trait(world.db)
    _seed_evidence(world.db)
    _seed_observation(world.db)
    _seed_style(world.db)
    _seed_event(
        world.db,
        _CONSENT_EVENT,
        event_type="consent.granted",
        speaker_class="system",
        source="user.persona_consent",
    )
    _seed_consent(world.db)
    _seed_version(world.db)
    _checkpoint(world.db)
    return world


def _seed_member_lineage(
    world: _World,
    *,
    relationship_id: str = "rel-member",
    source: str = _ACCOUNT,
    target: str = _MEMBER,
    relation_type: str = "parent_of",
) -> None:
    if _MEMBER not in _person_ids(world.identity):
        _seed_person(world.identity, _MEMBER)
    _seed_relationship(
        world.identity,
        relationship_id=relationship_id,
        source=source,
        target=target,
        relation_type=relation_type,
    )


def _person_ids(identity: Path) -> set[str]:
    if "identity_persons" not in _table_names(identity):
        return set()
    return {str(row["person_id"]) for row in _rows(identity, "identity_persons", "person_id")}


# -- report helpers -----------------------------------------------------------


def _plan_rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = report["rows"]
    assert isinstance(rows, list)
    return rows


def _row(report: Mapping[str, Any], table: str, row_id: str) -> dict[str, Any]:
    matches = [
        row
        for row in _plan_rows(report)
        if row.get("table") == table and row.get("source_row_id") == row_id
    ]
    assert len(matches) == 1, f"expected one {table}:{row_id} row, got {matches!r}"
    return matches[0]


def _blocked(
    report: Mapping[str, Any],
    table: str,
    row_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    row = _row(report, table, row_id)
    assert row["outcome"] in {"omitted", "quarantined"}, row
    assert row["subject_id"] is None, row
    if reason is not None:
        assert row["reason"] == reason, row
    return row


def _mapped(report: Mapping[str, Any], table: str, row_id: str) -> dict[str, Any]:
    row = _row(report, table, row_id)
    assert row["outcome"] == "mapped", row
    assert row["subject_id"] is not None, row
    return row


def _mentions(items: Sequence[Any], *needles: str) -> None:
    joined = "\n".join(str(item) for item in items)
    for needle in needles:
        assert needle in joined, (needle, list(items))


def _target_subjects(db: Path, table: str) -> list[str]:
    if table not in _table_names(db):
        return []
    return sorted(str(row["subject_id"]) for row in _rows(db, table, "rowid"))


def _receipt_outcomes(db: Path) -> dict[tuple[str, str], str]:
    if _RECEIPTS not in _table_names(db):
        return {}
    return {
        (str(row["table_name"]), str(row["source_row_id"])): str(row["outcome"])
        for row in _rows(db, _RECEIPTS, "table_name, source_row_id")
    }


def _rows_for_migration(db: Path, table: str, migration_id: str) -> list[dict[str, Any]]:
    if table not in _table_names(db):
        return []
    return [
        row
        for row in _rows(db, table, "rowid")
        if str(row["projection_migration_id"]) == migration_id
    ]


# -- plan ---------------------------------------------------------------------


def test_plan_is_read_only_and_classifies_every_row(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    before = _source_digest(world.db)

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    assert report["dry_run"] is True
    assert report["applied"] is False
    assert report["idempotent"] is False
    assert report["migration_id"] is None
    assert report["manifest_sha256"] == report["manifest"]["manifest_sha256"]
    assert {row["table"] for row in _plan_rows(report)} == set(_SOURCE_TABLES)
    for table, row_id in _HAPPY_ROW_IDS.items():
        row = _mapped(report, table, row_id)
        assert row["subject_id"] == _ACCOUNT
        assert row["subject_class"] == "owner/self"
    names = _table_names(world.db)
    for target in _PROJECTED.values():
        assert target not in names
    for support in _SUPPORT_TABLES:
        assert support not in names
    assert _source_digest(world.db) == before


def test_dry_run_is_an_alias_of_plan(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)

    planned = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )
    dry = module.dry_run(
        world.db, identity_path=world.identity, control_path=world.control
    )

    assert dry["dry_run"] is True
    assert dry["applied"] is False
    assert dry["manifest_sha256"] == planned["manifest_sha256"]
    assert _plan_rows(dry) == _plan_rows(planned)


# -- classification -----------------------------------------------------------


def test_unregistered_account_is_never_projected(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _unregister_account(world.control)

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    for table, row_id in _HAPPY_ROW_IDS.items():
        _blocked(report, table, row_id, reason="owner_account_unregistered")


def test_inactive_owner_person_is_never_projected(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _set_person_status(world.identity, _ACCOUNT, "disabled")

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    for table, row_id in _HAPPY_ROW_IDS.items():
        _blocked(report, table, row_id, reason="owner_person_inactive")


def test_missing_owner_evidence_is_never_projected(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path, with_binding=False)

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    for table, row_id in _HAPPY_ROW_IDS.items():
        _blocked(report, table, row_id, reason="no_owner_evidence")


def test_missing_owner_evidence_also_blocks_a_member_subject(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path, with_binding=False)
    _seed_member_lineage(world)
    _seed_trait_with_evidence(
        world, trait_id="trait-member", event_id="event-member", subject_id=_MEMBER
    )
    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-member", reason="no_owner_evidence")


def test_self_relationship_is_owner_evidence(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path, with_binding=False)
    _seed_relationship(
        world.identity,
        relationship_id="rel-self",
        source=_ACCOUNT,
        target=_ACCOUNT,
        relation_type="self",
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    row = _mapped(report, _TRAIT_TABLE, _TRAIT_ID)
    assert row["subject_id"] == _ACCOUNT
    assert row["subject_class"] == "owner/self"


def test_event_without_a_subject_is_not_projected_onto_the_account(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_trait_with_evidence(
        world, trait_id="trait-null", event_id="event-null", subject_id=None
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-null", reason="no_subject_evidence")
    _blocked(report, _EVIDENCE_TABLE, "trait-null::event-null", reason="no_subject_evidence")
    assert _ACCOUNT not in _target_subjects(world.db, _PROJECTED[_TRAIT_TABLE])


def test_mixed_subject_evidence_is_blocked(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_member_lineage(world)
    _seed_evidence(
        world.db,
        trait_id=_TRAIT_ID,
        event_id="event-mixed",
        subject_id=_MEMBER,
        occurred_at="2026-09-02T07:59:00+00:00",
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, _TRAIT_ID, reason="mixed_subject_evidence")
    _blocked(
        report,
        _EVIDENCE_TABLE,
        f"{_TRAIT_ID}::{_EVIDENCE_EVENT}",
        reason="mixed_subject_evidence",
    )
    _blocked(
        report,
        _EVIDENCE_TABLE,
        f"{_TRAIT_ID}::event-mixed",
        reason="mixed_subject_evidence",
    )


def test_partially_null_subject_evidence_is_blocked(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_evidence(
        world.db,
        trait_id=_TRAIT_ID,
        event_id="event-partial-null",
        subject_id=None,
        occurred_at="2026-09-02T07:59:00+00:00",
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, _TRAIT_ID, reason="mixed_subject_evidence")


def test_child_member_lineage_is_projected_to_the_member(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_member_lineage(world)
    _seed_trait_with_evidence(
        world, trait_id="trait-member", event_id="event-member", subject_id=_MEMBER
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    row = _mapped(report, _TRAIT_TABLE, "trait-member")
    assert row["subject_id"] == _MEMBER
    assert row["subject_class"] == "child/member"
    assert row["reason"] == "child_member_lineage"
    assert _mapped(report, _TRAIT_TABLE, _TRAIT_ID)["subject_id"] == _ACCOUNT


def test_member_without_lineage_is_not_projected(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_person(world.identity, _MEMBER)
    _seed_trait_with_evidence(
        world, trait_id="trait-member", event_id="event-member", subject_id=_MEMBER
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-member", reason="no_subject_evidence")


def test_inactive_subject_is_not_projected(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_member_lineage(world)
    _seed_trait_with_evidence(
        world, trait_id="trait-member", event_id="event-member", subject_id=_MEMBER
    )
    _set_person_status(world.identity, _MEMBER, "disabled")

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-member", reason="inactive_subject")


def test_subject_owned_by_another_account_is_foreign(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_account(world.control, _OTHER_ACCOUNT)
    _seed_person(world.identity, _OTHER_ACCOUNT)
    _seed_person(world.identity, _FOREIGN_SUBJECT)
    _seed_relationship(
        world.identity,
        relationship_id="rel-foreign",
        source=_OTHER_ACCOUNT,
        target=_FOREIGN_SUBJECT,
        relation_type="parent_of",
    )
    _seed_trait_with_evidence(
        world,
        trait_id="trait-foreign",
        event_id="event-foreign",
        subject_id=_FOREIGN_SUBJECT,
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-foreign", reason="foreign_subject")
    assert _FOREIGN_SUBJECT not in _target_subjects(world.db, _PROJECTED[_TRAIT_TABLE])


def test_ambiguous_member_lineage_is_blocked(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_account(world.control, _OTHER_ACCOUNT)
    _seed_person(world.identity, _OTHER_ACCOUNT)
    _seed_binding(
        world.identity,
        owner=_OTHER_ACCOUNT,
        binding_id=_OTHER_BINDING,
        device_id="device-2",
    )
    _seed_member_lineage(world)
    _seed_relationship(
        world.identity,
        relationship_id="rel-other",
        source=_OTHER_ACCOUNT,
        target=_MEMBER,
        relation_type="parent_of",
    )
    _seed_trait_with_evidence(
        world, trait_id="trait-member", event_id="event-member", subject_id=_MEMBER
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-member", reason="ambiguous_subject")


def test_evidence_account_mismatch_is_blocked(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_trait_with_evidence(
        world,
        trait_id="trait-other-account",
        event_id="event-other-account",
        subject_id=_ACCOUNT,
        evidence_account_id=_OTHER_ACCOUNT,
        event_account_id=_OTHER_ACCOUNT,
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-other-account", reason="evidence_account_mismatch")


def test_event_account_mismatch_is_blocked(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_evidence(
        world.db,
        trait_id=_TRAIT_ID,
        event_id="event-foreign-account",
        subject_id=_ACCOUNT,
        event_account_id=_OTHER_ACCOUNT,
    )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, _TRAIT_ID, reason="event_account_mismatch")


def test_trait_without_evidence_is_not_projected(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_trait(world.db, trait_id="trait-lonely")

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _TRAIT_TABLE, "trait-lonely")


def test_observation_receipt_needs_its_source_event(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_observation(world.db, event_id="event-missing", with_event=False)

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _OBSERVATION_TABLE, "event-missing")


def test_style_stats_with_only_null_subject_evidence_are_not_projected(
    tmp_path: Path,
) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_style(world.db, scene="kitchen")
    _seed_event(world.db, "event-unknown-speaker", subject_id=None)

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _STYLE_TABLE, f"{_ACCOUNT}::kitchen")


def test_consent_grant_event_must_be_a_grant(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_event(
        world.db,
        _CONSENT_REVOKE_EVENT,
        event_type="consent.revoked",
        speaker_class="system",
        source="user.persona_consent",
    )
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_CONSENT_TABLE} SET grant_event_id = ? WHERE account_id = ?",  # noqa: S608
            (_CONSENT_REVOKE_EVENT, _ACCOUNT),
        )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _CONSENT_TABLE, _ACCOUNT)


def test_consent_grant_and_revoke_must_share_one_subject(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_member_lineage(world)
    _seed_event(
        world.db,
        _CONSENT_REVOKE_EVENT,
        event_type="consent.revoked",
        speaker_class="system",
        source="user.persona_consent",
        subject_id=_MEMBER,
    )
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_CONSENT_TABLE} SET revoke_event_id = ?, revoked_at = ? "  # noqa: S608
            "WHERE account_id = ?",
            (_CONSENT_REVOKE_EVENT, _CREATED, _ACCOUNT),
        )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _CONSENT_TABLE, _ACCOUNT)


def test_consent_event_of_another_account_is_blocked(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_event(
        world.db,
        "event-consent-other",
        account_id=_OTHER_ACCOUNT,
        event_type="consent.granted",
        speaker_class="system",
        source="user.persona_consent",
    )
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_CONSENT_TABLE} SET grant_event_id = ? WHERE account_id = ?",  # noqa: S608
            ("event-consent-other", _ACCOUNT),
        )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _CONSENT_TABLE, _ACCOUNT)


# -- apply --------------------------------------------------------------------


def test_apply_projects_all_six_tables_with_receipts_and_backups(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    before = _source_digest(world.db)

    report = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )

    assert report["applied"] is True
    assert report["idempotent"] is False
    assert report["dry_run"] is False
    assert str(report["manifest_sha256"]) in str(report["migration_id"])
    for table, target in _PROJECTED.items():
        assert _row_count(world.db, target) == 1, table
        projected = _rows(world.db, target, "rowid")[0]
        assert projected["subject_id"] == _ACCOUNT
        assert projected["source_account_id"] == _ACCOUNT
        assert projected["projection_migration_id"] == report["migration_id"]
        source = _rows(world.db, table, _order_column(table))[0]
        for column, value in source.items():
            assert projected[column] == value, (table, column)
    assert _row_count(world.db, _RECEIPTS) == len(_HAPPY_ROW_IDS)
    assert _row_count(world.db, _BACKUPS) == len(_HAPPY_ROW_IDS)
    assert _row_count(world.db, _MIGRATIONS) == 1
    assert _row_count(world.db, _QUARANTINE) == 0
    assert _row_count(world.db, _AUDIT) >= len(_HAPPY_ROW_IDS) + 1
    assert _source_digest(world.db) == before


def test_apply_preserves_snapshot_json_bytes(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    raw = '[ { "trait_id" : "trait-1" , "description" : "喜欢清晨散步" } ]'
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_VERSION_TABLE} SET snapshot_json = ? WHERE version_id = ?",  # noqa: S608
            (raw, _VERSION_ID),
        )

    module.apply(world.db, identity_path=world.identity, control_path=world.control)

    projected = _rows(world.db, _PROJECTED[_VERSION_TABLE], "version_id")[0]
    source = _rows(world.db, _VERSION_TABLE, "version_id")[0]
    assert projected["snapshot_json"] == source["snapshot_json"] == raw
    assert projected["snapshot_json"].encode("utf-8") == raw.encode("utf-8")


def test_apply_rejects_a_stale_expected_manifest(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)

    with pytest.raises(module.PersonaProjectionError):
        module.apply(
            world.db,
            identity_path=world.identity,
            control_path=world.control,
            expected_manifest_sha256="0" * 64,
        )

    assert _row_count(world.db, _MIGRATIONS) == 0
    for target in _PROJECTED.values():
        assert target not in _table_names(world.db)


def test_reapplying_a_covered_plan_writes_nothing(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    first = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )
    before = (
        _row_count(world.db, _RECEIPTS),
        _row_count(world.db, _MIGRATIONS),
        _row_count(world.db, _PROJECTED[_TRAIT_TABLE]),
    )

    second = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )

    assert second["applied"] is False
    assert second["idempotent"] is True
    assert second["migration_id"] is None
    assert second["pending_row_count"] == 0
    assert second["statistics"]["writes"] == 0
    assert first["statistics"]["writes"] == len(_HAPPY_ROW_IDS)
    assert all(row["outcome"] == "already_projected" for row in _plan_rows(second))
    assert (
        _row_count(world.db, _RECEIPTS),
        _row_count(world.db, _MIGRATIONS),
        _row_count(world.db, _PROJECTED[_TRAIT_TABLE]),
    ) == before


def test_status_transition_is_refreshed_in_place(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    module.apply(world.db, identity_path=world.identity, control_path=world.control)
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_TRAIT_TABLE} SET status = 'confirmed' WHERE trait_id = ?",  # noqa: S608
            (_TRAIT_ID,),
        )

    report = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )

    assert report["applied"] is True
    assert report["statistics"]["writes"] == 1
    projected = _rows(world.db, _PROJECTED[_TRAIT_TABLE], "trait_id")[0]
    assert projected["status"] == "confirmed"
    assert _receipt_outcomes(world.db)[(_TRAIT_TABLE, _TRAIT_ID)] == "refreshed"
    assert _row_count(world.db, _MIGRATIONS) == 2


# -- drift --------------------------------------------------------------------


def test_content_drift_under_a_receipt_is_refused(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    module.apply(world.db, identity_path=world.identity, control_path=world.control)
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_TRAIT_TABLE} SET description = '被改写的描述' WHERE trait_id = ?",  # noqa: S608
            (_TRAIT_ID,),
        )

    with pytest.raises(module.PersonaProjectionError):
        module.apply(world.db, identity_path=world.identity, control_path=world.control)

    projected = _rows(world.db, _PROJECTED[_TRAIT_TABLE], "trait_id")[0]
    assert projected["description"] == "喜欢清晨散步"


def test_projected_status_drift_is_refused(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    module.apply(world.db, identity_path=world.identity, control_path=world.control)
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_PROJECTED[_TRAIT_TABLE]} SET status = 'disabled' WHERE trait_id = ?",  # noqa: S608
            (_TRAIT_ID,),
        )

    with pytest.raises(module.PersonaProjectionError):
        module.apply(world.db, identity_path=world.identity, control_path=world.control)


def test_a_projected_row_without_a_receipt_is_refused(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    module.apply(world.db, identity_path=world.identity, control_path=world.control)
    with _connect(world.db) as connection:
        connection.execute(
            f"DELETE FROM {_RECEIPTS} WHERE table_name = ?",  # noqa: S608
            (_TRAIT_TABLE,),
        )

    with pytest.raises(module.PersonaProjectionError):
        module.apply(world.db, identity_path=world.identity, control_path=world.control)

    assert _row_count(world.db, _PROJECTED[_TRAIT_TABLE]) == 1


# -- versions -----------------------------------------------------------------


def test_version_parent_chain_is_preserved(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_trait(world.db, trait_id="trait-2", normalized_key="trait-2")
    _seed_evidence(world.db, trait_id="trait-2", event_id="event-trait-2")
    _seed_version(
        world.db,
        version_id="version-0",
        number=2,
        status="superseded",
        snapshot='[{"trait_id": "trait-2"}]',
    )
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_VERSION_TABLE} SET parent_version_id = ? WHERE version_id = ?",  # noqa: S608
            ("version-0", _VERSION_ID),
        )

    module.apply(world.db, identity_path=world.identity, control_path=world.control)

    projected = {
        str(row["version_id"]): row
        for row in _rows(world.db, _PROJECTED[_VERSION_TABLE], "version_number")
    }
    assert set(projected) == {"version-0", _VERSION_ID}
    assert projected[_VERSION_ID]["parent_version_id"] == "version-0"
    assert projected["version-0"]["parent_version_id"] is None
    assert {row["subject_id"] for row in projected.values()} == {_ACCOUNT}


def test_child_of_a_refused_parent_is_not_projected(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_account(world.control, _OTHER_ACCOUNT)
    _seed_person(world.identity, _OTHER_ACCOUNT)
    _seed_person(world.identity, _FOREIGN_SUBJECT)
    _seed_relationship(
        world.identity,
        relationship_id="rel-foreign",
        source=_OTHER_ACCOUNT,
        target=_FOREIGN_SUBJECT,
        relation_type="parent_of",
    )
    _seed_trait(world.db, trait_id="trait-foreign", normalized_key="trait-foreign")
    _seed_evidence(
        world.db,
        trait_id="trait-foreign",
        event_id="event-foreign",
        subject_id=_FOREIGN_SUBJECT,
    )
    _seed_version(
        world.db,
        version_id="version-foreign",
        number=2,
        status="superseded",
        snapshot='[{"trait_id": "trait-foreign"}]',
    )
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_VERSION_TABLE} SET parent_version_id = ? WHERE version_id = ?",  # noqa: S608
            ("version-foreign", _VERSION_ID),
        )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _VERSION_TABLE, "version-foreign")
    _blocked(report, _VERSION_TABLE, _VERSION_ID)
    assert _row_count(world.db, _PROJECTED[_VERSION_TABLE]) == 0


def test_parent_cycle_is_blocked(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    _seed_version(
        world.db,
        version_id="version-cycle",
        number=2,
        status="superseded",
        snapshot='[{"trait_id": "trait-1"}]',
    )
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_VERSION_TABLE} SET parent_version_id = ? WHERE version_id = ?",  # noqa: S608
            ("version-cycle", _VERSION_ID),
        )
        connection.execute(
            f"UPDATE {_VERSION_TABLE} SET parent_version_id = ? WHERE version_id = ?",  # noqa: S608
            (_VERSION_ID, "version-cycle"),
        )

    report = module.plan(
        world.db, identity_path=world.identity, control_path=world.control
    )

    _blocked(report, _VERSION_TABLE, _VERSION_ID)
    _blocked(report, _VERSION_TABLE, "version-cycle")
    assert _row_count(world.db, _PROJECTED[_VERSION_TABLE]) == 0


# -- rollback -----------------------------------------------------------------


def test_rollback_removes_only_its_own_rows_and_is_idempotent(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    before = _source_digest(world.db)
    first = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )
    _seed_sentinel_row(world.db)

    report = module.rollback(
        world.db,
        identity_path=world.identity,
        control_path=world.control,
        migration_id=first["migration_id"],
    )

    assert report["status"] == "rolled_back"
    assert report["idempotent"] is False
    _mentions(
        report["deleted_rows"],
        _TRAIT_TABLE,
        _EVIDENCE_TABLE,
        _OBSERVATION_TABLE,
        _STYLE_TABLE,
        _CONSENT_TABLE,
        _VERSION_TABLE,
    )
    for target in _PROJECTED.values():
        assert _rows_for_migration(world.db, target, str(first["migration_id"])) == [], target
    assert _row_count(world.db, _RECEIPTS) == len(_HAPPY_ROW_IDS)
    assert _source_digest(world.db) == before
    assert _row_count(world.db, _PROJECTED[_TRAIT_TABLE]) == 1

    again = module.rollback(
        world.db,
        identity_path=world.identity,
        control_path=world.control,
        manifest_sha256=first["manifest_sha256"],
    )

    assert again["status"] == "rolled_back"
    assert again["idempotent"] is True
    assert again["deleted_rows"] == report["deleted_rows"]
    sentinel = [
        row
        for row in _rows(world.db, _PROJECTED[_TRAIT_TABLE], "trait_id")
        if row["trait_id"] == "trait-sentinel"
    ]
    assert len(sentinel) == 1, "rollback must not touch rows of another migration"


def _seed_sentinel_row(db: Path) -> None:
    with _connect(db) as connection:
        _insert(
            connection,
            _PROJECTED[_TRAIT_TABLE],
            {
                "trait_id": "trait-sentinel",
                "account_id": _OTHER_ACCOUNT,
                "category": "preference",
                "normalized_key": "trait-sentinel",
                "description": "不属于本次迁移",
                "context": "",
                "counterexample": "",
                "confidence": 0.5,
                "status": "candidate",
                "observation_count": 0,
                "created_at": _CREATED,
                "updated_at": _CREATED,
                "review_event_id": None,
                "source_account_id": _OTHER_ACCOUNT,
                "subject_id": _OTHER_ACCOUNT,
                "projection_migration_id": "other-migration",
                "projected_at": _CREATED,
            },
        )


def test_rollback_restores_a_refreshed_status(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    first = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )
    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_TRAIT_TABLE} SET status = 'confirmed' WHERE trait_id = ?",  # noqa: S608
            (_TRAIT_ID,),
        )
    refreshed = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )
    assert _rows(world.db, _PROJECTED[_TRAIT_TABLE], "trait_id")[0]["status"] == "confirmed"

    report = module.rollback(
        world.db,
        identity_path=world.identity,
        control_path=world.control,
        migration_id=refreshed["migration_id"],
    )

    assert report["status"] == "rolled_back"
    _mentions(report["restored_rows"], _TRAIT_ID)
    assert _rows(world.db, _PROJECTED[_TRAIT_TABLE], "trait_id")[0]["status"] == "candidate"
    assert first["migration_id"] != refreshed["migration_id"]


def test_source_rows_are_byte_identical_after_apply_and_rollback(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    before = _source_digest(world.db)

    applied = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )
    assert _source_digest(world.db) == before
    module.rollback(
        world.db,
        identity_path=world.identity,
        control_path=world.control,
        migration_id=applied["migration_id"],
    )

    assert _source_digest(world.db) == before


# -- guards -------------------------------------------------------------------


def test_projected_rows_cannot_be_edited_or_deleted_outside_a_rollback(
    tmp_path: Path,
) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    module.apply(world.db, identity_path=world.identity, control_path=world.control)

    with pytest.raises(sqlite3.DatabaseError):
        with _connect(world.db) as connection:
            connection.execute(
                f"UPDATE {_PROJECTED[_TRAIT_TABLE]} SET description = 'tampered' "  # noqa: S608
                "WHERE trait_id = ?",
                (_TRAIT_ID,),
            )
    with pytest.raises(sqlite3.DatabaseError):
        with _connect(world.db) as connection:
            connection.execute(
                f"DELETE FROM {_PROJECTED[_TRAIT_TABLE]} WHERE trait_id = ?",  # noqa: S608
                (_TRAIT_ID,),
            )
    with pytest.raises(sqlite3.DatabaseError):
        with _connect(world.db) as connection:
            connection.execute(
                f"UPDATE {_PROJECTED[_VERSION_TABLE]} SET snapshot_json = 'tampered'"  # noqa: S608
            )
    with pytest.raises(sqlite3.DatabaseError):
        with _connect(world.db) as connection:
            connection.execute(f"DELETE FROM {_PROJECTED[_VERSION_TABLE]}")  # noqa: S608

    assert _row_count(world.db, _PROJECTED[_TRAIT_TABLE]) == 1
    assert _row_count(world.db, _PROJECTED[_VERSION_TABLE]) == 1


def test_only_the_status_column_is_mutable_on_projected_rows(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path)
    module.apply(world.db, identity_path=world.identity, control_path=world.control)

    with _connect(world.db) as connection:
        connection.execute(
            f"UPDATE {_PROJECTED[_TRAIT_TABLE]} SET status = 'confirmed' WHERE trait_id = ?",  # noqa: S608
            (_TRAIT_ID,),
        )

    assert _rows(world.db, _PROJECTED[_TRAIT_TABLE], "trait_id")[0]["status"] == "confirmed"


# -- separate databases -------------------------------------------------------


def test_identity_and_control_may_live_in_separate_databases(tmp_path: Path) -> None:
    module = _api()
    world = _healthy_world(tmp_path, separate_identity=True, separate_control=True)
    assert world.identity != world.db
    assert world.control != world.db

    report = module.apply(
        world.db, identity_path=world.identity, control_path=world.control
    )

    assert report["applied"] is True
    assert report["source_path"] == str(world.db)
    assert report["identity_path"] == str(world.identity)
    assert report["control_path"] == str(world.control)
    for table, target in _PROJECTED.items():
        assert _row_count(world.db, target) == 1, table
    assert _rows(world.db, _PROJECTED[_TRAIT_TABLE], "trait_id")[0]["subject_id"] == _ACCOUNT
