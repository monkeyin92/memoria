-- Session Runtime production authority (PR-06/PR-07/PR-08/PR-17).
--
-- ``memoria_action_executor`` is the single cross-domain transaction login.
-- It is never a table owner, is NOSUPERUSER/NOBYPASSRLS, and receives no
-- direct table privileges.  It can only execute fixed-search-path SECURITY
-- DEFINER ports owned by the relevant domain owner.  Session API/projector/
-- worker/maintenance roles remain independent and cannot impersonate it.

DO $session_runtime_roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_owner'
    ) THEN
        CREATE ROLE memoria_session_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_action_bridge_owner'
    ) THEN
        CREATE ROLE memoria_action_bridge_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_device_action_bridge_owner'
    ) THEN
        CREATE ROLE memoria_device_action_bridge_owner
            NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_action_executor'
    ) THEN
        CREATE ROLE memoria_action_executor LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_action_executor LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_api'
    ) THEN
        CREATE ROLE memoria_session_api LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_session_api LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_projector'
    ) THEN
        CREATE ROLE memoria_session_projector LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_session_projector LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_worker'
    ) THEN
        CREATE ROLE memoria_session_worker LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_session_worker LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_maintenance'
    ) THEN
        CREATE ROLE memoria_session_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_session_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    EXECUTE format('GRANT memoria_session_owner TO %I', current_user);
    EXECUTE format('GRANT memoria_action_bridge_owner TO %I', current_user);
    EXECUTE format(
        'GRANT memoria_device_action_bridge_owner TO %I', current_user
    );
END
$session_runtime_roles$;

DO $session_runtime_dependencies$
BEGIN
    IF to_regprocedure('public.identity_binding_visible(text,text)') IS NULL THEN
        RAISE EXCEPTION
            'Session Runtime requires Identity authority in the same database';
    END IF;
    IF to_regclass('public.policy_receipts_v2') IS NULL
       OR to_regprocedure('public.policy_lock_receipt_v2(text)') IS NULL THEN
        RAISE EXCEPTION
            'Session Runtime requires PolicyReceiptV2 authority in the same database';
    END IF;
END
$session_runtime_dependencies$;

GRANT USAGE, CREATE ON SCHEMA public TO memoria_session_owner;
GRANT USAGE, CREATE ON SCHEMA public TO memoria_action_bridge_owner;
GRANT USAGE, CREATE ON SCHEMA public TO memoria_device_action_bridge_owner;
GRANT EXECUTE ON FUNCTION identity_binding_visible(text, text)
    TO memoria_session_owner, memoria_session_api,
       memoria_session_projector, memoria_session_maintenance;
GRANT REFERENCES ON TABLE policy_receipts_v2 TO memoria_session_owner;

SET ROLE memoria_session_owner;
SET search_path = public;

CREATE OR REPLACE FUNCTION session_runtime_actor() RETURNS text
LANGUAGE sql STABLE
AS $session_runtime_actor$
    SELECT NULLIF(current_setting('app.session_actor', true), '')
$session_runtime_actor$;

CREATE TABLE IF NOT EXISTS session_runtime_contexts (
    session_id TEXT PRIMARY KEY CHECK (char_length(session_id) BETWEEN 1 AND 128),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL CHECK (char_length(device_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    active_subject_id TEXT CHECK (
        active_subject_id IS NULL
        OR char_length(active_subject_id) BETWEEN 1 AND 128
    ),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    session_epoch INTEGER NOT NULL CHECK (session_epoch >= 1),
    profile_revision INTEGER NOT NULL CHECK (profile_revision >= 1),
    current_runtime_profile_id TEXT NOT NULL CHECK (
        char_length(current_runtime_profile_id) BETWEEN 1 AND 128
    ),
    generation_id INTEGER NOT NULL DEFAULT 0 CHECK (generation_id >= 0),
    turn_id INTEGER NOT NULL DEFAULT 0 CHECK (turn_id >= 0),
    tool_epoch INTEGER NOT NULL DEFAULT 0 CHECK (tool_epoch >= 0),
    state TEXT NOT NULL CHECK (state IN (
        'provisioning', 'active', 'closed', 'failed'
    )),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS session_runtime_profiles (
    runtime_profile_id TEXT PRIMARY KEY CHECK (
        char_length(runtime_profile_id) BETWEEN 1 AND 128
    ),
    session_id TEXT NOT NULL REFERENCES session_runtime_contexts(session_id)
        ON DELETE RESTRICT,
    profile_revision INTEGER NOT NULL CHECK (profile_revision >= 1),
    session_epoch INTEGER NOT NULL CHECK (session_epoch >= 1),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    active_subject_id TEXT CHECK (
        active_subject_id IS NULL
        OR char_length(active_subject_id) BETWEEN 1 AND 128
    ),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    signature TEXT NOT NULL CHECK (signature ~ '^[a-f0-9]{64}$'),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, profile_revision),
    UNIQUE (session_id, session_epoch),
    CHECK (expires_at > issued_at)
);

DO $session_runtime_current_profile_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'session_runtime_context_current_profile_fk'
    ) THEN
        ALTER TABLE session_runtime_contexts
        ADD CONSTRAINT session_runtime_context_current_profile_fk
        FOREIGN KEY (current_runtime_profile_id)
        REFERENCES session_runtime_profiles(runtime_profile_id)
        DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$session_runtime_current_profile_fk$;

CREATE TABLE IF NOT EXISTS session_runtime_profile_receipts (
    runtime_profile_id TEXT NOT NULL
        REFERENCES session_runtime_profiles(runtime_profile_id) ON DELETE RESTRICT,
    receipt_id TEXT NOT NULL
        REFERENCES policy_receipts_v2(receipt_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL
        REFERENCES session_runtime_contexts(session_id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (runtime_profile_id, receipt_id)
);

CREATE TABLE IF NOT EXISTS session_runtime_events (
    event_id TEXT PRIMARY KEY CHECK (char_length(event_id) BETWEEN 1 AND 128),
    event_type TEXT NOT NULL CHECK (char_length(event_type) BETWEEN 1 AND 64),
    session_id TEXT NOT NULL
        REFERENCES session_runtime_contexts(session_id) ON DELETE RESTRICT,
    event_sequence INTEGER NOT NULL CHECK (event_sequence >= 0),
    session_epoch INTEGER NOT NULL CHECK (session_epoch >= 0),
    actor_id TEXT CHECK (actor_id IS NULL OR char_length(actor_id) BETWEEN 1 AND 128),
    binding_id TEXT CHECK (
        binding_id IS NULL OR char_length(binding_id) BETWEEN 1 AND 128
    ),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, event_sequence)
);

CREATE TABLE IF NOT EXISTS session_runtime_outbox (
    outbox_id TEXT PRIMARY KEY CHECK (char_length(outbox_id) BETWEEN 1 AND 128),
    event_id TEXT NOT NULL UNIQUE
        REFERENCES session_runtime_events(event_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL
        REFERENCES session_runtime_contexts(session_id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    topic TEXT NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 128),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'processing', 'delivered', 'failed', 'dead_lettered'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_until TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR (status <> 'delivered' AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_session_runtime_outbox_pending
ON session_runtime_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS session_runtime_idempotency (
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL CHECK (
        char_length(idempotency_key) BETWEEN 1 AND 128
    ),
    operation TEXT NOT NULL CHECK (operation IN ('session.start', 'subject.switch')),
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    session_id TEXT NOT NULL CHECK (char_length(session_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    result_runtime_profile_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (actor_id, idempotency_key, operation),
    CHECK (
        (result_runtime_profile_id IS NULL AND completed_at IS NULL)
        OR (result_runtime_profile_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

DO $session_runtime_idempotency_profile_fk$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'session_runtime_idempotency_profile_fk'
    ) THEN
        ALTER TABLE session_runtime_idempotency
        ADD CONSTRAINT session_runtime_idempotency_profile_fk
        FOREIGN KEY (result_runtime_profile_id)
        REFERENCES session_runtime_profiles(runtime_profile_id)
        DEFERRABLE INITIALLY DEFERRED;
    END IF;
END
$session_runtime_idempotency_profile_fk$;

-- Durable side-effect intent and delivery outbox.  The action executor has no
-- table privilege: its single commit SECURITY DEFINER port below locks the
-- Session/profile/Policy authority and inserts both rows in this transaction.
CREATE TABLE IF NOT EXISTS session_runtime_tool_effect_intents (
    intent_id TEXT PRIMARY KEY CHECK (char_length(intent_id) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL UNIQUE CHECK (
        char_length(idempotency_key) BETWEEN 1 AND 512
    ),
    intent TEXT NOT NULL CHECK (char_length(intent) BETWEEN 1 AND 512),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
    session_id TEXT NOT NULL
        REFERENCES session_runtime_contexts(session_id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL CHECK (char_length(device_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    runtime_profile_id TEXT NOT NULL
        REFERENCES session_runtime_profiles(runtime_profile_id) ON DELETE RESTRICT,
    profile_revision INTEGER NOT NULL CHECK (profile_revision >= 1),
    session_epoch INTEGER NOT NULL CHECK (session_epoch >= 1),
    generation_id INTEGER NOT NULL CHECK (generation_id >= 0),
    turn_id INTEGER NOT NULL CHECK (turn_id >= 0),
    tool_epoch INTEGER NOT NULL CHECK (tool_epoch >= 0),
    policy_receipt_id TEXT NOT NULL
        REFERENCES policy_receipts_v2(receipt_id) ON DELETE RESTRICT,
    capability TEXT NOT NULL CHECK (char_length(capability) BETWEEN 1 AND 128),
    purpose TEXT NOT NULL CHECK (char_length(purpose) BETWEEN 1 AND 128),
    resource_id TEXT NOT NULL CHECK (char_length(resource_id) BETWEEN 1 AND 128),
    fence_fingerprint TEXT NOT NULL CHECK (fence_fingerprint ~ '^[a-f0-9]{64}$'),
    evidence_refs JSONB NOT NULL CHECK (
        jsonb_typeof(evidence_refs) = 'array'
        AND jsonb_array_length(evidence_refs) > 0
    ),
    action_fence_hash TEXT NOT NULL CHECK (action_fence_hash ~ '^[a-f0-9]{64}$'),
    context_hash TEXT NOT NULL CHECK (context_hash ~ '^[a-f0-9]{64}$'),
    authority_revision INTEGER NOT NULL CHECK (authority_revision >= 1),
    status TEXT NOT NULL DEFAULT 'committed' CHECK (
        status IN ('committed', 'delivered', 'failed', 'cancelled')
    ),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    committed_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS session_runtime_tool_effect_outbox (
    outbox_id TEXT PRIMARY KEY CHECK (char_length(outbox_id) BETWEEN 1 AND 160),
    intent_id TEXT NOT NULL UNIQUE
        REFERENCES session_runtime_tool_effect_intents(intent_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL
        REFERENCES session_runtime_contexts(session_id) ON DELETE RESTRICT,
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    topic TEXT NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 128),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'delivered', 'failed', 'dead_lettered')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_until TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_session_runtime_tool_effect_outbox_pending
ON session_runtime_tool_effect_outbox(status, created_at);

ALTER TABLE session_runtime_tool_effect_outbox
    ADD COLUMN IF NOT EXISTS locked_by TEXT;

CREATE OR REPLACE FUNCTION session_runtime_reject_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $session_runtime_reject_immutable_mutation$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'SR001';
END
$session_runtime_reject_immutable_mutation$;

DROP TRIGGER IF EXISTS session_runtime_profiles_immutable
    ON session_runtime_profiles;
CREATE TRIGGER session_runtime_profiles_immutable
BEFORE UPDATE OR DELETE ON session_runtime_profiles
FOR EACH ROW EXECUTE FUNCTION session_runtime_reject_immutable_mutation();
DROP TRIGGER IF EXISTS session_runtime_events_immutable
    ON session_runtime_events;
CREATE TRIGGER session_runtime_events_immutable
BEFORE UPDATE OR DELETE ON session_runtime_events
FOR EACH ROW EXECUTE FUNCTION session_runtime_reject_immutable_mutation();
DROP TRIGGER IF EXISTS session_runtime_receipts_immutable
    ON session_runtime_profile_receipts;
CREATE TRIGGER session_runtime_receipts_immutable
BEFORE UPDATE OR DELETE ON session_runtime_profile_receipts
FOR EACH ROW EXECUTE FUNCTION session_runtime_reject_immutable_mutation();

ALTER TABLE session_runtime_contexts ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_contexts FORCE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_profile_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_profile_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_events FORCE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_idempotency FORCE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_tool_effect_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_tool_effect_intents FORCE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_tool_effect_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE session_runtime_tool_effect_outbox FORCE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE session_runtime_contexts FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_profiles FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_profile_receipts FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_events FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_outbox FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_idempotency FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_tool_effect_intents FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_tool_effect_outbox FROM PUBLIC;
REVOKE ALL ON TABLE session_runtime_contexts FROM memoria_action_executor;
REVOKE ALL ON TABLE session_runtime_profiles FROM memoria_action_executor;
REVOKE ALL ON TABLE session_runtime_profile_receipts FROM memoria_action_executor;
REVOKE ALL ON TABLE session_runtime_events FROM memoria_action_executor;
REVOKE ALL ON TABLE session_runtime_outbox FROM memoria_action_executor;
REVOKE ALL ON TABLE session_runtime_idempotency FROM memoria_action_executor;
REVOKE ALL ON TABLE session_runtime_tool_effect_intents FROM memoria_action_executor;
REVOKE ALL ON TABLE session_runtime_tool_effect_outbox FROM memoria_action_executor;

GRANT SELECT ON session_runtime_contexts, session_runtime_profiles,
    session_runtime_profile_receipts, session_runtime_events
    TO memoria_session_api, memoria_session_projector;
GRANT SELECT ON session_runtime_contexts, session_runtime_profiles,
    session_runtime_profile_receipts, session_runtime_events,
    session_runtime_outbox, session_runtime_idempotency
    TO memoria_session_maintenance;
GRANT SELECT ON session_runtime_tool_effect_intents,
    session_runtime_tool_effect_outbox TO memoria_session_maintenance;

-- The FORCE-RLS table owner is still constrained; its policies are only used
-- inside narrowly granted SECURITY DEFINER functions below.
DROP POLICY IF EXISTS session_runtime_owner_contexts
    ON session_runtime_contexts;
CREATE POLICY session_runtime_owner_contexts ON session_runtime_contexts
    FOR ALL TO memoria_session_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS session_runtime_owner_profiles
    ON session_runtime_profiles;
CREATE POLICY session_runtime_owner_profiles ON session_runtime_profiles
    FOR ALL TO memoria_session_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS session_runtime_owner_receipts
    ON session_runtime_profile_receipts;
CREATE POLICY session_runtime_owner_receipts ON session_runtime_profile_receipts
    FOR ALL TO memoria_session_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS session_runtime_owner_events
    ON session_runtime_events;
CREATE POLICY session_runtime_owner_events ON session_runtime_events
    FOR ALL TO memoria_session_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS session_runtime_owner_outbox
    ON session_runtime_outbox;
CREATE POLICY session_runtime_owner_outbox ON session_runtime_outbox
    FOR ALL TO memoria_session_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS session_runtime_owner_idempotency
    ON session_runtime_idempotency;
CREATE POLICY session_runtime_owner_idempotency ON session_runtime_idempotency
    FOR ALL TO memoria_session_owner USING (true) WITH CHECK (true);
DROP POLICY IF EXISTS session_runtime_owner_tool_effect_intents
    ON session_runtime_tool_effect_intents;
CREATE POLICY session_runtime_owner_tool_effect_intents
    ON session_runtime_tool_effect_intents
    FOR ALL TO memoria_session_owner USING (current_user = 'memoria_session_owner')
    WITH CHECK (current_user = 'memoria_session_owner');
DROP POLICY IF EXISTS session_runtime_owner_tool_effect_outbox
    ON session_runtime_tool_effect_outbox;
CREATE POLICY session_runtime_owner_tool_effect_outbox
    ON session_runtime_tool_effect_outbox
    FOR ALL TO memoria_session_owner USING (current_user = 'memoria_session_owner')
    WITH CHECK (current_user = 'memoria_session_owner');

DROP POLICY IF EXISTS session_runtime_api_contexts
    ON session_runtime_contexts;
CREATE POLICY session_runtime_api_contexts ON session_runtime_contexts
    FOR SELECT TO memoria_session_api
    USING (identity_binding_visible(session_runtime_actor(), binding_id));
DROP POLICY IF EXISTS session_runtime_api_profiles
    ON session_runtime_profiles;
CREATE POLICY session_runtime_api_profiles ON session_runtime_profiles
    FOR SELECT TO memoria_session_api
    USING (identity_binding_visible(session_runtime_actor(), binding_id));
DROP POLICY IF EXISTS session_runtime_api_receipts
    ON session_runtime_profile_receipts;
CREATE POLICY session_runtime_api_receipts ON session_runtime_profile_receipts
    FOR SELECT TO memoria_session_api
    USING (identity_binding_visible(session_runtime_actor(), binding_id));
DROP POLICY IF EXISTS session_runtime_api_events
    ON session_runtime_events;
CREATE POLICY session_runtime_api_events ON session_runtime_events
    FOR SELECT TO memoria_session_api
    USING (
        binding_id IS NOT NULL
        AND identity_binding_visible(session_runtime_actor(), binding_id)
    );

DROP POLICY IF EXISTS session_runtime_projector_contexts
    ON session_runtime_contexts;
CREATE POLICY session_runtime_projector_contexts ON session_runtime_contexts
    FOR SELECT TO memoria_session_projector
    USING (current_user = 'memoria_session_projector');
DROP POLICY IF EXISTS session_runtime_projector_profiles
    ON session_runtime_profiles;
CREATE POLICY session_runtime_projector_profiles ON session_runtime_profiles
    FOR SELECT TO memoria_session_projector
    USING (current_user = 'memoria_session_projector');
DROP POLICY IF EXISTS session_runtime_projector_receipts
    ON session_runtime_profile_receipts;
CREATE POLICY session_runtime_projector_receipts ON session_runtime_profile_receipts
    FOR SELECT TO memoria_session_projector
    USING (current_user = 'memoria_session_projector');
DROP POLICY IF EXISTS session_runtime_projector_events
    ON session_runtime_events;
CREATE POLICY session_runtime_projector_events ON session_runtime_events
    FOR SELECT TO memoria_session_projector
    USING (current_user = 'memoria_session_projector');

DROP POLICY IF EXISTS session_runtime_maintenance_contexts
    ON session_runtime_contexts;
CREATE POLICY session_runtime_maintenance_contexts ON session_runtime_contexts
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');
DROP POLICY IF EXISTS session_runtime_maintenance_profiles
    ON session_runtime_profiles;
CREATE POLICY session_runtime_maintenance_profiles ON session_runtime_profiles
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');
DROP POLICY IF EXISTS session_runtime_maintenance_receipts
    ON session_runtime_profile_receipts;
CREATE POLICY session_runtime_maintenance_receipts ON session_runtime_profile_receipts
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');
DROP POLICY IF EXISTS session_runtime_maintenance_events
    ON session_runtime_events;
CREATE POLICY session_runtime_maintenance_events ON session_runtime_events
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');
DROP POLICY IF EXISTS session_runtime_maintenance_outbox
    ON session_runtime_outbox;
CREATE POLICY session_runtime_maintenance_outbox ON session_runtime_outbox
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');
DROP POLICY IF EXISTS session_runtime_maintenance_idempotency
    ON session_runtime_idempotency;
CREATE POLICY session_runtime_maintenance_idempotency ON session_runtime_idempotency
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');
DROP POLICY IF EXISTS session_runtime_worker_tool_effect_intents
    ON session_runtime_tool_effect_intents;
CREATE POLICY session_runtime_worker_tool_effect_intents
    ON session_runtime_tool_effect_intents
    FOR SELECT TO memoria_session_worker
    USING (current_user = 'memoria_session_worker');
DROP POLICY IF EXISTS session_runtime_worker_tool_effect_outbox
    ON session_runtime_tool_effect_outbox;
CREATE POLICY session_runtime_worker_tool_effect_outbox
    ON session_runtime_tool_effect_outbox
    FOR SELECT TO memoria_session_worker
    USING (current_user = 'memoria_session_worker');
DROP POLICY IF EXISTS session_runtime_maintenance_tool_effect_intents
    ON session_runtime_tool_effect_intents;
CREATE POLICY session_runtime_maintenance_tool_effect_intents
    ON session_runtime_tool_effect_intents
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');
DROP POLICY IF EXISTS session_runtime_maintenance_tool_effect_outbox
    ON session_runtime_tool_effect_outbox;
CREATE POLICY session_runtime_maintenance_tool_effect_outbox
    ON session_runtime_tool_effect_outbox
    FOR SELECT TO memoria_session_maintenance
    USING (current_user = 'memoria_session_maintenance');

CREATE OR REPLACE FUNCTION session_runtime_assert_action_context(
    p_session_id text,
    p_runtime_profile_id text,
    p_actor_id text,
    p_device_id text,
    p_binding_id text,
    p_binding_version integer,
    p_session_epoch integer
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_assert_action_context$
DECLARE
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    authenticated_subject text := NULLIF(
        current_setting('app.authenticated_subject', true), ''
    );
    authenticated_binding text := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    locked_binding jsonb;
BEGIN
    IF authenticated_actor IS DISTINCT FROM p_actor_id
       OR authenticated_device IS DISTINCT FROM p_device_id THEN
        RAISE EXCEPTION 'authenticated action context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    locked_binding := action_identity_lock_binding(
        p_actor_id, p_device_id, p_binding_version, CURRENT_TIMESTAMP
    );
    authenticated_binding := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    IF locked_binding IS NULL
       OR locked_binding ->> 'binding_id' <> p_binding_id
       OR authenticated_binding IS DISTINCT FROM p_binding_id THEN
        RAISE EXCEPTION 'binding action authority is unavailable'
            USING ERRCODE = 'SR403';
    END IF;
    PERFORM 1 FROM session_runtime_contexts
    WHERE session_id = p_session_id
      AND current_runtime_profile_id = p_runtime_profile_id
      AND actor_id = p_actor_id
      AND device_id = p_device_id
      AND binding_id = p_binding_id
      AND binding_version = p_binding_version
      AND session_epoch = p_session_epoch
      AND active_subject_id IS NOT DISTINCT FROM authenticated_subject
      AND state IN ('provisioning', 'active')
    FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session action authority is stale'
            USING ERRCODE = 'SR412';
    END IF;
    RETURN true;
END
$session_runtime_assert_action_context$;

CREATE OR REPLACE FUNCTION session_runtime_prepare_initial(
    p_context jsonb,
    p_request_hash text,
    p_idempotency_key text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_prepare_initial$
DECLARE
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    authenticated_binding text := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    existing session_runtime_idempotency%ROWTYPE;
    locked_binding jsonb;
BEGIN
    IF authenticated_actor IS NULL OR authenticated_device IS NULL
       OR p_context ->> 'actor_id' <> authenticated_actor
       OR p_context ->> 'device_id' <> authenticated_device THEN
        RAISE EXCEPTION 'authenticated session context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    locked_binding := action_identity_lock_binding(
        authenticated_actor,
        authenticated_device,
        (p_context ->> 'binding_version')::integer,
        (p_context ->> 'created_at')::timestamptz
    );
    authenticated_binding := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    IF locked_binding IS NULL
       OR locked_binding ->> 'binding_id' <> p_context ->> 'binding_id'
       OR authenticated_binding IS DISTINCT FROM p_context ->> 'binding_id' THEN
        RAISE EXCEPTION 'binding authority rejected session start'
            USING ERRCODE = 'SR403';
    END IF;
    IF p_request_hash !~ '^[a-f0-9]{64}$'
       OR char_length(p_idempotency_key) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'invalid idempotency fence' USING ERRCODE = 'SR400';
    END IF;

    INSERT INTO session_runtime_idempotency (
        actor_id, idempotency_key, operation, request_hash,
        session_id, binding_id, result_runtime_profile_id,
        created_at, completed_at
    ) VALUES (
        authenticated_actor, p_idempotency_key, 'session.start', p_request_hash,
        p_context ->> 'session_id', authenticated_binding, NULL,
        (p_context ->> 'created_at')::timestamptz, NULL
    ) ON CONFLICT DO NOTHING;

    SELECT * INTO existing FROM session_runtime_idempotency
    WHERE actor_id = authenticated_actor
      AND idempotency_key = p_idempotency_key
      AND operation = 'session.start'
    FOR UPDATE;
    IF existing.request_hash <> p_request_hash
       OR existing.session_id <> p_context ->> 'session_id'
       OR existing.binding_id <> authenticated_binding THEN
        RAISE EXCEPTION 'idempotency key payload conflict'
            USING ERRCODE = 'SR409';
    END IF;
    IF existing.result_runtime_profile_id IS NOT NULL THEN
        RETURN jsonb_build_object(
            'status', 'replay',
            'runtime_profile_id', existing.result_runtime_profile_id
        );
    END IF;

    INSERT INTO session_runtime_contexts (
        session_id, actor_id, device_id, binding_id, binding_version,
        active_subject_id, subject_revision, session_epoch, profile_revision,
        current_runtime_profile_id, generation_id, turn_id, tool_epoch,
        state, created_at, updated_at
    ) VALUES (
        p_context ->> 'session_id', p_context ->> 'actor_id',
        p_context ->> 'device_id', p_context ->> 'binding_id',
        (p_context ->> 'binding_version')::integer,
        NULLIF(p_context ->> 'active_subject_id', ''),
        (p_context ->> 'subject_revision')::integer,
        (p_context ->> 'session_epoch')::integer,
        (p_context ->> 'profile_revision')::integer,
        p_context ->> 'current_runtime_profile_id',
        (p_context ->> 'generation_id')::integer,
        (p_context ->> 'turn_id')::integer,
        (p_context ->> 'tool_epoch')::integer,
        'provisioning',
        (p_context ->> 'created_at')::timestamptz,
        (p_context ->> 'updated_at')::timestamptz
    );
    RETURN jsonb_build_object('status', 'prepared');
END
$session_runtime_prepare_initial$;

CREATE OR REPLACE FUNCTION session_runtime_commit_initial(
    p_profile jsonb,
    p_profile_revision integer,
    p_event jsonb,
    p_idempotency_key text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_commit_initial$
DECLARE
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    authenticated_binding text := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    receipt_id text;
BEGIN
    PERFORM session_runtime_assert_action_context(
        p_profile ->> 'session_id', p_profile ->> 'runtime_profile_id',
        p_profile ->> 'actor_id', p_profile ->> 'device_id',
        p_profile ->> 'binding_id',
        (p_profile ->> 'binding_version')::integer,
        (p_profile ->> 'session_epoch')::integer
    );
    IF p_profile ->> 'signature_schema' <> 'runtime-profile-v2'
       OR p_profile ->> 'actor_id' <> authenticated_actor
       OR p_profile ->> 'device_id' <> authenticated_device
       OR p_profile ->> 'binding_id' <> authenticated_binding THEN
        RAISE EXCEPTION 'signed profile action context mismatch'
            USING ERRCODE = 'SR403';
    END IF;

    INSERT INTO session_runtime_profiles (
        runtime_profile_id, session_id, profile_revision, session_epoch,
        actor_id, binding_id, binding_version, active_subject_id,
        subject_revision, payload_json, signature, issued_at, expires_at
    ) VALUES (
        p_profile ->> 'runtime_profile_id', p_profile ->> 'session_id',
        p_profile_revision, (p_profile ->> 'session_epoch')::integer,
        p_profile ->> 'actor_id', p_profile ->> 'binding_id',
        (p_profile ->> 'binding_version')::integer,
        NULLIF(p_profile ->> 'active_subject_id', ''),
        (p_profile ->> 'subject_revision')::integer,
        p_profile, p_profile ->> 'signature',
        (p_profile ->> 'issued_at')::timestamptz,
        (p_profile ->> 'expires_at')::timestamptz
    );

    FOR receipt_id IN
        SELECT jsonb_array_elements_text(p_profile -> 'policy_receipt_ids')
    LOOP
        INSERT INTO session_runtime_profile_receipts (
            runtime_profile_id, receipt_id, session_id, actor_id,
            binding_id, created_at
        ) VALUES (
            p_profile ->> 'runtime_profile_id', receipt_id,
            p_profile ->> 'session_id', authenticated_actor,
            authenticated_binding, (p_profile ->> 'issued_at')::timestamptz
        );
    END LOOP;

    INSERT INTO session_runtime_events (
        event_id, event_type, session_id, event_sequence, session_epoch,
        actor_id, binding_id, payload_json, occurred_at
    ) VALUES (
        p_event ->> 'event_id', p_event ->> 'event_type',
        p_event ->> 'session_id', (p_event ->> 'event_sequence')::integer,
        (p_event ->> 'session_epoch')::integer, p_event ->> 'actor_id',
        p_event ->> 'binding_id', p_event,
        (p_event ->> 'occurred_at')::timestamptz
    );
    INSERT INTO session_runtime_outbox (
        outbox_id, event_id, session_id, actor_id, binding_id, topic,
        payload_json, status, attempts, locked_until, last_error_code,
        created_at, updated_at, delivered_at
    ) VALUES (
        'outbox-' || (p_event ->> 'event_id'), p_event ->> 'event_id',
        p_event ->> 'session_id', authenticated_actor, authenticated_binding,
        'agent.runtime_profile.refresh', p_event, 'pending', 0, NULL, NULL,
        (p_event ->> 'occurred_at')::timestamptz,
        (p_event ->> 'occurred_at')::timestamptz, NULL
    );
    UPDATE session_runtime_contexts
    SET state = 'active', updated_at = (p_profile ->> 'issued_at')::timestamptz
    WHERE session_id = p_profile ->> 'session_id'
      AND current_runtime_profile_id = p_profile ->> 'runtime_profile_id'
      AND profile_revision = p_profile_revision
      AND state = 'provisioning';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session profile CAS conflict' USING ERRCODE = 'SR412';
    END IF;
    UPDATE session_runtime_idempotency
    SET result_runtime_profile_id = p_profile ->> 'runtime_profile_id',
        completed_at = (p_profile ->> 'issued_at')::timestamptz
    WHERE actor_id = authenticated_actor
      AND idempotency_key = p_idempotency_key
      AND operation = 'session.start'
      AND result_runtime_profile_id IS NULL;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session idempotency completion conflict'
            USING ERRCODE = 'SR409';
    END IF;
    RETURN p_profile;
END
$session_runtime_commit_initial$;

CREATE OR REPLACE FUNCTION session_runtime_action_current_profile(
    p_session_id text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_action_current_profile$
DECLARE
    result jsonb;
BEGIN
    SELECT p.payload_json INTO result
    FROM session_runtime_contexts c
    JOIN session_runtime_profiles p
      ON p.runtime_profile_id = c.current_runtime_profile_id
    WHERE c.session_id = p_session_id
      AND c.actor_id = NULLIF(
          current_setting('app.authenticated_actor', true), ''
      )
      AND c.device_id = NULLIF(
          current_setting('app.authenticated_device', true), ''
      )
      AND c.binding_id = NULLIF(
          current_setting('app.authenticated_binding', true), ''
      );
    RETURN result;
END
$session_runtime_action_current_profile$;

CREATE OR REPLACE FUNCTION session_runtime_prepare_rotation(
    p_expected_runtime_profile_id text,
    p_expected_profile_revision integer,
    p_expected_session_epoch integer,
    p_profile jsonb,
    p_profile_revision integer,
    p_event jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_prepare_rotation$
DECLARE
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    authenticated_binding text := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    current_context session_runtime_contexts%ROWTYPE;
    locked_binding jsonb;
    requested_subject text := NULLIF(p_profile ->> 'active_subject_id', '');
    claimed_subject text := NULLIF(
        p_event -> 'payload' ->> 'claimed_subject_id', ''
    );
BEGIN
    IF authenticated_actor IS NULL OR authenticated_device IS NULL
       OR p_profile ->> 'actor_id' <> authenticated_actor
       OR p_profile ->> 'device_id' <> authenticated_device
       OR p_profile ->> 'signature_schema' <> 'runtime-profile-v2'
       OR p_profile_revision <> p_expected_profile_revision + 1
       OR (p_profile ->> 'session_epoch')::integer
            <> p_expected_session_epoch + 1 THEN
        RAISE EXCEPTION 'rotated profile fence is invalid'
            USING ERRCODE = 'SR400';
    END IF;
    SELECT * INTO current_context
    FROM session_runtime_contexts
    WHERE session_id = p_profile ->> 'session_id'
      AND current_runtime_profile_id = p_expected_runtime_profile_id
      AND profile_revision = p_expected_profile_revision
      AND session_epoch = p_expected_session_epoch
      AND state = 'active'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session profile CAS conflict' USING ERRCODE = 'SR412';
    END IF;
    IF current_context.device_id <> authenticated_device
       OR current_context.binding_id <> p_profile ->> 'binding_id'
       OR current_context.binding_version
            <> (p_profile ->> 'binding_version')::integer THEN
        RAISE EXCEPTION 'rotation binding fence mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    locked_binding := action_identity_lock_binding(
        authenticated_actor,
        authenticated_device,
        current_context.binding_version,
        (p_profile ->> 'issued_at')::timestamptz
    );
    authenticated_binding := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    IF locked_binding IS NULL
       OR authenticated_binding IS DISTINCT FROM current_context.binding_id THEN
        RAISE EXCEPTION 'rotation binding authority rejected actor'
            USING ERRCODE = 'SR403';
    END IF;
    IF requested_subject IS NOT NULL AND NOT action_identity_can_switch_subject(
        authenticated_actor,
        authenticated_device,
        current_context.binding_version,
        requested_subject
    ) THEN
        RAISE EXCEPTION 'subject switch authority denied'
            USING ERRCODE = 'SR403';
    END IF;
    IF requested_subject IS NULL
       AND current_context.actor_id <> authenticated_actor
       AND (
           claimed_subject IS NULL
           OR NOT action_identity_can_switch_subject(
               authenticated_actor,
               authenticated_device,
               current_context.binding_version,
               claimed_subject
           )
       ) THEN
        RAISE EXCEPTION 'cross-actor unknown transition denied'
            USING ERRCODE = 'SR403';
    END IF;
    IF (p_event ->> 'generation_id')::integer
            <> current_context.generation_id + 1
       OR (p_event ->> 'turn_id')::integer <> current_context.turn_id + 1
       OR (p_event ->> 'tool_epoch')::integer
            <> current_context.tool_epoch + 1 THEN
        RAISE EXCEPTION 'generation/tool/effect fence did not advance'
            USING ERRCODE = 'SR400';
    END IF;

    UPDATE session_runtime_contexts
    SET actor_id = authenticated_actor,
        active_subject_id = requested_subject,
        subject_revision = (p_profile ->> 'subject_revision')::integer,
        session_epoch = (p_profile ->> 'session_epoch')::integer,
        profile_revision = p_profile_revision,
        current_runtime_profile_id = p_profile ->> 'runtime_profile_id',
        generation_id = (p_event ->> 'generation_id')::integer,
        turn_id = (p_event ->> 'turn_id')::integer,
        tool_epoch = (p_event ->> 'tool_epoch')::integer,
        state = 'provisioning',
        updated_at = (p_profile ->> 'issued_at')::timestamptz
    WHERE session_id = current_context.session_id;
    RETURN jsonb_build_object('status', 'prepared');
END
$session_runtime_prepare_rotation$;

CREATE OR REPLACE FUNCTION session_runtime_commit_rotation(
    p_profile jsonb,
    p_profile_revision integer,
    p_event jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_commit_rotation$
DECLARE
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_binding text := NULLIF(
        current_setting('app.authenticated_binding', true), ''
    );
    receipt_id text;
BEGIN
    PERFORM session_runtime_assert_action_context(
        p_profile ->> 'session_id', p_profile ->> 'runtime_profile_id',
        p_profile ->> 'actor_id', p_profile ->> 'device_id',
        p_profile ->> 'binding_id',
        (p_profile ->> 'binding_version')::integer,
        (p_profile ->> 'session_epoch')::integer
    );
    IF p_profile ->> 'actor_id' <> authenticated_actor
       OR p_profile ->> 'binding_id' <> authenticated_binding THEN
        RAISE EXCEPTION 'rotation commit authority mismatch'
            USING ERRCODE = 'SR403';
    END IF;

    INSERT INTO session_runtime_profiles (
        runtime_profile_id, session_id, profile_revision, session_epoch,
        actor_id, binding_id, binding_version, active_subject_id,
        subject_revision, payload_json, signature, issued_at, expires_at
    ) VALUES (
        p_profile ->> 'runtime_profile_id', p_profile ->> 'session_id',
        p_profile_revision, (p_profile ->> 'session_epoch')::integer,
        p_profile ->> 'actor_id', p_profile ->> 'binding_id',
        (p_profile ->> 'binding_version')::integer,
        NULLIF(p_profile ->> 'active_subject_id', ''),
        (p_profile ->> 'subject_revision')::integer,
        p_profile, p_profile ->> 'signature',
        (p_profile ->> 'issued_at')::timestamptz,
        (p_profile ->> 'expires_at')::timestamptz
    );
    FOR receipt_id IN
        SELECT jsonb_array_elements_text(p_profile -> 'policy_receipt_ids')
    LOOP
        INSERT INTO session_runtime_profile_receipts (
            runtime_profile_id, receipt_id, session_id, actor_id,
            binding_id, created_at
        ) VALUES (
            p_profile ->> 'runtime_profile_id', receipt_id,
            p_profile ->> 'session_id', authenticated_actor,
            authenticated_binding, (p_profile ->> 'issued_at')::timestamptz
        );
    END LOOP;
    INSERT INTO session_runtime_events (
        event_id, event_type, session_id, event_sequence, session_epoch,
        actor_id, binding_id, payload_json, occurred_at
    ) VALUES (
        p_event ->> 'event_id', p_event ->> 'event_type',
        p_event ->> 'session_id', (p_event ->> 'event_sequence')::integer,
        (p_event ->> 'session_epoch')::integer, p_event ->> 'actor_id',
        p_event ->> 'binding_id', p_event,
        (p_event ->> 'occurred_at')::timestamptz
    );
    INSERT INTO session_runtime_outbox (
        outbox_id, event_id, session_id, actor_id, binding_id, topic,
        payload_json, status, attempts, locked_until, last_error_code,
        created_at, updated_at, delivered_at
    ) VALUES (
        'outbox-' || (p_event ->> 'event_id'), p_event ->> 'event_id',
        p_event ->> 'session_id', authenticated_actor, authenticated_binding,
        'agent.runtime_profile.refresh', p_event, 'pending', 0, NULL, NULL,
        (p_event ->> 'occurred_at')::timestamptz,
        (p_event ->> 'occurred_at')::timestamptz, NULL
    );
    UPDATE session_runtime_contexts
    SET state = 'active', updated_at = (p_profile ->> 'issued_at')::timestamptz
    WHERE session_id = p_profile ->> 'session_id'
      AND current_runtime_profile_id = p_profile ->> 'runtime_profile_id'
      AND profile_revision = p_profile_revision
      AND actor_id = authenticated_actor
      AND state = 'provisioning';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'rotation commit CAS conflict' USING ERRCODE = 'SR412';
    END IF;
    RETURN p_profile;
END
$session_runtime_commit_rotation$;

CREATE OR REPLACE FUNCTION session_runtime_fail_session(
    p_session_id text,
    p_expected_runtime_profile_id text,
    p_expected_session_epoch integer,
    p_event jsonb
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_fail_session$
DECLARE
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    context_row session_runtime_contexts%ROWTYPE;
BEGIN
    SELECT * INTO context_row
    FROM session_runtime_contexts
    WHERE session_id = p_session_id
      AND current_runtime_profile_id = p_expected_runtime_profile_id
      AND session_epoch = p_expected_session_epoch
      AND actor_id = authenticated_actor
      AND device_id = authenticated_device
      AND state = 'active'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'failed-session CAS conflict' USING ERRCODE = 'SR412';
    END IF;
    PERFORM action_identity_lock_binding(
        authenticated_actor,
        authenticated_device,
        context_row.binding_version,
        (p_event ->> 'occurred_at')::timestamptz
    );
    IF (p_event ->> 'session_epoch')::integer <> context_row.session_epoch + 1
       OR (p_event ->> 'generation_id')::integer
            <> context_row.generation_id + 1
       OR (p_event ->> 'turn_id')::integer <> context_row.turn_id + 1
       OR (p_event ->> 'tool_epoch')::integer <> context_row.tool_epoch + 1 THEN
        RAISE EXCEPTION 'failed-session fence did not advance'
            USING ERRCODE = 'SR400';
    END IF;
    UPDATE session_runtime_contexts
    SET state = 'failed', active_subject_id = NULL,
        session_epoch = context_row.session_epoch + 1,
        generation_id = context_row.generation_id + 1,
        turn_id = context_row.turn_id + 1,
        tool_epoch = context_row.tool_epoch + 1,
        updated_at = (p_event ->> 'occurred_at')::timestamptz
    WHERE session_id = p_session_id;
    INSERT INTO session_runtime_events (
        event_id, event_type, session_id, event_sequence, session_epoch,
        actor_id, binding_id, payload_json, occurred_at
    ) VALUES (
        p_event ->> 'event_id', p_event ->> 'event_type', p_session_id,
        (p_event ->> 'event_sequence')::integer,
        (p_event ->> 'session_epoch')::integer, authenticated_actor,
        context_row.binding_id, p_event,
        (p_event ->> 'occurred_at')::timestamptz
    );
    INSERT INTO session_runtime_outbox (
        outbox_id, event_id, session_id, actor_id, binding_id, topic,
        payload_json, status, attempts, locked_until, last_error_code,
        created_at, updated_at, delivered_at
    ) VALUES (
        'outbox-' || (p_event ->> 'event_id'), p_event ->> 'event_id',
        p_session_id, authenticated_actor, context_row.binding_id,
        'agent.session.failed', p_event, 'pending', 0, NULL, NULL,
        (p_event ->> 'occurred_at')::timestamptz,
        (p_event ->> 'occurred_at')::timestamptz, NULL
    );
    RETURN true;
END
$session_runtime_fail_session$;

-- Session action fence authority.  The action login can only advance one
-- legal monotonic fence step (new turn / interrupt / tool) on the
-- authoritative context row and lock an exact-fence policy receipt; it never
-- holds direct table privileges.  Fence payloads are strictly validated:
-- required keys only, bounded text/number types, authenticated actor/device
-- and one frozen binding snapshot, full session/profile/subject context
-- equality against the FOR UPDATE context row, then exactly one legal
-- transition of generation/turn/tool epochs.
CREATE OR REPLACE FUNCTION session_runtime_fence_json_valid(
    p_fence jsonb,
    p_required_keys text[]
) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT
AS $session_runtime_fence_json_valid$
    SELECT jsonb_typeof(p_fence) = 'object'
       AND p_fence ?& p_required_keys
       AND p_fence - p_required_keys = '{}'::jsonb
$session_runtime_fence_json_valid$;

CREATE OR REPLACE FUNCTION session_runtime_fence_text_valid(
    p_value jsonb,
    p_min_len integer,
    p_max_len integer
) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT
AS $session_runtime_fence_text_valid$
    SELECT jsonb_typeof(p_value) = 'string'
       AND char_length(p_value #>> '{}') BETWEEN p_min_len AND p_max_len
$session_runtime_fence_text_valid$;

CREATE OR REPLACE FUNCTION session_runtime_fence_int_valid(
    p_value jsonb,
    p_min integer
) RETURNS boolean
LANGUAGE sql IMMUTABLE STRICT
AS $session_runtime_fence_int_valid$
    SELECT jsonb_typeof(p_value) = 'number'
       AND char_length(p_value #>> '{}') BETWEEN 1 AND 9
       AND (p_value #>> '{}') ~ '^[0-9]+$'
       AND (p_value #>> '{}')::integer >= p_min
$session_runtime_fence_int_valid$;

CREATE OR REPLACE FUNCTION session_runtime_advance_action_fence(
    p_fence jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_advance_action_fence$
DECLARE
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    locked_binding jsonb;
    current_context session_runtime_contexts%ROWTYPE;
    fence_kind text;
    claimed_active_subject text;
    occurred_at timestamptz;
    next_generation_id integer;
    next_turn_id integer;
    next_tool_epoch integer;
BEGIN
    IF NOT session_runtime_fence_json_valid(p_fence, ARRAY[
            'session_id', 'runtime_profile_id', 'actor_id', 'device_id',
            'binding_id', 'binding_version', 'session_epoch',
            'active_subject_id', 'subject_revision', 'fence_kind',
            'generation_id', 'turn_id', 'tool_epoch', 'occurred_at'
        ])
       OR NOT session_runtime_fence_text_valid(p_fence -> 'session_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(
           p_fence -> 'runtime_profile_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(p_fence -> 'actor_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(p_fence -> 'device_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(p_fence -> 'binding_id', 1, 128)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'binding_version', 1)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'session_epoch', 1)
       OR jsonb_typeof(p_fence -> 'active_subject_id') NOT IN ('string', 'null')
       OR (
           jsonb_typeof(p_fence -> 'active_subject_id') = 'string'
           AND NOT session_runtime_fence_text_valid(
               p_fence -> 'active_subject_id', 1, 128)
       )
       OR NOT session_runtime_fence_int_valid(p_fence -> 'subject_revision', 0)
       OR p_fence ->> 'fence_kind' NOT IN ('turn', 'interrupt', 'tool')
       OR NOT session_runtime_fence_int_valid(p_fence -> 'generation_id', 0)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'turn_id', 0)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'tool_epoch', 0)
       OR NOT session_runtime_fence_text_valid(p_fence -> 'occurred_at', 20, 35)
       OR (p_fence ->> 'occurred_at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T'
    THEN
        RAISE EXCEPTION 'invalid action fence payload' USING ERRCODE = 'SR400';
    END IF;
    occurred_at := (p_fence ->> 'occurred_at')::timestamptz;
    IF authenticated_actor IS DISTINCT FROM p_fence ->> 'actor_id'
       OR authenticated_device IS DISTINCT FROM p_fence ->> 'device_id' THEN
        RAISE EXCEPTION 'authenticated action context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    locked_binding := action_identity_lock_binding(
        authenticated_actor,
        authenticated_device,
        (p_fence ->> 'binding_version')::integer,
        occurred_at
    );
    IF locked_binding IS NULL
       OR locked_binding ->> 'binding_id' <> p_fence ->> 'binding_id'
       OR NULLIF(current_setting('app.authenticated_binding', true), '')
            IS DISTINCT FROM p_fence ->> 'binding_id' THEN
        RAISE EXCEPTION 'binding action authority is unavailable'
            USING ERRCODE = 'SR403';
    END IF;
    SELECT * INTO current_context
    FROM session_runtime_contexts
    WHERE session_id = p_fence ->> 'session_id'
      AND state = 'active'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session action fence is stale'
            USING ERRCODE = 'SR412';
    END IF;
    claimed_active_subject := NULLIF(p_fence ->> 'active_subject_id', '');
    IF current_context.current_runtime_profile_id
            <> p_fence ->> 'runtime_profile_id'
       OR current_context.actor_id <> p_fence ->> 'actor_id'
       OR current_context.device_id <> p_fence ->> 'device_id'
       OR current_context.binding_id <> p_fence ->> 'binding_id'
       OR current_context.binding_version
            <> (p_fence ->> 'binding_version')::integer
       OR current_context.session_epoch
            <> (p_fence ->> 'session_epoch')::integer
       OR current_context.active_subject_id IS DISTINCT FROM claimed_active_subject
       OR current_context.subject_revision
            <> (p_fence ->> 'subject_revision')::integer THEN
        RAISE EXCEPTION 'session action context is stale'
            USING ERRCODE = 'SR412';
    END IF;
    fence_kind := p_fence ->> 'fence_kind';
    next_generation_id := (p_fence ->> 'generation_id')::integer;
    next_turn_id := (p_fence ->> 'turn_id')::integer;
    next_tool_epoch := (p_fence ->> 'tool_epoch')::integer;
    IF NOT (
        (fence_kind = 'turn'
         AND next_turn_id = current_context.turn_id + 1
         AND next_generation_id = current_context.generation_id + 1
         AND next_tool_epoch = current_context.tool_epoch)
        OR (fence_kind = 'interrupt'
            AND next_turn_id = current_context.turn_id
            AND next_generation_id = current_context.generation_id + 1
            AND next_tool_epoch = current_context.tool_epoch)
        OR (fence_kind = 'tool'
            AND next_turn_id = current_context.turn_id
            AND next_generation_id = current_context.generation_id + 1
            AND next_tool_epoch = current_context.tool_epoch + 1)
    ) THEN
        RAISE EXCEPTION 'action fence did not legally advance'
            USING ERRCODE = 'SR409';
    END IF;
    UPDATE session_runtime_contexts
    SET generation_id = next_generation_id,
        turn_id = next_turn_id,
        tool_epoch = next_tool_epoch,
        updated_at = occurred_at
    WHERE session_id = current_context.session_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'session action fence CAS conflict'
            USING ERRCODE = 'SR412';
    END IF;
    RETURN jsonb_build_object(
        'status', 'advanced',
        'session_id', current_context.session_id,
        'runtime_profile_id', current_context.current_runtime_profile_id,
        'session_epoch', current_context.session_epoch,
        'fence_kind', fence_kind,
        'generation_id', next_generation_id,
        'turn_id', next_turn_id,
        'tool_epoch', next_tool_epoch,
        'occurred_at', occurred_at
    );
END
$session_runtime_advance_action_fence$;

CREATE OR REPLACE FUNCTION session_runtime_action_receipt_authority(
    p_receipt_id text,
    p_fence jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_action_receipt_authority$
DECLARE
    result jsonb;
    context_epochs record;
    authenticated_subject text := NULLIF(
        current_setting('app.authenticated_subject', true), ''
    );
    claimed_generation_id integer;
    claimed_turn_id integer;
    claimed_tool_epoch integer;
BEGIN
    IF p_receipt_id IS NULL
       OR char_length(p_receipt_id) NOT BETWEEN 1 AND 192
       OR NOT session_runtime_fence_json_valid(p_fence, ARRAY[
            'session_id', 'runtime_profile_id', 'actor_id', 'device_id',
            'binding_id', 'binding_version', 'session_epoch',
            'subject_revision', 'generation_id', 'turn_id', 'tool_epoch'
        ])
       OR NOT session_runtime_fence_text_valid(p_fence -> 'session_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(
           p_fence -> 'runtime_profile_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(p_fence -> 'actor_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(p_fence -> 'device_id', 1, 128)
       OR NOT session_runtime_fence_text_valid(p_fence -> 'binding_id', 1, 128)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'binding_version', 1)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'session_epoch', 1)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'subject_revision', 0)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'generation_id', 0)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'turn_id', 0)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'tool_epoch', 0)
    THEN
        RAISE EXCEPTION 'invalid receipt authority payload'
            USING ERRCODE = 'SR400';
    END IF;
    IF NOT policy_lock_receipt_v2(p_receipt_id) THEN
        RETURN NULL;
    END IF;
    SELECT to_jsonb(r) INTO result
    FROM policy_receipts_v2 r
    WHERE r.receipt_id = p_receipt_id;
    IF result IS NULL THEN
        RETURN NULL;
    END IF;
    claimed_generation_id := (p_fence ->> 'generation_id')::integer;
    claimed_turn_id := (p_fence ->> 'turn_id')::integer;
    claimed_tool_epoch := (p_fence ->> 'tool_epoch')::integer;
    IF result ->> 'session_id' <> p_fence ->> 'session_id'
       OR result ->> 'runtime_profile_id' <> p_fence ->> 'runtime_profile_id'
       OR result ->> 'actor_id' <> p_fence ->> 'actor_id'
       OR result ->> 'device_id' <> p_fence ->> 'device_id'
       OR result ->> 'binding_id' <> p_fence ->> 'binding_id'
       OR (result ->> 'binding_version')::integer
            <> (p_fence ->> 'binding_version')::integer
       OR (result ->> 'session_epoch')::integer
            <> (p_fence ->> 'session_epoch')::integer
       OR (result ->> 'subject_revision')::integer
            <> (p_fence ->> 'subject_revision')::integer
       OR NULLIF(result ->> 'subject_id', '')
            IS DISTINCT FROM authenticated_subject
       OR (result -> 'action_resource_fence' ->> 'generation_id')::integer
            <> claimed_generation_id
       OR (result -> 'action_resource_fence' ->> 'turn_id')::integer
            <> claimed_turn_id
       OR (result -> 'action_resource_fence' ->> 'tool_epoch')::integer
            <> claimed_tool_epoch THEN
        RAISE EXCEPTION 'receipt action fence mismatch'
            USING ERRCODE = 'SR412';
    END IF;
    PERFORM session_runtime_assert_action_context(
        p_fence ->> 'session_id', p_fence ->> 'runtime_profile_id',
        p_fence ->> 'actor_id', p_fence ->> 'device_id',
        p_fence ->> 'binding_id',
        (p_fence ->> 'binding_version')::integer,
        (p_fence ->> 'session_epoch')::integer
    );
    SELECT generation_id, turn_id, tool_epoch INTO context_epochs
    FROM session_runtime_contexts
    WHERE session_id = p_fence ->> 'session_id'
      AND current_runtime_profile_id = p_fence ->> 'runtime_profile_id';
    IF NOT FOUND
       OR context_epochs.generation_id <> claimed_generation_id
       OR context_epochs.turn_id <> claimed_turn_id
       OR context_epochs.tool_epoch <> claimed_tool_epoch THEN
        RAISE EXCEPTION 'session action fence is not current'
            USING ERRCODE = 'SR412';
    END IF;
    RETURN result;
END
$session_runtime_action_receipt_authority$;

CREATE OR REPLACE FUNCTION session_runtime_action_effect_context(
    p_session_id text,
    p_runtime_profile_id text,
    p_fence jsonb,
    p_profile jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_action_effect_context$
DECLARE
    current_context session_runtime_contexts%ROWTYPE;
    current_profile session_runtime_profiles%ROWTYPE;
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    authenticated_subject text := NULLIF(
        current_setting('app.authenticated_subject', true), ''
    );
BEGIN
    IF session_user <> 'memoria_action_executor'
       OR authenticated_actor IS NULL
       OR authenticated_device IS NULL
       OR NOT session_runtime_fence_json_valid(p_fence, ARRAY[
            'session_id', 'session_epoch', 'generation_id',
            'turn_id', 'tool_epoch'
       ])
       OR NOT session_runtime_fence_text_valid(p_fence -> 'session_id', 1, 128)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'session_epoch', 1)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'generation_id', 0)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'turn_id', 0)
       OR NOT session_runtime_fence_int_valid(p_fence -> 'tool_epoch', 0)
       OR p_session_id IS DISTINCT FROM p_fence ->> 'session_id'
       OR p_profile IS NULL
       OR jsonb_typeof(p_profile) <> 'object' THEN
        RAISE EXCEPTION 'invalid tool effect authority context'
            USING ERRCODE = 'SR400';
    END IF;
    SELECT * INTO current_context
    FROM session_runtime_contexts
    WHERE session_id = p_session_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tool effect Session authority is unavailable'
            USING ERRCODE = 'SR412';
    END IF;
    IF current_context.state <> 'active'
       OR current_context.current_runtime_profile_id <> p_runtime_profile_id
       OR current_context.actor_id <> authenticated_actor
       OR current_context.device_id <> authenticated_device
       OR current_context.active_subject_id IS DISTINCT FROM authenticated_subject
       OR current_context.session_epoch <> (p_fence ->> 'session_epoch')::integer
       OR current_context.generation_id <> (p_fence ->> 'generation_id')::integer
       OR current_context.turn_id <> (p_fence ->> 'turn_id')::integer
       OR current_context.tool_epoch <> (p_fence ->> 'tool_epoch')::integer THEN
        RAISE EXCEPTION 'tool effect Session fence is stale or forged'
            USING ERRCODE = 'SR412';
    END IF;
    SELECT * INTO current_profile
    FROM session_runtime_profiles
    WHERE runtime_profile_id = current_context.current_runtime_profile_id
    FOR SHARE;
    IF NOT FOUND OR current_profile.payload_json IS DISTINCT FROM p_profile THEN
        RAISE EXCEPTION 'tool effect signed Runtime Profile is not current'
            USING ERRCODE = 'SR403';
    END IF;
    RETURN jsonb_build_object(
        'context', to_jsonb(current_context),
        'profile', current_profile.payload_json
    );
END
$session_runtime_action_effect_context$;

CREATE OR REPLACE FUNCTION session_runtime_commit_tool_effect(
    p_intent jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_commit_tool_effect$
DECLARE
    current_context session_runtime_contexts%ROWTYPE;
    current_profile session_runtime_profiles%ROWTYPE;
    policy_row policy_receipts_v2%ROWTYPE;
    existing session_runtime_tool_effect_intents%ROWTYPE;
    locked_receipt_id text;
    intent_id text;
    commit_time timestamptz;
    authenticated_actor text := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_device text := NULLIF(
        current_setting('app.authenticated_device', true), ''
    );
    authenticated_subject text := NULLIF(
        current_setting('app.authenticated_subject', true), ''
    );
    fence jsonb := p_intent -> 'fence';
BEGIN
    IF session_user <> 'memoria_action_executor'
       OR NOT session_runtime_fence_json_valid(p_intent, ARRAY[
            'session_id', 'fence', 'runtime_profile', 'policy_receipt',
            'capability', 'purpose', 'resource_id', 'evidence_refs',
            'idempotency_key', 'intent', 'payload', 'payload_sha256',
            'fence_fingerprint', 'authority_now'
       ])
       OR jsonb_typeof(p_intent -> 'payload') <> 'object'
       OR jsonb_typeof(p_intent -> 'runtime_profile') <> 'object'
       OR jsonb_typeof(p_intent -> 'policy_receipt') <> 'object'
       OR jsonb_typeof(p_intent -> 'evidence_refs') <> 'array'
       OR char_length(p_intent ->> 'idempotency_key') NOT BETWEEN 1 AND 512
       OR char_length(p_intent ->> 'intent') NOT BETWEEN 1 AND 512
       OR p_intent ->> 'payload_sha256' !~ '^[a-f0-9]{64}$'
       OR p_intent ->> 'fence_fingerprint' !~ '^[a-f0-9]{64}$'
       OR p_intent ->> 'authority_now' IS NULL
       OR p_intent ->> 'session_id' IS NULL
       OR p_intent ->> 'capability' IS NULL
       OR p_intent ->> 'purpose' IS NULL
       OR p_intent ->> 'resource_id' IS NULL
       OR NOT session_runtime_fence_json_valid(fence, ARRAY[
            'session_id', 'session_epoch', 'generation_id',
            'turn_id', 'tool_epoch'
       ]) THEN
        RAISE EXCEPTION 'invalid durable tool effect intent'
            USING ERRCODE = 'SR400';
    END IF;
    commit_time := (p_intent ->> 'authority_now')::timestamptz;
    IF commit_time IS NULL THEN
        RAISE EXCEPTION 'tool effect authority time is invalid'
            USING ERRCODE = 'SR400';
    END IF;
    IF authenticated_actor IS NULL
       OR authenticated_device IS NULL
       OR p_intent ->> 'session_id' <> fence ->> 'session_id' THEN
        RAISE EXCEPTION 'authenticated tool effect context is missing'
            USING ERRCODE = 'SR403';
    END IF;
    SELECT * INTO current_context
    FROM session_runtime_contexts
    WHERE session_id = p_intent ->> 'session_id'
    FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tool effect Session authority is unavailable'
            USING ERRCODE = 'SR412';
    END IF;
    IF current_context.state <> 'active'
       OR current_context.actor_id <> authenticated_actor
       OR current_context.device_id <> authenticated_device
       OR current_context.active_subject_id IS DISTINCT FROM authenticated_subject
       OR current_context.current_runtime_profile_id
            <> p_intent -> 'runtime_profile' ->> 'runtime_profile_id'
       OR current_context.session_epoch <> (fence ->> 'session_epoch')::integer
       OR current_context.generation_id <> (fence ->> 'generation_id')::integer
       OR current_context.turn_id <> (fence ->> 'turn_id')::integer
       OR current_context.tool_epoch <> (fence ->> 'tool_epoch')::integer THEN
        RAISE EXCEPTION 'tool effect Session fence is stale or forged'
            USING ERRCODE = 'SR412';
    END IF;
    SELECT * INTO current_profile
    FROM session_runtime_profiles
    WHERE runtime_profile_id = current_context.current_runtime_profile_id
    FOR SHARE;
    IF NOT FOUND OR current_profile.payload_json IS DISTINCT FROM p_intent -> 'runtime_profile' THEN
        RAISE EXCEPTION 'tool effect signed Runtime Profile is not current'
            USING ERRCODE = 'SR403';
    END IF;
    locked_receipt_id := p_intent -> 'policy_receipt' ->> 'receipt_id';
    IF locked_receipt_id IS NULL
       OR p_intent -> 'evidence_refs' <> jsonb_build_array(locked_receipt_id)
       OR NOT policy_lock_receipt_v2(locked_receipt_id) THEN
        RAISE EXCEPTION 'tool effect Policy receipt is unavailable'
            USING ERRCODE = 'SR412';
    END IF;
    SELECT * INTO policy_row
    FROM policy_receipts_v2
    WHERE policy_receipts_v2.receipt_id = locked_receipt_id
    ;
    IF NOT FOUND
       OR policy_row.effect NOT IN ('allow', 'allow_with_obligations')
       OR policy_row.actor_id <> authenticated_actor
       OR policy_row.subject_id IS DISTINCT FROM authenticated_subject
       OR policy_row.session_id <> current_context.session_id
       OR policy_row.runtime_profile_id <> current_context.current_runtime_profile_id
       OR policy_row.session_epoch <> current_context.session_epoch
       OR policy_row.action_resource_fence ->> 'generation_id'
            <> fence ->> 'generation_id'
       OR policy_row.action_resource_fence ->> 'turn_id'
            <> fence ->> 'turn_id'
       OR policy_row.action_resource_fence ->> 'tool_epoch'
            <> fence ->> 'tool_epoch'
       OR policy_row.capability <> p_intent ->> 'capability'
       OR policy_row.purpose <> p_intent ->> 'purpose'
       OR policy_row.action_resource_fence ->> 'action_resource_id'
            <> p_intent ->> 'resource_id'
       OR policy_row.action_fence_hash <> p_intent -> 'policy_receipt' ->> 'action_fence_hash'
       OR policy_row.context_hash <> p_intent -> 'policy_receipt' ->> 'context_hash'
       OR commit_time < policy_row.created_at
       OR commit_time >= policy_row.expires_at
       OR commit_time < (policy_row.action_resource_fence ->> 'issued_at')::timestamptz
       OR commit_time >= (policy_row.action_resource_fence ->> 'valid_until')::timestamptz THEN
        RAISE EXCEPTION 'tool effect Policy receipt is stale, revoked, or mismatched'
            USING ERRCODE = 'SR412';
    END IF;
    IF NOT session_runtime_assert_action_context(
        current_context.session_id,
        current_context.current_runtime_profile_id,
        current_context.actor_id,
        current_context.device_id,
        current_context.binding_id,
        current_context.binding_version,
        current_context.session_epoch
    ) THEN
        RAISE EXCEPTION 'tool effect action context is unavailable'
            USING ERRCODE = 'SR403';
    END IF;

    SELECT * INTO existing
    FROM session_runtime_tool_effect_intents
    WHERE idempotency_key = p_intent ->> 'idempotency_key'
    FOR UPDATE;
    IF FOUND THEN
        IF existing.payload_sha256 <> p_intent ->> 'payload_sha256'
           OR existing.payload_json IS DISTINCT FROM p_intent -> 'payload'
           OR existing.intent <> p_intent ->> 'intent'
           OR existing.session_id <> current_context.session_id
           OR existing.actor_id <> current_context.actor_id
           OR existing.subject_id <> current_context.active_subject_id
           OR existing.runtime_profile_id
                <> current_context.current_runtime_profile_id
           OR existing.session_epoch <> current_context.session_epoch
           OR existing.generation_id <> (fence ->> 'generation_id')::integer
           OR existing.turn_id <> (fence ->> 'turn_id')::integer
           OR existing.tool_epoch <> (fence ->> 'tool_epoch')::integer
           OR existing.policy_receipt_id <> locked_receipt_id
           OR existing.capability <> p_intent ->> 'capability'
           OR existing.purpose <> p_intent ->> 'purpose'
           OR existing.resource_id <> p_intent ->> 'resource_id'
           OR existing.fence_fingerprint <> p_intent ->> 'fence_fingerprint'
           OR existing.evidence_refs IS DISTINCT FROM p_intent -> 'evidence_refs' THEN
            RAISE EXCEPTION 'tool effect idempotency key payload conflict'
                USING ERRCODE = 'SR409';
        END IF;
        RETURN jsonb_build_object(
            'intent_id', existing.intent_id,
            'idempotency_key', existing.idempotency_key,
            'committed_at', existing.committed_at,
            'fence_fingerprint', existing.fence_fingerprint,
            'authority_revision', existing.authority_revision,
            'capability', existing.capability,
            'purpose', existing.purpose,
            'resource_id', existing.resource_id,
            'intent_sha256', existing.payload_sha256,
            'evidence_refs', existing.evidence_refs
        );
    END IF;
    intent_id := 'effect-' || md5(
        p_intent ->> 'idempotency_key' || ':' || clock_timestamp()::text
    );
    INSERT INTO session_runtime_tool_effect_intents (
        intent_id, idempotency_key, intent, payload_json, payload_sha256,
        session_id, actor_id, subject_id, device_id, binding_id,
        binding_version, runtime_profile_id, profile_revision, session_epoch,
        generation_id, turn_id, tool_epoch, policy_receipt_id, capability,
        purpose, resource_id, evidence_refs, action_fence_hash, context_hash,
        fence_fingerprint, authority_revision, status, created_at, updated_at,
        committed_at
    ) VALUES (
        intent_id, p_intent ->> 'idempotency_key', p_intent ->> 'intent',
        p_intent -> 'payload', p_intent ->> 'payload_sha256',
        current_context.session_id, current_context.actor_id,
        current_context.active_subject_id, current_context.device_id,
        current_context.binding_id, current_context.binding_version,
        current_context.current_runtime_profile_id,
        current_context.profile_revision, current_context.session_epoch,
        (fence ->> 'generation_id')::integer,
        (fence ->> 'turn_id')::integer, (fence ->> 'tool_epoch')::integer,
        locked_receipt_id, policy_row.capability, policy_row.purpose,
        policy_row.action_resource_fence ->> 'action_resource_id',
        p_intent -> 'evidence_refs', policy_row.action_fence_hash,
        policy_row.context_hash, p_intent ->> 'fence_fingerprint',
        current_context.profile_revision,
        'committed', commit_time, commit_time, commit_time
    );
    INSERT INTO session_runtime_tool_effect_outbox (
        outbox_id, intent_id, session_id, actor_id, subject_id, topic,
        payload_json, status, attempts, created_at, updated_at
    ) VALUES (
        'effect-outbox-' || intent_id, intent_id, current_context.session_id,
        current_context.actor_id, current_context.active_subject_id,
        'agent.tool-effect.commit',
        jsonb_build_object(
            'intent_id', intent_id,
            'intent', p_intent ->> 'intent',
            'payload', p_intent -> 'payload',
            'payload_sha256', p_intent ->> 'payload_sha256',
            'capability', policy_row.capability,
            'purpose', policy_row.purpose,
            'resource_id', policy_row.action_resource_fence ->> 'action_resource_id'
        ),
        'pending', 0, commit_time, commit_time
    );
    RETURN jsonb_build_object(
        'intent_id', intent_id,
        'idempotency_key', p_intent ->> 'idempotency_key',
        'committed_at', commit_time,
        'fence_fingerprint', p_intent ->> 'fence_fingerprint',
        'authority_revision', current_context.profile_revision,
        'capability', policy_row.capability,
        'purpose', policy_row.purpose,
        'resource_id', policy_row.action_resource_fence ->> 'action_resource_id',
        'intent_sha256', p_intent ->> 'payload_sha256',
        'evidence_refs', p_intent -> 'evidence_refs'
    );
END
$session_runtime_commit_tool_effect$;

CREATE OR REPLACE FUNCTION session_runtime_reconcile_tool_effect(
    p_idempotency_key text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_reconcile_tool_effect$
DECLARE
    existing session_runtime_tool_effect_intents%ROWTYPE;
BEGIN
    IF session_user <> 'memoria_action_executor'
       OR char_length(p_idempotency_key) NOT BETWEEN 1 AND 512 THEN
        RAISE EXCEPTION 'invalid tool effect reconcile request'
            USING ERRCODE = 'SR400';
    END IF;
    SELECT * INTO existing
    FROM session_runtime_tool_effect_intents
    WHERE idempotency_key = p_idempotency_key
    FOR SHARE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('state', 'not_found');
    END IF;
    RETURN jsonb_build_object(
        'state', 'committed',
        'receipt', jsonb_build_object(
            'intent_id', existing.intent_id,
            'idempotency_key', existing.idempotency_key,
            'committed_at', existing.committed_at,
            'fence_fingerprint', existing.fence_fingerprint,
            'authority_revision', existing.authority_revision,
            'capability', existing.capability,
            'purpose', existing.purpose,
            'resource_id', existing.resource_id,
            'intent_sha256', existing.payload_sha256,
            'evidence_refs', existing.evidence_refs
        )
    );
END
$session_runtime_reconcile_tool_effect$;

CREATE OR REPLACE FUNCTION session_runtime_tool_effect_outbox_claim(
    p_worker_id text,
    p_limit integer,
    p_lease_seconds integer
) RETURNS SETOF jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_tool_effect_outbox_claim$
DECLARE
    claimed session_runtime_tool_effect_outbox%ROWTYPE;
    lease_until timestamptz := clock_timestamp()
        + (p_lease_seconds * interval '1 second');
BEGIN
    IF session_user <> 'memoria_session_worker'
       OR char_length(p_worker_id) NOT BETWEEN 1 AND 128
       OR p_limit NOT BETWEEN 1 AND 100
       OR p_lease_seconds NOT BETWEEN 1 AND 3600 THEN
        RAISE EXCEPTION 'invalid tool effect outbox worker request'
            USING ERRCODE = 'SR400';
    END IF;
    FOR claimed IN
        SELECT *
        FROM session_runtime_tool_effect_outbox
        WHERE status = 'pending'
           OR (status = 'processing'
               AND (locked_until IS NULL OR locked_until <= clock_timestamp()))
        ORDER BY created_at, outbox_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    LOOP
        UPDATE session_runtime_tool_effect_outbox
        SET status = 'processing', attempts = claimed.attempts + 1,
            locked_until = lease_until, locked_by = p_worker_id,
            updated_at = clock_timestamp()
        WHERE outbox_id = claimed.outbox_id;
        SELECT * INTO claimed
        FROM session_runtime_tool_effect_outbox
        WHERE outbox_id = claimed.outbox_id;
        RETURN NEXT to_jsonb(claimed);
    END LOOP;
END
$session_runtime_tool_effect_outbox_claim$;

CREATE OR REPLACE FUNCTION session_runtime_tool_effect_outbox_complete(
    p_outbox_id text,
    p_worker_id text,
    p_target_status text,
    p_error_code text DEFAULT NULL
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $session_runtime_tool_effect_outbox_complete$
DECLARE
    completed session_runtime_tool_effect_outbox%ROWTYPE;
    completed_at timestamptz := clock_timestamp();
BEGIN
    IF session_user <> 'memoria_session_worker'
       OR char_length(p_outbox_id) NOT BETWEEN 1 AND 160
       OR char_length(p_worker_id) NOT BETWEEN 1 AND 128
       OR p_target_status NOT IN ('delivered', 'failed', 'dead_lettered')
       OR (p_error_code IS NOT NULL AND char_length(p_error_code) > 128) THEN
        RAISE EXCEPTION 'invalid tool effect outbox completion'
            USING ERRCODE = 'SR400';
    END IF;
    UPDATE session_runtime_tool_effect_outbox
    SET status = p_target_status, locked_until = NULL, locked_by = NULL,
        last_error_code = p_error_code,
        delivered_at = CASE WHEN p_target_status = 'delivered'
            THEN completed_at ELSE NULL END,
        updated_at = completed_at
    WHERE outbox_id = p_outbox_id AND status = 'processing'
      AND locked_by = p_worker_id
    RETURNING * INTO completed;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'tool effect outbox lease is stale'
            USING ERRCODE = 'SR412';
    END IF;
    UPDATE session_runtime_tool_effect_intents
    SET status = CASE
            WHEN p_target_status = 'delivered' THEN 'delivered'
            ELSE 'failed'
        END,
        delivered_at = CASE WHEN p_target_status = 'delivered'
            THEN completed_at ELSE delivered_at END,
        updated_at = completed_at
    WHERE intent_id = completed.intent_id;
    RETURN to_jsonb(completed);
END
$session_runtime_tool_effect_outbox_complete$;

REVOKE ALL ON FUNCTION session_runtime_actor() FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_assert_action_context(
    text, text, text, text, text, integer, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_prepare_initial(jsonb, text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_commit_initial(
    jsonb, integer, jsonb, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_action_current_profile(text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_prepare_rotation(
    text, integer, integer, jsonb, integer, jsonb
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_commit_rotation(
    jsonb, integer, jsonb
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_fail_session(
    text, text, integer, jsonb
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_fence_json_valid(jsonb, text[])
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_fence_text_valid(
    jsonb, integer, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_fence_int_valid(jsonb, integer)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_advance_action_fence(jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_action_receipt_authority(text, jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_action_effect_context(
    text, text, jsonb, jsonb
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_commit_tool_effect(jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_reconcile_tool_effect(text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_tool_effect_outbox_claim(
    text, integer, integer
) FROM PUBLIC;
REVOKE ALL ON FUNCTION session_runtime_tool_effect_outbox_complete(
    text, text, text, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION session_runtime_actor()
    TO memoria_session_api, memoria_session_projector,
       memoria_session_worker, memoria_session_maintenance;
GRANT EXECUTE ON FUNCTION session_runtime_assert_action_context(
    text, text, text, text, text, integer, integer
) TO memoria_action_executor, memoria_action_bridge_owner;
GRANT EXECUTE ON FUNCTION session_runtime_prepare_initial(jsonb, text, text)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_commit_initial(
    jsonb, integer, jsonb, text
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_action_current_profile(text)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_prepare_rotation(
    text, integer, integer, jsonb, integer, jsonb
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_commit_rotation(
    jsonb, integer, jsonb
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_fail_session(
    text, text, integer, jsonb
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_advance_action_fence(jsonb)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_action_receipt_authority(text, jsonb)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_action_effect_context(
    text, text, jsonb, jsonb
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_commit_tool_effect(jsonb)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_reconcile_tool_effect(text)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION session_runtime_tool_effect_outbox_claim(
    text, integer, integer
) TO memoria_session_worker;
GRANT EXECUTE ON FUNCTION session_runtime_tool_effect_outbox_complete(
    text, text, text, text
) TO memoria_session_worker;

RESET ROLE;

-- Identity-owned row lock port.  The action role receives EXECUTE only; the
-- function validates authenticated actor/device and returns one frozen binding
-- snapshot while holding the authoritative binding row lock.
SET ROLE memoria_identity_owner;
SET search_path = public;
CREATE OR REPLACE FUNCTION action_identity_lock_binding(
    p_actor_id text,
    p_device_id text,
    p_expected_binding_version integer,
    p_now timestamptz
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $action_identity_lock_binding$
DECLARE
    binding_row identity_device_bindings%ROWTYPE;
    result jsonb;
BEGIN
    IF NULLIF(current_setting('app.authenticated_actor', true), '')
           IS DISTINCT FROM p_actor_id
       OR NULLIF(current_setting('app.authenticated_device', true), '')
           IS DISTINCT FROM p_device_id THEN
        RAISE EXCEPTION 'authenticated identity context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    SELECT * INTO binding_row
    FROM identity_device_bindings
    WHERE device_id = p_device_id
      AND status = 'active'
      AND valid_from <= p_now
      AND (valid_until IS NULL OR p_now < valid_until)
    ORDER BY binding_version DESC
    LIMIT 1
    FOR UPDATE;
    IF NOT FOUND OR NOT EXISTS (
        SELECT 1 FROM identity_device_binding_roles
        WHERE binding_id = binding_row.binding_id
          AND person_id = p_actor_id
          AND status = 'active'
    ) THEN
        RETURN NULL;
    END IF;
    IF binding_row.binding_version <> p_expected_binding_version THEN
        RAISE EXCEPTION 'binding version is stale' USING ERRCODE = 'SR412';
    END IF;
    PERFORM set_config(
        'app.authenticated_binding', binding_row.binding_id, true
    );
    SELECT jsonb_build_object(
        'binding_id', binding_row.binding_id,
        'device_id', binding_row.device_id,
        'binding_version', binding_row.binding_version,
        'declared_mode', binding_row.declared_mode,
        'family_space_id', binding_row.family_space_id,
        'account_owner_person_id', binding_row.account_owner_person_id,
        'valid_from', binding_row.valid_from,
        'valid_until', binding_row.valid_until,
        'service_profile_version', binding_row.service_profile_version,
        'policy_bundle_version', binding_row.policy_bundle_version,
        'consent_snapshot_id', binding_row.consent_snapshot_id,
        'persona_assignment_id', binding_row.persona_assignment_id,
        'roles', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'person_id', r.person_id,
                'role', r.role,
                'permissions', r.permissions_json
            ) ORDER BY r.person_id, r.role)
            FROM identity_device_binding_roles r
            WHERE r.binding_id = binding_row.binding_id
              AND r.status = 'active'
        ), '[]'::jsonb),
        'subjects', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'person_id', p.person_id,
                'display_name', p.display_name,
                'subject_category', p.subject_category,
                'age_band', p.age_band,
                'revision', 1
            ) ORDER BY p.person_id)
            FROM identity_persons p
            JOIN identity_device_binding_roles r
              ON r.person_id = p.person_id
            WHERE r.binding_id = binding_row.binding_id
              AND r.status = 'active'
              AND p.status = 'active'
        ), '[]'::jsonb),
        'relationships', COALESCE((
            SELECT jsonb_agg(jsonb_build_object(
                'relationship_id', relationship.relationship_id,
                'relation_type', relationship.relation_type,
                'status', relationship.status,
                'source_person_id', relationship.source_person_id,
                'target_person_id', relationship.target_person_id,
                'valid_from', relationship.valid_from,
                'valid_until', relationship.valid_until,
                'updated_at', relationship.updated_at
            ) ORDER BY relationship.relationship_id)
            FROM identity_relationships relationship
            WHERE relationship.status = 'active'
              AND relationship.valid_from <= p_now
              AND (
                  relationship.valid_until IS NULL
                  OR p_now < relationship.valid_until
              )
              AND EXISTS (
                  SELECT 1 FROM identity_device_binding_roles source_role
                  WHERE source_role.binding_id = binding_row.binding_id
                    AND source_role.person_id = relationship.source_person_id
                    AND source_role.status = 'active'
              )
              AND EXISTS (
                  SELECT 1 FROM identity_device_binding_roles target_role
                  WHERE target_role.binding_id = binding_row.binding_id
                    AND target_role.person_id = relationship.target_person_id
                    AND target_role.status = 'active'
              )
        ), '[]'::jsonb)
    ) INTO result;
    RETURN result;
END
$action_identity_lock_binding$;
REVOKE ALL ON FUNCTION action_identity_lock_binding(
    text, text, integer, timestamptz
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION action_identity_lock_binding(
    text, text, integer, timestamptz
) TO memoria_action_executor, memoria_session_owner;

CREATE OR REPLACE FUNCTION action_identity_can_switch_subject(
    p_actor_id text,
    p_device_id text,
    p_binding_version integer,
    p_subject_id text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $action_identity_can_switch_subject$
DECLARE
    selected_binding_id text;
BEGIN
    IF NULLIF(current_setting('app.authenticated_actor', true), '')
           IS DISTINCT FROM p_actor_id
       OR NULLIF(current_setting('app.authenticated_device', true), '')
           IS DISTINCT FROM p_device_id THEN
        RETURN false;
    END IF;
    SELECT binding_id INTO selected_binding_id
    FROM identity_device_bindings
    WHERE device_id = p_device_id
      AND binding_version = p_binding_version
      AND status = 'active'
    FOR SHARE;
    IF selected_binding_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM identity_device_binding_roles
        WHERE binding_id = selected_binding_id
          AND person_id = p_subject_id
          AND status = 'active'
    ) THEN
        RETURN false;
    END IF;
    IF p_actor_id = p_subject_id THEN
        RETURN true;
    END IF;
    IF EXISTS (
        SELECT 1 FROM identity_device_binding_roles
        WHERE binding_id = selected_binding_id
          AND person_id = p_actor_id
          AND role IN ('account_owner', 'device_admin')
          AND status = 'active'
    ) THEN
        RETURN true;
    END IF;
    RETURN EXISTS (
        SELECT 1
        FROM identity_device_binding_roles guardian
        JOIN identity_device_binding_roles primary_subject
          ON primary_subject.binding_id = guardian.binding_id
        WHERE guardian.binding_id = selected_binding_id
          AND guardian.person_id = p_actor_id
          AND guardian.role = 'guardian'
          AND guardian.status = 'active'
          AND primary_subject.person_id = p_subject_id
          AND primary_subject.role = 'primary_subject'
          AND primary_subject.status = 'active'
    );
END
$action_identity_can_switch_subject$;
REVOKE ALL ON FUNCTION action_identity_can_switch_subject(
    text, text, integer, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION action_identity_can_switch_subject(
    text, text, integer, text
) TO memoria_action_executor, memoria_session_owner;
RESET ROLE;

-- Device-owned trust lock port.  The NOLOGIN bridge has no public operation
-- other than this fixed SECURITY DEFINER function.  If Device Fleet has not
-- been installed in this database the function reports authority_unavailable;
-- Session may then issue only an unknown-safe profile.
DO $session_runtime_device_grants$
BEGIN
    IF to_regclass('public.device_fleet_devices') IS NOT NULL THEN
        GRANT SELECT, UPDATE ON TABLE device_fleet_devices
            TO memoria_device_action_bridge_owner;
        DROP POLICY IF EXISTS device_fleet_action_bridge_devices
            ON device_fleet_devices;
        CREATE POLICY device_fleet_action_bridge_devices
            ON device_fleet_devices
            FOR SELECT TO memoria_device_action_bridge_owner
            USING (current_user = 'memoria_device_action_bridge_owner');
        DROP POLICY IF EXISTS device_fleet_action_bridge_devices_lock
            ON device_fleet_devices;
        CREATE POLICY device_fleet_action_bridge_devices_lock
            ON device_fleet_devices
            FOR UPDATE TO memoria_device_action_bridge_owner
            USING (current_user = 'memoria_device_action_bridge_owner')
            WITH CHECK (false);
        IF to_regclass('public.device_fleet_certificates') IS NOT NULL THEN
            GRANT SELECT ON TABLE device_fleet_certificates
                TO memoria_device_action_bridge_owner;
            DROP POLICY IF EXISTS device_fleet_action_bridge_certificates
                ON device_fleet_certificates;
            CREATE POLICY device_fleet_action_bridge_certificates
                ON device_fleet_certificates
                FOR SELECT TO memoria_device_action_bridge_owner
                USING (current_user = 'memoria_device_action_bridge_owner');
        END IF;
        IF to_regclass('public.device_fleet_attestations') IS NOT NULL THEN
            GRANT SELECT ON TABLE device_fleet_attestations
                TO memoria_device_action_bridge_owner;
            DROP POLICY IF EXISTS device_fleet_action_bridge_attestations
                ON device_fleet_attestations;
            CREATE POLICY device_fleet_action_bridge_attestations
                ON device_fleet_attestations
                FOR SELECT TO memoria_device_action_bridge_owner
                USING (current_user = 'memoria_device_action_bridge_owner');
        END IF;
    END IF;
END
$session_runtime_device_grants$;

SET ROLE memoria_device_action_bridge_owner;
SET search_path = public;
CREATE OR REPLACE FUNCTION action_device_lock_trust(
    p_actor_id text,
    p_device_id text,
    p_binding_id text,
    p_binding_version integer,
    p_now timestamptz
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $action_device_lock_trust$
DECLARE
    lifecycle text;
    row_binding_id text;
    row_binding_version bigint;
    certificate_active boolean := false;
    attestation_active boolean := false;
BEGIN
    IF NULLIF(current_setting('app.authenticated_actor', true), '')
           IS DISTINCT FROM p_actor_id
       OR NULLIF(current_setting('app.authenticated_device', true), '')
           IS DISTINCT FROM p_device_id
       OR NULLIF(current_setting('app.authenticated_binding', true), '')
           IS DISTINCT FROM p_binding_id THEN
        RAISE EXCEPTION 'authenticated Device authority context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    IF to_regclass('public.device_fleet_devices') IS NULL THEN
        RETURN jsonb_build_object(
            'available', false,
            'device_trust', 'untrusted'
        );
    END IF;
    EXECUTE
        'SELECT lifecycle_status, binding_id, binding_version '
        'FROM public.device_fleet_devices WHERE device_id = $1 FOR SHARE'
    INTO lifecycle, row_binding_id, row_binding_version
    USING p_device_id;
    IF lifecycle IS NULL THEN
        RETURN jsonb_build_object(
            'available', true,
            'device_trust', 'revoked',
            'reason_code', 'device_not_registered'
        );
    END IF;
    IF row_binding_id IS DISTINCT FROM p_binding_id
       OR row_binding_version IS DISTINCT FROM p_binding_version THEN
        RETURN jsonb_build_object(
            'available', true,
            'device_trust', 'revoked',
            'reason_code', 'device_binding_mismatch'
        );
    END IF;
    IF lifecycle <> 'bound' THEN
        RETURN jsonb_build_object(
            'available', true,
            'device_trust', 'revoked',
            'reason_code', 'device_lifecycle_not_bound'
        );
    END IF;
    IF to_regclass('public.device_fleet_certificates') IS NOT NULL THEN
        EXECUTE
            'SELECT EXISTS ('
            'SELECT 1 FROM public.device_fleet_certificates '
            'WHERE device_id = $1 AND binding_id = $2 '
            'AND binding_version = $3 AND status = ''active'' '
            'AND valid_from <= $4 AND $4 < valid_until)'
        INTO certificate_active
        USING p_device_id, p_binding_id, p_binding_version, p_now;
    END IF;
    IF to_regclass('public.device_fleet_attestations') IS NOT NULL THEN
        EXECUTE
            'SELECT EXISTS ('
            'SELECT 1 FROM public.device_fleet_attestations '
            'WHERE device_id = $1 AND binding_id = $2 '
            'AND binding_version = $3 AND occurred_at <= $4 '
            'AND $4 < expires_at)'
        INTO attestation_active
        USING p_device_id, p_binding_id, p_binding_version, p_now;
    END IF;
    RETURN jsonb_build_object(
        'available', true,
        'device_trust', CASE
            WHEN certificate_active AND attestation_active THEN 'verified'
            ELSE 'untrusted'
        END,
        'reason_code', CASE
            WHEN certificate_active AND attestation_active
                THEN 'device_attestation_current'
            ELSE 'device_attestation_unavailable'
        END
    );
END
$action_device_lock_trust$;
REVOKE ALL ON FUNCTION action_device_lock_trust(
    text, text, text, integer, timestamptz
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION action_device_lock_trust(
    text, text, text, integer, timestamptz
) TO memoria_action_executor;
RESET ROLE;

-- Policy receipt append/lock bridge.  The NOLOGIN owner has the narrow table
-- privileges; the action login can only invoke these validated functions.
GRANT SELECT, INSERT ON TABLE policy_receipts_v2
    TO memoria_action_bridge_owner;
GRANT EXECUTE ON FUNCTION policy_lock_receipt_v2(text)
    TO memoria_action_bridge_owner, memoria_session_owner;
-- The Session owner's SECURITY DEFINER receipt port reads through this narrow
-- role-gated policy; the action login itself never gains table privileges.
GRANT SELECT ON TABLE policy_receipts_v2 TO memoria_session_owner;
DROP POLICY IF EXISTS policy_receipts_session_owner_select
    ON policy_receipts_v2;
CREATE POLICY policy_receipts_session_owner_select ON policy_receipts_v2
    FOR SELECT TO memoria_session_owner
    USING (current_user = 'memoria_session_owner');
DROP POLICY IF EXISTS policy_receipts_action_bridge_select
    ON policy_receipts_v2;
CREATE POLICY policy_receipts_action_bridge_select ON policy_receipts_v2
    FOR SELECT TO memoria_action_bridge_owner
    USING (current_user = 'memoria_action_bridge_owner');
DROP POLICY IF EXISTS policy_receipts_action_bridge_insert
    ON policy_receipts_v2;
CREATE POLICY policy_receipts_action_bridge_insert ON policy_receipts_v2
    FOR INSERT TO memoria_action_bridge_owner
    WITH CHECK (current_user = 'memoria_action_bridge_owner');
SET ROLE memoria_action_bridge_owner;
SET search_path = public;

CREATE OR REPLACE FUNCTION action_policy_insert_receipt(
    p_receipt jsonb
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $action_policy_insert_receipt$
DECLARE
    current_payload jsonb;
    authenticated_subject text := NULLIF(
        current_setting('app.authenticated_subject', true), ''
    );
BEGIN
    IF NULLIF(p_receipt ->> 'subject_id', '')
           IS DISTINCT FROM authenticated_subject THEN
        RAISE EXCEPTION 'policy receipt subject context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    PERFORM session_runtime_assert_action_context(
        p_receipt ->> 'session_id', p_receipt ->> 'runtime_profile_id',
        p_receipt ->> 'actor_id', p_receipt ->> 'device_id',
        p_receipt ->> 'binding_id',
        (p_receipt ->> 'binding_version')::integer,
        (p_receipt ->> 'session_epoch')::integer
    );
    INSERT INTO policy_receipts_v2
    SELECT (jsonb_populate_record(NULL::policy_receipts_v2, p_receipt)).*
    ON CONFLICT (receipt_id) DO NOTHING;
    SELECT to_jsonb(r) INTO current_payload
    FROM policy_receipts_v2 r
    WHERE receipt_id = p_receipt ->> 'receipt_id';
    RETURN current_payload;
END
$action_policy_insert_receipt$;

CREATE OR REPLACE FUNCTION action_policy_lock_receipt(
    p_receipt_id text
) RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $action_policy_lock_receipt$
DECLARE
    result jsonb;
    authenticated_subject text := NULLIF(
        current_setting('app.authenticated_subject', true), ''
    );
BEGIN
    IF NOT policy_lock_receipt_v2(p_receipt_id) THEN
        RETURN NULL;
    END IF;
    SELECT to_jsonb(r) INTO result
    FROM policy_receipts_v2 r
    WHERE receipt_id = p_receipt_id;
    IF result IS NULL THEN
        RETURN NULL;
    END IF;
    IF NULLIF(result ->> 'subject_id', '')
           IS DISTINCT FROM authenticated_subject THEN
        RAISE EXCEPTION 'policy receipt subject context mismatch'
            USING ERRCODE = 'SR403';
    END IF;
    PERFORM session_runtime_assert_action_context(
        result ->> 'session_id', result ->> 'runtime_profile_id',
        result ->> 'actor_id', result ->> 'device_id',
        result ->> 'binding_id',
        (result ->> 'binding_version')::integer,
        (result ->> 'session_epoch')::integer
    );
    RETURN result;
END
$action_policy_lock_receipt$;

REVOKE ALL ON FUNCTION action_policy_insert_receipt(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION action_policy_lock_receipt(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION action_policy_insert_receipt(jsonb)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION action_policy_lock_receipt(text)
    TO memoria_action_executor;
RESET ROLE;

-- Defense in depth: the unified role has no direct table privilege anywhere.
REVOKE ALL ON TABLE policy_receipts_v2 FROM memoria_action_executor;
REVOKE ALL ON TABLE identity_persons FROM memoria_action_executor;
REVOKE ALL ON TABLE identity_device_bindings FROM memoria_action_executor;
REVOKE ALL ON TABLE identity_device_binding_roles FROM memoria_action_executor;

-- Consent exposes one SECURITY DEFINER discovery port when its schema is
-- installed. The executor never receives Consent mutation-lock ports: the
-- opaque discovery proof is the sole cross-domain authority boundary.
DO $session_runtime_consent_ports$
BEGIN
    IF to_regprocedure(
        'public.consent_discover_action_fence('
        'text,text,text,text,text,integer,text,text,timestamptz)'
    ) IS NOT NULL THEN
        GRANT EXECUTE ON FUNCTION consent_discover_action_fence(
            text, text, text, text, text, integer, text, text, timestamptz
        ) TO memoria_action_executor;
    END IF;
END
$session_runtime_consent_ports$;
