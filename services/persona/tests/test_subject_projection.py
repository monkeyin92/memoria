"""Read-path tests for the subject-keyed Persona projection (P2-03).

The projection read is the product side of the operator-invoked migration, so
these tests run the real seam first and then read back.  They pin that a
migrated subject is readable, that an un-migrated database and an unprojected
subject answer with nothing instead of the account's rows, that a revoked
consent withdraws the active version while the operator can still see the rows,
and that a read never creates a schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from services.persona.migrations import account_projection as migration
from services.persona.subject_projection import (
    read_active_version,
    read_subject,
)
from services.persona.tests.test_account_projection_migration import (
    _ACCOUNT,
    _FOREIGN_SUBJECT,
    _healthy_world,
    _table_names,
)

_REVOKED_AT = "2026-09-02T00:00:00+00:00"


def _migrated(tmp_path: Path) -> Path:
    world = _healthy_world(tmp_path)
    report = migration.apply(
        world.db,
        identity_path=world.identity,
        control_path=world.control,
    )
    assert report["applied"] is True
    return world.db


def test_unmigrated_and_missing_databases_answer_nothing(tmp_path: Path) -> None:
    world = _healthy_world(tmp_path)

    assert read_active_version(world.db, _ACCOUNT) is None
    report = read_subject(world.db, _ACCOUNT)
    assert report["projection_present"] is False
    assert report["reason"] == "projection_missing"

    absent = tmp_path / "absent.sqlite3"
    assert read_active_version(absent, _ACCOUNT) is None
    assert read_subject(absent, _ACCOUNT)["reason"] == "database_missing"


def test_migrated_subject_is_readable_and_others_are_not(tmp_path: Path) -> None:
    db = _migrated(tmp_path)

    report = read_subject(db, _ACCOUNT)
    assert report["projection_present"] is True
    assert report["tables"]["persona_subject_traits"]["count"] == 1
    assert report["tables"]["persona_subject_versions"]["count"] == 1
    assert report["truncated"] is False

    active = read_active_version(db, _ACCOUNT)
    assert active is not None
    assert active["version_number"] == 1
    assert active["source_account_id"] == _ACCOUNT
    assert [str(item["trait_id"]) for item in active["snapshot"]]

    foreign = read_subject(db, _FOREIGN_SUBJECT)
    assert foreign["tables"]["persona_subject_traits"]["count"] == 0
    assert read_active_version(db, _FOREIGN_SUBJECT) is None


def test_revoked_consent_withdraws_only_the_active_version(tmp_path: Path) -> None:
    world = _healthy_world(tmp_path)
    with sqlite3.connect(world.db) as connection:
        connection.execute(
            "UPDATE persona_learning_consents SET revoked_at = ?",
            (_REVOKED_AT,),
        )
    assert (
        migration.apply(
            world.db,
            identity_path=world.identity,
            control_path=world.control,
        )["applied"]
        is True
    )

    assert read_active_version(world.db, _ACCOUNT) is None
    # The operator still reads the projected rows: revocation withdraws the
    # capsule, it does not erase the migration's evidence.
    assert (
        read_subject(world.db, _ACCOUNT)["tables"]["persona_subject_versions"]["count"] == 1
    )


def test_reads_never_create_a_schema(tmp_path: Path) -> None:
    world = _healthy_world(tmp_path)
    before = _table_names(world.db)

    read_subject(world.db, _ACCOUNT)
    read_active_version(world.db, _ACCOUNT)

    assert _table_names(world.db) == before
