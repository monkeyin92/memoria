-- Control API account store (accounts, auth sessions, devices, voice
-- sessions, messages), the subject-deletion ledger and the digital-self
-- preview tables.
--
-- These tables mirror the SQLite store column for column so one set of SQL
-- serves both backends: timestamps stay ISO-8601 TEXT (the code compares them
-- as strings) and booleans stay 0/1 BIGINT with CHECK constraints.
--
-- Row access is role-scoped like the evolution store: FORCE RLS keeps every
-- other role out, and memoria_control sees all rows. Pre-auth lookups (refresh
-- token hash, external identity) have no account context, so per-account
-- policies are a follow-up, not a regression: the SQLite file had none.

-- Roles first: a NOLOGIN owner owns every object, and the runtime role
-- memoria_control (password set by init-memoria.sh) is never an owner, so
-- FORCE RLS applies to it.
DO $control_roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_control_owner') THEN
        CREATE ROLE memoria_control_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_control') THEN
        CREATE ROLE memoria_control LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$control_roles$;
ALTER ROLE memoria_control_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
-- Foreign-key checks run as the table owner, so both roles need USAGE on the
-- schema that holds the tables (public in production, never CREATE).
DO $control_schema_usage$
BEGIN
    EXECUTE format(
        'GRANT USAGE ON SCHEMA %I TO memoria_control_owner, memoria_control',
        current_schema()
    );
END
$control_schema_usage$;

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
    auto_summary BIGINT NOT NULL DEFAULT 1 CHECK (auto_summary IN (0, 1)),
    voice_reply BIGINT NOT NULL DEFAULT 1 CHECK (voice_reply IN (0, 1)),
    gentle_reminders BIGINT NOT NULL DEFAULT 0 CHECK (gentle_reminders IN (0, 1)),
    reject_non_owner_voice BIGINT NOT NULL DEFAULT 1
        CHECK (reject_non_owner_voice IN (0, 1)),
    subject_category TEXT NOT NULL DEFAULT 'unknown'
        CHECK (subject_category IN ('unknown', 'minor', 'adult')),
    birth_year_band TEXT NOT NULL DEFAULT 'unknown'
        CHECK (birth_year_band IN ('unknown', 'under_14', '14_17', 'adult')),
    age_evidence_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed')),
    subject_revision BIGINT NOT NULL DEFAULT 0 CHECK (subject_revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    user_id TEXT PRIMARY KEY REFERENCES profiles(user_id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    username_normalized TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_identities (
    provider TEXT NOT NULL CHECK (provider IN ('wechat_openid', 'wechat_phone')),
    subject_hash TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, subject_hash),
    UNIQUE (provider, user_id)
);

CREATE INDEX IF NOT EXISTS idx_external_identities_user
ON external_identities(user_id, provider);

CREATE TABLE IF NOT EXISTS profile_avatars (
    user_id TEXT PRIMARY KEY REFERENCES profiles(user_id) ON DELETE CASCADE,
    public_id TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL CHECK (
        content_type IN ('image/png', 'image/jpeg', 'image/webp')
    ),
    content BYTEA NOT NULL,
    sha256 CHAR(64) NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
    refresh_hash CHAR(64) NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT
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
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
    client_message_id TEXT NOT NULL,
    request_fingerprint CHAR(64) NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    text TEXT NOT NULL,
    emotion TEXT,
    local_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, client_message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_user_date
ON messages(user_id, local_date, id);

CREATE TABLE IF NOT EXISTS daily_summaries (
    user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
    summary_date TEXT NOT NULL,
    content_json TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('qwen', 'deepseek', 'fallback')),
    message_count BIGINT NOT NULL,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, summary_date)
);

CREATE TABLE IF NOT EXISTS readiness_evidence (
    release_tag TEXT NOT NULL,
    llm_provider TEXT NOT NULL
        CHECK (llm_provider IN ('qwen', 'bailian_deepseek', 'deepseek')),
    marked_at TEXT NOT NULL,
    PRIMARY KEY (release_tag, llm_provider)
);

CREATE TABLE IF NOT EXISTS voice_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
    resource_owner_account_id TEXT NOT NULL,
    room_name TEXT NOT NULL UNIQUE,
    voice_backend TEXT NOT NULL DEFAULT 'cascade'
        CHECK (voice_backend IN ('cascade', 'qwen_omni')),
    omni_sdp_exchanges BIGINT NOT NULL DEFAULT 0
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
    relationship_profile_version BIGINT,
    legacy_grant_id TEXT,
    legacy_actor_role TEXT CHECK (
        legacy_actor_role IS NULL OR legacy_actor_role IN ('owner_preview', 'grantee')
    ),
    legacy_grantee_account_id TEXT,
    legacy_shell_id TEXT,
    legacy_grant_snapshot_sha256 TEXT,
    legacy_scope_sha256 TEXT,
    legacy_voice_allowed BIGINT CHECK (
        legacy_voice_allowed IS NULL OR legacy_voice_allowed IN (0, 1)
    ),
    legacy_expires_at TEXT,
    companion_style_id TEXT,
    companion_style_version TEXT,
    voice_profile_id TEXT,
    voice_profile_version BIGINT,
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
    created_at TEXT NOT NULL
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
    account_id TEXT NOT NULL REFERENCES profiles(user_id) ON DELETE CASCADE,
    public_key_b64 TEXT NOT NULL UNIQUE,
    firmware_channel TEXT NOT NULL CHECK (firmware_channel IN ('stable', 'canary', 'lab')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_identities_account
ON device_identities(account_id, revoked_at);

CREATE TABLE IF NOT EXISTS device_challenges (
    nonce_hash CHAR(64) PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES device_identities(device_id) ON DELETE CASCADE,
    issued_at_ms BIGINT NOT NULL,
    expires_at_ms BIGINT NOT NULL,
    used_at_ms BIGINT
);

CREATE INDEX IF NOT EXISTS idx_device_challenges_device
ON device_challenges(device_id, expires_at_ms, used_at_ms);

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

CREATE TABLE IF NOT EXISTS device_acoustic_capabilities (
    device_id TEXT PRIMARY KEY,
    board_profile TEXT NOT NULL,
    firmware_version_range TEXT NOT NULL DEFAULT '',
    acoustic_profile_version BIGINT NOT NULL CHECK (acoustic_profile_version >= 1),
    simultaneous_capture_playback BIGINT NOT NULL
        CHECK (simultaneous_capture_playback IN (0, 1)),
    aec_reference_type TEXT NOT NULL DEFAULT '',
    aec_verified BIGINT NOT NULL CHECK (aec_verified IN (0, 1)),
    max_barge_in_level TEXT NOT NULL DEFAULT '',
    tested_volume_range TEXT NOT NULL DEFAULT '',
    tested_distance_m DOUBLE PRECISION,
    test_report_uri TEXT NOT NULL DEFAULT '',
    approved_at TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS device_media_epoch_counters (
    device_id TEXT PRIMARY KEY,
    last_stream_epoch BIGINT NOT NULL CHECK (last_stream_epoch >= 1),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_media_sessions (
    session_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    subject_id TEXT NOT NULL,
    active_subject_id TEXT,
    client_id TEXT NOT NULL,
    runtime TEXT NOT NULL CHECK (runtime IN ('livekit_compat', 'direct_voice_core')),
    protocol_version BIGINT NOT NULL CHECK (protocol_version IN (1, 2)),
    stream_epoch BIGINT NOT NULL CHECK (stream_epoch >= 1),
    firmware_version TEXT NOT NULL DEFAULT '',
    board_profile TEXT NOT NULL DEFAULT '',
    runtime_profile_version BIGINT NOT NULL DEFAULT 1
        CHECK (runtime_profile_version >= 1),
    settings_version BIGINT NOT NULL DEFAULT 0
        CHECK (settings_version >= 0),
    audio_mode_requested TEXT NOT NULL DEFAULT 'half_duplex_safe',
    audio_mode_effective TEXT NOT NULL DEFAULT '',
    aec_profile_version BIGINT,
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

CREATE TABLE IF NOT EXISTS device_runtime_profile_ledger (
    device_id TEXT PRIMARY KEY,
    profile_version BIGINT NOT NULL CHECK (profile_version >= 1),
    runtime_profile_id TEXT,
    content_fingerprint TEXT NOT NULL,
    profile_fingerprint TEXT NOT NULL DEFAULT '',
    settings_fingerprint TEXT NOT NULL DEFAULT '',
    issued_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS device_runtime_profile_acks (
    device_id TEXT NOT NULL,
    profile_version BIGINT NOT NULL,
    runtime_profile_id TEXT,
    acked_by TEXT NOT NULL,
    acked_at TEXT NOT NULL,
    accepted BIGINT NOT NULL CHECK (accepted IN (0, 1)),
    PRIMARY KEY (device_id, profile_version)
);

CREATE INDEX IF NOT EXISTS idx_device_profile_acks_actor
ON device_runtime_profile_acks(acked_by, device_id);

CREATE TABLE IF NOT EXISTS device_settings (
    device_id TEXT PRIMARY KEY,
    settings_json TEXT NOT NULL,
    settings_version BIGINT NOT NULL CHECK (settings_version >= 1),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    update_reason TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS media_reply_delivery_events (
    event_id TEXT PRIMARY KEY CHECK (length(event_id) = 64),
    schema_version TEXT NOT NULL CHECK (schema_version = 'reply-delivery-v1'),
    delivery_id TEXT NOT NULL CHECK (length(delivery_id) BETWEEN 1 AND 512),
    session_id TEXT NOT NULL CHECK (length(session_id) BETWEEN 1 AND 128),
    session_epoch BIGINT NOT NULL CHECK (session_epoch >= 0),
    turn_id BIGINT NOT NULL CHECK (turn_id >= 0),
    generation_id BIGINT NOT NULL CHECK (generation_id >= 0),
    tool_epoch BIGINT NOT NULL CHECK (tool_epoch >= 0),
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
    first_frame_sent BIGINT NOT NULL CHECK (first_frame_sent IN (0, 1)),
    provider_completed BIGINT NOT NULL CHECK (provider_completed IN (0, 1)),
    actual_heard BIGINT NOT NULL CHECK (actual_heard IN (0, 1)),
    playback_ended BIGINT NOT NULL CHECK (playback_ended IN (0, 1)),
    reason TEXT CHECK (reason IS NULL OR length(reason) BETWEEN 1 AND 64),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE (delivery_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_media_reply_delivery_events_received
ON media_reply_delivery_events(received_at);

CREATE TABLE IF NOT EXISTS subject_deletions (
    account_id_hash TEXT NOT NULL,
    subject_id_hash TEXT NOT NULL,
    account_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('deleting', 'completed')),
    step TEXT NOT NULL,
    redact_identity BIGINT NOT NULL CHECK (redact_identity IN (0, 1)),
    lineage_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '{}',
    last_error TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (account_id_hash, subject_id_hash)
);

-- Digital-self preview tables (SelfPreviewRegistry). They live here because
-- account deletion erases them in the same transaction as the account.
CREATE TABLE IF NOT EXISTS digital_self_preview_grants (
    grant_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    perspective TEXT NOT NULL CHECK (perspective IN ('owner', 'child', 'friend')),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used_at TEXT,
    revoked_at TEXT,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    UNIQUE (account_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_preview_grants_account_status
ON digital_self_preview_grants(account_id, status, expires_at);

CREATE TABLE IF NOT EXISTS digital_self_preview_feedback (
    feedback_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id BIGINT NOT NULL CHECK (turn_id >= 0),
    generation_id BIGINT NOT NULL CHECK (generation_id >= 0),
    tool_epoch BIGINT NOT NULL CHECK (tool_epoch >= 0),
    version_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    action TEXT NOT NULL CHECK (action IN ('not_like_me', 'correction')),
    target_source_event_ids_json TEXT NOT NULL,
    correction_text TEXT,
    evidence_event_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (account_id, idempotency_key),
    UNIQUE (account_id, evidence_event_id)
);

CREATE INDEX IF NOT EXISTS idx_preview_feedback_fence
ON digital_self_preview_feedback(
    account_id, session_id, turn_id, generation_id, tool_epoch, version_id
);

CREATE TABLE IF NOT EXISTS digital_self_fidelity_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
    mapping_seed TEXT NOT NULL,
    verdict TEXT CHECK (verdict IS NULL OR verdict IN ('approve', 'reject')),
    verdict_rationale TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    UNIQUE (account_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_fidelity_evaluation_account_version
ON digital_self_fidelity_evaluations(account_id, version_id, created_at);

CREATE TABLE IF NOT EXISTS digital_self_fidelity_trials (
    trial_id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL
        REFERENCES digital_self_fidelity_evaluations(evaluation_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL CHECK (
        category IN ('fact', 'decision', 'relationship', 'humor', 'emotion', 'unknown', 'privacy')
    ),
    prompt TEXT NOT NULL,
    generic_answer TEXT NOT NULL,
    digital_self_answer TEXT NOT NULL,
    digital_self_slot TEXT NOT NULL CHECK (digital_self_slot IN ('a', 'b')),
    available BIGINT NOT NULL CHECK (available IN (0, 1)),
    coverage_gap TEXT,
    epistemic_status TEXT NOT NULL CHECK (
        epistemic_status IN ('fact', 'inference', 'unknown', 'not_applicable')
    ),
    has_source BIGINT NOT NULL CHECK (has_source IN (0, 1)),
    unsupported_fact BIGINT NOT NULL CHECK (unsupported_fact IN (0, 1)),
    decision_inference_disclosed BIGINT NOT NULL CHECK (
        decision_inference_disclosed IN (0, 1)
    ),
    privacy_refused BIGINT NOT NULL CHECK (privacy_refused IN (0, 1)),
    identity_disclosed BIGINT NOT NULL CHECK (identity_disclosed IN (0, 1)),
    preferred_slot TEXT CHECK (preferred_slot IS NULL OR preferred_slot IN ('a', 'b')),
    rationale TEXT,
    answered_at TEXT,
    UNIQUE (evaluation_id, category)
);

CREATE INDEX IF NOT EXISTS idx_fidelity_trials_account_evaluation
ON digital_self_fidelity_trials(account_id, evaluation_id, category);

DO $control_rls$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'profiles', 'accounts', 'external_identities', 'profile_avatars',
        'auth_sessions', 'auth_session_refresh_tokens', 'legacy_auth_upgrades',
        'messages', 'daily_summaries', 'readiness_evidence', 'voice_sessions',
        'voice_session_tombstones', 'account_deletions', 'device_identities',
        'device_challenges', 'device_control_intents', 'device_acoustic_capabilities',
        'device_media_epoch_counters', 'device_media_sessions',
        'device_runtime_profile_ledger', 'device_runtime_profile_acks',
        'device_settings', 'media_reply_delivery_events', 'subject_deletions',
        'digital_self_preview_grants', 'digital_self_preview_feedback',
        'digital_self_fidelity_evaluations', 'digital_self_fidelity_trials'
    ] LOOP
        EXECUTE format('ALTER TABLE %I OWNER TO memoria_control_owner', table_name);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', table_name);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
        EXECUTE format('REVOKE ALL ON %I FROM PUBLIC', table_name);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO memoria_control',
            table_name
        );
        EXECUTE format('DROP POLICY IF EXISTS control_role_rows ON %I', table_name);
        EXECUTE format(
            'CREATE POLICY control_role_rows ON %I TO memoria_control '
            'USING (true) WITH CHECK (true)',
            table_name
        );
    END LOOP;
    -- UPDATE lets the one-time SQLite migration continue the sequence (setval).
    GRANT USAGE, SELECT, UPDATE ON SEQUENCE messages_id_seq TO memoria_control;
END
$control_rls$;

-- Schema version ledger. This file is the idempotent baseline (version 1) and
-- is re-applied every release; any change it cannot express idempotently goes
-- in database/migrations/NNNN_<name>.sql, applied once, in order, each in one
-- transaction that also records its row here. The runtime role only reads the
-- ledger to refuse a database that is behind the code.
CREATE TABLE IF NOT EXISTS control_schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version >= 1),
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
ALTER TABLE control_schema_migrations OWNER TO memoria_control_owner;
ALTER TABLE control_schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_schema_migrations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON control_schema_migrations FROM PUBLIC;
GRANT SELECT ON control_schema_migrations TO memoria_control;
DROP POLICY IF EXISTS control_role_reads_versions ON control_schema_migrations;
CREATE POLICY control_role_reads_versions ON control_schema_migrations
    FOR SELECT TO memoria_control USING (true);
INSERT INTO control_schema_migrations (version, name, applied_at)
VALUES (1, 'baseline', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'))
ON CONFLICT (version) DO NOTHING;
