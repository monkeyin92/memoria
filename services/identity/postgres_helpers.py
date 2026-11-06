"""Small value helpers shared by the identity PostgreSQL store and its mixins."""

from __future__ import annotations

from datetime import UTC, datetime


def _timestamp(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)
