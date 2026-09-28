"""Account, auth, device and voice-session store for the control API.

``MemoryStore`` composes one mixin per persistence domain. The same SQL runs
on SQLite (a file path; development and tests) or PostgreSQL (a DSN;
production) through ``backend.PostgresConnection``. The SQLite schema and its
migrations live in ``schema.py``; the PostgreSQL schema is
``postgres_schema.sql``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any

from psycopg_pool import ConnectionPool

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
from services.control_api.app.database.backend import DbConnection, PostgresConnection
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

    def __init__(
        self,
        path: str = "",
        *,
        dsn: str = "",
        initialize_schema: bool = True,
    ) -> None:
        self.dsn = dsn.strip()
        if not self.dsn and not path.strip():
            raise ValueError("MEMORIA_DB_PATH or a PostgreSQL DSN is required")
        self.path = Path(path or ".").expanduser().resolve()
        self.initialize_schema = initialize_schema
        self._pool: ConnectionPool[Any] | None = None
        self._initialized = False
        self._initialize_lock = threading.Lock()

    @property
    def is_postgres(self) -> bool:
        return bool(self.dsn)

    def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()

    def connection(self) -> AbstractContextManager[DbConnection]:
        """One transaction; committed on success, rolled back on error."""

        return self._connection()

    @contextmanager
    def _connection(self) -> Iterator[DbConnection]:
        self.initialize()
        if self.is_postgres:
            if self._pool is None:
                raise RuntimeError("PostgreSQL control store is not initialized")
            # The outer transaction spans the whole method; each write in it is
            # a savepoint (see PostgresConnection), never its own commit.
            with self._pool.connection() as raw, raw.transaction():
                yield PostgresConnection(raw)
            return
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
    def _ensure_profile(connection: DbConnection, user_id: str, now: str) -> None:
        connection.execute(
            """
            INSERT INTO profiles (user_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT DO NOTHING
            """,
            (user_id, now, now),
        )
