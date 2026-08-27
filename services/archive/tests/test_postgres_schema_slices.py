from __future__ import annotations

from pathlib import Path


def test_unified_postgres_schema_matches_slice_concatenation() -> None:
    archive_dir = Path(__file__).resolve().parents[1]
    unified = (archive_dir / "postgres_schema.sql").read_text(encoding="utf-8")
    slices = "".join(
        (archive_dir / name).read_text(encoding="utf-8")
        for name in (
            "postgres_archive_schema.sql",
            "postgres_memory_schema.sql",
            "postgres_skill_schema.sql",
        )
    )
    assert unified == slices
