"""Operator entry tests for the four account→subject migrations (P2-03).

The entry is the only supported way to run the four SQLite seams, so these
tests pin the fences rather than the seams themselves (each seam already has
its own suite): a write needs the approved manifest SHA-256 *and* the exact
confirmation token, ``plan``/``status`` never create a journal, the receipt
read path answers after an apply, and the Control API never imports any of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from services.control_api.app.database import MemoryStore
from services.digital_self.registry import DigitalSelfRegistry
from services.governance.subject_migrations import (
    APPLY_CONFIRMATION,
    MIGRATION_NAMES,
    ROLLBACK_CONFIRMATION,
    MigrationError,
    MigrationTargets,
    apply,
    plan,
    rollback,
    status,
)
from services.identity.sqlite_store import SqliteIdentityStore
from services.persona.engine import PersonaEngine
from services.persona.tests.test_account_projection_migration import (
    _healthy_world,
    _target_subjects,
)
from services.persona.tests.test_account_projection_migration import (
    _table_names as _persona_table_names,
)

_PERSONA_TRAITS = "persona_subject_traits"
_PERSONA_MIGRATIONS = "persona_projection_migrations"

_APP_ROOT = Path(__file__).resolve().parents[2] / "control_api" / "app"
_FORBIDDEN_IMPORTS = (
    "services.governance.subject_migrations",
    "services.identity.migrations",
    "services.digital_self.migrations",
    "services.persona.migrations",
    "services.memory_scope.migrations",
)


def _bare_world(tmp_path: Path) -> MigrationTargets:
    """Every schema seam needs, with no accounts, evidence or projections."""

    db = tmp_path / "memoria.sqlite3"
    PersonaEngine.sqlite(db).initialize()
    MemoryStore(str(db)).initialize()
    SqliteIdentityStore(db).initialize()
    DigitalSelfRegistry.sqlite(db).initialize()
    return MigrationTargets(control=db, identity=db)


def _healthy_persona_world(tmp_path: Path) -> MigrationTargets:
    world = _healthy_world(tmp_path)
    return MigrationTargets(control=world.control, identity=world.identity)


def _journal_snapshot(db: Path) -> set[str]:
    return {
        name
        for name in _persona_table_names(db)
        if "projection" in name or name.startswith("legacy_archive")
    }


def test_migration_names_and_tokens_are_stable() -> None:
    assert MIGRATION_NAMES == ("durable_subject", "digital_self", "persona", "memory_scope")
    assert APPLY_CONFIRMATION == "apply-account-subject-migration"
    assert ROLLBACK_CONFIRMATION == "rollback-account-subject-migration"


def test_plan_is_read_only_for_every_seam(tmp_path: Path) -> None:
    targets = _bare_world(tmp_path)
    before = _journal_snapshot(targets.control)

    for name in MIGRATION_NAMES:
        report = plan(name, targets)
        assert report["migration"] == name
        assert report["dry_run"] is True
        assert report["rows"] == []

    assert _journal_snapshot(targets.control) == before


def test_unknown_migration_is_refused(tmp_path: Path) -> None:
    targets = _bare_world(tmp_path)
    with pytest.raises(MigrationError, match="unknown migration"):
        plan("persona_projection", targets)
    with pytest.raises(MigrationError, match="unknown migration"):
        status("persona_projection", targets)


def test_apply_demands_the_approved_manifest(tmp_path: Path) -> None:
    targets = _healthy_persona_world(tmp_path)
    before = _journal_snapshot(targets.control)
    approved = plan("persona", targets)["manifest_sha256"]

    with pytest.raises(MigrationError, match="approved manifest"):
        apply(
            "persona",
            targets,
            expected_manifest_sha256="0" * 64,
            confirmation=APPLY_CONFIRMATION,
        )
    assert _journal_snapshot(targets.control) == before

    with pytest.raises(MigrationError, match="confirmation token"):
        apply(
            "persona",
            targets,
            expected_manifest_sha256=str(approved),
            confirmation="apply",
        )
    assert _journal_snapshot(targets.control) == before


def test_apply_demands_a_non_empty_manifest(tmp_path: Path) -> None:
    targets = _healthy_persona_world(tmp_path)
    with pytest.raises(MigrationError, match="expect-manifest-sha256"):
        apply(
            "persona",
            targets,
            expected_manifest_sha256="   ",
            confirmation=APPLY_CONFIRMATION,
        )


def test_apply_status_and_rollback_round_trip(tmp_path: Path) -> None:
    targets = _healthy_persona_world(tmp_path)
    approved = plan("persona", targets)["manifest_sha256"]

    report = apply(
        "persona",
        targets,
        expected_manifest_sha256=str(approved),
        confirmation=APPLY_CONFIRMATION,
    )
    assert report["migration"] == "persona"
    assert report["scope"] == "persona"
    assert report["applied"] is True
    assert report["approved_manifest_sha256"] == approved
    assert report["manifest_matches_approved"] is True
    migration_id = str(report["migration_id"])
    assert migration_id
    assert _target_subjects(targets.control, _PERSONA_TRAITS)

    stored = status("persona", targets)
    assert stored["journal_present"] is True
    assert len(stored["runs"]) == 1
    assert stored["runs"][0]["migration_id"] == migration_id
    assert stored["runs"][0]["status"] == "applied"
    assert stored["receipts"]["total"] > 0
    assert stored["quarantined"] == 0

    rolled_back = rollback(
        "persona",
        targets,
        confirmation=ROLLBACK_CONFIRMATION,
        migration_id=migration_id,
    )
    assert rolled_back["migration"] == "persona"
    assert rolled_back["status"] == "rolled_back"
    assert _target_subjects(targets.control, _PERSONA_TRAITS) == []

    after = status("persona", targets)
    assert after["runs"][0]["status"] == "rolled_back"
    assert _PERSONA_MIGRATIONS in _persona_table_names(targets.control)


def test_rollback_demands_the_confirmation_token(tmp_path: Path) -> None:
    targets = _healthy_persona_world(tmp_path)
    approved = plan("persona", targets)["manifest_sha256"]
    report = apply(
        "persona",
        targets,
        expected_manifest_sha256=str(approved),
        confirmation=APPLY_CONFIRMATION,
    )
    with pytest.raises(MigrationError, match="confirmation token"):
        rollback(
            "persona",
            targets,
            confirmation="confirm",
            migration_id=str(report["migration_id"]),
        )
    with pytest.raises(MigrationError, match="migration-id or --manifest-sha256"):
        rollback("persona", targets, confirmation=ROLLBACK_CONFIRMATION)


def test_status_is_read_only_on_an_unmigrated_database(tmp_path: Path) -> None:
    targets = _bare_world(tmp_path)
    before = _journal_snapshot(targets.control)

    for name in MIGRATION_NAMES:
        report = status(name, targets)
        assert report["journal_present"] is False
        assert report["runs"] == []
        assert report["receipts"] == {"total": 0, "by_outcome": {}}

    assert _journal_snapshot(targets.control) == before


def test_status_refuses_a_missing_journal_database(tmp_path: Path) -> None:
    targets = MigrationTargets(control=tmp_path / "absent.sqlite3")
    with pytest.raises(MigrationError, match="does not exist"):
        status("durable_subject", targets)


def test_control_api_never_imports_the_migration_seams() -> None:
    """Application startup must never run an account→subject migration."""

    offenders: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name.startswith(forbidden) for forbidden in _FORBIDDEN_IMPORTS):
                    offenders.append(f"{path.name}:{name}")
    assert offenders == []
