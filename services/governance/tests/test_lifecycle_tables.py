from __future__ import annotations

import re
from pathlib import Path

from services.governance.account_data import (
    _POSTGRES_ARCHIVE_DELETE_ORDER,
    _SPEAKER_DELETE_ORDER,
)
from services.governance.lifecycle_tables import POSTGRES_ACCOUNT_LIFECYCLE_TABLES

_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\);",
    re.DOTALL,
)


def test_lifecycle_catalog_covers_every_account_scoped_postgres_table() -> None:
    root = Path(__file__).parents[3]
    schema_paths = (
        root / "services/archive/postgres_schema.sql",
        root / "services/archive/postgres_memory_schema.sql",
        root / "services/persona/postgres_schema.sql",
        root / "services/speaker/postgres_schema.sql",
        root / "services/voice_profile/postgres_schema.sql",
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

    assert discovered == set(POSTGRES_ACCOUNT_LIFECYCLE_TABLES)


def test_account_deletion_covers_the_lifecycle_catalog() -> None:
    deletion_tables = {
        *_POSTGRES_ARCHIVE_DELETE_ORDER,
        *_SPEAKER_DELETE_ORDER,
    }

    assert deletion_tables == set(POSTGRES_ACCOUNT_LIFECYCLE_TABLES)
