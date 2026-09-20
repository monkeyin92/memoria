"""Explicit operator entry for the four account→subject migrations (P2-03).

The four seams exist as separate operator-invoked modules --
``services.identity.migrations.durable_subject``,
``services.digital_self.migrations.account_projection``,
``services.persona.migrations.account_projection`` and
``services.memory_scope.migrations.legacy_archive`` -- but P2-03 requires one
audited entry instead of ad-hoc calls:

* application startup never runs a migration.  The Control API does not import
  this module, and every mutation below demands an operator fence: the exact
  confirmation token plus the manifest SHA-256 the operator approved while
  planning;
* the read path of the four seams lives here too, so an operator can query
  stored runs, receipt outcomes and quarantined rows without opening the
  SQLite files by hand (``回执可查询``);
* reports stay plain JSON, so any deployment tooling can wrap the command and
  archive the receipt.

This module is a seam, not an authority: it never rewrites a migration's
verdict, never picks a database for the caller and never retries a failed
apply with different inputs.  The four migrations remain SQLite-only bridges
for the development/legacy stores.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.digital_self.migrations import account_projection as _digital_self
from services.identity.migrations import durable_subject as _durable_subject
from services.memory_scope.migrations import legacy_archive as _memory_scope
from services.persona import subject_projection as _persona_projection
from services.persona.migrations import account_projection as _persona

__all__ = [
    "MIGRATION_NAMES",
    "MigrationError",
    "MigrationTargets",
    "apply",
    "plan",
    "read",
    "rollback",
    "status",
]

#: The four P2-03 seams, in lineage order.
MIGRATION_NAMES: tuple[str, ...] = (
    "durable_subject",
    "digital_self",
    "persona",
    "memory_scope",
)

#: Typed-confirmation tokens.  They are deliberately long and verb-specific so
#: a mistyped flag cannot start a write.
APPLY_CONFIRMATION = "apply-account-subject-migration"
ROLLBACK_CONFIRMATION = "rollback-account-subject-migration"

#: Hard cap for one status read; the operator reads the journals directly when
#: they need a longer history.
_STATUS_RUN_LIMIT = 20

#: Default page size for one subject read.
_SUBJECT_READ_LIMIT = 50


class MigrationError(RuntimeError):
    """The operator entry refused a request (fail closed)."""


@dataclass(frozen=True, slots=True)
class MigrationTargets:
    """Operator-supplied database paths for one or more migrations.

    ``control`` holds the Control API SQLite store (evidence, accounts,
    digital self and Persona tables); ``identity`` holds the identity authority
    (``None`` means the control database carries both schemas); ``archive`` is
    the legacy Archive store migrated into the Memory Scope adapter, and
    ``target`` is the separate Memory Scope database (``None`` keeps the target
    inside the archive file, exactly like the seam's own default).
    """

    control: Path
    identity: Path | None = None
    archive: Path | None = None
    target: Path | None = None

    def __post_init__(self) -> None:
        if not str(self.control).strip():
            raise MigrationError("a control database path is required")

    @property
    def archive_path(self) -> Path:
        return self.control if self.archive is None else self.archive


@dataclass(frozen=True, slots=True)
class _Seam:
    """How one migration is called, fenced and read back."""

    name: str
    scope: str
    journal_table: str
    receipt_table: str
    quarantine_table: str | None
    plan: Callable[..., dict[str, Any]]
    read: Callable[[MigrationTargets, str, int], dict[str, Any]]
    apply: Callable[..., dict[str, Any]]
    rollback: Callable[..., dict[str, Any]]
    journal_db: Callable[[MigrationTargets], Path]
    #: Whether the seam itself enforces ``expected_manifest_sha256``.  The
    #: command adds the fence when the seam does not.
    fences_manifest: bool


def _seam(name: str) -> _Seam:
    try:
        return _SEAMS[name]
    except KeyError:
        raise MigrationError(f"unknown migration: {name}") from None


def _control_journal(targets: MigrationTargets) -> Path:
    return targets.control


def _archive_journal(targets: MigrationTargets) -> Path:
    return targets.archive_path if targets.target is None else targets.target


def _durable_plan(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    del now  # The durable subject manifest is wall-clock independent.
    return _durable_subject.plan(targets.control, targets.identity)


def _durable_apply(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    return _durable_subject.apply(targets.control, targets.identity, now=now)


def _durable_rollback(
    targets: MigrationTargets,
    *,
    migration_id: str | None,
    manifest_sha256: str | None,
    expected_source_after_digest: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    return _durable_subject.rollback(
        targets.control,
        targets.identity,
        migration_id=migration_id,
        manifest_sha256=manifest_sha256,
        expected_source_after_digest=expected_source_after_digest,
        now=now,
    )


def _digital_self_plan(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    return _digital_self.plan(
        targets.control,
        identity_path=targets.identity,
        control_path=targets.control,
        now=now,
    )


def _digital_self_apply(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    return _digital_self.apply(
        targets.control,
        identity_path=targets.identity,
        control_path=targets.control,
        now=now,
    )


def _digital_self_rollback(
    targets: MigrationTargets,
    *,
    migration_id: str | None,
    manifest_sha256: str | None,
    expected_source_after_digest: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    del expected_source_after_digest  # Verified through the seam's own digests.
    return _digital_self.rollback(
        targets.control,
        identity_path=targets.identity,
        control_path=targets.control,
        migration_id=migration_id,
        manifest_sha256=manifest_sha256,
        now=now,
    )


def _persona_plan(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    return _persona.plan(
        targets.control,
        identity_path=targets.identity,
        control_path=targets.control,
        now=now,
    )


def _persona_apply(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    return _persona.apply(
        targets.control,
        identity_path=targets.identity,
        control_path=targets.control,
        now=now,
    )


def _persona_rollback(
    targets: MigrationTargets,
    *,
    migration_id: str | None,
    manifest_sha256: str | None,
    expected_source_after_digest: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    del expected_source_after_digest  # Verified through the seam's own digests.
    return _persona.rollback(
        targets.control,
        identity_path=targets.identity,
        control_path=targets.control,
        migration_id=migration_id,
        manifest_sha256=manifest_sha256,
        now=now,
    )


def _memory_scope_plan(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    return _memory_scope.plan(targets.archive_path, targets.target, now=now)


def _memory_scope_apply(targets: MigrationTargets, now: datetime | None) -> dict[str, Any]:
    return _memory_scope.apply(targets.archive_path, targets.target, now=now)


def _memory_scope_rollback(
    targets: MigrationTargets,
    *,
    migration_id: str | None,
    manifest_sha256: str | None,
    expected_source_after_digest: str | None,
    now: datetime | None,
) -> dict[str, Any]:
    del expected_source_after_digest  # Verified through the seam's own digests.
    return _memory_scope.rollback(
        targets.archive_path,
        targets.target,
        migration_id=migration_id,
        manifest_sha256=manifest_sha256,
        now=now,
    )


def _durable_read(
    targets: MigrationTargets, subject_id: str, limit: int
) -> dict[str, Any]:
    return _durable_subject.read_subject(
        targets.control, targets.identity, subject_id=subject_id, limit=limit
    )


def _digital_self_read(
    targets: MigrationTargets, subject_id: str, limit: int
) -> dict[str, Any]:
    return _digital_self.read_subject(targets.control, subject_id, limit=limit)


def _persona_read(
    targets: MigrationTargets, subject_id: str, limit: int
) -> dict[str, Any]:
    return _persona_projection.read_subject(targets.control, subject_id, limit=limit)


def _memory_scope_read(
    targets: MigrationTargets, subject_id: str, limit: int
) -> dict[str, Any]:
    return _memory_scope.read_subject(
        _archive_journal(targets), subject_id, limit=limit
    )


_SEAMS: dict[str, _Seam] = {
    "durable_subject": _Seam(
        name="durable_subject",
        scope="identity",
        journal_table="durable_subject_migrations",
        receipt_table="durable_subject_row_receipts",
        quarantine_table=None,
        plan=_durable_plan,
        read=_durable_read,
        apply=_durable_apply,
        rollback=_durable_rollback,
        journal_db=_control_journal,
        fences_manifest=False,
    ),
    "digital_self": _Seam(
        name="digital_self",
        scope="digital_self",
        journal_table="digital_self_projection_migrations",
        receipt_table="digital_self_projection_receipts",
        quarantine_table="digital_self_projection_quarantine",
        plan=_digital_self_plan,
        read=_digital_self_read,
        apply=_digital_self_apply,
        rollback=_digital_self_rollback,
        journal_db=_control_journal,
        fences_manifest=True,
    ),
    "persona": _Seam(
        name="persona",
        scope="persona",
        journal_table="persona_projection_migrations",
        receipt_table="persona_projection_receipts",
        quarantine_table="persona_projection_quarantine",
        plan=_persona_plan,
        read=_persona_read,
        apply=_persona_apply,
        rollback=_persona_rollback,
        journal_db=_control_journal,
        fences_manifest=True,
    ),
    "memory_scope": _Seam(
        name="memory_scope",
        scope="memory_scope",
        journal_table="legacy_archive_migrations",
        receipt_table="legacy_archive_row_receipts",
        quarantine_table=None,
        plan=_memory_scope_plan,
        read=_memory_scope_read,
        apply=_memory_scope_apply,
        rollback=_memory_scope_rollback,
        journal_db=_archive_journal,
        fences_manifest=True,
    ),
}


def _report_manifest_sha256(report: Mapping[str, Any]) -> str | None:
    value = str(report.get("manifest_sha256") or "").strip()
    if value:
        return value
    manifest = report.get("manifest")
    if isinstance(manifest, Mapping):
        nested = str(manifest.get("manifest_sha256") or "").strip()
        if nested:
            return nested
    return None


def _manifest_sha256(report: Mapping[str, Any]) -> str:
    value = _report_manifest_sha256(report)
    if not value:
        raise MigrationError(
            "the migration seam returned no manifest SHA-256; this build cannot "
            "fence the write and refuses to continue"
        )
    return value


def _now(now: datetime | None) -> datetime:
    return datetime.now(UTC) if now is None else now


def plan(
    migration: str,
    targets: MigrationTargets,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one migration's read-only manifest."""

    seam = _seam(migration)
    report = dict(seam.plan(targets, now))
    report["migration"] = seam.name
    report["scope"] = seam.scope
    report["dry_run"] = True
    return report


def read(
    migration: str,
    targets: MigrationTargets,
    *,
    subject_id: str,
    limit: int = _SUBJECT_READ_LIMIT,
) -> dict[str, Any]:
    """Read one subject's migrated rows (read-only; nothing here writes).

    The read path of the four seams: an operator asks what one subject owns
    after the migration, and every seam answers from its own target rows.  A
    subject that was never migrated answers with zero rows -- there is no
    fallback to the account-keyed source.
    """

    seam = _seam(migration)
    if limit < 1:
        raise MigrationError("limit must be positive")
    if not subject_id.strip():
        raise MigrationError("read requires a subject_id")
    report = dict(seam.read(targets, subject_id, limit))
    report["migration"] = seam.name
    report["scope"] = seam.scope
    report["read_only"] = True
    return report


def apply(
    migration: str,
    targets: MigrationTargets,
    *,
    expected_manifest_sha256: str,
    confirmation: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one approved manifest; refuses without both operator fences.

    ``expected_manifest_sha256`` is the SHA-256 the operator approved from a
    ``plan`` report.  It is compared against a fresh plan before any write --
    including for seams that re-check it internally -- so a source that moved
    between plan and apply fails closed instead of writing a different set.
    """

    seam = _seam(migration)
    expected = expected_manifest_sha256.strip()
    if not expected:
        raise MigrationError(
            "apply requires --expect-manifest-sha256 from the approved plan"
        )
    if confirmation != APPLY_CONFIRMATION:
        raise MigrationError(
            f"apply requires the confirmation token {APPLY_CONFIRMATION!r}"
        )
    current = seam.plan(targets, now)
    actual = _manifest_sha256(current)
    if actual != expected:
        raise MigrationError(
            "the current plan no longer matches the approved manifest SHA-256"
        )
    report = dict(seam.apply(targets, _now(now)))
    report["migration"] = seam.name
    report["scope"] = seam.scope
    report["approved_manifest_sha256"] = expected
    stored = _report_manifest_sha256(report)
    report["manifest_matches_approved"] = None if stored is None else stored == expected
    return report


def rollback(
    migration: str,
    targets: MigrationTargets,
    *,
    confirmation: str,
    migration_id: str | None = None,
    manifest_sha256: str | None = None,
    expected_source_after_digest: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Roll back exactly one stored run; refuses without the fence and an id."""

    seam = _seam(migration)
    if confirmation != ROLLBACK_CONFIRMATION:
        raise MigrationError(
            f"rollback requires the confirmation token {ROLLBACK_CONFIRMATION!r}"
        )
    if not (migration_id or manifest_sha256):
        raise MigrationError("rollback requires --migration-id or --manifest-sha256")
    report = dict(
        seam.rollback(
            targets,
            migration_id=migration_id,
            manifest_sha256=manifest_sha256,
            expected_source_after_digest=expected_source_after_digest,
            now=_now(now),
        )
    )
    report["migration"] = seam.name
    report["scope"] = seam.scope
    return report


def _read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _count(connection: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> int:
    row = connection.execute(sql, params).fetchone()
    return 0 if row is None else int(row[0])


def status(
    migration: str,
    targets: MigrationTargets,
    *,
    run_limit: int = _STATUS_RUN_LIMIT,
) -> dict[str, Any]:
    """Read one migration's stored runs and receipt outcomes (read-only).

    The journals are the audit receipt: this command is the supported way to
    query what an apply wrote, what it quarantined and which run a rollback
    must name.  It never creates a schema and never writes.
    """

    seam = _seam(migration)
    if run_limit < 1:
        raise MigrationError("run_limit must be positive")
    journal = seam.journal_db(targets)
    if not journal.exists():
        raise MigrationError(f"journal database does not exist: {journal}")
    connection = _read_only(journal)
    try:
        present = _table_exists(connection, seam.journal_table)
        report: dict[str, Any] = {
            "migration": seam.name,
            "scope": seam.scope,
            "journal_path": str(journal),
            "journal_present": present,
            "runs": [],
            "receipts": {"total": 0, "by_outcome": {}},
            "quarantined": 0,
        }
        if not present:
            return report
        runs = [
            dict(row)
            for row in connection.execute(
                f"SELECT * FROM {seam.journal_table} "
                "ORDER BY applied_at DESC, migration_id DESC LIMIT ?",
                (run_limit,),
            ).fetchall()
        ]
        report["runs"] = runs
        receipt_rows = connection.execute(
            f"SELECT outcome, COUNT(*) FROM {seam.receipt_table} "
            "GROUP BY outcome"
        ).fetchall()
        by_outcome = {str(row[0]): int(row[1]) for row in receipt_rows}
        report["receipts"] = {
            "total": sum(by_outcome.values()),
            "by_outcome": by_outcome,
        }
        if seam.quarantine_table is not None and _table_exists(
            connection, seam.quarantine_table
        ):
            report["quarantined"] = _count(
                connection,
                f"SELECT COUNT(*) FROM {seam.quarantine_table}",
                (),
            )
        return report
    finally:
        connection.close()
