-- Memoria PolicyReceiptV2 append-only store.
--
-- Deployment requirements:
--  * Apply as the table-owner/bootstrap role. Runtime roles are always LOGIN,
--    NOSUPERUSER and NOBYPASSRLS; never deploy them as table owner.
--  * memoria_policy_api appends API decisions; memoria_policy_projector is the
--    connection-bound Session/profile consumer and may append/read a batch;
--    worker and audit are read-only; maintenance may append quarantine/import
--    records but has no mutation privilege. memoria_policy remains a legacy API
--    login during rollout and has the same append/read boundary as API.
--  * API/decision-producer access is transaction-local actor+subject scoped.
--    Projector, worker, audit and maintenance are explicit global roles.
--    The action executor has no table privilege; its bridge functions use the
--    existing app.authenticated_actor/app.authenticated_subject settings.
--  * No runtime role receives UPDATE, DELETE or TRUNCATE. A PolicyReceiptV2 row
--    is the immutable audit record; there is no separate audit/outbox table.

CREATE OR REPLACE FUNCTION policy_jsonb_string_array_v2(candidate JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $policy_jsonb_string_array$
    SELECT jsonb_typeof(candidate) = 'array'
       AND NOT EXISTS (
           SELECT 1 FROM jsonb_array_elements(candidate) AS item
           WHERE jsonb_typeof(item) <> 'string'
              OR char_length(item #>> '{}') NOT BETWEEN 1 AND 128
       )
$policy_jsonb_string_array$;

CREATE OR REPLACE FUNCTION policy_jsonb_positive_int_array_v2(candidate JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $policy_jsonb_positive_int_array$
    SELECT jsonb_typeof(candidate) = 'array'
       AND NOT EXISTS (
           SELECT 1 FROM jsonb_array_elements(candidate) AS item
           WHERE jsonb_typeof(item) <> 'number'
              OR item::text !~ '^[1-9][0-9]*$'
       )
$policy_jsonb_positive_int_array$;

CREATE OR REPLACE FUNCTION policy_valid_obligations_v2(candidate JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $policy_valid_obligations$
    SELECT jsonb_typeof(candidate) = 'array'
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(candidate) AS obligation
           WHERE jsonb_typeof(obligation) <> 'object'
              OR NOT obligation ?& ARRAY['code', 'params']
              OR obligation - ARRAY['code', 'params'] <> '{}'::jsonb
              OR jsonb_typeof(obligation -> 'code') <> 'string'
              OR jsonb_typeof(obligation -> 'params') <> 'object'
              OR NOT (obligation -> 'params') ?& ARRAY[
                    'max_session_seconds', 'retention_ttl_seconds',
                    'quiet_hours', 'extras'
                 ]
              OR (obligation -> 'params') - ARRAY[
                    'max_session_seconds', 'retention_ttl_seconds',
                    'quiet_hours', 'extras'
                 ] <> '{}'::jsonb
              OR jsonb_typeof(obligation -> 'params' -> 'extras') <> 'array'
              OR NOT (
                    jsonb_typeof(obligation -> 'params' -> 'quiet_hours') = 'null'
                    OR (
                        jsonb_typeof(obligation -> 'params' -> 'quiet_hours') = 'array'
                        AND jsonb_array_length(
                            obligation -> 'params' -> 'quiet_hours'
                        ) = 2
                        AND NOT EXISTS (
                            SELECT 1 FROM jsonb_array_elements(
                                obligation -> 'params' -> 'quiet_hours'
                            ) AS quiet_item
                            WHERE jsonb_typeof(quiet_item) <> 'string'
                        )
                    )
                 )
              OR NOT (
                    jsonb_typeof(
                        obligation -> 'params' -> 'max_session_seconds'
                    ) = 'null'
                    OR (
                        jsonb_typeof(
                            obligation -> 'params' -> 'max_session_seconds'
                        ) = 'number'
                        AND (obligation -> 'params' ->> 'max_session_seconds')
                            ~ '^[1-9][0-9]*$'
                    )
                 )
              OR NOT (
                    jsonb_typeof(
                        obligation -> 'params' -> 'retention_ttl_seconds'
                    ) = 'null'
                    OR (
                        jsonb_typeof(
                            obligation -> 'params' -> 'retention_ttl_seconds'
                        ) = 'number'
                        AND (obligation -> 'params' ->> 'retention_ttl_seconds')
                            ~ '^[1-9][0-9]*$'
                    )
                 )
              OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements(
                        obligation -> 'params' -> 'extras'
                    ) AS extra_item
                    WHERE jsonb_typeof(extra_item) <> 'array'
                       OR jsonb_array_length(extra_item) <> 2
                       OR EXISTS (
                            SELECT 1 FROM jsonb_array_elements(extra_item) AS part
                            WHERE jsonb_typeof(part) <> 'string'
                       )
                 )
       )
$policy_valid_obligations$;

CREATE TABLE IF NOT EXISTS policy_receipts_v2 (
    receipt_id TEXT PRIMARY KEY CHECK (
        char_length(receipt_id) BETWEEN 1 AND 192
    ),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    subject_id TEXT CHECK (
        subject_id IS NULL OR char_length(subject_id) BETWEEN 1 AND 128
    ),
    resource_owner_id TEXT CHECK (
        resource_owner_id IS NULL
        OR char_length(resource_owner_id) BETWEEN 1 AND 128
    ),
    device_id TEXT NOT NULL CHECK (char_length(device_id) BETWEEN 1 AND 128),
    capability TEXT NOT NULL CHECK (capability IN (
        'chat', 'tutor', 'english_practice', 'memory_capture',
        'memory_promotion', 'family_shared_memory_proposal',
        'family_shared_memory_approval', 'family_shared_memory_promotion',
        'memory_recall_private', 'guardian_summary_view',
        'voice_profile_create', 'voice_clone_use', 'digital_self_preview',
        'legacy_grant_create', 'payment', 'raw_audio_retention',
        'model_training_contribution', 'crisis_notification',
        'device_ownership_transfer'
    )),
    purpose TEXT NOT NULL CHECK (purpose IN (
        'user_request', 'runtime_profile_issue', 'runtime_sensitive_action',
        'voice_profile', 'voice_clone', 'digital_self', 'legacy', 'payment',
        'raw_audio', 'model_training', 'device_transfer', 'memory_capture',
        'memory_promotion', 'family_shared_memory_proposal',
        'family_shared_memory_approval', 'family_shared_memory_promotion',
        'memory_recall', 'guardian_summary', 'crisis_response'
    )),
    effect TEXT NOT NULL CHECK (effect IN (
        'deny', 'allow', 'allow_with_obligations'
    )),
    reason_code TEXT NOT NULL CHECK (
        char_length(reason_code) BETWEEN 1 AND 256
    ),
    obligations JSONB NOT NULL CHECK (
        jsonb_typeof(obligations) = 'array'
        AND policy_valid_obligations_v2(obligations)
    ),
    policy_version TEXT NOT NULL CHECK (
        char_length(policy_version) BETWEEN 1 AND 128
    ),
    context_hash TEXT NOT NULL CHECK (context_hash ~ '^[a-f0-9]{64}$'),
    action_resource_fence JSONB NOT NULL CHECK (
        jsonb_typeof(action_resource_fence) = 'object'
        AND action_resource_fence ?& ARRAY[
            'action_fence_schema', 'capability', 'purpose',
            'action_resource_id', 'action_revision', 'action_evidence_hash',
            'canonical_hash', 'family_space_id', 'family_owner_subject_id',
            'proposal_id', 'proposal_revision', 'voter_subject_id',
            'approval_decision', 'required_approval_subject_ids',
            'approval_snapshots', 'capture_evidence_ids',
            'capture_evidence_hash', 'consent_snapshot_id',
            'consent_snapshot_revision', 'consent_snapshot_hash',
            'membership_snapshot_id', 'membership_snapshot_revision',
            'membership_snapshot_hash', 'generation_id', 'turn_id',
            'tool_epoch', 'issued_at', 'valid_until'
        ]
        AND action_resource_fence - ARRAY[
            'action_fence_schema', 'capability', 'purpose',
            'action_resource_id', 'action_revision', 'action_evidence_hash',
            'canonical_hash', 'family_space_id', 'family_owner_subject_id',
            'proposal_id', 'proposal_revision', 'voter_subject_id',
            'approval_decision', 'required_approval_subject_ids',
            'approval_snapshots', 'capture_evidence_ids',
            'capture_evidence_hash', 'consent_snapshot_id',
            'consent_snapshot_revision', 'consent_snapshot_hash',
            'membership_snapshot_id', 'membership_snapshot_revision',
            'membership_snapshot_hash', 'generation_id', 'turn_id',
            'tool_epoch', 'issued_at', 'valid_until'
        ] = '{}'::jsonb
        AND jsonb_typeof(action_resource_fence -> 'approval_snapshots') = 'array'
        AND jsonb_typeof(
            action_resource_fence -> 'required_approval_subject_ids'
        ) = 'array'
        AND jsonb_typeof(action_resource_fence -> 'capture_evidence_ids') = 'array'
    ),
    action_fence_hash TEXT NOT NULL CHECK (
        action_fence_hash ~ '^[a-f0-9]{64}$'
    ),
    consent_snapshot_ids JSONB NOT NULL CHECK (
        jsonb_typeof(consent_snapshot_ids) = 'array'
        AND policy_jsonb_string_array_v2(consent_snapshot_ids)),
    consent_snapshot_revisions JSONB NOT NULL CHECK (
        jsonb_typeof(consent_snapshot_revisions) = 'array'
        AND policy_jsonb_positive_int_array_v2(consent_snapshot_revisions)),
    relationship_snapshot_ids JSONB NOT NULL CHECK (
        jsonb_typeof(relationship_snapshot_ids) = 'array'
        AND policy_jsonb_string_array_v2(relationship_snapshot_ids)),
    relationship_snapshot_revisions JSONB NOT NULL CHECK (
        jsonb_typeof(relationship_snapshot_revisions) = 'array'
        AND policy_jsonb_positive_int_array_v2(relationship_snapshot_revisions)),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    binding_canonical_hash TEXT CHECK (
        binding_canonical_hash IS NULL
        OR binding_canonical_hash ~ '^[a-f0-9]{64}$'
    ),
    session_id TEXT NOT NULL CHECK (char_length(session_id) BETWEEN 1 AND 128),
    session_epoch INTEGER NOT NULL CHECK (session_epoch >= 1),
    runtime_profile_id TEXT NOT NULL CHECK (
        char_length(runtime_profile_id) BETWEEN 1 AND 128
    ),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    device_trust TEXT NOT NULL CHECK (device_trust IN (
        'trusted', 'verified', 'offline', 'untrusted', 'revoked'
    )),
    data_classification TEXT NOT NULL CHECK (data_classification IN (
        'ephemeral', 'public', 'private', 'biometric', 'aggregate',
        'safety_minimum', 'study_progress'
    )),
    safety_state TEXT NOT NULL CHECK (safety_state IN (
        'normal', 'concern', 'elevated_risk', 'self_crisis'
    )),
    jurisdiction TEXT NOT NULL CHECK (
        char_length(jurisdiction) BETWEEN 1 AND 128
    ),
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL CHECK (expires_at > created_at),
    exact_fence BOOLEAN NOT NULL,
    CHECK (
        NOT exact_fence
        OR binding_canonical_hash IS NOT NULL
    )
);

ALTER TABLE policy_receipts_v2 ENABLE ROW LEVEL SECURITY;
ALTER TABLE policy_receipts_v2 FORCE ROW LEVEL SECURITY;

-- Runtime consumers have no UPDATE privilege, which PostgreSQL otherwise
-- requires for SELECT ... FOR SHARE.  This narrow bootstrap-owned function
-- acquires only the immutable receipt row lock; callers then read through RLS.
CREATE OR REPLACE FUNCTION policy_lock_receipt_v2(p_receipt_id TEXT)
RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $policy_lock_receipt$
DECLARE
    caller_role TEXT := session_user;
    authenticated_actor TEXT := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
    authenticated_subject TEXT := NULLIF(
        current_setting('app.authenticated_subject', true), ''
    );
    receipt_actor TEXT;
    receipt_subject TEXT;
BEGIN
    IF caller_role NOT IN (
        'memoria_policy', 'memoria_policy_api',
        'memoria_policy_projector', 'memoria_policy_worker',
        'memoria_policy_audit', 'memoria_policy_maintenance',
        'memoria_action_executor'
    ) THEN
        RAISE EXCEPTION 'policy receipt lock caller is not authorized'
            USING ERRCODE = 'SR403';
    END IF;

    SELECT actor_id, subject_id
      INTO receipt_actor, receipt_subject
      FROM public.policy_receipts_v2
     WHERE receipt_id = p_receipt_id
     FOR SHARE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    IF caller_role IN (
        'memoria_policy', 'memoria_policy_api', 'memoria_action_executor'
    ) THEN
        IF authenticated_actor IS NULL
           OR receipt_actor IS DISTINCT FROM authenticated_actor THEN
            RAISE EXCEPTION 'policy receipt actor context mismatch'
                USING ERRCODE = 'SR403';
        END IF;
        IF receipt_subject IS DISTINCT FROM authenticated_subject THEN
            RAISE EXCEPTION 'policy receipt subject context mismatch'
                USING ERRCODE = 'SR403';
        END IF;
    END IF;
    RETURN FOUND;
END
$policy_lock_receipt$;

DO $policy_receipt_roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_policy'
    ) THEN
        CREATE ROLE memoria_policy LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_policy LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_policy_audit'
    ) THEN
        CREATE ROLE memoria_policy_audit LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_policy_audit LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_policy_api'
    ) THEN
        CREATE ROLE memoria_policy_api LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_policy_api LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_policy_projector'
    ) THEN
        CREATE ROLE memoria_policy_projector LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_policy_projector LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_policy_worker'
    ) THEN
        CREATE ROLE memoria_policy_worker LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_policy_worker LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_policy_maintenance'
    ) THEN
        CREATE ROLE memoria_policy_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_policy_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$policy_receipt_roles$;

REVOKE ALL PRIVILEGES ON TABLE policy_receipts_v2 FROM PUBLIC;
REVOKE ALL PRIVILEGES ON TABLE policy_receipts_v2 FROM memoria_policy;
REVOKE ALL PRIVILEGES ON TABLE policy_receipts_v2 FROM memoria_policy_api;
REVOKE ALL PRIVILEGES ON TABLE policy_receipts_v2 FROM memoria_policy_projector;
REVOKE ALL PRIVILEGES ON TABLE policy_receipts_v2 FROM memoria_policy_worker;
REVOKE ALL PRIVILEGES ON TABLE policy_receipts_v2 FROM memoria_policy_audit;
REVOKE ALL PRIVILEGES ON TABLE policy_receipts_v2 FROM memoria_policy_maintenance;
GRANT SELECT, INSERT ON TABLE policy_receipts_v2 TO memoria_policy;
GRANT SELECT, INSERT ON TABLE policy_receipts_v2 TO memoria_policy_api;
GRANT SELECT, INSERT ON TABLE policy_receipts_v2 TO memoria_policy_projector;
GRANT SELECT ON TABLE policy_receipts_v2 TO memoria_policy_worker;
GRANT SELECT ON TABLE policy_receipts_v2 TO memoria_policy_audit;
GRANT SELECT, INSERT ON TABLE policy_receipts_v2 TO memoria_policy_maintenance;
REVOKE ALL PRIVILEGES ON FUNCTION policy_lock_receipt_v2(TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION policy_lock_receipt_v2(TEXT)
    TO memoria_policy, memoria_policy_api, memoria_policy_projector,
       memoria_policy_worker, memoria_policy_maintenance;

DROP POLICY IF EXISTS policy_receipts_api_select ON policy_receipts_v2;
CREATE POLICY policy_receipts_api_select ON policy_receipts_v2
    FOR SELECT TO memoria_policy
    USING (
        current_user = 'memoria_policy'
        AND NULLIF(current_setting('app.authenticated_actor', true), '')
            IS NOT NULL
        AND actor_id = NULLIF(
            current_setting('app.authenticated_actor', true), ''
        )
        AND subject_id IS NOT DISTINCT FROM NULLIF(
            current_setting('app.authenticated_subject', true), ''
        )
    );

DROP POLICY IF EXISTS policy_receipts_api_insert ON policy_receipts_v2;
CREATE POLICY policy_receipts_api_insert ON policy_receipts_v2
    FOR INSERT TO memoria_policy
    WITH CHECK (
        current_user = 'memoria_policy'
        AND NULLIF(current_setting('app.authenticated_actor', true), '')
            IS NOT NULL
        AND actor_id = NULLIF(
            current_setting('app.authenticated_actor', true), ''
        )
        AND subject_id IS NOT DISTINCT FROM NULLIF(
            current_setting('app.authenticated_subject', true), ''
        )
    );

DROP POLICY IF EXISTS policy_receipts_v2_api_select ON policy_receipts_v2;
CREATE POLICY policy_receipts_v2_api_select ON policy_receipts_v2
    FOR SELECT TO memoria_policy_api
    USING (
        current_user = 'memoria_policy_api'
        AND NULLIF(current_setting('app.authenticated_actor', true), '')
            IS NOT NULL
        AND actor_id = NULLIF(
            current_setting('app.authenticated_actor', true), ''
        )
        AND subject_id IS NOT DISTINCT FROM NULLIF(
            current_setting('app.authenticated_subject', true), ''
        )
    );

DROP POLICY IF EXISTS policy_receipts_v2_api_insert ON policy_receipts_v2;
CREATE POLICY policy_receipts_v2_api_insert ON policy_receipts_v2
    FOR INSERT TO memoria_policy_api
    WITH CHECK (
        current_user = 'memoria_policy_api'
        AND NULLIF(current_setting('app.authenticated_actor', true), '')
            IS NOT NULL
        AND actor_id = NULLIF(
            current_setting('app.authenticated_actor', true), ''
        )
        AND subject_id IS NOT DISTINCT FROM NULLIF(
            current_setting('app.authenticated_subject', true), ''
        )
    );

DROP POLICY IF EXISTS policy_receipts_projector_select ON policy_receipts_v2;
CREATE POLICY policy_receipts_projector_select ON policy_receipts_v2
    FOR SELECT TO memoria_policy_projector
    USING (current_user = 'memoria_policy_projector');

DROP POLICY IF EXISTS policy_receipts_projector_insert ON policy_receipts_v2;
CREATE POLICY policy_receipts_projector_insert ON policy_receipts_v2
    FOR INSERT TO memoria_policy_projector
    WITH CHECK (current_user = 'memoria_policy_projector');

DROP POLICY IF EXISTS policy_receipts_worker_select ON policy_receipts_v2;
CREATE POLICY policy_receipts_worker_select ON policy_receipts_v2
    FOR SELECT TO memoria_policy_worker
    USING (current_user = 'memoria_policy_worker');

DROP POLICY IF EXISTS policy_receipts_audit_select ON policy_receipts_v2;
CREATE POLICY policy_receipts_audit_select ON policy_receipts_v2
    FOR SELECT TO memoria_policy_audit
    USING (current_user = 'memoria_policy_audit');

DROP POLICY IF EXISTS policy_receipts_maintenance_select ON policy_receipts_v2;
CREATE POLICY policy_receipts_maintenance_select ON policy_receipts_v2
    FOR SELECT TO memoria_policy_maintenance
    USING (current_user = 'memoria_policy_maintenance');

DROP POLICY IF EXISTS policy_receipts_maintenance_insert ON policy_receipts_v2;
CREATE POLICY policy_receipts_maintenance_insert ON policy_receipts_v2
    FOR INSERT TO memoria_policy_maintenance
    WITH CHECK (current_user = 'memoria_policy_maintenance');
