"""End-to-end tests for the Digital Self account -> subject projection seam.

What these tests pin
--------------------
* The seam is operator-invoked and SQLite-only: it is never called from
  startup, and it never writes, updates or deletes a source row.
* A version is projected only when the whole evidence chain holds: Control
  registration, an active Identity person, owner evidence, canonical manifest
  bytes through the product's own decoder, and owner-only evidence subjects.
  Everything else is ``quarantined`` or ``omitted`` with a receipt.
* ``manifest_json``, both manifest digests, the parent and rollback refs, every
  status and every lifecycle audit event survive byte for byte in the
  subject-keyed projection tables.
* Fences are receipt-scoped: changed content refuses a run, a status
  transition is refreshed in place, and rollback removes exactly the rows of
  its own run while staying idempotent.

The seam is imported lazily so a partially written module cannot break
collection of its neighbours.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from services.archive.life_archive import LifeArchive
from services.control_api.app.database import MemoryStore
from services.digital_self.compiler import build_manifest
from services.digital_self.domain import MemoryClaimManifestEntry
from services.digital_self.registry import DigitalSelfRegistry
from services.identity.sqlite_store import SqliteIdentityStore

_ACCOUNT = "account-owner"
_MEMBER = "person-member"
_OTHER = "person-elsewhere"
_DEVICE = "device-1"
_BINDING = "binding-1"
_CREATED = "2026-09-01T08:00:00+00:00"
_OCCURRED = "2026-09-01T07:59:00+00:00"
_VERSION_TABLE = "digital_self_versions"
_AUDIT_TABLE = "digital_self_lifecycle_audit_events"
_PROJECTED_VERSION_TABLE = "digital_self_subject_versions"
_PROJECTED_AUDIT_TABLE = "digital_self_subject_lifecycle_audit_events"
_SUPPORT_TABLES = (
    "digital_self_projection_migrations",
    "digital_self_projection_receipts",
    "digital_self_projection_quarantine",
    "digital_self_projection_backups",
    "digital_self_projection_audit_events",
)


def _api() -> Any:
    """Import the projection seam lazily (it is under active development)."""

    from services.digital_self.migrations import account_projection

    return account_projection


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
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
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
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


def _source_digest(path: Path) -> dict[str, Any]:
    """Content of the two source tables, so a run can be proven read-only."""

    payload = {
        "versions": _rows(path, _VERSION_TABLE, "version_id"),
        "audit": _rows(path, _AUDIT_TABLE, "event_id"),
    }
    return {
        "digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest(),
        "versions": len(payload["versions"]),
        "audit": len(payload["audit"]),
    }


def _build_databases(tmp_path: Path, *, separate_identity: bool = False) -> tuple[Path, Path]:
    db = tmp_path / "memoria.sqlite3"
    MemoryStore(str(db)).initialize()
    LifeArchive.sqlite(db).initialize()
    DigitalSelfRegistry.sqlite(db).initialize()
    identity = (tmp_path / "identity.sqlite3") if separate_identity else db
    SqliteIdentityStore(identity).initialize()
    return db, identity


def _seed_account(db: Path, account_id: str = _ACCOUNT) -> None:
    with _connect(db) as connection:
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


def _seed_evidence(
    db: Path,
    event_id: str,
    *,
    subject_id: str | None = _ACCOUNT,
    account_id: str = _ACCOUNT,
    speaker_class: str = "owner",
    occurred_at: str = _OCCURRED,
) -> None:
    payload_json = json.dumps(
        {"text": "历史话轮原文。", "persona_eligible": True}, ensure_ascii=False
    )
    with _connect(db) as connection:
        _insert(
            connection,
            "evidence_events",
            {
                "event_id": event_id,
                "account_id": account_id,
                "session_id": None,
                "turn_id": None,
                "generation_id": None,
                "event_type": "speech.utterance_finalized",
                "schema_version": 1,
                "occurred_at": occurred_at,
                "recorded_at": occurred_at,
                "subject_id": subject_id,
                "speaker_identity_id": None,
                "speaker_class": speaker_class,
                "source": "test.digital-self-projection",
                "consent_grant_id": None,
                "payload_json": payload_json,
                "content_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                "supersedes_event_id": None,
            },
        )


def _entry(claim_id: str, event_id: str) -> MemoryClaimManifestEntry:
    return MemoryClaimManifestEntry(
        claim_id=claim_id,
        category="life_story",
        subject_key="self",
        predicate="life_story.fact",
        value="历史记忆内容。",
        confidence=0.8,
        sensitive_domain="personal",
        extractor_version="rule-v1",
        source_event_id=event_id,
        valid_at=_OCCURRED,
    )


def _manifest_json(
    entries: list[MemoryClaimManifestEntry],
    *,
    parent: str | None = None,
    rollback_target: str | None = None,
) -> tuple[str, str, str]:
    manifest, manifest_bytes, manifest_sha256 = build_manifest(
        entries,
        compiler_version="test-compiler-1",
        policy_version="test-policy-1",
        persona_version_id=None,
        parent_version_id=parent,
        rollback_target_version_id=rollback_target,
    )
    return (
        manifest_bytes.decode("utf-8"),
        manifest_sha256,
        manifest.source_summary.source_summary_sha256,
    )


def _seed_version(
    db: Path,
    *,
    version_id: str,
    number: int = 1,
    status: str = "draft",
    account_id: str = _ACCOUNT,
    entries: list[MemoryClaimManifestEntry] | None = None,
    parent: str | None = None,
    rollback_target: str | None = None,
) -> tuple[str, str]:
    manifest_json, manifest_sha256, summary_sha256 = _manifest_json(
        entries if entries is not None else [_entry(f"claim-{version_id}", "evidence-a")],
        parent=parent,
        rollback_target=rollback_target,
    )
    with _connect(db) as connection:
        _insert(
            connection,
            _VERSION_TABLE,
            {
                "version_id": version_id,
                "account_id": account_id,
                "version_number": number,
                "status": status,
                "manifest_json": manifest_json,
                "manifest_sha256": manifest_sha256,
                "source_summary_sha256": summary_sha256,
                "parent_version_id": parent,
                "rollback_target_version_id": rollback_target,
                "created_at": _CREATED,
            },
        )
    _checkpoint(db)
    return manifest_sha256, manifest_json


def _seed_audit(
    db: Path,
    *,
    event_id: str,
    version_id: str,
    manifest_sha256: str,
    action: str = "build",
    from_status: str | None = None,
    to_status: str = "draft",
    account_id: str = _ACCOUNT,
    target_version_id: str | None = None,
    new_version_id: str | None = None,
    occurred_at: str = _CREATED,
) -> None:
    with _connect(db) as connection:
        _insert(
            connection,
            _AUDIT_TABLE,
            {
                "event_id": event_id,
                "account_id": account_id,
                "actor_account_id": account_id,
                "action": action,
                "version_id": version_id,
                "manifest_sha256": manifest_sha256,
                "from_status": from_status,
                "to_status": to_status,
                "target_version_id": target_version_id,
                "new_version_id": new_version_id,
                "occurred_at": occurred_at,
            },
        )


def _healthy_world(tmp_path: Path, *, separate_identity: bool = False) -> tuple[Path, Path]:
    """One registered owner, one owned binding, one version and its build event."""

    db, identity = _build_databases(tmp_path, separate_identity=separate_identity)
    _seed_account(db)
    _seed_person(identity, _ACCOUNT)
    _seed_binding(identity)
    _seed_evidence(db, "evidence-a")
    _seed_evidence(db, "evidence-b", occurred_at="2026-09-02T07:59:00+00:00")
    manifest_sha256, _ = _seed_version(
        db,
        version_id="version-1",
        entries=[_entry("claim-a", "evidence-a")],
    )
    _seed_audit(db, event_id="audit-1", version_id="version-1", manifest_sha256=manifest_sha256)
    return db, identity


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


def _quarantined(report: Mapping[str, Any], table: str, row_id: str, reason: str) -> None:
    row = _row(report, table, row_id)
    assert row["outcome"] == "quarantined", row
    assert row["reason"] == reason, row
    assert row["subject_id"] is None


# -- plan ---------------------------------------------------------------------


def test_plan_is_read_only_and_classifies_every_row(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    before = _source_digest(db)

    report = module.plan(db, identity_path=identity)

    assert report["scope"] == "digital_self_subject_projection"
    assert report["dry_run"] is True
    assert report["applied"] is False
    assert report["migration_id"] is None
    assert report["pending_row_count"] == 2
    assert report["manifest_sha256"] == report["manifest"]["manifest_sha256"]
    assert report["statistics"]["subjects"] == [_ACCOUNT]
    assert report["statistics"]["writes"] == 2
    version = _row(report, _VERSION_TABLE, "version-1")
    assert version["outcome"] == "mapped"
    assert version["subject_id"] == _ACCOUNT
    assert version["reason"] == "owner_binding"
    audit = _row(report, _AUDIT_TABLE, "audit-1")
    assert audit["outcome"] == "mapped"
    assert audit["subject_id"] == _ACCOUNT
    assert audit["reason"] == "lifecycle_build"
    assert not set(_SUPPORT_TABLES).intersection(_table_names(db))
    assert _PROJECTED_VERSION_TABLE not in _table_names(db)
    assert not set(_SUPPORT_TABLES).intersection(_table_names(identity))
    assert _source_digest(db) == before


def test_plan_requires_control_registration(tmp_path: Path) -> None:
    module = _api()
    db, identity = _build_databases(tmp_path)
    _seed_person(identity, _ACCOUNT)
    _seed_binding(identity)
    _seed_evidence(db, "evidence-a")
    manifest_sha256, _ = _seed_version(db, version_id="version-1")
    _seed_audit(db, event_id="audit-1", version_id="version-1", manifest_sha256=manifest_sha256)

    report = module.plan(db, identity_path=identity)

    _quarantined(report, _VERSION_TABLE, "version-1", "owner_account_unregistered")
    _quarantined(report, _AUDIT_TABLE, "audit-1", "audit_version_not_projected")


def test_plan_requires_an_active_identity_person(tmp_path: Path) -> None:
    module = _api()
    db, identity = _build_databases(tmp_path)
    _seed_account(db)
    _seed_evidence(db, "evidence-a")
    manifest_sha256, _ = _seed_version(db, version_id="version-1")
    _seed_audit(db, event_id="audit-1", version_id="version-1", manifest_sha256=manifest_sha256)

    report = module.plan(db, identity_path=identity)
    _quarantined(report, _VERSION_TABLE, "version-1", "owner_person_missing")

    _seed_person(identity, _ACCOUNT, status="disabled")
    _seed_binding(identity)
    report = module.plan(db, identity_path=identity)
    _quarantined(report, _VERSION_TABLE, "version-1", "owner_person_inactive")


def test_plan_requires_owner_evidence(tmp_path: Path) -> None:
    module = _api()
    db, identity = _build_databases(tmp_path)
    _seed_account(db)
    _seed_person(identity, _ACCOUNT)
    _seed_evidence(db, "evidence-a")
    _seed_version(db, version_id="version-1")

    report = module.plan(db, identity_path=identity)
    _quarantined(report, _VERSION_TABLE, "version-1", "owner_evidence_missing")


def test_self_relationship_is_owner_evidence(tmp_path: Path) -> None:
    module = _api()
    db, identity = _build_databases(tmp_path)
    _seed_account(db)
    _seed_person(identity, _ACCOUNT)
    _seed_relationship(
        identity,
        relationship_id="rel-self",
        source=_ACCOUNT,
        target=_ACCOUNT,
        relation_type="self",
    )
    _seed_evidence(db, "evidence-a")
    _seed_version(db, version_id="version-1")

    report = module.plan(db, identity_path=identity)

    row = _row(report, _VERSION_TABLE, "version-1")
    assert row["outcome"] == "mapped"
    assert row["reason"] == "owner_relationship"


def test_version_without_subject_evidence_is_omitted(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    with _connect(db) as connection:
        connection.execute("UPDATE evidence_events SET subject_id = NULL WHERE event_id = 'evidence-a'")
    _checkpoint(db)

    report = module.plan(db, identity_path=identity)

    row = _row(report, _VERSION_TABLE, "version-1")
    assert row["outcome"] == "omitted"
    assert row["reason"] == "no_subject_evidence"
    assert row["evidence_count"] == 1


def test_foreign_and_member_subject_evidence_is_quarantined(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    _seed_person(identity, _OTHER)
    with _connect(db) as connection:
        connection.execute(
            "UPDATE evidence_events SET subject_id = ? WHERE event_id = 'evidence-a'", (_OTHER,)
        )
    _checkpoint(db)
    _quarantined(
        module.plan(db, identity_path=identity),
        _VERSION_TABLE,
        "version-1",
        "foreign_subject_evidence",
    )

    _seed_person(identity, _MEMBER)
    _seed_role(identity, _MEMBER, role="member")
    with _connect(db) as connection:
        connection.execute(
            "UPDATE evidence_events SET subject_id = ? WHERE event_id = 'evidence-a'", (_MEMBER,)
        )
    _checkpoint(db)
    _quarantined(
        module.plan(db, identity_path=identity),
        _VERSION_TABLE,
        "version-1",
        "mixed_subject_evidence",
    )


def test_tampered_manifest_is_quarantined(tmp_path: Path) -> None:
    module = _api()
    db, identity = _build_databases(tmp_path)
    _seed_account(db)
    _seed_person(identity, _ACCOUNT)
    _seed_binding(identity)
    _seed_evidence(db, "evidence-a")
    manifest_json, manifest_sha256, summary_sha256 = _manifest_json(
        [_entry("claim-a", "evidence-a")]
    )
    with _connect(db) as connection:
        _insert(
            connection,
            _VERSION_TABLE,
            {
                "version_id": "version-1",
                "account_id": _ACCOUNT,
                "version_number": 1,
                "status": "draft",
                "manifest_json": manifest_json.replace("0.8", "0.9", 1),
                "manifest_sha256": manifest_sha256,
                "source_summary_sha256": summary_sha256,
                "parent_version_id": None,
                "rollback_target_version_id": None,
                "created_at": _CREATED,
            },
        )
    _checkpoint(db)

    report = module.plan(db, identity_path=identity)
    _quarantined(report, _VERSION_TABLE, "version-1", "manifest_integrity_failed")


def test_reference_to_an_unprojected_version_quarantines_the_child(
    tmp_path: Path,
) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    _seed_person(identity, _OTHER)
    _seed_evidence(db, "evidence-foreign", subject_id=_OTHER)
    _seed_version(
        db,
        version_id="version-parent",
        number=2,
        entries=[_entry("claim-p", "evidence-foreign")],
    )
    _seed_version(
        db,
        version_id="version-child",
        number=3,
        parent="version-parent",
        entries=[_entry("claim-c", "evidence-a")],
    )

    report = module.plan(db, identity_path=identity)

    _quarantined(report, _VERSION_TABLE, "version-parent", "foreign_subject_evidence")
    _quarantined(
        report,
        _VERSION_TABLE,
        "version-child",
        "parent_version_id_reference_not_projected",
    )


def test_missing_reference_quarantines_the_child(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    manifest_json, manifest_sha256, summary_sha256 = _manifest_json(
        [_entry("claim-c", "evidence-a")],
        parent="version-ghost",
    )
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=OFF")
    _insert(
        connection,
        _VERSION_TABLE,
        {
            "version_id": "version-child",
            "account_id": _ACCOUNT,
            "version_number": 2,
            "status": "draft",
            "manifest_json": manifest_json,
            "manifest_sha256": manifest_sha256,
            "source_summary_sha256": summary_sha256,
            "parent_version_id": "version-ghost",
            "rollback_target_version_id": None,
            "created_at": _CREATED,
        },
    )
    connection.commit()
    connection.close()
    _checkpoint(db)

    report = module.plan(db, identity_path=identity)
    _quarantined(
        report,
        _VERSION_TABLE,
        "version-child",
        "parent_version_id_reference_missing",
    )


# -- apply --------------------------------------------------------------------


def test_apply_projects_bytes_digests_and_lifecycle_history(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    _manifest_json_for_run, manifest_sha256, summary_sha256 = _manifest_json(
        [_entry("claim-a", "evidence-a")]
    )
    before = _source_digest(db)

    report = module.apply(db, identity_path=identity)

    assert report["applied"] is True
    assert report["idempotent"] is False
    assert report["dry_run"] is False
    assert str(report["manifest_sha256"]) in str(report["migration_id"])
    assert _row_count(db, _PROJECTED_VERSION_TABLE) == 1
    assert _row_count(db, _PROJECTED_AUDIT_TABLE) == 1
    projected = _rows(db, _PROJECTED_VERSION_TABLE, "version_id")[0]
    source = _rows(db, _VERSION_TABLE, "version_id")[0]
    assert projected["manifest_json"] == source["manifest_json"]
    assert (
        hashlib.sha256(projected["manifest_json"].encode("utf-8")).hexdigest()
        == manifest_sha256
    ), "the projected bytes must still be the bytes the digest was taken over"
    assert projected["manifest_sha256"] == source["manifest_sha256"] == manifest_sha256
    assert projected["source_summary_sha256"] == source["source_summary_sha256"]
    assert projected["source_summary_sha256"] == summary_sha256
    assert projected["parent_version_id"] == source["parent_version_id"]
    assert projected["rollback_target_version_id"] == source["rollback_target_version_id"]
    assert projected["created_at"] == source["created_at"]
    assert projected["version_number"] == source["version_number"]
    assert projected["status"] == source["status"] == "draft"
    assert projected["subject_id"] == projected["account_id"] == _ACCOUNT
    projected_audit = _rows(db, _PROJECTED_AUDIT_TABLE, "event_id")[0]
    source_audit = _rows(db, _AUDIT_TABLE, "event_id")[0]
    for column in (
        "account_id",
        "actor_account_id",
        "action",
        "version_id",
        "manifest_sha256",
        "from_status",
        "to_status",
        "target_version_id",
        "new_version_id",
        "occurred_at",
    ):
        assert projected_audit[column] == source_audit[column]
    assert projected_audit["subject_id"] == _ACCOUNT
    assert _row_count(db, "digital_self_projection_receipts") == 2
    assert _row_count(db, "digital_self_projection_backups") == 2
    assert _row_count(db, "digital_self_projection_migrations") == 1
    assert _row_count(db, "digital_self_projection_audit_events") == 3
    assert _row_count(db, "digital_self_projection_quarantine") == 0
    assert _source_digest(db) == before


def test_apply_rejects_a_stale_expected_manifest(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)

    with pytest.raises(module.DigitalSelfProjectionError):
        module.apply(db, identity_path=identity, expected_manifest_sha256="0" * 64)

    assert _row_count(db, "digital_self_projection_migrations") == 0
    assert _PROJECTED_VERSION_TABLE not in _table_names(db)


def test_reapplying_a_covered_plan_writes_nothing(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    first = module.apply(db, identity_path=identity)
    before = (
        _row_count(db, _PROJECTED_VERSION_TABLE),
        _row_count(db, _PROJECTED_AUDIT_TABLE),
        _row_count(db, "digital_self_projection_migrations"),
        _row_count(db, "digital_self_projection_receipts"),
    )

    second = module.apply(db, identity_path=identity)

    assert second["applied"] is False
    assert second["idempotent"] is True
    assert second["migration_id"] is None
    assert second["pending_row_count"] == 0
    assert second["statistics"]["writes"] == 0
    assert first["statistics"]["writes"] == 2
    assert all(
        row["outcome"] == "already_projected" for row in _plan_rows(second)
    )
    assert (
        _row_count(db, _PROJECTED_VERSION_TABLE),
        _row_count(db, _PROJECTED_AUDIT_TABLE),
        _row_count(db, "digital_self_projection_migrations"),
        _row_count(db, "digital_self_projection_receipts"),
    ) == before


def test_status_transition_is_refreshed_in_place(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    first = module.apply(db, identity_path=identity)
    manifest_json = _rows(db, _VERSION_TABLE, "version_id")[0]["manifest_json"]
    with _connect(db) as connection:
        connection.execute(
            "UPDATE digital_self_versions SET status = 'testing' WHERE version_id = 'version-1'"
        )
    _checkpoint(db)

    report = module.apply(db, identity_path=identity)

    row = _row(report, _VERSION_TABLE, "version-1")
    assert row["outcome"] == "refreshed"
    assert row["before_status"] == "draft"
    assert row["after_status"] == "testing"
    assert report["applied"] is True
    projected = _rows(db, _PROJECTED_VERSION_TABLE, "version_id")[0]
    assert projected["status"] == "testing"
    assert projected["manifest_json"] == manifest_json
    assert str(projected["projection_migration_id"]) == str(first["migration_id"])
    assert _row_count(db, _PROJECTED_VERSION_TABLE) == 1
    assert _row_count(db, "digital_self_projection_migrations") == 2

    rollback = module.rollback(
        db, identity_path=identity, migration_id=report["migration_id"]
    )
    assert rollback["deleted_rows"] == []
    assert rollback["restored_rows"] == [f"{_VERSION_TABLE}:version-1"]
    restored = _rows(db, _PROJECTED_VERSION_TABLE, "version_id")[0]
    assert restored["status"] == "draft"
    assert restored["manifest_json"] == manifest_json


def test_content_drift_under_a_receipt_is_refused(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    first = module.apply(db, identity_path=identity)
    manifest_json = _rows(db, _VERSION_TABLE, "version_id")[0]["manifest_json"]
    with _connect(db) as connection:
        connection.execute("DROP TRIGGER digital_self_manifest_immutable")
        connection.execute(
            "UPDATE digital_self_versions SET manifest_json = ? WHERE version_id = 'version-1'",
            (manifest_json.replace("0.8", "0.9", 1),),
        )
    _checkpoint(db)

    with pytest.raises(module.DigitalSelfProjectionError):
        module.plan(db, identity_path=identity)
    with pytest.raises(module.DigitalSelfProjectionError):
        module.apply(db, identity_path=identity)
    with pytest.raises(module.DigitalSelfProjectionError):
        module.rollback(db, identity_path=identity, migration_id=first["migration_id"])
    assert _row_count(db, _PROJECTED_VERSION_TABLE) == 1


def test_projected_status_drift_is_refused(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    first = module.apply(db, identity_path=identity)
    with _connect(db) as connection:
        connection.execute(
            "UPDATE digital_self_subject_versions SET status = 'revoked' "
            "WHERE version_id = 'version-1'"
        )

    with pytest.raises(module.DigitalSelfProjectionError):
        module.apply(db, identity_path=identity)
    with pytest.raises(module.DigitalSelfProjectionError):
        module.rollback(db, identity_path=identity, migration_id=first["migration_id"])


def test_rollback_removes_only_its_own_rows_and_is_idempotent(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    before = _source_digest(db)
    first = module.apply(db, identity_path=identity)

    report = module.rollback(db, identity_path=identity, migration_id=first["migration_id"])

    assert report["status"] == "rolled_back"
    assert report["idempotent"] is False
    assert report["deleted_rows"] == [
        f"{_AUDIT_TABLE}:audit-1",
        f"{_VERSION_TABLE}:version-1",
    ]
    assert report["restored_rows"] == []
    assert _row_count(db, _PROJECTED_VERSION_TABLE) == 0
    assert _row_count(db, _PROJECTED_AUDIT_TABLE) == 0
    assert _row_count(db, "digital_self_projection_receipts") == 2
    assert _row_count(db, "digital_self_projection_migrations") == 1
    assert _source_digest(db) == before

    again = module.rollback(
        db, identity_path=identity, manifest_sha256=first["manifest_sha256"]
    )
    assert again["idempotent"] is True
    assert again["status"] == "rolled_back"
    assert again["deleted_rows"] == report["deleted_rows"]

    third = module.apply(db, identity_path=identity)
    assert third["applied"] is True
    assert str(third["migration_id"]) != str(first["migration_id"])
    assert _row_count(db, _PROJECTED_VERSION_TABLE) == 1
    assert _row_count(db, _PROJECTED_AUDIT_TABLE) == 1
    assert _row_count(db, "digital_self_projection_migrations") == 2


def test_projected_rows_cannot_be_edited_or_deleted_outside_a_rollback(
    tmp_path: Path,
) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    module.apply(db, identity_path=identity)

    with pytest.raises(sqlite3.DatabaseError):
        with _connect(db) as connection:
            connection.execute(
                "UPDATE digital_self_subject_versions SET manifest_json = 'tampered' "
                "WHERE version_id = 'version-1'"
            )
    with pytest.raises(sqlite3.DatabaseError):
        with _connect(db) as connection:
            connection.execute(
                "DELETE FROM digital_self_subject_versions WHERE version_id = 'version-1'"
            )
    with pytest.raises(sqlite3.DatabaseError):
        with _connect(db) as connection:
            connection.execute(
                "UPDATE digital_self_subject_lifecycle_audit_events SET to_status = 'revoked'"
            )
    assert _row_count(db, _PROJECTED_VERSION_TABLE) == 1
    assert _row_count(db, _PROJECTED_AUDIT_TABLE) == 1


def test_identity_may_live_in_a_separate_database(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path, separate_identity=True)
    assert identity != db

    report = module.apply(db, identity_path=identity)

    assert report["applied"] is True
    assert report["identity_path"] == str(identity)
    assert report["source_path"] == str(db)
    projected = _rows(db, _PROJECTED_VERSION_TABLE, "version_id")[0]
    assert projected["subject_id"] == _ACCOUNT


def test_audit_event_of_an_unprojected_version_is_quarantined(tmp_path: Path) -> None:
    module = _api()
    db, identity = _healthy_world(tmp_path)
    _seed_person(identity, _OTHER)
    _seed_evidence(db, "evidence-foreign", subject_id=_OTHER)
    manifest_sha256, _ = _seed_version(
        db,
        version_id="version-foreign",
        number=2,
        entries=[_entry("claim-f", "evidence-foreign")],
    )
    _seed_audit(
        db,
        event_id="audit-foreign",
        version_id="version-foreign",
        manifest_sha256=manifest_sha256,
    )

    report = module.plan(db, identity_path=identity)

    _quarantined(report, _VERSION_TABLE, "version-foreign", "foreign_subject_evidence")
    _quarantined(report, _AUDIT_TABLE, "audit-foreign", "audit_version_not_projected")
    assert _row(report, _AUDIT_TABLE, "audit-1")["outcome"] == "mapped"


@pytest.mark.asyncio
async def test_real_digital_self_build_projects_end_to_end(tmp_path: Path) -> None:
    """A version the real registry built projects without touching it."""

    from services.archive.domain import EvidenceEvent
    from services.archive.memory_catalog import MemoryCatalog
    from services.archive.memory_extractor import RuleBasedMemoryExtractor
    from services.digital_self.registry import DigitalSelfRegistry as Registry
    from services.persona.engine import PersonaEngine
    from services.self_model.registry import SelfModelRegistry

    module = _api()
    db = tmp_path / "memoria.sqlite3"
    MemoryStore(str(db)).initialize()
    archive = LifeArchive.sqlite(db)
    archive.initialize()
    catalog = MemoryCatalog.sqlite(db, extractor=RuleBasedMemoryExtractor())
    catalog.initialize()
    PersonaEngine.sqlite(db).initialize()
    SelfModelRegistry.sqlite(db).initialize()
    registry = Registry.sqlite(db)
    registry.initialize()
    identity = tmp_path / "identity.sqlite3"
    SqliteIdentityStore(identity).initialize()
    _seed_account(db)
    _seed_person(identity, _ACCOUNT)
    _seed_binding(identity)
    await archive.record(
        EvidenceEvent(
            event_id="evidence-real",
            account_id=_ACCOUNT,
            event_type="speech.utterance_finalized",
            occurred_at=datetime(2026, 9, 1, 7, 59, tzinfo=UTC),
            speaker_class="owner",
            source="test.digital-self-projection",
            subject_id=_ACCOUNT,
            payload={
                "text": "我们家的家训是答应别人的事一定做到。",
                "interaction_mode": "companion",
                "simulated_output": False,
                "history_eligible": True,
                "owner_projection_eligible": True,
            },
        )
    )
    _checkpoint(db)
    await catalog.compile_pending()
    # The review path confirms a compiled claim before a Digital Self build.
    with _connect(db) as connection:
        connection.execute("UPDATE memory_claims SET status = 'confirmed'")
    _checkpoint(db)
    version = await registry.build(account_id=_ACCOUNT)

    report = module.apply(db, identity_path=identity)

    assert report["applied"] is True
    projected = _rows(db, _PROJECTED_VERSION_TABLE, "version_id")[0]
    source = _rows(db, _VERSION_TABLE, "version_id")[0]
    assert projected["version_id"] == version.version_id
    assert projected["subject_id"] == _ACCOUNT
    assert projected["status"] == "draft"
    assert projected["manifest_json"] == source["manifest_json"]
    assert projected["manifest_sha256"] == version.manifest_sha256
    audit = _rows(db, _PROJECTED_AUDIT_TABLE, "event_id")
    assert [row["action"] for row in audit] == ["build"]
    assert [row["subject_id"] for row in audit] == [_ACCOUNT]


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


def _seed_role(
    identity: Path,
    person_id: str,
    *,
    role: str = "member",
    binding_id: str = _BINDING,
    status: str = "active",
) -> None:
    with _connect(identity) as connection:
        _insert(
            connection,
            "identity_device_binding_roles",
            {
                "binding_id": binding_id,
                "person_id": person_id,
                "role": role,
                "status": status,
                "permissions_json": "[]",
                "granted_at": _CREATED,
                "ended_at": None if status == "active" else _CREATED,
            },
        )


def _seed_relationship(
    identity: Path,
    *,
    relationship_id: str,
    source: str,
    target: str,
    relation_type: str = "family_member_of",
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
