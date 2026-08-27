"""SQLite schema, migrations, and initialization for the memory store."""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '朋友',
    bio TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    phone_number_masked TEXT NOT NULL DEFAULT '',
    companion_id TEXT CHECK (
        companion_id IS NULL OR companion_id IN (
            'starlight', 'taoxi', 'mianmian', 'axu', 'xuanmo', 'zhiyao', 'yanxi'
        )
    ),
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    auto_summary INTEGER NOT NULL DEFAULT 1 CHECK (auto_summary IN (0, 1)),
    voice_reply INTEGER NOT NULL DEFAULT 1 CHECK (voice_reply IN (0, 1)),
    gentle_reminders INTEGER NOT NULL DEFAULT 0 CHECK (gentle_reminders IN (0, 1)),
    reject_non_owner_voice INTEGER NOT NULL DEFAULT 1
        CHECK (reject_non_owner_voice IN (0, 1)),
    subject_category TEXT NOT NULL DEFAULT 'unknown'
        CHECK (subject_category IN ('unknown', 'minor', 'adult')),
    birth_year_band TEXT NOT NULL DEFAULT 'unknown'
        CHECK (birth_year_band IN ('unknown', 'under_14', '14_17', 'adult')),
    age_evidence_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed')),
    subject_revision INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0),
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

CREATE TABLE IF NOT EXISTS external_identities (
    provider TEXT NOT NULL CHECK (provider IN ('wechat_openid', 'wechat_phone')),
    subject_hash TEXT NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, subject_hash),
    UNIQUE (provider, user_id),
    FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_external_identities_user
ON external_identities(user_id, provider);

CREATE TABLE IF NOT EXISTS profile_avatars (
    user_id TEXT PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL CHECK (
        content_type IN ('image/png', 'image/jpeg', 'image/webp')
    ),
    content BLOB NOT NULL,
    sha256 CHAR(64) NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    refresh_hash CHAR(64) NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
ON auth_sessions(user_id, expires_at);

CREATE TABLE IF NOT EXISTS auth_session_refresh_tokens (
    refresh_hash CHAR(64) PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES auth_sessions(session_id) ON DELETE CASCADE,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_auth_upgrades (
    legacy_token_hash CHAR(64) PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE REFERENCES auth_sessions(session_id) ON DELETE CASCADE,
    recovery_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    emotion TEXT,
    local_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, client_message_id),
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
    resource_owner_account_id TEXT NOT NULL,
    room_name TEXT NOT NULL UNIQUE,
    voice_backend TEXT NOT NULL DEFAULT 'cascade'
        CHECK (voice_backend IN ('cascade', 'qwen_omni')),
    omni_sdp_exchanges INTEGER NOT NULL DEFAULT 0
        CHECK (omni_sdp_exchanges >= 0),
    interaction_mode TEXT NOT NULL DEFAULT 'companion'
        CHECK (interaction_mode IN ('companion', 'self_preview', 'legacy', 'archive')),
    session_focus TEXT NOT NULL DEFAULT 'chat'
        CHECK (session_focus IN ('chat', 'tutor_english', 'tutor_homework')),
    mode_policy_version TEXT NOT NULL DEFAULT 's2-v1',
    digital_self_version_id TEXT,
    digital_self_manifest_sha256 TEXT,
    preview_grant_id TEXT,
    self_preview_perspective TEXT CHECK (
        self_preview_perspective IS NULL
        OR self_preview_perspective IN ('owner', 'child', 'friend')
    ),
    relationship_profile_id TEXT,
    relationship_profile_version INTEGER,
    legacy_grant_id TEXT,
    legacy_actor_role TEXT CHECK (
        legacy_actor_role IS NULL OR legacy_actor_role IN ('owner_preview', 'grantee')
    ),
    legacy_grantee_account_id TEXT,
    legacy_shell_id TEXT,
    legacy_grant_snapshot_sha256 TEXT,
    legacy_scope_sha256 TEXT,
    legacy_voice_allowed INTEGER CHECK (
        legacy_voice_allowed IS NULL OR legacy_voice_allowed IN (0, 1)
    ),
    legacy_expires_at TEXT,
    companion_style_id TEXT,
    companion_style_version TEXT,
    voice_profile_id TEXT,
    voice_profile_version INTEGER,
    voice_provider TEXT,
    voice_model TEXT,
    voice_resource_id TEXT,
    voice_provider_expires_at TEXT,
    voice_speaker_sha256 TEXT,
    fallback_voice_profile_id TEXT,
    fallback_voice_provider TEXT,
    fallback_voice_model TEXT,
    fallback_voice_resource_id TEXT,
    learning_task_id TEXT,
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
    llm_provider TEXT NOT NULL
        CHECK (llm_provider IN ('qwen', 'bailian_deepseek', 'deepseek')),
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

CREATE TABLE IF NOT EXISTS device_identities (
    device_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    public_key_b64 TEXT NOT NULL UNIQUE,
    firmware_channel TEXT NOT NULL CHECK (firmware_channel IN ('stable', 'canary', 'lab')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY (account_id) REFERENCES profiles(user_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_device_identities_account
ON device_identities(account_id, revoked_at);

CREATE TABLE IF NOT EXISTS device_challenges (
    nonce_hash CHAR(64) PRIMARY KEY,
    device_id TEXT NOT NULL,
    issued_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    used_at_ms INTEGER,
    FOREIGN KEY (device_id) REFERENCES device_identities(device_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_device_challenges_device
ON device_challenges(device_id, expires_at_ms, used_at_ms);

CREATE TABLE IF NOT EXISTS device_settings (
    device_id TEXT PRIMARY KEY,
    settings_json TEXT NOT NULL,
    settings_version INTEGER NOT NULL CHECK (settings_version >= 1),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    update_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS device_runtime_profile_ledger (
    device_id TEXT PRIMARY KEY,
    profile_version INTEGER NOT NULL CHECK (profile_version >= 1),
    runtime_profile_id TEXT,
    content_fingerprint TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL DEFAULT '',
    settings_fingerprint TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS device_runtime_profile_acks (
    device_id TEXT NOT NULL,
    profile_version INTEGER NOT NULL,
    runtime_profile_id TEXT,
    acked_by TEXT NOT NULL,
    acked_at TEXT NOT NULL,
    accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    PRIMARY KEY (device_id, profile_version)
);

CREATE TABLE IF NOT EXISTS device_media_epoch_counters (
    device_id TEXT PRIMARY KEY,
    last_stream_epoch INTEGER NOT NULL CHECK (last_stream_epoch >= 1),
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_profile_acks_actor
ON device_runtime_profile_acks(acked_by, device_id);

CREATE TABLE IF NOT EXISTS device_acoustic_capabilities (
    device_id TEXT PRIMARY KEY,
    board_profile TEXT NOT NULL,
    firmware_version_range TEXT NOT NULL DEFAULT '',
    acoustic_profile_version INTEGER NOT NULL CHECK (acoustic_profile_version >= 1),
    simultaneous_capture_playback INTEGER NOT NULL
        CHECK (simultaneous_capture_playback IN (0, 1)),
    aec_reference_type TEXT NOT NULL DEFAULT '',
    aec_verified INTEGER NOT NULL CHECK (aec_verified IN (0, 1)),
    max_barge_in_level TEXT NOT NULL DEFAULT '',
    tested_volume_range TEXT NOT NULL DEFAULT '',
    tested_distance_m REAL,
    test_report_uri TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS device_control_intents (
    intent_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    intent_type TEXT NOT NULL CHECK (intent_type IN ('wifi_reset')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'revoked', 'consumed')),
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_control_intents_device
ON device_control_intents(device_id, status, expires_at);

CREATE TABLE IF NOT EXISTS device_media_sessions (
    session_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    subject_id TEXT NOT NULL,
    active_subject_id TEXT,
    client_id TEXT NOT NULL,
    runtime TEXT NOT NULL CHECK (runtime IN ('livekit_compat', 'direct_voice_core')),
    protocol_version INTEGER NOT NULL CHECK (protocol_version IN (1, 2)),
    stream_epoch INTEGER NOT NULL CHECK (stream_epoch >= 1),
    firmware_version TEXT NOT NULL DEFAULT '',
    board_profile TEXT NOT NULL DEFAULT '',
    runtime_profile_version INTEGER NOT NULL DEFAULT 1
        CHECK (runtime_profile_version >= 1),
    settings_version INTEGER NOT NULL DEFAULT 0
        CHECK (settings_version >= 0),
    audio_mode_requested TEXT NOT NULL DEFAULT 'half_duplex_safe',
    audio_mode_effective TEXT NOT NULL DEFAULT '',
    aec_profile_version INTEGER,
    ticket_jti TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    connected_at TEXT,
    last_disconnected_at TEXT,
    last_disconnect_reason TEXT NOT NULL DEFAULT '',
    closed_at TEXT,
    close_reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_device_media_sessions_device
ON device_media_sessions(device_id, created_at);

CREATE TABLE IF NOT EXISTS media_reply_delivery_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) = 64),
    schema_version TEXT NOT NULL CHECK (schema_version = 'reply-delivery-v1'),
    delivery_id TEXT NOT NULL CHECK (length(delivery_id) BETWEEN 1 AND 512),
    session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
    session_epoch INTEGER NOT NULL CHECK (session_epoch >= 0),
    turn_id INTEGER NOT NULL CHECK (turn_id >= 0),
    generation_id INTEGER NOT NULL CHECK (generation_id >= 0),
    tool_epoch INTEGER NOT NULL CHECK (tool_epoch >= 0),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'first_frame_sent', 'provider_completed', 'actual_heard',
        'playback_ended', 'preempted', 'transport_rejected', 'error',
        'skipped', 'no_audio'
    )),
    terminal_event TEXT CHECK (terminal_event IS NULL OR terminal_event IN (
        'playback_ended', 'preempted', 'transport_rejected', 'error',
        'skipped', 'no_audio'
    )),
    terminal_reason TEXT CHECK (
        terminal_reason IS NULL OR length(terminal_reason) BETWEEN 1 AND 64
    ),
    first_frame_sent INTEGER NOT NULL CHECK (first_frame_sent IN (0, 1)),
    provider_completed INTEGER NOT NULL CHECK (provider_completed IN (0, 1)),
    actual_heard INTEGER NOT NULL CHECK (actual_heard IN (0, 1)),
    playback_ended INTEGER NOT NULL CHECK (playback_ended IN (0, 1)),
    reason TEXT CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 64),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (delivery_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_media_reply_delivery_events_received
ON media_reply_delivery_events(received_at);
"""

_PROFILE_BOOLEAN_COLUMNS = {
    "auto_summary": "INTEGER NOT NULL DEFAULT 1 CHECK (auto_summary IN (0, 1))",
    "voice_reply": "INTEGER NOT NULL DEFAULT 1 CHECK (voice_reply IN (0, 1))",
    "gentle_reminders": "INTEGER NOT NULL DEFAULT 0 CHECK (gentle_reminders IN (0, 1))",
    "reject_non_owner_voice": (
        "INTEGER NOT NULL DEFAULT 1 CHECK (reject_non_owner_voice IN (0, 1))"
    ),
}

_PROFILE_SUBJECT_COLUMNS = {
    "subject_category": (
        "TEXT NOT NULL DEFAULT 'unknown' CHECK (subject_category IN ('unknown', 'minor', 'adult'))"
    ),
    "birth_year_band": (
        "TEXT NOT NULL DEFAULT 'unknown' "
        "CHECK (birth_year_band IN ('unknown', 'under_14', '14_17', 'adult'))"
    ),
    "age_evidence_status": (
        "TEXT NOT NULL DEFAULT 'unverified' "
        "CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed'))"
    ),
    "subject_revision": "INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0)",
}


class SchemaMixin:
    """Schema creation, migrations, and store initialization."""

    if TYPE_CHECKING:
        from pathlib import Path

        path: Path
        _initialized: bool
        _initialize_lock: Any

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
                ledger_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(device_runtime_profile_ledger)"
                    )
                }
                if "profile_fingerprint" not in ledger_columns:
                    connection.execute(
                        "ALTER TABLE device_runtime_profile_ledger ADD COLUMN "
                        "profile_fingerprint TEXT NOT NULL DEFAULT ''"
                    )
                if "settings_fingerprint" not in ledger_columns:
                    connection.execute(
                        "ALTER TABLE device_runtime_profile_ledger ADD COLUMN "
                        "settings_fingerprint TEXT NOT NULL DEFAULT ''"
                    )
                # Old rows held exactly one undifferentiated fingerprint.
                # runtime_profile_id tells us which component produced it.
                connection.execute(
                    "UPDATE device_runtime_profile_ledger "
                    "SET profile_fingerprint = content_fingerprint "
                    "WHERE runtime_profile_id IS NOT NULL AND profile_fingerprint = ''"
                )
                connection.execute(
                    "UPDATE device_runtime_profile_ledger "
                    "SET settings_fingerprint = content_fingerprint "
                    "WHERE runtime_profile_id IS NULL AND settings_fingerprint = ''"
                )
                media_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(device_media_sessions)")
                }
                if "active_subject_id" not in media_columns:
                    connection.execute(
                        "ALTER TABLE device_media_sessions ADD COLUMN active_subject_id TEXT"
                    )
                    # Legacy rows used subject_id for both binding ownership
                    # and the Runtime Profile subject. Backfill exactly once
                    # during schema migration; future null values are an
                    # intentional unknown-safe fence and must stay null.
                    connection.execute(
                        "UPDATE device_media_sessions "
                        "SET active_subject_id = subject_id"
                    )
                for column, definition in {
                    "firmware_version": "TEXT NOT NULL DEFAULT ''",
                    "board_profile": "TEXT NOT NULL DEFAULT ''",
                    "runtime_profile_version": "INTEGER NOT NULL DEFAULT 1 CHECK (runtime_profile_version >= 1)",
                    "settings_version": "INTEGER NOT NULL DEFAULT 0 CHECK (settings_version >= 0)",
                    "audio_mode_requested": "TEXT NOT NULL DEFAULT 'half_duplex_safe'",
                    "audio_mode_effective": "TEXT NOT NULL DEFAULT ''",
                    "aec_profile_version": "INTEGER",
                    "last_disconnected_at": "TEXT",
                    "last_disconnect_reason": "TEXT NOT NULL DEFAULT ''",
                }.items():
                    if column not in media_columns:
                        connection.execute(
                            f"ALTER TABLE device_media_sessions ADD COLUMN {column} {definition}"
                        )
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
                readiness_table = connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'readiness_evidence'"
                ).fetchone()
                if readiness_table and "'bailian_deepseek'" not in str(readiness_table[0]):
                    connection.executescript(
                        """
                        ALTER TABLE readiness_evidence RENAME TO readiness_evidence_legacy;
                        CREATE TABLE readiness_evidence (
                            release_tag TEXT NOT NULL,
                            llm_provider TEXT NOT NULL
                                CHECK (llm_provider IN (
                                    'qwen', 'bailian_deepseek', 'deepseek'
                                )),
                            marked_at TEXT NOT NULL,
                            PRIMARY KEY (release_tag, llm_provider)
                        );
                        INSERT INTO readiness_evidence (
                            release_tag, llm_provider, marked_at
                        )
                        SELECT release_tag, llm_provider, marked_at
                        FROM readiness_evidence_legacy;
                        DROP TABLE readiness_evidence_legacy;
                        """
                    )
                existing_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(profiles)")
                }
                if "phone_number_masked" not in existing_columns:
                    connection.execute(
                        "ALTER TABLE profiles ADD COLUMN "
                        "phone_number_masked TEXT NOT NULL DEFAULT ''"
                    )
                for name, definition in _PROFILE_BOOLEAN_COLUMNS.items():
                    if name not in existing_columns:
                        connection.execute(f"ALTER TABLE profiles ADD COLUMN {name} {definition}")
                for name, definition in _PROFILE_SUBJECT_COLUMNS.items():
                    if name not in existing_columns:
                        connection.execute(f"ALTER TABLE profiles ADD COLUMN {name} {definition}")
                if "companion_id" not in existing_columns:
                    connection.execute("ALTER TABLE profiles ADD COLUMN companion_id TEXT")
                    # Preserve the existing experience for accounts created before
                    # companion selection existed. New registrations remain unset.
                    connection.execute(
                        """
                        UPDATE profiles SET companion_id = 'starlight'
                        WHERE user_id IN (SELECT user_id FROM accounts)
                        """
                    )
                self._migrate_profile_subject_contract(connection)
                self._migrate_profile_companion_constraint(connection)
                message_columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")
                }
                if "client_message_id" not in message_columns:
                    connection.execute("ALTER TABLE messages ADD COLUMN client_message_id TEXT")
                if "request_fingerprint" not in message_columns:
                    connection.execute("ALTER TABLE messages ADD COLUMN request_fingerprint TEXT")
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_user_client_message "
                    "ON messages(user_id, client_message_id) "
                    "WHERE client_message_id IS NOT NULL"
                )
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
                frozen_columns = {
                    "interaction_mode": "TEXT NOT NULL DEFAULT 'companion'",
                    "session_focus": "TEXT NOT NULL DEFAULT 'chat'",
                    "mode_policy_version": "TEXT NOT NULL DEFAULT 's2-v1'",
                    "digital_self_version_id": "TEXT",
                    "digital_self_manifest_sha256": "TEXT",
                    "preview_grant_id": "TEXT",
                    "self_preview_perspective": "TEXT",
                    "relationship_profile_id": "TEXT",
                    "relationship_profile_version": "INTEGER",
                    "legacy_grant_id": "TEXT",
                    "resource_owner_account_id": "TEXT",
                    "legacy_actor_role": "TEXT",
                    "legacy_grantee_account_id": "TEXT",
                    "legacy_shell_id": "TEXT",
                    "legacy_grant_snapshot_sha256": "TEXT",
                    "legacy_scope_sha256": "TEXT",
                    "legacy_voice_allowed": "INTEGER",
                    "legacy_expires_at": "TEXT",
                    "companion_style_id": "TEXT",
                    "companion_style_version": "TEXT",
                    "voice_profile_id": "TEXT",
                    "voice_profile_version": "INTEGER",
                    "voice_provider": "TEXT",
                    "voice_model": "TEXT",
                    "voice_resource_id": "TEXT",
                    "voice_provider_expires_at": "TEXT",
                    "voice_speaker_sha256": "TEXT",
                    "fallback_voice_profile_id": "TEXT",
                    "fallback_voice_provider": "TEXT",
                    "fallback_voice_model": "TEXT",
                    "fallback_voice_resource_id": "TEXT",
                    "learning_task_id": "TEXT",
                }
                for name, definition in frozen_columns.items():
                    if name not in voice_session_columns:
                        connection.execute(
                            f"ALTER TABLE voice_sessions ADD COLUMN {name} {definition}"
                        )
                connection.execute(
                    "UPDATE voice_sessions SET companion_style_id = 'starlight', "
                    "companion_style_version = 'companion-v1' "
                    "WHERE companion_style_id IS NULL"
                )
                connection.execute(
                    "UPDATE voice_sessions SET resource_owner_account_id = user_id "
                    "WHERE resource_owner_account_id IS NULL"
                )
                voice_session_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'voice_sessions'"
                ).fetchone()
                voice_session_definition = str(voice_session_sql[0]) if voice_session_sql else ""
                if (
                    "qwen_audio" in voice_session_definition
                    or "qwen_omni_plus" in voice_session_definition
                ):
                    connection.executescript(
                        """
                        ALTER TABLE voice_sessions RENAME TO voice_sessions_legacy;
                        CREATE TABLE voice_sessions (
                            session_id TEXT PRIMARY KEY,
                            user_id TEXT NOT NULL,
                            resource_owner_account_id TEXT NOT NULL,
                            room_name TEXT NOT NULL UNIQUE,
                            voice_backend TEXT NOT NULL DEFAULT 'cascade'
                                CHECK (voice_backend IN ('cascade', 'qwen_omni')),
                            omni_sdp_exchanges INTEGER NOT NULL DEFAULT 0
                                CHECK (omni_sdp_exchanges >= 0),
                            interaction_mode TEXT NOT NULL DEFAULT 'companion'
                                CHECK (interaction_mode IN ('companion', 'self_preview', 'legacy', 'archive')),
                            session_focus TEXT NOT NULL DEFAULT 'chat'
                                CHECK (session_focus IN ('chat', 'tutor_english', 'tutor_homework')),
                            mode_policy_version TEXT NOT NULL DEFAULT 's2-v1',
                            digital_self_version_id TEXT,
                            digital_self_manifest_sha256 TEXT,
                            preview_grant_id TEXT,
                            self_preview_perspective TEXT,
                            relationship_profile_id TEXT,
                            relationship_profile_version INTEGER,
                            legacy_grant_id TEXT,
                            legacy_actor_role TEXT,
                            legacy_grantee_account_id TEXT,
                            legacy_shell_id TEXT,
                            legacy_grant_snapshot_sha256 TEXT,
                            legacy_scope_sha256 TEXT,
                            legacy_voice_allowed INTEGER,
                            legacy_expires_at TEXT,
                            companion_style_id TEXT,
                            companion_style_version TEXT,
                            voice_profile_id TEXT,
                            voice_profile_version INTEGER,
                            voice_provider TEXT,
                            voice_model TEXT,
                            voice_resource_id TEXT,
                            voice_provider_expires_at TEXT,
                            voice_speaker_sha256 TEXT,
                            fallback_voice_profile_id TEXT,
                            fallback_voice_provider TEXT,
                            fallback_voice_model TEXT,
                            fallback_voice_resource_id TEXT,
                            learning_task_id TEXT,
                            created_at TEXT NOT NULL,
                            FOREIGN KEY (user_id) REFERENCES profiles(user_id)
                                ON DELETE CASCADE
                        );
                        INSERT INTO voice_sessions (
                            session_id, user_id, resource_owner_account_id,
                            room_name, voice_backend,
                            omni_sdp_exchanges, interaction_mode, session_focus,
                            mode_policy_version,
                            digital_self_version_id, digital_self_manifest_sha256,
                            preview_grant_id, self_preview_perspective,
                            relationship_profile_id, relationship_profile_version,
                            legacy_grant_id, legacy_actor_role,
                            legacy_grantee_account_id, legacy_shell_id,
                            legacy_grant_snapshot_sha256, legacy_scope_sha256,
                            legacy_voice_allowed, legacy_expires_at,
                            companion_style_id, companion_style_version,
                            voice_profile_id, voice_profile_version, voice_provider,
                            voice_model, voice_resource_id, voice_provider_expires_at,
                            voice_speaker_sha256, fallback_voice_profile_id,
                            fallback_voice_provider, fallback_voice_model,
                            fallback_voice_resource_id,
                            learning_task_id, created_at
                        )
                        SELECT
                            session_id,
                            user_id,
                            user_id,
                            room_name,
                            CASE
                                WHEN voice_backend = 'qwen_omni_plus' THEN 'qwen_omni'
                                WHEN voice_backend = 'qwen_audio' THEN 'cascade'
                                ELSE voice_backend
                            END,
                            COALESCE(omni_sdp_exchanges, 0),
                            'companion', 'chat', 's2-v1', NULL, NULL, NULL, NULL,
                            NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                            NULL, 'starlight', 'companion-v1',
                            NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                            NULL, NULL, NULL, NULL, NULL,
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

    @staticmethod
    def _migrate_profile_subject_contract(connection: sqlite3.Connection) -> None:
        """Quarantine historical default-adult rows and canonicalize age values.

        The former schema could not distinguish an explicitly verified adult
        from a row created through ``DEFAULT 'adult'``.  The multi-subject
        contract therefore treats every historical adult row as unverified
        ``unknown`` until a new age-evidence flow confirms it.  Minor rows keep
        their fail-closed category while the old age-band spelling is mapped.
        """

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'profiles'"
        ).fetchone()
        definition = str(row[0]) if row and row[0] else ""
        if (
            "DEFAULT 'unknown'" in definition
            and "'14_17'" in definition
            and "'14_to_17'" not in definition
            and "subject_category IN ('unknown', 'minor', 'adult')" in definition
        ):
            return
        category_pattern = (
            r"subject_category\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'adult'\s+"
            r"CHECK\s*\(\s*subject_category\s+IN\s*\(\s*'adult'\s*,\s*'minor'\s*\)\s*\)"
        )
        category_replacement = (
            "subject_category TEXT NOT NULL DEFAULT 'unknown' "
            "CHECK (subject_category IN ('unknown', 'minor', 'adult'))"
        )
        migrated_definition, category_replacements = re.subn(
            category_pattern,
            category_replacement,
            definition,
            count=1,
            flags=re.IGNORECASE,
        )
        age_pattern = (
            r"birth_year_band\s+TEXT\s+NOT\s+NULL\s+DEFAULT\s+'unknown'\s+"
            r"CHECK\s*\(\s*birth_year_band\s+IN\s*\(\s*'unknown'\s*,\s*"
            r"'under_14'\s*,\s*'14_to_17'\s*,\s*'18_or_over'\s*\)\s*\)"
        )
        age_replacement = (
            "birth_year_band TEXT NOT NULL DEFAULT 'unknown' "
            "CHECK (birth_year_band IN ('unknown', 'under_14', '14_17', 'adult'))"
        )
        migrated_definition, age_replacements = re.subn(
            age_pattern,
            age_replacement,
            migrated_definition,
            count=1,
            flags=re.IGNORECASE,
        )
        if category_replacements != 1 or age_replacements != 1:
            raise RuntimeError("profiles subject contract migration could not rewrite schema")
        columns = [str(column[1]) for column in connection.execute("PRAGMA table_info(profiles)")]
        if not columns:
            raise RuntimeError("profiles schema is unavailable")

        def quoted(column: str) -> str:
            return f'"{column.replace(chr(34), chr(34) * 2)}"'

        insert_columns = ", ".join(quoted(column) for column in columns)
        select_values: list[str] = []
        for column in columns:
            if column == "subject_category":
                select_values.append(
                    "CASE WHEN subject_category = 'minor' THEN 'minor' ELSE 'unknown' END"
                )
            elif column == "birth_year_band":
                select_values.append(
                    "CASE WHEN birth_year_band = 'under_14' THEN 'under_14' "
                    "WHEN birth_year_band = '14_to_17' THEN '14_17' "
                    "ELSE 'unknown' END"
                )
            elif column == "age_evidence_status":
                select_values.append("'unverified'")
            elif column == "subject_revision":
                select_values.append("subject_revision + 1")
            else:
                select_values.append(quoted(column))

        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE profiles RENAME TO profiles_legacy_subject")
            connection.execute(migrated_definition)
            connection.execute(
                f"INSERT INTO profiles ({insert_columns}) "
                f"SELECT {', '.join(select_values)} FROM profiles_legacy_subject"
            )
            connection.execute("DROP TABLE profiles_legacy_subject")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA legacy_alter_table=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("profiles subject contract migration failed foreign_key_check")

    @staticmethod
    def _migrate_profile_companion_constraint(connection: sqlite3.Connection) -> None:
        """Expand the historical five-companion CHECK without losing profile data.

        SQLite cannot alter a CHECK constraint in place.  The original CREATE
        statement is transformed narrowly, preserving every current/future
        column and constraint, then copied under ``legacy_alter_table`` so all
        child foreign keys continue to reference ``profiles``.
        """

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'profiles'"
        ).fetchone()
        definition = str(row[0]) if row and row[0] else ""
        if "'zhiyao'" in definition and "'yanxi'" in definition:
            return
        old_values = (
            r"'starlight'\s*,\s*'taoxi'\s*,\s*'mianmian'\s*,\s*"
            r"'axu'\s*,\s*'xuanmo'"
        )
        replacement = "'starlight', 'taoxi', 'mianmian', 'axu', 'xuanmo', 'zhiyao', 'yanxi'"
        migrated_definition, replacements = re.subn(
            old_values,
            replacement,
            definition,
            count=1,
        )
        # Profiles created before companion constraints existed already accept
        # the new ids. There is no reason to rebuild an unconstrained table.
        if replacements == 0:
            return
        columns = [str(column[1]) for column in connection.execute("PRAGMA table_info(profiles)")]
        if not columns:
            raise RuntimeError("profiles schema is unavailable")
        quoted_columns = ", ".join(
            f'"{column.replace(chr(34), chr(34) * 2)}"' for column in columns
        )
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA legacy_alter_table=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE profiles RENAME TO profiles_legacy")
            connection.execute(migrated_definition)
            connection.execute(
                f"INSERT INTO profiles ({quoted_columns}) "
                f"SELECT {quoted_columns} FROM profiles_legacy"
            )
            connection.execute("DROP TABLE profiles_legacy")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA legacy_alter_table=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("profiles migration failed foreign_key_check")
