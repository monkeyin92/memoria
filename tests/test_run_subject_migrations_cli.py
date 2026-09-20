"""Operator CLI contract for the four account→subject migrations (P2-03).

The command is the only supported execution entry for the four SQLite seams, so
this file pins the operator surface itself: ``list`` names the four migrations
and the fences, ``plan`` and ``status`` stay read-only on an unmigrated
database, ``apply`` refuses without both fences, and ``read`` answers per
subject without ever creating a schema.  Nothing here starts the Control API,
opens a network connection or touches a device.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from scripts import run_subject_migrations as cli
from services.control_api.app.database import MemoryStore
from services.digital_self.registry import DigitalSelfRegistry
from services.identity.sqlite_store import SqliteIdentityStore
from services.persona.engine import PersonaEngine

MIGRATIONS = ("durable_subject", "digital_self", "persona", "memory_scope")


def _world(tmp_path: Path) -> Path:
    """Every schema seam needs, with no accounts, evidence or projections."""

    db = tmp_path / "memoria.sqlite3"
    PersonaEngine.sqlite(db).initialize()
    MemoryStore(str(db)).initialize()
    SqliteIdentityStore(db).initialize()
    DigitalSelfRegistry.sqlite(db).initialize()
    return db


def _tables(db: Path) -> set[str]:
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_list_names_the_four_migrations_and_the_write_fences(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, report = _run(["list"], capsys)

    assert code == 0
    assert tuple(report["migrations"]) == MIGRATIONS
    assert report["apply_confirmation"] == "apply-account-subject-migration"
    assert report["rollback_confirmation"] == "rollback-account-subject-migration"
    assert "startup never runs" in report["note"]


def test_plan_status_and_read_stay_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _world(tmp_path)
    before = _tables(db)
    targets = ["--db", str(db), "--identity", str(db)]

    plan_code, planned = _run(["plan", "--migration", "persona", *targets], capsys)
    status_code, stored = _run(["status", "--migration", "persona", *targets], capsys)
    read_code, read_back = _run(
        ["read", "--migration", "persona", "--subject", "nobody", *targets], capsys
    )

    assert (plan_code, status_code, read_code) == (0, 0, 0)
    assert planned["dry_run"] is True
    assert planned["migration"] == "persona"
    assert stored["journal_present"] is False
    assert stored["runs"] == []
    assert read_back["read_only"] is True
    assert read_back["projection_present"] is False
    assert read_back["reason"] == "projection_missing"
    assert _tables(db) == before


def test_apply_requires_the_approved_manifest_and_the_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _world(tmp_path)
    targets = ["--db", str(db), "--identity", str(db)]

    wrong_manifest = cli.main(
        [
            "apply",
            "--migration",
            "persona",
            "--expect-manifest-sha256",
            "0" * 64,
            "--confirm",
            cli.APPLY_CONFIRMATION,
            *targets,
        ]
    )
    refused = json.loads(capsys.readouterr().out)

    assert wrong_manifest == cli._EXIT_REFUSED
    assert refused["ok"] is False
    assert "no longer matches" in refused["error"]

    wrong_token = cli.main(
        ["apply", "--migration", "persona", "--expect-manifest-sha256", "0" * 64,
         "--confirm", "apply", *targets]
    )
    assert wrong_token == cli._EXIT_REFUSED


def test_read_refuses_without_a_subject(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["read", "--migration", "persona", "--db", str(tmp_path / "m.sqlite3")])
    assert exited.value.code == 2
