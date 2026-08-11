-- Identity DDL owner: a dedicated NOLOGIN role so the SECURITY DEFINER
-- authority ports run as a limited, non-login principal (never as the
-- bootstrap/ops superuser) and function-level authority cannot be abused
-- through a login.  The executing bootstrap/ops role is granted
-- membership so the rest of the schema can SET ROLE and own the objects.
DO $identity_ddl_owner$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_owner') THEN
        CREATE ROLE memoria_identity_owner NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    EXECUTE format('GRANT memoria_identity_owner TO %I', current_user);
END
$identity_ddl_owner$;

DO $identity_roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity') THEN
        CREATE ROLE memoria_identity LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_registration') THEN
        CREATE ROLE memoria_identity_registration LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_outbox') THEN
        CREATE ROLE memoria_identity_outbox LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_migration') THEN
        CREATE ROLE memoria_identity_migration LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$identity_roles$;

-- The DDL owner needs USAGE/CREATE on the target schema before it can
-- create and own the identity objects (production may replace 'public'
-- with a dedicated schema name here).
GRANT USAGE, CREATE ON SCHEMA public TO memoria_identity_owner;

SET ROLE memoria_identity_owner;
SET search_path = public;

CREATE TABLE IF NOT EXISTS identity_persons (
    person_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (
        char_length(display_name) BETWEEN 1 AND 128
    ),
    subject_category TEXT NOT NULL DEFAULT 'unknown'
        CHECK (subject_category IN ('unknown', 'minor', 'adult')),
    age_band TEXT NOT NULL DEFAULT 'unknown'
        CHECK (age_band IN ('unknown', 'under_14', '14_17', 'adult')),
    age_evidence_status TEXT NOT NULL DEFAULT 'unverified'
        CHECK (age_evidence_status IN ('unverified', 'verified', 'disputed')),
    locale TEXT NOT NULL DEFAULT 'zh-CN'
        CHECK (char_length(locale) BETWEEN 2 AND 32),
    timezone TEXT NOT NULL CHECK (char_length(timezone) BETWEEN 1 AND 64),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (subject_category = 'adult' AND age_band = 'adult'
            AND age_evidence_status = 'verified')
        OR (subject_category = 'minor' AND age_band IN ('under_14', '14_17'))
        OR (subject_category = 'unknown'
            AND age_band IN ('unknown', 'adult')
            AND NOT (age_band = 'adult' AND age_evidence_status = 'verified'))
    )
);

CREATE TABLE IF NOT EXISTS identity_relationships (
    relationship_id TEXT PRIMARY KEY,
    source_person_id TEXT NOT NULL REFERENCES identity_persons(person_id),
    target_person_id TEXT NOT NULL REFERENCES identity_persons(person_id),
    relation_type TEXT NOT NULL CHECK (relation_type IN (
        'self', 'parent_of', 'child_of', 'guardian_of', 'ward_of',
        'spouse_of', 'sibling_of', 'caregiver_of', 'emergency_contact_for',
        'delegate_for', 'beneficiary_of', 'co_subject_of', 'family_member_of'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'active', 'suspended', 'revoked', 'expired', 'disputed'
    )),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    established_evidence_id TEXT NOT NULL CHECK (
        char_length(established_evidence_id) BETWEEN 1 AND 128
    ),
    confirmed_by_source_at TIMESTAMPTZ,
    confirmed_by_target_at TIMESTAMPTZ,
    dispute_resolution_acked_by_source_at TIMESTAMPTZ,
    dispute_resolution_acked_by_target_at TIMESTAMPTZ,
    requires_confirmation BOOLEAN NOT NULL,
    can_delegate BOOLEAN NOT NULL,
    delegated_from_relationship_id TEXT REFERENCES identity_relationships(relationship_id),
    delegation_depth INTEGER NOT NULL DEFAULT 0
        CHECK (delegation_depth BETWEEN 0 AND 3),
    permissions_json JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(permissions_json) = 'array'),
    dispute_reason TEXT,
    auto_suspended BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at TIMESTAMPTZ,
    revocation_evidence_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (relation_type = 'self' AND source_person_id = target_person_id)
        OR (relation_type <> 'self' AND source_person_id <> target_person_id)
    ),
    CHECK (valid_until IS NULL OR valid_until > valid_from),
    CHECK (
        (status = 'revoked' AND revoked_at IS NOT NULL
            AND revocation_evidence_id IS NOT NULL)
        OR (status <> 'revoked' AND revoked_at IS NULL
            AND revocation_evidence_id IS NULL)
    ),
    CHECK (
        (dispute_resolution_acked_by_source_at IS NULL
            AND dispute_resolution_acked_by_target_at IS NULL)
        OR status = 'disputed'
    ),
    CHECK (NOT auto_suspended OR status = 'suspended'),
    CHECK (
        (delegated_from_relationship_id IS NULL AND delegation_depth = 0)
        OR (delegated_from_relationship_id IS NOT NULL
            AND delegation_depth BETWEEN 1 AND 3)
    )
);

CREATE INDEX IF NOT EXISTS idx_identity_relationships_source
ON identity_relationships(source_person_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_relationships_target
ON identity_relationships(target_person_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_device_bindings (
    binding_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    declared_mode TEXT NOT NULL CHECK (declared_mode IN (
        'parent_for_child', 'self_use', 'child_for_parent', 'family_shared'
    )),
    family_space_id TEXT,
    account_owner_person_id TEXT NOT NULL REFERENCES identity_persons(person_id),
    binding_version INTEGER NOT NULL CHECK (binding_version > 0),
    status TEXT NOT NULL CHECK (status IN (
        'active', 'superseded', 'revoked', 'expired'
    )),
    reason TEXT NOT NULL CHECK (reason IN (
        'create', 'supersede', 'transfer', 'unbind', 'expire'
    )),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    supersedes_binding_id TEXT REFERENCES identity_device_bindings(binding_id),
    service_profile_version TEXT NOT NULL CHECK (
        char_length(service_profile_version) BETWEEN 1 AND 64
    ),
    policy_bundle_version TEXT NOT NULL CHECK (
        char_length(policy_bundle_version) BETWEEN 1 AND 64
    ),
    consent_snapshot_id TEXT,
    persona_assignment_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (device_id, binding_version),
    CHECK (valid_until IS NULL OR valid_until > valid_from),
    CHECK (supersedes_binding_id IS NULL OR supersedes_binding_id <> binding_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_bindings_single_active
ON identity_device_bindings(device_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_identity_bindings_device
ON identity_device_bindings(device_id, binding_version DESC);

CREATE TABLE IF NOT EXISTS identity_device_binding_roles (
    binding_id TEXT NOT NULL
        REFERENCES identity_device_bindings(binding_id) ON DELETE RESTRICT,
    person_id TEXT NOT NULL REFERENCES identity_persons(person_id),
    role TEXT NOT NULL CHECK (role IN (
        'account_owner', 'device_admin', 'primary_subject', 'guardian',
        'delegate', 'emergency_contact', 'member'
    )),
    status TEXT NOT NULL CHECK (status IN (
        'active', 'superseded', 'revoked', 'expired'
    )),
    permissions_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(permissions_json) = 'array'),
    granted_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    PRIMARY KEY (binding_id, person_id, role),
    CHECK (
        (status = 'active' AND ended_at IS NULL)
        OR (status <> 'active' AND ended_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_identity_binding_roles_person
ON identity_device_binding_roles(person_id, role, status);

CREATE TABLE IF NOT EXISTS identity_audit_events (
    event_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK (char_length(action) BETWEEN 1 AND 64),
    actor_person_id TEXT,
    subject_person_id TEXT,
    person_id TEXT,
    device_id TEXT,
    binding_id TEXT,
    relationship_id TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_identity_audit_device
ON identity_audit_events(device_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_person
ON identity_audit_events(person_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_binding
ON identity_audit_events(binding_id, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 64),
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'processing', 'delivered', 'failed', 'dead_lettered'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_until TIMESTAMPTZ,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR char_length(last_error_code) BETWEEN 1 AND 96
    ),
    created_at TIMESTAMPTZ NOT NULL,
    delivered_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR (status <> 'delivered' AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_identity_outbox_pending
ON identity_outbox(status, created_at);

CREATE TABLE IF NOT EXISTS identity_transfer_intents (
    transfer_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL CHECK (char_length(device_id) BETWEEN 1 AND 128),
    from_account_owner_person_id TEXT NOT NULL
        REFERENCES identity_persons(person_id),
    to_account_owner_person_id TEXT NOT NULL
        REFERENCES identity_persons(person_id),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'accepted', 'cancelled', 'expired', 'conflicted'
    )),
    step_up_evidence_id TEXT NOT NULL CHECK (
        char_length(step_up_evidence_id) BETWEEN 1 AND 128
    ),
    policy_receipt_id TEXT NOT NULL CHECK (
        char_length(policy_receipt_id) BETWEEN 1 AND 128
    ),
    idempotency_key TEXT NOT NULL DEFAULT ''
        CHECK (char_length(idempotency_key) BETWEEN 0 AND 64),
    evidence_hash TEXT NOT NULL DEFAULT ''
        CHECK (char_length(evidence_hash) IN (0, 64)),
    supersedes_binding_id TEXT REFERENCES identity_device_bindings(binding_id),
    resulting_binding_id TEXT REFERENCES identity_device_bindings(binding_id),
    created_by_person_id TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    accepted_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    cancelled_by_person_id TEXT,
    cancel_reason TEXT,
    CHECK (valid_until IS NULL OR valid_until > created_at),
    CHECK (
        (status = 'accepted' AND accepted_at IS NOT NULL
            AND resulting_binding_id IS NOT NULL)
        OR (status <> 'accepted' AND accepted_at IS NULL
            AND resulting_binding_id IS NULL)
    ),
    CHECK (
        (status = 'cancelled' AND cancelled_at IS NOT NULL
            AND cancelled_by_person_id IS NOT NULL)
        OR (status <> 'cancelled' AND cancelled_at IS NULL
            AND cancelled_by_person_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_identity_transfer_pending
ON identity_transfer_intents(device_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_identity_transfer_device
ON identity_transfer_intents(device_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS identity_idempotency_records (
    scope_key TEXT NOT NULL CHECK (
        char_length(scope_key) BETWEEN 1 AND 256
    ),
    idempotency_key TEXT NOT NULL CHECK (
        char_length(idempotency_key) BETWEEN 1 AND 64
    ),
    operation TEXT NOT NULL CHECK (operation IN (
        'transfer.create', 'transfer.accept', 'transfer.cancel'
    )),
    content_hash TEXT NOT NULL CHECK (
        content_hash ~ '^[a-f0-9]{64}$'
    ),
    result_payload_json JSONB NOT NULL
        CHECK (jsonb_typeof(result_payload_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (scope_key, idempotency_key)
);

-- Current function signatures are upgraded in place.  RLS policies and
-- triggers retain dependencies on these objects after the first install, so
-- dropping them would make this forward-only schema impossible to replay.
-- Explicit DROP statements below are reserved for obsolete overloads only.
CREATE OR REPLACE FUNCTION identity_idempotency_visible(
    p_actor text, p_scope text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT p_actor IS NOT NULL AND p_actor <> '' AND (
        split_part(p_scope, ':', 1) = p_actor
        OR (
            split_part(p_scope, ':', 1) = 'transfer'
            AND split_part(p_scope, ':', 3) IN ('accept', 'cancel')
            AND EXISTS (
                SELECT 1 FROM identity_transfer_intents t
                WHERE t.transfer_id = split_part(p_scope, ':', 2)
                  AND (
                    t.from_account_owner_person_id = p_actor
                    OR t.to_account_owner_person_id = p_actor
                  )
            )
        )
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_person_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $identity_person_immutable$
BEGIN
    IF NEW.person_id IS DISTINCT FROM OLD.person_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'identity person identity is immutable';
    END IF;
    RETURN NEW;
END
$identity_person_immutable$;

DROP TRIGGER IF EXISTS identity_person_core_immutable ON identity_persons;
CREATE TRIGGER identity_person_core_immutable
BEFORE UPDATE ON identity_persons
FOR EACH ROW EXECUTE FUNCTION identity_person_core_immutable_guard();

CREATE OR REPLACE FUNCTION identity_relationship_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $identity_relationship_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'status', 'valid_from', 'valid_until', 'confirmed_by_source_at',
        'confirmed_by_target_at', 'dispute_reason',
        'dispute_resolution_acked_by_source_at',
        'dispute_resolution_acked_by_target_at', 'auto_suspended',
        'revoked_at', 'revocation_evidence_id', 'updated_at'
    ]) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[
        'status', 'valid_from', 'valid_until', 'confirmed_by_source_at',
        'confirmed_by_target_at', 'dispute_reason',
        'dispute_resolution_acked_by_source_at',
        'dispute_resolution_acked_by_target_at', 'auto_suspended',
        'revoked_at', 'revocation_evidence_id', 'updated_at'
    ]) THEN
        RAISE EXCEPTION 'identity relationship identity is immutable';
    END IF;
    IF OLD.status = 'revoked' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'revoked relationship is immutable';
    END IF;
    RETURN NEW;
END
$identity_relationship_immutable$;

DROP TRIGGER IF EXISTS identity_relationship_core_immutable
ON identity_relationships;
CREATE TRIGGER identity_relationship_core_immutable
BEFORE UPDATE ON identity_relationships
FOR EACH ROW EXECUTE FUNCTION identity_relationship_core_immutable_guard();

CREATE OR REPLACE FUNCTION identity_binding_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $identity_binding_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['status', 'valid_until'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'valid_until']) THEN
        RAISE EXCEPTION 'identity binding version is immutable';
    END IF;
    RETURN NEW;
END
$identity_binding_immutable$;

DROP TRIGGER IF EXISTS identity_binding_core_immutable ON identity_device_bindings;
CREATE TRIGGER identity_binding_core_immutable
BEFORE UPDATE ON identity_device_bindings
FOR EACH ROW EXECUTE FUNCTION identity_binding_core_immutable_guard();

CREATE OR REPLACE FUNCTION identity_role_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $identity_role_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['status', 'ended_at'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'ended_at']) THEN
        RAISE EXCEPTION 'identity role grant is immutable';
    END IF;
    RETURN NEW;
END
$identity_role_immutable$;

DROP TRIGGER IF EXISTS identity_role_core_immutable ON identity_device_binding_roles;
CREATE TRIGGER identity_role_core_immutable
BEFORE UPDATE ON identity_device_binding_roles
FOR EACH ROW EXECUTE FUNCTION identity_role_core_immutable_guard();

CREATE OR REPLACE FUNCTION identity_transfer_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $identity_transfer_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'status', 'resulting_binding_id', 'updated_at', 'accepted_at',
        'cancelled_at', 'cancelled_by_person_id', 'cancel_reason'
    ]) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[
        'status', 'resulting_binding_id', 'updated_at', 'accepted_at',
        'cancelled_at', 'cancelled_by_person_id', 'cancel_reason'
    ]) THEN
        RAISE EXCEPTION 'identity transfer intent identity is immutable';
    END IF;
    IF OLD.status = 'pending' AND NEW.status = 'pending'
       AND NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
        RAISE EXCEPTION 'identity transfer intent requires a status change';
    END IF;
    RETURN NEW;
END
$identity_transfer_immutable$;

DROP TRIGGER IF EXISTS identity_transfer_core_immutable
ON identity_transfer_intents;
CREATE TRIGGER identity_transfer_core_immutable
BEFORE UPDATE ON identity_transfer_intents
FOR EACH ROW EXECUTE FUNCTION identity_transfer_core_immutable_guard();

CREATE OR REPLACE FUNCTION identity_delete_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $identity_delete$
BEGIN
    IF current_setting('app.identity_account_deletion', true) = '1' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'identity records require the account deletion pipeline';
END
$identity_delete$;

DROP TRIGGER IF EXISTS identity_persons_delete_guard ON identity_persons;
CREATE TRIGGER identity_persons_delete_guard
BEFORE DELETE ON identity_persons
FOR EACH ROW EXECUTE FUNCTION identity_delete_guard();

DROP TRIGGER IF EXISTS identity_relationships_delete_guard ON identity_relationships;
CREATE TRIGGER identity_relationships_delete_guard
BEFORE DELETE ON identity_relationships
FOR EACH ROW EXECUTE FUNCTION identity_delete_guard();

DROP TRIGGER IF EXISTS identity_bindings_delete_guard ON identity_device_bindings;
CREATE TRIGGER identity_bindings_delete_guard
BEFORE DELETE ON identity_device_bindings
FOR EACH ROW EXECUTE FUNCTION identity_delete_guard();

DROP TRIGGER IF EXISTS identity_binding_roles_delete_guard
ON identity_device_binding_roles;
CREATE TRIGGER identity_binding_roles_delete_guard
BEFORE DELETE ON identity_device_binding_roles
FOR EACH ROW EXECUTE FUNCTION identity_delete_guard();

DROP TRIGGER IF EXISTS identity_transfers_delete_guard
ON identity_transfer_intents;
CREATE TRIGGER identity_transfers_delete_guard
BEFORE DELETE ON identity_transfer_intents
FOR EACH ROW EXECUTE FUNCTION identity_delete_guard();

ALTER TABLE identity_persons ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_persons FORCE ROW LEVEL SECURITY;
ALTER TABLE identity_relationships ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_relationships FORCE ROW LEVEL SECURITY;
ALTER TABLE identity_device_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_device_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE identity_device_binding_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_device_binding_roles FORCE ROW LEVEL SECURITY;
ALTER TABLE identity_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE identity_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE identity_transfer_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_transfer_intents FORCE ROW LEVEL SECURITY;
ALTER TABLE identity_idempotency_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE identity_idempotency_records FORCE ROW LEVEL SECURITY;

-- FORCE RLS also applies to the dedicated table owner.  SECURITY DEFINER
-- authority ports therefore use an explicit owner-only policy while still
-- executing with row security enabled.  This is intentionally narrower than
-- an unrestricted USING policy: only the NOLOGIN owner principal can satisfy
-- it.
DO $identity_owner_policy$
DECLARE
    v_table text;
BEGIN
    FOREACH v_table IN ARRAY ARRAY[
        'identity_persons',
        'identity_relationships',
        'identity_device_bindings',
        'identity_device_binding_roles',
        'identity_audit_events',
        'identity_outbox',
        'identity_transfer_intents',
        'identity_idempotency_records'
    ] LOOP
        EXECUTE format(
            'DROP POLICY IF EXISTS identity_owner_full ON %I', v_table
        );
        EXECUTE format(
            'CREATE POLICY identity_owner_full ON %I '
            'AS PERMISSIVE FOR ALL TO memoria_identity_owner '
            'USING (current_user = ''memoria_identity_owner'') '
            'WITH CHECK (current_user = ''memoria_identity_owner'')',
            v_table
        );
    END LOOP;
END
$identity_owner_policy$;


-- Transaction-local context helpers -------------------------------------
CREATE OR REPLACE FUNCTION identity_actor() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.identity_actor', true), '')::text;
$$;

CREATE OR REPLACE FUNCTION identity_scope() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.identity_scope', true), '')::text;
$$;

CREATE OR REPLACE FUNCTION identity_shares_active_binding(
    person_a text, person_b text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT EXISTS (
        SELECT 1
        FROM identity_device_binding_roles ra
        JOIN identity_device_binding_roles rb
          ON rb.binding_id = ra.binding_id
        WHERE ra.person_id = person_a
          AND rb.person_id = person_b
          AND ra.status = 'active'
          AND rb.status = 'active'
    ));
END
$$;

-- Authority ports: these SECURITY DEFINER functions run as the table owner
-- and return booleans only, so RLS policies stay recursion-free and the API
-- role never reads protected rows through them.
CREATE OR REPLACE FUNCTION identity_person_exists(p text) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT EXISTS (SELECT 1 FROM identity_persons WHERE person_id = p));
END
$$;

CREATE OR REPLACE FUNCTION identity_relationship_active(
    p_source text, p_target text, p_relation_type text, p_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT EXISTS (
        SELECT 1 FROM identity_relationships r
        WHERE r.source_person_id = p_source
          AND r.target_person_id = p_target
          AND r.relation_type = p_relation_type
          AND r.status = 'active'
          AND r.valid_from <= p_at
          AND (r.valid_until IS NULL OR r.valid_until > p_at)
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_person_visible(
    p_actor text, p_person_id text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT p_actor IS NOT NULL AND p_actor <> '' AND (
        p_person_id = p_actor
        OR EXISTS (
            SELECT 1 FROM identity_relationships r
            WHERE r.status = 'active'
              AND (
                (r.source_person_id = p_actor
                 AND r.target_person_id = p_person_id)
                OR (r.target_person_id = p_actor
                    AND r.source_person_id = p_person_id)
              )
        )
        OR EXISTS (
            SELECT 1 FROM identity_device_binding_roles ra
            JOIN identity_device_binding_roles rb
              ON rb.binding_id = ra.binding_id
            WHERE ra.person_id = p_actor
              AND rb.person_id = p_person_id
              AND ra.status = 'active'
              AND rb.status = 'active'
        )
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_relationship_visible(
    actor text, source text, target text, delegated_from text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT actor IS NOT NULL AND actor <> '' AND (
        source = actor
        OR target = actor
        OR identity_shares_active_binding(actor, source)
        OR identity_shares_active_binding(actor, target)
        OR (
            delegated_from IS NOT NULL
            AND EXISTS (
                SELECT 1 FROM identity_relationships parent
                WHERE parent.relationship_id = delegated_from
                  AND (
                    parent.source_person_id = actor
                    OR parent.target_person_id = actor
                  )
            )
        )
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_relationship_parent_managed(
    actor text, parent_id text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT EXISTS (
        SELECT 1 FROM identity_relationships parent
        WHERE parent.relationship_id = parent_id
          AND (
            parent.source_person_id = actor
            OR parent.target_person_id = actor
          )
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_binding_visible(
    actor text, binding_id text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT actor IS NOT NULL AND actor <> '' AND EXISTS (
        SELECT 1 FROM identity_device_binding_roles r
        WHERE r.binding_id = identity_binding_visible.binding_id
          AND r.person_id = actor
          AND r.status = 'active'
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_binding_manage_allowed(
    actor text, binding_id text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT EXISTS (
        SELECT 1 FROM identity_device_bindings b
        WHERE b.binding_id = identity_binding_manage_allowed.binding_id
          AND b.account_owner_person_id = actor
        UNION ALL
        SELECT 1 FROM identity_device_binding_roles r
        WHERE r.binding_id = identity_binding_manage_allowed.binding_id
          AND r.person_id = actor
          AND r.status = 'active'
          AND (
            r.role = 'account_owner'
            OR r.permissions_json @> '["binding.manage"]'::jsonb
          )
    ) LIMIT 1);
END
$$;

CREATE OR REPLACE FUNCTION identity_role_visible(
    actor text, binding_id text, person_id text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT actor IS NOT NULL AND actor <> '' AND (
        person_id = actor
        OR EXISTS (
            SELECT 1 FROM identity_device_binding_roles mine
            WHERE mine.binding_id = identity_role_visible.binding_id
              AND mine.person_id = actor
              AND mine.status = 'active'
        )
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_transfer_visible(
    actor text, transfer_id text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT actor IS NOT NULL AND actor <> '' AND EXISTS (
        SELECT 1 FROM identity_transfer_intents t
        WHERE t.transfer_id = identity_transfer_visible.transfer_id
          AND (
            t.from_account_owner_person_id = actor
            OR t.to_account_owner_person_id = actor
          )
    ));
END
$$;

CREATE OR REPLACE FUNCTION identity_audit_visible(
    actor text, actor_person_id text, person_id text,
    subject_person_id text, device_id text, action text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    RETURN (SELECT actor IS NOT NULL AND actor <> '' AND (
        actor_person_id = actor
        OR person_id = actor
        OR subject_person_id = actor
        OR (
            -- Binding-level management events are visible only to the
            -- account owner / binding.manage holders of the device; ordinary
            -- members and primary subjects never read the full audit trail.
            (
                action LIKE 'binding.%'
                OR action LIKE 'transfer.%'
                OR action LIKE 'migration.%'
            )
            AND EXISTS (
                SELECT 1 FROM identity_device_bindings b
                JOIN identity_device_binding_roles r
                  ON r.binding_id = b.binding_id
                WHERE b.device_id = identity_audit_visible.device_id
                  AND r.person_id = actor
                  AND r.status = 'active'
                  AND (
                    b.account_owner_person_id = actor
                    OR r.permissions_json @> '["binding.manage"]'::jsonb
                  )
            )
        )
    ));
END
$$;

-- Authoritative write ports -------------------------------------------------
-- The GUC actor/scope context is NOT a privilege: a raw SQL client holding
-- the API DSN can set any transaction-local setting, so the API role is never
-- granted direct INSERT/UPDATE on persons, audit, outbox or idempotency
-- records.  All such writes go through SECURITY DEFINER ports owned by the
-- dedicated NOLOGIN DDL owner; person creation additionally requires the
-- dedicated memoria_identity_registration LOGIN role (EXECUTE only, no table
-- privileges), so "registration" can never be forged through a GUC.

DROP FUNCTION IF EXISTS identity_register_person(
    text, text, text, text, text, text, text, text, timestamptz, timestamptz,
    text, jsonb
);
DROP FUNCTION IF EXISTS identity_register_person(
    text, text, text, text, text, text, text, text, timestamptz, timestamptz,
    text, text, text, text, jsonb, timestamptz,
    text, text, text, jsonb, timestamptz
);
-- Removed generic write ports (drops kept so existing deployments converge):
-- audit/outbox rows are synthesized by the mutation triggers and person
-- updates are RLS/action-level; no generic SECURITY DEFINER write path may
-- survive for the API role.
DROP FUNCTION IF EXISTS identity_write_audit(
    text, text, text, text, text, text, text, text, jsonb, timestamptz
);
DROP FUNCTION IF EXISTS identity_enqueue_outbox(text, text, text, jsonb, timestamptz);
DROP FUNCTION IF EXISTS identity_update_person(
    text, text, text, text, text, text, text, text, timestamptz, text
);
DROP FUNCTION IF EXISTS identity_person_registration_snapshot(text);
CREATE OR REPLACE FUNCTION identity_register_person(
    p_person_id text,
    p_display_name text,
    p_subject_category text,
    p_age_band text,
    p_age_evidence_status text,
    p_locale text,
    p_timezone text,
    p_status text,
    p_created_at timestamptz,
    p_updated_at timestamptz,
    p_audit_actor_person_id text,
    p_payload jsonb,
    p_evidence_id text
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_row jsonb;
BEGIN
    IF p_person_id IS NULL OR char_length(p_person_id) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'identity_register_person: invalid person_id';
    END IF;
    IF p_display_name IS NULL OR char_length(p_display_name) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'identity_register_person: invalid display_name';
    END IF;
    IF p_locale IS NULL OR char_length(p_locale) NOT BETWEEN 2 AND 32 THEN
        RAISE EXCEPTION 'identity_register_person: invalid locale';
    END IF;
    IF p_timezone IS NULL OR char_length(p_timezone) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'identity_register_person: invalid timezone';
    END IF;
    IF p_subject_category NOT IN ('unknown', 'minor', 'adult') THEN
        RAISE EXCEPTION 'identity_register_person: invalid subject_category';
    END IF;
    IF p_age_band NOT IN ('unknown', 'under_14', '14_17', 'adult') THEN
        RAISE EXCEPTION 'identity_register_person: invalid age_band';
    END IF;
    IF p_age_evidence_status NOT IN ('unverified', 'verified', 'disputed') THEN
        RAISE EXCEPTION 'identity_register_person: invalid age_evidence_status';
    END IF;
    IF p_status IS DISTINCT FROM 'active' THEN
        RAISE EXCEPTION 'identity_register_person: only active registration is legal';
    END IF;
    -- A verified adult cannot be registered without an authoritative
    -- evidence id: self/API callers can never claim verified adult status.
    IF p_subject_category = 'adult' AND p_age_evidence_status = 'verified'
       AND (p_evidence_id IS NULL OR char_length(p_evidence_id) NOT BETWEEN 1 AND 128) THEN
        RAISE EXCEPTION
            'identity_register_person: verified adult registration requires an evidence id';
    END IF;
    IF p_created_at IS NULL OR p_updated_at IS NULL OR p_updated_at < p_created_at THEN
        RAISE EXCEPTION 'identity_register_person: invalid registration timestamps';
    END IF;
    -- Registration is person + audit + outbox atomically: the port always
    -- writes all three (no NULL audit/outbox path) with fixed action/topic
    -- and event ids derived from the person id, and the caller-supplied
    -- payload must be the canonical person snapshot (every field equals the
    -- person row).  Any violation aborts the whole transaction.
    IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object' THEN
        RAISE EXCEPTION 'identity_register_person: registration payload required';
    END IF;
    IF p_payload->>'person_id' IS DISTINCT FROM p_person_id
       OR p_payload->>'display_name' IS DISTINCT FROM p_display_name
       OR p_payload->>'subject_category' IS DISTINCT FROM p_subject_category
       OR p_payload->>'age_band' IS DISTINCT FROM p_age_band
       OR p_payload->>'age_evidence_status' IS DISTINCT FROM p_age_evidence_status
       OR p_payload->>'locale' IS DISTINCT FROM p_locale
       OR p_payload->>'timezone' IS DISTINCT FROM p_timezone
       OR p_payload->>'status' IS DISTINCT FROM p_status
       OR (p_payload->>'created_at')::timestamptz IS DISTINCT FROM p_created_at
       OR (p_payload->>'updated_at')::timestamptz IS DISTINCT FROM p_updated_at THEN
        RAISE EXCEPTION 'identity_register_person: payload does not match person';
    END IF;

    -- Atomic compare-or-insert: the registration role cannot read arbitrary
    -- persons through a standalone snapshot port.  An exact canonical replay
    -- returns the persisted DB row; any drift raises a conflict; the caller
    -- must prove the full canonical content to obtain any row.
    INSERT INTO identity_persons (
        person_id, display_name, subject_category, age_band,
        age_evidence_status, locale, timezone, status,
        created_at, updated_at
    ) VALUES (
        p_person_id, p_display_name, p_subject_category, p_age_band,
        p_age_evidence_status, p_locale, p_timezone, p_status,
        p_created_at, p_updated_at
    )
    ON CONFLICT (person_id) DO NOTHING;
    IF FOUND THEN
        -- First write of this person: persist the registration trail in the
        -- same transaction with fixed action/topic and derived event ids.
        INSERT INTO identity_audit_events (
            event_id, action, actor_person_id, subject_person_id,
            person_id, device_id, binding_id, relationship_id,
            payload_json, created_at
        ) VALUES (
            'person.register:' || p_person_id, 'person.register',
            p_audit_actor_person_id, p_person_id, p_person_id, NULL, NULL, NULL,
            CASE WHEN p_evidence_id IS NOT NULL
                 THEN p_payload || jsonb_build_object(
                     'age_evidence_id', p_evidence_id
                 )
                 ELSE p_payload
            END,
            p_created_at
        );
        INSERT INTO identity_outbox (
            outbox_id, event_id, topic, payload_json, status,
            attempts, locked_until, last_error_code, created_at,
            delivered_at, updated_at
        ) VALUES (
            'outbox.person.registered:' || p_person_id,
            'person.registered:' || p_person_id,
            'identity.person.registered', p_payload, 'pending', 0, NULL, NULL,
            p_created_at, NULL, p_created_at
        );
    END IF;
    SELECT to_jsonb(t) INTO v_row
    FROM identity_persons t
    WHERE t.person_id = p_person_id
    FOR UPDATE;
    IF v_row IS NULL THEN
        RAISE EXCEPTION 'identity_register_person: person did not persist';
    END IF;
    IF v_row->>'display_name' IS DISTINCT FROM p_display_name
       OR v_row->>'subject_category' IS DISTINCT FROM p_subject_category
       OR v_row->>'age_band' IS DISTINCT FROM p_age_band
       OR v_row->>'age_evidence_status' IS DISTINCT FROM p_age_evidence_status
       OR v_row->>'locale' IS DISTINCT FROM p_locale
       OR v_row->>'timezone' IS DISTINCT FROM p_timezone
       OR v_row->>'status' IS DISTINCT FROM p_status THEN
        RAISE EXCEPTION
            'identity_register_person: person already registered with '
            'different authoritative fields'
            USING ERRCODE = 'II001';
    END IF;
    RETURN v_row;
END
$$;

CREATE OR REPLACE FUNCTION identity_write_idempotency(
    p_scope_key text,
    p_idempotency_key text,
    p_operation text,
    p_content_hash text,
    p_result_payload jsonb,
    p_created_at timestamptz
) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_actor text;
BEGIN
    -- The GUC actor is the transaction-local context, never a caller-chosen
    -- credential: the scope key must be owned by the actor (either the
    -- actor-prefixed command scope or a transfer scope whose from/to owner
    -- is the actor), so raw SQL clients cannot claim or poison other
    -- subjects' idempotency keys.
    v_actor := NULLIF(current_setting('app.identity_actor', true), '');
    IF v_actor IS NULL OR v_actor = '' THEN
        RAISE EXCEPTION 'identity_write_idempotency: authenticated actor required';
    END IF;
    IF p_scope_key IS NULL OR char_length(p_scope_key) NOT BETWEEN 1 AND 256
       OR p_idempotency_key IS NULL OR char_length(p_idempotency_key) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'identity_write_idempotency: invalid key';
    END IF;
    IF p_operation NOT IN ('transfer.create', 'transfer.accept', 'transfer.cancel') THEN
        RAISE EXCEPTION 'identity_write_idempotency: invalid operation';
    END IF;
    IF p_content_hash IS NULL OR p_content_hash !~ '^[a-f0-9]{64}$' THEN
        RAISE EXCEPTION 'identity_write_idempotency: invalid content_hash';
    END IF;
    IF NOT identity_idempotency_visible(v_actor, p_scope_key) THEN
        RAISE EXCEPTION 'identity_write_idempotency: scope not owned by actor';
    END IF;
    INSERT INTO identity_idempotency_records (
        scope_key, idempotency_key, operation, content_hash,
        result_payload_json, created_at
    ) VALUES (
        p_scope_key, p_idempotency_key, p_operation, p_content_hash,
        COALESCE(p_result_payload, '{}'::jsonb), p_created_at
    )
    ON CONFLICT (scope_key, idempotency_key) DO NOTHING;
    IF FOUND THEN
        RETURN TRUE;
    END IF;
    RETURN FALSE;
END
$$;

CREATE OR REPLACE FUNCTION identity_patch_idempotency_result(
    p_scope_key text,
    p_idempotency_key text,
    p_result_payload jsonb
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_actor text;
BEGIN
    v_actor := NULLIF(current_setting('app.identity_actor', true), '');
    IF v_actor IS NULL OR v_actor = '' THEN
        RAISE EXCEPTION 'identity_patch_idempotency_result: authenticated actor required';
    END IF;
    IF NOT identity_idempotency_visible(v_actor, p_scope_key) THEN
        RAISE EXCEPTION 'identity_patch_idempotency_result: scope not owned by actor';
    END IF;
    UPDATE identity_idempotency_records
    SET result_payload_json = COALESCE(p_result_payload, '{}'::jsonb)
    WHERE scope_key = p_scope_key AND idempotency_key = p_idempotency_key;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'identity_patch_idempotency_result: record not found';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION identity_self_update_profile(
    p_person_id text,
    p_display_name text,
    p_locale text,
    p_timezone text,
    p_updated_at timestamptz,
    p_actor_person_id text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
BEGIN
    -- Self-profile port: only display_name/locale/timezone may change and
    -- only by the person themselves.  Age/status fields are never writable
    -- here (raw UPDATE is revoked from the API role entirely).
    IF p_actor_person_id IS NULL OR p_actor_person_id <> p_person_id THEN
        RAISE EXCEPTION
            'identity_self_update_profile: actor must be the person';
    END IF;
    IF p_actor_person_id <> NULLIF(current_setting('app.identity_actor', true), '') THEN
        RAISE EXCEPTION 'identity_self_update_profile: actor context mismatch';
    END IF;
    IF p_display_name IS NULL OR char_length(p_display_name) NOT BETWEEN 1 AND 128
       OR p_locale IS NULL OR char_length(p_locale) NOT BETWEEN 2 AND 32
       OR p_timezone IS NULL OR char_length(p_timezone) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'identity_self_update_profile: invalid profile fields';
    END IF;
    UPDATE identity_persons
    SET display_name = p_display_name,
        locale = p_locale,
        timezone = p_timezone,
        updated_at = p_updated_at
    WHERE person_id = p_person_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'identity_self_update_profile: person not found';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION identity_declare_age_evidence(
    p_person_id text,
    p_age_band text,
    p_updated_at timestamptz,
    p_actor_person_id text
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_old_category text;
    v_new_category text;
    v_new_evidence text;
BEGIN
    -- Age DECLARATION port: self or an active guardian may declare a minor/
    -- unknown band.  It can never claim adult or verified evidence; a
    -- contradiction against a verified adult downgrades into disputed
    -- (fail closed).  Authoritative adult verification is a separate port
    -- executable only by the registration authority.
    IF p_actor_person_id IS NULL OR char_length(p_actor_person_id) = 0 THEN
        RAISE EXCEPTION 'identity_declare_age_evidence: authenticated actor required';
    END IF;
    IF p_actor_person_id <> NULLIF(current_setting('app.identity_actor', true), '') THEN
        RAISE EXCEPTION 'identity_declare_age_evidence: actor context mismatch';
    END IF;
    IF p_age_band NOT IN ('unknown', 'under_14', '14_17') THEN
        RAISE EXCEPTION
            'identity_declare_age_evidence: declarations cannot claim adult';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM identity_persons WHERE person_id = p_person_id
    ) THEN
        RAISE EXCEPTION 'identity_declare_age_evidence: person not found';
    END IF;
    IF p_actor_person_id <> p_person_id AND NOT EXISTS (
        SELECT 1 FROM identity_relationships r
        WHERE r.source_person_id = p_actor_person_id
          AND r.target_person_id = p_person_id
          AND r.relation_type = 'guardian_of'
          AND r.status = 'active'
          AND r.valid_from <= p_updated_at
          AND (r.valid_until IS NULL OR r.valid_until > p_updated_at)
    ) THEN
        RAISE EXCEPTION
            'identity_declare_age_evidence: no active guardian relationship';
    END IF;
    SELECT subject_category INTO v_old_category
    FROM identity_persons WHERE person_id = p_person_id;
    IF v_old_category = 'adult' AND p_age_band <> 'adult' THEN
        -- Contradictory declaration against a verified adult: fail closed
        -- into unknown/disputed.
        v_new_category := 'unknown';
        v_new_evidence := 'disputed';
        p_age_band := 'unknown';
    ELSE
        IF p_age_band IN ('under_14', '14_17') THEN
            v_new_category := 'minor';
        ELSE
            v_new_category := 'unknown';
        END IF;
        v_new_evidence := 'unverified';
    END IF;
    UPDATE identity_persons
    SET subject_category = v_new_category,
        age_band = p_age_band,
        age_evidence_status = v_new_evidence,
        updated_at = p_updated_at
    WHERE person_id = p_person_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'identity_declare_age_evidence: person not found';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION identity_verify_age_evidence(
    p_person_id text,
    p_age_band text,
    p_age_evidence_status text,
    p_evidence_id text,
    p_verifier_person_id text,
    p_updated_at timestamptz
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_event_id text;
BEGIN
    -- Authoritative age verification: executable only by the dedicated
    -- registration role (never by the API role).  Requires a non-empty
    -- evidence id and a distinct verifier who is already a verified adult;
    -- only verified adult outcomes are accepted.  The audit/outbox trail is
    -- synthesized here with a fixed action and the evidence id.
    IF p_age_band IS DISTINCT FROM 'adult'
       OR p_age_evidence_status IS DISTINCT FROM 'verified' THEN
        RAISE EXCEPTION
            'identity_verify_age_evidence: only verified adult outcomes';
    END IF;
    IF p_evidence_id IS NULL OR char_length(p_evidence_id) NOT BETWEEN 1 AND 128 THEN
        RAISE EXCEPTION 'identity_verify_age_evidence: evidence id required';
    END IF;
    IF p_verifier_person_id IS NULL OR char_length(p_verifier_person_id) = 0
       OR p_verifier_person_id = p_person_id THEN
        RAISE EXCEPTION
            'identity_verify_age_evidence: distinct verifier required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM identity_persons WHERE person_id = p_person_id
    ) THEN
        RAISE EXCEPTION 'identity_verify_age_evidence: person not found';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM identity_persons
        WHERE person_id = p_verifier_person_id
          AND subject_category = 'adult'
          AND age_evidence_status = 'verified'
    ) THEN
        RAISE EXCEPTION
            'identity_verify_age_evidence: verifier must be a verified adult';
    END IF;
    -- The person-update trigger synthesizes declaration/profile audits; the
    -- verification audit carries the evidence id and is written here, so the
    -- trigger is suppressed for this statement.
    PERFORM set_config('app.identity_suppress_person_audit', '1', true);
    UPDATE identity_persons
    SET subject_category = 'adult',
        age_band = 'adult',
        age_evidence_status = 'verified',
        updated_at = p_updated_at
    WHERE person_id = p_person_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'identity_verify_age_evidence: person not found';
    END IF;
    v_event_id := 'evt:age-verify:' || gen_random_uuid()::text;
    INSERT INTO identity_audit_events (
        event_id, action, actor_person_id, subject_person_id,
        person_id, device_id, binding_id, relationship_id,
        payload_json, created_at
    ) VALUES (
        v_event_id, 'person.age_verification', p_verifier_person_id,
        p_person_id, p_person_id, NULL, NULL, NULL,
        jsonb_build_object(
            'person_id', p_person_id,
            'evidence_id', p_evidence_id,
            'verifier_person_id', p_verifier_person_id,
            'age_band', 'adult',
            'age_evidence_status', 'verified'
        ),
        p_updated_at
    );
    INSERT INTO identity_outbox (
        outbox_id, event_id, topic, payload_json, status,
        attempts, locked_until, last_error_code, created_at,
        delivered_at, updated_at
    ) VALUES (
        'outbox:' || v_event_id, v_event_id, 'identity.person.age_verified',
        jsonb_build_object(
            'person_id', p_person_id,
            'evidence_id', p_evidence_id,
            'verifier_person_id', p_verifier_person_id
        ),
        'pending', 0, NULL, NULL, p_updated_at, NULL, p_updated_at
    );
END
$$;

-- Business mutation events are synthesized inside the mutation transaction
-- by SECURITY DEFINER triggers with fixed action/topic and authoritative
-- ids: the API role has no direct audit/outbox write path at all (no table
-- grant, no generic port), so a forged GUC cannot fabricate audit or inject
-- outbox events.  The audit and outbox rows of one business event share the
-- same event_id.

CREATE OR REPLACE FUNCTION identity_audit_relationship_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_action text;
    v_topic text;
    v_event_id text;
    v_actor text;
BEGIN
    v_actor := NULLIF(current_setting('app.identity_actor', true), '');
    v_event_id := 'evt:relationship:' || gen_random_uuid()::text;
    IF TG_OP = 'INSERT' THEN
        v_action := 'relationship.proposed';
        v_topic := NULL;
    ELSIF NEW.status = 'revoked' THEN
        v_action := 'relationship.revoked';
        v_topic := NULL;
    ELSIF NEW.status = 'suspended' THEN
        v_action := 'relationship.suspended';
        v_topic := NULL;
    ELSIF NEW.status = 'disputed' THEN
        v_action := 'relationship.disputed';
        v_topic := NULL;
    ELSIF NEW.status = 'expired' THEN
        v_action := 'relationship.expired';
        v_topic := NULL;
    ELSIF NEW.status = 'active' THEN
        IF OLD.status = 'proposed' THEN
            v_action := 'relationship.confirmed';
            v_topic := 'identity.relationship.confirmed';
        ELSIF OLD.status = 'suspended' THEN
            v_action := 'relationship.resumed';
            v_topic := NULL;
        ELSIF OLD.status = 'disputed' THEN
            v_action := 'relationship.resolved';
            v_topic := NULL;
        ELSE
            v_action := 'relationship.active';
            v_topic := NULL;
        END IF;
    ELSE
        v_action := 'relationship.' || NEW.status;
        v_topic := NULL;
    END IF;
    INSERT INTO identity_audit_events (
        event_id, action, actor_person_id, subject_person_id,
        person_id, device_id, binding_id, relationship_id,
        payload_json, created_at
    ) VALUES (
        v_event_id, v_action, v_actor, NEW.target_person_id,
        NEW.source_person_id, NULL, NULL, NEW.relationship_id,
        to_jsonb(NEW), NEW.updated_at
    );
    IF v_topic IS NOT NULL THEN
        INSERT INTO identity_outbox (
            outbox_id, event_id, topic, payload_json, status,
            attempts, locked_until, last_error_code, created_at,
            delivered_at, updated_at
        ) VALUES (
            'outbox:' || v_event_id, v_event_id, v_topic, to_jsonb(NEW),
            'pending', 0, NULL, NULL, NEW.updated_at, NULL, NEW.updated_at
        );
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION identity_audit_binding_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_action text;
    v_topic text;
    v_event_id text;
    v_actor text;
BEGIN
    -- identity_device_bindings has no updated_at column; created_at is the
    -- authoritative event timestamp for both inserts and transitions.
    v_actor := NULLIF(current_setting('app.identity_actor', true), '');
    v_event_id := 'evt:binding:' || gen_random_uuid()::text;
    IF TG_OP = 'INSERT' THEN
        v_action := 'binding.create';
        v_topic := 'identity.binding.created';
    ELSIF NEW.status = 'superseded' THEN
        v_action := 'binding.supersede';
        v_topic := 'identity.binding.superseded';
    ELSIF NEW.status = 'revoked' THEN
        v_action := 'binding.revoke';
        v_topic := 'identity.binding.revoked';
    ELSIF NEW.status = 'expired' THEN
        v_action := 'binding.expire';
        v_topic := 'identity.binding.expired';
    ELSE
        v_action := 'binding.' || NEW.status;
        v_topic := NULL;
    END IF;
    INSERT INTO identity_audit_events (
        event_id, action, actor_person_id, subject_person_id,
        person_id, device_id, binding_id, relationship_id,
        payload_json, created_at
    ) VALUES (
        v_event_id, v_action, v_actor, NEW.account_owner_person_id,
        NEW.account_owner_person_id, NEW.device_id, NEW.binding_id, NULL,
        to_jsonb(NEW), NEW.created_at
    );
    IF v_topic IS NOT NULL THEN
        INSERT INTO identity_outbox (
            outbox_id, event_id, topic, payload_json, status,
            attempts, locked_until, last_error_code, created_at,
            delivered_at, updated_at
        ) VALUES (
            'outbox:' || v_event_id, v_event_id, v_topic, to_jsonb(NEW),
            'pending', 0, NULL, NULL, NEW.created_at, NULL, NEW.created_at
        );
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION identity_audit_transfer_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_action text;
    v_topic text;
    v_event_id text;
    v_actor text;
    v_transfer_event_id text;
BEGIN
    v_actor := NULLIF(current_setting('app.identity_actor', true), '');
    v_event_id := 'evt:transfer:' || gen_random_uuid()::text;
    IF TG_OP = 'INSERT' THEN
        v_action := 'transfer.create';
        v_topic := 'identity.transfer.created';
    ELSIF NEW.status = 'accepted' THEN
        v_action := 'transfer.accept';
        v_topic := 'identity.transfer.accepted';
    ELSIF NEW.status = 'cancelled' THEN
        v_action := 'transfer.cancel';
        v_topic := 'identity.transfer.cancelled';
    ELSIF NEW.status = 'expired' THEN
        v_action := 'transfer.expire';
        v_topic := NULL;
    ELSIF NEW.status = 'conflicted' THEN
        v_action := 'transfer.conflict';
        v_topic := NULL;
    ELSE
        v_action := 'transfer.' || NEW.status;
        v_topic := NULL;
    END IF;
    INSERT INTO identity_audit_events (
        event_id, action, actor_person_id, subject_person_id,
        person_id, device_id, binding_id, relationship_id,
        payload_json, created_at
    ) VALUES (
        v_event_id, v_action, v_actor, NEW.to_account_owner_person_id,
        NEW.from_account_owner_person_id, NEW.device_id,
        NEW.supersedes_binding_id, NULL, to_jsonb(NEW), NEW.updated_at
    );
    IF v_topic IS NOT NULL THEN
        INSERT INTO identity_outbox (
            outbox_id, event_id, topic, payload_json, status,
            attempts, locked_until, last_error_code, created_at,
            delivered_at, updated_at
        ) VALUES (
            'outbox:' || v_event_id, v_event_id, v_topic, to_jsonb(NEW),
            'pending', 0, NULL, NULL, NEW.updated_at, NULL, NEW.updated_at
        );
    END IF;
    -- The transfer accept transaction also transfers the binding: synthesize
    -- the binding.transfer event with its own business event identity.
    IF NEW.status = 'accepted' THEN
        v_transfer_event_id := 'evt:binding-transfer:' || gen_random_uuid()::text;
        INSERT INTO identity_audit_events (
            event_id, action, actor_person_id, subject_person_id,
            person_id, device_id, binding_id, relationship_id,
            payload_json, created_at
        ) VALUES (
            v_transfer_event_id, 'binding.transfer', v_actor,
            NEW.to_account_owner_person_id, NEW.from_account_owner_person_id,
            NEW.device_id, NEW.resulting_binding_id, NULL,
            to_jsonb(NEW), NEW.updated_at
        );
        INSERT INTO identity_outbox (
            outbox_id, event_id, topic, payload_json, status,
            attempts, locked_until, last_error_code, created_at,
            delivered_at, updated_at
        ) VALUES (
            'outbox:' || v_transfer_event_id, v_transfer_event_id,
            'identity.binding.transferred', to_jsonb(NEW), 'pending', 0,
            NULL, NULL, NEW.updated_at, NULL, NEW.updated_at
        );
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION identity_audit_person_update_event() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public SET row_security = on AS $$
DECLARE
    v_actor text;
    v_action text;
BEGIN
    -- Authoritative verifications write their own evidence-carrying audit
    -- row; the trigger is suppressed for those statements.
    IF NULLIF(current_setting('app.identity_suppress_person_audit', true), '')
       = '1' THEN
        RETURN NEW;
    END IF;
    v_actor := NULLIF(current_setting('app.identity_actor', true), '');
    IF v_actor = NEW.person_id THEN
        v_action := 'person.self_update';
    ELSIF v_actor IS NULL OR v_actor = '' THEN
        v_action := 'person.system_update';
    ELSE
        v_action := 'person.age_evidence.update';
    END IF;
    INSERT INTO identity_audit_events (
        event_id, action, actor_person_id, subject_person_id,
        person_id, device_id, binding_id, relationship_id,
        payload_json, created_at
    ) VALUES (
        'evt:person:' || gen_random_uuid()::text, v_action, v_actor,
        NEW.person_id, NEW.person_id, NULL, NULL, NULL,
        to_jsonb(NEW), NEW.updated_at
    );
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS identity_relationships_audit_trigger
    ON identity_relationships;
CREATE TRIGGER identity_relationships_audit_trigger
AFTER INSERT OR UPDATE ON identity_relationships
FOR EACH ROW EXECUTE FUNCTION identity_audit_relationship_event();

DROP TRIGGER IF EXISTS identity_bindings_audit_trigger
    ON identity_device_bindings;
CREATE TRIGGER identity_bindings_audit_trigger
AFTER INSERT OR UPDATE ON identity_device_bindings
FOR EACH ROW EXECUTE FUNCTION identity_audit_binding_event();

DROP TRIGGER IF EXISTS identity_transfers_audit_trigger
    ON identity_transfer_intents;
CREATE TRIGGER identity_transfers_audit_trigger
AFTER INSERT OR UPDATE ON identity_transfer_intents
FOR EACH ROW EXECUTE FUNCTION identity_audit_transfer_event();

DROP TRIGGER IF EXISTS identity_persons_update_audit_trigger
    ON identity_persons;
CREATE TRIGGER identity_persons_update_audit_trigger
AFTER UPDATE ON identity_persons
FOR EACH ROW EXECUTE FUNCTION identity_audit_person_update_event();

-- Trigger functions are only callable by their triggers, but they are still
-- SECURITY DEFINER and are revoked from PUBLIC for defense in depth.
REVOKE ALL ON FUNCTION identity_audit_relationship_event() FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_audit_binding_event() FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_audit_transfer_event() FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_audit_person_update_event() FROM PUBLIC;

REVOKE ALL ON FUNCTION identity_person_exists(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_relationship_active(
    text, text, text, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_person_visible(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_relationship_visible(
    text, text, text, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_relationship_parent_managed(text, text)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_binding_visible(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_binding_manage_allowed(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_role_visible(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_transfer_visible(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_audit_visible(
    text, text, text, text, text, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_shares_active_binding(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_actor() FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_scope() FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_idempotency_visible(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_register_person(
    text, text, text, text, text, text, text, text, timestamptz, timestamptz,
    text, jsonb, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_write_idempotency(
    text, text, text, text, jsonb, timestamptz
) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_patch_idempotency_result(text, text, jsonb)
    FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_self_update_profile(
    text, text, text, text, timestamptz, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_declare_age_evidence(
    text, text, timestamptz, text
) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_verify_age_evidence(
    text, text, text, text, text, timestamptz
) FROM PUBLIC;

-- Roles (admin bootstrap; LOGIN NOSUPERUSER NOBYPASSRLS) ----------------


DO $identity_policy$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity') THEN
        -- Person creation is an authoritative registration: the API role is
        -- never granted INSERT (identity_register_person is EXECUTE-only for
        -- the dedicated registration role).  Raw UPDATE is revoked entirely:
        -- profile/age changes go through the minimal action-level ports
        -- (self profile, age declaration, authoritative verification), so a
        -- forged actor GUC can never rewrite age/status fields directly.
        REVOKE INSERT ON identity_persons FROM memoria_identity;
        REVOKE UPDATE ON identity_persons FROM memoria_identity;
        GRANT SELECT ON identity_persons TO memoria_identity;
        GRANT SELECT, INSERT, UPDATE ON identity_relationships TO memoria_identity;
        GRANT SELECT, INSERT, UPDATE ON identity_device_bindings TO memoria_identity;
        GRANT SELECT, INSERT, UPDATE ON identity_device_binding_roles
            TO memoria_identity;
        GRANT SELECT, INSERT, UPDATE ON identity_transfer_intents TO memoria_identity;
        -- Audit, outbox and idempotency writes go through the SECURITY
        -- DEFINER write ports only; a forged GUC must never unlock raw DML.
        REVOKE INSERT, UPDATE ON identity_idempotency_records FROM memoria_identity;
        GRANT SELECT ON identity_idempotency_records TO memoria_identity;
        REVOKE INSERT ON identity_audit_events FROM memoria_identity;
        GRANT SELECT ON identity_audit_events TO memoria_identity;
        REVOKE INSERT ON identity_outbox FROM memoria_identity;

        DROP POLICY IF EXISTS identity_api_persons ON identity_persons;
        CREATE POLICY identity_api_persons ON identity_persons
            TO memoria_identity
            USING (identity_person_visible(identity_actor(), person_id))
            WITH CHECK (
                identity_actor() IS NOT NULL
                AND person_id = identity_actor()
            );

        DROP POLICY IF EXISTS identity_api_relationships ON identity_relationships;
        CREATE POLICY identity_api_relationships ON identity_relationships
            TO memoria_identity
            USING (
                identity_relationship_visible(
                    identity_actor(), source_person_id, target_person_id,
                    delegated_from_relationship_id
                )
            )
            WITH CHECK (
                identity_actor() IS NOT NULL
                AND (
                    source_person_id = identity_actor()
                    OR target_person_id = identity_actor()
                    OR (
                        delegated_from_relationship_id IS NOT NULL
                        AND identity_relationship_parent_managed(
                            identity_actor(), delegated_from_relationship_id
                        )
                    )
                )
            );

        DROP POLICY IF EXISTS identity_api_bindings ON identity_device_bindings;
        CREATE POLICY identity_api_bindings ON identity_device_bindings
            TO memoria_identity
            USING (
                identity_actor() IS NOT NULL AND (
                    account_owner_person_id = identity_actor()
                    OR identity_binding_visible(identity_actor(), binding_id)
                    OR EXISTS (
                        SELECT 1 FROM identity_transfer_intents ti
                        WHERE ti.supersedes_binding_id =
                              identity_device_bindings.binding_id
                          AND ti.to_account_owner_person_id = identity_actor()
                          AND ti.status IN ('pending', 'accepted')
                    )
                )
            )
            WITH CHECK (
                identity_actor() IS NOT NULL AND (
                    account_owner_person_id = identity_actor()
                    OR (
                        supersedes_binding_id IS NOT NULL
                        AND identity_binding_manage_allowed(
                            identity_actor(), supersedes_binding_id
                        )
                    )
                    OR EXISTS (
                        SELECT 1 FROM identity_transfer_intents ti
                        WHERE ti.supersedes_binding_id =
                              identity_device_bindings.binding_id
                          AND ti.to_account_owner_person_id = identity_actor()
                          AND ti.status IN ('pending', 'accepted')
                    )
                )
            );

        DROP POLICY IF EXISTS identity_api_roles ON identity_device_binding_roles;
        CREATE POLICY identity_api_roles ON identity_device_binding_roles
            TO memoria_identity
            USING (
                identity_actor() IS NOT NULL AND (
                    identity_role_visible(
                        identity_actor(), binding_id, person_id
                    )
                    OR EXISTS (
                        SELECT 1 FROM identity_transfer_intents ti
                        WHERE ti.supersedes_binding_id =
                              identity_device_binding_roles.binding_id
                          AND ti.to_account_owner_person_id = identity_actor()
                          AND ti.status IN ('pending', 'accepted')
                    )
                )
            )
            WITH CHECK (
                identity_actor() IS NOT NULL AND (
                    (
                        person_id = identity_actor()
                        AND role IN ('account_owner', 'primary_subject')
                    )
                    OR EXISTS (
                        SELECT 1 FROM identity_device_bindings b
                        WHERE b.binding_id = identity_device_binding_roles.binding_id
                          AND b.account_owner_person_id = identity_actor()
                    )
                    OR EXISTS (
                        SELECT 1 FROM identity_transfer_intents ti
                        WHERE ti.supersedes_binding_id =
                              identity_device_binding_roles.binding_id
                          AND ti.to_account_owner_person_id = identity_actor()
                          AND ti.status IN ('pending', 'accepted')
                    )
                )
            );

        DROP POLICY IF EXISTS identity_api_transfers ON identity_transfer_intents;
        CREATE POLICY identity_api_transfers ON identity_transfer_intents
            TO memoria_identity
            USING (identity_transfer_visible(identity_actor(), transfer_id))
            WITH CHECK (
                identity_actor() IS NOT NULL
                AND (
                    from_account_owner_person_id = identity_actor()
                    OR (
                        to_account_owner_person_id = identity_actor()
                        AND status IN ('accepted', 'expired', 'conflicted')
                    )
                )
            );

        DROP POLICY IF EXISTS identity_api_audit ON identity_audit_events;
        CREATE POLICY identity_api_audit ON identity_audit_events
            TO memoria_identity
            USING (
                identity_audit_visible(
                    identity_actor(), actor_person_id, person_id,
                    subject_person_id, device_id, action
                )
            )
            WITH CHECK (
                identity_actor() IS NOT NULL
            );

        -- The API never reads the outbox and never inserts into it directly;
        -- enqueue_outbox rides the mutation transaction via the write port.
        DROP POLICY IF EXISTS identity_api_outbox ON identity_outbox;

        -- Authority ports are executable by the API role only (pure booleans).
        GRANT EXECUTE ON FUNCTION identity_person_exists(text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_relationship_active(
            text, text, text, timestamptz
        ) TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_person_visible(text, text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_relationship_visible(
            text, text, text, text
        ) TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_relationship_parent_managed(
            text, text
        ) TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_binding_visible(text, text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_binding_manage_allowed(text, text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_role_visible(text, text, text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_transfer_visible(text, text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_audit_visible(
            text, text, text, text, text, text
        ) TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_shares_active_binding(text, text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_actor() TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_scope() TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_idempotency_visible(text, text)
            TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_write_idempotency(
            text, text, text, text, jsonb, timestamptz
        ) TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_patch_idempotency_result(
            text, text, jsonb
        ) TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_self_update_profile(
            text, text, text, text, timestamptz, text
        ) TO memoria_identity;
        GRANT EXECUTE ON FUNCTION identity_declare_age_evidence(
            text, text, timestamptz, text
        ) TO memoria_identity;

        DROP POLICY IF EXISTS identity_api_idempotency
            ON identity_idempotency_records;
        CREATE POLICY identity_api_idempotency ON identity_idempotency_records
            TO memoria_identity
            USING (identity_idempotency_visible(
                identity_actor(), scope_key
            ))
            WITH CHECK (identity_idempotency_visible(
                identity_actor(), scope_key
            ));
    END IF;

    -- Registration role: EXECUTE on the registration port only.  It holds
    -- no table privileges and cannot read or mutate relationships, bindings,
    -- transfers, audit or outbox; the port atomically writes person + audit
    -- + outbox inside the caller's transaction.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_registration') THEN
        GRANT EXECUTE ON FUNCTION identity_register_person(
            text, text, text, text, text, text, text, text, timestamptz, timestamptz,
            text, jsonb, text
        ) TO memoria_identity_registration;
        GRANT EXECUTE ON FUNCTION identity_verify_age_evidence(
            text, text, text, text, text, timestamptz
        ) TO memoria_identity_registration;
    END IF;

    -- Outbox worker: minimal role, outbox table only, status whitelist.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_outbox') THEN
        GRANT SELECT, UPDATE ON identity_outbox TO memoria_identity_outbox;
        DROP POLICY IF EXISTS identity_outbox_worker_outbox ON identity_outbox;
        CREATE POLICY identity_outbox_worker_outbox ON identity_outbox
            TO memoria_identity_outbox
            USING (status IN ('pending', 'processing', 'failed', 'dead_lettered'))
            WITH CHECK (status IN (
                'pending', 'processing', 'delivered', 'failed', 'dead_lettered'
            ));
    END IF;

    -- Migration role: maintenance only inside an explicit scope context.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_migration') THEN
        GRANT SELECT, INSERT, UPDATE ON identity_persons TO memoria_identity_migration;
        GRANT SELECT, INSERT, UPDATE ON identity_relationships
            TO memoria_identity_migration;
        GRANT SELECT, INSERT, UPDATE ON identity_device_bindings
            TO memoria_identity_migration;
        GRANT SELECT, INSERT, UPDATE ON identity_device_binding_roles
            TO memoria_identity_migration;
        GRANT SELECT, INSERT, UPDATE ON identity_transfer_intents
            TO memoria_identity_migration;
        GRANT SELECT, INSERT ON identity_idempotency_records
            TO memoria_identity_migration;
        GRANT SELECT, INSERT ON identity_audit_events TO memoria_identity_migration;
        GRANT SELECT, INSERT, UPDATE ON identity_outbox TO memoria_identity_migration;
        GRANT EXECUTE ON FUNCTION identity_scope() TO memoria_identity_migration;

        DROP POLICY IF EXISTS identity_migration_persons ON identity_persons;
        CREATE POLICY identity_migration_persons ON identity_persons
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');

        DROP POLICY IF EXISTS identity_migration_relationships ON identity_relationships;
        CREATE POLICY identity_migration_relationships ON identity_relationships
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');

        DROP POLICY IF EXISTS identity_migration_bindings ON identity_device_bindings;
        CREATE POLICY identity_migration_bindings ON identity_device_bindings
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');

        DROP POLICY IF EXISTS identity_migration_roles ON identity_device_binding_roles;
        CREATE POLICY identity_migration_roles ON identity_device_binding_roles
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');

        DROP POLICY IF EXISTS identity_migration_transfers ON identity_transfer_intents;
        CREATE POLICY identity_migration_transfers ON identity_transfer_intents
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');

        DROP POLICY IF EXISTS identity_migration_idempotency
            ON identity_idempotency_records;
        CREATE POLICY identity_migration_idempotency
            ON identity_idempotency_records
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');

        DROP POLICY IF EXISTS identity_migration_audit ON identity_audit_events;
        CREATE POLICY identity_migration_audit ON identity_audit_events
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');

        DROP POLICY IF EXISTS identity_migration_outbox ON identity_outbox;
        CREATE POLICY identity_migration_outbox ON identity_outbox
            TO memoria_identity_migration
            USING (identity_scope() = 'migration')
            WITH CHECK (identity_scope() = 'migration');
    END IF;
END
$identity_policy$;
