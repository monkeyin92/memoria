CREATE TABLE IF NOT EXISTS speaker_identities (
    identity_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    identity_type TEXT NOT NULL CHECK (identity_type IN ('owner', 'guest')),
    label TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, identity_type, label)
);

CREATE TABLE IF NOT EXISTS speaker_profiles (
    profile_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    identity_id UUID NOT NULL REFERENCES speaker_identities(identity_id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    template_version INTEGER NOT NULL CHECK (template_version > 0),
    template_ciphertext BYTEA,
    owner_threshold DOUBLE PRECISION NOT NULL,
    guest_threshold DOUBLE PRECISION NOT NULL,
    consent_grant_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('shadow', 'active', 'revoked')),
    evaluation_ref TEXT,
    evaluation_sample_count INTEGER,
    far DOUBLE PRECISION,
    frr DOUBLE PRECISION,
    eer DOUBLE PRECISION,
    unknown_rejection DOUBLE PRECISION,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT,
    UNIQUE (account_id, template_version)
);

ALTER TABLE speaker_profiles
    ADD COLUMN IF NOT EXISTS evaluation_sample_count INTEGER,
    ADD COLUMN IF NOT EXISTS far DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS frr DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS eer DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS unknown_rejection DOUBLE PRECISION;

CREATE UNIQUE INDEX IF NOT EXISTS idx_speaker_one_active_owner
ON speaker_profiles(account_id)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS speaker_enrollment_samples (
    sample_id UUID PRIMARY KEY,
    profile_id UUID NOT NULL REFERENCES speaker_profiles(profile_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    speech_ms INTEGER NOT NULL CHECK (speech_ms >= 0),
    snr_db DOUBLE PRECISION NOT NULL,
    quality_score DOUBLE PRECISION NOT NULL,
    replay_risk DOUBLE PRECISION NOT NULL,
    synthetic_risk DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    risk_assessment TEXT NOT NULL DEFAULT 'unavailable'
        CHECK (risk_assessment IN ('verified', 'unavailable')),
    device TEXT NOT NULL,
    scene TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE speaker_enrollment_samples
    ADD COLUMN IF NOT EXISTS synthetic_risk DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS risk_assessment TEXT NOT NULL DEFAULT 'unavailable';

CREATE TABLE IF NOT EXISTS speaker_enrollment_intents (
    intent_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    consent_policy_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('requested', 'consumed', 'revoked')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_speaker_enrollment_intents_account
ON speaker_enrollment_intents(account_id, state, expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_speaker_one_pending_enrollment_intent
ON speaker_enrollment_intents(account_id)
WHERE state = 'requested';

ALTER TABLE speaker_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE speaker_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE speaker_enrollment_samples ENABLE ROW LEVEL SECURITY;
ALTER TABLE speaker_enrollment_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE speaker_identities FORCE ROW LEVEL SECURITY;
ALTER TABLE speaker_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE speaker_enrollment_samples FORCE ROW LEVEL SECURITY;
ALTER TABLE speaker_enrollment_intents FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS speaker_identity_account_policy ON speaker_identities;
CREATE POLICY speaker_identity_account_policy ON speaker_identities
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS speaker_profile_account_policy ON speaker_profiles;
CREATE POLICY speaker_profile_account_policy ON speaker_profiles
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS speaker_sample_account_policy ON speaker_enrollment_samples;
CREATE POLICY speaker_sample_account_policy ON speaker_enrollment_samples
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS speaker_intent_account_policy ON speaker_enrollment_intents;
CREATE POLICY speaker_intent_account_policy ON speaker_enrollment_intents
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));
