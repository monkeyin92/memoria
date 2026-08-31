"""Device identity, settings, and media-session persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

_DEVICE_STREAM_EPOCH_MAX = (1 << 32) - 1


class DeviceStoreMixin:
    """Device identities, settings, control intents, and media sessions."""

    if TYPE_CHECKING:
        from contextlib import AbstractContextManager

        def _connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

        @staticmethod
        def _ensure_profile(
            connection: sqlite3.Connection, user_id: str, now: str
        ) -> None: ...

    def register_device_identity(
        self,
        *,
        device_id: str,
        account_id: str,
        public_key_b64: str,
        firmware_channel: str,
        now: str,
    ) -> dict[str, Any]:
        """Register one device public key without ever persisting its private key."""

        with self._connection() as connection:
            self._ensure_profile(connection, account_id, now)
            existing = connection.execute(
                "SELECT * FROM device_identities WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["account_id"]) != account_id
                    or str(existing["public_key_b64"]) != public_key_b64
                    or existing["revoked_at"] is not None
                ):
                    raise ValueError("device identity is already registered")
                connection.execute(
                    "UPDATE device_identities SET firmware_channel = ?, updated_at = ? "
                    "WHERE device_id = ?",
                    (firmware_channel, now, device_id),
                )
            else:
                try:
                    connection.execute(
                        """
                        INSERT INTO device_identities (
                            device_id, account_id, public_key_b64, firmware_channel,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (device_id, account_id, public_key_b64, firmware_channel, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError("device public key is already registered") from exc
            row = connection.execute(
                "SELECT * FROM device_identities WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same transaction inserted the row
                raise RuntimeError("device identity was not persisted")
            return dict(row)

    def get_device_identity(self, *, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_identities WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def count_active_device_identities(self, *, account_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM device_identities
                WHERE account_id = ? AND revoked_at IS NULL
                """,
                (account_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def revoke_device_identity(self, *, device_id: str, account_id: str, now: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE device_identities SET revoked_at = ?, updated_at = ? "
                "WHERE device_id = ? AND account_id = ? AND revoked_at IS NULL",
                (now, now, device_id, account_id),
            )
            return cursor.rowcount == 1

    def issue_device_challenge(
        self,
        *,
        nonce_hash: str,
        device_id: str,
        issued_at_ms: int,
        expires_at_ms: int,
    ) -> None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT revoked_at FROM device_identities WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise ValueError("device identity is not active")
            connection.execute(
                "INSERT INTO device_challenges "
                "(nonce_hash, device_id, issued_at_ms, expires_at_ms) VALUES (?, ?, ?, ?)",
                (nonce_hash, device_id, issued_at_ms, expires_at_ms),
            )

    def count_pending_device_challenges(self, *, device_id: str, now_ms: int) -> int:
        """Bound unauthenticated bootstrap state before issuing another nonce."""

        with self._connection() as connection:
            connection.execute(
                "DELETE FROM device_challenges "
                "WHERE device_id = ? AND (used_at_ms IS NOT NULL OR expires_at_ms < ?)",
                (device_id, now_ms),
            )
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM device_challenges "
                "WHERE device_id = ? AND used_at_ms IS NULL AND expires_at_ms >= ?",
                (device_id, now_ms),
            ).fetchone()
            return int(row["count"] if row is not None else 0)

    def consume_device_challenge(
        self,
        *,
        nonce_hash: str,
        device_id: str,
        now_ms: int,
    ) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE device_challenges
                SET used_at_ms = ?
                WHERE nonce_hash = ? AND device_id = ?
                  AND used_at_ms IS NULL AND expires_at_ms >= ?
                """,
                (now_ms, nonce_hash, device_id, now_ms),
            )
            return cursor.rowcount == 1

    def delete_device_identities(self, *, account_id: str) -> int:
        with self._connection() as connection:
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM device_identities WHERE account_id = ?",
                    (account_id,),
                ).fetchone()[0]
            )
            connection.execute("DELETE FROM device_identities WHERE account_id = ?", (account_id,))
            return count

    def get_device_settings(self, *, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_settings WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def update_device_settings(
        self,
        *,
        device_id: str,
        settings: Mapping[str, Any],
        settings_version: int,
        updated_by: str,
        updated_at: str,
        update_reason: str,
    ) -> dict[str, Any]:
        payload = json.dumps(
            dict(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO device_settings (
                    device_id, settings_json, settings_version, updated_by,
                    updated_at, update_reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    settings_json = excluded.settings_json,
                    settings_version = excluded.settings_version,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at,
                    update_reason = excluded.update_reason
                """,
                (
                    device_id,
                    payload,
                    settings_version,
                    updated_by,
                    updated_at,
                    update_reason,
                ),
            )
            row = connection.execute(
                "SELECT * FROM device_settings WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same transaction inserted the row
                raise RuntimeError("device settings were not persisted")
            return dict(row)

    def update_device_settings_and_profile_ledger(
        self,
        *,
        device_id: str,
        settings: Mapping[str, Any],
        settings_version: int,
        expected_current_settings_version: int,
        settings_fingerprint: str,
        updated_by: str,
        updated_at: str,
        update_reason: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically persist device settings and advance their profile version."""

        payload = json.dumps(
            dict(settings), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = connection.execute(
                "SELECT settings_version FROM device_settings WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            current_version = int(current_row[0]) if current_row is not None else 0
            if current_version != expected_current_settings_version:
                raise ValueError("settings_version_conflict")
            connection.execute(
                """
                INSERT INTO device_settings (
                    device_id, settings_json, settings_version, updated_by,
                    updated_at, update_reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    settings_json = excluded.settings_json,
                    settings_version = excluded.settings_version,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at,
                    update_reason = excluded.update_reason
                """,
                (
                    device_id,
                    payload,
                    settings_version,
                    updated_by,
                    updated_at,
                    update_reason,
                ),
            )
            existing = connection.execute(
                "SELECT profile_version, runtime_profile_id, profile_fingerprint, "
                "settings_fingerprint, expires_at FROM device_runtime_profile_ledger "
                "WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            previous_profile = str(existing["profile_fingerprint"]) if existing is not None else ""
            previous_settings = (
                str(existing["settings_fingerprint"]) if existing is not None else ""
            )
            content_fingerprint = (
                hashlib.sha256(
                    f"{previous_profile}\0{settings_fingerprint}".encode("ascii")
                ).hexdigest()
                if previous_profile
                else settings_fingerprint
            )
            changed = existing is None or settings_fingerprint != previous_settings
            profile_version = (
                1 if existing is None else int(existing["profile_version"]) + (1 if changed else 0)
            )
            runtime_profile_id = (
                str(existing["runtime_profile_id"])
                if existing is not None and existing["runtime_profile_id"] is not None
                else None
            )
            expires_at = (
                str(existing["expires_at"])
                if existing is not None and existing["expires_at"] is not None
                else None
            )
            connection.execute(
                """
                INSERT INTO device_runtime_profile_ledger (
                    device_id, profile_version, runtime_profile_id,
                    content_fingerprint, profile_fingerprint,
                    settings_fingerprint, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    profile_version = excluded.profile_version,
                    runtime_profile_id = excluded.runtime_profile_id,
                    content_fingerprint = excluded.content_fingerprint,
                    profile_fingerprint = excluded.profile_fingerprint,
                    settings_fingerprint = excluded.settings_fingerprint,
                    issued_at = excluded.issued_at,
                    expires_at = excluded.expires_at
                """,
                (
                    device_id,
                    profile_version,
                    runtime_profile_id,
                    content_fingerprint,
                    previous_profile,
                    settings_fingerprint,
                    updated_at,
                    expires_at,
                ),
            )
            settings_row = connection.execute(
                "SELECT * FROM device_settings WHERE device_id = ?", (device_id,)
            ).fetchone()
            ledger_row = connection.execute(
                "SELECT * FROM device_runtime_profile_ledger WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if settings_row is None or ledger_row is None:  # pragma: no cover
                raise RuntimeError("device settings transaction did not persist")
            return dict(settings_row), dict(ledger_row)

    def delete_device_settings(self, *, updated_by: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM device_settings WHERE updated_by = ?",
                (updated_by,),
            )
            return cursor.rowcount

    def get_device_runtime_profile_ledger(self, *, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_runtime_profile_ledger WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def upsert_device_runtime_profile_ledger(
        self,
        *,
        device_id: str,
        component: str,
        runtime_profile_id: str | None,
        component_fingerprint: str,
        issued_at: str,
        expires_at: str | None,
    ) -> dict[str, Any]:
        if component not in {"profile", "settings"}:
            raise ValueError("runtime profile ledger component is invalid")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT profile_version, runtime_profile_id, profile_fingerprint, "
                "settings_fingerprint, issued_at, expires_at "
                "FROM device_runtime_profile_ledger WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            previous_profile = str(existing["profile_fingerprint"]) if existing is not None else ""
            previous_settings = (
                str(existing["settings_fingerprint"]) if existing is not None else ""
            )
            profile_fingerprint = (
                component_fingerprint if component == "profile" else previous_profile
            )
            settings_fingerprint = (
                component_fingerprint if component == "settings" else previous_settings
            )
            if profile_fingerprint and settings_fingerprint:
                content_fingerprint = hashlib.sha256(
                    f"{profile_fingerprint}\0{settings_fingerprint}".encode("ascii")
                ).hexdigest()
            else:
                content_fingerprint = profile_fingerprint or settings_fingerprint
            changed = (
                existing is None
                or profile_fingerprint != previous_profile
                or settings_fingerprint != previous_settings
            )
            effective_version = (
                1 if existing is None else int(existing["profile_version"]) + (1 if changed else 0)
            )
            effective_runtime_profile_id = (
                runtime_profile_id
                if component == "profile"
                else (
                    str(existing["runtime_profile_id"])
                    if existing is not None and existing["runtime_profile_id"] is not None
                    else None
                )
            )
            effective_expires_at = (
                expires_at
                if component == "profile"
                else (
                    str(existing["expires_at"])
                    if existing is not None and existing["expires_at"] is not None
                    else None
                )
            )
            connection.execute(
                """
                INSERT INTO device_runtime_profile_ledger (
                    device_id, profile_version, runtime_profile_id,
                    content_fingerprint, profile_fingerprint,
                    settings_fingerprint, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    profile_version = excluded.profile_version,
                    runtime_profile_id = excluded.runtime_profile_id,
                    content_fingerprint = excluded.content_fingerprint,
                    profile_fingerprint = excluded.profile_fingerprint,
                    settings_fingerprint = excluded.settings_fingerprint,
                    issued_at = excluded.issued_at,
                    expires_at = excluded.expires_at
                """,
                (
                    device_id,
                    effective_version,
                    effective_runtime_profile_id,
                    content_fingerprint,
                    profile_fingerprint,
                    settings_fingerprint,
                    issued_at,
                    effective_expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM device_runtime_profile_ledger WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same transaction inserted the row
                raise RuntimeError("runtime profile ledger was not persisted")
            return dict(row)

    def last_device_profile_ack(self, *, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM device_runtime_profile_acks
                WHERE device_id = ?
                ORDER BY profile_version DESC
                LIMIT 1
                """,
                (device_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def record_device_profile_ack(
        self,
        *,
        device_id: str,
        profile_version: int,
        runtime_profile_id: str | None,
        acked_by: str,
        acked_at: str,
        accepted: bool,
    ) -> bool:
        """Return True for a new ack and False for an idempotent replay."""

        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO device_runtime_profile_acks (
                    device_id, profile_version, runtime_profile_id,
                    acked_by, acked_at, accepted
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    profile_version,
                    runtime_profile_id,
                    acked_by,
                    acked_at,
                    1 if accepted else 0,
                ),
            )
            return cursor.rowcount == 1

    def delete_device_profile_acks(self, *, acked_by: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM device_runtime_profile_acks WHERE acked_by = ?",
                (acked_by,),
            )
            return cursor.rowcount

    def get_device_acoustic_capability(self, *, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_acoustic_capabilities WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def upsert_device_acoustic_capability(
        self,
        *,
        device_id: str,
        board_profile: str,
        firmware_version_range: str,
        acoustic_profile_version: int,
        simultaneous_capture_playback: bool,
        aec_reference_type: str,
        aec_verified: bool,
        max_barge_in_level: str,
        tested_volume_range: str,
        tested_distance_m: float | None,
        test_report_uri: str,
        approved_at: str,
        approved_by: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO device_acoustic_capabilities (
                    device_id, board_profile, firmware_version_range,
                    acoustic_profile_version, simultaneous_capture_playback,
                    aec_reference_type, aec_verified, max_barge_in_level,
                    tested_volume_range, tested_distance_m, test_report_uri,
                    approved_at, approved_by, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    board_profile = excluded.board_profile,
                    firmware_version_range = excluded.firmware_version_range,
                    acoustic_profile_version = excluded.acoustic_profile_version,
                    simultaneous_capture_playback = excluded.simultaneous_capture_playback,
                    aec_reference_type = excluded.aec_reference_type,
                    aec_verified = excluded.aec_verified,
                    max_barge_in_level = excluded.max_barge_in_level,
                    tested_volume_range = excluded.tested_volume_range,
                    tested_distance_m = excluded.tested_distance_m,
                    test_report_uri = excluded.test_report_uri,
                    approved_at = excluded.approved_at,
                    approved_by = excluded.approved_by,
                    revoked_at = NULL
                """,
                (
                    device_id,
                    board_profile,
                    firmware_version_range,
                    acoustic_profile_version,
                    1 if simultaneous_capture_playback else 0,
                    aec_reference_type,
                    1 if aec_verified else 0,
                    max_barge_in_level,
                    tested_volume_range,
                    tested_distance_m,
                    test_report_uri,
                    approved_at,
                    approved_by,
                ),
            )
            row = connection.execute(
                "SELECT * FROM device_acoustic_capabilities WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same transaction inserted the row
                raise RuntimeError("acoustic capability was not persisted")
            return dict(row)

    def revoke_device_acoustic_capability(self, *, device_id: str, revoked_at: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE device_acoustic_capabilities SET revoked_at = ?
                WHERE device_id = ? AND revoked_at IS NULL
                """,
                (revoked_at, device_id),
            )
            return cursor.rowcount == 1

    def create_device_control_intent(
        self,
        *,
        intent_id: str,
        device_id: str,
        intent_type: str,
        requested_by: str,
        requested_at: str,
        expires_at: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO device_control_intents (
                    intent_id, device_id, intent_type, requested_by,
                    requested_at, expires_at, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    intent_id,
                    device_id,
                    intent_type,
                    requested_by,
                    requested_at,
                    expires_at,
                    requested_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM device_control_intents WHERE intent_id = ?",
                (intent_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same transaction inserted the row
                raise RuntimeError("device control intent was not persisted")
            return dict(row)

    def list_device_control_intents(self, *, device_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM device_control_intents
                WHERE device_id = ?
                ORDER BY requested_at DESC
                LIMIT 50
                """,
                (device_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def create_device_media_session(
        self,
        *,
        session_id: str,
        device_id: str,
        binding_id: str,
        binding_version: int,
        subject_id: str,
        active_subject_id: str | None,
        client_id: str,
        runtime: str,
        protocol_version: int,
        stream_epoch: int,
        firmware_version: str,
        board_profile: str,
        runtime_profile_version: int,
        settings_version: int,
        audio_mode_requested: str,
        ticket_jti: str,
        created_at: str,
        expires_at: str,
        audio_mode_effective: str = "",
        aec_profile_version: int | None = None,
    ) -> dict[str, Any]:
        """Persist one server-issued device media session (plan section 10.3)."""

        if stream_epoch < 1 or stream_epoch > _DEVICE_STREAM_EPOCH_MAX:
            raise ValueError("device media stream_epoch must be a positive uint32")

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO device_media_sessions (
                    session_id, device_id, binding_id, binding_version,
                    subject_id, active_subject_id, client_id, runtime, protocol_version,
                    stream_epoch, firmware_version, board_profile,
                    runtime_profile_version, settings_version,
                    audio_mode_requested, audio_mode_effective,
                    aec_profile_version, ticket_jti, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    device_id,
                    binding_id,
                    binding_version,
                    subject_id,
                    active_subject_id,
                    client_id,
                    runtime,
                    protocol_version,
                    stream_epoch,
                    firmware_version,
                    board_profile,
                    runtime_profile_version,
                    settings_version,
                    audio_mode_requested,
                    audio_mode_effective,
                    aec_profile_version,
                    ticket_jti,
                    created_at,
                    expires_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM device_media_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same transaction inserted the row
                raise RuntimeError("device media session was not persisted")
            return dict(row)

    def rotate_device_media_session_transport(
        self,
        *,
        session_id: str,
        expected_stream_epoch: int,
        stream_epoch: int,
        client_id: str,
        firmware_version: str,
        board_profile: str,
        runtime_profile_version: int,
        settings_version: int,
        audio_mode_requested: str,
        ticket_jti: str,
        expires_at: str,
        counter_updated_at: str,
    ) -> dict[str, Any]:
        """CAS-rotate one active direct Session onto a newer transport epoch.

        The Session row is the durable Control-plane projection of the active
        conversation.  A reconnect updates it in place so a late close report
        from the old epoch cannot close the replacement transport, while the
        PostgreSQL Session authority and conversation id remain unchanged.
        """

        if (
            expected_stream_epoch < 1
            or expected_stream_epoch > _DEVICE_STREAM_EPOCH_MAX
            or stream_epoch <= expected_stream_epoch
            or stream_epoch > _DEVICE_STREAM_EPOCH_MAX
        ):
            raise ValueError("stream_epoch must advance")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            counter = connection.execute(
                "SELECT last_stream_epoch FROM device_media_epoch_counters WHERE device_id = "
                "(SELECT device_id FROM device_media_sessions WHERE session_id = ?)",
                (session_id,),
            ).fetchone()
            counter_epoch = int(counter[0]) if counter is not None else expected_stream_epoch
            if counter_epoch != expected_stream_epoch:
                raise ValueError("device_media_session_epoch_conflict")
            cursor = connection.execute(
                """
                UPDATE device_media_sessions
                SET stream_epoch = ?, client_id = ?, firmware_version = ?,
                    board_profile = ?, runtime_profile_version = ?,
                    settings_version = ?, audio_mode_requested = ?,
                    audio_mode_effective = '', aec_profile_version = NULL,
                    ticket_jti = ?, expires_at = ?, connected_at = NULL
                WHERE session_id = ? AND stream_epoch = ?
                  AND runtime = 'direct_voice_core' AND protocol_version = 2
                  AND closed_at IS NULL
                """,
                (
                    stream_epoch,
                    client_id,
                    firmware_version,
                    board_profile,
                    runtime_profile_version,
                    settings_version,
                    audio_mode_requested,
                    ticket_jti,
                    expires_at,
                    session_id,
                    expected_stream_epoch,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("device_media_session_epoch_conflict")
            connection.execute(
                """
                INSERT INTO device_media_epoch_counters (
                    device_id, last_stream_epoch, updated_at
                ) SELECT device_id, ?, ? FROM device_media_sessions WHERE session_id = ?
                ON CONFLICT(device_id) DO UPDATE SET
                    last_stream_epoch = excluded.last_stream_epoch,
                    updated_at = excluded.updated_at
                """,
                (stream_epoch, counter_updated_at, session_id),
            )
            row = connection.execute(
                "SELECT * FROM device_media_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - same transaction updated it
                raise RuntimeError("device media session disappeared during reconnect")
            return dict(row)

    def get_device_media_session(self, *, session_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_media_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def next_device_media_stream_epoch(self, *, device_id: str) -> int:
        """Reserve the next monotonic device epoch transactionally.

        Gaps are safe; reuse is not. This counter advances before ticket
        issuance so a failed request can never cause a later stale socket to
        share its epoch.
        """

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_stream_epoch FROM device_media_epoch_counters WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            historical = connection.execute(
                "SELECT COALESCE(MAX(stream_epoch), 0) FROM device_media_sessions "
                "WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            current = max(
                int(row[0]) if row is not None else 0,
                int(historical[0]) if historical is not None else 0,
            )
            if current >= _DEVICE_STREAM_EPOCH_MAX:
                raise ValueError("device media stream_epoch exhausted")
            next_epoch = current + 1
            connection.execute(
                "INSERT INTO device_media_epoch_counters "
                "(device_id, last_stream_epoch, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET "
                "last_stream_epoch = excluded.last_stream_epoch, "
                "updated_at = excluded.updated_at",
                (device_id, next_epoch, datetime.now(UTC).isoformat()),
            )
            return next_epoch

    def latest_device_media_session(self, *, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM device_media_sessions WHERE device_id = ? "
                "ORDER BY stream_epoch DESC, created_at DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def delete_device_media_session(self, *, session_id: str) -> bool:
        """Delete one unissued/aborted device-media projection by exact id."""

        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM device_media_sessions WHERE session_id = ?",
                (session_id,),
            )
            return cursor.rowcount == 1

    def close_device_media_session(
        self,
        *,
        session_id: str,
        reason: str,
        closed_at: str,
        expected_stream_epoch: int | None = None,
    ) -> bool:
        """Mark one device-media projection closed, preserving the first reason.

        Idempotent: a projection already carrying closed_at is left
        untouched so a retried close after an authority replay never
        overwrites the original reason. The abort path keeps using
        delete_device_media_session (hard delete) and is unaffected.
        """
        with self._connection() as connection:
            sql = (
                "UPDATE device_media_sessions SET closed_at = ?, close_reason = ? "
                "WHERE session_id = ? AND closed_at IS NULL"
            )
            params: tuple[object, ...] = (closed_at, reason, session_id)
            if expected_stream_epoch is not None:
                sql += " AND stream_epoch = ?"
                params += (expected_stream_epoch,)
            cursor = connection.execute(sql, params)
            return cursor.rowcount == 1

    def record_device_media_disconnect(
        self,
        *,
        session_id: str,
        expected_stream_epoch: int,
        reason: str,
        disconnected_at: str,
    ) -> bool:
        """Record a transport loss without terminating Session authority.

        Network loss, Edge restart, and lease supersession end one transport
        epoch only.  They deliberately leave ``closed_at`` unset so the same
        Session may reconnect on a strictly newer epoch.  The epoch CAS keeps
        a late report from an old socket from overwriting current diagnostics.
        """

        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE device_media_sessions "
                "SET last_disconnected_at = ?, last_disconnect_reason = ? "
                "WHERE session_id = ? AND stream_epoch = ? AND closed_at IS NULL",
                (
                    disconnected_at,
                    reason,
                    session_id,
                    expected_stream_epoch,
                ),
            )
            return cursor.rowcount == 1

    def delete_device_media_sessions(self, *, subject_id: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM device_media_sessions WHERE subject_id = ?",
                (subject_id,),
            )
            return cursor.rowcount

    def delete_device_control_intents(self, *, requested_by: str) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM device_control_intents WHERE requested_by = ?",
                (requested_by,),
            )
            return cursor.rowcount
