"""SQLite memory store for the control API.

``MemoryStore`` composes one mixin per persistence domain; the schema and
migrations live in ``schema.py`` and the shared connection helpers here.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from services.control_api.app.database.account_store import (
    AccountStoreMixin,
    ExternalIdentityConflictError,
)
from services.control_api.app.database.auth_session_store import (
    AUTH_REFRESH_CONCURRENT_RETRY_AFTER_S,
    AUTH_REFRESH_REPLAY_GRACE_S,
    AuthSessionRotationResult,
    AuthSessionRotationStatus,
    AuthSessionStoreMixin,
)
from services.control_api.app.database.device_store import DeviceStoreMixin
from services.control_api.app.database.record_store import (
    MessageIdempotencyConflictError,
    RecordStoreMixin,
)
from services.control_api.app.database.schema import SchemaMixin
from services.control_api.app.database.session_store import VoiceSessionStoreMixin

__all__ = [
    "AUTH_REFRESH_CONCURRENT_RETRY_AFTER_S",
    "AUTH_REFRESH_REPLAY_GRACE_S",
    "AuthSessionRotationResult",
    "AuthSessionRotationStatus",
    "ExternalIdentityConflictError",
    "MemoryStore",
    "MessageIdempotencyConflictError",
]


class MemoryStore(
    SchemaMixin,
    DeviceStoreMixin,
    AuthSessionStoreMixin,
    AccountStoreMixin,
    VoiceSessionStoreMixin,
    RecordStoreMixin,
):
    """Open short-lived connections so FastAPI worker threads can share one store."""

    def __init__(self, path: str) -> None:
        if not path.strip():
            raise ValueError("MEMORIA_DB_PATH must not be empty")
        self.path = Path(path).expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ensure_profile(connection: sqlite3.Connection, user_id: str, now: str) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO profiles (user_id, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (user_id, now, now),
        )
