CREATE TABLE IF NOT EXISTS persona_traits (
    trait_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    description TEXT NOT NULL,
    context TEXT NOT NULL,
    counterexample TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disabled')
    ),
    observation_count INTEGER NOT NULL DEFAULT 0 CHECK (observation_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_event_id TEXT,
    UNIQUE (account_id, category, normalized_key)
);

CREATE TABLE IF NOT EXISTS persona_evidence (
    trait_id UUID NOT NULL REFERENCES persona_traits(trait_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    scene TEXT NOT NULL,
    weight DOUBLE PRECISION NOT NULL CHECK (weight BETWEEN 0 AND 1),
    occurred_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (trait_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS persona_observation_receipts (
    source_event_id TEXT PRIMARY KEY
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS speech_style_stats (
    account_id TEXT NOT NULL,
    scene TEXT NOT NULL,
    utterance_count INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    speech_duration_ms BIGINT NOT NULL DEFAULT 0,
    pause_ratio_sum DOUBLE PRECISION NOT NULL DEFAULT 0,
    pause_sample_count INTEGER NOT NULL DEFAULT 0,
    tic_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, scene)
);

CREATE TABLE IF NOT EXISTS persona_learning_consents (
    account_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    grant_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id),
    revoke_event_id TEXT REFERENCES archive_evidence_events(event_id)
);

CREATE TABLE IF NOT EXISTS persona_versions (
    version_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    reason TEXT NOT NULL,
    snapshot JSONB NOT NULL,
    parent_version_id UUID REFERENCES persona_versions(version_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, version_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_persona_one_active_version
ON persona_versions(account_id) WHERE status = 'active';

ALTER TABLE persona_traits ENABLE ROW LEVEL SECURITY;
ALTER TABLE persona_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE persona_observation_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE speech_style_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE persona_learning_consents ENABLE ROW LEVEL SECURITY;
ALTER TABLE persona_versions ENABLE ROW LEVEL SECURITY;

ALTER TABLE persona_traits FORCE ROW LEVEL SECURITY;
ALTER TABLE persona_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE persona_observation_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE speech_style_stats FORCE ROW LEVEL SECURITY;
ALTER TABLE persona_learning_consents FORCE ROW LEVEL SECURITY;
ALTER TABLE persona_versions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS persona_trait_account_policy ON persona_traits;
CREATE POLICY persona_trait_account_policy ON persona_traits
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS persona_evidence_account_policy ON persona_evidence;
CREATE POLICY persona_evidence_account_policy ON persona_evidence
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS persona_receipt_account_policy ON persona_observation_receipts;
CREATE POLICY persona_receipt_account_policy ON persona_observation_receipts
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS speech_style_account_policy ON speech_style_stats;
CREATE POLICY speech_style_account_policy ON speech_style_stats
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS persona_consent_account_policy ON persona_learning_consents;
CREATE POLICY persona_consent_account_policy ON persona_learning_consents
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS persona_version_account_policy ON persona_versions;
CREATE POLICY persona_version_account_policy ON persona_versions
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));
