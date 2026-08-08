CREATE TABLE IF NOT EXISTS evolution_learning_signals (
    signal_id TEXT PRIMARY KEY,
    task_family TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('owner_private', 'global_redacted')),
    account_id TEXT,
    CHECK ((scope = 'owner_private' AND account_id IS NOT NULL)
        OR (scope = 'global_redacted' AND account_id IS NULL)),
    payload JSONB NOT NULL,
    payload_hash CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_candidates (
    candidate_id TEXT PRIMARY KEY,
    task_family TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('knowledge', 'prompt', 'skill', 'harness', 'parameter')),
    scope TEXT NOT NULL CHECK (scope IN ('owner_private', 'global_redacted')),
    account_id TEXT,
    CHECK ((scope = 'owner_private' AND account_id IS NOT NULL)
        OR (scope = 'global_redacted' AND account_id IS NULL)),
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'validated', 'canary', 'stable', 'rejected', 'retired')),
    payload JSONB NOT NULL,
    artifact_hash CHAR(64) NOT NULL,
    source_signal_ids JSONB NOT NULL,
    expected_behavior TEXT NOT NULL,
    regression_guards JSONB NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    trusted_root_sha256 CHAR(64) NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_validations (
    validation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES evolution_candidates(candidate_id),
    payload JSONB NOT NULL,
    passed BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_activation_events (
    activation_id UUID PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES evolution_candidates(candidate_id),
    task_id TEXT NOT NULL,
    activated BOOLEAN NOT NULL,
    adhered BOOLEAN NOT NULL,
    outcome_passed BOOLEAN NOT NULL,
    evidence_event_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_lifecycle_events (
    sequence BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL REFERENCES evolution_candidates(candidate_id),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'transition', 'supersede', 'curate', 'rollback_retire', 'rollback_restore'
    )),
    from_status TEXT NOT NULL CHECK (from_status IN (
        'candidate', 'validated', 'canary', 'stable', 'rejected', 'retired'
    )),
    to_status TEXT NOT NULL CHECK (to_status IN (
        'candidate', 'validated', 'canary', 'stable', 'rejected', 'retired'
    )),
    reason TEXT NOT NULL DEFAULT '',
    related_candidate_id TEXT REFERENCES evolution_candidates(candidate_id),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_control_state (
    state_key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

-- Durable, controller-only account deletion fences.  Rows are intentionally
-- retained after erasure so a restarted worker cannot recreate private data.
CREATE TABLE IF NOT EXISTS evolution_account_deletion_fences (
    account_id TEXT PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_sleep_signal_receipts (
    signal_id TEXT PRIMARY KEY REFERENCES evolution_learning_signals(signal_id),
    processed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evolution_signals_family
ON evolution_learning_signals(task_family, created_at);

-- A pair may be reviewed by multiple evaluator versions, but one evaluator
-- version must not manufacture multiple support rows by changing evaluation_id.
CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_signals_trajectory_evaluation
ON evolution_learning_signals(
    (payload->'source_event_ids'->>0),
    (payload->'source_event_ids'->>1),
    (payload->'artifact_versions'->>'trajectory_evaluator')
)
WHERE jsonb_typeof(payload->'source_event_ids') = 'array'
  AND jsonb_array_length(payload->'source_event_ids') = 2
  AND jsonb_typeof(payload->'artifact_versions'->'trajectory_evaluator') = 'string';

CREATE INDEX IF NOT EXISTS idx_evolution_candidates_status
ON evolution_candidates(status, updated_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_candidates_one_stable
ON evolution_candidates(task_family, kind, scope, account_id) NULLS NOT DISTINCT
WHERE status = 'stable';

CREATE INDEX IF NOT EXISTS idx_evolution_candidates_account
ON evolution_candidates(account_id, created_at);

CREATE INDEX IF NOT EXISTS idx_evolution_deletion_fences_started
ON evolution_account_deletion_fences(started_at);

CREATE INDEX IF NOT EXISTS idx_evolution_validations_candidate
ON evolution_validations(candidate_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_activation_candidate_evidence
ON evolution_activation_events(candidate_id, evidence_event_id);

-- Evolution is a privileged, offline controller. It is never queried with a
-- normal user DSN; FORCE RLS keeps an accidental shared connection fail-closed.
ALTER TABLE evolution_learning_signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_validations ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_activation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_lifecycle_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_control_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_sleep_signal_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_account_deletion_fences ENABLE ROW LEVEL SECURITY;
ALTER TABLE evolution_learning_signals FORCE ROW LEVEL SECURITY;
ALTER TABLE evolution_candidates FORCE ROW LEVEL SECURITY;
ALTER TABLE evolution_validations FORCE ROW LEVEL SECURITY;
ALTER TABLE evolution_activation_events FORCE ROW LEVEL SECURITY;
ALTER TABLE evolution_lifecycle_events FORCE ROW LEVEL SECURITY;
ALTER TABLE evolution_control_state FORCE ROW LEVEL SECURITY;
ALTER TABLE evolution_sleep_signal_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE evolution_account_deletion_fences FORCE ROW LEVEL SECURITY;

DO $evolution_policy$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_evolution') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON evolution_learning_signals TO memoria_evolution;
        GRANT SELECT, INSERT, UPDATE, DELETE ON evolution_candidates TO memoria_evolution;
        GRANT SELECT, INSERT, UPDATE, DELETE ON evolution_validations TO memoria_evolution;
        GRANT SELECT, INSERT, UPDATE, DELETE ON evolution_activation_events TO memoria_evolution;
        GRANT SELECT, INSERT ON evolution_lifecycle_events TO memoria_evolution;
        GRANT USAGE, SELECT ON SEQUENCE evolution_lifecycle_events_sequence_seq TO memoria_evolution;
        GRANT SELECT, INSERT, UPDATE, DELETE ON evolution_control_state TO memoria_evolution;
        GRANT SELECT, INSERT, UPDATE, DELETE ON evolution_sleep_signal_receipts TO memoria_evolution;
        GRANT SELECT, INSERT, UPDATE, DELETE ON evolution_account_deletion_fences TO memoria_evolution;
        DROP POLICY IF EXISTS evolution_controller_signals ON evolution_learning_signals;
        CREATE POLICY evolution_controller_signals ON evolution_learning_signals
            TO memoria_evolution USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS evolution_controller_candidates ON evolution_candidates;
        CREATE POLICY evolution_controller_candidates ON evolution_candidates
            TO memoria_evolution USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS evolution_controller_validations ON evolution_validations;
        CREATE POLICY evolution_controller_validations ON evolution_validations
            TO memoria_evolution USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS evolution_controller_activations ON evolution_activation_events;
        CREATE POLICY evolution_controller_activations ON evolution_activation_events
            TO memoria_evolution USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS evolution_controller_lifecycle ON evolution_lifecycle_events;
        CREATE POLICY evolution_controller_lifecycle ON evolution_lifecycle_events
            TO memoria_evolution USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS evolution_controller_state ON evolution_control_state;
        CREATE POLICY evolution_controller_state ON evolution_control_state
            TO memoria_evolution USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS evolution_controller_receipts ON evolution_sleep_signal_receipts;
        CREATE POLICY evolution_controller_receipts ON evolution_sleep_signal_receipts
            TO memoria_evolution USING (true) WITH CHECK (true);
        DROP POLICY IF EXISTS evolution_controller_deletion_fences ON evolution_account_deletion_fences;
        CREATE POLICY evolution_controller_deletion_fences ON evolution_account_deletion_fences
            TO memoria_evolution USING (true) WITH CHECK (true);
    END IF;
END
$evolution_policy$;

CREATE OR REPLACE FUNCTION evolution_block_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $immutable$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('app.evolution_account_deletion', true) = '1' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'evolution evidence is append-only';
END
$immutable$;

CREATE OR REPLACE FUNCTION evolution_scope_account_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $scope_guard$
BEGIN
    IF NOT (
        (NEW.scope = 'owner_private' AND NEW.account_id IS NOT NULL)
        OR (NEW.scope = 'global_redacted' AND NEW.account_id IS NULL)
    ) THEN
        RAISE EXCEPTION 'evolution scope/account mismatch';
    END IF;
    RETURN NEW;
END
$scope_guard$;

CREATE OR REPLACE FUNCTION evolution_owner_private_write_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $private_guard$
DECLARE
    account_id_value TEXT;
BEGIN
    IF current_setting('app.evolution_account_deletion', true) = '1' THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME IN ('evolution_learning_signals', 'evolution_candidates') THEN
        account_id_value := CASE
            WHEN NEW.scope = 'owner_private' THEN NEW.account_id
            ELSE NULL
        END;
    ELSIF TG_TABLE_NAME = 'evolution_validations'
       OR TG_TABLE_NAME = 'evolution_activation_events'
       OR TG_TABLE_NAME = 'evolution_lifecycle_events' THEN
        SELECT candidate.account_id INTO account_id_value
        FROM evolution_candidates candidate
        WHERE candidate.candidate_id = NEW.candidate_id
          AND candidate.scope = 'owner_private';
    ELSIF TG_TABLE_NAME = 'evolution_sleep_signal_receipts' THEN
        SELECT signal.account_id INTO account_id_value
        FROM evolution_learning_signals signal
        WHERE signal.signal_id = NEW.signal_id
          AND signal.scope = 'owner_private';
    END IF;
    IF account_id_value IS NOT NULL
       THEN
        -- The deletion marker and every owner-private write take the same
        -- transaction-scoped advisory lock. This closes the check/insert
        -- window across independent control-api workers.
        PERFORM pg_advisory_xact_lock(hashtextextended(account_id_value, 0));
    END IF;
    IF account_id_value IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM evolution_account_deletion_fences fence
           WHERE fence.account_id = account_id_value
       ) THEN
        RAISE EXCEPTION 'owner-private evolution write blocked by account deletion';
    END IF;
    RETURN NEW;
END
$private_guard$;

DROP TRIGGER IF EXISTS evolution_signal_deletion_fence ON evolution_learning_signals;
CREATE TRIGGER evolution_signal_deletion_fence
BEFORE INSERT OR UPDATE ON evolution_learning_signals
FOR EACH ROW EXECUTE FUNCTION evolution_owner_private_write_guard();

DROP TRIGGER IF EXISTS evolution_candidate_deletion_fence ON evolution_candidates;
CREATE TRIGGER evolution_candidate_deletion_fence
BEFORE INSERT OR UPDATE ON evolution_candidates
FOR EACH ROW EXECUTE FUNCTION evolution_owner_private_write_guard();

DROP TRIGGER IF EXISTS evolution_validation_deletion_fence ON evolution_validations;
CREATE TRIGGER evolution_validation_deletion_fence
BEFORE INSERT OR UPDATE ON evolution_validations
FOR EACH ROW EXECUTE FUNCTION evolution_owner_private_write_guard();

DROP TRIGGER IF EXISTS evolution_activation_deletion_fence ON evolution_activation_events;
CREATE TRIGGER evolution_activation_deletion_fence
BEFORE INSERT OR UPDATE ON evolution_activation_events
FOR EACH ROW EXECUTE FUNCTION evolution_owner_private_write_guard();

DROP TRIGGER IF EXISTS evolution_lifecycle_deletion_fence ON evolution_lifecycle_events;
CREATE TRIGGER evolution_lifecycle_deletion_fence
BEFORE INSERT OR UPDATE ON evolution_lifecycle_events
FOR EACH ROW EXECUTE FUNCTION evolution_owner_private_write_guard();

DROP TRIGGER IF EXISTS evolution_receipt_deletion_fence ON evolution_sleep_signal_receipts;
CREATE TRIGGER evolution_receipt_deletion_fence
BEFORE INSERT OR UPDATE ON evolution_sleep_signal_receipts
FOR EACH ROW EXECUTE FUNCTION evolution_owner_private_write_guard();

DROP TRIGGER IF EXISTS evolution_signal_scope_guard ON evolution_learning_signals;
CREATE TRIGGER evolution_signal_scope_guard
BEFORE INSERT OR UPDATE ON evolution_learning_signals
FOR EACH ROW EXECUTE FUNCTION evolution_scope_account_guard();

DROP TRIGGER IF EXISTS evolution_candidate_scope_guard ON evolution_candidates;
CREATE TRIGGER evolution_candidate_scope_guard
BEFORE INSERT OR UPDATE ON evolution_candidates
FOR EACH ROW EXECUTE FUNCTION evolution_scope_account_guard();

DROP TRIGGER IF EXISTS evolution_signals_immutable ON evolution_learning_signals;
CREATE TRIGGER evolution_signals_immutable
BEFORE UPDATE OR DELETE ON evolution_learning_signals
FOR EACH ROW EXECUTE FUNCTION evolution_block_immutable_mutation();

DROP TRIGGER IF EXISTS evolution_validations_immutable ON evolution_validations;
CREATE TRIGGER evolution_validations_immutable
BEFORE UPDATE OR DELETE ON evolution_validations
FOR EACH ROW EXECUTE FUNCTION evolution_block_immutable_mutation();

DROP TRIGGER IF EXISTS evolution_activations_immutable ON evolution_activation_events;
CREATE TRIGGER evolution_activations_immutable
BEFORE UPDATE OR DELETE ON evolution_activation_events
FOR EACH ROW EXECUTE FUNCTION evolution_block_immutable_mutation();

DROP TRIGGER IF EXISTS evolution_lifecycle_immutable ON evolution_lifecycle_events;
CREATE TRIGGER evolution_lifecycle_immutable
BEFORE UPDATE OR DELETE ON evolution_lifecycle_events
FOR EACH ROW EXECUTE FUNCTION evolution_block_immutable_mutation();

DROP TRIGGER IF EXISTS evolution_candidates_immutable_delete ON evolution_candidates;
CREATE TRIGGER evolution_candidates_immutable_delete
BEFORE DELETE ON evolution_candidates
FOR EACH ROW EXECUTE FUNCTION evolution_block_immutable_mutation();

DROP TRIGGER IF EXISTS evolution_sleep_signal_receipts_immutable ON evolution_sleep_signal_receipts;
CREATE TRIGGER evolution_sleep_signal_receipts_immutable
BEFORE UPDATE OR DELETE ON evolution_sleep_signal_receipts
FOR EACH ROW EXECUTE FUNCTION evolution_block_immutable_mutation();

CREATE OR REPLACE FUNCTION evolution_candidate_manifest_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $candidate_guard$
BEGIN
    IF NEW.candidate_id <> OLD.candidate_id
       OR NEW.task_family <> OLD.task_family
       OR NEW.kind <> OLD.kind
       OR NEW.scope <> OLD.scope
       OR NEW.account_id IS DISTINCT FROM OLD.account_id
       OR NEW.version <> OLD.version
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.artifact_hash <> OLD.artifact_hash
       OR NEW.source_signal_ids IS DISTINCT FROM OLD.source_signal_ids
       OR NEW.expected_behavior <> OLD.expected_behavior
       OR NEW.regression_guards IS DISTINCT FROM OLD.regression_guards
       OR NEW.risk <> OLD.risk
       OR NEW.trusted_root_sha256 <> OLD.trusted_root_sha256
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'evolution candidate manifest is immutable';
    END IF;
    RETURN NEW;
END
$candidate_guard$;

DROP TRIGGER IF EXISTS evolution_candidate_manifest_guard ON evolution_candidates;
CREATE TRIGGER evolution_candidate_manifest_guard
BEFORE UPDATE ON evolution_candidates
FOR EACH ROW EXECUTE FUNCTION evolution_candidate_manifest_guard();

CREATE INDEX IF NOT EXISTS idx_evolution_lifecycle_candidate
ON evolution_lifecycle_events(candidate_id, sequence);
