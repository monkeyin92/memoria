-- PostgreSQL production schema for the memory scope domain (PR-17).
-- Records/status events are append-only; withdrawal is a status event that
-- makes the record invisible to read paths (section 13.5).
--
-- RLS is REAL, never an open pass-all policy: every policy evaluates the
-- transaction-level app context set by the adapter
-- (``app.memory.actor_subject_id``, ``app.memory.subject_id``,
-- ``app.memory.family_space_id``, ``app.memory.record_id``,
-- ``app.memory.proposal_id``).  Command-level authority comes from REAL
-- database roles (``memoria_memory_api`` vs ``memoria_memory_worker`` via
-- ``pg_has_role`` - a GUC is never a credential, P0-2); the GUCs only carry
-- row context.  With FORCE RLS and no context every comparison is NULL ->
-- false, so reads return nothing and writes are rejected (fail closed).
-- The adapter refuses to start unless it can set and read this context
-- inside a transaction.

-- Runtime logins are never object owners.  A dedicated NOLOGIN owner keeps
-- tables and SECURITY DEFINER functions away from the bootstrap superuser
-- while FORCE RLS remains effective for the sensitive commit path.
DO $memory_roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_memory_owner'
    ) THEN
        CREATE ROLE memoria_memory_owner
            NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_memory_api'
    ) THEN
        CREATE ROLE memoria_memory_api
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_memory_worker'
    ) THEN
        CREATE ROLE memoria_memory_worker
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_action_executor'
    ) THEN
        CREATE ROLE memoria_action_executor
            LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
    IF current_user <> 'memoria_memory_owner' THEN
        EXECUTE format('GRANT memoria_memory_owner TO %I', current_user);
    END IF;
END
$memory_roles$;

ALTER ROLE memoria_memory_owner
    NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE memoria_memory_api
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE memoria_memory_worker
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE memoria_action_executor
    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

GRANT USAGE, CREATE ON SCHEMA public TO memoria_memory_owner;
-- The bootstrap/migration role grants the owner only the narrow Session
-- receipt lookup used by the capture SECURITY DEFINER function.  This must
-- happen before SET ROLE below because the action-policy bridge is owned by
-- another domain and cannot be re-granted by the Memory owner.
DO $memory_capture_receipt_bootstrap$
BEGIN
    IF to_regprocedure('public.action_policy_lock_receipt(text)') IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION action_policy_lock_receipt(TEXT)
            TO memoria_memory_owner;
    END IF;
    IF to_regprocedure(
        'public.session_runtime_assert_action_context(text,text,text,text,text,integer,integer)'
    ) IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION session_runtime_assert_action_context(
            TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, INTEGER
        ) TO memoria_memory_owner;
    END IF;
    IF to_regprocedure(
        'public.session_runtime_action_current_profile(text)'
    ) IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION session_runtime_action_current_profile(TEXT)
            TO memoria_memory_owner;
    END IF;
END
$memory_capture_receipt_bootstrap$;
SET ROLE memoria_memory_owner;
SET search_path = public;

CREATE TABLE IF NOT EXISTS memory_records (
    record_id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN (
        'unknown', 'session_ephemeral', 'personal_private',
        'guardian_summary', 'family_shared', 'legacy_archive'
    )),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    resource_owner_id TEXT NOT NULL CHECK (char_length(resource_owner_id) BETWEEN 1 AND 128),
    family_space_id TEXT,
    co_subject_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(co_subject_ids) = 'array'),
    source_evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(source_evidence_ids) = 'array'),
    policy_receipt_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(policy_receipt_id) BETWEEN 1 AND 128),
    promotion_receipt_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(promotion_receipt_id) BETWEEN 0 AND 128),
    promotion_fence_context_hash TEXT NOT NULL DEFAULT ''
        CHECK (char_length(promotion_fence_context_hash) BETWEEN 0 AND 128),
    approval_evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(approval_evidence_refs) = 'array'),
    consent_snapshot_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(consent_snapshot_id) BETWEEN 1 AND 128),
    memory_type TEXT NOT NULL DEFAULT 'semantic',
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0.5
        CHECK (confidence >= 0.0 AND confidence <= 1.0),
    retention TEXT NOT NULL DEFAULT 'indefinite' CHECK (retention IN (
        'session_only', 'ttl', 'indefinite'
    )),
    retention_expires_at TIMESTAMPTZ,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload) = 'object'),
    created_by_actor_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(created_by_actor_id) BETWEEN 1 AND 128),
    created_at TIMESTAMPTZ NOT NULL,
    shared_proposal_id TEXT,
    CHECK (scope <> 'family_shared' OR family_space_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_memory_records_subject
ON memory_records(subject_id, scope, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_records_family
ON memory_records(family_space_id, scope, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_records_co_subject
ON memory_records USING GIN (co_subject_ids);
-- At most one promoted record per shared proposal (fifth review): the
-- atomic vote+promotion path depends on this uniqueness.
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_records_shared_proposal
ON memory_records(shared_proposal_id) WHERE shared_proposal_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS memory_status_events (
    event_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL REFERENCES memory_records(record_id),
    status TEXT NOT NULL CHECK (status IN (
        'candidate', 'confirmed', 'disputed', 'revoked'
    )),
    reason_code TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_status_events_record
ON memory_status_events(record_id, created_at);

CREATE TABLE IF NOT EXISTS memory_shared_proposals (
    proposal_id TEXT PRIMARY KEY,
    family_space_id TEXT NOT NULL CHECK (char_length(family_space_id) BETWEEN 1 AND 128),
    proposer_subject_id TEXT NOT NULL,
    co_subject_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(co_subject_ids) = 'array'),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    -- Immutable fence snapshot the proposal was issued under (main
    -- architecture review): the final confirmation re-verifies receipt /
    -- consent / membership against exactly this fence.
    session_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(session_id) BETWEEN 1 AND 128),
    epoch INTEGER NOT NULL DEFAULT 1 CHECK (epoch >= 1),
    binding_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_role TEXT NOT NULL DEFAULT ''
        CHECK (char_length(binding_role) BETWEEN 1 AND 64),
    runtime_profile_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(runtime_profile_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(device_id) BETWEEN 0 AND 128),
    subject_revision INTEGER NOT NULL DEFAULT 0 CHECK (subject_revision >= 0),
    generation_id TEXT,
    turn_id INTEGER CHECK (turn_id >= 1),
    valid_until TIMESTAMPTZ,
    fence_context_hash TEXT NOT NULL DEFAULT ''
        CHECK (char_length(fence_context_hash) BETWEEN 1 AND 128),
    title TEXT NOT NULL CHECK (char_length(title) BETWEEN 1 AND 256),
    content TEXT NOT NULL CHECK (char_length(content) BETWEEN 1 AND 16384),
    source_evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(source_evidence_ids) = 'array'),
    proposal_policy_receipt_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(proposal_policy_receipt_id) BETWEEN 1 AND 128),
    consent_snapshot_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(consent_snapshot_id) BETWEEN 1 AND 128),
    -- Canonical exact-action evidence (PolicyActionResourceFence contract):
    -- proposal revision, capture evidence digest and consent/membership
    -- snapshot id+revision+hash are the AUTHORITATIVE values the final
    -- promotion fence must match field-by-field.
    proposal_revision INTEGER NOT NULL DEFAULT 1 CHECK (proposal_revision >= 1),
    capture_evidence_hash TEXT NOT NULL DEFAULT ''
        CHECK (char_length(capture_evidence_hash) IN (0, 64)),
    consent_snapshot_revision INTEGER NOT NULL DEFAULT 0
        CHECK (consent_snapshot_revision >= 0),
    consent_snapshot_hash TEXT NOT NULL DEFAULT ''
        CHECK (char_length(consent_snapshot_hash) IN (0, 64)),
    membership_snapshot_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(membership_snapshot_id) BETWEEN 0 AND 128),
    membership_snapshot_revision INTEGER NOT NULL DEFAULT 0
        CHECK (membership_snapshot_revision >= 0),
    membership_snapshot_hash TEXT NOT NULL DEFAULT ''
        CHECK (char_length(membership_snapshot_hash) IN (0, 64)),
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0),
    tool_epoch INTEGER NOT NULL DEFAULT 0 CHECK (tool_epoch >= 0),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'approvals_complete', 'promoted', 'frozen', 'withdrawn'
    )),
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memory_proposals_family
ON memory_shared_proposals(family_space_id, status, created_at);

-- The FK is added only after its target table exists so a fresh database
-- initializes in one pass (P0-1; see the schema-order guard test).
ALTER TABLE memory_records DROP CONSTRAINT IF EXISTS memory_records_shared_proposal_fk;
ALTER TABLE memory_records ADD CONSTRAINT memory_records_shared_proposal_fk
    FOREIGN KEY (shared_proposal_id) REFERENCES memory_shared_proposals(proposal_id);

CREATE TABLE IF NOT EXISTS memory_shared_votes (
    proposal_id TEXT NOT NULL REFERENCES memory_shared_proposals(proposal_id),
    subject_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('confirm', 'object')),
    voted_at TIMESTAMPTZ NOT NULL,
    evidence_id TEXT NOT NULL DEFAULT '',
    -- Per-vote approval evidence (THREE authority actions): confirms MUST
    -- carry a non-empty family_shared_memory_approval receipt id,
    -- objections MUST be empty.  The final promotion receipt belongs to
    -- the finalizer action only and is never stored on a vote.
    approval_receipt_id TEXT NOT NULL DEFAULT '',
    -- Canonical per-vote approval snapshot triple (PolicyApprovalSnapshotFence
    -- contract): the promotion fence's approval_snapshots must
    -- record-set-equal the persisted votes on subject/snapshot/revision/hash.
    approval_snapshot_id TEXT NOT NULL DEFAULT '',
    approval_snapshot_revision INTEGER NOT NULL DEFAULT 0
        CHECK (approval_snapshot_revision >= 0),
    approval_snapshot_hash TEXT NOT NULL DEFAULT ''
        CHECK (char_length(approval_snapshot_hash) IN (0, 64)),
    CHECK (
        (decision = 'confirm'
            AND char_length(approval_receipt_id) BETWEEN 1 AND 128
            AND char_length(approval_snapshot_id) BETWEEN 1 AND 128
            AND approval_snapshot_revision >= 1
            AND char_length(approval_snapshot_hash) = 64)
        OR (decision = 'object' AND approval_receipt_id = ''
            AND approval_snapshot_id = '' AND approval_snapshot_revision = 0
            AND approval_snapshot_hash = '')
    ),
    -- Append-only superseding votes (main review): the same subject may
    -- first confirm and then object (BOTH rows are kept; the latest
    -- decision wins), so approvals_complete never locks objection out.
    -- A duplicate (subject, decision) pair is still rejected.
    PRIMARY KEY (proposal_id, subject_id, decision)
);

CREATE TABLE IF NOT EXISTS memory_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processed')),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_outbox_pending
ON memory_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS memory_audit_events (
    event_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    actor_subject_id TEXT NOT NULL,
    subject_id TEXT,
    record_id TEXT,
    proposal_id TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_audit_record
ON memory_audit_events(record_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memory_audit_proposal
ON memory_audit_events(proposal_id, created_at);

-- Append-only guards: nobody may update or delete durable rows.
CREATE OR REPLACE FUNCTION memory_records_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'memory_records is append-only';
END;
$$;

CREATE OR REPLACE FUNCTION memory_status_events_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'memory_status_events is append-only';
END;
$$;

CREATE OR REPLACE FUNCTION memory_votes_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'memory_shared_votes is append-only';
END;
$$;

DROP TRIGGER IF EXISTS trg_memory_records_immutable ON memory_records;
CREATE TRIGGER trg_memory_records_immutable
BEFORE UPDATE OR DELETE ON memory_records
FOR EACH ROW EXECUTE FUNCTION memory_records_immutable();

DROP TRIGGER IF EXISTS trg_memory_status_events_immutable ON memory_status_events;
CREATE TRIGGER trg_memory_status_events_immutable
BEFORE UPDATE OR DELETE ON memory_status_events
FOR EACH ROW EXECUTE FUNCTION memory_status_events_immutable();

DROP TRIGGER IF EXISTS trg_memory_votes_immutable ON memory_shared_votes;
CREATE TRIGGER trg_memory_votes_immutable
BEFORE UPDATE OR DELETE ON memory_shared_votes
FOR EACH ROW EXECUTE FUNCTION memory_votes_immutable();

-- ---------------------------------------------------------------------------
-- Row-level security: transaction-level app context, minimal policies.
-- The adapter sets app.memory.* inside each transaction; with FORCE RLS a
-- missing context fails closed at the database level too.
-- ---------------------------------------------------------------------------

ALTER TABLE memory_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_status_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_shared_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_shared_votes ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_audit_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_records FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_status_events FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_shared_proposals FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_shared_votes FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_audit_events FORCE ROW LEVEL SECURITY;

DO $memory_policy$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_memory_api') AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_memory_worker') THEN
        -- P0-2: real role separation; GUCs only carry row context.
        GRANT SELECT, INSERT ON memory_records
            TO memoria_memory_api, memoria_memory_worker;
        GRANT SELECT, INSERT ON memory_status_events
            TO memoria_memory_api, memoria_memory_worker;
        GRANT SELECT, INSERT, UPDATE ON memory_shared_proposals
            TO memoria_memory_api, memoria_memory_worker;
        GRANT SELECT, INSERT ON memory_shared_votes
            TO memoria_memory_api, memoria_memory_worker;
        GRANT INSERT ON memory_audit_events
            TO memoria_memory_api, memoria_memory_worker;
        GRANT SELECT ON memory_audit_events
            TO memoria_memory_worker;
        GRANT INSERT ON memory_outbox
            TO memoria_memory_api, memoria_memory_worker;
        GRANT SELECT, UPDATE ON memory_outbox
            TO memoria_memory_worker;

        -- memory_records: an actor sees rows they are subject / owner /
        -- co-subject of; writes must be for the fenced subject by the fenced
        -- actor inside the fenced family space.
        DROP POLICY IF EXISTS memory_records_select ON memory_records;
        CREATE POLICY memory_records_select ON memory_records FOR SELECT
            TO memoria_memory_api, memoria_memory_worker
            USING (
                (
                    subject_id = current_setting('app.memory.actor_subject_id', true)
                    OR resource_owner_id = current_setting('app.memory.actor_subject_id', true)
                    OR co_subject_ids @> to_jsonb(
                        current_setting('app.memory.actor_subject_id', true)
                    )
                )
                AND (
                    (
                        family_space_id IS NULL
                        AND current_setting('app.memory.family_space_id', true) IS NULL
                    )
                    OR family_space_id = current_setting(
                        'app.memory.family_space_id', true
                    )
                )
                OR (
                    -- Guardian/legacy grant read (fifth review): the service
                    -- sets these contexts ONLY after authoritative grant
                    -- verification; the scope is pinned so a grant can never
                    -- read outside its scope.
                    resource_owner_id = current_setting(
                        'app.memory.grant_owner_id', true
                    )
                    AND scope = current_setting('app.memory.grant_scope', true)
                )
            );

        DROP POLICY IF EXISTS memory_records_insert ON memory_records;
        CREATE POLICY memory_records_insert ON memory_records FOR INSERT
            TO memoria_memory_api, memoria_memory_worker, memoria_memory_owner
            WITH CHECK (
                subject_id = current_setting('app.memory.subject_id', true)
                AND created_by_actor_id = current_setting(
                    'app.memory.actor_subject_id', true
                )
                AND (
                    family_space_id IS NULL
                    OR family_space_id = current_setting(
                        'app.memory.family_space_id', true
                    )
                )
            );

        DROP POLICY IF EXISTS memory_records_update ON memory_records;
        CREATE POLICY memory_records_update ON memory_records FOR UPDATE
            TO memoria_memory_api, memoria_memory_worker
            USING (
                (
                    subject_id = current_setting('app.memory.actor_subject_id', true)
                    OR resource_owner_id = current_setting('app.memory.actor_subject_id', true)
                )
                AND (
                    (
                        family_space_id IS NULL
                        AND current_setting('app.memory.family_space_id', true) IS NULL
                    )
                    OR family_space_id = current_setting(
                        'app.memory.family_space_id', true
                    )
                )
            )
            WITH CHECK (
                subject_id = current_setting('app.memory.subject_id', true)
                AND created_by_actor_id = current_setting(
                    'app.memory.actor_subject_id', true
                )
                AND (
                    (
                        family_space_id IS NULL
                        AND current_setting('app.memory.family_space_id', true) IS NULL
                    )
                    OR family_space_id = current_setting(
                        'app.memory.family_space_id', true
                    )
                )
            );

        -- status events: only for records the actor can see (record rows are
        -- themselves RLS-filtered, so the subquery stays actor-scoped).
        DROP POLICY IF EXISTS memory_status_events_select ON memory_status_events;
        CREATE POLICY memory_status_events_select ON memory_status_events FOR SELECT
            TO memoria_memory_api, memoria_memory_worker
            USING (
                EXISTS (
                    SELECT 1 FROM memory_records mr
                    WHERE mr.record_id = memory_status_events.record_id
                )
            );

        DROP POLICY IF EXISTS memory_status_events_insert ON memory_status_events;
        CREATE POLICY memory_status_events_insert ON memory_status_events FOR INSERT
            TO memoria_memory_api, memoria_memory_worker
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM memory_records mr
                    WHERE mr.record_id = memory_status_events.record_id
                )
            );

        DROP POLICY IF EXISTS memory_status_events_update ON memory_status_events;
        CREATE POLICY memory_status_events_update ON memory_status_events FOR UPDATE
            TO memoria_memory_api, memoria_memory_worker
            USING (
                EXISTS (
                    SELECT 1 FROM memory_records mr
                    WHERE mr.record_id = memory_status_events.record_id
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM memory_records mr
                    WHERE mr.record_id = memory_status_events.record_id
                )
            );

        -- proposals: proposer or listed co-subject may read; only the
        -- proposer may insert; updates keep the actor as proposer/co-subject.
        DROP POLICY IF EXISTS memory_proposals_select ON memory_shared_proposals;
        CREATE POLICY memory_proposals_select ON memory_shared_proposals FOR SELECT
            TO memoria_memory_api, memoria_memory_worker
            USING (
                (
                    proposer_subject_id = current_setting('app.memory.actor_subject_id', true)
                    OR co_subject_ids @> to_jsonb(
                        current_setting('app.memory.actor_subject_id', true)
                    )
                )
                AND (
                    family_space_id = current_setting(
                        'app.memory.family_space_id', true
                    )
                )
            );

        DROP POLICY IF EXISTS memory_proposals_insert ON memory_shared_proposals;
        CREATE POLICY memory_proposals_insert ON memory_shared_proposals FOR INSERT
            TO memoria_memory_api, memoria_memory_worker
            WITH CHECK (
                proposer_subject_id = current_setting('app.memory.actor_subject_id', true)
            );

        DROP POLICY IF EXISTS memory_proposals_update ON memory_shared_proposals;
        CREATE POLICY memory_proposals_update ON memory_shared_proposals FOR UPDATE
            TO memoria_memory_api, memoria_memory_worker
            USING (
                (
                    proposer_subject_id = current_setting('app.memory.actor_subject_id', true)
                    OR co_subject_ids @> to_jsonb(
                        current_setting('app.memory.actor_subject_id', true)
                    )
                )
                AND (
                    family_space_id = current_setting(
                        'app.memory.family_space_id', true
                    )
                )
            )
            WITH CHECK (
                (
                    proposer_subject_id = current_setting('app.memory.actor_subject_id', true)
                    OR co_subject_ids @> to_jsonb(
                        current_setting('app.memory.actor_subject_id', true)
                    )
                )
                AND (
                    family_space_id = current_setting(
                        'app.memory.family_space_id', true
                    )
                )
            );

        -- votes: only for proposals the actor is part of; only the actor's
        -- own vote can be inserted.
        DROP POLICY IF EXISTS memory_votes_select ON memory_shared_votes;
        CREATE POLICY memory_votes_select ON memory_shared_votes FOR SELECT
            TO memoria_memory_api, memoria_memory_worker
            USING (
                EXISTS (
                    SELECT 1 FROM memory_shared_proposals p
                    WHERE p.proposal_id = memory_shared_votes.proposal_id
                )
            );

        DROP POLICY IF EXISTS memory_votes_insert ON memory_shared_votes;
        CREATE POLICY memory_votes_insert ON memory_shared_votes FOR INSERT
            TO memoria_memory_api, memoria_memory_worker
            WITH CHECK (
                subject_id = current_setting('app.memory.actor_subject_id', true)
            );

        DROP POLICY IF EXISTS memory_votes_update ON memory_shared_votes;
        CREATE POLICY memory_votes_update ON memory_shared_votes FOR UPDATE
            TO memoria_memory_api, memoria_memory_worker
            USING (subject_id = current_setting('app.memory.actor_subject_id', true))
            WITH CHECK (subject_id = current_setting('app.memory.actor_subject_id', true));

        -- P1-7: outbox/audit split by command - API may only append inside
        -- its atomic transactions; the worker polls/updates the outbox and
        -- reads audit; nobody global-scans business content via these.
        DROP POLICY IF EXISTS memory_outbox_insert ON memory_outbox;
        CREATE POLICY memory_outbox_insert ON memory_outbox FOR INSERT
            TO memoria_memory_api, memoria_memory_worker
            WITH CHECK (
                pg_has_role(current_user, 'memoria_memory_worker', 'member')
                OR pg_has_role(current_user, 'memoria_memory_api', 'member')
            );

        DROP POLICY IF EXISTS memory_outbox_select ON memory_outbox;
        CREATE POLICY memory_outbox_select ON memory_outbox FOR SELECT
            TO memoria_memory_api, memoria_memory_worker
            USING (
                pg_has_role(current_user, 'memoria_memory_worker', 'member')
            );

        DROP POLICY IF EXISTS memory_outbox_update ON memory_outbox;
        CREATE POLICY memory_outbox_update ON memory_outbox FOR UPDATE
            TO memoria_memory_api, memoria_memory_worker
            USING (
                pg_has_role(current_user, 'memoria_memory_worker', 'member')
            )
            WITH CHECK (
                pg_has_role(current_user, 'memoria_memory_worker', 'member')
            );

        DROP POLICY IF EXISTS memory_audit_insert ON memory_audit_events;
        CREATE POLICY memory_audit_insert ON memory_audit_events FOR INSERT
            TO memoria_memory_api, memoria_memory_worker
            WITH CHECK (
                pg_has_role(current_user, 'memoria_memory_worker', 'member')
                OR pg_has_role(current_user, 'memoria_memory_api', 'member')
            );

        DROP POLICY IF EXISTS memory_audit_select ON memory_audit_events;
        CREATE POLICY memory_audit_select ON memory_audit_events FOR SELECT
            TO memoria_memory_api, memoria_memory_worker
            USING (
                pg_has_role(current_user, 'memoria_memory_worker', 'member')
            );
    END IF;
END
$memory_policy$;

-- Narrow SECURITY DEFINER sensitive-commit function (cross-domain
-- same-connection supplement): the Policy SensitiveWriteService's write
-- callback runs inside a caller-owned NOBYPASSRLS transaction and invokes
-- ONLY this single-action function - the API role is NEVER granted table
-- UPDATE/INSERT privileges on memory_records and never reads Policy/Consent
-- tables directly.  The function fixes search_path, verifies the
-- session-local app.memory.* context (actor/subject/family) against its
-- arguments (fail closed on mismatch) and inserts ONE memory row.
CREATE OR REPLACE FUNCTION memory_sensitive_commit(
    p_record_id TEXT,
    p_scope TEXT,
    p_subject_id TEXT,
    p_resource_owner_id TEXT,
    p_family_space_id TEXT,
    p_co_subject_ids JSONB,
    p_source_evidence_ids JSONB,
    p_policy_receipt_id TEXT,
    p_consent_snapshot_id TEXT,
    p_created_by_actor_id TEXT
) RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET lock_timeout = '5s'
AS $$
DECLARE
    v_actor TEXT := current_setting('app.memory.actor_subject_id', true);
    v_subject TEXT := current_setting('app.memory.subject_id', true);
    v_family TEXT := current_setting('app.memory.family_space_id', true);
BEGIN
    IF v_actor IS NULL OR v_actor = '' THEN
        RAISE EXCEPTION 'memory_sensitive_commit: actor context missing (fail closed)';
    END IF;
    IF v_actor <> p_created_by_actor_id THEN
        RAISE EXCEPTION 'memory_sensitive_commit: actor mismatch (fail closed)';
    END IF;
    IF v_subject IS NULL OR v_subject <> p_subject_id THEN
        RAISE EXCEPTION 'memory_sensitive_commit: subject mismatch (fail closed)';
    END IF;
    IF p_family_space_id IS NOT NULL
       AND (v_family IS NULL OR v_family <> p_family_space_id) THEN
        RAISE EXCEPTION 'memory_sensitive_commit: family mismatch (fail closed)';
    END IF;
    INSERT INTO memory_records (
        record_id, scope, subject_id, resource_owner_id, family_space_id,
        co_subject_ids, source_evidence_ids, policy_receipt_id,
        consent_snapshot_id, memory_type, confidence, retention,
        payload, created_by_actor_id, created_at
    ) VALUES (
        p_record_id, p_scope, p_subject_id, p_resource_owner_id,
        p_family_space_id, p_co_subject_ids, p_source_evidence_ids,
        p_policy_receipt_id, p_consent_snapshot_id,
        'semantic', 1.0, 'indefinite', '{}'::jsonb,
        p_created_by_actor_id, now()
    );
    RETURN p_record_id;
END;
$$;

REVOKE ALL ON FUNCTION memory_sensitive_commit FROM PUBLIC;
-- Only the NARROW action-executor role may invoke the sensitive commit:
-- the API/worker roles have NO function privilege and NO direct table
-- grants, so the same-connection executor is the single write path.
GRANT EXECUTE ON FUNCTION memory_sensitive_commit
    TO memoria_action_executor;

-- One policy receipt may produce AT MOST ONE durable memory record: a
-- replayed/tampered write with the same receipt (different content,
-- evidence or idempotency) is rejected by the unique constraint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_records_policy_receipt
ON memory_records(policy_receipt_id) WHERE policy_receipt_id <> '';

-- PR-12 capture ports.  The action executor never receives table privileges;
-- these ports are the only capture write boundary.  Python derives
-- ``capture:<canonical_action_sha256>`` from the contract's canonical JSON.
-- The lock port cannot reconstruct that JSON from its deliberately narrow
-- signature, so it validates the capture prefix/sha256 shape and the full
-- subject/evidence/resource lock; the commit port cross-checks the same
-- canonical hash against the stored Policy receipt and action fence.
CREATE OR REPLACE FUNCTION memory_capture_lock_action_resource(
    p_capability TEXT,
    p_action_resource_id TEXT,
    p_subject_id TEXT,
    p_capture_evidence_ids TEXT[],
    p_content_sha256 TEXT,
    p_action_revision INTEGER
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_capture_lock_action_resource$
DECLARE
    expected_resource_id TEXT;
    v_evidence_id TEXT;
    evidence_count INTEGER;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'memory capture resource lock requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF p_capability IS DISTINCT FROM 'memory_capture'
       OR p_subject_id IS NULL OR btrim(p_subject_id) = ''
       OR p_content_sha256 IS NULL
       OR p_content_sha256 !~ '^[a-f0-9]{64}$'
       OR p_action_revision IS NULL OR p_action_revision < 1
       OR p_capture_evidence_ids IS NULL
       OR cardinality(p_capture_evidence_ids) < 1 THEN
        RAISE EXCEPTION 'invalid memory capture action resource'
            USING ERRCODE = 'SR400';
    END IF;
    IF NULLIF(current_setting('app.memory.actor_subject_id', true), '')
            IS DISTINCT FROM p_subject_id
       OR NULLIF(current_setting('app.memory.subject_id', true), '')
            IS DISTINCT FROM p_subject_id THEN
        RAISE EXCEPTION 'memory capture subject context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    IF (SELECT count(*) FROM unnest(p_capture_evidence_ids))
            <> (SELECT count(DISTINCT item) FROM unnest(p_capture_evidence_ids) AS item) THEN
        RAISE EXCEPTION 'memory capture evidence ids must be unique'
            USING ERRCODE = 'SR400';
    END IF;
    expected_resource_id := split_part(p_action_resource_id, ':', 1);
    IF expected_resource_id IS DISTINCT FROM 'capture'
       OR p_action_resource_id !~ '^capture:[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'memory capture action resource binding mismatch'
            USING ERRCODE = 'SR412';
    END IF;

    -- Serialize the first writer for this canonical resource before checking
    -- the append-only table.  A retry therefore cannot race the NOT EXISTS
    -- check into producing a second record.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_action_resource_id, 0));
    IF EXISTS (
        SELECT 1 FROM memory_records
        WHERE payload ->> 'canonical_action_sha256'
            = substring(p_action_resource_id from 9)
    ) THEN
        RAISE EXCEPTION 'memory capture action resource is already written'
            USING ERRCODE = 'SR409';
    END IF;

    FOREACH v_evidence_id IN ARRAY p_capture_evidence_ids LOOP
        SELECT count(*) INTO evidence_count
        FROM memory_capture_evidence
        WHERE memory_capture_evidence.evidence_id = v_evidence_id
          AND subject_id = p_subject_id
          AND status = 'active'
          AND valid_from <= statement_timestamp()
          AND statement_timestamp() < valid_until;
        IF evidence_count <> 1 THEN
            RAISE EXCEPTION 'memory capture evidence is unavailable'
                USING ERRCODE = 'SR503';
        END IF;
        PERFORM 1
        FROM memory_capture_evidence
        WHERE memory_capture_evidence.evidence_id = v_evidence_id
          AND subject_id = p_subject_id
          AND status = 'active'
          AND valid_from <= statement_timestamp()
          AND statement_timestamp() < valid_until
        ORDER BY revision DESC
        LIMIT 1
        FOR UPDATE;
    END LOOP;
    RETURN TRUE;
END
$memory_capture_lock_action_resource$;

CREATE OR REPLACE FUNCTION memory_capture_commit(p_payload JSONB)
RETURNS TEXT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_capture_commit$
DECLARE
    receipt JSONB;
    fence JSONB;
    actor TEXT := NULLIF(current_setting('app.memory.actor_subject_id', true), '');
    subject TEXT := NULLIF(current_setting('app.memory.subject_id', true), '');
    family TEXT := NULLIF(current_setting('app.memory.family_space_id', true), '');
    record_id TEXT := p_payload ->> 'record_id';
    content_sha256 TEXT := p_payload ->> 'content_sha256';
    evidence_ids TEXT[];
    fence_evidence_ids TEXT[];
    action_revision INTEGER;
    obligation_count INTEGER;
    retention_ttl_seconds INTEGER;
    expected_retention_expires_at TIMESTAMPTZ;
    supplied_retention_expires_at TIMESTAMPTZ;
    now_value TIMESTAMPTZ := clock_timestamp();
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'memory capture commit requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF actor IS NULL OR subject IS NULL OR p_payload IS NULL
       OR jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'memory capture context or payload is missing'
            USING ERRCODE = 'SR403';
    END IF;
    receipt := action_policy_lock_receipt(p_payload ->> 'policy_receipt_id');
    IF receipt IS NULL THEN
        RAISE EXCEPTION 'memory capture policy receipt is unavailable'
            USING ERRCODE = 'SR403';
    END IF;
    fence := receipt -> 'action_resource_fence';
    IF (receipt ->> 'effect') NOT IN ('allow', 'allow_with_obligations')
       OR receipt ->> 'capability' IS DISTINCT FROM 'memory_capture'
       OR receipt ->> 'purpose' IS DISTINCT FROM 'memory_capture'
       OR receipt ->> 'actor_id' IS DISTINCT FROM actor
       OR receipt ->> 'subject_id' IS DISTINCT FROM subject
       OR receipt ->> 'resource_owner_id' IS DISTINCT FROM subject
       OR p_payload ->> 'policy_receipt_id' IS DISTINCT FROM receipt ->> 'receipt_id'
       OR p_payload -> 'action_resource_fence' IS DISTINCT FROM fence
       OR receipt ->> 'action_fence_hash' IS DISTINCT FROM fence ->> 'canonical_hash'
       OR (receipt ->> 'expires_at')::TIMESTAMPTZ <= now_value THEN
        RAISE EXCEPTION 'memory capture policy receipt or fence mismatch'
            USING ERRCODE = 'SR412';
    END IF;
    IF fence ->> 'capability' IS DISTINCT FROM 'memory_capture'
       OR fence ->> 'purpose' IS DISTINCT FROM 'memory_capture'
       OR fence ->> 'action_resource_id'
            IS DISTINCT FROM p_payload ->> 'action_resource_id'
       OR fence ->> 'action_resource_id'
            IS DISTINCT FROM 'capture:' || (p_payload ->> 'canonical_action_sha256')
       OR p_payload ->> 'canonical_action_sha256'
            IS DISTINCT FROM substring(fence ->> 'action_resource_id' from 9)
       OR p_payload ->> 'canonical_action_sha256' !~ '^[a-f0-9]{64}$'
       OR fence ->> 'action_revision' IS NULL
       OR fence ->> 'family_space_id' IS NOT NULL
       OR fence ->> 'family_owner_subject_id' IS NOT NULL
       OR fence ->> 'proposal_id' IS NOT NULL
       OR fence ->> 'voter_subject_id' IS NOT NULL
       OR fence ->> 'approval_decision' IS NOT NULL THEN
        RAISE EXCEPTION 'memory capture action fence is not canonical'
            USING ERRCODE = 'SR412';
    END IF;
    IF record_id IS NULL OR btrim(record_id) = ''
       OR p_payload ->> 'subject_id' IS DISTINCT FROM subject
       OR p_payload ->> 'created_by_actor_id' IS DISTINCT FROM actor
       OR p_payload ->> 'resource_owner_id'
            IS DISTINCT FROM receipt ->> 'resource_owner_id'
       OR p_payload ->> 'family_space_id' IS DISTINCT FROM family
       OR p_payload -> 'co_subject_ids' IS NULL
       OR jsonb_typeof(p_payload -> 'co_subject_ids') <> 'array'
       OR p_payload ->> 'memory_type' IS DISTINCT FROM 'semantic'
       OR p_payload ->> 'scope' NOT IN (
            'personal_private', 'guardian_summary'
       )
       OR (
            p_payload ->> 'scope' = 'guardian_summary'
            AND NOT EXISTS (
                SELECT 1
                FROM jsonb_array_elements(receipt -> 'obligations') AS item
                WHERE item ->> 'code' = 'PERSIST_AGGREGATE_ONLY'
            )
       )
       OR p_payload ->> 'confidence' IS NULL
       OR (p_payload ->> 'confidence')::NUMERIC < 0.0
       OR (p_payload ->> 'confidence')::NUMERIC > 1.0
       OR p_payload ->> 'retention' IS DISTINCT FROM 'ttl'
       OR p_payload ->> 'retention_expires_at' IS NULL
       OR COALESCE(p_payload ->> 'promotion_receipt_id', '') <> ''
       OR COALESCE(p_payload ->> 'promotion_fence_context_hash', '') <> ''
       OR COALESCE(p_payload -> 'approval_evidence_refs', '[]'::jsonb)
            <> '[]'::jsonb
       OR COALESCE(p_payload ->> 'shared_proposal_id', '') <> '' THEN
        RAISE EXCEPTION 'memory capture record authority fields are invalid'
            USING ERRCODE = 'SR403';
    END IF;
    IF content_sha256 IS NULL OR content_sha256 !~ '^[a-f0-9]{64}$'
       OR p_payload -> 'payload' IS NULL
       OR jsonb_typeof(p_payload -> 'payload') <> 'object'
       OR p_payload ->> 'status' IS DISTINCT FROM 'confirmed' THEN
        RAISE EXCEPTION 'memory capture business payload is invalid'
            USING ERRCODE = 'SR400';
    END IF;
    SELECT count(*), max((item -> 'params' ->> 'retention_ttl_seconds')::INTEGER)
    INTO obligation_count, retention_ttl_seconds
    FROM jsonb_array_elements(receipt -> 'obligations') AS item
    WHERE item ->> 'code' = 'RETENTION_TTL';
    IF obligation_count <> 1
       OR retention_ttl_seconds IS NULL OR retention_ttl_seconds < 1 THEN
        RAISE EXCEPTION 'memory capture receipt must carry one RETENTION_TTL'
            USING ERRCODE = 'SR412';
    END IF;
    expected_retention_expires_at :=
        (receipt ->> 'created_at')::TIMESTAMPTZ
        + make_interval(secs => retention_ttl_seconds);
    supplied_retention_expires_at :=
        (p_payload ->> 'retention_expires_at')::TIMESTAMPTZ;
    IF abs(extract(epoch FROM (
            supplied_retention_expires_at - expected_retention_expires_at
       ))) > 1 THEN
        RAISE EXCEPTION 'memory capture retention expiry is not receipt-derived'
            USING ERRCODE = 'SR412';
    END IF;
    action_revision := (fence ->> 'action_revision')::INTEGER;
    evidence_ids := ARRAY(
        SELECT jsonb_array_elements_text(p_payload -> 'source_evidence_ids')
    );
    fence_evidence_ids := ARRAY(
        SELECT jsonb_array_elements_text(fence -> 'capture_evidence_ids')
    );
    IF p_payload -> 'source_evidence_ids' IS NULL
       OR jsonb_typeof(p_payload -> 'source_evidence_ids') <> 'array'
       OR evidence_ids IS NULL OR cardinality(evidence_ids) < 1
       OR evidence_ids IS DISTINCT FROM fence_evidence_ids
       OR p_payload ->> 'consent_snapshot_id'
            IS DISTINCT FROM fence ->> 'consent_snapshot_id' THEN
        RAISE EXCEPTION 'memory capture evidence or consent fence mismatch'
            USING ERRCODE = 'SR412';
    END IF;
    IF memory_capture_lock_action_resource(
        'memory_capture', p_payload ->> 'action_resource_id', subject, evidence_ids,
        content_sha256, action_revision
    ) IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'memory capture resource lock failed'
            USING ERRCODE = 'SR409';
    END IF;

    INSERT INTO memory_records (
        record_id, scope, subject_id, resource_owner_id, family_space_id,
        co_subject_ids, source_evidence_ids, policy_receipt_id,
        consent_snapshot_id, memory_type, confidence, retention,
        retention_expires_at, payload, created_by_actor_id, created_at
    ) VALUES (
        record_id, p_payload ->> 'scope', subject,
        receipt ->> 'resource_owner_id',
        NULLIF(p_payload ->> 'family_space_id', ''),
        p_payload -> 'co_subject_ids', p_payload -> 'source_evidence_ids',
        receipt ->> 'receipt_id', fence ->> 'consent_snapshot_id',
        'semantic', (p_payload ->> 'confidence')::NUMERIC, 'ttl',
        expected_retention_expires_at,
        (p_payload -> 'payload') || jsonb_build_object(
            'canonical_action_sha256', p_payload ->> 'canonical_action_sha256'
        ),
        actor, now_value
    );
    INSERT INTO memory_status_events (
        event_id, record_id, status, reason_code, created_at
    ) VALUES (
        record_id || ':captured', record_id, 'confirmed',
        'policy_authorized_capture', now_value
    );
    INSERT INTO memory_outbox (
        outbox_id, event_id, topic, payload, status, created_at
    ) VALUES (
        record_id || ':captured', record_id || ':captured',
        'memory.record.captured', p_payload, 'pending', now_value
    );
    INSERT INTO memory_audit_events (
        event_id, action, actor_subject_id, subject_id, record_id,
        proposal_id, payload, created_at
    ) VALUES (
        record_id || ':captured', 'memory.capture.committed', actor,
        subject, record_id, NULL, p_payload, now_value
    );
    RETURN record_id;
END
$memory_capture_commit$;

REVOKE ALL ON FUNCTION memory_capture_lock_action_resource(
    TEXT, TEXT, TEXT, TEXT[], TEXT, INTEGER
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_capture_commit(JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory_capture_lock_action_resource(
    TEXT, TEXT, TEXT, TEXT[], TEXT, INTEGER
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_capture_commit(JSONB)
    TO memoria_action_executor;

-- ---------------------------------------------------------------------------
-- Family-shared action-executor ports (PR-10/PR-12/PR-14).
--
-- The action executor deliberately receives no table privilege.  These two
-- append-only authority projections are written by the owning maintenance
-- process and are consumed only through the SECURITY DEFINER lock ports
-- below.  A family action therefore cannot turn an HTTP-supplied id into
-- evidence: the id must resolve to a current row while the caller-owned
-- transaction holds its row lock.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_shared_membership_snapshots (
    snapshot_id TEXT PRIMARY KEY CHECK (char_length(snapshot_id) BETWEEN 1 AND 128),
    family_space_id TEXT NOT NULL CHECK (char_length(family_space_id) BETWEEN 1 AND 128),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    canonical_hash TEXT NOT NULL CHECK (canonical_hash ~ '^[a-f0-9]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
    family_owner_subject_id TEXT NOT NULL
        CHECK (char_length(family_owner_subject_id) BETWEEN 1 AND 128),
    subject_ids JSONB NOT NULL CHECK (jsonb_typeof(subject_ids) = 'array'),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL CHECK (valid_until > valid_from),
    UNIQUE (family_space_id, binding_id, binding_version, revision)
);

CREATE TABLE IF NOT EXISTS memory_capture_evidence (
    evidence_id TEXT NOT NULL CHECK (char_length(evidence_id) BETWEEN 1 AND 256),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    canonical_hash TEXT NOT NULL CHECK (canonical_hash ~ '^[a-f0-9]{64}$'),
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL CHECK (valid_until > valid_from),
    PRIMARY KEY (evidence_id, revision)
);

ALTER TABLE memory_shared_membership_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_shared_membership_snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_capture_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_capture_evidence FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS memory_owner_membership_snapshots
    ON memory_shared_membership_snapshots;
CREATE POLICY memory_owner_membership_snapshots
    ON memory_shared_membership_snapshots
    FOR ALL TO memoria_memory_owner
    USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
    WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));
DROP POLICY IF EXISTS memory_owner_capture_evidence ON memory_capture_evidence;
CREATE POLICY memory_owner_capture_evidence
    ON memory_capture_evidence
    FOR ALL TO memoria_memory_owner
    USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
    WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));

-- Archive evidence projector.  The Archive compiler supplies only a
-- server-canonicalized candidate; this SECURITY DEFINER port independently
-- locks the current Session/Identity authority before it can create a Memory
-- capture evidence row.  No Archive/API role receives table privileges.
CREATE OR REPLACE FUNCTION memory_project_capture_evidence(p_payload JSONB)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_project_capture_evidence$
DECLARE
    profile JSONB;
    existing memory_capture_evidence%ROWTYPE;
    occurred_at TIMESTAMPTZ;
    profile_issued_at TIMESTAMPTZ;
    profile_expires_at TIMESTAMPTZ;
    expected_scope TEXT;
BEGIN
    IF NOT pg_has_role(
        session_user, 'memoria_action_executor', 'member'
    ) THEN
        RAISE EXCEPTION 'memory evidence projection requires action executor'
            USING ERRCODE = 'MC403';
    END IF;
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
       OR NOT p_payload ?& ARRAY[
            'active_subject_id', 'actor_id', 'binding_id',
            'binding_version', 'canonical_hash', 'content_sha256',
            'device_id', 'event_id', 'event_sequence', 'generation_id',
            'memory_scope', 'occurred_at', 'runtime_profile_id',
            'session_id', 'session_epoch', 'subject_revision',
            'tool_epoch', 'turn_id'
       ]
       OR p_payload - ARRAY[
            'active_subject_id', 'actor_id', 'binding_id',
            'binding_version', 'canonical_hash', 'content_sha256',
            'device_id', 'event_id', 'event_sequence', 'generation_id',
            'memory_scope', 'occurred_at', 'runtime_profile_id',
            'session_id', 'session_epoch', 'subject_revision',
            'tool_epoch', 'turn_id'
       ] <> '{}'::JSONB
       OR p_payload ->> 'canonical_hash' !~ '^[a-f0-9]{64}$'
       OR p_payload ->> 'content_sha256' !~ '^[a-f0-9]{64}$'
       OR p_payload ->> 'binding_version' !~ '^[1-9][0-9]*$'
       OR p_payload ->> 'session_epoch' !~ '^[1-9][0-9]*$'
       OR p_payload ->> 'subject_revision' !~ '^[0-9]+$'
       OR p_payload ->> 'event_sequence' !~ '^[1-9][0-9]*$'
       OR p_payload ->> 'generation_id' !~ '^[0-9]+$'
       OR p_payload ->> 'turn_id' !~ '^[0-9]+$'
       OR p_payload ->> 'tool_epoch' !~ '^[0-9]+$'
       OR p_payload ->> 'memory_scope' NOT IN (
            'personal_private', 'guardian_summary', 'family_shared'
       ) THEN
        RAISE EXCEPTION 'memory evidence projection payload is invalid'
            USING ERRCODE = 'MC403';
    END IF;
    IF NULLIF(p_payload ->> 'active_subject_id', '') IS NULL
       OR NULLIF(p_payload ->> 'actor_id', '') IS NULL
       OR NULLIF(p_payload ->> 'binding_id', '') IS NULL
       OR NULLIF(p_payload ->> 'device_id', '') IS NULL
       OR NULLIF(p_payload ->> 'event_id', '') IS NULL
       OR NULLIF(p_payload ->> 'runtime_profile_id', '') IS NULL
       OR NULLIF(p_payload ->> 'session_id', '') IS NULL THEN
        RAISE EXCEPTION 'memory evidence projection identity is incomplete'
            USING ERRCODE = 'MC403';
    END IF;

    PERFORM session_runtime_assert_action_context(
        p_payload ->> 'session_id',
        p_payload ->> 'runtime_profile_id', p_payload ->> 'actor_id',
        p_payload ->> 'device_id', p_payload ->> 'binding_id',
        (p_payload ->> 'binding_version')::INTEGER,
        (p_payload ->> 'session_epoch')::INTEGER
    );
    profile := session_runtime_action_current_profile(
        p_payload ->> 'session_id'
    );
    IF profile IS NULL THEN
        RAISE EXCEPTION 'memory evidence Session profile is unavailable'
            USING ERRCODE = 'MC403';
    END IF;
    expected_scope := CASE profile ->> 'service_mode'
        WHEN 'adult_companion' THEN 'personal_private'
        WHEN 'student_minor' THEN 'personal_private'
        WHEN 'senior_companion' THEN 'personal_private'
        WHEN 'family_shared' THEN 'family_shared'
        ELSE NULL
    END;
    IF profile ->> 'session_id' IS DISTINCT FROM p_payload ->> 'session_id'
       OR profile ->> 'runtime_profile_id'
            IS DISTINCT FROM p_payload ->> 'runtime_profile_id'
       OR profile ->> 'actor_id' IS DISTINCT FROM p_payload ->> 'actor_id'
       OR profile ->> 'device_id' IS DISTINCT FROM p_payload ->> 'device_id'
       OR profile ->> 'binding_id' IS DISTINCT FROM p_payload ->> 'binding_id'
       OR profile ->> 'active_subject_id'
            IS DISTINCT FROM p_payload ->> 'active_subject_id'
       OR profile ->> 'speaker_state' IS DISTINCT FROM 'confirmed'
       OR expected_scope IS NULL
       OR expected_scope IS DISTINCT FROM p_payload ->> 'memory_scope'
       OR (profile ->> 'binding_version')::INTEGER
            IS DISTINCT FROM (p_payload ->> 'binding_version')::INTEGER
       OR (profile ->> 'session_epoch')::INTEGER
            IS DISTINCT FROM (p_payload ->> 'session_epoch')::INTEGER
       OR (profile ->> 'subject_revision')::INTEGER
            IS DISTINCT FROM (p_payload ->> 'subject_revision')::INTEGER THEN
        RAISE EXCEPTION 'memory evidence Session profile does not match candidate'
            USING ERRCODE = 'MC403';
    END IF;
    BEGIN
        occurred_at := (p_payload ->> 'occurred_at')::TIMESTAMPTZ;
        profile_issued_at := (profile ->> 'issued_at')::TIMESTAMPTZ;
        profile_expires_at := (profile ->> 'expires_at')::TIMESTAMPTZ;
    EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN
        RAISE EXCEPTION 'memory evidence timestamps are invalid'
            USING ERRCODE = 'MC403';
    END;
    IF occurred_at < profile_issued_at
       OR occurred_at >= profile_expires_at
       OR CURRENT_TIMESTAMP >= profile_expires_at THEN
        RAISE EXCEPTION 'memory evidence Session profile is outside validity'
            USING ERRCODE = 'MC403';
    END IF;

    SELECT * INTO existing
    FROM memory_capture_evidence
    WHERE evidence_id = p_payload ->> 'event_id' AND revision = 1
    FOR UPDATE;
    IF FOUND THEN
        IF existing.canonical_hash IS DISTINCT FROM p_payload ->> 'canonical_hash'
           OR existing.subject_id
                IS DISTINCT FROM p_payload ->> 'active_subject_id'
           OR existing.binding_id IS DISTINCT FROM p_payload ->> 'binding_id'
           OR existing.binding_version IS DISTINCT FROM
                (p_payload ->> 'binding_version')::INTEGER
           OR existing.valid_from IS DISTINCT FROM occurred_at
           OR existing.valid_until IS DISTINCT FROM profile_expires_at THEN
            RAISE EXCEPTION 'memory evidence idempotency conflict'
                USING ERRCODE = 'MC403';
        END IF;
        RETURN existing.status = 'active'
            AND existing.valid_from <= CURRENT_TIMESTAMP
            AND CURRENT_TIMESTAMP < existing.valid_until;
    END IF;

    INSERT INTO memory_capture_evidence (
        evidence_id, revision, canonical_hash, status, subject_id,
        binding_id, binding_version, valid_from, valid_until
    ) VALUES (
        p_payload ->> 'event_id', 1, p_payload ->> 'canonical_hash',
        'active', p_payload ->> 'active_subject_id',
        p_payload ->> 'binding_id',
        (p_payload ->> 'binding_version')::INTEGER,
        occurred_at, profile_expires_at
    );
    RETURN TRUE;
END
$memory_project_capture_evidence$;

REVOKE ALL ON FUNCTION memory_project_capture_evidence(JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory_project_capture_evidence(JSONB)
    TO memoria_action_executor;

-- The memory owner is the only identity allowed to execute the shared action
-- implementation.  Runtime logins can reach it only through the narrow ports.
DO $memory_shared_owner_policies$
BEGIN
    DROP POLICY IF EXISTS memory_owner_records ON memory_records;
    CREATE POLICY memory_owner_records ON memory_records
        FOR ALL TO memoria_memory_owner
        USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
        WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));
    DROP POLICY IF EXISTS memory_owner_status_events ON memory_status_events;
    CREATE POLICY memory_owner_status_events ON memory_status_events
        FOR ALL TO memoria_memory_owner
        USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
        WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));
    DROP POLICY IF EXISTS memory_owner_proposals ON memory_shared_proposals;
    CREATE POLICY memory_owner_proposals ON memory_shared_proposals
        FOR ALL TO memoria_memory_owner
        USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
        WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));
    DROP POLICY IF EXISTS memory_owner_votes ON memory_shared_votes;
    CREATE POLICY memory_owner_votes ON memory_shared_votes
        FOR ALL TO memoria_memory_owner
        USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
        WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));
    DROP POLICY IF EXISTS memory_owner_outbox ON memory_outbox;
    CREATE POLICY memory_owner_outbox ON memory_outbox
        FOR ALL TO memoria_memory_owner
        USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
        WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));
    DROP POLICY IF EXISTS memory_owner_audit ON memory_audit_events;
    CREATE POLICY memory_owner_audit ON memory_audit_events
        FOR ALL TO memoria_memory_owner
        USING (pg_has_role(current_user, 'memoria_memory_owner', 'member'))
        WITH CHECK (pg_has_role(current_user, 'memoria_memory_owner', 'member'));
END
$memory_shared_owner_policies$;

CREATE OR REPLACE FUNCTION memory_shared_lock_membership(
    p_family_space_id TEXT,
    p_subject_ids TEXT[],
    p_binding_id TEXT,
    p_binding_version INTEGER,
    p_now TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_lock_membership$
DECLARE
    membership_row memory_shared_membership_snapshots%ROWTYPE;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'shared membership lock requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF p_family_space_id IS NULL OR p_binding_id IS NULL
       OR p_binding_version IS NULL OR p_binding_version < 1
       OR p_now IS NULL OR p_subject_ids IS NULL
       OR cardinality(p_subject_ids) < 1 THEN
        RAISE EXCEPTION 'invalid membership action fence'
            USING ERRCODE = 'SR400';
    END IF;
    SELECT * INTO membership_row
    FROM memory_shared_membership_snapshots
    WHERE family_space_id = p_family_space_id
      AND binding_id = p_binding_id
      AND binding_version = p_binding_version
      AND status = 'active'
      AND valid_from <= p_now
      AND p_now < valid_until
    ORDER BY revision DESC
    LIMIT 1
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'current family membership is unavailable'
            USING ERRCODE = 'SR503';
    END IF;
    IF NOT membership_row.subject_ids @> to_jsonb(p_subject_ids) THEN
        RAISE EXCEPTION 'family membership does not cover action subjects'
            USING ERRCODE = 'SR403';
    END IF;
    RETURN jsonb_build_object(
        'snapshot_id', membership_row.snapshot_id,
        'revision', membership_row.revision,
        'canonical_hash', membership_row.canonical_hash,
        'status', membership_row.status,
        'family_space_id', membership_row.family_space_id,
        'family_owner_subject_id', membership_row.family_owner_subject_id,
        'subject_ids', membership_row.subject_ids,
        'binding_id', membership_row.binding_id,
        'binding_version', membership_row.binding_version,
        'valid_from', membership_row.valid_from,
        'valid_until', membership_row.valid_until
    );
END
$memory_shared_lock_membership$;

CREATE OR REPLACE FUNCTION memory_shared_lock_capture_evidence(
    p_evidence_ids TEXT[],
    p_subject_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER,
    p_now TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_lock_capture_evidence$
DECLARE
    v_evidence_id TEXT;
    evidence_row memory_capture_evidence%ROWTYPE;
    result JSONB := '[]'::jsonb;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'capture evidence lock requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF p_evidence_ids IS NULL OR cardinality(p_evidence_ids) < 1
       OR p_subject_id IS NULL OR p_binding_id IS NULL
       OR p_binding_version IS NULL OR p_binding_version < 1
       OR p_now IS NULL THEN
        RAISE EXCEPTION 'capture evidence is required'
            USING ERRCODE = 'SR400';
    END IF;
    IF (SELECT count(*) FROM unnest(p_evidence_ids))
           <> (SELECT count(DISTINCT item) FROM unnest(p_evidence_ids) AS item) THEN
        RAISE EXCEPTION 'capture evidence ids must be unique'
            USING ERRCODE = 'SR400';
    END IF;
    FOREACH v_evidence_id IN ARRAY p_evidence_ids LOOP
        SELECT * INTO evidence_row
        FROM memory_capture_evidence
        WHERE memory_capture_evidence.evidence_id = v_evidence_id
          AND subject_id = p_subject_id
          AND binding_id = p_binding_id
          AND binding_version = p_binding_version
          AND status = 'active'
          AND valid_from <= p_now
          AND p_now < valid_until
        ORDER BY revision DESC
        LIMIT 1
        FOR UPDATE;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'capture evidence is unavailable'
                USING ERRCODE = 'SR503';
        END IF;
        result := result || jsonb_build_array(jsonb_build_object(
            'evidence_id', evidence_row.evidence_id,
            'revision', evidence_row.revision,
            'canonical_hash', evidence_row.canonical_hash,
            'status', evidence_row.status,
            'subject_id', evidence_row.subject_id,
            'binding_id', evidence_row.binding_id,
            'binding_version', evidence_row.binding_version,
            'valid_from', evidence_row.valid_from,
            'valid_until', evidence_row.valid_until
        ));
    END LOOP;
    RETURN result;
END
$memory_shared_lock_capture_evidence$;

CREATE OR REPLACE FUNCTION memory_shared_lock_proposal(
    p_proposal_id TEXT,
    p_actor_subject_id TEXT,
    p_now TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_lock_proposal$
DECLARE
    proposal_row memory_shared_proposals%ROWTYPE;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'proposal lock requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF p_proposal_id IS NULL OR p_actor_subject_id IS NULL OR p_now IS NULL THEN
        RAISE EXCEPTION 'invalid proposal action fence'
            USING ERRCODE = 'SR400';
    END IF;
    SELECT * INTO proposal_row
    FROM memory_shared_proposals
    WHERE proposal_id = p_proposal_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shared proposal is unavailable'
            USING ERRCODE = 'SR404';
    END IF;
    IF proposal_row.proposer_subject_id <> p_actor_subject_id
       AND NOT proposal_row.co_subject_ids @> to_jsonb(p_actor_subject_id) THEN
        RAISE EXCEPTION 'actor is not a subject of the proposal'
            USING ERRCODE = 'SR403';
    END IF;
    RETURN to_jsonb(proposal_row);
END
$memory_shared_lock_proposal$;

CREATE OR REPLACE FUNCTION memory_shared_lock_approvals(
    p_proposal_id TEXT,
    p_now TIMESTAMPTZ
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_lock_approvals$
DECLARE
    vote_row memory_shared_votes%ROWTYPE;
    result JSONB := '[]'::jsonb;
    proposal_revision INTEGER;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'approval lock requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    SELECT proposal.proposal_revision INTO proposal_revision
    FROM memory_shared_proposals AS proposal
    WHERE proposal.proposal_id = p_proposal_id
    FOR UPDATE;
    IF NOT FOUND OR p_now IS NULL THEN
        RAISE EXCEPTION 'proposal approval authority is unavailable'
            USING ERRCODE = 'SR503';
    END IF;
    FOR vote_row IN
        SELECT * FROM memory_shared_votes
        WHERE proposal_id = p_proposal_id
        ORDER BY subject_id, voted_at, decision
        FOR UPDATE
    LOOP
        result := result || jsonb_build_array(jsonb_build_object(
            'proposal_id', vote_row.proposal_id,
            'subject_id', vote_row.subject_id,
            'decision', vote_row.decision,
            'evidence_id', vote_row.evidence_id,
            'approval_receipt_id', vote_row.approval_receipt_id,
            'snapshot_id', vote_row.approval_snapshot_id,
            'revision', vote_row.approval_snapshot_revision,
            'canonical_hash', vote_row.approval_snapshot_hash,
            'status', CASE WHEN vote_row.decision = 'confirm'
                THEN 'active' ELSE 'inactive' END,
            'proposal_revision', proposal_revision,
            'voted_at', vote_row.voted_at
        ));
    END LOOP;
    RETURN result;
END
$memory_shared_lock_approvals$;

CREATE OR REPLACE FUNCTION memory_shared_lock_action_resource(
    p_capability TEXT,
    p_action_resource_id TEXT,
    p_proposal_id TEXT,
    p_proposal_revision INTEGER
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_lock_action_resource$
DECLARE
    ignored BOOLEAN;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'action resource lock requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF p_capability = 'family_shared_memory_proposal' THEN
        IF p_action_resource_id IS NULL OR p_proposal_id IS DISTINCT FROM p_action_resource_id
           OR EXISTS (
               SELECT 1 FROM memory_shared_proposals
               WHERE proposal_id = p_action_resource_id
           ) THEN
            RAISE EXCEPTION 'new shared proposal resource is not available'
                USING ERRCODE = 'SR409';
        END IF;
        RETURN TRUE;
    END IF;
    SELECT TRUE INTO ignored
    FROM memory_shared_proposals
    WHERE proposal_id = p_action_resource_id
      AND proposal_id = p_proposal_id
      AND proposal_revision = p_proposal_revision
    FOR UPDATE;
    IF ignored IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'shared action resource fence is stale'
            USING ERRCODE = 'SR412';
    END IF;
    RETURN TRUE;
END
$memory_shared_lock_action_resource$;

CREATE OR REPLACE FUNCTION memory_shared_action_propose(p_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_action_propose$
DECLARE
    v_actor TEXT := p_payload ->> 'actor_subject_id';
    v_proposal_id TEXT := p_payload ->> 'proposal_id';
    v_family_space_id TEXT := p_payload ->> 'family_space_id';
    v_binding_id TEXT := p_payload ->> 'binding_id';
    v_binding_version INTEGER := (p_payload ->> 'binding_version')::INTEGER;
    v_subject_ids TEXT[];
    v_source_ids TEXT[];
    v_required_subject_ids TEXT[];
    v_membership JSONB;
    v_captures JSONB;
    receipt JSONB;
    fence JSONB := p_payload -> 'action_resource_fence';
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'shared propose requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF NULLIF(current_setting('app.authenticated_actor', true), '')
           IS DISTINCT FROM v_actor
       OR NULLIF(current_setting('app.authenticated_subject', true), '')
           IS DISTINCT FROM v_actor
       OR NULLIF(current_setting('app.authenticated_binding', true), '')
           IS DISTINCT FROM v_binding_id THEN
        RAISE EXCEPTION 'shared propose authenticated context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    receipt := action_policy_lock_receipt(
        p_payload ->> 'proposal_policy_receipt_id'
    );
    IF receipt IS NULL
       OR (receipt ->> 'effect') NOT IN ('allow', 'allow_with_obligations')
       OR receipt ->> 'capability' IS DISTINCT FROM 'family_shared_memory_proposal'
       OR receipt ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_proposal'
       OR receipt ->> 'actor_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'subject_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'resource_owner_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'action_fence_hash' IS DISTINCT FROM (fence ->> 'canonical_hash')
       OR p_payload -> 'action_resource_fence' IS DISTINCT FROM (receipt -> 'action_resource_fence')
       OR (receipt ->> 'expires_at')::TIMESTAMPTZ <= (p_payload ->> 'now')::TIMESTAMPTZ
       OR fence ->> 'capability' IS DISTINCT FROM 'family_shared_memory_proposal'
       OR fence ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_proposal'
       OR fence ->> 'action_resource_id' IS DISTINCT FROM v_proposal_id
       OR fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER <> 1
       OR fence ->> 'family_space_id' IS DISTINCT FROM v_family_space_id
       OR fence ->> 'family_owner_subject_id' IS DISTINCT FROM v_actor THEN
        RAISE EXCEPTION 'shared proposal receipt or action fence is invalid'
            USING ERRCODE = 'SR412';
    END IF;
    SELECT array_agg(value ORDER BY value)
    INTO v_subject_ids
    FROM jsonb_array_elements_text(COALESCE(p_payload -> 'co_subject_ids', '[]'::jsonb)) AS item(value);
    SELECT array_agg(value ORDER BY value)
    INTO v_source_ids
    FROM jsonb_array_elements_text(COALESCE(p_payload -> 'source_evidence_ids', '[]'::jsonb)) AS item(value);
    SELECT array_agg(DISTINCT value ORDER BY value)
    INTO v_required_subject_ids
    FROM unnest(ARRAY[v_actor] || COALESCE(v_subject_ids, ARRAY[]::TEXT[])) AS item(value);
    v_membership := memory_shared_lock_membership(
        v_family_space_id, v_required_subject_ids, v_binding_id, v_binding_version,
        (p_payload ->> 'now')::TIMESTAMPTZ
    );
    v_captures := memory_shared_lock_capture_evidence(
        v_source_ids, v_actor, v_binding_id, v_binding_version,
        (p_payload ->> 'now')::TIMESTAMPTZ
    );
    IF fence ->> 'action_resource_id' IS DISTINCT FROM v_proposal_id
       OR fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER <> 1
       OR fence ->> 'family_space_id' IS DISTINCT FROM v_family_space_id
       OR fence ->> 'membership_snapshot_id' IS DISTINCT FROM (v_membership ->> 'snapshot_id')
       OR (fence ->> 'membership_snapshot_revision')::INTEGER
            IS DISTINCT FROM (v_membership ->> 'revision')::INTEGER
       OR fence ->> 'membership_snapshot_hash'
            IS DISTINCT FROM (v_membership ->> 'canonical_hash')
       OR (SELECT array_agg(value ORDER BY value)
           FROM jsonb_array_elements_text(fence -> 'capture_evidence_ids') AS item(value))
            IS DISTINCT FROM v_source_ids THEN
        RAISE EXCEPTION 'shared proposal resource fence mismatch'
            USING ERRCODE = 'SR412';
    END IF;
    INSERT INTO memory_shared_proposals (
        proposal_id, family_space_id, proposer_subject_id, co_subject_ids,
        binding_version, session_id, epoch, binding_id, binding_role,
        runtime_profile_id, device_id, subject_revision, generation_id,
        turn_id, valid_until, fence_context_hash, title, content,
        source_evidence_ids, proposal_policy_receipt_id, consent_snapshot_id,
        proposal_revision, capture_evidence_hash, consent_snapshot_revision,
        consent_snapshot_hash, membership_snapshot_id,
        membership_snapshot_revision, membership_snapshot_hash, generation,
        tool_epoch, status, created_at, resolved_at
    ) VALUES (
        v_proposal_id, v_family_space_id, v_actor,
        to_jsonb(COALESCE(v_subject_ids, ARRAY[]::TEXT[])),
        v_binding_version, p_payload ->> 'session_id',
        (p_payload ->> 'epoch')::INTEGER, v_binding_id,
        p_payload ->> 'binding_role', p_payload ->> 'runtime_profile_id',
        p_payload ->> 'device_id', (p_payload ->> 'subject_revision')::INTEGER,
        NULLIF(p_payload ->> 'generation_id', ''),
        NULLIF(p_payload ->> 'turn_id', '')::INTEGER,
        (p_payload ->> 'valid_until')::TIMESTAMPTZ,
        p_payload ->> 'fence_context_hash', p_payload ->> 'title',
        p_payload ->> 'content', to_jsonb(COALESCE(v_source_ids, ARRAY[]::TEXT[])),
        p_payload ->> 'proposal_policy_receipt_id',
        p_payload ->> 'consent_snapshot_id', 1,
        p_payload ->> 'capture_evidence_hash',
        (p_payload ->> 'consent_snapshot_revision')::INTEGER,
        p_payload ->> 'consent_snapshot_hash', v_membership ->> 'snapshot_id',
        (v_membership ->> 'revision')::INTEGER, v_membership ->> 'canonical_hash',
        (p_payload ->> 'generation')::INTEGER,
        (p_payload ->> 'tool_epoch')::INTEGER, 'pending',
        (p_payload ->> 'now')::TIMESTAMPTZ, NULL
    );
    INSERT INTO memory_audit_events (
        event_id, action, actor_subject_id, subject_id, record_id,
        proposal_id, payload, created_at
    ) VALUES (
        v_proposal_id || ':proposed', 'memory.shared.proposed', v_actor, v_actor,
        NULL, v_proposal_id, p_payload, (p_payload ->> 'now')::TIMESTAMPTZ
    ) ON CONFLICT (event_id) DO NOTHING;
    INSERT INTO memory_outbox (
        outbox_id, event_id, topic, payload, status, created_at
    ) VALUES (
        v_proposal_id || ':proposed', v_proposal_id || ':proposed',
        'memory.shared.proposed', p_payload, 'pending',
        (p_payload ->> 'now')::TIMESTAMPTZ
    ) ON CONFLICT (outbox_id) DO NOTHING;
    RETURN jsonb_build_object('proposal_id', v_proposal_id, 'status', 'pending');
END
$memory_shared_action_propose$;

CREATE OR REPLACE FUNCTION memory_shared_action_confirm(p_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_action_confirm$
DECLARE
    v_actor TEXT := p_payload ->> 'actor_subject_id';
    v_proposal_id TEXT := p_payload ->> 'proposal_id';
    proposal_row memory_shared_proposals%ROWTYPE;
    existing_vote memory_shared_votes%ROWTYPE;
    required_count INTEGER;
    confirmed_count INTEGER;
    status_value TEXT := 'pending';
    receipt JSONB;
    fence JSONB := p_payload -> 'action_resource_fence';
    now_value TIMESTAMPTZ := (p_payload ->> 'now')::TIMESTAMPTZ;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'shared confirm requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    SELECT * INTO proposal_row
    FROM memory_shared_proposals
    WHERE memory_shared_proposals.proposal_id = v_proposal_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shared proposal is unavailable'
            USING ERRCODE = 'SR404';
    END IF;
    receipt := action_policy_lock_receipt(p_payload ->> 'approval_receipt_id');
    IF receipt IS NULL
       OR (receipt ->> 'effect') NOT IN ('allow', 'allow_with_obligations')
       OR receipt ->> 'capability' IS DISTINCT FROM 'family_shared_memory_approval'
       OR receipt ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_approval'
       OR receipt ->> 'actor_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'subject_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'resource_owner_id'
            IS DISTINCT FROM proposal_row.proposer_subject_id
       OR receipt ->> 'action_fence_hash' IS DISTINCT FROM (fence ->> 'canonical_hash')
       OR p_payload -> 'action_resource_fence' IS DISTINCT FROM (receipt -> 'action_resource_fence')
       OR (receipt ->> 'expires_at')::TIMESTAMPTZ <= now_value
       OR fence ->> 'capability' IS DISTINCT FROM 'family_shared_memory_approval'
       OR fence ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_approval'
       OR fence ->> 'action_resource_id' IS DISTINCT FROM v_proposal_id
       OR fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER
            IS DISTINCT FROM proposal_row.proposal_revision
       OR fence ->> 'family_space_id' IS DISTINCT FROM proposal_row.family_space_id
       OR fence ->> 'voter_subject_id' IS DISTINCT FROM v_actor
       OR fence ->> 'approval_decision' IS DISTINCT FROM 'confirm'
       OR fence ->> 'membership_snapshot_id'
            IS DISTINCT FROM proposal_row.membership_snapshot_id
       OR (fence ->> 'membership_snapshot_revision')::INTEGER
            IS DISTINCT FROM proposal_row.membership_snapshot_revision
       OR fence ->> 'membership_snapshot_hash'
            IS DISTINCT FROM proposal_row.membership_snapshot_hash THEN
        RAISE EXCEPTION 'shared confirm receipt or action fence is invalid'
            USING ERRCODE = 'SR412';
    END IF;
    IF proposal_row.proposer_subject_id <> v_actor
       AND NOT proposal_row.co_subject_ids @> to_jsonb(v_actor) THEN
        RAISE EXCEPTION 'confirming actor is not a proposal subject'
            USING ERRCODE = 'SR403';
    END IF;
    IF proposal_row.proposal_revision <> (p_payload ->> 'proposal_revision')::INTEGER
       OR proposal_row.family_space_id IS DISTINCT FROM p_payload ->> 'family_space_id'
       OR proposal_row.binding_id IS DISTINCT FROM p_payload ->> 'binding_id'
       OR proposal_row.binding_version <> (p_payload ->> 'binding_version')::INTEGER
       OR fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER <> proposal_row.proposal_revision
       OR fence ->> 'family_space_id' IS DISTINCT FROM proposal_row.family_space_id
       OR fence ->> 'membership_snapshot_id'
            IS DISTINCT FROM proposal_row.membership_snapshot_id
       OR (fence ->> 'membership_snapshot_revision')::INTEGER
            IS DISTINCT FROM proposal_row.membership_snapshot_revision
       OR fence ->> 'membership_snapshot_hash'
            IS DISTINCT FROM proposal_row.membership_snapshot_hash
       THEN
        RAISE EXCEPTION 'confirm proposal revision or fence is stale'
            USING ERRCODE = 'SR412';
    END IF;
    SELECT * INTO existing_vote
    FROM memory_shared_votes
    WHERE memory_shared_votes.proposal_id = v_proposal_id
      AND memory_shared_votes.subject_id = v_actor
      AND decision = 'confirm'
    FOR UPDATE;
    IF FOUND THEN
        IF existing_vote.approval_receipt_id
                IS DISTINCT FROM p_payload ->> 'approval_receipt_id'
           OR existing_vote.approval_snapshot_id
                IS DISTINCT FROM p_payload ->> 'approval_snapshot_id'
           OR existing_vote.approval_snapshot_revision
                <> (p_payload ->> 'approval_snapshot_revision')::INTEGER
           OR existing_vote.approval_snapshot_hash
                IS DISTINCT FROM p_payload ->> 'approval_snapshot_hash' THEN
            RAISE EXCEPTION 'confirm retry carries a different approval receipt'
                USING ERRCODE = 'SR409';
        END IF;
    ELSE
        INSERT INTO memory_shared_votes (
            proposal_id, subject_id, decision, voted_at, evidence_id,
            approval_receipt_id, approval_snapshot_id,
            approval_snapshot_revision, approval_snapshot_hash
        ) VALUES (
            v_proposal_id, v_actor, 'confirm', now_value,
            p_payload ->> 'approval_evidence_id',
            p_payload ->> 'approval_receipt_id',
            p_payload ->> 'approval_snapshot_id',
            (p_payload ->> 'approval_snapshot_revision')::INTEGER,
            p_payload ->> 'approval_snapshot_hash'
        );
    END IF;
    SELECT count(*) INTO required_count
    FROM jsonb_array_elements_text(
        jsonb_build_array(proposal_row.proposer_subject_id)
        || proposal_row.co_subject_ids
    );
    SELECT count(DISTINCT subject_id) INTO confirmed_count
    FROM memory_shared_votes
    WHERE memory_shared_votes.proposal_id = v_proposal_id
      AND decision = 'confirm';
    IF confirmed_count = required_count THEN
        status_value := 'approvals_complete';
        UPDATE memory_shared_proposals
        SET status = 'approvals_complete', resolved_at = now_value
        WHERE memory_shared_proposals.proposal_id = v_proposal_id;
    END IF;
    INSERT INTO memory_audit_events (
        event_id, action, actor_subject_id, subject_id, record_id,
        proposal_id, payload, created_at
    ) VALUES (
        v_proposal_id || ':confirm:' || v_actor, 'memory.shared.confirmed',
        v_actor, v_actor, NULL, v_proposal_id, p_payload, now_value
    ) ON CONFLICT (event_id) DO NOTHING;
    RETURN jsonb_build_object('proposal_id', v_proposal_id, 'status', status_value);
END
$memory_shared_action_confirm$;

CREATE OR REPLACE FUNCTION memory_shared_action_object(p_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_action_object$
DECLARE
    v_actor TEXT := p_payload ->> 'actor_subject_id';
    v_proposal_id TEXT := p_payload ->> 'proposal_id';
    proposal_row memory_shared_proposals%ROWTYPE;
    receipt JSONB;
    fence JSONB;
    now_value TIMESTAMPTZ := (p_payload ->> 'now')::TIMESTAMPTZ;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'shared object requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    SELECT * INTO proposal_row
    FROM memory_shared_proposals
    WHERE memory_shared_proposals.proposal_id = v_proposal_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shared proposal is unavailable'
            USING ERRCODE = 'SR404';
    END IF;
    receipt := action_policy_lock_receipt(p_payload ->> 'policy_receipt_id');
    fence := receipt -> 'action_resource_fence';
    IF receipt IS NULL
       OR (receipt ->> 'effect') NOT IN ('allow', 'allow_with_obligations')
       OR receipt ->> 'capability' IS DISTINCT FROM 'family_shared_memory_approval'
       OR receipt ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_approval'
       OR receipt ->> 'actor_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'subject_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'resource_owner_id'
            IS DISTINCT FROM proposal_row.proposer_subject_id
       OR p_payload -> 'action_resource_fence' IS DISTINCT FROM fence
       OR receipt ->> 'action_fence_hash' IS DISTINCT FROM fence ->> 'canonical_hash'
       OR (receipt ->> 'expires_at')::TIMESTAMPTZ <= now_value
       OR fence ->> 'capability' IS DISTINCT FROM 'family_shared_memory_approval'
       OR fence ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_approval'
       OR fence ->> 'action_resource_id' IS DISTINCT FROM v_proposal_id
       OR fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER
            IS DISTINCT FROM proposal_row.proposal_revision
       OR fence ->> 'family_space_id' IS DISTINCT FROM proposal_row.family_space_id
       OR fence ->> 'voter_subject_id' IS DISTINCT FROM v_actor
       OR fence ->> 'approval_decision' IS DISTINCT FROM 'confirm'
       OR fence ->> 'membership_snapshot_id'
            IS DISTINCT FROM proposal_row.membership_snapshot_id
       OR (fence ->> 'membership_snapshot_revision')::INTEGER
            IS DISTINCT FROM proposal_row.membership_snapshot_revision
       OR fence ->> 'membership_snapshot_hash'
            IS DISTINCT FROM proposal_row.membership_snapshot_hash
       OR (fence -> 'required_approval_subject_ids') IS DISTINCT FROM (
            SELECT jsonb_agg(item.value ORDER BY item.value)
            FROM jsonb_array_elements_text(
                jsonb_build_array(proposal_row.proposer_subject_id)
                || proposal_row.co_subject_ids
            ) AS item(value)
       )
       OR receipt ->> 'device_id' IS DISTINCT FROM proposal_row.device_id
       OR receipt ->> 'binding_id' IS DISTINCT FROM proposal_row.binding_id
       OR (receipt ->> 'binding_version')::INTEGER
            IS DISTINCT FROM proposal_row.binding_version
       OR p_payload ->> 'privacy_action' IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'shared object receipt or action fence is invalid'
            USING ERRCODE = 'SR412';
    END IF;
    IF proposal_row.proposer_subject_id <> v_actor
       AND NOT proposal_row.co_subject_ids @> to_jsonb(v_actor) THEN
        RAISE EXCEPTION 'objecting actor is not a proposal subject'
            USING ERRCODE = 'SR403';
    END IF;
    IF proposal_row.proposal_revision <> (p_payload ->> 'proposal_revision')::INTEGER
       OR proposal_row.fence_context_hash
            IS DISTINCT FROM p_payload ->> 'fence_context_hash' THEN
        RAISE EXCEPTION 'object proposal fence is stale'
            USING ERRCODE = 'SR412';
    END IF;
    IF proposal_row.status NOT IN ('pending', 'approvals_complete') THEN
        RETURN jsonb_build_object('proposal_id', v_proposal_id, 'status', proposal_row.status);
    END IF;
    INSERT INTO memory_shared_votes (
        proposal_id, subject_id, decision, voted_at, evidence_id,
        approval_receipt_id, approval_snapshot_id,
        approval_snapshot_revision, approval_snapshot_hash
    ) VALUES (
        v_proposal_id, v_actor, 'object', now_value,
        p_payload ->> 'evidence_id', '', '', 0, ''
    ) ON CONFLICT (proposal_id, subject_id, decision) DO NOTHING;
    UPDATE memory_shared_proposals
    SET status = 'frozen', resolved_at = now_value
    WHERE memory_shared_proposals.proposal_id = v_proposal_id;
    INSERT INTO memory_audit_events (
        event_id, action, actor_subject_id, subject_id, record_id,
        proposal_id, payload, created_at
    ) VALUES (
        v_proposal_id || ':object:' || v_actor, 'memory.shared.objected',
        v_actor, v_actor, NULL, v_proposal_id, p_payload, now_value
    ) ON CONFLICT (event_id) DO NOTHING;
    RETURN jsonb_build_object('proposal_id', v_proposal_id, 'status', 'frozen');
END
$memory_shared_action_object$;

CREATE OR REPLACE FUNCTION memory_shared_action_withdraw(p_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_action_withdraw$
DECLARE
    v_actor TEXT := p_payload ->> 'actor_subject_id';
    v_proposal_id TEXT := p_payload ->> 'proposal_id';
    proposal_row memory_shared_proposals%ROWTYPE;
    v_record_id TEXT;
    receipt JSONB;
    fence JSONB;
    now_value TIMESTAMPTZ := (p_payload ->> 'now')::TIMESTAMPTZ;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'shared withdraw requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    SELECT * INTO proposal_row
    FROM memory_shared_proposals
    WHERE memory_shared_proposals.proposal_id = v_proposal_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shared proposal is unavailable'
            USING ERRCODE = 'SR404';
    END IF;
    receipt := action_policy_lock_receipt(p_payload ->> 'policy_receipt_id');
    fence := receipt -> 'action_resource_fence';
    IF receipt IS NULL
       OR (receipt ->> 'effect') NOT IN ('allow', 'allow_with_obligations')
       OR receipt ->> 'capability' IS DISTINCT FROM 'family_shared_memory_approval'
       OR receipt ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_approval'
       OR receipt ->> 'actor_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'subject_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'resource_owner_id'
            IS DISTINCT FROM proposal_row.proposer_subject_id
       OR p_payload -> 'action_resource_fence' IS DISTINCT FROM fence
       OR receipt ->> 'action_fence_hash' IS DISTINCT FROM fence ->> 'canonical_hash'
       OR (receipt ->> 'expires_at')::TIMESTAMPTZ <= now_value
       OR fence ->> 'capability' IS DISTINCT FROM 'family_shared_memory_approval'
       OR fence ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_approval'
       OR fence ->> 'action_resource_id' IS DISTINCT FROM v_proposal_id
       OR fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER
            IS DISTINCT FROM proposal_row.proposal_revision
       OR fence ->> 'family_space_id' IS DISTINCT FROM proposal_row.family_space_id
       OR fence ->> 'voter_subject_id' IS DISTINCT FROM v_actor
       OR fence ->> 'approval_decision' IS DISTINCT FROM 'confirm'
       OR fence ->> 'membership_snapshot_id'
            IS DISTINCT FROM proposal_row.membership_snapshot_id
       OR (fence ->> 'membership_snapshot_revision')::INTEGER
            IS DISTINCT FROM proposal_row.membership_snapshot_revision
       OR fence ->> 'membership_snapshot_hash'
            IS DISTINCT FROM proposal_row.membership_snapshot_hash
       OR (fence -> 'required_approval_subject_ids') IS DISTINCT FROM (
            SELECT jsonb_agg(item.value ORDER BY item.value)
            FROM jsonb_array_elements_text(
                jsonb_build_array(proposal_row.proposer_subject_id)
                || proposal_row.co_subject_ids
            ) AS item(value)
       )
       OR receipt ->> 'device_id' IS DISTINCT FROM proposal_row.device_id
       OR receipt ->> 'binding_id' IS DISTINCT FROM proposal_row.binding_id
       OR (receipt ->> 'binding_version')::INTEGER
            IS DISTINCT FROM proposal_row.binding_version
       OR p_payload ->> 'privacy_action' IS DISTINCT FROM 'withdraw' THEN
        RAISE EXCEPTION 'shared withdraw receipt or action fence is invalid'
            USING ERRCODE = 'SR412';
    END IF;
    IF proposal_row.proposer_subject_id <> v_actor
       AND NOT proposal_row.co_subject_ids @> to_jsonb(v_actor) THEN
        RAISE EXCEPTION 'withdrawing actor is not a proposal subject'
            USING ERRCODE = 'SR403';
    END IF;
    IF proposal_row.proposal_revision <> (p_payload ->> 'proposal_revision')::INTEGER
       OR proposal_row.fence_context_hash
            IS DISTINCT FROM p_payload ->> 'fence_context_hash' THEN
        RAISE EXCEPTION 'withdraw proposal fence is stale'
            USING ERRCODE = 'SR412';
    END IF;
    IF proposal_row.status = 'withdrawn' THEN
        RETURN jsonb_build_object('proposal_id', v_proposal_id, 'status', 'withdrawn');
    END IF;
    UPDATE memory_shared_proposals
    SET status = 'withdrawn', resolved_at = now_value
    WHERE memory_shared_proposals.proposal_id = v_proposal_id;
    SELECT memory_records.record_id INTO v_record_id
    FROM memory_records
    WHERE memory_records.shared_proposal_id = v_proposal_id
    FOR UPDATE;
    IF v_record_id IS NOT NULL THEN
        INSERT INTO memory_status_events (
            event_id, record_id, status, reason_code, created_at
        ) VALUES (
            v_proposal_id || ':withdrawn:' || v_record_id, v_record_id, 'revoked',
            'shared_memory_withdrawn', now_value
        ) ON CONFLICT (event_id) DO NOTHING;
    END IF;
    INSERT INTO memory_audit_events (
        event_id, action, actor_subject_id, subject_id, record_id,
        proposal_id, payload, created_at
    ) VALUES (
        v_proposal_id || ':withdraw:' || v_actor, 'memory.shared.withdrawn',
        v_actor, v_actor, v_record_id, v_proposal_id, p_payload, now_value
    ) ON CONFLICT (event_id) DO NOTHING;
    RETURN jsonb_build_object('proposal_id', v_proposal_id, 'status', 'withdrawn');
END
$memory_shared_action_withdraw$;

CREATE OR REPLACE FUNCTION memory_shared_action_promote(p_payload JSONB)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $memory_shared_action_promote$
DECLARE
    v_actor TEXT := p_payload ->> 'actor_subject_id';
    v_proposal_id TEXT := p_payload ->> 'proposal_id';
    proposal_row memory_shared_proposals%ROWTYPE;
    membership JSONB;
    expected JSONB;
    vote_row memory_shared_votes%ROWTYPE;
    receipt JSONB;
    v_record_id TEXT := p_payload ->> 'record_id';
    required_subject_ids TEXT[];
    confirmed_count INTEGER;
    now_value TIMESTAMPTZ := (p_payload ->> 'now')::TIMESTAMPTZ;
    fence JSONB := p_payload -> 'action_resource_fence';
    approval_refs JSONB := '[]'::jsonb;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'shared promotion requires action executor'
            USING ERRCODE = 'SR403';
    END IF;
    IF v_record_id IS NULL OR btrim(v_record_id) = '' THEN
        RAISE EXCEPTION 'promotion record id is required'
            USING ERRCODE = 'SR400';
    END IF;
    SELECT * INTO proposal_row
    FROM memory_shared_proposals
    WHERE memory_shared_proposals.proposal_id = v_proposal_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'shared proposal is unavailable'
            USING ERRCODE = 'SR404';
    END IF;
    receipt := action_policy_lock_receipt(p_payload ->> 'promotion_receipt_id');
    IF receipt IS NULL
       OR (receipt ->> 'effect') NOT IN ('allow', 'allow_with_obligations')
       OR receipt ->> 'capability' IS DISTINCT FROM 'family_shared_memory_promotion'
       OR receipt ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_promotion'
       OR receipt ->> 'actor_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'subject_id' IS DISTINCT FROM v_actor
       OR receipt ->> 'resource_owner_id'
            IS DISTINCT FROM proposal_row.proposer_subject_id
       OR receipt ->> 'action_fence_hash' IS DISTINCT FROM (fence ->> 'canonical_hash')
       OR p_payload -> 'action_resource_fence' IS DISTINCT FROM (receipt -> 'action_resource_fence')
       OR (receipt ->> 'expires_at')::TIMESTAMPTZ <= now_value
       OR fence ->> 'capability' IS DISTINCT FROM 'family_shared_memory_promotion'
       OR fence ->> 'purpose' IS DISTINCT FROM 'family_shared_memory_promotion'
       OR fence ->> 'action_resource_id' IS DISTINCT FROM v_proposal_id
       OR fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER
            IS DISTINCT FROM proposal_row.proposal_revision
       OR fence ->> 'family_space_id' IS DISTINCT FROM proposal_row.family_space_id
       OR fence ->> 'membership_snapshot_id'
            IS DISTINCT FROM proposal_row.membership_snapshot_id
       OR (fence ->> 'membership_snapshot_revision')::INTEGER
            IS DISTINCT FROM proposal_row.membership_snapshot_revision
       OR fence ->> 'membership_snapshot_hash'
            IS DISTINCT FROM proposal_row.membership_snapshot_hash THEN
        RAISE EXCEPTION 'shared promotion receipt or action fence is invalid'
            USING ERRCODE = 'SR412';
    END IF;
    IF proposal_row.status <> 'approvals_complete' THEN
        RETURN jsonb_build_object('proposal_id', v_proposal_id, 'status', proposal_row.status);
    END IF;
    SELECT array_agg(DISTINCT value ORDER BY value)
    INTO required_subject_ids
    FROM jsonb_array_elements_text(
        jsonb_build_array(proposal_row.proposer_subject_id)
        || proposal_row.co_subject_ids
    ) AS item(value);
    membership := memory_shared_lock_membership(
        proposal_row.family_space_id, required_subject_ids,
        proposal_row.binding_id, proposal_row.binding_version, now_value
    );
    IF membership ->> 'snapshot_id' IS DISTINCT FROM proposal_row.membership_snapshot_id
       OR (membership ->> 'revision')::INTEGER
            IS DISTINCT FROM proposal_row.membership_snapshot_revision
       OR membership ->> 'canonical_hash'
            IS DISTINCT FROM proposal_row.membership_snapshot_hash
       OR fence ->> 'membership_snapshot_id'
            IS DISTINCT FROM proposal_row.membership_snapshot_id
       OR (fence ->> 'membership_snapshot_revision')::INTEGER
            IS DISTINCT FROM proposal_row.membership_snapshot_revision
       OR fence ->> 'membership_snapshot_hash'
            IS DISTINCT FROM proposal_row.membership_snapshot_hash THEN
        RAISE EXCEPTION 'promotion membership CAS failed'
            USING ERRCODE = 'SR412';
    END IF;
    FOR vote_row IN
        SELECT * FROM memory_shared_votes
        WHERE memory_shared_votes.proposal_id = v_proposal_id
        ORDER BY subject_id, voted_at, decision
        FOR UPDATE
    LOOP
        IF vote_row.decision = 'confirm' THEN
            approval_refs := approval_refs || jsonb_build_array(jsonb_build_array(
                vote_row.subject_id, vote_row.approval_receipt_id,
                vote_row.approval_snapshot_id,
                vote_row.approval_snapshot_revision,
                vote_row.approval_snapshot_hash
            ));
        END IF;
    END LOOP;
    SELECT count(DISTINCT subject_id) INTO confirmed_count
    FROM memory_shared_votes
    WHERE memory_shared_votes.proposal_id = v_proposal_id
      AND decision = 'confirm';
    IF confirmed_count <> cardinality(required_subject_ids) THEN
        RAISE EXCEPTION 'promotion approval set is incomplete'
            USING ERRCODE = 'SR412';
    END IF;
    IF fence ->> 'proposal_id' IS DISTINCT FROM v_proposal_id
       OR (fence ->> 'proposal_revision')::INTEGER <> proposal_row.proposal_revision
       OR fence ->> 'family_space_id' IS DISTINCT FROM proposal_row.family_space_id
       OR (SELECT count(*) FROM jsonb_array_elements(fence -> 'approval_snapshots'))
            <> confirmed_count THEN
        RAISE EXCEPTION 'promotion action fence is stale'
            USING ERRCODE = 'SR412';
    END IF;
    UPDATE memory_shared_proposals
    SET status = 'promoted', resolved_at = now_value
    WHERE memory_shared_proposals.proposal_id = v_proposal_id;
    INSERT INTO memory_records (
        record_id, scope, subject_id, resource_owner_id, family_space_id,
        co_subject_ids, source_evidence_ids, policy_receipt_id,
        promotion_receipt_id, promotion_fence_context_hash,
        approval_evidence_refs, consent_snapshot_id, memory_type, confidence,
        retention, retention_expires_at, payload, created_by_actor_id,
        created_at, shared_proposal_id
    ) VALUES (
        v_record_id, 'family_shared', proposal_row.proposer_subject_id,
        proposal_row.family_space_id, proposal_row.family_space_id,
        proposal_row.co_subject_ids, proposal_row.source_evidence_ids,
        proposal_row.proposal_policy_receipt_id,
        p_payload ->> 'promotion_receipt_id',
        fence ->> 'canonical_hash', approval_refs,
        fence ->> 'consent_snapshot_id', 'semantic', 1.0, 'indefinite', NULL,
        jsonb_build_object('title', proposal_row.title, 'content', proposal_row.content),
        v_actor, now_value, v_proposal_id
    ) ON CONFLICT (shared_proposal_id)
        WHERE shared_proposal_id IS NOT NULL
        DO NOTHING;
    IF NOT FOUND THEN
        SELECT memory_records.record_id INTO v_record_id
        FROM memory_records
        WHERE memory_records.shared_proposal_id = v_proposal_id;
    END IF;
    INSERT INTO memory_status_events (
        event_id, record_id, status, reason_code, created_at
    ) VALUES (
        v_proposal_id || ':promoted', v_record_id, 'confirmed',
        'all_co_subjects_confirmed', now_value
    ) ON CONFLICT (event_id) DO NOTHING;
    INSERT INTO memory_outbox (
        outbox_id, event_id, topic, payload, status, created_at
    ) VALUES (
        v_proposal_id || ':promoted', v_proposal_id || ':promoted',
        'memory.shared.promoted', p_payload, 'pending', now_value
    ) ON CONFLICT (outbox_id) DO NOTHING;
    INSERT INTO memory_audit_events (
        event_id, action, actor_subject_id, subject_id, record_id,
        proposal_id, payload, created_at
    ) VALUES (
        v_proposal_id || ':promoted', 'memory.shared.promoted', v_actor,
        proposal_row.proposer_subject_id, v_record_id, v_proposal_id,
        p_payload, now_value
    ) ON CONFLICT (event_id) DO NOTHING;
    RETURN jsonb_build_object(
        'proposal_id', v_proposal_id, 'status', 'promoted', 'record_id', v_record_id
    );
END
$memory_shared_action_promote$;

REVOKE ALL ON FUNCTION memory_shared_lock_membership(
    TEXT, TEXT[], TEXT, INTEGER, TIMESTAMPTZ
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_lock_capture_evidence(
    TEXT[], TEXT, TEXT, INTEGER, TIMESTAMPTZ
) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_lock_proposal(TEXT, TEXT, TIMESTAMPTZ)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_lock_approvals(TEXT, TIMESTAMPTZ)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_lock_action_resource(TEXT, TEXT, TEXT, INTEGER)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_action_propose(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_action_confirm(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_action_object(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_action_withdraw(JSONB) FROM PUBLIC;
REVOKE ALL ON FUNCTION memory_shared_action_promote(JSONB) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION memory_shared_lock_membership(
    TEXT, TEXT[], TEXT, INTEGER, TIMESTAMPTZ
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_lock_capture_evidence(
    TEXT[], TEXT, TEXT, INTEGER, TIMESTAMPTZ
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_lock_proposal(TEXT, TEXT, TIMESTAMPTZ)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_lock_approvals(TEXT, TIMESTAMPTZ)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_lock_action_resource(TEXT, TEXT, TEXT, INTEGER)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_action_propose(JSONB)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_action_confirm(JSONB)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_action_object(JSONB)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_action_withdraw(JSONB)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION memory_shared_action_promote(JSONB)
    TO memoria_action_executor;

RESET ROLE;
