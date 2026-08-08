from __future__ import annotations

import re
from pathlib import Path

from services.evolution.account_repository import EVOLUTION_ACCOUNT_TABLES
from services.governance.account_data import (
    _POSTGRES_ARCHIVE_DELETE_ORDER,
    _SPEAKER_DELETE_ORDER,
)
from services.governance.lifecycle_tables import (
    POSTGRES_ACCOUNT_LIFECYCLE_TABLES,
    POSTGRES_AUTHORITATIVE_ACCOUNT_TABLES,
    POSTGRES_CONTROLLER_ONLY_RLS_TABLES,
    POSTGRES_EVOLUTION_ACCOUNT_TABLES,
    POSTGRES_PROJECTION_ACCOUNT_TABLES,
)

_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\);",
    re.DOTALL,
)


def test_lifecycle_catalog_covers_every_account_scoped_postgres_table() -> None:
    root = Path(__file__).parents[3]
    schema_paths = (
        root / "services/archive/postgres_schema.sql",
        root / "services/archive/postgres_memory_schema.sql",
        root / "services/archive/postgres_skill_schema.sql",
        root / "services/persona/postgres_schema.sql",
        root / "services/digital_self/postgres_schema.sql",
        root / "services/self_model/postgres_schema.sql",
        root / "services/speaker/postgres_schema.sql",
        root / "services/voice_profile/postgres_schema.sql",
        root / "services/evolution/postgres_schema.sql",
    )
    discovered: set[str] = set()
    for path in schema_paths:
        schema = path.read_text(encoding="utf-8")
        discovered.update(
            name
            for name, definition in _CREATE_TABLE.findall(schema)
            if re.search(r"\baccount_id\b", definition)
        )
        if "CREATE TABLE IF NOT EXISTS memory_vector_documents" in schema:
            discovered.add("memory_vector_documents")

    # Validation, activation, lifecycle, and sleep-receipt rows inherit the
    # account through their owning candidate/signal rather than a duplicated
    # account_id column.
    discovered.update(POSTGRES_EVOLUTION_ACCOUNT_TABLES)
    # Deletion fences carry an account key only to reject future writes; they
    # are permanent controller state and must not enter account erasure.
    discovered.difference_update(
        set(POSTGRES_CONTROLLER_ONLY_RLS_TABLES) - set(POSTGRES_EVOLUTION_ACCOUNT_TABLES)
    )

    assert discovered == set(POSTGRES_ACCOUNT_LIFECYCLE_TABLES)
    assert "evolution_control_state" not in POSTGRES_ACCOUNT_LIFECYCLE_TABLES


def test_account_deletion_covers_the_lifecycle_catalog() -> None:
    deletion_tables = {
        *_POSTGRES_ARCHIVE_DELETE_ORDER,
        *_SPEAKER_DELETE_ORDER,
        *EVOLUTION_ACCOUNT_TABLES,
    }

    assert deletion_tables == set(POSTGRES_ACCOUNT_LIFECYCLE_TABLES)


def test_skill_state_survives_projection_rebuilds() -> None:
    skill_tables = {
        "skill_run_steps",
        "skill_runs",
        "skill_version_evidence",
        "skill_versions",
        "skill_definitions",
    }

    assert skill_tables <= set(POSTGRES_AUTHORITATIVE_ACCOUNT_TABLES)
    assert skill_tables.isdisjoint(POSTGRES_PROJECTION_ACCOUNT_TABLES)
