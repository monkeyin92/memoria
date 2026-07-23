"""Load the idempotent PostgreSQL 17 Legacy migration."""

from pathlib import Path


def read_postgres_schema() -> str:
    return Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
