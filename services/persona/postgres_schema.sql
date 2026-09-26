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
    subject_id TEXT NOT NULL,
    CONSTRAINT persona_traits_account_subject_key
        UNIQUE (account_id, subject_id, category, normalized_key)
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
    subject_id TEXT NOT NULL,
    CONSTRAINT speech_style_stats_pkey PRIMARY KEY (account_id, subject_id, scene)
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
    subject_id TEXT NOT NULL,
    CONSTRAINT persona_versions_account_subject_version_key
        UNIQUE (account_id, subject_id, version_number)
);

-- Persona-owned rows are keyed by (account_id, subject_id): account_id is the
-- binding/custodian account, subject_id the person whose persona it is.  A row
-- written without a subject (legacy writers, restores, fixtures) belongs to
-- the account holder.
CREATE OR REPLACE FUNCTION persona_default_subject_id() RETURNS trigger
LANGUAGE plpgsql AS $persona_default_subject$
BEGIN
    IF NEW.subject_id IS NULL THEN
        NEW.subject_id := NEW.account_id;
    END IF;
    RETURN NEW;
END;
$persona_default_subject$;

-- Migrates a pre-subject database in place and is a catalog-only no-op once
-- migrated: every ALTER sits behind a guard, so a later startup takes no
-- table lock here.  Existing rows are the account holder's own persona.
-- FORCE ROW LEVEL SECURITY would hide every row from the owner's backfill
-- (no app.account_id is set at startup), so it is lifted for the backfill
-- only; the RLS policies themselves stay keyed on account_id.
DO $persona_subject_migration$
DECLARE
    target TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY['persona_traits', 'speech_style_stats', 'persona_versions']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_attribute
            WHERE attrelid = to_regclass(target)
              AND attname = 'subject_id'
              AND NOT attisdropped
        ) THEN
            EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY', target);
            EXECUTE format('ALTER TABLE %I ADD COLUMN subject_id TEXT', target);
            EXECUTE format(
                'UPDATE %I SET subject_id = account_id WHERE subject_id IS NULL',
                target
            );
            EXECUTE format('ALTER TABLE %I ALTER COLUMN subject_id SET NOT NULL', target);
            EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', target);
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('persona_traits')
          AND conname = 'persona_traits_account_id_category_normalized_key_key'
    ) THEN
        ALTER TABLE persona_traits
            DROP CONSTRAINT persona_traits_account_id_category_normalized_key_key;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('persona_traits')
          AND conname = 'persona_traits_account_subject_key'
    ) THEN
        ALTER TABLE persona_traits
            ADD CONSTRAINT persona_traits_account_subject_key
            UNIQUE (account_id, subject_id, category, normalized_key);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS c
        JOIN pg_attribute AS a
          ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
        WHERE c.conrelid = to_regclass('speech_style_stats')
          AND c.contype = 'p'
          AND a.attname = 'subject_id'
    ) THEN
        ALTER TABLE speech_style_stats DROP CONSTRAINT IF EXISTS speech_style_stats_pkey;
        ALTER TABLE speech_style_stats
            ADD CONSTRAINT speech_style_stats_pkey
            PRIMARY KEY (account_id, subject_id, scene);
    END IF;

    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('persona_versions')
          AND conname = 'persona_versions_account_id_version_number_key'
    ) THEN
        ALTER TABLE persona_versions
            DROP CONSTRAINT persona_versions_account_id_version_number_key;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = to_regclass('persona_versions')
          AND conname = 'persona_versions_account_subject_version_key'
    ) THEN
        ALTER TABLE persona_versions
            ADD CONSTRAINT persona_versions_account_subject_version_key
            UNIQUE (account_id, subject_id, version_number);
    END IF;
    IF to_regclass('idx_pg_persona_one_active_version') IS NOT NULL THEN
        DROP INDEX idx_pg_persona_one_active_version;
    END IF;
    IF to_regclass('idx_pg_persona_one_active_subject_version') IS NULL THEN
        CREATE UNIQUE INDEX idx_pg_persona_one_active_subject_version
        ON persona_versions(account_id, subject_id) WHERE status = 'active';
    END IF;

    FOREACH target IN ARRAY ARRAY['persona_traits', 'speech_style_stats', 'persona_versions']
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = to_regclass(target)
              AND tgname = target || '_default_subject'
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE INSERT ON %I '
                'FOR EACH ROW EXECUTE FUNCTION persona_default_subject_id()',
                target || '_default_subject',
                target
            );
        END IF;
    END LOOP;
END;
$persona_subject_migration$;

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
