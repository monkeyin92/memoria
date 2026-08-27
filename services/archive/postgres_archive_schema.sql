CREATE TABLE IF NOT EXISTS archive_consent_grants (
    consent_grant_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    retention_policy TEXT NOT NULL DEFAULT 'account_lifetime',
    granted_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    evidence_event_id TEXT
);

CREATE TABLE IF NOT EXISTS archive_evidence_events (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    session_id TEXT,
    turn_id BIGINT,
    generation_id BIGINT,
    event_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    speaker_identity_id TEXT,
    speaker_class TEXT NOT NULL CHECK (
        speaker_class IN ('owner', 'guest', 'uncertain', 'assistant', 'system')
    ),
    source TEXT NOT NULL,
    consent_grant_id TEXT REFERENCES archive_consent_grants(consent_grant_id),
    payload JSONB NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    supersedes_event_id TEXT REFERENCES archive_evidence_events(event_id)
);

CREATE INDEX IF NOT EXISTS idx_archive_evidence_account_occurred
ON archive_evidence_events(account_id, occurred_at DESC, event_id DESC);

CREATE INDEX IF NOT EXISTS idx_archive_evidence_payload_gin
ON archive_evidence_events USING GIN(payload);

CREATE TABLE IF NOT EXISTS archive_processing_outbox (
    outbox_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'completed', 'failed', 'dead')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 8 CHECK (max_attempts > 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_until TIMESTAMPTZ,
    fencing_token BIGINT NOT NULL DEFAULT 0 CHECK (fencing_token >= 0),
    worker_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    dead_lettered_at TIMESTAMPTZ,
    replay_count INTEGER NOT NULL DEFAULT 0 CHECK (replay_count >= 0),
    last_error_code TEXT
);

ALTER TABLE archive_processing_outbox
ADD COLUMN IF NOT EXISTS max_attempts INTEGER NOT NULL DEFAULT 8;
ALTER TABLE archive_processing_outbox
ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;
ALTER TABLE archive_processing_outbox
ADD COLUMN IF NOT EXISTS fencing_token BIGINT NOT NULL DEFAULT 0;
ALTER TABLE archive_processing_outbox
ADD COLUMN IF NOT EXISTS worker_id TEXT;
ALTER TABLE archive_processing_outbox
ADD COLUMN IF NOT EXISTS dead_lettered_at TIMESTAMPTZ;
ALTER TABLE archive_processing_outbox
ADD COLUMN IF NOT EXISTS replay_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE archive_processing_outbox
DROP CONSTRAINT IF EXISTS archive_processing_outbox_status_check;
ALTER TABLE archive_processing_outbox
ADD CONSTRAINT archive_processing_outbox_status_check CHECK (
    status IN ('pending', 'processing', 'completed', 'failed', 'dead')
);
ALTER TABLE archive_processing_outbox
DROP CONSTRAINT IF EXISTS archive_processing_outbox_max_attempts_check;
ALTER TABLE archive_processing_outbox
ADD CONSTRAINT archive_processing_outbox_max_attempts_check CHECK (max_attempts > 0);
ALTER TABLE archive_processing_outbox
DROP CONSTRAINT IF EXISTS archive_processing_outbox_fencing_token_check;
ALTER TABLE archive_processing_outbox
ADD CONSTRAINT archive_processing_outbox_fencing_token_check CHECK (fencing_token >= 0);
ALTER TABLE archive_processing_outbox
DROP CONSTRAINT IF EXISTS archive_processing_outbox_replay_count_check;
ALTER TABLE archive_processing_outbox
ADD CONSTRAINT archive_processing_outbox_replay_count_check CHECK (replay_count >= 0);

UPDATE archive_processing_outbox
SET status = 'dead',
    locked_until = NULL,
    worker_id = NULL,
    dead_lettered_at = COALESCE(dead_lettered_at, now())
WHERE status = 'failed' AND attempts >= max_attempts;

CREATE INDEX IF NOT EXISTS idx_archive_outbox_pending
ON archive_processing_outbox(status, available_at, locked_until, outbox_id);

CREATE INDEX IF NOT EXISTS idx_archive_outbox_claimable
ON archive_processing_outbox(
    task_type,
    status,
    available_at,
    locked_until,
    attempts,
    max_attempts,
    outbox_id
);

CREATE TABLE IF NOT EXISTS archive_outbox_replay_audit (
    replay_id UUID PRIMARY KEY,
    outbox_id UUID NOT NULL
        REFERENCES archive_processing_outbox(outbox_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_attempts INTEGER NOT NULL CHECK (previous_attempts >= 0),
    previous_error_code TEXT,
    replayed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_archive_outbox_replay_account
ON archive_outbox_replay_audit(account_id, replayed_at DESC, replay_id);

CREATE TABLE IF NOT EXISTS archive_evidence_blobs (
    blob_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    evidence_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count >= 0),
    content_sha256 CHAR(64) NOT NULL,
    encryption_key_version TEXT NOT NULL,
    retention_policy TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE archive_consent_grants ADD COLUMN IF NOT EXISTS retention_policy TEXT NOT NULL
DEFAULT 'account_lifetime';

WITH ranked_active_consents AS (
    SELECT consent_grant_id,
           ROW_NUMBER() OVER (
               PARTITION BY account_id, purpose
               ORDER BY granted_at DESC, consent_grant_id DESC
           ) AS active_rank
    FROM archive_consent_grants
    WHERE revoked_at IS NULL
)
UPDATE archive_consent_grants consent
SET revoked_at = now()
FROM ranked_active_consents ranked
WHERE consent.consent_grant_id = ranked.consent_grant_id
  AND ranked.active_rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_consent_active
ON archive_consent_grants(account_id, purpose)
WHERE revoked_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_blob_event
ON archive_evidence_blobs(evidence_event_id);

CREATE TABLE IF NOT EXISTS archive_transcript_versions (
    transcript_version_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id BIGINT NOT NULL,
    evidence_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id),
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, session_id, turn_id, evidence_event_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_transcript_current
ON archive_transcript_versions(account_id, session_id, turn_id)
WHERE is_current;

ALTER TABLE archive_consent_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE archive_evidence_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE archive_processing_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE archive_outbox_replay_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE archive_evidence_blobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE archive_transcript_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE archive_consent_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE archive_evidence_events FORCE ROW LEVEL SECURITY;
ALTER TABLE archive_processing_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE archive_outbox_replay_audit FORCE ROW LEVEL SECURITY;
ALTER TABLE archive_evidence_blobs FORCE ROW LEVEL SECURITY;
ALTER TABLE archive_transcript_versions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS archive_consent_account_policy ON archive_consent_grants;
CREATE POLICY archive_consent_account_policy ON archive_consent_grants
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS archive_evidence_account_policy ON archive_evidence_events;
CREATE POLICY archive_evidence_account_policy ON archive_evidence_events
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS archive_outbox_account_policy ON archive_processing_outbox;
CREATE POLICY archive_outbox_account_policy ON archive_processing_outbox
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS archive_outbox_replay_account_policy ON archive_outbox_replay_audit;
CREATE POLICY archive_outbox_replay_account_policy ON archive_outbox_replay_audit
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

-- The background compiler is a NOLOGIN group role with access only to compile
-- tasks in the outbox. It does not bypass RLS on evidence or projections.
DROP POLICY IF EXISTS archive_outbox_compiler_policy ON archive_processing_outbox;
DO $compiler_role$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_archive_compiler') THEN
        EXECUTE format(
            'GRANT USAGE ON SCHEMA %I TO memoria_archive_compiler',
            current_schema()
        );
        GRANT SELECT ON archive_processing_outbox TO memoria_archive_compiler;
        GRANT UPDATE (
            status,
            attempts,
            available_at,
            locked_until,
            fencing_token,
            worker_id,
            completed_at,
            dead_lettered_at,
            last_error_code
        )
            ON archive_processing_outbox TO memoria_archive_compiler;
        CREATE POLICY archive_outbox_compiler_policy ON archive_processing_outbox
            TO memoria_archive_compiler
            USING (task_type = 'compile_evidence')
            WITH CHECK (task_type = 'compile_evidence');
    END IF;
END
$compiler_role$;

DROP POLICY IF EXISTS archive_blob_account_policy ON archive_evidence_blobs;
CREATE POLICY archive_blob_account_policy ON archive_evidence_blobs
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS archive_transcript_account_policy ON archive_transcript_versions;
CREATE POLICY archive_transcript_account_policy ON archive_transcript_versions
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));
