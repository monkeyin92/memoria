CREATE TABLE IF NOT EXISTS self_model_cognitive_claims (
    claim_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    claim_type TEXT NOT NULL CHECK (claim_type IN (
        'belief', 'preference', 'value', 'decision_rule',
        'red_line', 'uncertainty', 'conflict', 'support'
    )),
    statement TEXT NOT NULL CHECK (length(btrim(statement)) > 0),
    context TEXT NOT NULL DEFAULT '',
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    sharing_scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN (
        'candidate', 'confirmed', 'disputed', 'retracted', 'superseded'
    )),
    unresolved_conflict BOOLEAN NOT NULL DEFAULT false,
    owner_reviewed_at TIMESTAMPTZ,
    step_up_verified BOOLEAN NOT NULL DEFAULT false,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (claim_id, account_id)
);

CREATE TABLE IF NOT EXISTS self_model_cognitive_claim_sources (
    claim_id UUID NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK (relation IN ('support', 'counterexample')),
    adopted BOOLEAN NOT NULL DEFAULT true,
    negative BOOLEAN NOT NULL DEFAULT false,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (claim_id, source_event_id, relation),
    FOREIGN KEY (claim_id, account_id)
        REFERENCES self_model_cognitive_claims(claim_id, account_id) ON DELETE CASCADE,
    CHECK (NOT adopted OR relation = 'support')
);

CREATE TABLE IF NOT EXISTS self_model_decision_cases (
    case_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('real', 'hypothetical')),
    context TEXT NOT NULL CHECK (length(btrim(context)) > 0),
    options JSONB NOT NULL,
    constraints JSONB NOT NULL,
    chosen_option TEXT NOT NULL,
    rejected_options JSONB NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    reflection TEXT NOT NULL DEFAULT '',
    still_endorsed BOOLEAN NOT NULL DEFAULT true,
    sharing_scope TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN (
        'candidate', 'confirmed', 'disputed', 'retracted', 'superseded'
    )),
    unresolved_conflict BOOLEAN NOT NULL DEFAULT false,
    owner_reviewed_at TIMESTAMPTZ,
    step_up_verified BOOLEAN NOT NULL DEFAULT false,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id, account_id)
);

CREATE TABLE IF NOT EXISTS self_model_decision_case_sources (
    case_id UUID NOT NULL,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK (relation IN ('support', 'counterexample')),
    adopted BOOLEAN NOT NULL DEFAULT true,
    negative BOOLEAN NOT NULL DEFAULT false,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (case_id, source_event_id, relation),
    FOREIGN KEY (case_id, account_id)
        REFERENCES self_model_decision_cases(case_id, account_id) ON DELETE CASCADE,
    CHECK (NOT adopted OR relation = 'support')
);

CREATE TABLE IF NOT EXISTS self_model_relationship_profiles (
    profile_id UUID NOT NULL,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    person_id UUID NOT NULL REFERENCES person_entities(person_id),
    relationship_id UUID NOT NULL REFERENCES relationships(relationship_id),
    salutation TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT '',
    advice_style TEXT NOT NULL DEFAULT '',
    sharing_scope TEXT NOT NULL,
    boundaries JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN (
        'candidate', 'approved', 'revoked', 'superseded'
    )),
    unresolved_conflict BOOLEAN NOT NULL DEFAULT false,
    owner_reviewed_at TIMESTAMPTZ,
    step_up_verified BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, version_number),
    UNIQUE (account_id, profile_id, version_number)
);

ALTER TABLE self_model_relationship_profiles
DROP CONSTRAINT IF EXISTS
    self_model_relationship_profi_account_id_person_id_version__key;

CREATE TABLE IF NOT EXISTS self_model_relationship_profile_sources (
    profile_id UUID NOT NULL,
    profile_version INTEGER NOT NULL CHECK (profile_version > 0),
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    relation TEXT NOT NULL CHECK (relation IN ('support', 'counterexample')),
    adopted BOOLEAN NOT NULL DEFAULT true,
    negative BOOLEAN NOT NULL DEFAULT false,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile_id, profile_version, source_event_id, relation),
    FOREIGN KEY (profile_id, profile_version)
        REFERENCES self_model_relationship_profiles(profile_id, version_number) ON DELETE CASCADE,
    CHECK (NOT adopted OR relation = 'support')
);

CREATE TABLE IF NOT EXISTS self_model_audit_events (
    event_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    actor_account_id TEXT NOT NULL CHECK (actor_account_id = account_id),
    transaction_id UUID NOT NULL,
    action TEXT NOT NULL CHECK (action IN (
        'create_claim', 'create_decision_case', 'create_relationship_profile',
        'add_source', 'review_claim', 'review_decision_case',
        'approve_relationship_profile', 'revoke_relationship_profile',
        'delete_account'
    )),
    target_kind TEXT NOT NULL CHECK (target_kind IN (
        'cognitive_claim', 'decision_case', 'relationship_profile', 'account'
    )),
    target_id UUID,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS self_model_command_receipts (
    account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_type TEXT NOT NULL,
    payload_sha256 CHAR(64) NOT NULL,
    result_kind TEXT NOT NULL CHECK (result_kind IN (
        'cognitive_claim', 'decision_case', 'relationship_profile'
    )),
    result_id UUID NOT NULL,
    result_version INTEGER NOT NULL CHECK (result_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_self_model_claim_account_status
ON self_model_cognitive_claims(account_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_self_model_decision_account_status
ON self_model_decision_cases(account_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_self_model_relationship_account_status
ON self_model_relationship_profiles(account_id, person_id, status, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_self_model_audit_account_occurred
ON self_model_audit_events(account_id, occurred_at DESC);

CREATE OR REPLACE FUNCTION self_model_relationship_profile_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'approved'
           AND current_setting('app.self_model_delete', true) IS DISTINCT FROM 'true' THEN
            RAISE EXCEPTION 'approved relationship profile versions are immutable';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'approved' THEN
        IF NEW.status NOT IN ('approved', 'revoked', 'superseded')
           OR (to_jsonb(NEW) - ARRAY['status'])
              IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status']) THEN
            RAISE EXCEPTION 'approved relationship profile versions are immutable';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS self_model_relationship_profile_immutable
ON self_model_relationship_profiles;
CREATE TRIGGER self_model_relationship_profile_immutable
BEFORE UPDATE OR DELETE ON self_model_relationship_profiles
FOR EACH ROW EXECUTE FUNCTION self_model_relationship_profile_guard();

CREATE OR REPLACE FUNCTION self_model_audit_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('app.self_model_delete', true) = 'true' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'self model audit events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS self_model_audit_immutable ON self_model_audit_events;
CREATE TRIGGER self_model_audit_immutable
BEFORE UPDATE OR DELETE ON self_model_audit_events
FOR EACH ROW EXECUTE FUNCTION self_model_audit_immutable_guard();

ALTER TABLE self_model_cognitive_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE self_model_cognitive_claim_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE self_model_decision_cases ENABLE ROW LEVEL SECURITY;
ALTER TABLE self_model_decision_case_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE self_model_relationship_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE self_model_relationship_profile_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE self_model_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE self_model_command_receipts ENABLE ROW LEVEL SECURITY;

ALTER TABLE self_model_cognitive_claims FORCE ROW LEVEL SECURITY;
ALTER TABLE self_model_cognitive_claim_sources FORCE ROW LEVEL SECURITY;
ALTER TABLE self_model_decision_cases FORCE ROW LEVEL SECURITY;
ALTER TABLE self_model_decision_case_sources FORCE ROW LEVEL SECURITY;
ALTER TABLE self_model_relationship_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE self_model_relationship_profile_sources FORCE ROW LEVEL SECURITY;
ALTER TABLE self_model_audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE self_model_command_receipts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS self_model_cognitive_claim_account_policy
ON self_model_cognitive_claims;
CREATE POLICY self_model_cognitive_claim_account_policy ON self_model_cognitive_claims
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS self_model_cognitive_claim_source_account_policy
ON self_model_cognitive_claim_sources;
CREATE POLICY self_model_cognitive_claim_source_account_policy ON self_model_cognitive_claim_sources
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS self_model_decision_case_account_policy ON self_model_decision_cases;
CREATE POLICY self_model_decision_case_account_policy ON self_model_decision_cases
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS self_model_decision_case_source_account_policy
ON self_model_decision_case_sources;
CREATE POLICY self_model_decision_case_source_account_policy ON self_model_decision_case_sources
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS self_model_relationship_profile_account_policy
ON self_model_relationship_profiles;
CREATE POLICY self_model_relationship_profile_account_policy
ON self_model_relationship_profiles
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS self_model_relationship_profile_source_account_policy
ON self_model_relationship_profile_sources;
CREATE POLICY self_model_relationship_profile_source_account_policy
ON self_model_relationship_profile_sources
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS self_model_audit_account_policy ON self_model_audit_events;
CREATE POLICY self_model_audit_account_policy ON self_model_audit_events
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS self_model_command_receipt_account_policy ON self_model_command_receipts;
CREATE POLICY self_model_command_receipt_account_policy ON self_model_command_receipts
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));
