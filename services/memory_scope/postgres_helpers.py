"""Shared asyncpg decoding and clock helpers for the memory-scope store modules.

Split out of ``postgres_store.py`` so the store and its record mixin share one
definition of the JSONB decoders and the timezone-aware clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def _jsonb_list(value: object) -> list[Any]:
    """asyncpg may return JSONB as a decoded Python list OR as a str (when
    the connection codec did not decode it); both shapes must decode to a
    real list (fifth review)."""
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list) or not all(
        isinstance(item, str) for item in decoded
    ):
        raise ValueError("expected a JSON array of strings")
    return decoded


def _jsonb_dict(value: object) -> dict[str, object]:
    decoded: object = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise ValueError("expected a JSON object")
    return decoded


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: datetime | None) -> datetime | None:
    return value
