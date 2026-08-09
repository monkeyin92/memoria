CREATE TABLE IF NOT EXISTS guardian_links (
    link_id UUID PRIMARY KEY,
    guardian_user_id TEXT NOT NULL,
    minor_user_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('parent', 'legal_guardian')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked')),
    verified_via TEXT NOT NULL CHECK (
        verified_via IN ('wechat_identity', 'manual_review')
    ),
    binding_code_hash CHAR(64) NOT NULL CHECK (
        binding_code_hash ~ '^[0-9a-f]{64}$'
    ),
    binding_expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CHECK (guardian_user_id <> minor_user_id),
    CHECK (binding_expires_at > created_at),
    CHECK (
        (status = 'pending' AND activated_at IS NULL AND revoked_at IS NULL)
        OR (status = 'active' AND activated_at IS NOT NULL AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_links_live_pair
ON guardian_links(guardian_user_id, minor_user_id)
WHERE status IN ('pending', 'active');

CREATE INDEX IF NOT EXISTS idx_guardian_links_guardian_status
ON guardian_links(guardian_user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_guardian_links_minor_status
ON guardian_links(minor_user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS guardian_consents (
    consent_id UUID PRIMARY KEY,
    link_id UUID NOT NULL REFERENCES guardian_links(link_id) ON DELETE CASCADE,
    consent_kind TEXT NOT NULL CHECK (consent_kind IN (
        'minor_voice_session', 'memory_retention',
        'weekly_report', 'corpus_recording'
    )),
    policy_version TEXT NOT NULL CHECK (
        char_length(policy_version) BETWEEN 1 AND 64
    ),
    granted_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    evidence_event_id TEXT NOT NULL CHECK (
        char_length(evidence_event_id) BETWEEN 1 AND 128
    ),
    revocation_evidence_event_id TEXT CHECK (
        revocation_evidence_event_id IS NULL
        OR char_length(revocation_evidence_event_id) BETWEEN 1 AND 128
    ),
    CHECK (
        (revoked_at IS NULL AND revocation_evidence_event_id IS NULL)
        OR (revoked_at IS NOT NULL AND revocation_evidence_event_id IS NOT NULL)
    )
);

ALTER TABLE guardian_consents ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

DO $guardian_consent_expiry_constraints$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'guardian_consent_expiry_after_grant'
    ) THEN
        ALTER TABLE guardian_consents
            ADD CONSTRAINT guardian_consent_expiry_after_grant
            CHECK (expires_at IS NULL OR expires_at > granted_at);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'guardian_consent_corpus_retention'
    ) THEN
        ALTER TABLE guardian_consents
            ADD CONSTRAINT guardian_consent_corpus_retention
            CHECK (
                (consent_kind = 'corpus_recording' AND expires_at IS NOT NULL
                    AND expires_at <= granted_at + INTERVAL '30 days')
                OR (consent_kind <> 'corpus_recording' AND expires_at IS NULL)
            );
    END IF;
END
$guardian_consent_expiry_constraints$;

DROP INDEX IF EXISTS idx_guardian_consents_active_kind;
CREATE UNIQUE INDEX idx_guardian_consents_active_kind
ON guardian_consents(link_id, consent_kind)
WHERE revoked_at IS NULL AND expires_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_consents_grant_evidence
ON guardian_consents(evidence_event_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_consents_revoke_evidence
ON guardian_consents(revocation_evidence_event_id)
WHERE revocation_evidence_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_guardian_consents_link_granted
ON guardian_consents(link_id, granted_at DESC);

CREATE TABLE IF NOT EXISTS guardian_corpus_samples (
    sample_id UUID PRIMARY KEY,
    minor_user_id TEXT NOT NULL,
    consent_id UUID NOT NULL REFERENCES guardian_consents(consent_id),
    source_event_id TEXT NOT NULL UNIQUE CHECK (
        char_length(source_event_id) BETWEEN 1 AND 128
    ),
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count > 0),
    content_sha256 CHAR(64) NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    encryption_key_version TEXT NOT NULL,
    object_backend TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    CHECK (expires_at > created_at),
    CHECK (expires_at <= created_at + INTERVAL '30 days')
);

CREATE INDEX IF NOT EXISTS idx_guardian_corpus_expiry
ON guardian_corpus_samples(deleted_at, expires_at, sample_id);

CREATE INDEX IF NOT EXISTS idx_guardian_corpus_minor
ON guardian_corpus_samples(minor_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS tutor_practice_sessions (
    session_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    focus TEXT NOT NULL CHECK (focus IN ('tutor_english', 'tutor_homework')),
    task_id TEXT NOT NULL CHECK (char_length(task_id) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'paused', 'completed')),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    event_ids_json JSONB NOT NULL CHECK (jsonb_typeof(event_ids_json) = 'array'),
    practiced_seconds INTEGER NOT NULL DEFAULT 0 CHECK (practiced_seconds >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_practice_account_updated
ON tutor_practice_sessions(account_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS tutor_study_progress (
    account_id TEXT PRIMARY KEY,
    practiced_seconds INTEGER NOT NULL DEFAULT 0 CHECK (practiced_seconds >= 0),
    active_days_json JSONB NOT NULL CHECK (jsonb_typeof(active_days_json) = 'array'),
    current_streak_days INTEGER NOT NULL DEFAULT 0 CHECK (current_streak_days >= 0),
    weak_points_json JSONB NOT NULL CHECK (jsonb_typeof(weak_points_json) = 'array'),
    mastered_skills_json JSONB NOT NULL CHECK (jsonb_typeof(mastered_skills_json) = 'array'),
    source_event_ids_json JSONB NOT NULL CHECK (
        jsonb_typeof(source_event_ids_json) = 'array'
    ),
    last_practiced_at TIMESTAMPTZ,
    rebuilt_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS guardian_crisis_events (
    crisis_event_id UUID PRIMARY KEY,
    evidence_event_id TEXT NOT NULL UNIQUE CHECK (
        char_length(evidence_event_id) BETWEEN 1 AND 128
    ),
    minor_user_id TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    script_version TEXT NOT NULL CHECK (
        char_length(script_version) BETWEEN 1 AND 64
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_guardian_crisis_minor_occurred
ON guardian_crisis_events(minor_user_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS guardian_notification_outbox (
    notification_id UUID PRIMARY KEY,
    crisis_event_id UUID NOT NULL REFERENCES guardian_crisis_events(crisis_event_id)
        ON DELETE CASCADE,
    guardian_user_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel = 'wechat_subscription'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR char_length(last_error_code) BETWEEN 1 AND 96
    ),
    UNIQUE(crisis_event_id, guardian_user_id),
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR (status <> 'delivered' AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_guardian_notification_recipient_status
ON guardian_notification_outbox(guardian_user_id, status, created_at DESC);

CREATE OR REPLACE FUNCTION guardian_link_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $guardian_link_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['status', 'activated_at', 'revoked_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status', 'activated_at', 'revoked_at']) THEN
        RAISE EXCEPTION 'guardian link identity is immutable';
    END IF;
    RETURN NEW;
END
$guardian_link_immutable$;

DROP TRIGGER IF EXISTS guardian_link_core_immutable ON guardian_links;
CREATE TRIGGER guardian_link_core_immutable
BEFORE UPDATE ON guardian_links
FOR EACH ROW EXECUTE FUNCTION guardian_link_core_immutable_guard();

CREATE OR REPLACE FUNCTION guardian_consent_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $guardian_consent_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['revoked_at', 'revocation_evidence_event_id'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['revoked_at', 'revocation_evidence_event_id']) THEN
        RAISE EXCEPTION 'guardian consent grant is immutable';
    END IF;
    IF OLD.revoked_at IS NOT NULL AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'revoked guardian consent is immutable';
    END IF;
    RETURN NEW;
END
$guardian_consent_immutable$;

DROP TRIGGER IF EXISTS guardian_consent_core_immutable ON guardian_consents;
CREATE TRIGGER guardian_consent_core_immutable
BEFORE UPDATE ON guardian_consents
FOR EACH ROW EXECUTE FUNCTION guardian_consent_core_immutable_guard();

CREATE OR REPLACE FUNCTION guardian_delete_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $guardian_delete$
BEGIN
    IF current_setting('app.guardian_account_deletion', true) = '1' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'guardian records require the account deletion pipeline';
END
$guardian_delete$;

DROP TRIGGER IF EXISTS guardian_links_delete_guard ON guardian_links;
CREATE TRIGGER guardian_links_delete_guard
BEFORE DELETE ON guardian_links
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

DROP TRIGGER IF EXISTS guardian_consents_delete_guard ON guardian_consents;
CREATE TRIGGER guardian_consents_delete_guard
BEFORE DELETE ON guardian_consents
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

DROP TRIGGER IF EXISTS guardian_corpus_samples_delete_guard ON guardian_corpus_samples;
CREATE TRIGGER guardian_corpus_samples_delete_guard
BEFORE DELETE ON guardian_corpus_samples
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

DROP TRIGGER IF EXISTS tutor_practice_sessions_delete_guard ON tutor_practice_sessions;
CREATE TRIGGER tutor_practice_sessions_delete_guard
BEFORE DELETE ON tutor_practice_sessions
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

DROP TRIGGER IF EXISTS tutor_study_progress_delete_guard ON tutor_study_progress;
CREATE TRIGGER tutor_study_progress_delete_guard
BEFORE DELETE ON tutor_study_progress
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

DROP TRIGGER IF EXISTS guardian_crisis_events_delete_guard ON guardian_crisis_events;
CREATE TRIGGER guardian_crisis_events_delete_guard
BEFORE DELETE ON guardian_crisis_events
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

DROP TRIGGER IF EXISTS guardian_notification_outbox_delete_guard
ON guardian_notification_outbox;
CREATE TRIGGER guardian_notification_outbox_delete_guard
BEFORE DELETE ON guardian_notification_outbox
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

ALTER TABLE guardian_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_corpus_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_study_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_crisis_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_links FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_consents FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_corpus_samples FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_study_progress FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_crisis_events FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_notification_outbox FORCE ROW LEVEL SECURITY;

DO $guardian_policy$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON guardian_links TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON guardian_consents TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON guardian_corpus_samples TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_practice_sessions TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_study_progress TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON guardian_crisis_events TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON guardian_notification_outbox TO memoria_guardian;

        DROP POLICY IF EXISTS guardian_controller_links ON guardian_links;
        CREATE POLICY guardian_controller_links ON guardian_links
            TO memoria_guardian USING (true) WITH CHECK (true);

        DROP POLICY IF EXISTS guardian_controller_consents ON guardian_consents;
        CREATE POLICY guardian_controller_consents ON guardian_consents
            TO memoria_guardian USING (true) WITH CHECK (true);

        DROP POLICY IF EXISTS guardian_controller_corpus_samples ON guardian_corpus_samples;
        CREATE POLICY guardian_controller_corpus_samples ON guardian_corpus_samples
            TO memoria_guardian USING (true) WITH CHECK (true);

        DROP POLICY IF EXISTS guardian_controller_tutor_sessions ON tutor_practice_sessions;
        CREATE POLICY guardian_controller_tutor_sessions ON tutor_practice_sessions
            TO memoria_guardian USING (true) WITH CHECK (true);

        DROP POLICY IF EXISTS guardian_controller_tutor_progress ON tutor_study_progress;
        CREATE POLICY guardian_controller_tutor_progress ON tutor_study_progress
            TO memoria_guardian USING (true) WITH CHECK (true);

        DROP POLICY IF EXISTS guardian_controller_crisis_events ON guardian_crisis_events;
        CREATE POLICY guardian_controller_crisis_events ON guardian_crisis_events
            TO memoria_guardian USING (true) WITH CHECK (true);

        DROP POLICY IF EXISTS guardian_controller_notification_outbox
            ON guardian_notification_outbox;
        CREATE POLICY guardian_controller_notification_outbox ON guardian_notification_outbox
            TO memoria_guardian USING (true) WITH CHECK (true);
    END IF;
END
$guardian_policy$;
