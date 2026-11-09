"""Shared value decoders for the guardian PostgreSQL store.

The main store and its domain mixins parse the same wire values (UUIDs,
timestamps, hashes, JSON columns). They live here so a mixin never has to
import its composing store back.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any


def _timestamp(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _uuid(value: str, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("binding_code_hash must be a SHA-256 hex digest")
    return normalized


def _json_decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _json_array(value: object, *, field: str) -> list[object]:
    decoded = _json_decoded(value)
    if not isinstance(decoded, list):
        raise RuntimeError(f"{field} must be a JSON array")
    return decoded
