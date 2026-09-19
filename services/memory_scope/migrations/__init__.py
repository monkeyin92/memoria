"""Operator-invoked migrations for the Memory Scope SQLite adapter."""

from services.memory_scope.migrations.legacy_archive import (
    LegacyArchiveMigrationError,
    apply,
    dry_run,
    plan,
    rollback,
)

__all__ = [
    "LegacyArchiveMigrationError",
    "apply",
    "dry_run",
    "plan",
    "rollback",
]
