"""Production checks for the control store's database URL."""

from __future__ import annotations

from urllib.parse import urlsplit

CONTROL_DATABASE_ROLE = "memoria_control"


def validate_control_database_url(url: str) -> None:
    """Accept no URL (the SQLite file) or PostgreSQL as memoria_control.

    The URL stays optional until the production data is migrated; once set it
    must be PostgreSQL and use the dedicated role, never a shared one.
    """

    value = url.strip()
    if not value:
        return
    if not value.startswith(("postgresql://", "postgres://")):
        raise ValueError("MEMORIA_CONTROL_DATABASE_URL must be a PostgreSQL URL")
    if (urlsplit(value).username or "") != CONTROL_DATABASE_ROLE:
        raise ValueError(
            "MEMORIA_CONTROL_DATABASE_URL must use the independent "
            f"{CONTROL_DATABASE_ROLE} role"
        )


__all__ = ["CONTROL_DATABASE_ROLE", "validate_control_database_url"]
