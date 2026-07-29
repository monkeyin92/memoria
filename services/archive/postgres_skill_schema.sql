CREATE TABLE IF NOT EXISTS skill_definitions (
    skill_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'approved', 'retired')
    ),
    latest_version INTEGER NOT NULL CHECK (latest_version >= 1),
    approved_version INTEGER,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, name)
);

CREATE TABLE IF NOT EXISTS skill_versions (
    skill_id UUID NOT NULL REFERENCES skill_definitions(skill_id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    account_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'approved', 'superseded', 'retired')
    ),
    description TEXT NOT NULL,
    trigger_phrases JSONB NOT NULL,
    input_schema JSONB NOT NULL,
    output_schema JSONB NOT NULL,
    output_template JSONB NOT NULL,
    allowed_tools JSONB NOT NULL,
    steps JSONB NOT NULL,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('explicit_instruction', 'repeated_tool_success')
    ),
    domain_category TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (
        sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')
    ),
    salience DOUBLE PRECISION NOT NULL CHECK (salience BETWEEN 0 AND 1),
    created_at TIMESTAMPTZ NOT NULL,
    approved_at TIMESTAMPTZ,
    approval_event_id TEXT REFERENCES archive_evidence_events(event_id),
    PRIMARY KEY (skill_id, version)
);

CREATE TABLE IF NOT EXISTS skill_version_evidence (
    skill_id UUID NOT NULL,
    version INTEGER NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    PRIMARY KEY (skill_id, version, source_event_id),
    FOREIGN KEY (skill_id, version)
        REFERENCES skill_versions(skill_id, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS skill_runs (
    run_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    skill_id UUID NOT NULL,
    skill_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    rollback_status TEXT NOT NULL CHECK (
        rollback_status IN ('not_required', 'completed', 'partial')
    ),
    confirmation_event_id TEXT NOT NULL UNIQUE
        REFERENCES archive_evidence_events(event_id),
    input_json JSONB NOT NULL,
    output_json JSONB,
    error_code TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (skill_id, skill_version)
        REFERENCES skill_versions(skill_id, version) ON DELETE CASCADE
);

ALTER TABLE skill_runs
    DROP CONSTRAINT IF EXISTS skill_runs_skill_id_skill_version_fkey;
ALTER TABLE skill_runs
    ADD CONSTRAINT skill_runs_skill_id_skill_version_fkey
    FOREIGN KEY (skill_id, skill_version)
    REFERENCES skill_versions(skill_id, version) ON DELETE CASCADE;

CREATE TABLE IF NOT EXISTS skill_run_steps (
    run_id UUID NOT NULL REFERENCES skill_runs(run_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    account_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('forward', 'compensation')),
    tool_name TEXT NOT NULL,
    arguments_json JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    output_json JSONB,
    error_code TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_pg_skill_versions_account_status
ON skill_versions(account_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pg_skill_runs_account_started
ON skill_runs(account_id, started_at DESC);

ALTER TABLE skill_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_version_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE skill_run_steps ENABLE ROW LEVEL SECURITY;

ALTER TABLE skill_definitions FORCE ROW LEVEL SECURITY;
ALTER TABLE skill_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE skill_version_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE skill_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE skill_run_steps FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS skill_definition_account_policy ON skill_definitions;
CREATE POLICY skill_definition_account_policy ON skill_definitions
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS skill_version_account_policy ON skill_versions;
CREATE POLICY skill_version_account_policy ON skill_versions
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS skill_version_evidence_account_policy ON skill_version_evidence;
CREATE POLICY skill_version_evidence_account_policy ON skill_version_evidence
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS skill_run_account_policy ON skill_runs;
CREATE POLICY skill_run_account_policy ON skill_runs
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS skill_run_step_account_policy ON skill_run_steps;
CREATE POLICY skill_run_step_account_policy ON skill_run_steps
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));
