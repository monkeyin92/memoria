"""Contract tests for the operator-invoked durable-subject migration."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.identity.migrations import durable_subject
from services.identity.migrations.durable_subject import (
    DurableSubjectMigrationError,
    apply,
    dry_run,
    plan,
    rollback,
)

_NOW = datetime(2026, 9, 18, 1, 0, tzinfo=UTC)


def _create_identity(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE identity_persons (
                person_id TEXT PRIMARY KEY,
                status TEXT NOT NULL
            );
            CREATE TABLE identity_relationships (
                relationship_id TEXT PRIMARY KEY,
                source_person_id TEXT NOT NULL,
                target_person_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE identity_device_bindings (
                binding_id TEXT PRIMARY KEY,
                account_owner_person_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE identity_device_binding_roles (
                binding_id TEXT NOT NULL,
                person_id TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO identity_persons (person_id, status) VALUES
                ('u1', 'active'),
                ('u2', 'active'),
                ('child', 'active'),
                ('foreign', 'active'),
                ('inactive', 'disabled'),
                ('ambiguous', 'active');
            INSERT INTO identity_device_bindings (
                binding_id, account_owner_person_id, status
            ) VALUES
                ('binding-u1', 'u1', 'active'),
                ('binding-u2', 'u2', 'active'),
                ('binding-foreign', 'u2', 'active'),
                ('binding-ambiguous-u1', 'u1', 'active'),
                ('binding-ambiguous-u2', 'u2', 'active');
            INSERT INTO identity_device_binding_roles (
                binding_id, person_id, role, status
            ) VALUES
                ('binding-u1', 'child', 'member', 'active'),
                ('binding-foreign', 'foreign', 'member', 'active'),
                ('binding-ambiguous-u1', 'ambiguous', 'member', 'active'),
                ('binding-ambiguous-u2', 'ambiguous', 'member', 'active');
            """
        )


def _create_control(path: Path, *, known_subject_column: bool = True) -> None:
    subject_column = "subject_id TEXT," if known_subject_column else ""
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE accounts (
                user_id TEXT PRIMARY KEY,
                label TEXT NOT NULL
            );
            CREATE TABLE evidence_events (
                event_id TEXT PRIMARY KEY,
                account_id TEXT,
                {subject_column}
                speaker_class TEXT,
                payload_json TEXT,
                note TEXT NOT NULL
            );
            CREATE TABLE archive_evidence_events (
                event_id TEXT PRIMARY KEY,
                account_id TEXT,
                {subject_column}
                speaker_class TEXT,
                payload TEXT,
                note TEXT NOT NULL
            );
            CREATE TABLE custom_lineage (
                event_id TEXT PRIMARY KEY,
                account_id TEXT,
                subject_id TEXT,
                speaker_class TEXT,
                payload TEXT,
                note TEXT NOT NULL
            );
            INSERT INTO accounts (user_id, label) VALUES
                ('u1', 'owner'),
                ('u2', 'other owner');
            """
        )


def _insert_event(
    connection: sqlite3.Connection,
    table: str,
    event_id: str,
    account_id: str,
    subject_id: str | None,
    speaker_class: str,
    payload: object,
    note: str = "original",
) -> None:
    payload_text = payload if isinstance(payload, str) else json.dumps(payload)
    payload_column = "payload_json" if table == "evidence_events" else "payload"
    connection.execute(
        f"INSERT INTO {table} "
        f"(event_id, account_id, subject_id, speaker_class, {payload_column}, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, account_id, subject_id, speaker_class, payload_text, note),
    )


def _seed_database(tmp_path: Path, *, split_identity: bool = False) -> tuple[Path, Path | None]:
    control = tmp_path / ("control.sqlite3" if split_identity else "combined.sqlite3")
    identity = tmp_path / "identity.sqlite3" if split_identity else None
    _create_control(control)
    _create_identity(identity or control)

    with sqlite3.connect(control) as connection:
        _insert_event(
            connection,
            "evidence_events",
            "e-owner-null",
            "u1",
            None,
            "owner",
            {"message": "owner"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-child",
            "u1",
            "child",
            "member",
            {"message": "child", "subject_id": "child"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-guest",
            "u1",
            None,
            "guest",
            {"message": "guest"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-assistant",
            "u1",
            None,
            "assistant",
            {"message": "assistant"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-foreign",
            "u1",
            "foreign",
            "member",
            {"message": "foreign"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-inactive",
            "u1",
            "inactive",
            "member",
            {"message": "inactive"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-payload-conflict",
            "u1",
            None,
            "owner",
            {"subject_id": "child"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-invalid-json",
            "u1",
            None,
            "owner",
            "{not-json",
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-owner-speaker-conflict",
            "u1",
            "child",
            "owner",
            {"message": "owner says child", "subject_id": "child"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-ambiguous",
            "u1",
            "ambiguous",
            "member",
            {"message": "ambiguous"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-existing-owner",
            "u1",
            "u1",
            "owner",
            {"message": "already mapped", "subject_id": "u1"},
        )
        _insert_event(
            connection,
            "evidence_events",
            "e-unknown",
            "u1",
            "missing-person",
            "member",
            {"message": "unknown"},
        )
        _insert_event(
            connection,
            "archive_evidence_events",
            "a-owner-null",
            "u1",
            None,
            "owner",
            {"message": "archive owner"},
        )
        _insert_event(
            connection,
            "custom_lineage",
            "c-owner-null",
            "u1",
            None,
            "owner",
            {"message": "dynamic owner"},
        )
    return control, identity


def _subjects(path: Path, table: str) -> dict[str, str | None]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0]): row[1]
            for row in connection.execute(
                f"SELECT event_id, subject_id FROM {table} ORDER BY event_id"
            )
        }


def _support_tables(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name LIKE 'durable_subject_%' "
                "ORDER BY name"
            )
        ]


def _row(report: dict[str, object], table: str, event_id: str) -> dict[str, object]:
    rows = report["rows"]
    assert isinstance(rows, list)
    return next(
        item
        for item in rows
        if item["table"] == table and item["source_row_id"] == event_id
    )


def test_dry_run_and_plan_are_read_only_and_manifest_checksum_is_stable(
    tmp_path: Path,
) -> None:
    control, _ = _seed_database(tmp_path)
    before = {
        table: _subjects(control, table)
        for table in ("evidence_events", "archive_evidence_events", "custom_lineage")
    }

    first = plan(control)
    second = plan(control)
    dry = dry_run(control)

    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["manifest_sha256"] == dry["manifest_sha256"]
    assert first["dry_run"] is True
    assert first["statistics"]["changed"] == 3
    assert first["tables"] == [
        "archive_evidence_events",
        "custom_lineage",
        "evidence_events",
    ]
    assert _support_tables(control) == []
    assert {
        table: _subjects(control, table)
        for table in ("evidence_events", "archive_evidence_events", "custom_lineage")
    } == before


def test_plan_maps_only_proven_owner_history_and_quarantines_unsafe_rows(
    tmp_path: Path,
) -> None:
    control, _ = _seed_database(tmp_path)
    report = plan(control)

    assert _row(report, "evidence_events", "e-owner-null")["outcome"] == "mapped"
    assert _row(report, "evidence_events", "e-owner-null")["after_subject"] == "u1"
    assert _row(report, "evidence_events", "e-child") == {
        "table": "evidence_events",
        "source_row_id": "e-child",
        "before_row_digest": _row(report, "evidence_events", "e-child")["before_row_digest"],
        "before_subject": "child",
        "after_subject": "child",
        "reason": "valid_member_subject",
        "outcome": "mapped",
        "after_row_digest": _row(report, "evidence_events", "e-child")["after_row_digest"],
    }
    assert _row(report, "evidence_events", "e-guest")["outcome"] == "omitted"
    assert _row(report, "evidence_events", "e-guest")["reason"] == "no_subject_evidence"
    assert _row(report, "evidence_events", "e-assistant")["outcome"] == "omitted"
    assert _row(report, "evidence_events", "e-foreign")["reason"] == "foreign_subject"
    assert _row(report, "evidence_events", "e-inactive")["reason"] == "subject_inactive"
    assert _row(report, "evidence_events", "e-payload-conflict")["reason"] == "payload_subject_conflict"
    assert _row(report, "evidence_events", "e-invalid-json")["reason"] == "invalid_payload"
    assert _row(report, "evidence_events", "e-owner-speaker-conflict")["reason"] == (
        "owner_speaker_subject_conflict"
    )
    assert _row(report, "evidence_events", "e-ambiguous")["reason"] == (
        "subject_binding_ambiguous"
    )
    assert _row(report, "evidence_events", "e-unknown")["reason"] == "subject_unknown"
    assert _row(report, "evidence_events", "e-existing-owner")["reason"] == (
        "valid_owner_subject"
    )
    assert _row(report, "archive_evidence_events", "a-owner-null")["after_subject"] == "u1"
    assert _row(report, "custom_lineage", "c-owner-null")["after_subject"] == "u1"


def test_apply_changes_only_safe_rows_and_is_idempotent(tmp_path: Path) -> None:
    control, _ = _seed_database(tmp_path)
    before = _subjects(control, "evidence_events")

    first = apply(control, now=_NOW)
    second = apply(control, now=_NOW)

    assert first["applied"] is True
    assert first["status"] == "applied"
    assert first["statistics"]["changed"] == 3
    assert first["migration_id"] == f"durable-subject-v1-{first['manifest_sha256']}"
    assert first["active_migration_count"] == 1
    assert first["active_migration_ids"] == [first["migration_id"]]
    assert second["migration_id"] == first["migration_id"]
    assert second["manifest_sha256"] == first["manifest_sha256"]
    assert second["idempotent"] is True
    assert second["control_path"] == str(control.resolve())
    assert second["identity_path"] == str(control.resolve())
    assert second["planned_at"] == _NOW.isoformat()
    assert set(second["tables"]) == {
        "archive_evidence_events",
        "custom_lineage",
        "evidence_events",
    }
    assert second["active_migration_count"] == 1
    assert _subjects(control, "evidence_events")["e-owner-null"] == "u1"
    assert _subjects(control, "evidence_events")["e-child"] == "child"
    assert _subjects(control, "evidence_events")["e-guest"] is None
    assert _subjects(control, "evidence_events")["e-assistant"] is None
    assert _subjects(control, "evidence_events")["e-payload-conflict"] is None
    assert _subjects(control, "evidence_events")["e-existing-owner"] == "u1"
    assert _subjects(control, "archive_evidence_events")["a-owner-null"] == "u1"
    assert _subjects(control, "custom_lineage")["c-owner-null"] == "u1"
    assert before["e-child"] == "child"

    with sqlite3.connect(control) as connection:
        migration_count = connection.execute(
            "SELECT count(*) FROM durable_subject_migrations"
        ).fetchone()[0]
        receipt_count = connection.execute(
            "SELECT count(*) FROM durable_subject_row_receipts"
        ).fetchone()[0]
        backup_count = connection.execute(
            "SELECT count(*) FROM durable_subject_backups"
        ).fetchone()[0]
        audit_count = connection.execute(
            "SELECT count(*) FROM durable_subject_audit_events"
        ).fetchone()[0]
    assert migration_count == 1
    assert receipt_count == first["statistics"]["row_count"]
    assert backup_count == 3
    assert audit_count == first["statistics"]["row_count"]


def test_apply_rejects_source_drift_after_an_active_migration(tmp_path: Path) -> None:
    control, _ = _seed_database(tmp_path)
    apply(control, now=_NOW)
    with sqlite3.connect(control) as connection:
        connection.execute(
            "UPDATE evidence_events SET note = 'changed outside migration' "
            "WHERE event_id = 'e-guest'"
        )

    with pytest.raises(DurableSubjectMigrationError, match="target drift detected"):
        apply(control, now=_NOW)


def test_apply_rejects_identity_authority_drift_after_an_active_migration(
    tmp_path: Path,
) -> None:
    control, _ = _seed_database(tmp_path)
    apply(control, now=_NOW)
    with sqlite3.connect(control) as connection:
        connection.execute(
            "UPDATE identity_persons SET status = 'disabled' WHERE person_id = 'u1'"
        )

    with pytest.raises(
        DurableSubjectMigrationError, match="identity authority drifted"
    ):
        apply(control, now=_NOW)


def test_rollback_restores_mapped_rows_and_keeps_receipts_and_backups(
    tmp_path: Path,
) -> None:
    control, _ = _seed_database(tmp_path)
    applied = apply(control, now=_NOW)

    result = rollback(
        control,
        migration_id=str(applied["migration_id"]),
        expected_source_after_digest=str(applied["source_digest_after"]),
        now=_NOW,
    )

    assert result["rolled_back"] is True
    assert result["restored_count"] == 3
    assert _subjects(control, "evidence_events")["e-owner-null"] is None
    assert _subjects(control, "archive_evidence_events")["a-owner-null"] is None
    assert _subjects(control, "custom_lineage")["c-owner-null"] is None
    with sqlite3.connect(control) as connection:
        status = connection.execute(
            "SELECT status FROM durable_subject_migrations WHERE migration_id = ?",
            (applied["migration_id"],),
        ).fetchone()[0]
        receipts = connection.execute(
            "SELECT count(*) FROM durable_subject_row_receipts WHERE migration_id = ?",
            (applied["migration_id"],),
        ).fetchone()[0]
        backups = connection.execute(
            "SELECT count(*) FROM durable_subject_backups WHERE migration_id = ?",
            (applied["migration_id"],),
        ).fetchone()[0]
        rollback_audits = connection.execute(
            "SELECT count(*) FROM durable_subject_audit_events "
            "WHERE migration_id = ? AND action = 'migration.durable_subject.rollback'",
            (applied["migration_id"],),
        ).fetchone()[0]
    assert status == "rolled_back"
    assert receipts == applied["statistics"]["row_count"]
    assert backups == 3
    assert rollback_audits == 3

    by_manifest = rollback(
        control,
        manifest_sha256=str(applied["manifest_sha256"]),
        expected_source_after_digest=str(applied["source_digest_after"]),
        now=_NOW,
    )
    by_id = rollback(
        control,
        migration_id=str(applied["migration_id"]),
        now=_NOW,
    )
    assert by_manifest["idempotent"] is True
    assert by_manifest["restored_count"] == 3
    assert set(by_manifest["restored_rows"]) == {
        "archive_evidence_events:a-owner-null",
        "custom_lineage:c-owner-null",
        "evidence_events:e-owner-null",
    }
    assert by_manifest["rollback_source_digest"] == result["rollback_source_digest"]
    assert by_id["idempotent"] is True
    assert by_id["restored_rows"] == by_manifest["restored_rows"]


def test_multiple_active_migrations_are_reported_on_a_noop_apply(
    tmp_path: Path,
) -> None:
    control, _ = _seed_database(tmp_path)
    first = apply(control, now=_NOW)
    with sqlite3.connect(control) as connection:
        _insert_event(
            connection,
            "evidence_events",
            "e-later-owner-null",
            "u1",
            None,
            "owner",
            {"message": "later"},
        )
    second = apply(control, now=_NOW)
    noop = apply(control, now=_NOW)

    assert second["migration_id"] != first["migration_id"]
    assert noop["migration_id"] == second["migration_id"]
    assert noop["idempotent"] is True
    assert noop["active_migration_count"] == 2
    assert set(noop["active_migration_ids"]) == {
        first["migration_id"],
        second["migration_id"],
    }


def test_rollback_is_scoped_to_the_selected_migration(
    tmp_path: Path,
) -> None:
    control, _ = _seed_database(tmp_path)
    first = apply(control, now=_NOW)
    with sqlite3.connect(control) as connection:
        _insert_event(
            connection,
            "evidence_events",
            "e-later-owner-null",
            "u1",
            None,
            "owner",
            {"message": "later"},
        )
    second = apply(control, now=_NOW)

    rolled_first = rollback(control, migration_id=str(first["migration_id"]), now=_NOW)

    assert rolled_first["restored_count"] == 3
    assert _subjects(control, "evidence_events")["e-owner-null"] is None
    assert _subjects(control, "evidence_events")["e-later-owner-null"] == "u1"
    with sqlite3.connect(control) as connection:
        statuses = dict(
            connection.execute(
                "SELECT migration_id, status FROM durable_subject_migrations"
            ).fetchall()
        )
    assert statuses[str(first["migration_id"])] == "rolled_back"
    assert statuses[str(second["migration_id"])] == "applied"


def test_rollback_requires_two_selectors_to_agree(tmp_path: Path) -> None:
    control, _ = _seed_database(tmp_path)
    applied = apply(control, now=_NOW)

    with pytest.raises(
        DurableSubjectMigrationError,
        match="selectors identify different durable subject migrations",
    ):
        rollback(
            control,
            migration_id="durable-subject-v1-not-this-one",
            manifest_sha256=str(applied["manifest_sha256"]),
            now=_NOW,
        )


def test_rolled_back_migration_cannot_be_reactivated(tmp_path: Path) -> None:
    control, _ = _seed_database(tmp_path)
    applied = apply(control, now=_NOW)
    rollback(control, migration_id=str(applied["migration_id"]), now=_NOW)

    with pytest.raises(DurableSubjectMigrationError, match="cannot be reactivated"):
        apply(control, now=_NOW)


def test_audit_failure_rolls_back_source_and_support_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control, _ = _seed_database(tmp_path)
    before = _subjects(control, "evidence_events")

    def fail_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(durable_subject, "_insert_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        apply(control, now=_NOW)

    assert _subjects(control, "evidence_events") == before
    assert _support_tables(control) == []


def test_repeated_support_schema_upgrade_adds_new_columns_once(
    tmp_path: Path,
) -> None:
    control, _ = _seed_database(tmp_path)
    with sqlite3.connect(control) as connection:
        connection.execute(
            """
            CREATE TABLE durable_subject_migrations (
                migration_id TEXT PRIMARY KEY,
                manifest_sha256 TEXT NOT NULL,
                source_digest_before TEXT NOT NULL,
                source_digest_after TEXT NOT NULL,
                identity_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                statistics_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                rolled_back_at TEXT
            )
            """
        )

    result = apply(control, now=_NOW)
    with sqlite3.connect(control) as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(durable_subject_migrations)"
            )
        }
        assert {
            "rollback_source_digest",
            "control_path",
            "identity_path",
            "planned_at",
        }.issubset(columns)

    second = apply(control, now=_NOW)
    assert second["migration_id"] == result["migration_id"]


def test_no_evidence_source_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control.sqlite3"
    identity = tmp_path / "identity.sqlite3"
    with sqlite3.connect(control) as connection:
        connection.execute("CREATE TABLE accounts (user_id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO accounts (user_id) VALUES ('u1')")
    _create_identity(identity)

    with pytest.raises(
        DurableSubjectMigrationError, match="no known evidence source"
    ):
        plan(control, identity)


def test_rollback_without_migration_receipt_fails_closed(tmp_path: Path) -> None:
    control, _ = _seed_database(tmp_path)

    with pytest.raises(
        DurableSubjectMigrationError,
        match="no durable subject migration receipt found",
    ):
        rollback(control, migration_id="missing", now=_NOW)


def test_rollback_refuses_target_drift_without_partial_restore(tmp_path: Path) -> None:
    control, _ = _seed_database(tmp_path)
    applied = apply(control, now=_NOW)
    with sqlite3.connect(control) as connection:
        connection.execute(
            "UPDATE evidence_events SET note = 'operator edit' "
            "WHERE event_id = 'e-owner-null'"
        )

    with pytest.raises(DurableSubjectMigrationError, match="target drift detected"):
        rollback(control, migration_id=str(applied["migration_id"]), now=_NOW)
    assert _subjects(control, "evidence_events")["e-owner-null"] == "u1"
    with sqlite3.connect(control) as connection:
        assert connection.execute(
            "SELECT status FROM durable_subject_migrations WHERE migration_id = ?",
            (applied["migration_id"],),
        ).fetchone()[0] == "applied"


def test_split_identity_database_is_read_only_authority(tmp_path: Path) -> None:
    control, identity = _seed_database(tmp_path, split_identity=True)
    assert identity is not None

    report = apply(control, identity, now=_NOW)

    assert report["applied"] is True
    assert _subjects(control, "evidence_events")["e-owner-null"] == "u1"
    assert _support_tables(control)
    assert _support_tables(identity) == []


def test_missing_identity_authority_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control.sqlite3"
    _create_control(control)

    with pytest.raises(
        DurableSubjectMigrationError, match="missing required table identity_persons"
    ):
        plan(control)


def test_known_evidence_table_missing_subject_column_fails_closed(tmp_path: Path) -> None:
    control = tmp_path / "control.sqlite3"
    identity = tmp_path / "identity.sqlite3"
    _create_control(control, known_subject_column=False)
    _create_identity(identity)

    with pytest.raises(
        DurableSubjectMigrationError,
        match=r"known evidence table main\.(evidence_events|archive_evidence_events) "
        "is missing required columns",
    ):
        plan(control, identity)
