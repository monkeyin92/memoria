-- Memoria Consent Authority PostgreSQL schema (production authority, plan §11.7).
--
-- Security boundary, stated precisely:
--  * PostgreSQL RLS separates the API, outbox worker and audit service roles.
--    These are service-role boundaries, not per-request identity boundaries.
--  * Per-request actor/subject authority is decided by ConsentAuthority from
--    authoritative SubjectProof, BindingEvidence and RelationshipEvidence.
--  * The API role cannot create its own actor/subject/binding authorization.
--    A deployment-owned SECURITY DEFINER function is the sole write entry to
--    consent_authorization; absent a mapping, API reads/writes fail closed.
--  * The worker has zero table privileges.  It can only claim pending events
--    and complete processing events through monotonic SECURITY DEFINER APIs.
--
-- All service roles are NOSUPERUSER NOBYPASSRLS.  Production connections must
-- use those roles directly.  JSONB is strictly decoded by postgres_store.py;
-- unknown canonical evidence/snapshot keys are rejected on read.

-- Bootstrap roles first so tables/functions can be owned by the deployment
-- role.  The owner is NOLOGIN and is not a runtime service identity.
DO $consent_roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_consent_owner') THEN
        CREATE ROLE memoria_consent_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_consent') THEN
        CREATE ROLE memoria_consent LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_consent_outbox') THEN
        CREATE ROLE memoria_consent_outbox LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_policy_projector') THEN
        CREATE ROLE memoria_policy_projector LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_consent_audit') THEN
        CREATE ROLE memoria_consent_audit LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_consent_maintenance') THEN
        CREATE ROLE memoria_consent_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    -- The unified action executor is normally created by the Session Runtime
    -- schema.  Bootstrapping the same non-superuser role here keeps the
    -- discovery port grantable in either install order; attributes are
    -- reasserted so both schemas converge on the identical boundary.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_action_executor') THEN
        CREATE ROLE memoria_action_executor LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_action_executor LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$consent_roles$;

ALTER ROLE memoria_consent_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE memoria_consent LOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE memoria_consent_outbox LOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE memoria_policy_projector LOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE memoria_consent_audit LOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE memoria_consent_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE memoria_action_executor LOGIN NOSUPERUSER NOBYPASSRLS;

CREATE TABLE IF NOT EXISTS consent_authorization (
    db_role TEXT NOT NULL CHECK (
        db_role IN (
            'memoria_consent', 'memoria_policy_projector',
            'memoria_consent_maintenance'
        )
    ),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (db_role, actor_id, subject_id, binding_id)
);

CREATE TABLE IF NOT EXISTS consent_offer (
    offer_id TEXT NOT NULL CHECK (char_length(offer_id) BETWEEN 1 AND 128),
    version INTEGER NOT NULL CHECK (version >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'revoked', 'expired', 'superseded')
    ),
    capability TEXT NOT NULL CHECK (char_length(capability) BETWEEN 1 AND 128),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    resource_owner_id TEXT NOT NULL CHECK (
        char_length(resource_owner_id) BETWEEN 1 AND 128
    ),
    purpose TEXT NOT NULL CHECK (
        purpose IN (
            'user_request', 'runtime_profile_issue', 'runtime_sensitive_action',
            'voice_profile', 'voice_clone', 'digital_self', 'legacy', 'payment',
            'raw_audio', 'model_training', 'device_transfer', 'memory_capture',
            'memory_promotion', 'family_shared_memory_proposal',
            'family_shared_memory_approval', 'family_shared_memory_promotion',
            'memory_recall', 'guardian_summary', 'crisis_response'
        )
    ),
    params_json JSONB NOT NULL CHECK (jsonb_typeof(params_json) = 'object'),
    policy_version TEXT NOT NULL CHECK (
        char_length(policy_version) BETWEEN 1 AND 128
    ),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL CHECK (valid_until > valid_from),
    issued_at TIMESTAMPTZ NOT NULL,
    issuer TEXT NOT NULL CHECK (char_length(issuer) BETWEEN 1 AND 128),
    canonical_hash TEXT NOT NULL CHECK (canonical_hash ~ '^[a-f0-9]{64}$'),
    supersedes_offer_id TEXT CHECK (
        supersedes_offer_id IS NULL
        OR char_length(supersedes_offer_id) BETWEEN 1 AND 128
    ),
    PRIMARY KEY (offer_id, version),
    CHECK (
        (version = 1 AND supersedes_offer_id IS NULL)
        OR (version > 1 AND supersedes_offer_id IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_consent_offer_authority
    ON consent_offer (actor_id, subject_id, offer_id, version DESC);

CREATE TABLE IF NOT EXISTS consent_evidence (
    consent_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'revoked', 'expired', 'disputed', 'superseded')
    ),
    evidence_json JSONB NOT NULL CHECK (jsonb_typeof(evidence_json) = 'object'),
    PRIMARY KEY (consent_id, version)
);
CREATE INDEX IF NOT EXISTS idx_consent_evidence_active
    ON consent_evidence (
        actor_id, subject_id, binding_id, binding_version, status
    );

CREATE TABLE IF NOT EXISTS consent_snapshot (
    snapshot_id TEXT PRIMARY KEY CHECK (char_length(snapshot_id) BETWEEN 1 AND 128),
    version INTEGER NOT NULL CHECK (version >= 1),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    snapshot_json JSONB NOT NULL CHECK (jsonb_typeof(snapshot_json) = 'object'),
    UNIQUE (subject_id, binding_id, binding_version, version)
);
CREATE INDEX IF NOT EXISTS idx_consent_snapshot_key
    ON consent_snapshot (actor_id, subject_id, binding_id, binding_version, version);

-- Initial binding acceptance is an immutable audit fact, not a standing
-- capability grant.  It is written before the Identity binding becomes
-- active, so access is bounded by the dedicated consent service role rather
-- than by a consent_authorization row that cannot exist yet.
CREATE TABLE IF NOT EXISTS binding_consent_snapshot (
    snapshot_id TEXT PRIMARY KEY CHECK (
        char_length(snapshot_id) BETWEEN 1 AND 128
    ),
    binding_id TEXT NOT NULL CHECK (
        char_length(binding_id) BETWEEN 1 AND 128
    ),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    actor_person_id TEXT NOT NULL CHECK (
        char_length(actor_person_id) BETWEEN 1 AND 128
    ),
    account_owner_person_id TEXT NOT NULL CHECK (
        char_length(account_owner_person_id) BETWEEN 1 AND 128
    ),
    device_id TEXT NOT NULL CHECK (
        char_length(device_id) BETWEEN 1 AND 128
    ),
    declared_mode TEXT NOT NULL CHECK (
        declared_mode IN (
            'parent_for_child', 'self_use',
            'child_for_parent', 'family_shared'
        )
    ),
    catalog_version TEXT NOT NULL CHECK (
        char_length(catalog_version) BETWEEN 1 AND 128
    ),
    canonical_hash TEXT NOT NULL CHECK (
        canonical_hash ~ '^[a-f0-9]{64}$'
    ),
    snapshot_json JSONB NOT NULL CHECK (
        jsonb_typeof(snapshot_json) = 'object'
    ),
    issued_at TIMESTAMPTZ NOT NULL,
    UNIQUE (binding_id, binding_version)
);
CREATE INDEX IF NOT EXISTS idx_binding_consent_snapshot_actor
    ON binding_consent_snapshot (
        actor_person_id, account_owner_person_id, device_id, issued_at
    );

CREATE TABLE IF NOT EXISTS consent_outbox (
    event_id TEXT PRIMARY KEY CHECK (char_length(event_id) BETWEEN 1 AND 128),
    aggregate_type TEXT NOT NULL CHECK (aggregate_type IN (
        'consent_offer', 'consent_grant', 'consent_revoke', 'consent_dispute',
        'consent_expire', 'consent_snapshot'
    )),
    aggregate_id TEXT NOT NULL CHECK (char_length(aggregate_id) BETWEEN 1 AND 128),
    version INTEGER NOT NULL CHECK (version >= 1),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'array'),
    created_at TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'processing', 'delivered', 'dead_lettered')
    ),
    claimed_by TEXT CHECK (
        claimed_by IS NULL OR char_length(claimed_by) BETWEEN 1 AND 128
    ),
    claimed_at TIMESTAMPTZ,
    CHECK (
        (status = 'pending' AND claimed_by IS NULL AND claimed_at IS NULL)
        OR (status IN ('processing', 'delivered', 'dead_lettered')
            AND claimed_by IS NOT NULL AND claimed_at IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_consent_outbox_worker
    ON consent_outbox (status, created_at);

CREATE TABLE IF NOT EXISTS consent_audit (
    audit_id TEXT PRIMARY KEY CHECK (char_length(audit_id) BETWEEN 1 AND 128),
    event_id TEXT NOT NULL CHECK (char_length(event_id) BETWEEN 1 AND 128),
    action TEXT NOT NULL CHECK (
        action IN ('offer_create', 'grant', 'revoke', 'dispute', 'expire', 'snapshot')
    ),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    consent_id TEXT,
    snapshot_id TEXT,
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'array'),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_consent_audit_subject
    ON consent_audit (actor_id, subject_id, binding_id, created_at);

CREATE TABLE IF NOT EXISTS consent_idempotency (
    idempotency_key TEXT PRIMARY KEY CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    consent_id TEXT,
    version INTEGER,
    snapshot_id TEXT,
    event_id TEXT NOT NULL CHECK (char_length(event_id) BETWEEN 1 AND 128),
    audit_id TEXT,
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    created_at TIMESTAMPTZ NOT NULL
);

-- The only mutable Consent records are narrow current-head pointers.  All
-- offer/evidence/snapshot facts remain append-only.  Revision zero is a locked
-- first-creation sentinel; positive revisions require complete current facts.
CREATE TABLE IF NOT EXISTS consent_offer_head (
    offer_id TEXT PRIMARY KEY CHECK (char_length(offer_id) BETWEEN 1 AND 128),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0),
    current_hash TEXT,
    current_status TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (current_version = 0 AND current_hash IS NULL AND current_status IS NULL)
        OR (current_version > 0 AND current_hash ~ '^[a-f0-9]{64}$'
            AND current_status IN ('active', 'revoked', 'expired', 'superseded'))
    )
);

CREATE TABLE IF NOT EXISTS consent_evidence_head (
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    capability TEXT NOT NULL CHECK (char_length(capability) BETWEEN 1 AND 128),
    purpose TEXT NOT NULL CHECK (char_length(purpose) BETWEEN 1 AND 128),
    current_consent_id TEXT,
    current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    current_hash TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (actor_id, subject_id, binding_id, binding_version, capability, purpose),
    CHECK (
        (current_revision = 0 AND current_consent_id IS NULL AND current_hash IS NULL)
        OR (current_revision > 0
            AND char_length(current_consent_id) BETWEEN 1 AND 128
            AND current_hash ~ '^[a-f0-9]{64}$')
    )
);

CREATE TABLE IF NOT EXISTS consent_snapshot_head (
    subject_id TEXT NOT NULL CHECK (char_length(subject_id) BETWEEN 1 AND 128),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    current_snapshot_id TEXT,
    current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    current_hash TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (subject_id, binding_id, binding_version),
    CHECK (
        (current_revision = 0 AND current_snapshot_id IS NULL AND current_hash IS NULL)
        OR (current_revision > 0
            AND char_length(current_snapshot_id) BETWEEN 1 AND 128
            AND current_hash ~ '^[a-f0-9]{64}$')
    )
);

ALTER TABLE consent_authorization OWNER TO memoria_consent_owner;
ALTER TABLE consent_offer OWNER TO memoria_consent_owner;
ALTER TABLE consent_evidence OWNER TO memoria_consent_owner;
ALTER TABLE consent_snapshot OWNER TO memoria_consent_owner;
ALTER TABLE binding_consent_snapshot OWNER TO memoria_consent_owner;
ALTER TABLE consent_outbox OWNER TO memoria_consent_owner;
ALTER TABLE consent_audit OWNER TO memoria_consent_owner;
ALTER TABLE consent_idempotency OWNER TO memoria_consent_owner;
ALTER TABLE consent_offer_head OWNER TO memoria_consent_owner;
ALTER TABLE consent_evidence_head OWNER TO memoria_consent_owner;
ALTER TABLE consent_snapshot_head OWNER TO memoria_consent_owner;

ALTER TABLE consent_authorization ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_offer ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE binding_consent_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_offer_head ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_evidence_head ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_snapshot_head ENABLE ROW LEVEL SECURITY;
ALTER TABLE consent_authorization FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_offer FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_snapshot FORCE ROW LEVEL SECURITY;
ALTER TABLE binding_consent_snapshot FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_audit FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_idempotency FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_offer_head FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_evidence_head FORCE ROW LEVEL SECURITY;
ALTER TABLE consent_snapshot_head FORCE ROW LEVEL SECURITY;

-- Owner-only authorization provisioning.  Runtime roles never receive EXECUTE.
CREATE OR REPLACE FUNCTION consent_authorize(
    p_db_role TEXT,
    p_actor_id TEXT,
    p_subject_id TEXT,
    p_binding_id TEXT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_authorize$
BEGIN
    IF current_user <> 'memoria_consent_owner' THEN
        RAISE EXCEPTION 'consent_authorize must run as consent owner';
    END IF;
    IF p_db_role NOT IN (
        'memoria_consent', 'memoria_policy_projector',
        'memoria_consent_maintenance'
    ) THEN
        RAISE EXCEPTION 'unknown consent runtime role';
    END IF;
    IF p_actor_id IS NULL OR btrim(p_actor_id) = ''
       OR p_subject_id IS NULL OR btrim(p_subject_id) = ''
       OR p_binding_id IS NULL OR btrim(p_binding_id) = '' THEN
        RAISE EXCEPTION 'actor, subject and binding are required';
    END IF;
    INSERT INTO public.consent_authorization (
        db_role, actor_id, subject_id, binding_id
    ) VALUES (
        p_db_role, p_actor_id, p_subject_id, p_binding_id
    ) ON CONFLICT DO NOTHING;
END
$consent_authorize$;
ALTER FUNCTION consent_authorize(TEXT, TEXT, TEXT, TEXT)
    OWNER TO memoria_consent_owner;

-- Existing-head lock used by the transaction-bound authorizer.  Missing heads
-- remain missing (fail closed); this read seam never creates authority state.
CREATE OR REPLACE FUNCTION consent_lock_authority_head(
    p_request_actor_id TEXT,
    p_evidence_actor_id TEXT,
    p_subject_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER,
    p_capability TEXT,
    p_purpose TEXT
) RETURNS TABLE (
    actor_id TEXT,
    subject_id TEXT,
    binding_id TEXT,
    binding_version INTEGER,
    capability TEXT,
    purpose TEXT,
    current_consent_id TEXT,
    current_revision INTEGER,
    current_hash TEXT,
    updated_at TIMESTAMPTZ,
    evidence_json JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_lock_authority_head$
BEGIN
    RETURN QUERY
    SELECT head.*, evidence.evidence_json
    FROM public.consent_evidence_head AS head
    JOIN public.consent_evidence AS evidence
      ON evidence.actor_id = head.actor_id
     AND evidence.subject_id = head.subject_id
     AND evidence.binding_id = head.binding_id
     AND evidence.binding_version = head.binding_version
     AND evidence.consent_id = head.current_consent_id
     AND evidence.version = head.current_revision
    WHERE head.actor_id = p_evidence_actor_id
      AND head.subject_id = p_subject_id
      AND head.binding_id = p_binding_id
      AND head.binding_version = p_binding_version
      AND head.capability = p_capability
      AND head.purpose = p_purpose
      AND EXISTS (
          SELECT 1 FROM public.consent_authorization AS authz
          WHERE authz.db_role = session_user
            AND authz.actor_id = p_request_actor_id
            AND authz.subject_id = p_subject_id
            AND authz.binding_id = p_binding_id
      )
    FOR UPDATE OF head;
END
$consent_lock_authority_head$;
ALTER FUNCTION consent_lock_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT
)
    OWNER TO memoria_consent_owner;

CREATE OR REPLACE FUNCTION consent_lock_snapshot_head(
    p_request_actor_id TEXT,
    p_subject_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER
) RETURNS TABLE (
    subject_id TEXT,
    binding_id TEXT,
    binding_version INTEGER,
    current_snapshot_id TEXT,
    current_revision INTEGER,
    current_hash TEXT,
    updated_at TIMESTAMPTZ,
    snapshot_json JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_lock_snapshot_head$
BEGIN
    RETURN QUERY
    SELECT head.*, snapshot.snapshot_json
    FROM public.consent_snapshot_head AS head
    JOIN public.consent_snapshot AS snapshot
      ON snapshot.subject_id = head.subject_id
     AND snapshot.binding_id = head.binding_id
     AND snapshot.binding_version = head.binding_version
     AND snapshot.snapshot_id = head.current_snapshot_id
     AND snapshot.version = head.current_revision
    WHERE head.subject_id = p_subject_id
      AND head.binding_id = p_binding_id
      AND head.binding_version = p_binding_version
      AND EXISTS (
          SELECT 1 FROM public.consent_authorization AS authz
          WHERE authz.db_role = session_user
            AND authz.actor_id = p_request_actor_id
            AND authz.subject_id = p_subject_id
            AND authz.binding_id = p_binding_id
      )
    FOR UPDATE OF head;
END
$consent_lock_snapshot_head$;
ALTER FUNCTION consent_lock_snapshot_head(TEXT, TEXT, TEXT, INTEGER)
    OWNER TO memoria_consent_owner;

-- Action-time consent discovery port.  The unified action executor calls
-- this inside its own transaction after the Session action fence has locked
-- the Identity binding and set app.authenticated_*; discovery then locks the
-- current evidence heads (canonical key order) and the current global
-- snapshot head (evidence before snapshot, matching the transaction
-- authorizer's lock order) and returns the minimal canonical state: one
-- JSONB array of current canonical consent evidence plus one canonical
-- snapshot (NULL when no snapshot head exists yet).
--
-- Revoked/expired current evidence is returned, never silently dropped;
-- effectiveness is decided by the policy engine, not by this read port.
-- The caller identity is pinned to session_user so SET ROLE cannot smuggle a
-- different authorization boundary past the check.
CREATE OR REPLACE FUNCTION consent_discover_action_fence(
    p_actor_id TEXT,
    p_subject_id TEXT,
    p_resource_owner_id TEXT,
    p_device_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER,
    p_capability TEXT,
    p_purpose TEXT,
    p_now TIMESTAMPTZ
) RETURNS TABLE (
    consent_json JSONB,
    snapshot_json JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $consent_discover_action_fence$
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
    candidate record;
    head_row consent_evidence_head%ROWTYPE;
    evidence_row consent_evidence%ROWTYPE;
    matched_consents jsonb := '[]'::jsonb;
    current_snapshot_json jsonb := NULL;
    expected_purpose text;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'consent discovery requires the action executor role';
    END IF;
    IF p_actor_id IS NULL OR btrim(p_actor_id) = ''
       OR char_length(p_actor_id) > 128
       OR p_device_id IS NULL OR btrim(p_device_id) = ''
       OR char_length(p_device_id) > 128
       OR p_binding_id IS NULL OR btrim(p_binding_id) = ''
       OR char_length(p_binding_id) > 128
       OR p_binding_version IS NULL OR p_binding_version < 1
       OR p_capability IS NULL OR btrim(p_capability) = ''
       OR char_length(p_capability) > 128
       OR p_purpose IS NULL OR btrim(p_purpose) = ''
       OR char_length(p_purpose) > 256
       OR p_now IS NULL THEN
        RAISE EXCEPTION 'invalid consent discovery fence';
    END IF;
    IF authenticated_actor IS DISTINCT FROM p_actor_id
       OR authenticated_device IS DISTINCT FROM p_device_id THEN
        RAISE EXCEPTION 'authenticated action context mismatch';
    END IF;
    IF authenticated_binding IS NOT NULL
       AND authenticated_binding IS DISTINCT FROM p_binding_id THEN
        RAISE EXCEPTION 'authenticated binding mismatch';
    END IF;
    -- Action discovery accepts only the one semantic runtime purpose for each
    -- generated capability. Profile-issue purposes never enter this port.
    expected_purpose := CASE p_capability
        WHEN 'chat' THEN 'user_request'
        WHEN 'tutor' THEN 'user_request'
        WHEN 'english_practice' THEN 'user_request'
        WHEN 'memory_capture' THEN 'memory_capture'
        WHEN 'memory_promotion' THEN 'memory_promotion'
        WHEN 'family_shared_memory_proposal'
            THEN 'family_shared_memory_proposal'
        WHEN 'family_shared_memory_approval'
            THEN 'family_shared_memory_approval'
        WHEN 'family_shared_memory_promotion'
            THEN 'family_shared_memory_promotion'
        WHEN 'memory_recall_private' THEN 'memory_recall'
        WHEN 'guardian_summary_view' THEN 'guardian_summary'
        WHEN 'voice_profile_create' THEN 'voice_profile'
        WHEN 'voice_clone_use' THEN 'voice_clone'
        WHEN 'digital_self_preview' THEN 'digital_self'
        WHEN 'legacy_grant_create' THEN 'legacy'
        WHEN 'payment' THEN 'payment'
        WHEN 'raw_audio_retention' THEN 'raw_audio'
        WHEN 'model_training_contribution' THEN 'model_training'
        WHEN 'crisis_notification' THEN 'crisis_response'
        WHEN 'device_ownership_transfer' THEN 'device_transfer'
        ELSE NULL
    END;
    IF expected_purpose IS NULL OR p_purpose <> expected_purpose THEN
        RAISE EXCEPTION 'canonical capability/purpose mismatch';
    END IF;
    IF NOT identity_binding_visible(p_actor_id, p_binding_id) THEN
        RAISE EXCEPTION 'actor is not an active binding role';
    END IF;
    IF p_subject_id IS NOT NULL
       AND NOT identity_binding_visible(p_subject_id, p_binding_id) THEN
        RAISE EXCEPTION 'subject is not an active binding role';
    END IF;
    IF p_subject_id IS NOT NULL AND p_actor_id <> p_subject_id THEN
        IF p_resource_owner_id IS DISTINCT FROM p_subject_id
           OR NOT identity_relationship_active(
                p_actor_id, p_subject_id, 'guardian_of', p_now
           ) THEN
            RAISE EXCEPTION 'actor cannot discover subject consent';
        END IF;
    END IF;

    -- Lock every candidate evidence head one key at a time in canonical key
    -- order (deterministic; same order as the transaction authorizer), then
    -- re-read the locked head so the joined evidence is the locked revision.
    FOR candidate IN
        SELECT head.actor_id, head.subject_id, head.binding_id,
               head.binding_version, head.capability, head.purpose
        FROM public.consent_evidence_head AS head
        WHERE head.binding_id = p_binding_id
          AND head.binding_version = p_binding_version
          AND head.subject_id = p_subject_id
          AND head.capability = p_capability
          AND head.purpose = p_purpose
          AND head.actor_id IN (p_actor_id, p_subject_id)
        ORDER BY head.actor_id, head.subject_id, head.binding_id,
                 head.binding_version, head.capability, head.purpose
    LOOP
        SELECT * INTO head_row
        FROM public.consent_evidence_head AS head
        WHERE head.actor_id = candidate.actor_id
          AND head.subject_id = candidate.subject_id
          AND head.binding_id = candidate.binding_id
          AND head.binding_version = candidate.binding_version
          AND head.capability = candidate.capability
          AND head.purpose = candidate.purpose
        FOR UPDATE OF head;
        IF NOT FOUND OR head_row.current_revision = 0 THEN
            CONTINUE;
        END IF;
        SELECT * INTO evidence_row
        FROM public.consent_evidence AS evidence
        WHERE evidence.actor_id = head_row.actor_id
          AND evidence.subject_id = head_row.subject_id
          AND evidence.binding_id = head_row.binding_id
          AND evidence.binding_version = head_row.binding_version
          AND evidence.consent_id = head_row.current_consent_id
          AND evidence.version = head_row.current_revision;
        IF FOUND
           AND evidence_row.evidence_json ->> 'resource_owner_id'
                = p_resource_owner_id
           AND (evidence_row.evidence_json ->> 'device_id' IS NULL
                OR evidence_row.evidence_json ->> 'device_id' = p_device_id) THEN
            matched_consents := matched_consents || evidence_row.evidence_json;
        END IF;
    END LOOP;

    -- Lock the current global snapshot head after the evidence heads,
    -- matching TransactionBoundConsentAuthorizer's lock order.
    SELECT snapshot.snapshot_json INTO current_snapshot_json
    FROM public.consent_snapshot_head AS head
    JOIN public.consent_snapshot AS snapshot
      ON snapshot.subject_id = head.subject_id
     AND snapshot.binding_id = head.binding_id
     AND snapshot.binding_version = head.binding_version
     AND snapshot.snapshot_id = head.current_snapshot_id
     AND snapshot.version = head.current_revision
    WHERE head.subject_id = p_subject_id
      AND head.binding_id = p_binding_id
      AND head.binding_version = p_binding_version
      AND head.current_revision > 0
    FOR UPDATE OF head;

    RETURN QUERY
    SELECT matched_consents, current_snapshot_json;
END
$consent_discover_action_fence$;
ALTER FUNCTION consent_discover_action_fence(
    TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT, TIMESTAMPTZ
)
    OWNER TO memoria_consent_owner;

CREATE OR REPLACE FUNCTION consent_ensure_authority_head(
    p_request_actor_id TEXT,
    p_evidence_actor_id TEXT,
    p_subject_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER,
    p_capability TEXT,
    p_purpose TEXT
) RETURNS SETOF consent_evidence_head
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_ensure_authority_head$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.consent_authorization AS authz
        WHERE authz.db_role = session_user
          AND authz.actor_id = p_request_actor_id
          AND authz.subject_id = p_subject_id
          AND authz.binding_id = p_binding_id
    ) THEN
        RAISE EXCEPTION 'consent authorization mapping required';
    END IF;
    INSERT INTO public.consent_evidence_head (
        actor_id, subject_id, binding_id, binding_version, capability, purpose
    ) VALUES (
        p_evidence_actor_id, p_subject_id, p_binding_id, p_binding_version,
        p_capability, p_purpose
    ) ON CONFLICT DO NOTHING;
    RETURN QUERY
    SELECT head.* FROM public.consent_evidence_head AS head
    WHERE head.subject_id = p_subject_id
      AND head.binding_id = p_binding_id
      AND head.binding_version = p_binding_version
      AND head.capability = p_capability
      AND head.purpose = p_purpose
      AND head.actor_id = p_evidence_actor_id
    FOR UPDATE OF head;
END
$consent_ensure_authority_head$;
ALTER FUNCTION consent_ensure_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT
)
    OWNER TO memoria_consent_owner;

CREATE OR REPLACE FUNCTION consent_advance_authority_head(
    p_request_actor_id TEXT,
    p_evidence_actor_id TEXT,
    p_subject_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER,
    p_capability TEXT,
    p_purpose TEXT,
    p_expected_revision INTEGER,
    p_expected_hash TEXT,
    p_consent_id TEXT,
    p_revision INTEGER,
    p_hash TEXT
) RETURNS SETOF consent_evidence_head
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_advance_authority_head$
BEGIN
    IF p_revision <> p_expected_revision + 1 OR p_hash !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'authority head revision/hash transition is invalid';
    END IF;
    RETURN QUERY
    UPDATE public.consent_evidence_head AS head
    SET current_consent_id = p_consent_id,
        current_revision = p_revision,
        current_hash = p_hash,
        updated_at = CURRENT_TIMESTAMP
    WHERE head.actor_id = p_evidence_actor_id
      AND head.subject_id = p_subject_id
      AND head.binding_id = p_binding_id
      AND head.binding_version = p_binding_version
      AND head.capability = p_capability
      AND head.purpose = p_purpose
      AND head.current_revision = p_expected_revision
      AND head.current_hash IS NOT DISTINCT FROM p_expected_hash
      AND EXISTS (
          SELECT 1 FROM public.consent_authorization AS authz
          WHERE authz.db_role = session_user
            AND authz.actor_id = p_request_actor_id
            AND authz.subject_id = p_subject_id
            AND authz.binding_id = p_binding_id
      )
    RETURNING head.*;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'stale authority head';
    END IF;
END
$consent_advance_authority_head$;
ALTER FUNCTION consent_advance_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT, INTEGER, TEXT, TEXT, INTEGER, TEXT
) OWNER TO memoria_consent_owner;

CREATE OR REPLACE FUNCTION consent_ensure_snapshot_head(
    p_request_actor_id TEXT,
    p_subject_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER
) RETURNS SETOF consent_snapshot_head
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_ensure_snapshot_head$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.consent_authorization AS authz
        WHERE authz.db_role = session_user
          AND authz.actor_id = p_request_actor_id
          AND authz.subject_id = p_subject_id
          AND authz.binding_id = p_binding_id
    ) THEN
        RAISE EXCEPTION 'consent authorization mapping required';
    END IF;
    INSERT INTO public.consent_snapshot_head (
        subject_id, binding_id, binding_version
    ) VALUES (
        p_subject_id, p_binding_id, p_binding_version
    ) ON CONFLICT DO NOTHING;
    RETURN QUERY
    SELECT head.* FROM public.consent_snapshot_head AS head
    WHERE head.subject_id = p_subject_id
      AND head.binding_id = p_binding_id
      AND head.binding_version = p_binding_version
    FOR UPDATE OF head;
END
$consent_ensure_snapshot_head$;
ALTER FUNCTION consent_ensure_snapshot_head(TEXT, TEXT, TEXT, INTEGER)
    OWNER TO memoria_consent_owner;

CREATE OR REPLACE FUNCTION consent_advance_snapshot_head(
    p_request_actor_id TEXT,
    p_subject_id TEXT,
    p_binding_id TEXT,
    p_binding_version INTEGER,
    p_expected_revision INTEGER,
    p_expected_hash TEXT,
    p_snapshot_id TEXT,
    p_revision INTEGER,
    p_hash TEXT
) RETURNS SETOF consent_snapshot_head
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_advance_snapshot_head$
BEGIN
    IF p_revision <> p_expected_revision + 1 OR p_hash !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'snapshot head revision/hash transition is invalid';
    END IF;
    RETURN QUERY
    UPDATE public.consent_snapshot_head AS head
    SET current_snapshot_id = p_snapshot_id,
        current_revision = p_revision,
        current_hash = p_hash,
        updated_at = CURRENT_TIMESTAMP
    WHERE head.subject_id = p_subject_id
      AND head.binding_id = p_binding_id
      AND head.binding_version = p_binding_version
      AND head.current_revision = p_expected_revision
      AND head.current_hash IS NOT DISTINCT FROM p_expected_hash
      AND EXISTS (
          SELECT 1 FROM public.consent_authorization AS authz
          WHERE authz.db_role = session_user
            AND authz.actor_id = p_request_actor_id
            AND authz.subject_id = p_subject_id
            AND authz.binding_id = p_binding_id
      )
    RETURNING head.*;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'stale snapshot head';
    END IF;
END
$consent_advance_snapshot_head$;
ALTER FUNCTION consent_advance_snapshot_head(
    TEXT, TEXT, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, TEXT
) OWNER TO memoria_consent_owner;

CREATE OR REPLACE FUNCTION consent_ensure_offer_head(
    p_offer_id TEXT,
    p_actor_id TEXT,
    p_subject_id TEXT
) RETURNS SETOF consent_offer_head
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_ensure_offer_head$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM public.consent_authorization AS authz
        WHERE authz.db_role = session_user
          AND authz.actor_id = p_actor_id
          AND authz.subject_id = p_subject_id
    ) THEN
        RAISE EXCEPTION 'consent authorization mapping required';
    END IF;
    INSERT INTO public.consent_offer_head (offer_id, actor_id, subject_id)
    VALUES (p_offer_id, p_actor_id, p_subject_id)
    ON CONFLICT DO NOTHING;
    RETURN QUERY
    SELECT head.* FROM public.consent_offer_head AS head
    WHERE head.offer_id = p_offer_id
      AND head.actor_id = p_actor_id
      AND head.subject_id = p_subject_id
    FOR UPDATE OF head;
END
$consent_ensure_offer_head$;
ALTER FUNCTION consent_ensure_offer_head(TEXT, TEXT, TEXT)
    OWNER TO memoria_consent_owner;

CREATE OR REPLACE FUNCTION consent_advance_offer_head(
    p_offer_id TEXT,
    p_expected_version INTEGER,
    p_expected_hash TEXT,
    p_version INTEGER,
    p_hash TEXT,
    p_status TEXT
) RETURNS SETOF consent_offer_head
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_advance_offer_head$
BEGIN
    IF p_version <> p_expected_version + 1
       OR p_hash !~ '^[a-f0-9]{64}$'
       OR p_status NOT IN ('active', 'revoked', 'expired', 'superseded') THEN
        RAISE EXCEPTION 'offer head transition is invalid';
    END IF;
    RETURN QUERY
    UPDATE public.consent_offer_head AS head
    SET current_version = p_version,
        current_hash = p_hash,
        current_status = p_status,
        updated_at = CURRENT_TIMESTAMP
    WHERE head.offer_id = p_offer_id
      AND head.current_version = p_expected_version
      AND head.current_hash IS NOT DISTINCT FROM p_expected_hash
      AND EXISTS (
          SELECT 1 FROM public.consent_authorization AS authz
          WHERE authz.db_role = session_user
            AND authz.actor_id = head.actor_id
            AND authz.subject_id = head.subject_id
      )
    RETURNING head.*;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'stale offer head';
    END IF;
END
$consent_advance_offer_head$;
ALTER FUNCTION consent_advance_offer_head(TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT)
    OWNER TO memoria_consent_owner;

-- Worker claim: the caller has EXECUTE only, never table SELECT/UPDATE.
-- SECURITY DEFINER changes current_user to the owner, so PostgreSQL's caller
-- identity is checked with session_user; production workers log in directly as
-- memoria_consent_outbox (SET ROLE is intentionally not an authorization seam).
CREATE OR REPLACE FUNCTION consent_claim_outbox(
    p_worker TEXT,
    p_limit INTEGER
) RETURNS SETOF consent_outbox
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_claim$
BEGIN
    IF current_user <> 'memoria_consent_owner'
       OR session_user <> 'memoria_consent_outbox' THEN
        RAISE EXCEPTION 'consent outbox worker role required';
    END IF;
    IF p_worker IS NULL OR btrim(p_worker) = '' OR char_length(p_worker) > 128 THEN
        RAISE EXCEPTION 'worker id must be a bounded non-empty string';
    END IF;
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 1000 THEN
        RAISE EXCEPTION 'claim limit must be within 1..1000';
    END IF;

    RETURN QUERY
    WITH candidates AS (
        SELECT event_id
        FROM public.consent_outbox
        WHERE status = 'pending'
        ORDER BY created_at, event_id
        FOR UPDATE SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE public.consent_outbox AS outbox
    SET status = 'processing',
        claimed_by = p_worker,
        claimed_at = CURRENT_TIMESTAMP
    FROM candidates
    WHERE outbox.event_id = candidates.event_id
      AND outbox.status = 'pending'
    RETURNING outbox.*;
END
$consent_claim$;
ALTER FUNCTION consent_claim_outbox(TEXT, INTEGER)
    OWNER TO memoria_consent_owner;

-- Worker completion: only processing -> delivered/dead_lettered is legal and
-- only the status column is updated.  Event content stays immutable.
CREATE OR REPLACE FUNCTION consent_complete_outbox(
    p_event_id TEXT,
    p_target_status TEXT
) RETURNS SETOF consent_outbox
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_complete$
DECLARE
    changed INTEGER;
BEGIN
    IF current_user <> 'memoria_consent_owner'
       OR session_user <> 'memoria_consent_outbox' THEN
        RAISE EXCEPTION 'consent outbox worker role required';
    END IF;
    IF p_target_status NOT IN ('delivered', 'dead_lettered') THEN
        RAISE EXCEPTION 'target status must be delivered or dead_lettered';
    END IF;

    RETURN QUERY
    UPDATE public.consent_outbox AS outbox
    SET status = p_target_status
    WHERE outbox.event_id = p_event_id
      AND outbox.status = 'processing'
    RETURNING outbox.*;
    GET DIAGNOSTICS changed = ROW_COUNT;
    IF changed <> 1 THEN
        RAISE EXCEPTION 'event is missing or not processing';
    END IF;
END
$consent_complete$;
ALTER FUNCTION consent_complete_outbox(TEXT, TEXT)
    OWNER TO memoria_consent_owner;

-- Narrow idempotency replay read.  The API still has no SELECT on audit.
CREATE OR REPLACE FUNCTION consent_read_audit(p_audit_id TEXT)
RETURNS SETOF consent_audit
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_read_audit$
    SELECT audit.*
    FROM public.consent_audit AS audit
    WHERE audit.audit_id = p_audit_id
      AND EXISTS (
          SELECT 1
          FROM public.consent_authorization AS authz
          WHERE authz.db_role = 'memoria_consent'
            AND authz.actor_id = audit.actor_id
            AND authz.subject_id = audit.subject_id
            AND authz.binding_id = audit.binding_id
      );
$consent_read_audit$;
ALTER FUNCTION consent_read_audit(TEXT) OWNER TO memoria_consent_owner;

-- Narrow idempotency replay read.  The API still has no SELECT on outbox.
CREATE OR REPLACE FUNCTION consent_read_outbox(p_event_id TEXT)
RETURNS SETOF consent_outbox
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $consent_read_outbox$
    SELECT outbox.*
    FROM public.consent_outbox AS outbox
    WHERE outbox.event_id = p_event_id
      AND EXISTS (
          SELECT 1
          FROM public.consent_authorization AS authz
          WHERE authz.db_role = 'memoria_consent'
            AND authz.actor_id = outbox.actor_id
            AND authz.subject_id = outbox.subject_id
            AND authz.binding_id = outbox.binding_id
      );
$consent_read_outbox$;
ALTER FUNCTION consent_read_outbox(TEXT) OWNER TO memoria_consent_owner;

-- RLS policies.  API data policies all bind row actor/subject/binding columns
-- to a deployment-provisioned authorization tuple; there is no caller-set GUC.
DROP POLICY IF EXISTS consent_owner_authorization ON consent_authorization;
CREATE POLICY consent_owner_authorization ON consent_authorization
    FOR ALL TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner')
    WITH CHECK (current_user = 'memoria_consent_owner');

DROP POLICY IF EXISTS consent_api_authorization ON consent_authorization;
CREATE POLICY consent_api_authorization ON consent_authorization
    FOR SELECT TO memoria_consent
    USING (
        current_user = 'memoria_consent'
        AND db_role = current_user
    );

DROP POLICY IF EXISTS consent_api_offer_select ON consent_offer;
CREATE POLICY consent_api_offer_select ON consent_offer
    FOR SELECT TO memoria_consent
    USING (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_offer.actor_id
          AND authz.subject_id = consent_offer.subject_id
          AND char_length(authz.binding_id) > 0
    ));
DROP POLICY IF EXISTS consent_api_offer_insert ON consent_offer;
CREATE POLICY consent_api_offer_insert ON consent_offer
    FOR INSERT TO memoria_consent
    WITH CHECK (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_offer.actor_id
          AND authz.subject_id = consent_offer.subject_id
          AND char_length(authz.binding_id) > 0
    ));

DROP POLICY IF EXISTS consent_api_evidence_select ON consent_evidence;
CREATE POLICY consent_api_evidence_select ON consent_evidence
    FOR SELECT TO memoria_consent
    USING (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_evidence.actor_id
          AND authz.subject_id = consent_evidence.subject_id
          AND authz.binding_id = consent_evidence.binding_id
    ));
DROP POLICY IF EXISTS consent_owner_evidence_select ON consent_evidence;
CREATE POLICY consent_owner_evidence_select ON consent_evidence
    FOR SELECT TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner');
DROP POLICY IF EXISTS consent_api_evidence_insert ON consent_evidence;
CREATE POLICY consent_api_evidence_insert ON consent_evidence
    FOR INSERT TO memoria_consent
    WITH CHECK (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_evidence.actor_id
          AND authz.subject_id = consent_evidence.subject_id
          AND authz.binding_id = consent_evidence.binding_id
    ));
DROP POLICY IF EXISTS consent_api_snapshot_select ON consent_snapshot;
CREATE POLICY consent_api_snapshot_select ON consent_snapshot
    FOR SELECT TO memoria_consent
    USING (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_snapshot.actor_id
          AND authz.subject_id = consent_snapshot.subject_id
          AND authz.binding_id = consent_snapshot.binding_id
    ));
DROP POLICY IF EXISTS consent_owner_snapshot_select ON consent_snapshot;
CREATE POLICY consent_owner_snapshot_select ON consent_snapshot
    FOR SELECT TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner');
DROP POLICY IF EXISTS consent_api_snapshot_insert ON consent_snapshot;
CREATE POLICY consent_api_snapshot_insert ON consent_snapshot
    FOR INSERT TO memoria_consent
    WITH CHECK (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_snapshot.actor_id
          AND authz.subject_id = consent_snapshot.subject_id
          AND authz.binding_id = consent_snapshot.binding_id
    ));

DROP POLICY IF EXISTS binding_consent_owner ON binding_consent_snapshot;
CREATE POLICY binding_consent_owner ON binding_consent_snapshot
    FOR ALL TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner')
    WITH CHECK (current_user = 'memoria_consent_owner');
DROP POLICY IF EXISTS binding_consent_api_select ON binding_consent_snapshot;
CREATE POLICY binding_consent_api_select ON binding_consent_snapshot
    FOR SELECT TO memoria_consent
    USING (current_user = 'memoria_consent');
DROP POLICY IF EXISTS binding_consent_api_insert ON binding_consent_snapshot;
CREATE POLICY binding_consent_api_insert ON binding_consent_snapshot
    FOR INSERT TO memoria_consent
    WITH CHECK (current_user = 'memoria_consent');

DROP POLICY IF EXISTS consent_api_idempotency_select ON consent_idempotency;
CREATE POLICY consent_api_idempotency_select ON consent_idempotency
    FOR SELECT TO memoria_consent
    USING (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_idempotency.actor_id
          AND authz.subject_id = consent_idempotency.subject_id
          AND authz.binding_id = consent_idempotency.binding_id
    ));
DROP POLICY IF EXISTS consent_api_idempotency_insert ON consent_idempotency;
CREATE POLICY consent_api_idempotency_insert ON consent_idempotency
    FOR INSERT TO memoria_consent
    WITH CHECK (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_idempotency.actor_id
          AND authz.subject_id = consent_idempotency.subject_id
          AND authz.binding_id = consent_idempotency.binding_id
    ));

DROP POLICY IF EXISTS consent_api_audit_insert ON consent_audit;
CREATE POLICY consent_api_audit_insert ON consent_audit
    FOR INSERT TO memoria_consent
    WITH CHECK (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_audit.actor_id
          AND authz.subject_id = consent_audit.subject_id
          AND authz.binding_id = consent_audit.binding_id
    ));

DROP POLICY IF EXISTS consent_auditor_read ON consent_audit;
CREATE POLICY consent_auditor_read ON consent_audit
    FOR SELECT TO memoria_consent_audit
    USING (current_user = 'memoria_consent_audit');

DROP POLICY IF EXISTS consent_owner_audit ON consent_audit;
CREATE POLICY consent_owner_audit ON consent_audit
    FOR SELECT TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner');

DROP POLICY IF EXISTS consent_api_outbox_insert ON consent_outbox;
CREATE POLICY consent_api_outbox_insert ON consent_outbox
    FOR INSERT TO memoria_consent
    WITH CHECK (EXISTS (
        SELECT 1 FROM consent_authorization AS authz
        WHERE authz.db_role = current_user
          AND authz.actor_id = consent_outbox.actor_id
          AND authz.subject_id = consent_outbox.subject_id
          AND authz.binding_id = consent_outbox.binding_id
    ));

DROP POLICY IF EXISTS consent_owner_outbox ON consent_outbox;
CREATE POLICY consent_owner_outbox ON consent_outbox
    FOR ALL TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner')
    WITH CHECK (current_user = 'memoria_consent_owner');

DROP POLICY IF EXISTS consent_owner_offer_head ON consent_offer_head;
CREATE POLICY consent_owner_offer_head ON consent_offer_head
    FOR ALL TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner')
    WITH CHECK (current_user = 'memoria_consent_owner');
DROP POLICY IF EXISTS consent_owner_evidence_head ON consent_evidence_head;
CREATE POLICY consent_owner_evidence_head ON consent_evidence_head
    FOR ALL TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner')
    WITH CHECK (current_user = 'memoria_consent_owner');
DROP POLICY IF EXISTS consent_owner_snapshot_head ON consent_snapshot_head;
CREATE POLICY consent_owner_snapshot_head ON consent_snapshot_head
    FOR ALL TO memoria_consent_owner
    USING (current_user = 'memoria_consent_owner')
    WITH CHECK (current_user = 'memoria_consent_owner');

-- Exact service-role privilege matrix.  Revoke first so rerunning this schema
-- also removes privileges left by an older deployment.
REVOKE ALL ON consent_authorization FROM memoria_consent;
REVOKE ALL ON consent_offer FROM memoria_consent;
REVOKE ALL ON consent_evidence FROM memoria_consent;
REVOKE ALL ON consent_snapshot FROM memoria_consent;
REVOKE ALL ON binding_consent_snapshot FROM memoria_consent;
REVOKE ALL ON consent_idempotency FROM memoria_consent;
REVOKE ALL ON consent_audit FROM memoria_consent;
REVOKE ALL ON consent_outbox FROM memoria_consent;
REVOKE ALL ON consent_offer_head FROM memoria_consent;
REVOKE ALL ON consent_evidence_head FROM memoria_consent;
REVOKE ALL ON consent_snapshot_head FROM memoria_consent;
GRANT SELECT ON consent_authorization TO memoria_consent;
GRANT SELECT, INSERT ON consent_offer TO memoria_consent;
GRANT SELECT, INSERT ON consent_evidence TO memoria_consent;
GRANT SELECT, INSERT ON consent_snapshot TO memoria_consent;
GRANT SELECT, INSERT ON binding_consent_snapshot TO memoria_consent;
GRANT SELECT, INSERT ON consent_idempotency TO memoria_consent;
GRANT INSERT ON consent_audit TO memoria_consent;
GRANT INSERT ON consent_outbox TO memoria_consent;

REVOKE ALL ON consent_authorization FROM memoria_consent_outbox;
REVOKE ALL ON consent_offer FROM memoria_consent_outbox;
REVOKE ALL ON consent_evidence FROM memoria_consent_outbox;
REVOKE ALL ON consent_snapshot FROM memoria_consent_outbox;
REVOKE ALL ON binding_consent_snapshot FROM memoria_consent_outbox;
REVOKE ALL ON consent_idempotency FROM memoria_consent_outbox;
REVOKE ALL ON consent_audit FROM memoria_consent_outbox;
REVOKE ALL ON consent_outbox FROM memoria_consent_outbox;
REVOKE ALL ON consent_offer_head FROM memoria_consent_outbox;
REVOKE ALL ON consent_evidence_head FROM memoria_consent_outbox;
REVOKE ALL ON consent_snapshot_head FROM memoria_consent_outbox;

REVOKE ALL ON consent_authorization FROM memoria_consent_audit;
REVOKE ALL ON consent_offer FROM memoria_consent_audit;
REVOKE ALL ON consent_evidence FROM memoria_consent_audit;
REVOKE ALL ON consent_snapshot FROM memoria_consent_audit;
REVOKE ALL ON binding_consent_snapshot FROM memoria_consent_audit;
REVOKE ALL ON consent_idempotency FROM memoria_consent_audit;
REVOKE ALL ON consent_outbox FROM memoria_consent_audit;
REVOKE ALL ON consent_audit FROM memoria_consent_audit;
REVOKE ALL ON consent_offer_head FROM memoria_consent_audit;
REVOKE ALL ON consent_evidence_head FROM memoria_consent_audit;
REVOKE ALL ON consent_snapshot_head FROM memoria_consent_audit;
GRANT SELECT ON consent_audit TO memoria_consent_audit;

REVOKE ALL ON consent_authorization FROM memoria_policy_projector;
REVOKE ALL ON consent_offer FROM memoria_policy_projector;
REVOKE ALL ON consent_evidence FROM memoria_policy_projector;
REVOKE ALL ON consent_snapshot FROM memoria_policy_projector;
REVOKE ALL ON binding_consent_snapshot FROM memoria_policy_projector;
REVOKE ALL ON consent_idempotency FROM memoria_policy_projector;
REVOKE ALL ON consent_audit FROM memoria_policy_projector;
REVOKE ALL ON consent_outbox FROM memoria_policy_projector;
REVOKE ALL ON consent_offer_head FROM memoria_policy_projector;
REVOKE ALL ON consent_evidence_head FROM memoria_policy_projector;
REVOKE ALL ON consent_snapshot_head FROM memoria_policy_projector;
REVOKE ALL ON consent_authorization FROM memoria_consent_maintenance;
REVOKE ALL ON consent_offer FROM memoria_consent_maintenance;
REVOKE ALL ON consent_evidence FROM memoria_consent_maintenance;
REVOKE ALL ON consent_snapshot FROM memoria_consent_maintenance;
REVOKE ALL ON binding_consent_snapshot FROM memoria_consent_maintenance;
REVOKE ALL ON consent_idempotency FROM memoria_consent_maintenance;
REVOKE ALL ON consent_audit FROM memoria_consent_maintenance;
REVOKE ALL ON consent_outbox FROM memoria_consent_maintenance;
REVOKE ALL ON consent_offer_head FROM memoria_consent_maintenance;
REVOKE ALL ON consent_evidence_head FROM memoria_consent_maintenance;
REVOKE ALL ON consent_snapshot_head FROM memoria_consent_maintenance;

REVOKE ALL ON FUNCTION consent_authorize(TEXT, TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_authorize(TEXT, TEXT, TEXT, TEXT)
    FROM memoria_consent, memoria_consent_outbox, memoria_consent_audit;
GRANT EXECUTE ON FUNCTION consent_authorize(TEXT, TEXT, TEXT, TEXT)
    TO memoria_consent_owner;

REVOKE ALL ON FUNCTION consent_claim_outbox(TEXT, INTEGER) FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_complete_outbox(TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION consent_claim_outbox(TEXT, INTEGER)
    TO memoria_consent_outbox;
GRANT EXECUTE ON FUNCTION consent_complete_outbox(TEXT, TEXT)
    TO memoria_consent_outbox;

REVOKE ALL ON FUNCTION consent_read_audit(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION consent_read_audit(TEXT) TO memoria_consent;
REVOKE ALL ON FUNCTION consent_read_outbox(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION consent_read_outbox(TEXT) TO memoria_consent;

REVOKE ALL ON FUNCTION consent_lock_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT
)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_lock_snapshot_head(TEXT, TEXT, TEXT, INTEGER)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_ensure_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_advance_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT, INTEGER, TEXT, TEXT, INTEGER, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_ensure_snapshot_head(TEXT, TEXT, TEXT, INTEGER)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_advance_snapshot_head(
    TEXT, TEXT, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, TEXT
) FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_ensure_offer_head(TEXT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON FUNCTION consent_advance_offer_head(
    TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION consent_lock_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT
) TO memoria_consent, memoria_policy_projector, memoria_consent_maintenance;
GRANT EXECUTE ON FUNCTION consent_lock_snapshot_head(TEXT, TEXT, TEXT, INTEGER)
    TO memoria_consent, memoria_policy_projector, memoria_consent_maintenance;
GRANT EXECUTE ON FUNCTION consent_ensure_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT
) TO memoria_consent, memoria_consent_maintenance;
GRANT EXECUTE ON FUNCTION consent_advance_authority_head(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT, INTEGER, TEXT, TEXT, INTEGER, TEXT
) TO memoria_consent, memoria_consent_maintenance;
GRANT EXECUTE ON FUNCTION consent_ensure_snapshot_head(TEXT, TEXT, TEXT, INTEGER)
    TO memoria_consent, memoria_consent_maintenance;
GRANT EXECUTE ON FUNCTION consent_advance_snapshot_head(
    TEXT, TEXT, TEXT, INTEGER, INTEGER, TEXT, TEXT, INTEGER, TEXT
) TO memoria_consent, memoria_consent_maintenance;
GRANT EXECUTE ON FUNCTION consent_ensure_offer_head(TEXT, TEXT, TEXT)
    TO memoria_consent, memoria_consent_maintenance;
GRANT EXECUTE ON FUNCTION consent_advance_offer_head(
    TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT
) TO memoria_consent, memoria_consent_maintenance;

-- Action-time discovery is a read/lock seam for the unified action executor
-- only.  The role is bootstrapped above so the grant is valid in either
-- install order; Session Runtime additionally grants the same EXECUTE when
-- its schema is applied after Consent.
REVOKE ALL ON FUNCTION consent_discover_action_fence(
    TEXT, TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, TEXT, TIMESTAMPTZ
) FROM PUBLIC;
DO $consent_discover_executor_grant$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_action_executor'
    ) THEN
        GRANT EXECUTE ON FUNCTION consent_discover_action_fence(
            text, text, text, text, text, integer, text, text, timestamptz
        ) TO memoria_action_executor;
    END IF;
END
$consent_discover_executor_grant$;

-- The discovery port validates the actor/subject against Identity's
-- binding-role authority.  The function is a pure boolean and grants no
-- table visibility; the grant is conditional because Identity may be
-- installed after Consent.
DO $consent_identity_authority_grant$
BEGIN
    IF to_regprocedure(
        'public.identity_binding_visible(text,text)'
    ) IS NOT NULL THEN
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION %s TO %I',
            'identity_binding_visible(text, text)',
            'memoria_consent_owner'
        );
    END IF;
    IF to_regprocedure(
        'public.identity_relationship_active(text,text,text,timestamptz)'
    ) IS NOT NULL THEN
        EXECUTE format(
            'GRANT EXECUTE ON FUNCTION %s TO %I',
            'identity_relationship_active(text, text, text, timestamptz)',
            'memoria_consent_owner'
        );
    END IF;
END
$consent_identity_authority_grant$;
