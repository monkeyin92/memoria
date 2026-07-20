"""Small SQLite store for the H5 conversation history."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '朋友',
    bio TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    auto_summary INTEGER NOT NULL DEFAULT 1 CHECK (auto_summary IN (0, 1)),
    voice_reply INTEGER NOT NULL DEFAULT 1 CHECK (voice_reply IN (0, 1)),
    gentle_reminders INTEGER NOT NULL DEFAULT 0 CHECK (gentle_reminders IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    username_normalized TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    emotion TEXT,
    local_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_user_date
ON messages(user_id, local_date, id);

CREATE TABLE IF NOT EXISTS daily_summaries (
    user_id TEXT NOT NULL,
    summary_date TEXT NOT NULL,
    content_json TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('qwen', 'deepseek', 'fallback')),
    message_count INTEGER NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, summary_date),
    FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    room_name TEXT NOT NULL UNIQUE,
    voice_backend TEXT NOT NULL DEFAULT 'cascade'
        CHECK (voice_backend IN ('cascade', 'qwen_omni')),
    omni_sdp_exchanges INTEGER NOT NULL DEFAULT 0
        CHECK (omni_sdp_exchanges >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_voice_sessions_user
ON voice_sessions(user_id, created_at);

CREATE TABLE IF NOT EXISTS voice_session_tombstones (
    session_id TEXT PRIMARY KEY,
    user_id_hash TEXT NOT NULL,
    deleted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voice_session_tombstones_user
ON voice_session_tombstones(user_id_hash, deleted_at);

CREATE TABLE IF NOT EXISTS readiness_evidence (
    release_tag TEXT NOT NULL,
    llm_provider TEXT NOT NULL CHECK (llm_provider IN ('qwen', 'deepseek')),
    marked_at TEXT NOT NULL,
    PRIMARY KEY (release_tag, llm_provider)
);

CREATE TABLE IF NOT EXISTS account_deletions (
    user_id_hash TEXT PRIMARY KEY,
    user_id TEXT UNIQUE,
    request_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('deleting', 'completed')),
    step TEXT NOT NULL,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    progress_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    deleted_counts_json TEXT NOT NULL DEFAULT '{}'
);
"""

_PROFILE_BOOLEAN_COLUMNS = {
    "auto_summary": "INTEGER NOT NULL DEFAULT 1 CHECK (auto_summary IN (0, 1))",
    "voice_reply": "INTEGER NOT NULL DEFAULT 1 CHECK (voice_reply IN (0, 1))",
    "gentle_reminders": "INTEGER NOT NULL DEFAULT 0 CHECK (gentle_reminders IN (0, 1))",
}


class MemoryStore:
    """Open short-lived connections so FastAPI worker threads can share one store."""

    def __init__(self, path: str) -> None:
        if not path.strip():
            raise ValueError("MEMORIA_DB_PATH must not be empty")
        self.path = Path(path).expanduser().resolve()
        self._initialized = False
        self._initialize_lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and not self.path.is_file():
                raise ValueError("MEMORIA_DB_PATH must point to a file")
            with sqlite3.connect(self.path, timeout=5) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")
                connection.executescript(_SCHEMA)
                summary_table = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'daily_summaries'"
                ).fetchone()
                if summary_table and "'qwen'" not in str(summary_table[0]):
                    connection.executescript(
                        """
                        ALTER TABLE daily_summaries RENAME TO daily_summaries_legacy;
                        CREATE TABLE daily_summaries (
                            user_id TEXT NOT NULL,
                            summary_date TEXT NOT NULL,
                            content_json TEXT NOT NULL,
                            source TEXT NOT NULL
                                CHECK (source IN ('qwen', 'deepseek', 'fallback')),
                            message_count INTEGER NOT NULL,
                            generated_at TEXT NOT NULL,
                            PRIMARY KEY (user_id, summary_date),
                            FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
                        );
                        INSERT INTO daily_summaries (
                            user_id, summary_date, content_json, source,
                            message_count, generated_at
                        )
                        SELECT user_id, summary_date, content_json,
                               CASE WHEN source = 'dashscope' THEN 'qwen' ELSE source END,
                               message_count, generated_at
                        FROM daily_summaries_legacy;
                        DROP TABLE daily_summaries_legacy;
                        """
                    )
                existing_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(profiles)")
                }
                for name, definition in _PROFILE_BOOLEAN_COLUMNS.items():
                    if name not in existing_columns:
                        connection.execute(f"ALTER TABLE profiles ADD COLUMN {name} {definition}")
                voice_session_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(voice_sessions)")
                }
                if "voice_backend" not in voice_session_columns:
                    connection.execute(
                        "ALTER TABLE voice_sessions ADD COLUMN "
                        "voice_backend TEXT NOT NULL DEFAULT 'cascade'"
                    )
                if "omni_sdp_exchanges" not in voice_session_columns:
                    connection.execute(
                        "ALTER TABLE voice_sessions ADD COLUMN "
                        "omni_sdp_exchanges INTEGER NOT NULL DEFAULT 0 "
                        "CHECK (omni_sdp_exchanges >= 0)"
                    )
                voice_session_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'voice_sessions'"
                ).fetchone()
                voice_session_definition = str(voice_session_sql[0]) if voice_session_sql else ""
                if "qwen_audio" in voice_session_definition or "qwen_omni_plus" in voice_session_definition:
                    connection.executescript(
                        """
                        ALTER TABLE voice_sessions RENAME TO voice_sessions_legacy;
                        CREATE TABLE voice_sessions (
                            session_id TEXT PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            room_name TEXT NOT NULL UNIQUE,
                            voice_backend TEXT NOT NULL DEFAULT 'cascade'
                                CHECK (voice_backend IN ('cascade', 'qwen_omni')),
                            omni_sdp_exchanges INTEGER NOT NULL DEFAULT 0
                                CHECK (omni_sdp_exchanges >= 0),
                            created_at TEXT NOT NULL,
                            FOREIGN KEY (user_id) REFERENCES profiles(user_id)
                                ON DELETE CASCADE
                        );
                        INSERT INTO voice_sessions (
                            session_id, user_id, room_name, voice_backend,
                            omni_sdp_exchanges, created_at
                        )
                        SELECT
                            session_id,
                            user_id,
                            room_name,
                            CASE
                                WHEN voice_backend = 'qwen_omni_plus' THEN 'qwen_omni'
                                WHEN voice_backend = 'qwen_audio' THEN 'cascade'
                                ELSE voice_backend
                            END,
                            COALESCE(omni_sdp_exchanges, 0),
                            created_at
                        FROM voice_sessions_legacy;
                        DROP TABLE voice_sessions_legacy;
                        CREATE INDEX IF NOT EXISTS idx_voice_sessions_user
                        ON voice_sessions(user_id, created_at);
                        """
                    )
                deletion_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(account_deletions)")
                }
                if "status" not in deletion_columns:
                    connection.executescript(
                        """
                        ALTER TABLE account_deletions RENAME TO account_deletions_legacy;
                        CREATE TABLE account_deletions (
                            user_id_hash TEXT PRIMARY KEY,
                            user_id TEXT UNIQUE,
                            request_id TEXT NOT NULL UNIQUE,
                            status TEXT NOT NULL
                                CHECK (status IN ('deleting', 'completed')),
                            step TEXT NOT NULL,
                            started_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            completed_at TEXT,
                            progress_json TEXT NOT NULL DEFAULT '{}',
                            last_error TEXT,
                            deleted_counts_json TEXT NOT NULL DEFAULT '{}'
                        );
                        INSERT INTO account_deletions (
                            user_id_hash, request_id, status, step, started_at,
                            updated_at, completed_at, deleted_counts_json
                        )
                        SELECT
                            user_id_hash, request_id, 'completed', 'completed',
                            completed_at, completed_at, completed_at,
                            deleted_counts_json
                        FROM account_deletions_legacy;
                        DROP TABLE account_deletions_legacy;
                        """
                    )
            self._initialized = True

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

    def add_message(
        self,
        *,
        user_id: str,
        role: str,
        text: str,
        emotion: str | None,
        local_date: str,
        created_at: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, created_at)
            cursor = connection.execute(
                """
                INSERT INTO messages (user_id, role, text, emotion, local_date, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, role, text, emotion, local_date, created_at),
            )
            row = connection.execute(
                """
                SELECT id, user_id, role, text, emotion, local_date, created_at
                FROM messages WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite guarantees RETURNING row here
            raise RuntimeError("message insert failed")
        return dict(row)

    def register_account(
        self,
        *,
        user_id: str,
        username: str,
        username_normalized: str,
        password_hash: str,
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, now)
            connection.execute(
                """
                INSERT INTO accounts (
                    user_id, username, username_normalized, password_hash,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, username_normalized, password_hash, now, now),
            )
            row = connection.execute(
                """
                SELECT user_id, username, username_normalized, password_hash,
                       created_at, updated_at
                FROM accounts WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - inserted in the same transaction
            raise RuntimeError("account insert failed")
        return dict(row)

    def get_account_by_username(self, *, username_normalized: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, username, username_normalized, password_hash,
                       created_at, updated_at
                FROM accounts WHERE username_normalized = ?
                """,
                (username_normalized,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_account(self, *, user_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, username, username_normalized, password_hash,
                       created_at, updated_at
                FROM accounts WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _user_id_hash(user_id: str) -> str:
        return hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def is_account_deleted(self, *, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM account_deletions
                WHERE user_id_hash = ? AND status = 'completed'
                """,
                (self._user_id_hash(user_id),),
            ).fetchone()
        return row is not None

    def is_account_unavailable(self, *, user_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM account_deletions WHERE user_id_hash = ?",
                (self._user_id_hash(user_id),),
            ).fetchone()
        return row is not None

    def get_account_deletion(self, *, user_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT request_id, status, step, started_at, updated_at,
                       completed_at, progress_json, last_error,
                       deleted_counts_json
                FROM account_deletions WHERE user_id_hash = ?
                """,
                (self._user_id_hash(user_id),),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["progress"] = json.loads(str(result.pop("progress_json")))
        result["deleted_counts"] = json.loads(str(result.pop("deleted_counts_json")))
        return result

    def begin_account_deletion(self, *, user_id: str, started_at: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_id FROM account_deletions WHERE user_id_hash = ?",
                (self._user_id_hash(user_id),),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO account_deletions (
                        user_id_hash, user_id, request_id, status, step,
                        started_at, updated_at
                    ) VALUES (?, ?, ?, 'deleting', 'started', ?, ?)
                    """,
                    (
                        self._user_id_hash(user_id),
                        user_id,
                        str(uuid.uuid4()),
                        started_at,
                        started_at,
                    ),
                )
        deletion = self.get_account_deletion(user_id=user_id)
        if deletion is None:  # pragma: no cover - inserted/read in one local store
            raise RuntimeError("account deletion initialization failed")
        return deletion

    def update_account_deletion(
        self,
        *,
        user_id: str,
        request_id: str,
        step: str,
        updated_at: str,
        progress: Mapping[str, int] | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE account_deletions
                SET step = ?, updated_at = ?, progress_json = ?, last_error = ?
                WHERE user_id_hash = ? AND request_id = ? AND status = 'deleting'
                """,
                (
                    step,
                    updated_at,
                    json.dumps(progress or {}, sort_keys=True, separators=(",", ":")),
                    last_error,
                    self._user_id_hash(user_id),
                    request_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("account deletion request is not active")
        deletion = self.get_account_deletion(user_id=user_id)
        if deletion is None:  # pragma: no cover - updated/read in one local store
            raise RuntimeError("account deletion update failed")
        return deletion

    def pending_account_deletions(self, *, limit: int = 100) -> tuple[str, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("pending deletion limit must be between 1 and 1000")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM account_deletions
                WHERE status = 'deleting' AND user_id IS NOT NULL
                ORDER BY started_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(str(row["user_id"]) for row in rows)

    def export_account_data(self, *, user_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            profile = connection.execute(
                """
                SELECT user_id, display_name, bio, avatar_url, timezone,
                       auto_summary, voice_reply, gentle_reminders,
                       created_at, updated_at
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            account = connection.execute(
                """
                SELECT user_id, username, created_at, updated_at
                FROM accounts WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            messages = connection.execute(
                """
                SELECT id, role, text, emotion, local_date, created_at
                FROM messages WHERE user_id = ? ORDER BY id
                """,
                (user_id,),
            ).fetchall()
            summaries = connection.execute(
                """
                SELECT summary_date, content_json, source, message_count, generated_at
                FROM daily_summaries WHERE user_id = ? ORDER BY summary_date
                """,
                (user_id,),
            ).fetchall()
            sessions = connection.execute(
                """
                SELECT session_id, voice_backend, created_at
                FROM voice_sessions WHERE user_id = ? ORDER BY created_at, session_id
                """,
                (user_id,),
            ).fetchall()
        profile_data = dict(profile) if profile is not None else None
        if profile_data is not None:
            for key in ("auto_summary", "voice_reply", "gentle_reminders"):
                profile_data[key] = bool(profile_data[key])
        summary_data = []
        for row in summaries:
            item = dict(row)
            item["content"] = json.loads(str(item.pop("content_json")))
            summary_data.append(item)
        return {
            "profile": profile_data,
            "account": dict(account) if account is not None else None,
            "messages": [dict(row) for row in messages],
            "daily_summaries": summary_data,
            "voice_sessions": [dict(row) for row in sessions],
        }

    def finalize_account_deletion(
        self,
        *,
        user_id: str,
        request_id: str,
        completed_at: str,
        deleted_counts: Mapping[str, int],
    ) -> dict[str, int]:
        with self._connection() as connection:
            deletion = connection.execute(
                """
                SELECT request_id, status, deleted_counts_json
                FROM account_deletions WHERE user_id_hash = ?
                """,
                (self._user_id_hash(user_id),),
            ).fetchone()
            if deletion is None or str(deletion["request_id"]) != request_id:
                raise RuntimeError("account deletion request is not active")
            if str(deletion["status"]) == "completed":
                return {
                    str(key): int(value)
                    for key, value in json.loads(str(deletion["deleted_counts_json"])).items()
                }
            counts = {
                "messages": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
                "daily_summaries": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM daily_summaries WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
                "voice_sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM voice_sessions WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
            }
            connection.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
            combined = {**deleted_counts, **counts}
            connection.execute(
                """
                UPDATE account_deletions
                SET user_id = NULL, status = 'completed', step = 'completed',
                    updated_at = ?, completed_at = ?, progress_json = '{}',
                    last_error = NULL, deleted_counts_json = ?
                WHERE user_id_hash = ? AND request_id = ? AND status = 'deleting'
                """,
                (
                    completed_at,
                    completed_at,
                    json.dumps(combined, sort_keys=True, separators=(",", ":")),
                    self._user_id_hash(user_id),
                    request_id,
                ),
            )
        return combined

    def add_voice_session(
        self,
        *,
        session_id: str,
        user_id: str,
        room_name: str,
        voice_backend: str,
        created_at: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, created_at)
            connection.execute(
                """
                INSERT INTO voice_sessions (
                    session_id, user_id, room_name, voice_backend, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, user_id, room_name, voice_backend, created_at),
            )
            row = connection.execute(
                """
                SELECT session_id, user_id, room_name, voice_backend, created_at
                FROM voice_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - inserted in the same transaction
            raise RuntimeError("voice session insert failed")
        return dict(row)

    def get_voice_session(self, *, session_id: str, user_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, user_id, room_name, voice_backend, created_at
                FROM voice_sessions
                WHERE session_id = ? AND user_id = ?
                """,
                (session_id, user_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_voice_session_by_id(self, *, session_id: str) -> dict[str, Any] | None:
        """Resolve archive ownership server-side; never accept account_id from an Agent."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT session_id, user_id, room_name, voice_backend, created_at
                FROM voice_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_voice_sessions(self, *, user_id: str) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, user_id, room_name, voice_backend, created_at
                FROM voice_sessions WHERE user_id = ? ORDER BY created_at, session_id
                """,
                (user_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def mark_voice_sessions_deleting(self, *, user_id: str, deleted_at: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO voice_session_tombstones (
                    session_id, user_id_hash, deleted_at
                )
                SELECT session_id, ?, ? FROM voice_sessions WHERE user_id = ?
                """,
                (self._user_id_hash(user_id), deleted_at, user_id),
            )
        return max(0, cursor.rowcount)

    def is_voice_session_tombstoned(self, *, session_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM voice_session_tombstones WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row is not None

    def delete_voice_sessions(self, *, user_id: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM voice_sessions WHERE user_id = ?",
                (user_id,),
            )
        return max(0, cursor.rowcount)

    def reserve_omni_sdp_exchange(
        self,
        *,
        session_id: str,
        user_id: str,
        max_exchanges: int,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE voice_sessions
                SET omni_sdp_exchanges = omni_sdp_exchanges + 1
                WHERE session_id = ?
                  AND user_id = ?
                  AND voice_backend IN ('qwen_omni')
                  AND omni_sdp_exchanges < ?
                """,
                (session_id, user_id, max_exchanges),
            )
        return cursor.rowcount == 1

    def mark_readiness(
        self,
        *,
        release_tag: str,
        llm_provider: str,
        marked_at: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO readiness_evidence (release_tag, llm_provider, marked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(release_tag, llm_provider) DO UPDATE SET
                    marked_at = excluded.marked_at
                """,
                (release_tag, llm_provider, marked_at),
            )

    def get_readiness(
        self,
        *,
        release_tag: str,
        llm_provider: str,
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT release_tag, llm_provider, marked_at
                FROM readiness_evidence
                WHERE release_tag = ? AND llm_provider = ?
                """,
                (release_tag, llm_provider),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_messages(self, *, user_id: str, summary_date: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, role, text, emotion, local_date, created_at
                FROM messages
                WHERE user_id = ? AND local_date = ?
                ORDER BY id
                """,
                (user_id, summary_date),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_days(
        self,
        *,
        user_id: str,
        limit: int,
        before: str | None,
    ) -> list[dict[str, Any]]:
        before_clause = "AND day < ?" if before else ""
        parameters: list[Any] = [user_id, user_id]
        if before:
            parameters.append(before)
        parameters.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                WITH days AS (
                    SELECT local_date AS day FROM messages WHERE user_id = ?
                    UNION
                    SELECT summary_date AS day FROM daily_summaries WHERE user_id = ?
                ), counts AS (
                    SELECT local_date AS day, COUNT(*) AS message_count
                    FROM messages WHERE user_id = ? GROUP BY local_date
                )
                SELECT days.day, COALESCE(counts.message_count, 0) AS message_count,
                       daily_summaries.content_json, daily_summaries.source,
                       daily_summaries.generated_at
                FROM days
                LEFT JOIN counts ON counts.day = days.day
                LEFT JOIN daily_summaries
                  ON daily_summaries.user_id = ? AND daily_summaries.summary_date = days.day
                WHERE 1 = 1 {before_clause}
                ORDER BY days.day DESC
                LIMIT ?
                """,
                [user_id, user_id, user_id, user_id, *parameters[2:]],
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_content = item.pop("content_json")
            item["summary"] = json.loads(raw_content) if raw_content else None
            result.append(item)
        return result

    def upsert_summary(
        self,
        *,
        user_id: str,
        summary_date: str,
        content: Mapping[str, Any],
        source: str,
        message_count: int,
        generated_at: str,
    ) -> dict[str, Any]:
        content_json = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, generated_at)
            connection.execute(
                """
                INSERT INTO daily_summaries (
                    user_id, summary_date, content_json, source, message_count, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, summary_date) DO UPDATE SET
                    content_json = excluded.content_json,
                    source = excluded.source,
                    message_count = excluded.message_count,
                    generated_at = excluded.generated_at
                """,
                (
                    user_id,
                    summary_date,
                    content_json,
                    source,
                    message_count,
                    generated_at,
                ),
            )
        return {
            "day": summary_date,
            "message_count": message_count,
            "summary": dict(content),
            "source": source,
            "generated_at": generated_at,
        }

    def get_profile(self, *, user_id: str, now: str) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, now)
            row = connection.execute(
                """
                SELECT user_id, display_name, bio, avatar_url, timezone,
                       auto_summary, voice_reply, gentle_reminders,
                       created_at, updated_at
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - ensured in the same transaction
            raise RuntimeError("profile initialization failed")
        return dict(row)

    def update_profile(
        self,
        *,
        user_id: str,
        values: Mapping[str, Any],
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, now)
            current = connection.execute(
                """
                SELECT display_name, bio, avatar_url, timezone,
                       auto_summary, voice_reply, gentle_reminders
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None:  # pragma: no cover - ensured in the same transaction
                raise RuntimeError("profile initialization failed")
            merged = {**dict(current), **values}
            connection.execute(
                """
                UPDATE profiles
                SET display_name = ?, bio = ?, avatar_url = ?, timezone = ?,
                    auto_summary = ?, voice_reply = ?, gentle_reminders = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    merged["display_name"],
                    merged["bio"],
                    merged["avatar_url"],
                    merged["timezone"],
                    int(bool(merged["auto_summary"])),
                    int(bool(merged["voice_reply"])),
                    int(bool(merged["gentle_reminders"])),
                    now,
                    user_id,
                ),
            )
            row = connection.execute(
                """
                SELECT user_id, display_name, bio, avatar_url, timezone,
                       auto_summary, voice_reply, gentle_reminders,
                       created_at, updated_at
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:  # pragma: no cover
            raise RuntimeError("profile update failed")
        return dict(row)
