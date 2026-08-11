-- Memoria Device Onboarding production authority.
--
-- This is a forward-only, onboarding-specific schema.  It deliberately does
-- not reuse the older device_fleet_* tables: onboarding owns the manufactured
-- device identity until the Identity binding authority has committed it.
-- Runtime API connections use memoria_device_onboarding_api, which is a
-- NOSUPERUSER/NOBYPASSRLS role.  The maintenance role is only for controlled
-- migrations and manufacturing registration.

DO $roles$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_device_onboarding_api'
    ) THEN
        CREATE ROLE memoria_device_onboarding_api LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_device_onboarding_api LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'memoria_device_onboarding_maintenance'
    ) THEN
        CREATE ROLE memoria_device_onboarding_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    ELSE
        ALTER ROLE memoria_device_onboarding_maintenance LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$roles$;

CREATE TABLE IF NOT EXISTS device_onboarding_devices (
    device_id TEXT PRIMARY KEY CHECK (char_length(device_id) BETWEEN 1 AND 128),
    certificate_id TEXT NOT NULL UNIQUE CHECK (char_length(certificate_id) BETWEEN 1 AND 128),
    public_key_b64 TEXT NOT NULL CHECK (char_length(public_key_b64) = 43),
    product_model TEXT NOT NULL CHECK (char_length(product_model) BETWEEN 1 AND 128),
    hardware_revision TEXT NOT NULL CHECK (char_length(hardware_revision) BETWEEN 1 AND 128),
    firmware_version TEXT NOT NULL CHECK (char_length(firmware_version) BETWEEN 1 AND 128),
    firmware_security_version INTEGER NOT NULL CHECK (firmware_security_version >= 0),
    capability_manifest_hash TEXT NOT NULL CHECK (capability_manifest_hash ~ '^[a-f0-9]{64}$'),
    minimum_firmware_security_version INTEGER NOT NULL
        CHECK (minimum_firmware_security_version >= 0),
    lifecycle_status TEXT NOT NULL CHECK (
        lifecycle_status IN ('manufactured', 'provisioned', 'bound', 'revoked')
    ),
    last_monotonic_counter BIGINT NOT NULL DEFAULT 0
        CHECK (last_monotonic_counter >= 0),
    binding_id TEXT CHECK (binding_id IS NULL OR char_length(binding_id) BETWEEN 1 AND 128),
    binding_version BIGINT CHECK (binding_version IS NULL OR binding_version >= 1),
    actor_id TEXT CHECK (actor_id IS NULL OR char_length(actor_id) BETWEEN 1 AND 128),
    activation_version BIGINT NOT NULL DEFAULT 0 CHECK (activation_version >= 0),
    last_activation_counter BIGINT NOT NULL DEFAULT 0
        CHECK (last_activation_counter >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK ((lifecycle_status = 'bound') = (
        binding_id IS NOT NULL AND binding_version IS NOT NULL AND actor_id IS NOT NULL
    ))
);

CREATE TABLE IF NOT EXISTS device_onboarding_sessions (
    onboarding_session_id TEXT PRIMARY KEY CHECK (
        char_length(onboarding_session_id) BETWEEN 1 AND 128
    ),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    client_onboarding_id TEXT NOT NULL CHECK (
        char_length(client_onboarding_id) BETWEEN 1 AND 128
    ),
    qr_nonce_hash TEXT NOT NULL CHECK (qr_nonce_hash ~ '^[a-f0-9]{64}$'),
    pop_hash TEXT NOT NULL CHECK (pop_hash ~ '^[a-f0-9]{64}$'),
    mobile_nonce_hash TEXT NOT NULL CHECK (mobile_nonce_hash ~ '^[a-f0-9]{64}$'),
    protocol_version INTEGER NOT NULL CHECK (protocol_version >= 1),
    ble_name TEXT NOT NULL CHECK (char_length(ble_name) BETWEEN 1 AND 128),
    ble_service_uuid TEXT NOT NULL CHECK (char_length(ble_service_uuid) BETWEEN 1 AND 128),
    state TEXT NOT NULL CHECK (char_length(state) BETWEEN 1 AND 64),
    state_version BIGINT NOT NULL DEFAULT 1 CHECK (state_version >= 1),
    first_seen_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    proximity_verified_at TIMESTAMPTZ,
    wifi_connected_at TIMESTAMPTZ,
    device_online_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    failure_code TEXT,
    UNIQUE (actor_id, client_onboarding_id),
    UNIQUE (device_id, qr_nonce_hash),
    CHECK (first_seen_at < expires_at)
);

CREATE TABLE IF NOT EXISTS device_onboarding_events (
    event_id TEXT PRIMARY KEY CHECK (char_length(event_id) BETWEEN 1 AND 160),
    onboarding_session_id TEXT NOT NULL
        REFERENCES device_onboarding_sessions(onboarding_session_id),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    event_type TEXT NOT NULL CHECK (char_length(event_type) BETWEEN 1 AND 128),
    previous_state TEXT,
    next_state TEXT,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('user', 'device', 'system')),
    actor_id TEXT,
    reason_code TEXT,
    payload_redacted JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(payload_redacted) = 'object'),
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS device_onboarding_challenges (
    challenge_id TEXT PRIMARY KEY CHECK (char_length(challenge_id) BETWEEN 1 AND 128),
    onboarding_session_id TEXT NOT NULL
        REFERENCES device_onboarding_sessions(onboarding_session_id),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    nonce_hash TEXT NOT NULL CHECK (nonce_hash ~ '^[a-f0-9]{64}$'),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    CHECK (issued_at < expires_at)
);

CREATE TABLE IF NOT EXISTS device_media_challenges (
    challenge_id TEXT PRIMARY KEY CHECK (char_length(challenge_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    certificate_id TEXT NOT NULL,
    client_id TEXT NOT NULL CHECK (char_length(client_id) BETWEEN 1 AND 128),
    nonce_hash TEXT NOT NULL CHECK (nonce_hash ~ '^[a-f0-9]{64}$'),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    CHECK (issued_at < expires_at)
);

CREATE TABLE IF NOT EXISTS device_onboarding_claims (
    claim_id TEXT PRIMARY KEY CHECK (char_length(claim_id) BETWEEN 1 AND 128),
    onboarding_session_id TEXT NOT NULL UNIQUE
        REFERENCES device_onboarding_sessions(onboarding_session_id),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    status TEXT NOT NULL CHECK (status IN (
        'reserved', 'binding_committing', 'binding_created', 'committed',
        'released', 'expired', 'conflict'
    )),
    idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
    reserved_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    binding_id TEXT,
    binding_version BIGINT CHECK (binding_version IS NULL OR binding_version >= 1),
    committed_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    failure_code TEXT,
    CHECK (reserved_at < expires_at)
);

CREATE TABLE IF NOT EXISTS device_onboarding_bindings (
    binding_id TEXT PRIMARY KEY CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    claim_id TEXT NOT NULL UNIQUE REFERENCES device_onboarding_claims(claim_id),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    status TEXT NOT NULL CHECK (status IN ('draft', 'committed', 'released')),
    initialization_json JSONB NOT NULL CHECK (jsonb_typeof(initialization_json) = 'object'),
    idempotency_key TEXT NOT NULL CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
    created_at TIMESTAMPTZ NOT NULL,
    committed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS device_onboarding_activations (
    activation_id TEXT PRIMARY KEY CHECK (char_length(activation_id) BETWEEN 1 AND 128),
    device_id TEXT NOT NULL REFERENCES device_onboarding_devices(device_id),
    actor_id TEXT NOT NULL CHECK (char_length(actor_id) BETWEEN 1 AND 128),
    claim_id TEXT NOT NULL REFERENCES device_onboarding_claims(claim_id),
    binding_id TEXT NOT NULL REFERENCES device_onboarding_bindings(binding_id),
    binding_version BIGINT NOT NULL CHECK (binding_version >= 1),
    activation_version BIGINT NOT NULL CHECK (activation_version >= 1),
    manifest_json JSONB NOT NULL CHECK (jsonb_typeof(manifest_json) = 'object'),
    manifest_hash TEXT NOT NULL CHECK (manifest_hash ~ '^[a-f0-9]{64}$'),
    status TEXT NOT NULL CHECK (status IN (
        'pending_manifest', 'manifest_ready', 'device_downloading',
        'device_applied', 'device_acknowledged', 'ready_for_conversation', 'failed'
    )),
    issued_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    downloaded_at TIMESTAMPTZ,
    applied_at TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    ack_counter BIGINT CHECK (ack_counter IS NULL OR ack_counter >= 1),
    ack_json JSONB CHECK (ack_json IS NULL OR jsonb_typeof(ack_json) = 'object'),
    UNIQUE (device_id, activation_version),
    CHECK (issued_at < expires_at)
);

CREATE INDEX IF NOT EXISTS device_onboarding_claims_device_active
    ON device_onboarding_claims(device_id, status, expires_at);
CREATE INDEX IF NOT EXISTS device_onboarding_activations_device_latest
    ON device_onboarding_activations(device_id, activation_version DESC);
CREATE INDEX IF NOT EXISTS device_media_challenges_device_outstanding
    ON device_media_challenges(device_id, expires_at, used_at);
CREATE INDEX IF NOT EXISTS device_onboarding_events_session_time
    ON device_onboarding_events(onboarding_session_id, occurred_at);

-- The lookup context is deliberately narrower than a general service bypass:
-- each adapter operation sets actor/device or one exact opaque lookup key in
-- the same transaction.  A raw connection with no context sees no rows.
CREATE OR REPLACE FUNCTION device_onboarding_scope_actor(p_actor_id TEXT)
RETURNS BOOLEAN
LANGUAGE SQL STABLE
AS $scope_actor$
    SELECT NULLIF(current_setting('app.device_onboarding.actor_id', true), '')
           IS NOT NULL
       AND p_actor_id IS NOT NULL
       AND p_actor_id = NULLIF(
           current_setting('app.device_onboarding.actor_id', true), ''
       )
$scope_actor$;

CREATE OR REPLACE FUNCTION device_onboarding_scope_device(p_device_id TEXT)
RETURNS BOOLEAN
LANGUAGE SQL STABLE
AS $scope_device$
    SELECT NULLIF(current_setting('app.device_onboarding.device_id', true), '')
           IS NOT NULL
       AND p_device_id = NULLIF(
           current_setting('app.device_onboarding.device_id', true), ''
       )
$scope_device$;

CREATE OR REPLACE FUNCTION device_onboarding_scope_lookup(
    p_kind TEXT,
    p_id TEXT
)
RETURNS BOOLEAN
LANGUAGE SQL STABLE
AS $scope_lookup$
    SELECT NULLIF(current_setting('app.device_onboarding.lookup_kind', true), '')
           = p_kind
       AND NULLIF(current_setting('app.device_onboarding.lookup_id', true), '')
           = p_id
$scope_lookup$;

CREATE OR REPLACE FUNCTION device_onboarding_scope_maintenance()
RETURNS BOOLEAN
LANGUAGE SQL STABLE
AS $scope_maintenance$
    SELECT current_user = 'memoria_device_onboarding_maintenance'
$scope_maintenance$;

ALTER TABLE device_onboarding_devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_media_challenges ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_activations ENABLE ROW LEVEL SECURITY;

ALTER TABLE device_onboarding_devices FORCE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_events FORCE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_challenges FORCE ROW LEVEL SECURITY;
ALTER TABLE device_media_challenges FORCE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_claims FORCE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_bindings FORCE ROW LEVEL SECURITY;
ALTER TABLE device_onboarding_activations FORCE ROW LEVEL SECURITY;

DO $policies$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_onboarding_devices'
          AND policyname = 'device_onboarding_devices_scope'
    ) THEN
        CREATE POLICY device_onboarding_devices_scope
        ON device_onboarding_devices FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
            OR device_onboarding_scope_lookup('device', device_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_onboarding_sessions'
          AND policyname = 'device_onboarding_sessions_scope'
    ) THEN
        CREATE POLICY device_onboarding_sessions_scope
        ON device_onboarding_sessions FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
            OR device_onboarding_scope_lookup('session', onboarding_session_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_onboarding_events'
          AND policyname = 'device_onboarding_events_scope'
    ) THEN
        CREATE POLICY device_onboarding_events_scope
        ON device_onboarding_events FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
            OR device_onboarding_scope_lookup('event', event_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_onboarding_challenges'
          AND policyname = 'device_onboarding_challenges_scope'
    ) THEN
        CREATE POLICY device_onboarding_challenges_scope
        ON device_onboarding_challenges FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_lookup('challenge', challenge_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_media_challenges'
          AND policyname = 'device_media_challenges_scope'
    ) THEN
        CREATE POLICY device_media_challenges_scope
        ON device_media_challenges FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_lookup('media_challenge', challenge_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_onboarding_claims'
          AND policyname = 'device_onboarding_claims_scope'
    ) THEN
        CREATE POLICY device_onboarding_claims_scope
        ON device_onboarding_claims FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
            OR device_onboarding_scope_lookup('claim', claim_id)
            OR device_onboarding_scope_lookup('session', onboarding_session_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_onboarding_bindings'
          AND policyname = 'device_onboarding_bindings_scope'
    ) THEN
        CREATE POLICY device_onboarding_bindings_scope
        ON device_onboarding_bindings FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
            OR device_onboarding_scope_lookup('binding', binding_id)
            OR device_onboarding_scope_lookup('claim', claim_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'device_onboarding_activations'
          AND policyname = 'device_onboarding_activations_scope'
    ) THEN
        CREATE POLICY device_onboarding_activations_scope
        ON device_onboarding_activations FOR ALL
        USING (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
            OR device_onboarding_scope_lookup('activation', activation_id)
        )
        WITH CHECK (
            device_onboarding_scope_maintenance()
            OR device_onboarding_scope_device(device_id)
            OR device_onboarding_scope_actor(actor_id)
        );
    END IF;
END
$policies$;

REVOKE ALL ON TABLE
    device_onboarding_devices,
    device_onboarding_sessions,
    device_onboarding_events,
    device_onboarding_challenges,
    device_media_challenges,
    device_onboarding_claims,
    device_onboarding_bindings,
    device_onboarding_activations
FROM PUBLIC;

GRANT USAGE ON SCHEMA public
    TO memoria_device_onboarding_api,
       memoria_device_onboarding_maintenance;
GRANT SELECT, INSERT, UPDATE ON TABLE
    device_onboarding_devices,
    device_onboarding_sessions,
    device_onboarding_events,
    device_onboarding_challenges,
    device_media_challenges,
    device_onboarding_claims,
    device_onboarding_bindings,
    device_onboarding_activations
TO memoria_device_onboarding_api;
GRANT SELECT, INSERT, UPDATE ON TABLE
    device_onboarding_devices,
    device_onboarding_sessions,
    device_onboarding_events,
    device_onboarding_challenges,
    device_media_challenges,
    device_onboarding_claims,
    device_onboarding_bindings,
    device_onboarding_activations
TO memoria_device_onboarding_maintenance;

GRANT EXECUTE ON FUNCTION device_onboarding_scope_actor(TEXT),
    device_onboarding_scope_device(TEXT),
    device_onboarding_scope_lookup(TEXT, TEXT),
    device_onboarding_scope_maintenance()
TO memoria_device_onboarding_api,
   memoria_device_onboarding_maintenance;
