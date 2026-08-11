"""Legacy adult-default migration seam: dry-run / checksum / rollback."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from services.identity.migrations.legacy_adult import apply, dry_run, rollback


def _legacy_db(path: Path) -> None:
    """A pre-remediation identity_persons table (old adult default)."""
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE identity_persons (
                person_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '朋友',
                subject_category TEXT NOT NULL DEFAULT 'adult'
                    CHECK (subject_category IN ('adult', 'minor')),
                age_band TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (age_band IN ('unknown', 'under_14', '14_17', 'adult')),
                age_evidence_status TEXT NOT NULL DEFAULT 'unverified'
                    CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed')),
                locale TEXT NOT NULL DEFAULT 'zh-CN',
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO identity_persons (
                person_id, display_name, subject_category, age_band,
                age_evidence_status, created_at, updated_at
            ) VALUES
                ('legacy-adult', '老用户', 'adult', 'unknown', 'unverified', 't0', 't0'),
                ('legacy-adult-band', '老用户2', 'adult', 'adult', 'unverified', 't0', 't0'),
                ('verified-adult', '验证用户', 'adult', 'adult', 'verified', 't0', 't0'),
                ('minor', '孩子', 'minor', 'under_14', 'unverified', 't0', 't0');
            """
        )


def test_dry_run_reports_count_and_checksum(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    report = dry_run(path)
    assert report["dry_run"] is True
    assert report["affected_count"] == 2
    assert set(report["person_ids"]) == {"legacy-adult", "legacy-adult-band"}
    assert len(report["checksum_before"]) == 64


def test_apply_demotes_only_unverified_adults_with_audit(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    report = apply(path, dry_run=True)
    assert report["affected_count"] == 2
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT * FROM identity_persons").fetchall()
    assert len(rows) == 4  # dry run changed nothing

    report = apply(path)
    assert report["affected_count"] == 2
    assert report["checksum_before"] != report["checksum_after"]
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        legacy = connection.execute(
            "SELECT * FROM identity_persons WHERE person_id = 'legacy-adult'"
        ).fetchone()
        verified = connection.execute(
            "SELECT * FROM identity_persons WHERE person_id = 'verified-adult'"
        ).fetchone()
        minor = connection.execute(
            "SELECT * FROM identity_persons WHERE person_id = 'minor'"
        ).fetchone()
        audits = connection.execute(
            "SELECT action FROM identity_audit_events ORDER BY created_at"
        ).fetchall()
        backups = connection.execute(
            "SELECT * FROM identity_legacy_adult_backup"
        ).fetchall()
    assert (
        legacy["subject_category"],
        legacy["age_band"],
        legacy["age_evidence_status"],
    ) == ("unknown", "unknown", "unverified")
    assert verified["subject_category"] == "adult"
    assert verified["age_evidence_status"] == "verified"
    assert minor["subject_category"] == "minor"
    assert [str(row["action"]) for row in audits] == [
        "migration.legacy_adult.demote",
        "migration.legacy_adult.demote",
    ]
    assert len(backups) == 2


def test_apply_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    first = apply(path)
    second = apply(path)
    assert first["affected_count"] == 2
    assert second["affected_count"] == 0


def test_rollback_restores_and_verifies_checksum(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    report = apply(path)
    rollback_report = rollback(
        path, expected_checksum=report["checksum_after"]
    )
    assert rollback_report["restored_count"] == 2
    assert (
        rollback_report["checksum_after_restore"] == report["checksum_before"]
    )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        legacy = connection.execute(
            "SELECT * FROM identity_persons WHERE person_id = 'legacy-adult'"
        ).fetchone()
        backups = connection.execute(
            "SELECT * FROM identity_legacy_adult_backup"
        ).fetchall()
    assert legacy["subject_category"] == "adult"
    assert backups == []


def test_rollback_refuses_drifted_state(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path)
    report = apply(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE identity_persons SET updated_at = 't-drifted' "
            "WHERE person_id = 'legacy-adult'"
        )
    try:
        rollback(path, expected_checksum=report["checksum_after"])
    except RuntimeError as exc:
        assert "drifted" in str(exc)
    else:
        raise AssertionError("rollback must refuse drifted state")
