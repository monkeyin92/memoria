"""Message, daily summary, profile, and delivery-event persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any


class MessageIdempotencyConflictError(ValueError):
    pass


class RecordStoreMixin:
    """Messages, daily summaries, profiles, and media delivery events."""

    if TYPE_CHECKING:
        from contextlib import AbstractContextManager

        def _connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

        @staticmethod
        def _ensure_profile(
            connection: sqlite3.Connection, user_id: str, now: str
        ) -> None: ...

    def add_message(
        self,
        *,
        user_id: str,
        client_message_id: str,
        request_fingerprint: str,
        role: str,
        text: str,
        emotion: str | None,
        local_date: str,
        created_at: str,
    ) -> tuple[dict[str, Any], bool]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, created_at)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO messages (
                        user_id, client_message_id, request_fingerprint,
                        role, text, emotion, local_date, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        client_message_id,
                        request_fingerprint,
                        role,
                        text,
                        emotion,
                        local_date,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                row = connection.execute(
                    """
                    SELECT id, user_id, client_message_id, request_fingerprint,
                           role, text, emotion, local_date, created_at
                    FROM messages WHERE user_id = ? AND client_message_id = ?
                    """,
                    (user_id, client_message_id),
                ).fetchone()
                if row is None:
                    raise
                existing = dict(row)
                if existing.pop("request_fingerprint") != request_fingerprint:
                    raise MessageIdempotencyConflictError(client_message_id) from exc
                return existing, True
            row = connection.execute(
                """
                SELECT id, user_id, client_message_id, role, text, emotion, local_date, created_at
                FROM messages WHERE id = ?
                """,
                (cursor.lastrowid,),
            ).fetchone()
        if row is None:  # pragma: no cover - SQLite guarantees RETURNING row here
            raise RuntimeError("message insert failed")
        return dict(row), False

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

    def get_daily_summary(self, *, user_id: str, summary_date: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT summary_date, content_json, source, message_count, generated_at
                FROM daily_summaries
                WHERE user_id = ? AND summary_date = ?
                """,
                (user_id, summary_date),
            ).fetchone()
            if row is None:
                return None
            item = dict(row)
            item["content"] = json.loads(str(item.pop("content_json")))
            return item

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
                SELECT user_id, display_name, bio, avatar_url, phone_number_masked,
                       companion_id, timezone,
                       auto_summary, voice_reply, gentle_reminders, reject_non_owner_voice,
                       subject_category, birth_year_band, age_evidence_status, subject_revision,
                       created_at, updated_at
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:  # pragma: no cover - ensured in the same transaction
            raise RuntimeError("profile initialization failed")
        return dict(row)

    def get_subject_profile(self, *, user_id: str) -> dict[str, Any] | None:
        """Read the fail-closed capability root without creating a profile."""

        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, display_name, companion_id, subject_category,
                       birth_year_band, age_evidence_status, subject_revision
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        return dict(row) if row is not None else None

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
                SELECT display_name, bio, avatar_url, companion_id, timezone,
                       auto_summary, voice_reply, gentle_reminders, reject_non_owner_voice
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
                SET display_name = ?, bio = ?, avatar_url = ?, companion_id = ?, timezone = ?,
                    auto_summary = ?, voice_reply = ?, gentle_reminders = ?,
                    reject_non_owner_voice = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    merged["display_name"],
                    merged["bio"],
                    merged["avatar_url"],
                    merged["companion_id"],
                    merged["timezone"],
                    int(bool(merged["auto_summary"])),
                    int(bool(merged["voice_reply"])),
                    int(bool(merged["gentle_reminders"])),
                    int(bool(merged["reject_non_owner_voice"])),
                    now,
                    user_id,
                ),
            )
            row = connection.execute(
                """
                SELECT user_id, display_name, bio, avatar_url, phone_number_masked,
                       companion_id, timezone,
                       auto_summary, voice_reply, gentle_reminders, reject_non_owner_voice,
                       subject_category, birth_year_band, age_evidence_status, subject_revision,
                       created_at, updated_at
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:  # pragma: no cover
            raise RuntimeError("profile update failed")
        return dict(row)

    def record_media_reply_delivery_event(
        self,
        *,
        event_id: str,
        schema_version: str,
        delivery_id: str,
        session_id: str,
        session_epoch: int,
        turn_id: int,
        generation_id: int,
        tool_epoch: int,
        event_type: str,
        terminal_event: str | None,
        terminal_reason: str | None,
        first_frame_sent: bool,
        provider_completed: bool,
        actual_heard: bool,
        playback_ended: bool,
        reason: str | None,
        occurred_at: str,
        received_at: str,
        retention_days: int = 7,
    ) -> bool:
        """Insert one idempotent media delivery observation outside archive history."""

        if retention_days <= 0:
            raise ValueError("media reply delivery retention must be positive")
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT event_id FROM media_reply_delivery_events "
                "WHERE delivery_id = ? AND event_type = ?",
                (delivery_id, event_type),
            ).fetchone()
            if existing is not None:
                if str(existing["event_id"]) != event_id:
                    raise ValueError("media reply delivery event conflicts with existing event")
                return False
            connection.execute(
                """
                INSERT INTO media_reply_delivery_events (
                    event_id, schema_version, delivery_id, session_id,
                    session_epoch, turn_id, generation_id, tool_epoch,
                    event_type, terminal_event, terminal_reason,
                    first_frame_sent, provider_completed, actual_heard,
                    playback_ended, reason, occurred_at, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    schema_version,
                    delivery_id,
                    session_id,
                    session_epoch,
                    turn_id,
                    generation_id,
                    tool_epoch,
                    event_type,
                    terminal_event,
                    terminal_reason,
                    int(first_frame_sent),
                    int(provider_completed),
                    int(actual_heard),
                    int(playback_ended),
                    reason,
                    occurred_at,
                    received_at,
                ),
            )
            cutoff = (
                datetime.now(UTC) - timedelta(days=retention_days)
            ).isoformat().replace("+00:00", "Z")
            stale = connection.execute(
                "SELECT event_id FROM media_reply_delivery_events "
                "WHERE received_at < ? ORDER BY received_at LIMIT 256",
                (cutoff,),
            ).fetchall()
            if stale:
                connection.executemany(
                    "DELETE FROM media_reply_delivery_events WHERE event_id = ?",
                    [(str(row["event_id"]),) for row in stale],
                )
        return True

    def media_reply_delivery_events(
        self,
        *,
        delivery_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Read only the exact fence-bound delivery projection."""

        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, schema_version, delivery_id, session_id,
                       session_epoch, turn_id, generation_id, tool_epoch,
                       event_type, terminal_event, terminal_reason,
                       first_frame_sent, provider_completed, actual_heard,
                       playback_ended, reason, occurred_at, received_at
                FROM media_reply_delivery_events
                WHERE delivery_id = ?
                ORDER BY occurred_at, event_id
                """,
                (delivery_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)
