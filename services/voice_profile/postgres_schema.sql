CREATE TABLE IF NOT EXISTS voice_clone_consents (
    account_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    grant_event_id TEXT NOT NULL,
    revoke_event_id TEXT
);

CREATE TABLE IF NOT EXISTS voice_samples (
    sample_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES voice_clone_consents(account_id) ON DELETE CASCADE,
    object_key TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_count BIGINT NOT NULL CHECK (byte_count > 0),
    content_sha256 CHAR(64) NOT NULL,
    encryption_key_version TEXT NOT NULL,
    object_backend TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms > 0),
    sample_rate INTEGER NOT NULL CHECK (sample_rate >= 16000),
    created_at TIMESTAMPTZ NOT NULL,
    deleted_at TIMESTAMPTZ,
    UNIQUE (sample_id, account_id)
);

CREATE TABLE IF NOT EXISTS voice_enrollment_operations (
    operation_id UUID PRIMARY KEY,
    enrollment_key TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES voice_clone_consents(account_id) ON DELETE CASCADE,
    profile_id UUID NOT NULL UNIQUE,
    sample_id UUID NOT NULL UNIQUE,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    provider TEXT NOT NULL,
    provider_region TEXT NOT NULL,
    target_model TEXT NOT NULL,
    provider_prefix TEXT NOT NULL,
    sample_purpose TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'intent', 'upload_submitted', 'sample_uploaded',
            'provider_submitted', 'provider_created', 'completed',
            'reconciliation_required', 'revoked'
        )
    ),
    provider_voice_id TEXT,
    provider_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, enrollment_key),
    UNIQUE (account_id, version_number)
);

CREATE TABLE IF NOT EXISTS voice_profiles (
    profile_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    sample_id UUID NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    provider TEXT NOT NULL,
    provider_region TEXT NOT NULL,
    target_model TEXT NOT NULL,
    provider_voice_id TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('enrolling', 'candidate', 'active', 'failed', 'revoked')
    ),
    evaluation_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (evaluation_status IN ('pending', 'passed', 'failed')),
    quality_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (quality_status IN ('pending', 'passed', 'failed')),
    sample_validation_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (sample_validation_status IN ('pending', 'passed', 'failed')),
    deletion_status TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (deletion_status IN ('not_requested', 'pending', 'completed', 'failed')),
    provider_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    provider_deleted_at TIMESTAMPTZ,
    UNIQUE (account_id, version_number),
    UNIQUE (profile_id, account_id),
    FOREIGN KEY (sample_id, account_id)
        REFERENCES voice_samples(sample_id, account_id) ON DELETE CASCADE
);

ALTER TABLE voice_profiles
ADD COLUMN IF NOT EXISTS quality_status TEXT NOT NULL DEFAULT 'pending';

ALTER TABLE voice_profiles
ADD COLUMN IF NOT EXISTS sample_validation_status TEXT NOT NULL DEFAULT 'pending';

-- NULL means the account's own personal voice, unbound to any custom persona.
-- A custom persona owns its own active clone, so "one active" has to hold per
-- persona rather than per account.
ALTER TABLE voice_profiles
ADD COLUMN IF NOT EXISTS custom_persona_id TEXT;

DROP INDEX IF EXISTS idx_voice_one_active;

-- Two partial indexes, not one: in a unique index NULLs are distinct from each
-- other, so a non-null-only rule would leave an account free to hold any number
-- of unbound active clones.
CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_one_active_owner
ON voice_profiles(account_id) WHERE status = 'active' AND custom_persona_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_voice_one_active_persona
ON voice_profiles(custom_persona_id)
WHERE status = 'active' AND custom_persona_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS voice_blind_trials (
    trial_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    profile_id UUID NOT NULL,
    candidate_slot TEXT NOT NULL CHECK (candidate_slot IN ('A', 'B')),
    preview_text TEXT,
    previewed_a BOOLEAN NOT NULL DEFAULT FALSE,
    previewed_b BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (profile_id, account_id)
        REFERENCES voice_profiles(profile_id, account_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS voice_evaluations (
    evaluation_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    profile_id UUID NOT NULL,
    similarity DOUBLE PRECISION NOT NULL,
    naturalness DOUBLE PRECISION NOT NULL,
    accent_similarity DOUBLE PRECISION NOT NULL
        CHECK (accent_similarity BETWEEN 1 AND 5),
    emotion_adherence DOUBLE PRECISION NOT NULL
        CHECK (emotion_adherence BETWEEN 1 AND 5),
    instruction_adherence DOUBLE PRECISION NOT NULL
        CHECK (instruction_adherence BETWEEN 1 AND 5),
    uncanny DOUBLE PRECISION NOT NULL,
    candidate_preferred BOOLEAN,
    first_audio_ms INTEGER NOT NULL,
    cancel_tail_ms INTEGER NOT NULL,
    timestamp_error_ms INTEGER NOT NULL,
    notes TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (profile_id, account_id)
        REFERENCES voice_profiles(profile_id, account_id) ON DELETE CASCADE
);

ALTER TABLE voice_evaluations
ADD COLUMN IF NOT EXISTS candidate_preferred BOOLEAN;

ALTER TABLE voice_evaluations
ADD COLUMN IF NOT EXISTS accent_similarity DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE voice_evaluations
ADD COLUMN IF NOT EXISTS emotion_adherence DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE voice_evaluations
ADD COLUMN IF NOT EXISTS instruction_adherence DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE voice_evaluations ALTER COLUMN accent_similarity DROP DEFAULT;
ALTER TABLE voice_evaluations ALTER COLUMN emotion_adherence DROP DEFAULT;
ALTER TABLE voice_evaluations ALTER COLUMN instruction_adherence DROP DEFAULT;

CREATE TABLE IF NOT EXISTS voice_quality_measurements (
    measurement_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    profile_id UUID NOT NULL,
    source_run_id TEXT NOT NULL,
    first_audio_ms INTEGER NOT NULL CHECK (first_audio_ms >= 0),
    cancel_tail_ms INTEGER NOT NULL CHECK (cancel_tail_ms >= 0),
    timestamp_error_ms INTEGER NOT NULL CHECK (timestamp_error_ms >= 0),
    long_sentence_chars INTEGER NOT NULL CHECK (long_sentence_chars > 0),
    long_sentence_completion_ratio DOUBLE PRECISION NOT NULL
        CHECK (long_sentence_completion_ratio BETWEEN 0 AND 1),
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, source_run_id),
    FOREIGN KEY (profile_id, account_id)
        REFERENCES voice_profiles(profile_id, account_id) ON DELETE CASCADE
);

ALTER TABLE voice_quality_measurements
ADD COLUMN IF NOT EXISTS long_sentence_chars INTEGER NOT NULL DEFAULT 0;
ALTER TABLE voice_quality_measurements
ADD COLUMN IF NOT EXISTS long_sentence_completion_ratio DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE voice_quality_measurements ALTER COLUMN long_sentence_chars DROP DEFAULT;
ALTER TABLE voice_quality_measurements ALTER COLUMN long_sentence_completion_ratio DROP DEFAULT;

-- What the submitted recording actually contained, measured locally before any
-- provider work. One row per profile: a sample is validated once.
CREATE TABLE IF NOT EXISTS voice_sample_validations (
    validation_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    profile_id UUID NOT NULL,
    sample_id UUID NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    sample_rate INTEGER NOT NULL CHECK (sample_rate > 0),
    channels INTEGER NOT NULL CHECK (channels > 0),
    rms_dbfs DOUBLE PRECISION NOT NULL,
    peak_dbfs DOUBLE PRECISION NOT NULL,
    clipped_ratio DOUBLE PRECISION NOT NULL CHECK (clipped_ratio BETWEEN 0 AND 1),
    silence_ratio DOUBLE PRECISION NOT NULL CHECK (silence_ratio BETWEEN 0 AND 1),
    speech_ms INTEGER NOT NULL CHECK (speech_ms >= 0),
    dc_offset DOUBLE PRECISION NOT NULL,
    reasons_json JSONB NOT NULL CHECK (jsonb_typeof(reasons_json) = 'array'),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, profile_id),
    FOREIGN KEY (profile_id, account_id)
        REFERENCES voice_profiles(profile_id, account_id) ON DELETE CASCADE
);

ALTER TABLE voice_profiles DISABLE ROW LEVEL SECURITY;
ALTER TABLE voice_evaluations DISABLE ROW LEVEL SECURITY;
ALTER TABLE voice_quality_measurements DISABLE ROW LEVEL SECURITY;
ALTER TABLE voice_sample_validations DISABLE ROW LEVEL SECURITY;

UPDATE voice_profiles AS profile
SET evaluation_status = 'pending',
    status = CASE WHEN profile.status = 'active' THEN 'candidate' ELSE profile.status END,
    activated_at = CASE WHEN profile.status = 'active' THEN NULL ELSE profile.activated_at END,
    updated_at = CURRENT_TIMESTAMP
WHERE profile.evaluation_status = 'passed'
  AND EXISTS (
      SELECT 1 FROM voice_evaluations AS evaluation
      WHERE evaluation.profile_id = profile.profile_id
        AND (
            evaluation.accent_similarity = 0
            OR evaluation.emotion_adherence = 0
            OR evaluation.instruction_adherence = 0
        )
  );

UPDATE voice_profiles AS profile
SET quality_status = 'pending',
    status = CASE WHEN profile.status = 'active' THEN 'candidate' ELSE profile.status END,
    activated_at = CASE WHEN profile.status = 'active' THEN NULL ELSE profile.activated_at END,
    updated_at = CURRENT_TIMESTAMP
WHERE profile.quality_status = 'passed'
  AND EXISTS (
      SELECT 1 FROM voice_quality_measurements AS measurement
      WHERE measurement.profile_id = profile.profile_id
        AND (
            measurement.long_sentence_chars = 0
            OR measurement.long_sentence_completion_ratio = 0
        )
  );

ALTER TABLE voice_clone_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_enrollment_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_blind_trials ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_evaluations ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_quality_measurements ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_sample_validations ENABLE ROW LEVEL SECURITY;
ALTER TABLE voice_clone_consents FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_samples FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_enrollment_operations FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_blind_trials FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_evaluations FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_quality_measurements FORCE ROW LEVEL SECURITY;
ALTER TABLE voice_sample_validations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS voice_consent_account_policy ON voice_clone_consents;
CREATE POLICY voice_consent_account_policy ON voice_clone_consents
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS voice_sample_account_policy ON voice_samples;
CREATE POLICY voice_sample_account_policy ON voice_samples
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS voice_sample_provider_read ON voice_samples;
CREATE POLICY voice_sample_provider_read ON voice_samples FOR SELECT
USING (sample_id::text = current_setting('app.voice_sample_id', true));

DROP POLICY IF EXISTS voice_enrollment_operation_account_policy
ON voice_enrollment_operations;
CREATE POLICY voice_enrollment_operation_account_policy
ON voice_enrollment_operations
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS voice_profile_account_policy ON voice_profiles;
CREATE POLICY voice_profile_account_policy ON voice_profiles
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS voice_profile_internal_lookup ON voice_profiles;
CREATE POLICY voice_profile_internal_lookup ON voice_profiles FOR SELECT
USING (profile_id::text = current_setting('app.voice_profile_id', true));

DROP POLICY IF EXISTS voice_blind_trial_account_policy ON voice_blind_trials;
CREATE POLICY voice_blind_trial_account_policy ON voice_blind_trials
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS voice_evaluation_account_policy ON voice_evaluations;
CREATE POLICY voice_evaluation_account_policy ON voice_evaluations
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS voice_quality_measurement_account_policy
ON voice_quality_measurements;
CREATE POLICY voice_quality_measurement_account_policy
ON voice_quality_measurements
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS voice_sample_validation_account_policy
ON voice_sample_validations;
CREATE POLICY voice_sample_validation_account_policy
ON voice_sample_validations
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));
