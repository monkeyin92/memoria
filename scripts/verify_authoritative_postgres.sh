#!/bin/sh
set -eu

# Verify the installed production authority contract without exposing any
# credential. The check intentionally runs through the local administrator
# socket inside the PostgreSQL container; no administrator DSN is persisted in
# a long-lived application env.
: "${POSTGRES_CONTAINER:?set POSTGRES_CONTAINER to the running PostgreSQL container}"

actual="$(
  docker exec -i "$POSTGRES_CONTAINER" \
    psql \
      --username memoria_admin \
      --dbname memoria \
      --set=ON_ERROR_STOP=1 \
      --tuples-only \
      --no-align <<'SQL'
WITH runtime_roles(role_name) AS (
    VALUES
        ('memoria_app'),
        ('memoria_compiler'),
        ('memoria_evolution'),
        ('memoria_guardian'),
        ('memoria_guardian_maintenance'),
        ('memoria_guardian_worker'),
        ('memoria_identity'),
        ('memoria_identity_registration'),
        ('memoria_consent'),
        ('memoria_device_onboarding_api'),
        ('memoria_device_onboarding_maintenance'),
        ('memoria_session_api'),
        ('memoria_action_executor'),
        ('memoria_session_projector'),
        ('memoria_session_worker'),
        ('memoria_session_maintenance'),
        ('memoria_memory_api'),
        ('memoria_memory_worker')
),
required_tables(table_name, force_rls) AS (
    VALUES
        ('identity_persons', FALSE),
        ('identity_relationships', FALSE),
        ('identity_device_bindings', FALSE),
        ('identity_device_binding_roles', FALSE),
        ('identity_audit_events', FALSE),
        ('identity_outbox', FALSE),
        ('identity_transfer_intents', FALSE),
        ('identity_idempotency_records', FALSE),
        ('consent_authorization', TRUE),
        ('consent_offer', TRUE),
        ('consent_evidence', TRUE),
        ('consent_snapshot', TRUE),
        ('binding_consent_snapshot', TRUE),
        ('consent_outbox', TRUE),
        ('consent_audit', TRUE),
        ('consent_idempotency', TRUE),
        ('consent_offer_head', TRUE),
        ('consent_evidence_head', TRUE),
        ('consent_snapshot_head', TRUE),
        ('policy_receipts_v2', TRUE),
        ('device_fleet_devices', TRUE),
        ('device_fleet_certificates', TRUE),
        ('device_fleet_attestation_challenges', TRUE),
        ('device_fleet_attestations', TRUE),
        ('device_fleet_remote_commands', TRUE),
        ('device_fleet_remote_command_executions', TRUE),
        ('device_fleet_command_outbox', TRUE),
        ('device_fleet_dispatcher_heartbeats', TRUE),
        ('device_fleet_binding_events', TRUE),
        ('device_fleet_sim_profiles', TRUE),
        ('device_fleet_sim_events', TRUE),
        ('device_fleet_ota_assignments', TRUE),
        ('device_fleet_ota_receipts', TRUE),
        ('device_onboarding_devices', TRUE),
        ('device_onboarding_sessions', TRUE),
        ('device_onboarding_events', TRUE),
        ('device_onboarding_challenges', TRUE),
        ('device_media_challenges', TRUE),
        ('device_onboarding_claims', TRUE),
        ('device_onboarding_bindings', TRUE),
        ('device_onboarding_activations', TRUE),
        ('session_runtime_contexts', TRUE),
        ('session_runtime_profiles', TRUE),
        ('session_runtime_profile_receipts', TRUE),
        ('session_runtime_events', TRUE),
        ('session_runtime_outbox', TRUE),
        ('session_runtime_idempotency', TRUE),
        ('evolution_learning_signals', TRUE),
        ('evolution_candidates', TRUE),
        ('evolution_validations', TRUE),
        ('evolution_activation_events', TRUE),
        ('evolution_lifecycle_events', TRUE),
        ('evolution_control_state', TRUE),
        ('evolution_account_deletion_fences', TRUE),
        ('evolution_sleep_signal_receipts', TRUE),
        ('guardian_links', TRUE),
        ('guardian_consents', TRUE),
        ('guardian_corpus_samples', TRUE),
        ('tutor_practice_sessions', TRUE),
        ('tutor_study_progress', TRUE),
        ('tutor_practice_evidence', TRUE),
        ('tutor_commit_outbox', TRUE),
        ('guardian_crisis_events', TRUE),
        ('guardian_notification_outbox', TRUE),
        ('memory_records', TRUE),
        ('memory_status_events', TRUE),
        ('memory_shared_proposals', TRUE),
        ('memory_shared_votes', TRUE),
        ('memory_outbox', TRUE),
        ('memory_audit_events', TRUE)
),
table_state AS (
    SELECT
        required_tables.table_name,
        required_tables.force_rls,
        tables.oid,
        tables.relrowsecurity,
        tables.relforcerowsecurity
    FROM required_tables
    LEFT JOIN pg_class AS tables
      ON tables.oid = to_regclass('public.' || required_tables.table_name)
     AND tables.relkind IN ('r', 'p')
),
required_functions(function_name) AS (
    VALUES
        ('identity_register_person'),
        ('consent_authorize'),
        ('policy_lock_receipt_v2'),
        ('device_fleet_action_commit_command'),
        ('device_onboarding_scope_actor'),
        ('device_onboarding_scope_device'),
        ('device_onboarding_scope_lookup'),
        ('device_onboarding_scope_maintenance'),
        ('session_runtime_commit_initial'),
        ('evolution_block_immutable_mutation'),
        ('guardian_tutor_outbox_claim'),
        ('memory_sensitive_commit')
),
owner_roles(role_name) AS (
    VALUES
        ('memoria_archive_compiler'),
        ('memoria_identity_owner'),
        ('memoria_consent_owner'),
        ('memoria_session_owner'),
        ('memoria_action_bridge_owner'),
        ('memoria_device_action_bridge_owner'),
        ('memoria_memory_owner')
),
required_table_owners(table_name, owner_role) AS (
    VALUES
        ('memory_records', 'memoria_memory_owner'),
        ('memory_status_events', 'memoria_memory_owner'),
        ('memory_shared_proposals', 'memoria_memory_owner'),
        ('memory_shared_votes', 'memoria_memory_owner'),
        ('memory_outbox', 'memoria_memory_owner'),
        ('memory_audit_events', 'memoria_memory_owner')
),
required_function_owners(function_name, owner_role) AS (
    VALUES
        ('memory_sensitive_commit', 'memoria_memory_owner')
)
SELECT
    (
        SELECT count(*)
        FROM runtime_roles
        LEFT JOIN pg_roles
          ON pg_roles.rolname = runtime_roles.role_name
        WHERE pg_roles.oid IS NULL
           OR NOT pg_roles.rolcanlogin
           OR pg_roles.rolsuper
           OR pg_roles.rolcreatedb
           OR pg_roles.rolcreaterole
           OR pg_roles.rolbypassrls
    )
    || '|' ||
    (
        SELECT count(*)
        FROM runtime_roles
        WHERE NOT has_database_privilege(
            runtime_roles.role_name,
            current_database(),
            'CONNECT'
        )
    )
    || '|' ||
    (SELECT count(*) FROM table_state WHERE oid IS NULL)
    || '|' ||
    (
        SELECT count(*)
        FROM table_state
        WHERE oid IS NOT NULL AND NOT relrowsecurity
    )
    || '|' ||
    (
        SELECT count(*)
        FROM table_state
        WHERE oid IS NOT NULL AND force_rls AND NOT relforcerowsecurity
    )
    || '|' ||
    (
        SELECT count(*)
        FROM required_functions
        WHERE NOT EXISTS (
            SELECT 1
            FROM pg_proc
            JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
            WHERE pg_namespace.nspname = 'public'
              AND pg_proc.proname = required_functions.function_name
        )
    )
    || '|' ||
    (
        SELECT count(*)
        FROM owner_roles
        LEFT JOIN pg_roles
          ON pg_roles.rolname = owner_roles.role_name
        WHERE pg_roles.oid IS NULL
           OR pg_roles.rolcanlogin
           OR pg_roles.rolsuper
           OR pg_roles.rolcreatedb
           OR pg_roles.rolcreaterole
           OR pg_roles.rolbypassrls
    )
    || '|' ||
    (
        SELECT count(*)
        FROM (
            SELECT required_table_owners.table_name
            FROM required_table_owners
            LEFT JOIN pg_class
              ON pg_class.oid = to_regclass(
                  'public.' || required_table_owners.table_name
              )
            LEFT JOIN pg_roles
              ON pg_roles.oid = pg_class.relowner
            WHERE pg_roles.rolname IS DISTINCT FROM
                  required_table_owners.owner_role
            UNION ALL
            SELECT required_function_owners.function_name
            FROM required_function_owners
            WHERE NOT EXISTS (
                SELECT 1
                FROM pg_proc
                JOIN pg_namespace
                  ON pg_namespace.oid = pg_proc.pronamespace
                JOIN pg_roles
                  ON pg_roles.oid = pg_proc.proowner
                WHERE pg_namespace.nspname = 'public'
                  AND pg_proc.proname =
                      required_function_owners.function_name
                  AND pg_roles.rolname =
                      required_function_owners.owner_role
            )
        ) AS ownership_violations
    );
SQL
)"

expected="0|0|0|0|0|0|0|0"
if [ "$actual" != "$expected" ]; then
  echo "authoritative PostgreSQL contract verification failed: $actual" >&2
  echo "expected invalid/missing counts: $expected" >&2
  exit 1
fi

echo "authoritative PostgreSQL contract verified"
