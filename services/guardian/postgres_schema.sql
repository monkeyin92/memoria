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

-- Person-scoped consents for subjects that have no account at all (the
-- ``parent_for_child`` + ``subject_draft`` binding shape).  The grantor is the
-- binding owner; the subject never confirms anything and no guardian link is
-- manufactured.  A distinct table keeps the two key spaces (link-scoped vs
-- person-scoped) honest instead of making ``link_id`` nullable.
CREATE TABLE IF NOT EXISTS guardian_person_consents (
    consent_id UUID PRIMARY KEY,
    subject_person_id TEXT NOT NULL CHECK (
        char_length(subject_person_id) BETWEEN 1 AND 128
    ),
    grantor_person_id TEXT NOT NULL CHECK (
        char_length(grantor_person_id) BETWEEN 1 AND 128
    ),
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
    CHECK (subject_person_id <> grantor_person_id),
    CHECK (expires_at IS NULL OR expires_at > granted_at),
    CHECK (
        (consent_kind = 'corpus_recording' AND expires_at IS NOT NULL
            AND expires_at <= granted_at + INTERVAL '30 days')
        OR (consent_kind <> 'corpus_recording' AND expires_at IS NULL)
    ),
    CHECK (
        (revoked_at IS NULL AND revocation_evidence_event_id IS NULL)
        OR (revoked_at IS NOT NULL AND revocation_evidence_event_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_person_consents_active_kind
ON guardian_person_consents(subject_person_id, consent_kind)
WHERE revoked_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_person_consents_grant_evidence
ON guardian_person_consents(evidence_event_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_guardian_person_consents_revoke_evidence
ON guardian_person_consents(revocation_evidence_event_id)
WHERE revocation_evidence_event_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_guardian_person_consents_subject
ON guardian_person_consents(subject_person_id, granted_at DESC);

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
    subject_id TEXT,
    actor_id TEXT,
    voice_session_id TEXT,
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

ALTER TABLE tutor_practice_sessions ADD COLUMN IF NOT EXISTS subject_id TEXT;
ALTER TABLE tutor_practice_sessions ADD COLUMN IF NOT EXISTS actor_id TEXT;
ALTER TABLE tutor_practice_sessions ADD COLUMN IF NOT EXISTS voice_session_id TEXT;
UPDATE tutor_practice_sessions SET actor_id = account_id WHERE actor_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_tutor_practice_subject_updated
ON tutor_practice_sessions(subject_id, updated_at DESC);

-- Study progress is one projection per subject (unique subject_id); the acting
-- owner account_id is an ordinary column, so one owner can hold progress for
-- several subjects.
CREATE TABLE IF NOT EXISTS tutor_study_progress (
    account_id TEXT NOT NULL,
    subject_id TEXT,
    actor_id TEXT,
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

ALTER TABLE tutor_study_progress ADD COLUMN IF NOT EXISTS subject_id TEXT;
ALTER TABLE tutor_study_progress ADD COLUMN IF NOT EXISTS actor_id TEXT;
UPDATE tutor_study_progress SET actor_id = account_id WHERE actor_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tutor_progress_subject
ON tutor_study_progress(subject_id) WHERE subject_id IS NOT NULL;

-- In-place rekey: installs created before the per-subject model keyed the
-- table by account_id, so a second subject under the same owner failed the
-- insert.  Only that exact legacy key is dropped; every row is kept.
DO $tutor_progress_rekey$
DECLARE
    legacy_key NAME;
BEGIN
    SELECT con.conname INTO legacy_key
    FROM pg_constraint con
    JOIN pg_attribute att
      ON att.attrelid = con.conrelid AND att.attname = 'account_id'
    WHERE con.conrelid = 'public.tutor_study_progress'::regclass
      AND con.contype = 'p'
      AND con.conkey = ARRAY[att.attnum];
    IF legacy_key IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE public.tutor_study_progress DROP CONSTRAINT %I',
            legacy_key
        );
    END IF;
END
$tutor_progress_rekey$;
ALTER TABLE tutor_study_progress ALTER COLUMN account_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tutor_progress_account
ON tutor_study_progress(account_id);

CREATE TABLE IF NOT EXISTS tutor_practice_evidence (
    event_id TEXT PRIMARY KEY,
    assessment_id TEXT UNIQUE,
    kind TEXT NOT NULL CHECK (
        kind IN ('tutor.practice_turn_recorded', 'tutor.practice_completed')
    ),
    subject_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    envelope_json JSONB NOT NULL CHECK (jsonb_typeof(envelope_json) = 'object'),
    envelope_sha256 CHAR(64) NOT NULL CHECK (envelope_sha256 ~ '^[0-9a-f]{64}$'),
    commit_sha256 CHAR(64) NOT NULL CHECK (commit_sha256 ~ '^[0-9a-f]{64}$'),
    outcome TEXT,
    skill_key TEXT,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL CHECK (session_revision >= 0),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_evidence_subject_created
ON tutor_practice_evidence(subject_id, created_at);

CREATE TABLE IF NOT EXISTS tutor_commit_outbox (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (
        kind IN ('tutor.practice_turn_recorded', 'tutor.practice_completed')
    ),
    subject_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    archive_payload_json JSONB NOT NULL CHECK (jsonb_typeof(archive_payload_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'delivered')),
    claimed_by TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_at TIMESTAMPTZ,
    lease_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tutor_outbox_pending
ON tutor_commit_outbox(status, subject_id, created_at);

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
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'delivered', 'failed', 'no_subscription')
    ),
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

-- Push delivery state (forward-only upgrade of an existing outbox).  The
-- lease columns are only written by the maintenance-owned delivery functions
-- below; ``reserved_template_id`` records that this row already consumed one
-- subscribe-message acceptance, so a reclaim never consumes a second one.
ALTER TABLE guardian_notification_outbox
    ADD COLUMN IF NOT EXISTS claimed_by TEXT CHECK (
        claimed_by IS NULL OR char_length(claimed_by) BETWEEN 1 AND 128
    ),
    ADD COLUMN IF NOT EXISTS lease_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reserved_template_id TEXT CHECK (
        reserved_template_id IS NULL
        OR reserved_template_id ~ '^[A-Za-z0-9_-]{1,128}$'
    );

DO $guardian_outbox_status$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'guardian_notification_outbox'::regclass
          AND conname = 'guardian_notification_outbox_status_check'
          AND pg_get_constraintdef(oid) LIKE '%no_subscription%'
    ) THEN
        ALTER TABLE guardian_notification_outbox
            DROP CONSTRAINT IF EXISTS guardian_notification_outbox_status_check;
        ALTER TABLE guardian_notification_outbox
            ADD CONSTRAINT guardian_notification_outbox_status_check CHECK (
                status IN ('pending', 'delivered', 'failed', 'no_subscription')
            );
    END IF;
END
$guardian_outbox_status$;

CREATE INDEX IF NOT EXISTS idx_guardian_notification_recipient_status
ON guardian_notification_outbox(guardian_user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_guardian_notification_push_claim
ON guardian_notification_outbox(created_at, notification_id)
WHERE status = 'pending';

-- One-time WeChat subscribe-message acceptances per guardian and template.
-- ``openid`` is the send address, written only from a server-side
-- jscode2session exchange that matched the guardian's bound WeChat identity.
CREATE TABLE IF NOT EXISTS guardian_push_subscriptions (
    guardian_user_id TEXT NOT NULL CHECK (
        char_length(guardian_user_id) BETWEEN 1 AND 128
    ),
    template_id TEXT NOT NULL CHECK (template_id ~ '^[A-Za-z0-9_-]{1,128}$'),
    openid TEXT CHECK (openid IS NULL OR char_length(openid) BETWEEN 1 AND 128),
    remaining INTEGER NOT NULL DEFAULT 0 CHECK (remaining BETWEEN 0 AND 20),
    last_result TEXT NOT NULL CHECK (last_result IN ('accept', 'reject', 'ban')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (guardian_user_id, template_id),
    CHECK (remaining = 0 OR openid IS NOT NULL)
);

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

DROP TRIGGER IF EXISTS tutor_practice_evidence_delete_guard ON tutor_practice_evidence;
CREATE TRIGGER tutor_practice_evidence_delete_guard
BEFORE DELETE ON tutor_practice_evidence
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

DROP TRIGGER IF EXISTS tutor_commit_outbox_delete_guard ON tutor_commit_outbox;
CREATE TRIGGER tutor_commit_outbox_delete_guard
BEFORE DELETE ON tutor_commit_outbox
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

DROP TRIGGER IF EXISTS guardian_push_subscriptions_delete_guard
ON guardian_push_subscriptions;
CREATE TRIGGER guardian_push_subscriptions_delete_guard
BEFORE DELETE ON guardian_push_subscriptions
FOR EACH ROW EXECUTE FUNCTION guardian_delete_guard();

-- Delivery only settles state: the recipient and the event a notification
-- belongs to can never be rewritten by an UPDATE.
CREATE OR REPLACE FUNCTION guardian_notification_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $guardian_notification_immutable$
BEGIN
    IF NEW.notification_id IS DISTINCT FROM OLD.notification_id
       OR NEW.crisis_event_id IS DISTINCT FROM OLD.crisis_event_id
       OR NEW.guardian_user_id IS DISTINCT FROM OLD.guardian_user_id
       OR NEW.channel IS DISTINCT FROM OLD.channel
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'guardian notification identity is immutable';
    END IF;
    RETURN NEW;
END
$guardian_notification_immutable$;

DROP TRIGGER IF EXISTS guardian_notification_core_immutable
ON guardian_notification_outbox;
CREATE TRIGGER guardian_notification_core_immutable
BEFORE UPDATE ON guardian_notification_outbox
FOR EACH ROW EXECUTE FUNCTION guardian_notification_core_immutable_guard();

CREATE OR REPLACE FUNCTION tutor_authority_columns_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $tutor_authority$
BEGIN
    IF NEW.subject_id IS NULL OR btrim(NEW.subject_id) = ''
       OR NEW.actor_id IS NULL OR btrim(NEW.actor_id) = '' THEN
        RAISE EXCEPTION 'tutor authority columns are required';
    END IF;
    RETURN NEW;
END
$tutor_authority$;

CREATE OR REPLACE FUNCTION tutor_session_voice_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $tutor_session$
BEGIN
    IF NEW.voice_session_id IS NULL OR btrim(NEW.voice_session_id) = '' THEN
        RAISE EXCEPTION 'tutor authority columns are required';
    END IF;
    RETURN NEW;
END
$tutor_session$;

DROP TRIGGER IF EXISTS tutor_sessions_authority_guard ON tutor_practice_sessions;
CREATE TRIGGER tutor_sessions_authority_guard
BEFORE INSERT OR UPDATE ON tutor_practice_sessions
FOR EACH ROW EXECUTE FUNCTION tutor_authority_columns_guard();

DROP TRIGGER IF EXISTS tutor_sessions_voice_guard ON tutor_practice_sessions;
CREATE TRIGGER tutor_sessions_voice_guard
BEFORE INSERT OR UPDATE ON tutor_practice_sessions
FOR EACH ROW EXECUTE FUNCTION tutor_session_voice_guard();

DROP TRIGGER IF EXISTS tutor_progress_authority_guard ON tutor_study_progress;
CREATE TRIGGER tutor_progress_authority_guard
BEFORE INSERT OR UPDATE ON tutor_study_progress
FOR EACH ROW EXECUTE FUNCTION tutor_authority_columns_guard();

DROP TRIGGER IF EXISTS tutor_evidence_authority_guard ON tutor_practice_evidence;
CREATE TRIGGER tutor_evidence_authority_guard
BEFORE INSERT OR UPDATE ON tutor_practice_evidence
FOR EACH ROW EXECUTE FUNCTION tutor_authority_columns_guard();

DROP TRIGGER IF EXISTS tutor_outbox_authority_guard ON tutor_commit_outbox;
CREATE TRIGGER tutor_outbox_authority_guard
BEFORE INSERT OR UPDATE ON tutor_commit_outbox
FOR EACH ROW EXECUTE FUNCTION tutor_authority_columns_guard();

-- PR-13: account-scoped export/delete runs through SECURITY DEFINER
-- functions owned by the maintenance role.  The API role can never set a
-- broad-scope GUC to widen RLS: policies require current_user to BE the
-- maintenance role, and the function body sets the scope flag itself.
CREATE OR REPLACE FUNCTION guardian_tutor_account_scope_export(
    target_account_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $maintenance$
DECLARE
    result JSONB;
BEGIN
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    SELECT jsonb_build_object(
        'tutor_practice_sessions', COALESCE((
            SELECT jsonb_agg(to_jsonb(row))
            FROM (
                SELECT * FROM tutor_practice_sessions
                WHERE account_id = target_account_id
                   OR actor_id = target_account_id
                ORDER BY created_at, session_id
            ) row
        ), '[]'::jsonb),
        'tutor_study_progress', COALESCE((
            SELECT jsonb_agg(to_jsonb(row))
            FROM (
                SELECT * FROM tutor_study_progress
                WHERE account_id = target_account_id
                   OR actor_id = target_account_id
                ORDER BY subject_id IS NULL, subject_id
            ) row
        ), '[]'::jsonb),
        'tutor_practice_evidence', COALESCE((
            SELECT jsonb_agg(to_jsonb(row))
            FROM (
                SELECT * FROM tutor_practice_evidence
                WHERE subject_id = target_account_id
                   OR actor_id = target_account_id
                ORDER BY created_at, event_id
            ) row
        ), '[]'::jsonb),
        'tutor_commit_outbox', COALESCE((
            SELECT jsonb_agg(to_jsonb(row))
            FROM (
                SELECT * FROM tutor_commit_outbox
                WHERE subject_id = target_account_id
                   OR actor_id = target_account_id
                ORDER BY created_at, event_id
            ) row
        ), '[]'::jsonb)
    ) INTO result;
    RETURN result;
END
$maintenance$;

CREATE OR REPLACE FUNCTION guardian_tutor_account_scope_delete(
    target_account_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $maintenance$
DECLARE
    deleted JSONB;
BEGIN
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    PERFORM set_config('app.guardian_account_deletion', '1', true);
    WITH removed_sessions AS (
        DELETE FROM tutor_practice_sessions
        WHERE account_id = target_account_id OR actor_id = target_account_id
        RETURNING 1
    ), removed_progress AS (
        DELETE FROM tutor_study_progress
        WHERE account_id = target_account_id OR actor_id = target_account_id
        RETURNING 1
    ), removed_evidence AS (
        DELETE FROM tutor_practice_evidence
        WHERE subject_id = target_account_id OR actor_id = target_account_id
        RETURNING 1
    ), removed_outbox AS (
        DELETE FROM tutor_commit_outbox
        WHERE subject_id = target_account_id OR actor_id = target_account_id
        RETURNING 1
    )
    SELECT jsonb_build_object(
        'tutor_practice_sessions', (SELECT count(*) FROM removed_sessions),
        'tutor_study_progress', (SELECT count(*) FROM removed_progress),
        'tutor_practice_evidence', (SELECT count(*) FROM removed_evidence),
        'tutor_commit_outbox', (SELECT count(*) FROM removed_outbox)
    ) INTO deleted;
    RETURN deleted;
END
$maintenance$;

CREATE OR REPLACE FUNCTION guardian_tutor_account_scope_remaining(
    target_account_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $maintenance$
DECLARE
    result JSONB;
BEGIN
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    SELECT jsonb_build_object(
        'tutor_practice_sessions', (
            SELECT count(*) FROM tutor_practice_sessions
            WHERE account_id = target_account_id OR actor_id = target_account_id
        ),
        'tutor_study_progress', (
            SELECT count(*) FROM tutor_study_progress
            WHERE account_id = target_account_id OR actor_id = target_account_id
        ),
        'tutor_practice_evidence', (
            SELECT count(*) FROM tutor_practice_evidence
            WHERE subject_id = target_account_id OR actor_id = target_account_id
        ),
        'tutor_commit_outbox', (
            SELECT count(*) FROM tutor_commit_outbox
            WHERE subject_id = target_account_id OR actor_id = target_account_id
        )
    ) INTO result;
    RETURN result;
END
$maintenance$;

-- Subject-scoped deletion: one bound subject (a child or elder with no
-- account) inside the binding owner's account.  Same role discipline as the
-- account-scope functions above: SECURITY DEFINER under the maintenance role,
-- which alone may open the scope flags.  A tutor row is the subject's only
-- when it names the subject AND the owner, so the owner's own practice and
-- another subject's practice stay.  Crisis rows carry no account: the crisis
-- evidence is stored under the subject's own id.  Person consents are never
-- touched (they are the consent audit) and neither is the guardian-side
-- subscribe-message ledger.
--
-- The event-id function returns the subject's tutor archive evidence ids:
-- tutor practice is archived under the owner account, and rows archived
-- before the projection carried subject_id can only be found through these
-- tables, so the ids must be read before the rows are deleted.
CREATE OR REPLACE FUNCTION guardian_subject_scope_tutor_event_ids(
    target_account_id TEXT,
    target_subject_id TEXT
) RETURNS TEXT[]
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $maintenance$
DECLARE
    result TEXT[];
BEGIN
    IF COALESCE(btrim(target_account_id), '') = ''
       OR COALESCE(btrim(target_subject_id), '') = ''
       OR target_account_id = target_subject_id THEN
        RAISE EXCEPTION 'subject scope requires a distinct owner account and subject';
    END IF;
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    SELECT COALESCE(array_agg(DISTINCT ids.event_id), ARRAY[]::TEXT[])
    INTO result
    FROM (
        SELECT event_id FROM tutor_practice_evidence
        WHERE subject_id = target_subject_id AND actor_id = target_account_id
        UNION
        SELECT event_id FROM tutor_commit_outbox
        WHERE subject_id = target_subject_id AND actor_id = target_account_id
        UNION
        SELECT archive_payload_json->>'event_id' FROM tutor_commit_outbox
        WHERE subject_id = target_subject_id AND actor_id = target_account_id
        UNION
        SELECT jsonb_array_elements_text(event_ids_json) FROM tutor_practice_sessions
        WHERE subject_id = target_subject_id
          AND (account_id = target_account_id OR actor_id = target_account_id)
        UNION
        SELECT jsonb_array_elements_text(source_event_ids_json) FROM tutor_study_progress
        WHERE subject_id = target_subject_id
          AND (account_id = target_account_id OR actor_id = target_account_id)
    ) ids(event_id)
    WHERE COALESCE(ids.event_id, '') <> '';
    RETURN result;
END
$maintenance$;

CREATE OR REPLACE FUNCTION guardian_subject_scope_delete(
    target_account_id TEXT,
    target_subject_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $maintenance$
DECLARE
    deleted JSONB;
BEGIN
    IF COALESCE(btrim(target_account_id), '') = ''
       OR COALESCE(btrim(target_subject_id), '') = ''
       OR target_account_id = target_subject_id THEN
        RAISE EXCEPTION 'subject scope requires a distinct owner account and subject';
    END IF;
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    PERFORM set_config('memoria.guardian_maintenance_scope', '1', true);
    PERFORM set_config('app.guardian_account_deletion', '1', true);
    WITH removed_notifications AS (
        DELETE FROM guardian_notification_outbox outbox
        USING guardian_crisis_events crisis
        WHERE outbox.crisis_event_id = crisis.crisis_event_id
          AND crisis.minor_user_id = target_subject_id
        RETURNING 1
    ), removed_sessions AS (
        DELETE FROM tutor_practice_sessions
        WHERE subject_id = target_subject_id
          AND (account_id = target_account_id OR actor_id = target_account_id)
        RETURNING 1
    ), removed_progress AS (
        DELETE FROM tutor_study_progress
        WHERE subject_id = target_subject_id
          AND (account_id = target_account_id OR actor_id = target_account_id)
        RETURNING 1
    ), removed_evidence AS (
        DELETE FROM tutor_practice_evidence
        WHERE subject_id = target_subject_id AND actor_id = target_account_id
        RETURNING 1
    ), removed_outbox AS (
        DELETE FROM tutor_commit_outbox
        WHERE subject_id = target_subject_id AND actor_id = target_account_id
        RETURNING 1
    )
    SELECT jsonb_build_object(
        'guardian_notifications', (SELECT count(*) FROM removed_notifications),
        'tutor_practice_sessions', (SELECT count(*) FROM removed_sessions),
        'tutor_study_progress', (SELECT count(*) FROM removed_progress),
        'tutor_practice_evidence', (SELECT count(*) FROM removed_evidence),
        'tutor_commit_outbox', (SELECT count(*) FROM removed_outbox)
    ) INTO deleted;
    -- The crisis rows go after their notifications have: the outbox rows'
    -- USING join must still see the event they belong to.
    WITH removed_crisis AS (
        DELETE FROM guardian_crisis_events
        WHERE minor_user_id = target_subject_id
        RETURNING 1
    )
    SELECT deleted || jsonb_build_object(
        'crisis_events', (SELECT count(*) FROM removed_crisis)
    ) INTO deleted;
    RETURN deleted;
END
$maintenance$;

CREATE OR REPLACE FUNCTION guardian_subject_scope_remaining(
    target_account_id TEXT,
    target_subject_id TEXT
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $maintenance$
DECLARE
    result JSONB;
BEGIN
    IF COALESCE(btrim(target_account_id), '') = ''
       OR COALESCE(btrim(target_subject_id), '') = ''
       OR target_account_id = target_subject_id THEN
        RAISE EXCEPTION 'subject scope requires a distinct owner account and subject';
    END IF;
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    PERFORM set_config('memoria.guardian_maintenance_scope', '1', true);
    SELECT jsonb_build_object(
        'crisis_events', (
            SELECT count(*) FROM guardian_crisis_events
            WHERE minor_user_id = target_subject_id
        ),
        'guardian_notifications', (
            SELECT count(*) FROM guardian_notification_outbox outbox
            JOIN guardian_crisis_events crisis
              ON crisis.crisis_event_id = outbox.crisis_event_id
            WHERE crisis.minor_user_id = target_subject_id
        ),
        'tutor_practice_sessions', (
            SELECT count(*) FROM tutor_practice_sessions
            WHERE subject_id = target_subject_id
              AND (account_id = target_account_id OR actor_id = target_account_id)
        ),
        'tutor_study_progress', (
            SELECT count(*) FROM tutor_study_progress
            WHERE subject_id = target_subject_id
              AND (account_id = target_account_id OR actor_id = target_account_id)
        ),
        'tutor_practice_evidence', (
            SELECT count(*) FROM tutor_practice_evidence
            WHERE subject_id = target_subject_id AND actor_id = target_account_id
        ),
        'tutor_commit_outbox', (
            SELECT count(*) FROM tutor_commit_outbox
            WHERE subject_id = target_subject_id AND actor_id = target_account_id
        ),
        -- Purged by the corpus retention service (object first, then the
        -- row is marked deleted); only live samples count as remaining.
        'corpus_samples', (
            SELECT count(*) FROM guardian_corpus_samples
            WHERE minor_user_id = target_subject_id AND deleted_at IS NULL
        )
    ) INTO result;
    RETURN result;
END
$maintenance$;

-- Outbox worker entry points: SECURITY DEFINER under the maintenance role.
-- Claim leases rows with an expiry so a crashed worker's claim is reclaimed;
-- SKIP LOCKED keeps concurrent workers from double-delivering.  The API
-- role can only CALL these functions; it can never open account scope.
CREATE OR REPLACE FUNCTION guardian_tutor_outbox_claim(
    p_worker_id TEXT,
    p_subject_id TEXT,
    p_limit INTEGER,
    p_lease_seconds INTEGER
) RETURNS TABLE (
    event_id TEXT,
    kind TEXT,
    subject_id TEXT,
    actor_id TEXT,
    archive_payload_json JSONB,
    created_at TIMESTAMPTZ
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $outbox_worker$
#variable_conflict use_column
BEGIN
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    RETURN QUERY
    UPDATE tutor_commit_outbox AS o
    SET status = 'claimed',
        claimed_by = p_worker_id,
        attempt_count = attempt_count + 1,
        claimed_at = now(),
        lease_until = now() + make_interval(secs => p_lease_seconds)
    WHERE o.event_id IN (
        SELECT event_id FROM tutor_commit_outbox
        WHERE (status = 'pending'
               OR (status = 'claimed' AND lease_until < now()))
          AND (p_subject_id IS NULL OR subject_id = p_subject_id)
        ORDER BY created_at, event_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    )
    RETURNING
        o.event_id,
        o.kind,
        o.subject_id,
        o.actor_id,
        o.archive_payload_json,
        o.created_at;
END
$outbox_worker$;

CREATE OR REPLACE FUNCTION guardian_tutor_outbox_mark_delivered(
    p_event_id TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $outbox_worker$
#variable_conflict use_column
BEGIN
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    UPDATE tutor_commit_outbox
    SET status = 'delivered', lease_until = NULL
    WHERE event_id = p_event_id AND status = 'claimed';
END
$outbox_worker$;

CREATE OR REPLACE FUNCTION guardian_tutor_outbox_release(
    p_event_id TEXT
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $outbox_worker$
#variable_conflict use_column
BEGIN
    PERFORM set_config('memoria.allow_account_scope', '1', true);
    UPDATE tutor_commit_outbox
    SET status = 'pending', claimed_by = NULL,
        claimed_at = NULL, lease_until = NULL
    WHERE event_id = p_event_id AND status = 'claimed';
END
$outbox_worker$;

-- This predicate is deliberately invoker-security: it is called from the
-- outbox policy while the caller is still subject-scoped, so the linked event
-- and relationship remain subject to the same RLS boundary.
-- The recipient of a crisis notification is either an activated guardian link
-- or a declared guardianship: an account-less subject can never confirm a
-- link, and the declaration is the only basis its guardian has.  The declared
-- branch is delegated to a maintenance-owned SECURITY DEFINER wrapper so the
-- Identity authority stays a pure boolean and no table visibility leaks.
CREATE OR REPLACE FUNCTION guardian_relationship_declared(
    p_guardian_user_id TEXT,
    p_minor_user_id TEXT,
    p_at TIMESTAMPTZ
) RETURNS BOOLEAN
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public SET row_security = on
AS $guardian_declared$
BEGIN
    -- plpgsql so the Guardian schema can be installed before Identity; the
    -- name is resolved at execution, and a missing authority means there is no
    -- declaration to honour (fail closed, never fail open).
    --
    -- Binding-scoped on purpose (P0-04): every read/enqueue branch that
    -- treats someone as a declared guardian goes through this one predicate,
    -- so a third-party self-declaration without a binding can never read the
    -- subject's crisis evidence or receive its notification.
    IF to_regprocedure(
        'public.identity_relationship_declared_for_binding(text,text,timestamptz)'
    ) IS NULL THEN
        RETURN FALSE;
    END IF;
    RETURN identity_relationship_declared_for_binding(
        p_guardian_user_id, p_minor_user_id, p_at
    );
END
$guardian_declared$;

CREATE OR REPLACE FUNCTION guardian_notification_subject_allowed(
    p_crisis_event_id UUID,
    p_guardian_user_id TEXT,
    p_subject_id TEXT
) RETURNS BOOLEAN
LANGUAGE sql STABLE SET search_path = public
AS $notification_scope$
    SELECT EXISTS (
        SELECT 1
        FROM guardian_crisis_events crisis
        WHERE crisis.crisis_event_id = p_crisis_event_id
          AND (
              crisis.minor_user_id = p_subject_id
              OR p_subject_id = p_guardian_user_id
          )
          AND (
              EXISTS (
                  SELECT 1
                  FROM guardian_links link
                  WHERE link.guardian_user_id = p_guardian_user_id
                    AND link.minor_user_id = crisis.minor_user_id
                    AND link.status = 'active'
              )
              OR guardian_relationship_declared(
                  p_guardian_user_id,
                  crisis.minor_user_id,
                  crisis.occurred_at
              )
          )
    )
$notification_scope$;

-- Notification rows are emitted only by the crisis transaction.  The API
-- role calls this narrow function; it cannot INSERT into the outbox table
-- directly.  The function is owned by the maintenance role and validates the
-- event/active-link pair before opening its short, transaction-local scope.
CREATE OR REPLACE FUNCTION guardian_enqueue_notification(
    p_notification_id UUID,
    p_crisis_event_id UUID,
    p_guardian_user_id TEXT,
    p_created_at TIMESTAMPTZ
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $notification_enqueue$
BEGIN
    PERFORM set_config('memoria.guardian_maintenance_scope', '1', true);
    IF NOT EXISTS (
        SELECT 1
        FROM guardian_crisis_events crisis
        JOIN guardian_links link ON link.minor_user_id = crisis.minor_user_id
        WHERE crisis.crisis_event_id = p_crisis_event_id
          AND link.guardian_user_id = p_guardian_user_id
          AND link.status = 'active'
    ) THEN
        RAISE EXCEPTION 'guardian notification target is not an active link';
    END IF;
    INSERT INTO guardian_notification_outbox(
        notification_id, crisis_event_id, guardian_user_id,
        channel, status, attempts, created_at
    ) VALUES (
        p_notification_id, p_crisis_event_id, p_guardian_user_id,
        'wechat_subscription', 'pending', 0, p_created_at
    ) ON CONFLICT(crisis_event_id, guardian_user_id) DO NOTHING;
    PERFORM set_config('memoria.guardian_maintenance_scope', '0', true);
END
$notification_enqueue$;

-- A subject created by an adult device binding has no account, so no guardian
-- link can ever be confirmed for it.  The only authorized recipient is the
-- guardian whose ``guardian_of`` relationship to the subject is a one-sided
-- declaration (the guardian confirmed; the subject has no endpoint to
-- confirm).  The declaration is verified here against the Identity authority,
-- so this function never becomes a generic "enqueue for any id" primitive,
-- and it is deliberately separate from the active-link function so a
-- declaration can never be mistaken for verified guardianship.
CREATE OR REPLACE FUNCTION guardian_enqueue_declared_notification(
    p_notification_id UUID,
    p_crisis_event_id UUID,
    p_guardian_user_id TEXT,
    p_created_at TIMESTAMPTZ
) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $notification_enqueue_declared$
DECLARE
    v_minor_user_id TEXT;
BEGIN
    PERFORM set_config('memoria.guardian_maintenance_scope', '1', true);
    -- A declared guardian has no active link, so the ordinary actor/subject
    -- branch of the outbox policy can never pass for this row.  This dedicated
    -- flag is set only here, inside a SECURITY DEFINER function whose owner is
    -- the maintenance role; the policy's current_user check keeps an API-role
    -- caller from forging it.
    PERFORM set_config('memoria.guardian_declared_notification_scope', '1', true);
    SELECT crisis.minor_user_id INTO v_minor_user_id
    FROM guardian_crisis_events crisis
    WHERE crisis.crisis_event_id = p_crisis_event_id;
    IF v_minor_user_id IS NULL THEN
        RAISE EXCEPTION 'guardian notification target has no crisis event';
    END IF;
    -- P0-04 source constraint: the declaration must be binding-scoped.  The
    -- declarant has to own an ACTIVE binding naming this subject as primary
    -- subject, so a third party that merely learned the subject's person id
    -- and self-accepted a ``guardian_of`` invite can never become a recipient.
    -- The predicate accepts a pending guardian declaration, the guardian
    -- attested active since 2026-09-25 (a pending-only check here refused
    -- every binding made since, found 2026-09-26) and an elder's attested
    -- delegate (P0-04 D7).  A missing authority fails closed.
    IF to_regprocedure(
        'public.identity_relationship_declared_for_binding(text,text,timestamptz)'
    ) IS NULL
    OR NOT identity_relationship_declared_for_binding(
        p_guardian_user_id, v_minor_user_id, p_created_at
    ) THEN
        RAISE EXCEPTION
            'guardian notification target is not a declared guardian: no binding-scoped declaration';
    END IF;
    INSERT INTO guardian_notification_outbox(
        notification_id, crisis_event_id, guardian_user_id,
        channel, status, attempts, created_at
    ) VALUES (
        p_notification_id, p_crisis_event_id, p_guardian_user_id,
        'wechat_subscription', 'pending', 0, p_created_at
    ) ON CONFLICT(crisis_event_id, guardian_user_id) DO NOTHING;
    PERFORM set_config('memoria.guardian_declared_notification_scope', '0', true);
    PERFORM set_config('memoria.guardian_maintenance_scope', '0', true);
END
$notification_enqueue_declared$;

-- Crisis push delivery.  These SECURITY DEFINER functions are owned by the
-- maintenance role and executable ONLY by the worker role; the API role can
-- neither claim a notification nor read a stored openid.  Each opens the
-- maintenance scope (to read the event) and the dedicated delivery scope
-- (the only UPDATE path on the outbox) for its own transaction.
CREATE OR REPLACE FUNCTION guardian_crisis_push_claim(
    p_worker_id TEXT,
    p_now TIMESTAMPTZ,
    p_limit INTEGER,
    p_lease_seconds INTEGER,
    p_max_attempts INTEGER,
    p_max_age_seconds INTEGER
) RETURNS TABLE (
    notification_id UUID,
    crisis_event_id UUID,
    guardian_user_id TEXT,
    minor_user_id TEXT,
    occurred_at TIMESTAMPTZ,
    attempts INTEGER
)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $crisis_push_claim$
#variable_conflict use_column
BEGIN
    IF p_worker_id IS NULL OR char_length(p_worker_id) NOT BETWEEN 1 AND 128
       OR p_now IS NULL
       OR p_limit NOT BETWEEN 1 AND 100
       OR p_lease_seconds NOT BETWEEN 5 AND 600
       OR p_max_attempts NOT BETWEEN 1 AND 10
       OR p_max_age_seconds NOT BETWEEN 60 AND 604800 THEN
        RAISE EXCEPTION 'crisis push claim arguments are invalid';
    END IF;
    PERFORM set_config('memoria.guardian_maintenance_scope', '1', true);
    PERFORM set_config('memoria.guardian_push_delivery_scope', '1', true);
    UPDATE guardian_notification_outbox AS stale
    SET status = 'failed', last_error_code = 'attempts_exhausted',
        claimed_by = NULL, lease_until = NULL, next_attempt_at = NULL
    WHERE stale.status = 'pending'
      AND stale.attempts >= p_max_attempts
      AND (stale.lease_until IS NULL OR stale.lease_until < p_now);
    RETURN QUERY
    WITH claimed AS (
        UPDATE guardian_notification_outbox AS o
        SET claimed_by = p_worker_id,
            attempts = o.attempts + 1,
            lease_until = p_now + make_interval(secs => p_lease_seconds)
        WHERE o.notification_id IN (
            SELECT candidate.notification_id
            FROM guardian_notification_outbox candidate
            WHERE candidate.status = 'pending'
              AND candidate.channel = 'wechat_subscription'
              AND candidate.attempts < p_max_attempts
              AND candidate.created_at >= p_now - make_interval(secs => p_max_age_seconds)
              AND (candidate.next_attempt_at IS NULL OR candidate.next_attempt_at <= p_now)
              AND (candidate.lease_until IS NULL OR candidate.lease_until < p_now)
            ORDER BY candidate.created_at, candidate.notification_id
            FOR UPDATE SKIP LOCKED
            LIMIT p_limit
        )
        RETURNING o.notification_id, o.crisis_event_id, o.guardian_user_id,
                  o.attempts, o.created_at
    )
    SELECT claimed.notification_id, claimed.crisis_event_id,
           claimed.guardian_user_id, crisis.minor_user_id,
           crisis.occurred_at, claimed.attempts
    FROM claimed
    JOIN guardian_crisis_events crisis
      ON crisis.crisis_event_id = claimed.crisis_event_id
    ORDER BY claimed.created_at, claimed.notification_id;
END
$crisis_push_claim$;

CREATE OR REPLACE FUNCTION guardian_crisis_push_reserve(
    p_notification_id UUID,
    p_worker_id TEXT,
    p_template_id TEXT,
    p_now TIMESTAMPTZ
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $crisis_push_reserve$
DECLARE
    v_guardian_user_id TEXT;
    v_reserved_template_id TEXT;
    v_openid TEXT;
BEGIN
    IF p_template_id IS NULL OR p_template_id !~ '^[A-Za-z0-9_-]{1,128}$' THEN
        RAISE EXCEPTION 'crisis push template id is invalid';
    END IF;
    PERFORM set_config('memoria.guardian_maintenance_scope', '1', true);
    PERFORM set_config('memoria.guardian_push_delivery_scope', '1', true);
    SELECT outbox.guardian_user_id, outbox.reserved_template_id
    INTO v_guardian_user_id, v_reserved_template_id
    FROM guardian_notification_outbox outbox
    WHERE outbox.notification_id = p_notification_id
      AND outbox.status = 'pending'
      AND outbox.claimed_by = p_worker_id
      AND outbox.lease_until >= p_now
    FOR UPDATE;
    IF v_guardian_user_id IS NULL THEN
        RAISE EXCEPTION 'crisis push claim is not held';
    END IF;
    IF v_reserved_template_id = p_template_id THEN
        SELECT subscription.openid INTO v_openid
        FROM guardian_push_subscriptions subscription
        WHERE subscription.guardian_user_id = v_guardian_user_id
          AND subscription.template_id = p_template_id;
        RETURN v_openid;
    END IF;
    UPDATE guardian_push_subscriptions subscription
    SET remaining = subscription.remaining - 1, updated_at = p_now
    WHERE subscription.guardian_user_id = v_guardian_user_id
      AND subscription.template_id = p_template_id
      AND subscription.remaining > 0
      AND subscription.openid IS NOT NULL
    RETURNING subscription.openid INTO v_openid;
    IF v_openid IS NULL THEN
        RETURN NULL;
    END IF;
    UPDATE guardian_notification_outbox
    SET reserved_template_id = p_template_id
    WHERE notification_id = p_notification_id;
    RETURN v_openid;
END
$crisis_push_reserve$;

CREATE OR REPLACE FUNCTION guardian_crisis_push_complete(
    p_notification_id UUID,
    p_worker_id TEXT,
    p_outcome TEXT,
    p_error_code TEXT,
    p_retry_delay_seconds INTEGER,
    p_exhaust_subscription BOOLEAN,
    p_now TIMESTAMPTZ
) RETURNS BOOLEAN
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public
AS $crisis_push_complete$
DECLARE
    v_guardian_user_id TEXT;
    v_reserved_template_id TEXT;
BEGIN
    IF p_outcome IS NULL
       OR p_outcome NOT IN ('delivered', 'no_subscription', 'failed', 'retry')
       OR (p_error_code IS NOT NULL AND char_length(p_error_code) NOT BETWEEN 1 AND 96)
       OR (p_outcome = 'retry' AND (
            p_retry_delay_seconds IS NULL
            OR p_retry_delay_seconds NOT BETWEEN 1 AND 3600
       ))
       OR p_now IS NULL THEN
        RAISE EXCEPTION 'crisis push completion arguments are invalid';
    END IF;
    PERFORM set_config('memoria.guardian_maintenance_scope', '1', true);
    PERFORM set_config('memoria.guardian_push_delivery_scope', '1', true);
    SELECT outbox.guardian_user_id, outbox.reserved_template_id
    INTO v_guardian_user_id, v_reserved_template_id
    FROM guardian_notification_outbox outbox
    WHERE outbox.notification_id = p_notification_id
      AND outbox.status = 'pending'
      AND outbox.claimed_by = p_worker_id
    FOR UPDATE;
    IF v_guardian_user_id IS NULL THEN
        RETURN FALSE;
    END IF;
    IF v_reserved_template_id IS NOT NULL
       AND (p_outcome IN ('failed', 'retry') OR COALESCE(p_exhaust_subscription, FALSE)) THEN
        UPDATE guardian_push_subscriptions subscription
        SET remaining = CASE
                WHEN COALESCE(p_exhaust_subscription, FALSE) THEN 0
                ELSE LEAST(subscription.remaining + 1, 20)
            END,
            updated_at = p_now
        WHERE subscription.guardian_user_id = v_guardian_user_id
          AND subscription.template_id = v_reserved_template_id;
    END IF;
    UPDATE guardian_notification_outbox
    SET status = CASE WHEN p_outcome = 'retry' THEN 'pending' ELSE p_outcome END,
        delivered_at = CASE WHEN p_outcome = 'delivered' THEN p_now ELSE NULL END,
        last_error_code = CASE WHEN p_outcome = 'delivered' THEN NULL ELSE p_error_code END,
        claimed_by = NULL,
        lease_until = NULL,
        reserved_template_id = NULL,
        next_attempt_at = CASE
            WHEN p_outcome = 'retry'
            THEN p_now + make_interval(secs => p_retry_delay_seconds)
            ELSE NULL
        END
    WHERE notification_id = p_notification_id;
    RETURN TRUE;
END
$crisis_push_complete$;

ALTER TABLE guardian_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_person_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_corpus_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_study_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_commit_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_crisis_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_push_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_links FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_consents FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_person_consents FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_corpus_samples FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_study_progress FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_commit_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_crisis_events FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_notification_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_push_subscriptions FORCE ROW LEVEL SECURITY;

DO $guardian_policy$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian') THEN
        GRANT USAGE ON SCHEMA public TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_links TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_consents TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_person_consents TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_corpus_samples TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_practice_sessions TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_study_progress TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_practice_evidence TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_commit_outbox TO memoria_guardian;
        GRANT SELECT, INSERT ON guardian_crisis_events TO memoria_guardian;
        GRANT SELECT ON guardian_notification_outbox TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_push_subscriptions TO memoria_guardian;

        -- The maintenance role is the only role allowed to perform an
        -- account-wide guardian export/deletion or retention sweep.  The API
        -- role never receives a broad-scope escape hatch.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian_maintenance') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE
                ON guardian_links, guardian_consents, guardian_person_consents,
                   guardian_corpus_samples,
                   guardian_crisis_events, guardian_notification_outbox,
                   guardian_push_subscriptions
                TO memoria_guardian_maintenance;
        END IF;

        DROP POLICY IF EXISTS guardian_controller_links ON guardian_links;
        CREATE POLICY guardian_controller_links ON guardian_links
            FOR SELECT TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                    AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                    AND (
                        (
                            guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                            AND (
                                minor_user_id = current_setting('memoria.guardian_subject_id', true)
                                OR current_setting('memoria.guardian_subject_id', true)
                                    = current_setting('memoria.guardian_actor_id', true)
                            )
                        )
                        OR (
                            minor_user_id = current_setting('memoria.guardian_actor_id', true)
                            AND minor_user_id = current_setting('memoria.guardian_subject_id', true)
                        )
                    )
                )
            );

        DROP POLICY IF EXISTS guardian_controller_links_insert ON guardian_links;
        CREATE POLICY guardian_controller_links_insert ON guardian_links
            FOR INSERT TO memoria_guardian
            WITH CHECK (
                COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                AND guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                AND minor_user_id = current_setting('memoria.guardian_subject_id', true)
            );

        DROP POLICY IF EXISTS guardian_controller_links_update ON guardian_links;
        CREATE POLICY guardian_controller_links_update ON guardian_links
            FOR UPDATE TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND (
                        minor_user_id = current_setting('memoria.guardian_subject_id', true)
                        OR current_setting('memoria.guardian_subject_id', true)
                            = current_setting('memoria.guardian_actor_id', true)
                    )
                )
                OR (
                    minor_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND minor_user_id = current_setting('memoria.guardian_subject_id', true)
                )
            )
            WITH CHECK (
                (
                    guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND minor_user_id = current_setting('memoria.guardian_subject_id', true)
                )
                OR (
                    minor_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND minor_user_id = current_setting('memoria.guardian_subject_id', true)
                )
                OR (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
            );

        DROP POLICY IF EXISTS guardian_controller_links_delete ON guardian_links;
        CREATE POLICY guardian_controller_links_delete ON guardian_links
            FOR DELETE TO memoria_guardian_maintenance
            USING (
                current_user = 'memoria_guardian_maintenance'
                AND COALESCE(
                    current_setting('memoria.guardian_maintenance_scope', true),
                    ''
                ) = '1'
            );

        DROP POLICY IF EXISTS guardian_controller_consents ON guardian_consents;
        CREATE POLICY guardian_controller_consents ON guardian_consents
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                    AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                    AND EXISTS (
                        SELECT 1
                        FROM guardian_links link
                        WHERE link.link_id = guardian_consents.link_id
                          AND (
                              (
                                  link.guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                                  AND (
                                      link.minor_user_id = current_setting('memoria.guardian_subject_id', true)
                                      OR current_setting('memoria.guardian_subject_id', true)
                                          = current_setting('memoria.guardian_actor_id', true)
                                  )
                              )
                              OR (
                                  link.minor_user_id = current_setting('memoria.guardian_actor_id', true)
                                  AND link.minor_user_id = current_setting('memoria.guardian_subject_id', true)
                              )
                          )
                    )
                )
            )
            WITH CHECK (
                COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                AND EXISTS (
                    SELECT 1
                    FROM guardian_links link
                    WHERE link.link_id = guardian_consents.link_id
                      AND link.guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                      AND link.minor_user_id = current_setting('memoria.guardian_subject_id', true)
                )
            );

        -- Person-scoped consents carry no link, so visibility is defined by the
        -- two person keys instead: the subject may always read its own rows
        -- (that is the policy gate at turn time), and the grantor may read and
        -- write the rows it granted.  Write requires actor == grantor kept
        -- separate from subject, so a subject can never grant consent to itself
        -- through this table.
        DROP POLICY IF EXISTS guardian_controller_person_consents
            ON guardian_person_consents;
        CREATE POLICY guardian_controller_person_consents
            ON guardian_person_consents
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                    AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                    AND subject_person_id = current_setting('memoria.guardian_subject_id', true)
                    AND (
                        subject_person_id = current_setting('memoria.guardian_actor_id', true)
                        OR grantor_person_id = current_setting('memoria.guardian_actor_id', true)
                    )
                )
            )
            WITH CHECK (
                COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                AND subject_person_id = current_setting('memoria.guardian_subject_id', true)
                AND grantor_person_id = current_setting('memoria.guardian_actor_id', true)
            );

        DROP POLICY IF EXISTS guardian_controller_corpus_samples ON guardian_corpus_samples;
        CREATE POLICY guardian_controller_corpus_samples ON guardian_corpus_samples
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    minor_user_id = current_setting('memoria.guardian_subject_id', true)
                    AND (
                        minor_user_id = current_setting('memoria.guardian_actor_id', true)
                        OR EXISTS (
                            SELECT 1
                            FROM guardian_consents consent
                            JOIN guardian_links link ON link.link_id = consent.link_id
                            WHERE consent.consent_id = guardian_corpus_samples.consent_id
                              AND link.minor_user_id = current_setting('memoria.guardian_subject_id', true)
                              AND link.guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                        )
                    )
                )
            )
            WITH CHECK (
                COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                AND minor_user_id = current_setting('memoria.guardian_subject_id', true)
                AND (
                    minor_user_id = current_setting('memoria.guardian_actor_id', true)
                    OR EXISTS (
                        SELECT 1
                        FROM guardian_consents consent
                        JOIN guardian_links link ON link.link_id = consent.link_id
                        WHERE consent.consent_id = guardian_corpus_samples.consent_id
                          AND link.minor_user_id = current_setting('memoria.guardian_subject_id', true)
                          AND link.guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                    )
                )
            );

        -- PR-13: subject-scoped rows, fail closed.  A session-local
        -- memoria.subject_id shows only that subject's rows.  The broad
        -- account scope is ONLY honored when current_user IS the
        -- memoria_guardian_maintenance role (SECURITY DEFINER maintenance
        -- functions run as it), so an API role setting the flag itself
        -- still sees nothing.  With neither context set, nothing is visible
        -- or writable.  Legacy rows without a subject_id are quarantined:
        -- never visible under a subject scope.
        DROP POLICY IF EXISTS guardian_controller_tutor_sessions ON tutor_practice_sessions;
        CREATE POLICY guardian_controller_tutor_sessions ON tutor_practice_sessions
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            )
            WITH CHECK (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            );

        DROP POLICY IF EXISTS guardian_controller_tutor_progress ON tutor_study_progress;
        CREATE POLICY guardian_controller_tutor_progress ON tutor_study_progress
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            )
            WITH CHECK (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            );

        DROP POLICY IF EXISTS guardian_controller_tutor_evidence ON tutor_practice_evidence;
        CREATE POLICY guardian_controller_tutor_evidence ON tutor_practice_evidence
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            )
            WITH CHECK (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            );

        DROP POLICY IF EXISTS guardian_controller_tutor_outbox ON tutor_commit_outbox;
        CREATE POLICY guardian_controller_tutor_outbox ON tutor_commit_outbox
            TO memoria_guardian, memoria_guardian_maintenance, memoria_guardian_worker
            USING (
                (
                    current_user IN (
                        'memoria_guardian_maintenance',
                        'memoria_guardian_worker'
                    )
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            )
            WITH CHECK (
                (
                    current_user IN (
                        'memoria_guardian_maintenance',
                        'memoria_guardian_worker'
                    )
                    AND COALESCE(
                        current_setting('memoria.allow_account_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.subject_id', true), '') <> ''
                    AND subject_id = current_setting('memoria.subject_id', true)
                )
            );

        DROP POLICY IF EXISTS guardian_controller_crisis_events ON guardian_crisis_events;
        CREATE POLICY guardian_controller_crisis_events ON guardian_crisis_events
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    minor_user_id = current_setting('memoria.guardian_subject_id', true)
                    AND (
                        minor_user_id = current_setting('memoria.guardian_actor_id', true)
                        OR EXISTS (
                            SELECT 1 FROM guardian_links link
                            WHERE link.guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                              AND link.minor_user_id = guardian_crisis_events.minor_user_id
                              AND link.status = 'active'
                        )
                    )
                )
                OR (
                    current_setting('memoria.guardian_actor_id', true)
                        = current_setting('memoria.guardian_subject_id', true)
                    AND (
                        EXISTS (
                            SELECT 1 FROM guardian_links link
                            WHERE link.guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                              AND link.minor_user_id = guardian_crisis_events.minor_user_id
                              AND link.status = 'active'
                        )
                        -- The declared guardian reads the crisis evidence of the
                        -- subject they declared responsibility for; without this
                        -- the notification row is stranded behind the event it
                        -- belongs to.
                        OR guardian_relationship_declared(
                            current_setting('memoria.guardian_actor_id', true),
                            guardian_crisis_events.minor_user_id,
                            guardian_crisis_events.occurred_at
                        )
                    )
                )
            )
            WITH CHECK (
                COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                AND minor_user_id = current_setting('memoria.guardian_subject_id', true)
                AND minor_user_id = current_setting('memoria.guardian_actor_id', true)
            );

        DROP POLICY IF EXISTS guardian_controller_notification_outbox
            ON guardian_notification_outbox;
        CREATE POLICY guardian_controller_notification_outbox ON guardian_notification_outbox
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND guardian_notification_subject_allowed(
                        crisis_event_id,
                        guardian_user_id,
                        current_setting('memoria.guardian_subject_id', true)
                    )
                )
                OR (
                    current_setting('memoria.guardian_actor_id', true)
                        = current_setting('memoria.guardian_subject_id', true)
                    AND guardian_notification_subject_allowed(
                        crisis_event_id,
                        guardian_user_id,
                        current_setting('memoria.guardian_subject_id', true)
                    )
                )
            )
            WITH CHECK (
                (
                    COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                    AND COALESCE(current_setting('memoria.guardian_subject_id', true), '') <> ''
                    AND guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND guardian_notification_subject_allowed(
                        crisis_event_id,
                        guardian_user_id,
                        current_setting('memoria.guardian_subject_id', true)
                    )
                )
                OR (
                    current_setting('memoria.guardian_actor_id', true)
                        = current_setting('memoria.guardian_subject_id', true)
                    AND guardian_notification_subject_allowed(
                        crisis_event_id,
                        guardian_user_id,
                        current_setting('memoria.guardian_subject_id', true)
                    )
                )
                -- A declared guardian has no active link, so every branch above
                -- is unreachable for that recipient.  Only the declared-guardian
                -- enqueue port sets this flag, and only inside the maintenance
                -- role's own SECURITY DEFINER transaction after validating the
                -- declaration against the Identity authority.
                OR (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting(
                            'memoria.guardian_declared_notification_scope', true
                        ),
                        ''
                    ) = '1'
                )
            );

        -- The delivery functions are the only UPDATE path on the outbox for the
        -- maintenance role: the ordinary policy's WITH CHECK never admits a
        -- maintenance write, and this scope flag is set only inside the
        -- maintenance-owned delivery functions (current_user check below).
        DROP POLICY IF EXISTS guardian_crisis_push_delivery
            ON guardian_notification_outbox;
        CREATE POLICY guardian_crisis_push_delivery ON guardian_notification_outbox
            FOR UPDATE TO memoria_guardian_maintenance
            USING (
                current_user = 'memoria_guardian_maintenance'
                AND COALESCE(
                    current_setting('memoria.guardian_push_delivery_scope', true),
                    ''
                ) = '1'
            )
            WITH CHECK (
                current_user = 'memoria_guardian_maintenance'
                AND COALESCE(
                    current_setting('memoria.guardian_push_delivery_scope', true),
                    ''
                ) = '1'
            );

        -- A guardian reads and writes only its own subscribe-message ledger,
        -- in its own actor == subject context.  The maintenance scope serves
        -- delivery settlement and account export/deletion.
        DROP POLICY IF EXISTS guardian_controller_push_subscriptions
            ON guardian_push_subscriptions;
        CREATE POLICY guardian_controller_push_subscriptions ON guardian_push_subscriptions
            TO memoria_guardian, memoria_guardian_maintenance
            USING (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                    AND guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND guardian_user_id = current_setting('memoria.guardian_subject_id', true)
                )
            )
            WITH CHECK (
                (
                    current_user = 'memoria_guardian_maintenance'
                    AND COALESCE(
                        current_setting('memoria.guardian_maintenance_scope', true),
                        ''
                    ) = '1'
                )
                OR (
                    COALESCE(current_setting('memoria.guardian_actor_id', true), '') <> ''
                    AND guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                    AND guardian_user_id = current_setting('memoria.guardian_subject_id', true)
                )
            );

        -- Account governance and outbox delivery run through dedicated
        -- LOGIN/NOBYPASSRLS roles.  The API role (memoria_guardian) receives
        -- NO EXECUTE grant on any of these SECURITY DEFINER functions, so it
        -- can never open the broad account scope or impersonate a worker.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian_maintenance') THEN
            GRANT USAGE ON SCHEMA public TO memoria_guardian_maintenance;
            GRANT SELECT, INSERT, UPDATE, DELETE
                ON tutor_practice_sessions, tutor_study_progress
                TO memoria_guardian_maintenance;
            -- The account-scope functions only read and physically delete
            -- these rows; no maintenance port writes them.
            GRANT SELECT, DELETE
                ON tutor_practice_evidence, tutor_commit_outbox
                TO memoria_guardian_maintenance;
            REVOKE EXECUTE ON FUNCTION guardian_relationship_declared(TEXT, TEXT, TIMESTAMPTZ)
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_enqueue_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION guardian_relationship_declared(TEXT, TEXT, TIMESTAMPTZ)
                TO memoria_guardian;
            ALTER FUNCTION guardian_relationship_declared(TEXT, TEXT, TIMESTAMPTZ)
                OWNER TO memoria_guardian_maintenance;
            GRANT EXECUTE ON FUNCTION guardian_enqueue_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                TO memoria_guardian;
            ALTER FUNCTION guardian_enqueue_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                OWNER TO memoria_guardian_maintenance;
            REVOKE EXECUTE ON FUNCTION guardian_enqueue_declared_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION guardian_enqueue_declared_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                TO memoria_guardian;
            ALTER FUNCTION guardian_enqueue_declared_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                OWNER TO memoria_guardian_maintenance;
            REVOKE EXECUTE ON FUNCTION guardian_crisis_push_claim(
                TEXT, TIMESTAMPTZ, INTEGER, INTEGER, INTEGER, INTEGER
            ) FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_crisis_push_reserve(
                UUID, TEXT, TEXT, TIMESTAMPTZ
            ) FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_crisis_push_complete(
                UUID, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, TIMESTAMPTZ
            ) FROM PUBLIC;
            ALTER FUNCTION guardian_crisis_push_claim(
                TEXT, TIMESTAMPTZ, INTEGER, INTEGER, INTEGER, INTEGER
            ) OWNER TO memoria_guardian_maintenance;
            ALTER FUNCTION guardian_crisis_push_reserve(
                UUID, TEXT, TEXT, TIMESTAMPTZ
            ) OWNER TO memoria_guardian_maintenance;
            ALTER FUNCTION guardian_crisis_push_complete(
                UUID, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, TIMESTAMPTZ
            ) OWNER TO memoria_guardian_maintenance;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian_worker') THEN
                GRANT EXECUTE ON FUNCTION guardian_crisis_push_claim(
                    TEXT, TIMESTAMPTZ, INTEGER, INTEGER, INTEGER, INTEGER
                ) TO memoria_guardian_worker;
                GRANT EXECUTE ON FUNCTION guardian_crisis_push_reserve(
                    UUID, TEXT, TEXT, TIMESTAMPTZ
                ) TO memoria_guardian_worker;
                GRANT EXECUTE ON FUNCTION guardian_crisis_push_complete(
                    UUID, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, TIMESTAMPTZ
                ) TO memoria_guardian_worker;
            END IF;
            REVOKE EXECUTE ON FUNCTION guardian_tutor_account_scope_export(TEXT)
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_tutor_account_scope_delete(TEXT)
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_tutor_account_scope_remaining(TEXT)
                FROM PUBLIC;
            ALTER FUNCTION guardian_tutor_account_scope_export(TEXT)
                OWNER TO memoria_guardian_maintenance;
            ALTER FUNCTION guardian_tutor_account_scope_delete(TEXT)
                OWNER TO memoria_guardian_maintenance;
            ALTER FUNCTION guardian_tutor_account_scope_remaining(TEXT)
                OWNER TO memoria_guardian_maintenance;
            REVOKE EXECUTE ON FUNCTION guardian_subject_scope_tutor_event_ids(TEXT, TEXT)
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_subject_scope_delete(TEXT, TEXT)
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_subject_scope_remaining(TEXT, TEXT)
                FROM PUBLIC;
            ALTER FUNCTION guardian_subject_scope_tutor_event_ids(TEXT, TEXT)
                OWNER TO memoria_guardian_maintenance;
            ALTER FUNCTION guardian_subject_scope_delete(TEXT, TEXT)
                OWNER TO memoria_guardian_maintenance;
            ALTER FUNCTION guardian_subject_scope_remaining(TEXT, TEXT)
                OWNER TO memoria_guardian_maintenance;
        END IF;
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian_worker') THEN
            GRANT USAGE ON SCHEMA public TO memoria_guardian_worker;
            GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_commit_outbox
                TO memoria_guardian_worker;
            REVOKE EXECUTE ON FUNCTION guardian_tutor_outbox_claim(TEXT, TEXT, INTEGER, INTEGER)
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_tutor_outbox_mark_delivered(TEXT)
                FROM PUBLIC;
            REVOKE EXECUTE ON FUNCTION guardian_tutor_outbox_release(TEXT)
                FROM PUBLIC;
            ALTER FUNCTION guardian_tutor_outbox_claim(TEXT, TEXT, INTEGER, INTEGER)
                OWNER TO memoria_guardian_worker;
            ALTER FUNCTION guardian_tutor_outbox_mark_delivered(TEXT)
                OWNER TO memoria_guardian_worker;
            ALTER FUNCTION guardian_tutor_outbox_release(TEXT)
                OWNER TO memoria_guardian_worker;
        END IF;
    END IF;
END
$guardian_policy$;

-- The declared-guardian notification port validates its recipient against the
-- Identity authority.  The function is a pure boolean and grants no table
-- visibility.  Each schema grants for the order it can observe: Guardian grants
-- here when Identity is already installed, and the Identity schema grants to
-- this owner when Guardian was installed first.  Without both, the SECURITY
-- DEFINER function raises "permission denied" at runtime instead of failing
-- at install time.
DO $guardian_identity_authority_grant$
BEGIN
    IF to_regprocedure(
        'public.identity_relationship_source_confirmed(text,text,text,timestamptz)'
    ) IS NOT NULL THEN
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION %s TO %I',
            'identity_relationship_source_confirmed(text, text, text, timestamptz)',
            'memoria_guardian_maintenance'
        );
    END IF;
    IF to_regprocedure(
        'public.identity_relationship_declared_for_binding(text,text,timestamptz)'
    ) IS NOT NULL THEN
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION %s TO %I',
            'identity_relationship_declared_for_binding(text, text, timestamptz)',
            'memoria_guardian_maintenance'
        );
    END IF;
END
$guardian_identity_authority_grant$;
