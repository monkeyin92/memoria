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

CREATE TABLE IF NOT EXISTS tutor_study_progress (
    account_id TEXT PRIMARY KEY,
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
        'tutor_study_progress', (
            SELECT to_jsonb(row)
            FROM (
                SELECT * FROM tutor_study_progress
                WHERE account_id = target_account_id
                   OR actor_id = target_account_id
                ORDER BY subject_id IS NULL, subject_id
                LIMIT 1
            ) row
        )
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
    )
    SELECT jsonb_build_object(
        'tutor_practice_sessions', (SELECT count(*) FROM removed_sessions),
        'tutor_study_progress', (SELECT count(*) FROM removed_progress)
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
        JOIN guardian_links link
          ON link.guardian_user_id = p_guardian_user_id
         AND link.minor_user_id = crisis.minor_user_id
         AND link.status = 'active'
        WHERE crisis.crisis_event_id = p_crisis_event_id
          AND (
              crisis.minor_user_id = p_subject_id
              OR p_subject_id = p_guardian_user_id
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

ALTER TABLE guardian_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_corpus_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_study_progress ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE tutor_commit_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_crisis_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_notification_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE guardian_links FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_consents FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_corpus_samples FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_study_progress FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_practice_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE tutor_commit_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_crisis_events FORCE ROW LEVEL SECURITY;
ALTER TABLE guardian_notification_outbox FORCE ROW LEVEL SECURITY;

DO $guardian_policy$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian') THEN
        GRANT USAGE ON SCHEMA public TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_links TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_consents TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE ON guardian_corpus_samples TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_practice_sessions TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_study_progress TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_practice_evidence TO memoria_guardian;
        GRANT SELECT, INSERT, UPDATE, DELETE ON tutor_commit_outbox TO memoria_guardian;
        GRANT SELECT, INSERT ON guardian_crisis_events TO memoria_guardian;
        GRANT SELECT ON guardian_notification_outbox TO memoria_guardian;

        -- The maintenance role is the only role allowed to perform an
        -- account-wide guardian export/deletion or retention sweep.  The API
        -- role never receives a broad-scope escape hatch.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian_maintenance') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE
                ON guardian_links, guardian_consents, guardian_corpus_samples,
                   guardian_crisis_events, guardian_notification_outbox
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
            TO memoria_guardian
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
                    AND EXISTS (
                        SELECT 1 FROM guardian_links link
                        WHERE link.guardian_user_id = current_setting('memoria.guardian_actor_id', true)
                          AND link.minor_user_id = guardian_crisis_events.minor_user_id
                          AND link.status = 'active'
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
            REVOKE EXECUTE ON FUNCTION guardian_enqueue_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION guardian_enqueue_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                TO memoria_guardian;
            ALTER FUNCTION guardian_enqueue_notification(UUID, UUID, TEXT, TIMESTAMPTZ)
                OWNER TO memoria_guardian_maintenance;
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
