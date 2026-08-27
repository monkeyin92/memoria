"""Voice session, readiness, and SDP-exchange persistence."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any


class VoiceSessionStoreMixin:
    """Voice sessions, readiness markers, and Omni SDP reservations."""

    if TYPE_CHECKING:
        from contextlib import AbstractContextManager

        def _connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

        @staticmethod
        def _ensure_profile(
            connection: sqlite3.Connection, user_id: str, now: str
        ) -> None: ...

        @staticmethod
        def _user_id_hash(user_id: str) -> str: ...

    def add_voice_session(
        self,
        *,
        session_id: str,
        user_id: str,
        resource_owner_account_id: str | None = None,
        room_name: str,
        voice_backend: str,
        created_at: str,
        interaction_mode: str,
        mode_policy_version: str,
        session_focus: str = "chat",
        digital_self_version_id: str | None,
        digital_self_manifest_sha256: str | None = None,
        preview_grant_id: str | None = None,
        self_preview_perspective: str | None = None,
        relationship_profile_id: str | None = None,
        relationship_profile_version: int | None = None,
        legacy_grant_id: str | None = None,
        legacy_actor_role: str | None = None,
        legacy_grantee_account_id: str | None = None,
        legacy_shell_id: str | None = None,
        legacy_grant_snapshot_sha256: str | None = None,
        legacy_scope_sha256: str | None = None,
        legacy_voice_allowed: bool | None = None,
        legacy_expires_at: str | None = None,
        companion_style_id: str | None = None,
        companion_style_version: str | None = None,
        voice_profile_id: str | None = None,
        voice_profile_version: int | None = None,
        voice_provider: str | None = None,
        voice_model: str | None = None,
        voice_resource_id: str | None = None,
        voice_provider_expires_at: str | None = None,
        voice_speaker_sha256: str | None = None,
        fallback_voice_profile_id: str | None = None,
        fallback_voice_provider: str | None = None,
        fallback_voice_model: str | None = None,
        fallback_voice_resource_id: str | None = None,
        learning_task_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, created_at)
            connection.execute(
                """
                INSERT INTO voice_sessions (
                    session_id, user_id, resource_owner_account_id,
                    room_name, voice_backend, interaction_mode, session_focus,
                    mode_policy_version, digital_self_version_id,
                    digital_self_manifest_sha256, preview_grant_id,
                    self_preview_perspective, relationship_profile_id,
                    relationship_profile_version, legacy_grant_id,
                    legacy_actor_role, legacy_grantee_account_id, legacy_shell_id,
                    legacy_grant_snapshot_sha256, legacy_scope_sha256,
                    legacy_voice_allowed, legacy_expires_at,
                    companion_style_id, companion_style_version,
                    voice_profile_id, voice_profile_version, voice_provider,
                    voice_model, voice_resource_id, voice_provider_expires_at,
                    voice_speaker_sha256, fallback_voice_profile_id,
                    fallback_voice_provider, fallback_voice_model,
                    fallback_voice_resource_id,
                    learning_task_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    user_id,
                    resource_owner_account_id or user_id,
                    room_name,
                    voice_backend,
                    interaction_mode,
                    session_focus,
                    mode_policy_version,
                    digital_self_version_id,
                    digital_self_manifest_sha256,
                    preview_grant_id,
                    self_preview_perspective,
                    relationship_profile_id,
                    relationship_profile_version,
                    legacy_grant_id,
                    legacy_actor_role,
                    legacy_grantee_account_id,
                    legacy_shell_id,
                    legacy_grant_snapshot_sha256,
                    legacy_scope_sha256,
                    None if legacy_voice_allowed is None else int(legacy_voice_allowed),
                    legacy_expires_at,
                    companion_style_id,
                    companion_style_version,
                    voice_profile_id,
                    voice_profile_version,
                    voice_provider,
                    voice_model,
                    voice_resource_id,
                    voice_provider_expires_at,
                    voice_speaker_sha256,
                    fallback_voice_profile_id,
                    fallback_voice_provider,
                    fallback_voice_model,
                    fallback_voice_resource_id,
                    learning_task_id,
                    created_at,
                ),
            )
            row = connection.execute(
                """
                SELECT session_id, user_id, resource_owner_account_id,
                       room_name, voice_backend, interaction_mode, session_focus,
                       mode_policy_version, digital_self_version_id,
                       digital_self_manifest_sha256, preview_grant_id,
                       self_preview_perspective, relationship_profile_id,
                       relationship_profile_version, legacy_grant_id,
                       legacy_actor_role, legacy_grantee_account_id, legacy_shell_id,
                       legacy_grant_snapshot_sha256, legacy_scope_sha256,
                       legacy_voice_allowed, legacy_expires_at,
                       companion_style_id, companion_style_version,
                       voice_profile_id, voice_profile_version, voice_provider,
                       voice_model, voice_resource_id, voice_provider_expires_at,
                       voice_speaker_sha256, fallback_voice_profile_id,
                       fallback_voice_provider, fallback_voice_model,
                       fallback_voice_resource_id,
                       learning_task_id, created_at
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
                SELECT session_id, user_id, resource_owner_account_id,
                       room_name, voice_backend, interaction_mode, session_focus,
                       mode_policy_version, digital_self_version_id,
                       digital_self_manifest_sha256, preview_grant_id,
                       self_preview_perspective, relationship_profile_id,
                       relationship_profile_version, legacy_grant_id,
                       legacy_actor_role, legacy_grantee_account_id, legacy_shell_id,
                       legacy_grant_snapshot_sha256, legacy_scope_sha256,
                       legacy_voice_allowed, legacy_expires_at,
                       companion_style_id, companion_style_version,
                       voice_profile_id, voice_profile_version, voice_provider,
                       voice_model, voice_resource_id, voice_provider_expires_at,
                       voice_speaker_sha256, fallback_voice_profile_id,
                       fallback_voice_provider, fallback_voice_model,
                       fallback_voice_resource_id,
                       learning_task_id, created_at
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
                SELECT session_id, user_id, resource_owner_account_id,
                       room_name, voice_backend, interaction_mode, session_focus,
                       mode_policy_version, digital_self_version_id,
                       digital_self_manifest_sha256, preview_grant_id,
                       self_preview_perspective, relationship_profile_id,
                       relationship_profile_version, legacy_grant_id,
                       legacy_actor_role, legacy_grantee_account_id, legacy_shell_id,
                       legacy_grant_snapshot_sha256, legacy_scope_sha256,
                       legacy_voice_allowed, legacy_expires_at,
                       companion_style_id, companion_style_version,
                       voice_profile_id, voice_profile_version, voice_provider,
                       voice_model, voice_resource_id, voice_provider_expires_at,
                       voice_speaker_sha256, fallback_voice_profile_id,
                       fallback_voice_provider, fallback_voice_model,
                       fallback_voice_resource_id,
                       learning_task_id, created_at
                FROM voice_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def delete_voice_session(self, *, session_id: str) -> bool:
        """Delete one aborted cross-store projection without touching its account peers."""

        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM voice_sessions WHERE session_id = ?",
                (session_id,),
            )
            return cursor.rowcount == 1

    def list_voice_sessions(self, *, user_id: str) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT session_id, user_id, resource_owner_account_id,
                       room_name, voice_backend, interaction_mode, session_focus,
                       mode_policy_version, digital_self_version_id,
                       digital_self_manifest_sha256, preview_grant_id,
                       self_preview_perspective, relationship_profile_id,
                       relationship_profile_version, legacy_grant_id,
                       legacy_actor_role, legacy_grantee_account_id, legacy_shell_id,
                       legacy_grant_snapshot_sha256, legacy_scope_sha256,
                       legacy_voice_allowed, legacy_expires_at,
                       companion_style_id, companion_style_version,
                       voice_profile_id, voice_profile_version, voice_provider,
                       voice_model, voice_resource_id, voice_provider_expires_at,
                       voice_speaker_sha256, fallback_voice_profile_id,
                       fallback_voice_provider, fallback_voice_model,
                       fallback_voice_resource_id,
                       learning_task_id, created_at
                FROM voice_sessions
                WHERE user_id = ? OR resource_owner_account_id = ?
                   OR legacy_grantee_account_id = ?
                ORDER BY created_at, session_id
                """,
                (user_id, user_id, user_id),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def mark_voice_sessions_deleting(self, *, user_id: str, deleted_at: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO voice_session_tombstones (
                    session_id, user_id_hash, deleted_at
                )
                SELECT session_id, ?, ? FROM voice_sessions
                WHERE user_id = ? OR resource_owner_account_id = ?
                   OR legacy_grantee_account_id = ?
                """,
                (
                    self._user_id_hash(user_id),
                    deleted_at,
                    user_id,
                    user_id,
                    user_id,
                ),
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
            connection.execute(
                """
                DELETE FROM media_reply_delivery_events
                WHERE session_id IN (
                    SELECT session_id FROM voice_sessions
                    WHERE user_id = ? OR resource_owner_account_id = ?
                       OR legacy_grantee_account_id = ?
                )
                """,
                (user_id, user_id, user_id),
            )
            cursor = connection.execute(
                """
                DELETE FROM voice_sessions
                WHERE user_id = ? OR resource_owner_account_id = ?
                   OR legacy_grantee_account_id = ?
                """,
                (user_id, user_id, user_id),
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
