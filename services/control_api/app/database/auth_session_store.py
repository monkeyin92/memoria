"""Authentication session creation, rotation, and revocation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

AUTH_REFRESH_REPLAY_GRACE_S = 5
AUTH_REFRESH_CONCURRENT_RETRY_AFTER_S = 1


class AuthSessionRotationStatus(StrEnum):
    ROTATED = "rotated"
    CONCURRENT_RETRY = "concurrent_retry"
    REPLAY_REVOKED = "replay_revoked"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class AuthSessionRotationResult:
    status: AuthSessionRotationStatus
    session: dict[str, Any] | None = None


class AuthSessionStoreMixin:
    """Auth session lifecycle bound to account identity."""

    if TYPE_CHECKING:
        from contextlib import AbstractContextManager

        def _connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

        @staticmethod
        def _user_id_hash(user_id: str) -> str: ...

        @staticmethod
        def _ensure_profile(
            connection: sqlite3.Connection, user_id: str, now: str
        ) -> None: ...

    def create_auth_session(
        self,
        *,
        session_id: str,
        user_id: str,
        refresh_hash: str,
        expires_at: str,
        now: str,
        replace_existing_sessions: bool = False,
    ) -> bool:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_profile(connection, user_id, now)
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    session_id, user_id, refresh_hash, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, refresh_hash, expires_at, now, now),
            )
            connection.execute(
                """
                INSERT INTO auth_session_refresh_tokens (refresh_hash, session_id, created_at)
                VALUES (?, ?, ?)
                """,
                (refresh_hash, session_id, now),
            )
            if replace_existing_sessions:
                connection.execute(
                    """
                    UPDATE auth_sessions
                    SET revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                    WHERE user_id = ? AND session_id <> ?
                    """,
                    (now, now, user_id, session_id),
                )
        return True

    def create_or_recover_legacy_upgrade_session(
        self,
        *,
        legacy_token_hash: str,
        session_id: str,
        user_id: str,
        refresh_hash: str,
        expires_at: str,
        recovery_expires_at: str,
        now: str,
    ) -> dict[str, Any] | None:
        """Atomically create one migration session or recover it for a brief retry."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT u.user_id, u.session_id, u.recovery_expires_at,
                       s.expires_at, s.revoked_at
                FROM legacy_auth_upgrades AS u
                JOIN auth_sessions AS s ON s.session_id = u.session_id
                WHERE u.legacy_token_hash = ?
                """,
                (legacy_token_hash,),
            ).fetchone()
            if existing is not None:
                recovery_expires = datetime.fromisoformat(
                    str(existing["recovery_expires_at"]).replace("Z", "+00:00")
                )
                requested_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
                session_expires = datetime.fromisoformat(
                    str(existing["expires_at"]).replace("Z", "+00:00")
                )
                if (
                    recovery_expires > requested_at
                    and session_expires > requested_at
                    and existing["revoked_at"] is None
                    and str(existing["user_id"]) == user_id
                ):
                    return {"user_id": user_id, "session_id": str(existing["session_id"])}
                connection.execute(
                    "DELETE FROM legacy_auth_upgrades WHERE legacy_token_hash = ?",
                    (legacy_token_hash,),
                )
                return None
            if (
                connection.execute(
                    "SELECT 1 FROM auth_sessions WHERE user_id = ? LIMIT 1", (user_id,)
                ).fetchone()
                is not None
            ):
                return None
            self._ensure_profile(connection, user_id, now)
            connection.execute(
                """
                INSERT INTO auth_sessions (
                    session_id, user_id, refresh_hash, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, user_id, refresh_hash, expires_at, now, now),
            )
            connection.execute(
                """
                INSERT INTO auth_session_refresh_tokens (refresh_hash, session_id, created_at)
                VALUES (?, ?, ?)
                """,
                (refresh_hash, session_id, now),
            )
            connection.execute(
                """
                INSERT INTO legacy_auth_upgrades (
                    legacy_token_hash, user_id, session_id, recovery_expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (legacy_token_hash, user_id, session_id, recovery_expires_at, now),
            )
        return {"user_id": user_id, "session_id": session_id}

    def auth_session_active(self, *, session_id: str, user_id: str, now: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM auth_sessions
                WHERE session_id = ? AND user_id = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (session_id, user_id, now),
            ).fetchone()
        return row is not None

    def rotate_auth_session(
        self,
        *,
        refresh_hash: str,
        next_refresh_hash: str,
        now: str,
    ) -> AuthSessionRotationResult:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT s.session_id, s.user_id, s.expires_at, s.revoked_at, t.consumed_at
                FROM auth_session_refresh_tokens AS t
                JOIN auth_sessions AS s ON s.session_id = t.session_id
                WHERE t.refresh_hash = ?
                """,
                (refresh_hash,),
            ).fetchone()
            if row is None:
                return AuthSessionRotationResult(AuthSessionRotationStatus.INVALID)
            if row["consumed_at"] is not None:
                try:
                    consumed_at = datetime.fromisoformat(
                        str(row["consumed_at"]).replace("Z", "+00:00")
                    )
                    requested_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
                    age_s = (requested_at - consumed_at).total_seconds()
                except (TypeError, ValueError):
                    age_s = AUTH_REFRESH_REPLAY_GRACE_S
                if 0 <= age_s < AUTH_REFRESH_REPLAY_GRACE_S:
                    return AuthSessionRotationResult(
                        AuthSessionRotationStatus.CONCURRENT_RETRY,
                    )
                connection.execute(
                    "UPDATE auth_sessions SET revoked_at = ?, updated_at = ? WHERE session_id = ?",
                    (now, now, row["session_id"]),
                )
                return AuthSessionRotationResult(AuthSessionRotationStatus.REPLAY_REVOKED)
            if row["revoked_at"] is not None or str(row["expires_at"]) <= now:
                return AuthSessionRotationResult(AuthSessionRotationStatus.INVALID)
            connection.execute(
                "UPDATE auth_session_refresh_tokens SET consumed_at = ? WHERE refresh_hash = ?",
                (now, refresh_hash),
            )
            connection.execute(
                """
                INSERT INTO auth_session_refresh_tokens (refresh_hash, session_id, created_at)
                VALUES (?, ?, ?)
                """,
                (next_refresh_hash, row["session_id"], now),
            )
            connection.execute(
                """
                UPDATE auth_sessions SET refresh_hash = ?, updated_at = ? WHERE session_id = ?
                """,
                (next_refresh_hash, now, row["session_id"]),
            )
        return AuthSessionRotationResult(
            AuthSessionRotationStatus.ROTATED,
            dict(row),
        )

    def revoke_auth_session(self, *, session_id: str, user_id: str, now: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (now, now, session_id, user_id),
            )
        return cursor.rowcount == 1

    def revoke_all_auth_sessions(self, *, user_id: str, now: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                WHERE user_id = ?
                """,
                (now, now, user_id),
            )
        return max(0, cursor.rowcount)
