"""Small SQLite store for the H5 conversation history."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from services.guardian.domain import (
    AgeEvidenceStatus,
    BirthYearBand,
    SubjectCategory,
    validate_subject_transition,
)

_DEVICE_STREAM_EPOCH_MAX = (1 << 32) - 1

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
    client_id TEXT NOT NULL,
    runtime TEXT NOT NULL CHECK (runtime IN ('livekit_compat', 'direct_voice_core')),
    protocol_version INTEGER NOT NULL CHECK (protocol_version IN (1, 2)),
    stream_epoch INTEGER NOT NULL CHECK (stream_epoch >= 1),
    firmware_version TEXT NOT NULL DEFAULT '',
    board_profile TEXT NOT NULL DEFAULT '',
    runtime_profile_id TEXT NOT NULL CHECK (length(runtime_profile_id) > 0),
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


class MessageIdempotencyConflictError(ValueError):
    pass


class ExternalIdentityConflictError(ValueError):
    pass


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
                for column, definition in {
                    "firmware_version": "TEXT NOT NULL DEFAULT ''",
                    "board_profile": "TEXT NOT NULL DEFAULT ''",
                    # Historical rows predate the per-Session profile fence.
                    # They migrate as blank and are rejected by Direct resume
                    # and /session-policy instead of being guessed from the
                    # device-global ledger. New writes require a non-blank id.
                    "runtime_profile_id": "TEXT NOT NULL DEFAULT ''",
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
        client_id: str,
        runtime: str,
        protocol_version: int,
        stream_epoch: int,
        firmware_version: str,
        board_profile: str,
        runtime_profile_id: str,
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

        if not runtime_profile_id.strip():
            raise ValueError("device media runtime_profile_id must not be blank")
        if runtime_profile_version < 1:
            raise ValueError("device media runtime_profile_version must be positive")
        if stream_epoch < 1 or stream_epoch > _DEVICE_STREAM_EPOCH_MAX:
            raise ValueError("device media stream_epoch must be a positive uint32")

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO device_media_sessions (
                    session_id, device_id, binding_id, binding_version,
                    subject_id, client_id, runtime, protocol_version,
                    stream_epoch, firmware_version, board_profile,
                    runtime_profile_id, runtime_profile_version, settings_version,
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
                    client_id,
                    runtime,
                    protocol_version,
                    stream_epoch,
                    firmware_version,
                    board_profile,
                    runtime_profile_id,
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
        expected_runtime_profile_id: str,
        expected_runtime_profile_version: int,
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

        if not expected_runtime_profile_id.strip() or expected_runtime_profile_version < 1:
            raise ValueError("device media RuntimeProfile fence is invalid")
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
                    board_profile = ?, settings_version = ?, audio_mode_requested = ?,
                    audio_mode_effective = '', aec_profile_version = NULL,
                    ticket_jti = ?, expires_at = ?, connected_at = NULL
                WHERE session_id = ? AND stream_epoch = ?
                  AND runtime = 'direct_voice_core' AND protocol_version = 2
                  AND runtime_profile_id = ? AND runtime_profile_version = ?
                  AND closed_at IS NULL
                """,
                (
                    stream_epoch,
                    client_id,
                    firmware_version,
                    board_profile,
                    settings_version,
                    audio_mode_requested,
                    ticket_jti,
                    expires_at,
                    session_id,
                    expected_stream_epoch,
                    expected_runtime_profile_id,
                    expected_runtime_profile_version,
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

    def register_account(
        self,
        *,
        user_id: str,
        username: str,
        username_normalized: str,
        password_hash: str,
        display_name: str | None = None,
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, now)
            if display_name:
                connection.execute(
                    """
                    UPDATE profiles
                    SET display_name = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (display_name, now, user_id),
                )
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
            if row is None:
                row = connection.execute(
                    """
                    SELECT profiles.user_id, NULL AS username,
                           NULL AS username_normalized, '' AS password_hash,
                           MIN(external_identities.created_at) AS created_at,
                           MAX(external_identities.updated_at) AS updated_at
                    FROM profiles
                    JOIN external_identities
                      ON external_identities.user_id = profiles.user_id
                    WHERE profiles.user_id = ?
                    GROUP BY profiles.user_id
                    """,
                    (user_id,),
                ).fetchone()
        return dict(row) if row is not None else None

    def external_identity_user(
        self,
        *,
        provider: str,
        subject_hash: str,
    ) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id FROM external_identities
                WHERE provider = ? AND subject_hash = ?
                """,
                (provider, subject_hash),
            ).fetchone()
        return str(row["user_id"]) if row is not None else None

    def has_external_identity(self, *, user_id: str, provider: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM external_identities
                WHERE user_id = ? AND provider = ?
                """,
                (user_id, provider),
            ).fetchone()
        return row is not None

    def bind_external_identities(
        self,
        *,
        preferred_user_id: str,
        identities: Mapping[str, str],
        now: str,
    ) -> tuple[str, bool]:
        clean = {
            str(provider): str(subject_hash)
            for provider, subject_hash in identities.items()
            if str(provider) and str(subject_hash)
        }
        if not clean:
            raise ValueError("at least one external identity is required")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("(?, ?)" for _ in clean)
            parameters: list[str] = []
            for provider, subject_hash in clean.items():
                parameters.extend((provider, subject_hash))
            rows = connection.execute(
                f"""
                SELECT provider, subject_hash, user_id
                FROM external_identities
                WHERE (provider, subject_hash) IN ({placeholders})
                """,
                parameters,
            ).fetchall()
            resolved_users = {str(row["user_id"]) for row in rows}
            if len(resolved_users) > 1:
                raise ExternalIdentityConflictError("external identities belong to different users")
            user_id = next(iter(resolved_users), preferred_user_id)
            self._ensure_profile(connection, user_id, now)
            changed = False
            for provider, subject_hash in clean.items():
                existing_for_user = connection.execute(
                    """
                    SELECT subject_hash FROM external_identities
                    WHERE provider = ? AND user_id = ?
                    """,
                    (provider, user_id),
                ).fetchone()
                if (
                    existing_for_user is not None
                    and str(existing_for_user["subject_hash"]) != subject_hash
                ):
                    raise ExternalIdentityConflictError(
                        f"user already has a different {provider} identity"
                    )
                existing_subject = connection.execute(
                    """
                    SELECT user_id FROM external_identities
                    WHERE provider = ? AND subject_hash = ?
                    """,
                    (provider, subject_hash),
                ).fetchone()
                if existing_subject is not None:
                    if str(existing_subject["user_id"]) != user_id:
                        raise ExternalIdentityConflictError(
                            f"{provider} identity belongs to another user"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO external_identities (
                        provider, subject_hash, user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (provider, subject_hash, user_id, now, now),
                )
                changed = True
        return user_id, changed

    def update_external_profile(
        self,
        *,
        user_id: str,
        display_name: str | None,
        phone_number_masked: str | None,
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_profile(connection, user_id, now)
            current = connection.execute(
                """
                SELECT display_name, phone_number_masked
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None:  # pragma: no cover
                raise RuntimeError("profile initialization failed")
            next_display_name = (
                display_name.strip()
                if display_name is not None and display_name.strip()
                else str(current["display_name"])
            )
            next_phone = (
                phone_number_masked.strip()
                if phone_number_masked is not None and phone_number_masked.strip()
                else str(current["phone_number_masked"])
            )
            connection.execute(
                """
                UPDATE profiles
                SET display_name = ?, phone_number_masked = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (next_display_name, next_phone, now, user_id),
            )
        return self.get_profile(user_id=user_id, now=now)

    def save_profile_avatar(
        self,
        *,
        user_id: str,
        public_id: str,
        content_type: str,
        content: bytes,
        sha256: str,
        avatar_url: str,
        now: str,
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_profile(connection, user_id, now)
            connection.execute(
                """
                INSERT INTO profile_avatars (
                    user_id, public_id, content_type, content, sha256, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    public_id = excluded.public_id,
                    content_type = excluded.content_type,
                    content = excluded.content,
                    sha256 = excluded.sha256,
                    updated_at = excluded.updated_at
                """,
                (user_id, public_id, content_type, content, sha256, now),
            )
            connection.execute(
                "UPDATE profiles SET avatar_url = ?, updated_at = ? WHERE user_id = ?",
                (avatar_url, now, user_id),
            )
        avatar = self.get_profile_avatar(public_id=public_id)
        if avatar is None:  # pragma: no cover
            raise RuntimeError("profile avatar persistence failed")
        return avatar

    def get_profile_avatar(self, *, public_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT user_id, public_id, content_type, content, sha256, updated_at
                FROM profile_avatars WHERE public_id = ?
                """,
                (public_id,),
            ).fetchone()
        return dict(row) if row is not None else None

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
                SELECT user_id, display_name, bio, avatar_url, phone_number_masked,
                       companion_id, timezone,
                       auto_summary, voice_reply, gentle_reminders, reject_non_owner_voice,
                       subject_category, birth_year_band, age_evidence_status, subject_revision,
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
                SELECT session_id, voice_backend, interaction_mode, session_focus,
                       mode_policy_version,
                       resource_owner_account_id,
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
                FROM voice_sessions
                WHERE user_id = ? OR resource_owner_account_id = ?
                   OR legacy_grantee_account_id = ?
                ORDER BY created_at, session_id
                """,
                (user_id, user_id, user_id),
            ).fetchall()
            preview: dict[str, list[dict[str, Any]]] = {
                "grants": [],
                "feedback": [],
                "fidelity_evaluations": [],
                "fidelity_trials": [],
            }
            preview_tables = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name IN (
                        'digital_self_preview_grants',
                        'digital_self_preview_feedback',
                        'digital_self_fidelity_evaluations',
                        'digital_self_fidelity_trials'
                    )
                    """
                ).fetchall()
            }
            if "digital_self_preview_grants" in preview_tables:
                preview["grants"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT grant_id, version_id, manifest_sha256, perspective,
                               status, expires_at, created_at, used_at, revoked_at
                        FROM digital_self_preview_grants
                        WHERE account_id = ? ORDER BY created_at, grant_id
                        """,
                        (user_id,),
                    ).fetchall()
                ]
            if "digital_self_preview_feedback" in preview_tables:
                for row in connection.execute(
                    """
                    SELECT feedback_id, session_id, turn_id, generation_id,
                           tool_epoch, version_id, manifest_sha256, action,
                           target_source_event_ids_json, correction_text,
                           evidence_event_id, created_at
                    FROM digital_self_preview_feedback
                    WHERE account_id = ? ORDER BY created_at, feedback_id
                    """,
                    (user_id,),
                ).fetchall():
                    item = dict(row)
                    item["target_source_event_ids"] = json.loads(
                        str(item.pop("target_source_event_ids_json"))
                    )
                    preview["feedback"].append(item)
            if "digital_self_fidelity_evaluations" in preview_tables:
                preview["fidelity_evaluations"] = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT evaluation_id, version_id, manifest_sha256,
                               status, verdict, verdict_rationale,
                               created_at, completed_at
                        FROM digital_self_fidelity_evaluations
                        WHERE account_id = ? ORDER BY created_at, evaluation_id
                        """,
                        (user_id,),
                    ).fetchall()
                ]
            if "digital_self_fidelity_trials" in preview_tables:
                for row in connection.execute(
                    """
                    SELECT trial_id, evaluation_id, category, prompt,
                           generic_answer, digital_self_answer, digital_self_slot,
                           available, coverage_gap, epistemic_status, has_source,
                           unsupported_fact, decision_inference_disclosed,
                           privacy_refused, identity_disclosed, preferred_slot,
                           rationale, answered_at
                    FROM digital_self_fidelity_trials
                    WHERE account_id = ?
                    ORDER BY evaluation_id, category, trial_id
                    """,
                    (user_id,),
                ).fetchall():
                    item = dict(row)
                    digital_slot = str(item.pop("digital_self_slot"))
                    generic_answer = str(item.pop("generic_answer"))
                    digital_answer = str(item.pop("digital_self_answer"))
                    item["slot_a"] = digital_answer if digital_slot == "a" else generic_answer
                    item["slot_b"] = digital_answer if digital_slot == "b" else generic_answer
                    for key in (
                        "available",
                        "has_source",
                        "unsupported_fact",
                        "decision_inference_disclosed",
                        "privacy_refused",
                        "identity_disclosed",
                    ):
                        item[key] = bool(item[key])
                    preview["fidelity_trials"].append(item)
        profile_data = dict(profile) if profile is not None else None
        if profile_data is not None:
            for key in (
                "auto_summary",
                "voice_reply",
                "gentle_reminders",
                "reject_non_owner_voice",
            ):
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
            "digital_self_preview": preview,
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
            preview_delete_order = (
                "digital_self_fidelity_trials",
                "digital_self_fidelity_evaluations",
                "digital_self_preview_feedback",
                "digital_self_preview_grants",
            )
            existing_preview_tables = {
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name IN (
                        'digital_self_preview_grants',
                        'digital_self_preview_feedback',
                        'digital_self_fidelity_evaluations',
                        'digital_self_fidelity_trials'
                    )
                    """
                ).fetchall()
            }
            preview_counts: dict[str, int] = {}
            for table in preview_delete_order:
                if table not in existing_preview_tables:
                    continue
                preview_counts[table] = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE account_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    f"DELETE FROM {table} WHERE account_id = ?",
                    (user_id,),
                )
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
                "auth_sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM auth_sessions WHERE user_id = ?", (user_id,)
                    ).fetchone()[0]
                ),
                "device_identities": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_identities WHERE account_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_settings": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_settings WHERE updated_by = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_runtime_profile_acks": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_runtime_profile_acks WHERE acked_by = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_control_intents": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_control_intents WHERE requested_by = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                "device_media_sessions": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM device_media_sessions WHERE subject_id = ?",
                        (user_id,),
                    ).fetchone()[0]
                ),
                **preview_counts,
            }
            connection.execute("DELETE FROM device_identities WHERE account_id = ?", (user_id,))
            connection.execute("DELETE FROM device_settings WHERE updated_by = ?", (user_id,))
            connection.execute(
                "DELETE FROM device_runtime_profile_acks WHERE acked_by = ?", (user_id,)
            )
            connection.execute(
                "DELETE FROM device_control_intents WHERE requested_by = ?", (user_id,)
            )
            connection.execute("DELETE FROM device_media_sessions WHERE subject_id = ?", (user_id,))
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

    def update_subject_profile(
        self,
        *,
        user_id: str,
        subject_category: SubjectCategory,
        birth_year_band: BirthYearBand,
        age_evidence_status: AgeEvidenceStatus = "unverified",
        now: str,
        age_eligible: bool = False,
        guardian_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Apply the subject ratchet and invalidate every existing auth capability.

        Capability routes always re-read the profile, while revoking all auth
        sessions makes already-minted access and refresh tokens unusable on
        their next request.  The category, age band, revision, and revocation
        are committed in one SQLite transaction.
        """

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_profile(connection, user_id, now)
            current = connection.execute(
                """
                SELECT subject_category, birth_year_band, age_evidence_status, subject_revision
                FROM profiles WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if current is None:  # pragma: no cover
                raise RuntimeError("profile initialization failed")
            current_category = cast(SubjectCategory, str(current["subject_category"]))
            current_band = cast(BirthYearBand, str(current["birth_year_band"]))
            current_evidence = cast(
                AgeEvidenceStatus,
                str(current["age_evidence_status"]),
            )
            validate_subject_transition(
                current_category=current_category,
                current_birth_year_band=current_band,
                current_age_evidence_status=current_evidence,
                target_category=subject_category,
                target_birth_year_band=birth_year_band,
                target_age_evidence_status=age_evidence_status,
                age_eligible=age_eligible,
                guardian_confirmed=guardian_confirmed,
            )
            changed = (
                current_category != subject_category
                or current_band != birth_year_band
                or current_evidence != age_evidence_status
            )
            if changed:
                connection.execute(
                    """
                    UPDATE profiles
                    SET subject_category = ?, birth_year_band = ?, age_evidence_status = ?,
                        subject_revision = subject_revision + 1, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        subject_category,
                        birth_year_band,
                        age_evidence_status,
                        now,
                        user_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE auth_sessions
                    SET revoked_at = COALESCE(revoked_at, ?), updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now, now, user_id),
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
            raise RuntimeError("subject profile update failed")
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
