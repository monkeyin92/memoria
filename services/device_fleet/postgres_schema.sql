-- Device Fleet PR-16 authoritative store.
-- Runtime roles are LOGIN NOSUPERUSER NOBYPASSRLS and never own tables.
-- Schema/migration execution is restricted to an administrator or the
-- dedicated maintenance identity; application processes do not run DDL.

DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_device_fleet_api') THEN
        CREATE ROLE memoria_device_fleet_api LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_device_fleet_projector') THEN
        CREATE ROLE memoria_device_fleet_projector LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_device_fleet_worker') THEN
        CREATE ROLE memoria_device_fleet_worker LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_device_fleet_maintenance') THEN
        CREATE ROLE memoria_device_fleet_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_action_executor') THEN
        CREATE ROLE memoria_action_executor LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_action_executor LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$roles$;

CREATE TABLE IF NOT EXISTS device_fleet_devices (
    device_id TEXT PRIMARY KEY CHECK (char_length(device_id) BETWEEN 1 AND 128),
    family_space_id TEXT CHECK (family_space_id IS NULL OR char_length(family_space_id) BETWEEN 1 AND 128),
    binding_id TEXT CHECK (binding_id IS NULL OR char_length(binding_id) BETWEEN 1 AND 128),
    binding_version BIGINT CHECK (binding_version IS NULL OR binding_version >= 1),
    binding_version_floor BIGINT NOT NULL DEFAULT 0 CHECK (binding_version_floor >= 0),
    lifecycle_status TEXT NOT NULL CHECK (lifecycle_status IN (
        'manufactured', 'provisioned', 'bound', 'suspended', 'revoked',
        'wipe_pending', 'wiped', 'retired'
    )),
    lifecycle_reason_code TEXT,
    capability_manifest_hash TEXT NOT NULL CHECK (capability_manifest_hash ~ '^[a-f0-9]{64}$'),
    capabilities JSONB NOT NULL CHECK (jsonb_typeof(capabilities) = 'array'),
    firmware_version TEXT NOT NULL CHECK (char_length(firmware_version) BETWEEN 1 AND 64),
    firmware_security_version INTEGER NOT NULL CHECK (firmware_security_version >= 1),
    firmware_sha256 TEXT NOT NULL CHECK (firmware_sha256 ~ '^[a-f0-9]{64}$'),
    bootloader_version TEXT NOT NULL CHECK (char_length(bootloader_version) BETWEEN 1 AND 64),
    physical_mute_state TEXT NOT NULL DEFAULT 'unknown' CHECK (physical_mute_state IN (
        'unknown', 'unattested', 'engaged', 'disengaged', 'mismatch'
    )),
    privacy_light_state TEXT NOT NULL DEFAULT 'unknown' CHECK (privacy_light_state IN (
        'unknown', 'unattested', 'on', 'off', 'mismatch'
    )),
    attested_sim_status TEXT NOT NULL DEFAULT 'unknown' CHECK (attested_sim_status IN (
        'unknown', 'absent', 'inactive', 'active', 'suspended', 'revoked', 'expired'
    )),
    current_sim_id TEXT,
    sim_authority_revision BIGINT NOT NULL DEFAULT 0 CHECK (sim_authority_revision >= 0),
    active_ota_slot TEXT NOT NULL DEFAULT 'a' CHECK (active_ota_slot IN ('a', 'b')),
    ota_boot_status TEXT NOT NULL DEFAULT 'confirmed' CHECK (ota_boot_status IN (
        'unknown', 'empty', 'staged', 'booting', 'confirmed', 'failed',
        'rollback_pending', 'rolled_back'
    )),
    anti_rollback_floor_version TEXT NOT NULL CHECK (char_length(anti_rollback_floor_version) BETWEEN 1 AND 64),
    anti_rollback_floor_security_version INTEGER NOT NULL CHECK (anti_rollback_floor_security_version >= 1),
    current_certificate_id TEXT,
    last_attestation_counter BIGINT NOT NULL DEFAULT 0 CHECK (last_attestation_counter >= 0),
    last_ota_counter BIGINT NOT NULL DEFAULT 0 CHECK (last_ota_counter >= 0),
    last_command_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_command_sequence >= 0),
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version >= 1),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK ((lifecycle_status = 'bound') =
           (family_space_id IS NOT NULL AND binding_id IS NOT NULL AND binding_version IS NOT NULL)
           OR lifecycle_status IN ('suspended', 'revoked', 'wipe_pending'))
);

CREATE TABLE IF NOT EXISTS device_fleet_certificates (
    certificate_id TEXT PRIMARY KEY CHECK (char_length(certificate_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT,
    binding_id TEXT,
    binding_version BIGINT,
    public_key_b64 TEXT NOT NULL CHECK (char_length(public_key_b64) BETWEEN 40 AND 64),
    key_algorithm TEXT NOT NULL CHECK (key_algorithm = 'ed25519'),
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked', 'expired')),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revocation_reason_code TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    CHECK (valid_from < valid_until),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CHECK ((status = 'revoked') = (revocation_reason_code IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS device_fleet_one_active_certificate
    ON device_fleet_certificates(device_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS device_fleet_attestation_challenges (
    nonce TEXT PRIMARY KEY CHECK (char_length(nonce) BETWEEN 16 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    CHECK (issued_at < expires_at)
);

CREATE TABLE IF NOT EXISTS device_fleet_attestations (
    attestation_id TEXT PRIMARY KEY CHECK (char_length(attestation_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    certificate_id TEXT NOT NULL REFERENCES device_fleet_certificates(certificate_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    monotonic_counter BIGINT NOT NULL CHECK (monotonic_counter >= 1),
    nonce TEXT NOT NULL UNIQUE REFERENCES device_fleet_attestation_challenges(nonce),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL,
    UNIQUE (device_id, monotonic_counter),
    CHECK (occurred_at < expires_at)
);
CREATE INDEX IF NOT EXISTS device_fleet_attestations_latest
    ON device_fleet_attestations(device_id, monotonic_counter DESC);

CREATE TABLE IF NOT EXISTS device_fleet_remote_commands (
    command_id TEXT PRIMARY KEY CHECK (char_length(command_id) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    command_sequence BIGINT NOT NULL CHECK (command_sequence >= 1),
    command_type TEXT NOT NULL CHECK (command_type IN (
        'force_mute', 'revoke_device', 'secure_wipe', 'assign_ota', 'collect_diagnostics'
    )),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    reason_code TEXT NOT NULL CHECK (char_length(reason_code) BETWEEN 1 AND 128),
    target_certificate_id TEXT NOT NULL CHECK (char_length(target_certificate_id) BETWEEN 1 AND 128),
    expected_monotonic_counter BIGINT NOT NULL CHECK (expected_monotonic_counter >= 1),
    expected_firmware_security_version INTEGER NOT NULL CHECK (expected_firmware_security_version >= 1),
    parameters JSONB NOT NULL CHECK (jsonb_typeof(parameters) = 'object'),
    parameters_hash TEXT NOT NULL CHECK (parameters_hash ~ '^[a-f0-9]{64}$'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    binding_role TEXT NOT NULL CHECK (binding_role IN (
        'account_owner', 'device_admin', 'guardian'
    )),
    authority_receipt_id TEXT NOT NULL CHECK (char_length(authority_receipt_id) BETWEEN 1 AND 192),
    action_resource_id TEXT NOT NULL CHECK (char_length(action_resource_id) BETWEEN 1 AND 128),
    action_resource_hash TEXT NOT NULL CHECK (action_resource_hash ~ '^[a-f0-9]{64}$'),
    action_fence_hash TEXT NOT NULL CHECK (char_length(action_fence_hash) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'accepted', 'dispatched', 'device_acknowledged',
        'hardware_confirmed', 'failed', 'uncertain'
    )),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    dispatched_at TIMESTAMPTZ,
    device_acknowledged_at TIMESTAMPTZ,
    hardware_confirmed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    last_error_code TEXT,
    UNIQUE (device_id, idempotency_key),
    UNIQUE (device_id, command_sequence),
    CHECK (issued_at < expires_at),
    CHECK (status NOT IN ('accepted', 'dispatched', 'device_acknowledged',
                          'hardware_confirmed') OR accepted_at IS NOT NULL),
    CHECK (status NOT IN ('dispatched', 'device_acknowledged', 'hardware_confirmed')
           OR dispatched_at IS NOT NULL),
    CHECK (status NOT IN ('device_acknowledged', 'hardware_confirmed')
           OR device_acknowledged_at IS NOT NULL),
    CHECK (status <> 'hardware_confirmed' OR hardware_confirmed_at IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS device_fleet_remote_command_executions (
    execution_id TEXT PRIMARY KEY CHECK (char_length(execution_id) BETWEEN 1 AND 128),
    command_id TEXT NOT NULL UNIQUE REFERENCES device_fleet_remote_commands(command_id),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    command_sequence BIGINT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    accepted_at TIMESTAMPTZ NOT NULL,
    UNIQUE (device_id, command_sequence)
);

CREATE TABLE IF NOT EXISTS device_fleet_command_outbox (
    command_id TEXT PRIMARY KEY REFERENCES device_fleet_remote_commands(command_id),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    status TEXT NOT NULL CHECK (status IN (
        'ready', 'leased', 'dispatched', 'acknowledged', 'failed', 'uncertain'
    )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    lease_token TEXT,
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK ((status = 'leased') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS device_fleet_dispatcher_heartbeats (
    dispatcher_id TEXT NOT NULL CHECK (char_length(dispatcher_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    heartbeat_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (dispatcher_id, device_id)
);

CREATE TABLE IF NOT EXISTS device_fleet_binding_events (
    event_id TEXT PRIMARY KEY CHECK (char_length(event_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    event_type TEXT NOT NULL CHECK (event_type IN ('bound', 'unbound', 'transferred', 'rebound')),
    previous_family_space_id TEXT,
    previous_binding_id TEXT,
    previous_binding_version BIGINT,
    actor_id TEXT NOT NULL,
    authority_receipt_id TEXT NOT NULL CHECK (
        char_length(authority_receipt_id) BETWEEN 1 AND 192
    ),
    action_resource_id TEXT NOT NULL CHECK (
        char_length(action_resource_id) BETWEEN 1 AND 192
    ),
    action_resource_hash TEXT NOT NULL CHECK (
        action_resource_hash ~ '^[a-f0-9]{64}$'
    ),
    action_fence_hash TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);

-- SIM authority is server-owned.  Device attestations only update
-- device_fleet_devices.attested_sim_status and can never mutate these rows.
CREATE TABLE IF NOT EXISTS device_fleet_sim_profiles (
    sim_id TEXT PRIMARY KEY CHECK (char_length(sim_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    iccid TEXT CHECK (iccid IS NULL OR char_length(iccid) BETWEEN 6 AND 32),
    esim_profile_id TEXT CHECK (
        esim_profile_id IS NULL OR char_length(esim_profile_id) BETWEEN 1 AND 128
    ),
    provider TEXT NOT NULL CHECK (char_length(provider) BETWEEN 1 AND 128),
    profile_kind TEXT NOT NULL CHECK (profile_kind IN ('physical', 'esim')),
    provider_status TEXT NOT NULL CHECK (provider_status IN (
        'unknown', 'inactive', 'active', 'suspended', 'revoked', 'expired'
    )),
    server_status TEXT NOT NULL CHECK (server_status IN (
        'inactive', 'active', 'suspended', 'revoked', 'expired', 'replaced'
    )),
    authority_revision BIGINT NOT NULL CHECK (authority_revision >= 1),
    replaced_by_sim_id TEXT REFERENCES device_fleet_sim_profiles(sim_id),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (profile_kind = 'physical' AND esim_profile_id IS NULL)
        OR (profile_kind = 'esim' AND iccid IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS device_fleet_sim_profiles_device_revision
    ON device_fleet_sim_profiles(device_id, authority_revision DESC);

CREATE TABLE IF NOT EXISTS device_fleet_sim_events (
    event_id TEXT PRIMARY KEY CHECK (char_length(event_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    sim_id TEXT NOT NULL REFERENCES device_fleet_sim_profiles(sim_id),
    authority_revision BIGINT NOT NULL CHECK (authority_revision >= 1),
    action TEXT NOT NULL CHECK (action IN (
        'provision', 'suspend', 'revoke', 'expire', 'replace'
    )),
    previous_server_status TEXT,
    server_status TEXT NOT NULL,
    provider_status TEXT NOT NULL,
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    authority_receipt_id TEXT NOT NULL CHECK (
        char_length(authority_receipt_id) BETWEEN 1 AND 192
    ),
    action_resource_id TEXT NOT NULL CHECK (
        char_length(action_resource_id) BETWEEN 1 AND 192
    ),
    action_resource_hash TEXT NOT NULL CHECK (
        action_resource_hash ~ '^[a-f0-9]{64}$'
    ),
    action_fence_hash TEXT NOT NULL CHECK (action_fence_hash ~ '^[a-f0-9]{64}$'),
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS device_fleet_ota_assignments (
    assignment_id TEXT PRIMARY KEY CHECK (char_length(assignment_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    artifact_url TEXT NOT NULL CHECK (artifact_url ~ '^https://'),
    target_slot TEXT NOT NULL CHECK (target_slot IN ('a', 'b')),
    target_version TEXT NOT NULL,
    target_firmware_security_version INTEGER NOT NULL CHECK (target_firmware_security_version >= 1),
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[a-f0-9]{64}$'),
    artifact_size_bytes BIGINT NOT NULL CHECK (artifact_size_bytes >= 1),
    min_bootloader_version TEXT NOT NULL,
    anti_rollback_floor_version TEXT NOT NULL,
    anti_rollback_floor_security_version INTEGER NOT NULL CHECK (anti_rollback_floor_security_version >= 1),
    channel TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'downloading', 'staged', 'activated', 'cancelled', 'failed', 'superseded'
    )),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    binding_role TEXT NOT NULL CHECK (binding_role IN ('account_owner', 'device_admin')),
    authority_receipt_id TEXT NOT NULL CHECK (char_length(authority_receipt_id) BETWEEN 1 AND 192),
    action_resource_id TEXT NOT NULL CHECK (char_length(action_resource_id) BETWEEN 1 AND 128),
    action_resource_hash TEXT NOT NULL CHECK (action_resource_hash ~ '^[a-f0-9]{64}$'),
    action_fence_hash TEXT NOT NULL CHECK (char_length(action_fence_hash) BETWEEN 1 AND 128),
    action_parameters_hash TEXT NOT NULL CHECK (action_parameters_hash ~ '^[a-f0-9]{64}$'),
    target_certificate_id TEXT NOT NULL,
    expected_monotonic_counter BIGINT NOT NULL CHECK (expected_monotonic_counter >= 1),
    expected_firmware_security_version INTEGER NOT NULL CHECK (expected_firmware_security_version >= 1),
    assigned_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (assigned_at < expires_at)
);

CREATE TABLE IF NOT EXISTS device_fleet_ota_receipts (
    receipt_id TEXT PRIMARY KEY CHECK (char_length(receipt_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_fleet_devices(device_id),
    certificate_id TEXT NOT NULL REFERENCES device_fleet_certificates(certificate_id),
    assignment_id TEXT REFERENCES device_fleet_ota_assignments(assignment_id),
    family_space_id TEXT NOT NULL,
    binding_id TEXT NOT NULL,
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    monotonic_counter BIGINT NOT NULL CHECK (monotonic_counter >= 1),
    active_slot TEXT NOT NULL CHECK (active_slot IN ('a', 'b')),
    pending_slot TEXT CHECK (pending_slot IS NULL OR pending_slot IN ('a', 'b')),
    boot_status TEXT NOT NULL CHECK (boot_status IN (
        'empty', 'staged', 'booting', 'confirmed', 'failed',
        'rollback_pending', 'rolled_back'
    )),
    highest_accepted_firmware_security_version INTEGER NOT NULL,
    anti_rollback_floor_security_version INTEGER NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL,
    UNIQUE (device_id, monotonic_counter)
);
CREATE INDEX IF NOT EXISTS device_fleet_ota_receipts_latest
    ON device_fleet_ota_receipts(device_id, monotonic_counter DESC);

CREATE OR REPLACE FUNCTION device_fleet_scope_matches(
    row_device_id TEXT,
    row_binding_id TEXT,
    row_binding_version BIGINT,
    row_family_space_id TEXT
) RETURNS BOOLEAN
LANGUAGE sql
STABLE
SET search_path = pg_catalog, public
AS $scope$
    SELECT current_user = 'memoria_device_fleet_maintenance'
        OR (
            row_device_id = NULLIF(current_setting('app.device_fleet.device_id', true), '')
            AND row_binding_id = NULLIF(current_setting('app.device_fleet.binding_id', true), '')
            AND row_binding_version = NULLIF(current_setting('app.device_fleet.binding_version', true), '')::BIGINT
            AND row_family_space_id = NULLIF(current_setting('app.device_fleet.family_space_id', true), '')
        )
$scope$;

ALTER TABLE device_fleet_devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_certificates ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_attestation_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_attestations ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_remote_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_remote_command_executions ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_command_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_dispatcher_heartbeats ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_binding_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_sim_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_sim_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_ota_assignments ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_ota_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_devices FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_certificates FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_attestation_challenges FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_attestations FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_remote_commands FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_remote_command_executions FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_command_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_dispatcher_heartbeats FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_binding_events FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_sim_profiles FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_sim_events FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_ota_assignments FORCE ROW LEVEL SECURITY;
ALTER TABLE device_fleet_ota_receipts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS device_fleet_devices_scope ON device_fleet_devices;
CREATE POLICY device_fleet_devices_scope ON device_fleet_devices
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_certificates_scope ON device_fleet_certificates;
CREATE POLICY device_fleet_certificates_scope ON device_fleet_certificates
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_challenges_scope ON device_fleet_attestation_challenges;
CREATE POLICY device_fleet_challenges_scope ON device_fleet_attestation_challenges
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_attestations_scope ON device_fleet_attestations;
CREATE POLICY device_fleet_attestations_scope ON device_fleet_attestations
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_commands_scope ON device_fleet_remote_commands;
CREATE POLICY device_fleet_commands_scope ON device_fleet_remote_commands
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_executions_scope ON device_fleet_remote_command_executions;
CREATE POLICY device_fleet_executions_scope ON device_fleet_remote_command_executions
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_command_outbox_scope ON device_fleet_command_outbox;
CREATE POLICY device_fleet_command_outbox_scope ON device_fleet_command_outbox
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_dispatcher_heartbeats_scope ON device_fleet_dispatcher_heartbeats;
CREATE POLICY device_fleet_dispatcher_heartbeats_scope ON device_fleet_dispatcher_heartbeats
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_binding_events_scope ON device_fleet_binding_events;
CREATE POLICY device_fleet_binding_events_scope ON device_fleet_binding_events
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_sim_profiles_scope ON device_fleet_sim_profiles;
CREATE POLICY device_fleet_sim_profiles_scope ON device_fleet_sim_profiles
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_sim_events_scope ON device_fleet_sim_events;
CREATE POLICY device_fleet_sim_events_scope ON device_fleet_sim_events
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_ota_assignments_scope ON device_fleet_ota_assignments;
CREATE POLICY device_fleet_ota_assignments_scope ON device_fleet_ota_assignments
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));
DROP POLICY IF EXISTS device_fleet_ota_receipts_scope ON device_fleet_ota_receipts;
CREATE POLICY device_fleet_ota_receipts_scope ON device_fleet_ota_receipts
    USING (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id))
    WITH CHECK (device_fleet_scope_matches(device_id, binding_id, binding_version, family_space_id));

-- Read-only runtime roles receive EXECUTE on this exact-context lock port, not
-- UPDATE privileges on authority tables.  Locks are retained by the caller's
-- already-open transaction after the function returns.
CREATE OR REPLACE FUNCTION device_fleet_lock_trust_heads(
    p_device_id TEXT,
    p_binding_id TEXT,
    p_binding_version BIGINT,
    p_family_space_id TEXT
) RETURNS BOOLEAN
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $device_fleet_lock_trust_heads$
DECLARE
    device_row device_fleet_devices%ROWTYPE;
BEGIN
    IF session_user NOT IN (
        'memoria_device_fleet_api',
        'memoria_device_fleet_projector',
        'memoria_device_fleet_worker',
        'memoria_action_executor'
    ) THEN
        RAISE EXCEPTION 'device trust lock caller rejected';
    END IF;
    IF NULLIF(current_setting('app.device_fleet.device_id', true), '')
           IS DISTINCT FROM p_device_id
       OR NULLIF(current_setting('app.device_fleet.binding_id', true), '')
           IS DISTINCT FROM p_binding_id
       OR NULLIF(current_setting('app.device_fleet.binding_version', true), '')::BIGINT
           IS DISTINCT FROM p_binding_version
       OR NULLIF(current_setting('app.device_fleet.family_space_id', true), '')
           IS DISTINCT FROM p_family_space_id THEN
        RAISE EXCEPTION 'device trust lock context mismatch';
    END IF;

    SELECT * INTO device_row
    FROM device_fleet_devices
    WHERE device_id = p_device_id
      AND binding_id = p_binding_id
      AND binding_version = p_binding_version
      AND family_space_id = p_family_space_id
    FOR SHARE;
    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;
    IF device_row.current_certificate_id IS NOT NULL THEN
        PERFORM 1 FROM device_fleet_certificates
        WHERE certificate_id = device_row.current_certificate_id
        FOR SHARE;
    END IF;
    PERFORM 1 FROM device_fleet_attestations
    WHERE device_id = p_device_id
    ORDER BY monotonic_counter DESC LIMIT 1
    FOR SHARE;
    PERFORM 1 FROM device_fleet_binding_events
    WHERE device_id = p_device_id
    ORDER BY binding_version DESC, occurred_at DESC LIMIT 1
    FOR SHARE;
    IF device_row.current_sim_id IS NOT NULL THEN
        PERFORM 1 FROM device_fleet_sim_profiles
        WHERE sim_id = device_row.current_sim_id
        FOR SHARE;
    END IF;
    PERFORM 1 FROM device_fleet_remote_commands
    WHERE device_id = p_device_id
    ORDER BY command_sequence DESC LIMIT 1
    FOR SHARE;
    RETURN TRUE;
END
$device_fleet_lock_trust_heads$;
ALTER FUNCTION device_fleet_lock_trust_heads(TEXT, TEXT, BIGINT, TEXT)
    OWNER TO memoria_device_fleet_maintenance;
REVOKE ALL ON FUNCTION device_fleet_lock_trust_heads(TEXT, TEXT, BIGINT, TEXT)
    FROM PUBLIC;

CREATE OR REPLACE FUNCTION device_fleet_assert_action_authority(
    p_receipt_id TEXT,
    p_actor_id TEXT,
    p_device_id TEXT,
    p_binding_id TEXT,
    p_binding_version BIGINT,
    p_action_resource_id TEXT,
    p_action_resource_hash TEXT,
    p_action_fence_hash TEXT
) RETURNS VOID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $device_fleet_assert_action_authority$
DECLARE
    receipt JSONB;
    fence JSONB;
    authenticated_actor TEXT := NULLIF(
        current_setting('app.authenticated_actor', true), ''
    );
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'device action caller rejected';
    END IF;
    IF to_regprocedure('public.action_policy_lock_receipt(text)') IS NULL THEN
        RAISE EXCEPTION 'device action Policy authority unavailable';
    END IF;
    IF authenticated_actor IS DISTINCT FROM p_actor_id THEN
        RAISE EXCEPTION 'device action authenticated actor mismatch';
    END IF;
    EXECUTE 'SELECT public.action_policy_lock_receipt($1)'
        INTO receipt USING p_receipt_id;
    IF receipt IS NULL THEN
        RAISE EXCEPTION 'device action Policy receipt missing';
    END IF;
    fence := receipt -> 'action_resource_fence';
    IF receipt ->> 'actor_id' IS DISTINCT FROM p_actor_id
       OR receipt ->> 'device_id' IS DISTINCT FROM p_device_id
       OR receipt ->> 'binding_id' IS DISTINCT FROM p_binding_id
       OR (receipt ->> 'binding_version')::BIGINT IS DISTINCT FROM p_binding_version
       OR receipt ->> 'effect' NOT IN ('allow', 'allow_with_obligations')
       OR COALESCE((receipt ->> 'exact_fence')::BOOLEAN, FALSE) IS NOT TRUE
       OR receipt ->> 'device_trust' NOT IN ('trusted', 'verified')
       OR (receipt ->> 'expires_at')::TIMESTAMPTZ <= clock_timestamp()
       OR fence ->> 'action_resource_id' IS DISTINCT FROM p_action_resource_id
       OR (fence ->> 'action_revision')::BIGINT IS DISTINCT FROM p_binding_version
       OR receipt ->> 'action_fence_hash' IS DISTINCT FROM p_action_fence_hash
       OR p_action_resource_id IS DISTINCT FROM
          ('device-action-' || p_action_resource_hash) THEN
        RAISE EXCEPTION 'device action Policy receipt mismatch';
    END IF;
END
$device_fleet_assert_action_authority$;
ALTER FUNCTION device_fleet_assert_action_authority(
    TEXT, TEXT, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT
) OWNER TO memoria_device_fleet_maintenance;
REVOKE ALL ON FUNCTION device_fleet_assert_action_authority(
    TEXT, TEXT, TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT
) FROM PUBLIC;

-- The unified action role has no table ACL.  This function locks and returns
-- only one exact device action snapshot for Python signature construction.
CREATE OR REPLACE FUNCTION device_fleet_action_lock_snapshot(
    p_device_id TEXT,
    p_binding_id TEXT,
    p_binding_version BIGINT,
    p_family_space_id TEXT,
    p_idempotency_key TEXT DEFAULT NULL,
    p_assignment_id TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $device_fleet_action_lock_snapshot$
DECLARE
    device_row device_fleet_devices%ROWTYPE;
    certificate_payload JSONB;
    attestation_payload JSONB;
    sim_payload JSONB;
    existing_command_payload JSONB;
    assignment_payload JSONB;
    max_sequence BIGINT;
BEGIN
    IF session_user <> 'memoria_action_executor' THEN
        RAISE EXCEPTION 'device action snapshot caller rejected';
    END IF;
    IF NULLIF(current_setting('app.device_fleet.device_id', true), '')
           IS DISTINCT FROM p_device_id
       OR NULLIF(current_setting('app.device_fleet.binding_id', true), '')
           IS DISTINCT FROM p_binding_id
       OR NULLIF(current_setting('app.device_fleet.binding_version', true), '')::BIGINT
           IS DISTINCT FROM p_binding_version
       OR NULLIF(current_setting('app.device_fleet.family_space_id', true), '')
           IS DISTINCT FROM p_family_space_id THEN
        RAISE EXCEPTION 'device action snapshot context mismatch';
    END IF;
    SELECT * INTO device_row FROM device_fleet_devices
    WHERE device_id = p_device_id
      AND binding_id = p_binding_id
      AND binding_version = p_binding_version
      AND family_space_id = p_family_space_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF device_row.current_certificate_id IS NOT NULL THEN
        SELECT to_jsonb(c) INTO certificate_payload
        FROM device_fleet_certificates c
        WHERE c.certificate_id = device_row.current_certificate_id
        FOR SHARE;
    END IF;
    SELECT to_jsonb(a) INTO attestation_payload
    FROM device_fleet_attestations a
    WHERE a.device_id = p_device_id
    ORDER BY a.monotonic_counter DESC LIMIT 1
    FOR SHARE;
    PERFORM 1 FROM device_fleet_binding_events
    WHERE device_id = p_device_id
    ORDER BY binding_version DESC, occurred_at DESC LIMIT 1
    FOR SHARE;
    IF device_row.current_sim_id IS NOT NULL THEN
        SELECT to_jsonb(s) INTO sim_payload
        FROM device_fleet_sim_profiles s
        WHERE s.sim_id = device_row.current_sim_id
        FOR SHARE;
    END IF;
    PERFORM 1 FROM device_fleet_remote_commands
    WHERE device_id = p_device_id
    ORDER BY command_sequence DESC LIMIT 1
    FOR SHARE;
    SELECT COALESCE(MAX(command_sequence), 0) INTO max_sequence
    FROM device_fleet_remote_commands WHERE device_id = p_device_id;
    IF p_idempotency_key IS NOT NULL THEN
        SELECT to_jsonb(c) INTO existing_command_payload
        FROM device_fleet_remote_commands c
        WHERE c.device_id = p_device_id
          AND c.idempotency_key = p_idempotency_key
        FOR SHARE;
    END IF;
    IF p_assignment_id IS NOT NULL THEN
        SELECT to_jsonb(a) INTO assignment_payload
        FROM device_fleet_ota_assignments a
        WHERE a.device_id = p_device_id
          AND a.assignment_id = p_assignment_id
        FOR SHARE;
    END IF;
    RETURN jsonb_build_object(
        'device', to_jsonb(device_row),
        'certificate', certificate_payload,
        'attestation', attestation_payload,
        'sim', sim_payload,
        'existing_command', existing_command_payload,
        'assignment', assignment_payload,
        'max_command_sequence', max_sequence
    );
END
$device_fleet_action_lock_snapshot$;
ALTER FUNCTION device_fleet_action_lock_snapshot(
    TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT
) OWNER TO memoria_device_fleet_maintenance;
REVOKE ALL ON FUNCTION device_fleet_action_lock_snapshot(
    TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION device_fleet_action_commit_command(
    p_command JSONB,
    p_parameters JSONB,
    p_authority JSONB
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $device_fleet_action_commit_command$
DECLARE
    device_row device_fleet_devices%ROWTYPE;
    existing_row device_fleet_remote_commands%ROWTYPE;
    expected_sequence BIGINT;
    command_type_value TEXT := p_command ->> 'command_type';
BEGIN
    PERFORM device_fleet_assert_action_authority(
        p_authority ->> 'authority_receipt_id',
        p_command ->> 'actor_id',
        p_command ->> 'device_id',
        p_command ->> 'expected_binding_id',
        (p_command ->> 'expected_binding_version')::BIGINT,
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash'
    );
    IF (command_type_value = 'force_mute' AND p_parameters <> '{"muted":true}'::JSONB)
       OR (command_type_value IN ('revoke_device', 'secure_wipe')
           AND p_parameters <> '{}'::JSONB)
       OR (command_type_value = 'collect_diagnostics'
           AND p_parameters <> '{"scope":"minimal"}'::JSONB)
       OR (command_type_value = 'assign_ota' AND (
            NOT p_parameters ?& ARRAY[
                'assignment_id', 'artifact_url', 'artifact_sha256',
                'artifact_size_bytes'
            ]
            OR p_parameters - ARRAY[
                'assignment_id', 'artifact_url', 'artifact_sha256',
                'artifact_size_bytes'
            ] <> '{}'::JSONB
       )) THEN
        RAISE EXCEPTION 'remote command parameters rejected';
    END IF;
    SELECT * INTO device_row FROM device_fleet_devices
    WHERE device_id = p_command ->> 'device_id'
      AND binding_id = p_command ->> 'expected_binding_id'
      AND binding_version = (p_command ->> 'expected_binding_version')::BIGINT
      AND family_space_id = NULLIF(
          current_setting('app.device_fleet.family_space_id', true), ''
      )
    FOR UPDATE;
    IF NOT FOUND
       OR device_row.lifecycle_status <> 'bound'
       OR device_row.current_certificate_id IS DISTINCT FROM
          p_command ->> 'target_certificate_id'
       OR device_row.last_attestation_counter IS DISTINCT FROM
          (p_command ->> 'expected_monotonic_counter')::BIGINT
       OR device_row.firmware_security_version IS DISTINCT FROM
          (p_command ->> 'expected_firmware_security_version')::INTEGER THEN
        RAISE EXCEPTION 'remote command current fence mismatch';
    END IF;
    SELECT * INTO existing_row FROM device_fleet_remote_commands
    WHERE device_id = p_command ->> 'device_id'
      AND idempotency_key = p_command ->> 'idempotency_key'
    FOR SHARE;
    IF FOUND THEN
        IF existing_row.payload <> p_command
           OR existing_row.parameters <> p_parameters
           OR existing_row.authority_receipt_id IS DISTINCT FROM
              p_authority ->> 'authority_receipt_id' THEN
            RAISE EXCEPTION 'remote command idempotency conflict';
        END IF;
        RETURN existing_row.payload;
    END IF;
    SELECT GREATEST(
        COALESCE(MAX(command_sequence), 0), device_row.last_command_sequence
    ) + 1 INTO expected_sequence
    FROM device_fleet_remote_commands
    WHERE device_id = p_command ->> 'device_id';
    IF (p_command ->> 'command_sequence')::BIGINT <> expected_sequence THEN
        RAISE EXCEPTION 'remote command sequence conflict';
    END IF;
    INSERT INTO device_fleet_remote_commands (
        command_id, idempotency_key, device_id, family_space_id, binding_id,
        binding_version, command_sequence, command_type, actor_id,
        reason_code, target_certificate_id, expected_monotonic_counter,
        expected_firmware_security_version, parameters, parameters_hash,
        payload, issued_at, expires_at, binding_role,
        authority_receipt_id, action_resource_id, action_resource_hash,
        action_fence_hash
    ) VALUES (
        p_command ->> 'command_id', p_command ->> 'idempotency_key',
        p_command ->> 'device_id',
        current_setting('app.device_fleet.family_space_id', false),
        p_command ->> 'expected_binding_id',
        (p_command ->> 'expected_binding_version')::BIGINT,
        (p_command ->> 'command_sequence')::BIGINT, command_type_value,
        p_command ->> 'actor_id', p_command ->> 'reason_code',
        p_command ->> 'target_certificate_id',
        (p_command ->> 'expected_monotonic_counter')::BIGINT,
        (p_command ->> 'expected_firmware_security_version')::INTEGER,
        p_parameters, p_command ->> 'parameters_hash', p_command,
        (p_command ->> 'issued_at')::TIMESTAMPTZ,
        (p_command ->> 'expires_at')::TIMESTAMPTZ,
        p_authority ->> 'binding_role',
        p_authority ->> 'authority_receipt_id',
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash'
    );
    RETURN p_command;
END
$device_fleet_action_commit_command$;
ALTER FUNCTION device_fleet_action_commit_command(JSONB, JSONB, JSONB)
    OWNER TO memoria_device_fleet_maintenance;
REVOKE ALL ON FUNCTION device_fleet_action_commit_command(JSONB, JSONB, JSONB)
    FROM PUBLIC;

CREATE OR REPLACE FUNCTION device_fleet_action_commit_ota(
    p_record JSONB,
    p_authority JSONB
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $device_fleet_action_commit_ota$
DECLARE
    assignment JSONB := p_record -> 'assignment';
    device_row device_fleet_devices%ROWTYPE;
    existing_row device_fleet_ota_assignments%ROWTYPE;
    expected_slot TEXT;
BEGIN
    PERFORM device_fleet_assert_action_authority(
        p_authority ->> 'authority_receipt_id',
        p_authority ->> 'actor_id',
        assignment ->> 'device_id',
        p_record ->> 'binding_id',
        (p_record ->> 'binding_version')::BIGINT,
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash'
    );
    SELECT * INTO device_row FROM device_fleet_devices
    WHERE device_id = assignment ->> 'device_id'
      AND binding_id = p_record ->> 'binding_id'
      AND binding_version = (p_record ->> 'binding_version')::BIGINT
      AND family_space_id = NULLIF(
          current_setting('app.device_fleet.family_space_id', true), ''
      )
    FOR UPDATE;
    IF NOT FOUND
       OR device_row.lifecycle_status <> 'bound'
       OR device_row.current_certificate_id IS DISTINCT FROM
          p_record ->> 'target_certificate_id'
       OR device_row.last_attestation_counter IS DISTINCT FROM
          (p_record ->> 'expected_monotonic_counter')::BIGINT
       OR device_row.firmware_security_version IS DISTINCT FROM
          (p_record ->> 'expected_firmware_security_version')::INTEGER THEN
        RAISE EXCEPTION 'OTA current fence mismatch';
    END IF;
    expected_slot := CASE WHEN device_row.active_ota_slot = 'a' THEN 'b' ELSE 'a' END;
    IF assignment ->> 'target_slot' <> expected_slot
       OR assignment ->> 'status' <> 'pending'
       OR p_record ->> 'artifact_url' !~ '^https://'
       OR (assignment ->> 'target_firmware_security_version')::INTEGER
          <= GREATEST(
              device_row.firmware_security_version,
              device_row.anti_rollback_floor_security_version
          ) THEN
        RAISE EXCEPTION 'OTA assignment rejected';
    END IF;
    SELECT * INTO existing_row FROM device_fleet_ota_assignments
    WHERE assignment_id = assignment ->> 'assignment_id'
    FOR SHARE;
    IF FOUND THEN
        IF existing_row.payload <> assignment
           OR existing_row.artifact_url <> p_record ->> 'artifact_url'
           OR existing_row.actor_id <> p_authority ->> 'actor_id' THEN
            RAISE EXCEPTION 'OTA assignment id conflict';
        END IF;
        RETURN existing_row.payload;
    END IF;
    INSERT INTO device_fleet_ota_assignments (
        assignment_id, device_id, family_space_id, binding_id,
        binding_version, artifact_url, target_slot, target_version,
        target_firmware_security_version, artifact_sha256,
        artifact_size_bytes, min_bootloader_version,
        anti_rollback_floor_version,
        anti_rollback_floor_security_version, channel, status, payload,
        actor_id, binding_role, authority_receipt_id, action_resource_id,
        action_resource_hash, action_fence_hash, action_parameters_hash,
        target_certificate_id, expected_monotonic_counter,
        expected_firmware_security_version, assigned_at, expires_at,
        updated_at
    ) VALUES (
        assignment ->> 'assignment_id', assignment ->> 'device_id',
        current_setting('app.device_fleet.family_space_id', false),
        p_record ->> 'binding_id',
        (p_record ->> 'binding_version')::BIGINT,
        p_record ->> 'artifact_url', assignment ->> 'target_slot',
        assignment ->> 'target_version',
        (assignment ->> 'target_firmware_security_version')::INTEGER,
        assignment ->> 'artifact_sha256',
        (assignment ->> 'artifact_size_bytes')::BIGINT,
        assignment ->> 'min_bootloader_version',
        assignment ->> 'anti_rollback_floor_version',
        (assignment ->> 'anti_rollback_floor_security_version')::INTEGER,
        assignment ->> 'channel', 'pending', assignment,
        p_authority ->> 'actor_id', p_authority ->> 'binding_role',
        p_authority ->> 'authority_receipt_id',
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash',
        p_record ->> 'action_parameters_hash',
        p_record ->> 'target_certificate_id',
        (p_record ->> 'expected_monotonic_counter')::BIGINT,
        (p_record ->> 'expected_firmware_security_version')::INTEGER,
        (assignment ->> 'assigned_at')::TIMESTAMPTZ,
        (assignment ->> 'expires_at')::TIMESTAMPTZ,
        (assignment ->> 'assigned_at')::TIMESTAMPTZ
    );
    RETURN assignment;
END
$device_fleet_action_commit_ota$;
ALTER FUNCTION device_fleet_action_commit_ota(JSONB, JSONB)
    OWNER TO memoria_device_fleet_maintenance;
REVOKE ALL ON FUNCTION device_fleet_action_commit_ota(JSONB, JSONB)
    FROM PUBLIC;

CREATE OR REPLACE FUNCTION device_fleet_action_transfer_binding(
    p_record JSONB,
    p_authority JSONB
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $device_fleet_action_transfer_binding$
DECLARE
    device_row device_fleet_devices%ROWTYPE;
    old_certificate_id TEXT;
BEGIN
    PERFORM device_fleet_assert_action_authority(
        p_authority ->> 'authority_receipt_id',
        p_authority ->> 'actor_id',
        p_record ->> 'device_id',
        p_record ->> 'expected_binding_id',
        (p_record ->> 'expected_binding_version')::BIGINT,
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash'
    );
    SELECT * INTO device_row FROM device_fleet_devices
    WHERE device_id = p_record ->> 'device_id'
      AND family_space_id = p_record ->> 'expected_family_space_id'
      AND binding_id = p_record ->> 'expected_binding_id'
      AND binding_version = (p_record ->> 'expected_binding_version')::BIGINT
    FOR UPDATE;
    IF NOT FOUND
       OR device_row.lifecycle_status <> 'bound'
       OR device_row.binding_version_floor IS DISTINCT FROM
          (p_record ->> 'expected_binding_version')::BIGINT
       OR (p_record ->> 'new_binding_version')::BIGINT <>
          (p_record ->> 'expected_binding_version')::BIGINT + 1
       OR device_row.current_certificate_id IS DISTINCT FROM
          p_record ->> 'target_certificate_id'
       OR device_row.last_attestation_counter IS DISTINCT FROM
          (p_record ->> 'expected_monotonic_counter')::BIGINT
       OR device_row.firmware_security_version IS DISTINCT FROM
          (p_record ->> 'expected_firmware_security_version')::INTEGER THEN
        RAISE EXCEPTION 'binding version conflict';
    END IF;
    old_certificate_id := device_row.current_certificate_id;
    UPDATE device_fleet_certificates SET status = 'revoked',
        revoked_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ,
        revocation_reason_code = 'binding_transferred'
    WHERE certificate_id = old_certificate_id AND status = 'active';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'binding current certificate conflict';
    END IF;
    UPDATE device_fleet_attestation_challenges
    SET consumed_at = COALESCE(
        consumed_at, (p_record ->> 'occurred_at')::TIMESTAMPTZ
    ) WHERE device_id = p_record ->> 'device_id';
    UPDATE device_fleet_remote_commands SET status = 'failed',
        failed_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ,
        last_error_code = 'binding_changed'
    WHERE device_id = p_record ->> 'device_id'
      AND status IN ('pending', 'accepted', 'dispatched', 'uncertain');
    UPDATE device_fleet_command_outbox SET status = 'failed',
        lease_token = NULL, lease_owner = NULL, lease_until = NULL,
        last_error_code = 'binding_changed',
        updated_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ
    WHERE device_id = p_record ->> 'device_id'
      AND status IN ('ready', 'leased', 'dispatched', 'uncertain');
    UPDATE device_fleet_ota_assignments SET status = 'superseded',
        updated_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ
    WHERE device_id = p_record ->> 'device_id'
      AND status IN ('pending', 'downloading', 'staged', 'activated');
    INSERT INTO device_fleet_certificates (
        certificate_id, device_id, family_space_id, binding_id,
        binding_version, public_key_b64, key_algorithm, status,
        valid_from, valid_until, created_at
    ) VALUES (
        p_record ->> 'new_certificate_id', p_record ->> 'device_id',
        p_record ->> 'new_family_space_id', p_record ->> 'new_binding_id',
        (p_record ->> 'new_binding_version')::BIGINT,
        p_record ->> 'new_public_key_b64', 'ed25519', 'active',
        (p_record ->> 'occurred_at')::TIMESTAMPTZ,
        (p_record ->> 'certificate_valid_until')::TIMESTAMPTZ,
        (p_record ->> 'occurred_at')::TIMESTAMPTZ
    );
    UPDATE device_fleet_sim_profiles SET
        family_space_id = p_record ->> 'new_family_space_id',
        binding_id = p_record ->> 'new_binding_id',
        binding_version = (p_record ->> 'new_binding_version')::BIGINT,
        updated_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ
    WHERE sim_id = device_row.current_sim_id;
    UPDATE device_fleet_devices SET
        family_space_id = p_record ->> 'new_family_space_id',
        binding_id = p_record ->> 'new_binding_id',
        binding_version = (p_record ->> 'new_binding_version')::BIGINT,
        binding_version_floor = (p_record ->> 'new_binding_version')::BIGINT,
        current_certificate_id = p_record ->> 'new_certificate_id',
        lifecycle_status = 'bound', lifecycle_reason_code = 'transferred',
        physical_mute_state = 'unknown', privacy_light_state = 'unknown',
        attested_sim_status = 'unknown',
        state_version = state_version + 1,
        updated_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ
    WHERE device_id = p_record ->> 'device_id';
    INSERT INTO device_fleet_binding_events (
        event_id, device_id, family_space_id, binding_id, binding_version,
        event_type, previous_family_space_id, previous_binding_id,
        previous_binding_version, actor_id, authority_receipt_id,
        action_resource_id, action_resource_hash, action_fence_hash,
        occurred_at
    ) VALUES (
        p_record ->> 'event_id', p_record ->> 'device_id',
        p_record ->> 'new_family_space_id', p_record ->> 'new_binding_id',
        (p_record ->> 'new_binding_version')::BIGINT, 'transferred',
        p_record ->> 'expected_family_space_id',
        p_record ->> 'expected_binding_id',
        (p_record ->> 'expected_binding_version')::BIGINT,
        p_authority ->> 'actor_id', p_authority ->> 'authority_receipt_id',
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash',
        (p_record ->> 'occurred_at')::TIMESTAMPTZ
    );
    RETURN jsonb_build_object(
        'device_id', p_record ->> 'device_id',
        'family_space_id', p_record ->> 'new_family_space_id',
        'binding_id', p_record ->> 'new_binding_id',
        'binding_version', (p_record ->> 'new_binding_version')::BIGINT
    );
END
$device_fleet_action_transfer_binding$;
ALTER FUNCTION device_fleet_action_transfer_binding(JSONB, JSONB)
    OWNER TO memoria_device_fleet_maintenance;
REVOKE ALL ON FUNCTION device_fleet_action_transfer_binding(JSONB, JSONB)
    FROM PUBLIC;

CREATE OR REPLACE FUNCTION device_fleet_action_transition_sim(
    p_record JSONB,
    p_authority JSONB
) RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
SET row_security = on
AS $device_fleet_action_transition_sim$
DECLARE
    device_row device_fleet_devices%ROWTYPE;
    sim_row device_fleet_sim_profiles%ROWTYPE;
    next_revision BIGINT;
    result_sim_id TEXT;
    result_provider TEXT;
    result_kind TEXT;
    result_provider_status TEXT;
    result_status TEXT;
    result_iccid TEXT;
    result_esim_profile_id TEXT;
BEGIN
    PERFORM device_fleet_assert_action_authority(
        p_authority ->> 'authority_receipt_id',
        p_authority ->> 'actor_id',
        p_record ->> 'device_id',
        p_record ->> 'binding_id',
        (p_record ->> 'binding_version')::BIGINT,
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash'
    );
    SELECT * INTO device_row FROM device_fleet_devices
    WHERE device_id = p_record ->> 'device_id'
      AND family_space_id = p_record ->> 'family_space_id'
      AND binding_id = p_record ->> 'binding_id'
      AND binding_version = (p_record ->> 'binding_version')::BIGINT
    FOR UPDATE;
    SELECT * INTO sim_row FROM device_fleet_sim_profiles
    WHERE sim_id = p_record ->> 'expected_sim_id'
    FOR UPDATE;
    IF device_row.device_id IS NULL
       OR sim_row.sim_id IS NULL
       OR device_row.current_sim_id IS DISTINCT FROM sim_row.sim_id
       OR device_row.sim_authority_revision IS DISTINCT FROM
          (p_record ->> 'expected_revision')::BIGINT
       OR sim_row.authority_revision IS DISTINCT FROM
          (p_record ->> 'expected_revision')::BIGINT
       OR device_row.current_certificate_id IS DISTINCT FROM
          p_record ->> 'target_certificate_id'
       OR device_row.last_attestation_counter IS DISTINCT FROM
          (p_record ->> 'expected_monotonic_counter')::BIGINT
       OR device_row.firmware_security_version IS DISTINCT FROM
          (p_record ->> 'expected_firmware_security_version')::INTEGER THEN
        RAISE EXCEPTION 'SIM revision conflict';
    END IF;
    next_revision := sim_row.authority_revision + 1;
    IF p_record ->> 'action' = 'replace' THEN
        IF p_record ->> 'replacement_sim_id' IS NULL
           OR p_record ->> 'replacement_provider' IS NULL
           OR p_record ->> 'replacement_profile_kind' NOT IN ('physical', 'esim')
        THEN
            RAISE EXCEPTION 'replacement SIM profile required';
        END IF;
        result_sim_id := p_record ->> 'replacement_sim_id';
        result_provider := p_record ->> 'replacement_provider';
        result_kind := p_record ->> 'replacement_profile_kind';
        result_provider_status := 'active';
        result_status := 'active';
        result_iccid := CASE WHEN result_kind = 'physical' THEN result_sim_id END;
        result_esim_profile_id := CASE WHEN result_kind = 'esim' THEN result_sim_id END;
        INSERT INTO device_fleet_sim_profiles (
            sim_id, device_id, family_space_id, binding_id, binding_version,
            iccid, esim_profile_id, provider, profile_kind, provider_status,
            server_status, authority_revision, created_at, updated_at
        ) VALUES (
            result_sim_id, p_record ->> 'device_id',
            p_record ->> 'family_space_id', p_record ->> 'binding_id',
            (p_record ->> 'binding_version')::BIGINT,
            result_iccid, result_esim_profile_id, result_provider,
            result_kind, 'active', 'active', next_revision,
            (p_record ->> 'occurred_at')::TIMESTAMPTZ,
            (p_record ->> 'occurred_at')::TIMESTAMPTZ
        );
        UPDATE device_fleet_sim_profiles SET server_status = 'replaced',
            replaced_by_sim_id = result_sim_id,
            updated_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ
        WHERE sim_id = sim_row.sim_id;
    ELSE
        result_status := CASE p_record ->> 'action'
            WHEN 'suspend' THEN 'suspended'
            WHEN 'revoke' THEN 'revoked'
            WHEN 'expire' THEN 'expired'
            ELSE NULL
        END;
        IF result_status IS NULL THEN
            RAISE EXCEPTION 'unsupported SIM lifecycle action';
        END IF;
        result_sim_id := sim_row.sim_id;
        result_provider := sim_row.provider;
        result_kind := sim_row.profile_kind;
        result_provider_status := result_status;
        result_iccid := sim_row.iccid;
        result_esim_profile_id := sim_row.esim_profile_id;
        UPDATE device_fleet_sim_profiles SET provider_status = result_status,
            server_status = result_status, authority_revision = next_revision,
            updated_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ
        WHERE sim_id = sim_row.sim_id;
    END IF;
    UPDATE device_fleet_devices SET current_sim_id = result_sim_id,
        sim_authority_revision = next_revision,
        state_version = state_version + 1,
        updated_at = (p_record ->> 'occurred_at')::TIMESTAMPTZ
    WHERE device_id = p_record ->> 'device_id';
    INSERT INTO device_fleet_sim_events (
        event_id, device_id, family_space_id, binding_id, binding_version,
        sim_id, authority_revision, action, previous_server_status,
        server_status, provider_status, actor_id, authority_receipt_id,
        action_resource_id, action_resource_hash, action_fence_hash,
        occurred_at
    ) VALUES (
        p_record ->> 'event_id', p_record ->> 'device_id',
        p_record ->> 'family_space_id', p_record ->> 'binding_id',
        (p_record ->> 'binding_version')::BIGINT, result_sim_id,
        next_revision, p_record ->> 'action', sim_row.server_status,
        result_status, result_provider_status, p_authority ->> 'actor_id',
        p_authority ->> 'authority_receipt_id',
        p_authority ->> 'action_resource_id',
        p_authority ->> 'action_resource_hash',
        p_authority ->> 'action_fence_hash',
        (p_record ->> 'occurred_at')::TIMESTAMPTZ
    );
    RETURN jsonb_build_object(
        'sim_id', result_sim_id,
        'provider', result_provider,
        'profile_kind', result_kind,
        'provider_status', result_provider_status,
        'status', result_status,
        'revision', next_revision,
        'iccid', result_iccid,
        'esim_profile_id', result_esim_profile_id
    );
END
$device_fleet_action_transition_sim$;
ALTER FUNCTION device_fleet_action_transition_sim(JSONB, JSONB)
    OWNER TO memoria_device_fleet_maintenance;
REVOKE ALL ON FUNCTION device_fleet_action_transition_sim(JSONB, JSONB)
    FROM PUBLIC;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON device_fleet_devices, device_fleet_certificates,
    device_fleet_attestation_challenges, device_fleet_attestations,
    device_fleet_remote_commands, device_fleet_remote_command_executions,
    device_fleet_command_outbox, device_fleet_dispatcher_heartbeats,
    device_fleet_binding_events, device_fleet_sim_profiles,
    device_fleet_sim_events, device_fleet_ota_assignments,
    device_fleet_ota_receipts FROM memoria_action_executor;
GRANT SELECT ON device_fleet_devices, device_fleet_certificates,
    device_fleet_attestation_challenges, device_fleet_attestations,
    device_fleet_remote_commands, device_fleet_remote_command_executions,
    device_fleet_command_outbox, device_fleet_dispatcher_heartbeats,
    device_fleet_binding_events, device_fleet_sim_profiles, device_fleet_sim_events,
    device_fleet_ota_assignments, device_fleet_ota_receipts
    TO memoria_device_fleet_api;
GRANT INSERT, UPDATE ON device_fleet_devices, device_fleet_attestation_challenges,
    device_fleet_attestations TO memoria_device_fleet_api;
GRANT INSERT ON device_fleet_remote_commands TO memoria_device_fleet_api;
GRANT UPDATE ON device_fleet_certificates, device_fleet_remote_commands,
    device_fleet_command_outbox TO memoria_device_fleet_api;
GRANT INSERT ON device_fleet_ota_assignments TO memoria_device_fleet_api;
GRANT SELECT ON device_fleet_devices, device_fleet_certificates,
    device_fleet_attestations, device_fleet_remote_commands,
    device_fleet_remote_command_executions, device_fleet_ota_assignments,
    device_fleet_ota_receipts, device_fleet_command_outbox,
    device_fleet_dispatcher_heartbeats, device_fleet_binding_events,
    device_fleet_sim_profiles, device_fleet_sim_events
    TO memoria_device_fleet_projector;
GRANT SELECT ON device_fleet_devices, device_fleet_certificates,
    device_fleet_attestations, device_fleet_remote_commands,
    device_fleet_remote_command_executions, device_fleet_ota_assignments,
    device_fleet_ota_receipts, device_fleet_command_outbox,
    device_fleet_dispatcher_heartbeats, device_fleet_binding_events,
    device_fleet_sim_profiles, device_fleet_sim_events
    TO memoria_device_fleet_worker;
GRANT UPDATE ON device_fleet_devices, device_fleet_certificates,
    device_fleet_remote_commands
    TO memoria_device_fleet_worker;
GRANT INSERT ON device_fleet_remote_command_executions
    TO memoria_device_fleet_worker;
GRANT INSERT, UPDATE ON device_fleet_command_outbox,
    device_fleet_dispatcher_heartbeats TO memoria_device_fleet_worker;
GRANT UPDATE ON device_fleet_ota_assignments TO memoria_device_fleet_worker;
GRANT INSERT ON device_fleet_ota_receipts TO memoria_device_fleet_worker;
GRANT ALL ON device_fleet_devices, device_fleet_certificates,
    device_fleet_attestation_challenges, device_fleet_attestations,
    device_fleet_remote_commands, device_fleet_remote_command_executions,
    device_fleet_command_outbox, device_fleet_dispatcher_heartbeats,
    device_fleet_binding_events, device_fleet_sim_profiles, device_fleet_sim_events,
    device_fleet_ota_assignments, device_fleet_ota_receipts
    TO memoria_device_fleet_maintenance;
GRANT EXECUTE ON FUNCTION device_fleet_scope_matches(TEXT, TEXT, BIGINT, TEXT)
    TO memoria_device_fleet_api, memoria_device_fleet_projector,
       memoria_device_fleet_worker, memoria_device_fleet_maintenance;
GRANT EXECUTE ON FUNCTION device_fleet_lock_trust_heads(TEXT, TEXT, BIGINT, TEXT)
    TO memoria_device_fleet_api, memoria_device_fleet_projector,
       memoria_device_fleet_worker, memoria_action_executor;
GRANT EXECUTE ON FUNCTION device_fleet_action_lock_snapshot(
    TEXT, TEXT, BIGINT, TEXT, TEXT, TEXT
) TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION device_fleet_action_commit_command(JSONB, JSONB, JSONB)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION device_fleet_action_commit_ota(JSONB, JSONB)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION device_fleet_action_transfer_binding(JSONB, JSONB)
    TO memoria_action_executor;
GRANT EXECUTE ON FUNCTION device_fleet_action_transition_sim(JSONB, JSONB)
    TO memoria_action_executor;
