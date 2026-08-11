-- Memoria notification state machine (PR-15 / section 11.6 / 11.7).
--
-- Mirrors services/notification/domain.py one-to-one.  Delivery attempts
-- are leased with fencing tokens; cancellation is terminal; relationship
-- snapshots fail closed on revoked/expired/disputed.  Every table is
-- FORCE RLS and core columns are immutable after insert; rows are only
-- ever deleted through the account-deletion pipeline (delete guards).
--
-- RLS is REAL, never an open pass-all policy: every policy evaluates the
-- transaction-level app context set by the adapter
-- (``app.notification.subject_person_id``, ``app.notification.person_id``,
-- ``app.notification.recipient_id``).  Command-level authority comes from
-- REAL database roles (``memoria_notification_api`` vs
-- ``memoria_notification_worker`` via ``pg_has_role`` - a GUC is never a
-- credential, P0-2); the GUCs only carry row context.  With FORCE RLS and
-- no context every comparison is NULL -> false, so reads return nothing
-- and writes are rejected (fail closed).  The adapter refuses to start
-- unless it can set and read this context inside a transaction.
-- API writes additionally require the independent transaction-local
-- ``app.authenticated_actor`` / ``app.authenticated_subject`` context;
-- payload actor/subject columns are never authentication credentials.

CREATE TABLE IF NOT EXISTS notification_intents (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE
        CHECK (char_length(idempotency_key) BETWEEN 1 AND 128),
    intent_kind TEXT NOT NULL CHECK (intent_kind IN (
        'crisis_safety', 'emergency', 'care_alert'
    )),
    subject_person_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        CHECK (char_length(source_event_id) BETWEEN 1 AND 128),
    policy_receipt_id TEXT NOT NULL
        CHECK (char_length(policy_receipt_id) BETWEEN 1 AND 128),
    -- Immutable fence snapshot the intent was issued under (third review):
    -- audit / replay / cancellation can prove the epoch/binding/profile.
    session_id TEXT NOT NULL CHECK (char_length(session_id) BETWEEN 1 AND 128),
    epoch INTEGER NOT NULL CHECK (epoch >= 1),
    binding_id TEXT NOT NULL CHECK (char_length(binding_id) BETWEEN 1 AND 128),
    binding_version INTEGER NOT NULL CHECK (binding_version >= 1),
    runtime_profile_id TEXT NOT NULL
        CHECK (char_length(runtime_profile_id) BETWEEN 1 AND 128),
    actor_person_id TEXT NOT NULL
        CHECK (char_length(actor_person_id) BETWEEN 1 AND 128),
    fence_context_hash TEXT NOT NULL
        CHECK (char_length(fence_context_hash) BETWEEN 1 AND 128),
    -- Full fence snapshot persisted so the worker can re-verify the policy
    -- receipt (and its exact evidence fence) right before an external send
    -- (TOCTOU, red team 12): device + subject revision are part of the
    -- PolicyReceiptV2 identity contract.
    device_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(device_id) BETWEEN 0 AND 128),
    subject_revision INTEGER NOT NULL DEFAULT 0
        CHECK (subject_revision >= 0),
    valid_until TIMESTAMPTZ NOT NULL,
    template_key TEXT NOT NULL CHECK (template_key IN (
        'crisis_safety_notice', 'emergency_notice', 'care_alert_notice'
    )),
    template_params_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(template_params_json) = 'object'),
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'safety_concern', 'emergency_alert', 'care_reminder'
    )),
    script_version TEXT NOT NULL
        CHECK (char_length(script_version) BETWEEN 1 AND 64),
    occurred_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'in_progress', 'delivered', 'dead_lettered', 'cancelled'
    )),
    cancelled_reason TEXT CHECK (cancelled_reason IN (
        'wrong_contact', 'relationship_revoked', 'relationship_expired',
        'relationship_disputed', 'operator_override', 'user_request',
        'authorization_revoked', 'authorization_expired'
    )),
    cancelled_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (status = 'cancelled' AND cancelled_reason IS NOT NULL
            AND cancelled_at IS NOT NULL)
        OR (status <> 'cancelled' AND cancelled_reason IS NULL
            AND cancelled_at IS NULL)
    ),
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL)
        OR (status <> 'delivered' AND delivered_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_notification_intents_subject
ON notification_intents(subject_person_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_intents_status
ON notification_intents(status, created_at);

CREATE TABLE IF NOT EXISTS notification_recipients (
    recipient_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES notification_intents(intent_id)
        ON DELETE RESTRICT,
    person_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN (
        'guardian', 'emergency_contact', 'delegate'
    )),
    relationship_id TEXT NOT NULL,
    relationship_status TEXT NOT NULL CHECK (relationship_status IN (
        'pending', 'active', 'suspended', 'revoked', 'expired', 'disputed'
    )),
    channels_json JSONB NOT NULL CHECK (jsonb_typeof(channels_json) = 'array'),
    channel_index INTEGER NOT NULL DEFAULT 0 CHECK (channel_index >= 0),
    status TEXT NOT NULL CHECK (status IN (
        'pending', 'in_progress', 'delivered', 'failed', 'dead_lettered', 'cancelled'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_retries INTEGER NOT NULL CHECK (max_retries BETWEEN 1 AND 10),
    next_attempt_at TIMESTAMPTZ,
    leased_until TIMESTAMPTZ,
    fencing_token TEXT,
    last_error_code TEXT CHECK (
        last_error_code IS NULL OR char_length(last_error_code) BETWEEN 1 AND 96
    ),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_until TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    delivered_channel TEXT CHECK (delivered_channel IS NULL OR delivered_channel IN (
        'wechat_subscription', 'sms', 'phone_call'
    )),
    cancelled_reason TEXT CHECK (cancelled_reason IN (
        'wrong_contact', 'relationship_revoked', 'relationship_expired',
        'relationship_disputed', 'operator_override', 'user_request',
        'authorization_revoked', 'authorization_expired'
    )),
    -- Authoritative evidence of the relationship snapshot the recipient
    -- was resolved from (P0 TOCTOU): the worker re-verifies these against
    -- the authoritative resolver immediately before an external send.
    relationship_snapshot_id TEXT NOT NULL DEFAULT ''
        CHECK (char_length(relationship_snapshot_id) BETWEEN 0 AND 128),
    relationship_revision INTEGER NOT NULL DEFAULT 1
        CHECK (relationship_revision >= 1),
    cancelled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    CHECK (valid_until IS NULL OR valid_until > valid_from),
    CHECK (
        (status = 'cancelled' AND cancelled_reason IS NOT NULL
            AND cancelled_at IS NOT NULL)
        OR (status <> 'cancelled' AND cancelled_reason IS NULL
            AND cancelled_at IS NULL)
    ),
    CHECK (
        (status = 'delivered' AND delivered_at IS NOT NULL
            AND delivered_channel IS NOT NULL)
        OR (status <> 'delivered' AND delivered_at IS NULL
            AND delivered_channel IS NULL)
    ),
    CHECK (
        status <> 'in_progress'
        OR (fencing_token IS NOT NULL AND leased_until IS NOT NULL)
    ),
    CHECK (
        status NOT IN ('pending', 'failed')
        OR (fencing_token IS NULL AND leased_until IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_notification_recipients_intent
ON notification_recipients(intent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notification_recipients_relationship
ON notification_recipients(relationship_id, status);
CREATE INDEX IF NOT EXISTS idx_notification_recipients_due
ON notification_recipients(status, next_attempt_at, leased_until);

CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES notification_intents(intent_id)
        ON DELETE RESTRICT,
    recipient_id TEXT NOT NULL REFERENCES notification_recipients(recipient_id)
        ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    channel TEXT NOT NULL CHECK (channel IN (
        'wechat_subscription', 'sms', 'phone_call'
    )),
    -- STABLE provider idempotency key shared by every retry of the same
    -- recipient+channel (NEVER contains attempt_number/fencing token).
    logical_delivery_key TEXT NOT NULL
        CHECK (char_length(logical_delivery_key) BETWEEN 1 AND 320),
    status TEXT NOT NULL
        CHECK (status IN ('leased', 'delivered', 'failed', 'uncertain')),
    fencing_token TEXT NOT NULL,
    leased_until TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    error_code TEXT CHECK (
        error_code IS NULL OR char_length(error_code) BETWEEN 1 AND 96
    ),
    UNIQUE (intent_id, recipient_id, attempt_number),
    CHECK (
        (status = 'leased' AND finished_at IS NULL)
        OR (status <> 'leased' AND finished_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_notification_attempts_recipient
ON notification_delivery_attempts(recipient_id, attempt_number);

CREATE TABLE IF NOT EXISTS notification_receipts (
    receipt_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES notification_intents(intent_id),
    recipient_id TEXT NOT NULL REFERENCES notification_recipients(recipient_id),
    attempt_id TEXT NOT NULL UNIQUE
        REFERENCES notification_delivery_attempts(attempt_id),
    channel TEXT NOT NULL CHECK (channel IN (
        'wechat_subscription', 'sms', 'phone_call'
    )),
    channel_receipt_id TEXT NOT NULL
        CHECK (char_length(channel_receipt_id) BETWEEN 1 AND 128),
    delivered_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_receipts_intent
ON notification_receipts(intent_id, delivered_at);

CREATE TABLE IF NOT EXISTS notification_audit_events (
    event_id TEXT PRIMARY KEY,
    action TEXT NOT NULL CHECK (char_length(action) BETWEEN 1 AND 64),
    actor_person_id TEXT,
    intent_id TEXT,
    recipient_id TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(payload_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notification_audit_intent
ON notification_audit_events(intent_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notification_audit_recipient
ON notification_audit_events(recipient_id, created_at DESC);

CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL CHECK (char_length(topic) BETWEEN 1 AND 64),
    -- API writes bind this fact from app.authenticated_actor; worker rows may
    -- be NULL because worker authority is the real database role.
    actor_person_id TEXT,
    -- P0-2: outbox rows are scoped to the intent's subject so the API role
    -- can append its own business events without ever reading them; worker
    -- delivery-state rows may carry NULL (the worker role is trusted).
    subject_person_id TEXT,
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

CREATE INDEX IF NOT EXISTS idx_notification_outbox_pending
ON notification_outbox(status, created_at);

-- ---------------------------------------------------------------------------
-- Immutability guards (attempt / receipt / core identity columns)
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION notification_intent_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $notification_intent_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'status', 'cancelled_reason', 'cancelled_at', 'delivered_at', 'updated_at'
    ]) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[
        'status', 'cancelled_reason', 'cancelled_at', 'delivered_at', 'updated_at'
    ]) THEN
        RAISE EXCEPTION 'notification intent identity is immutable';
    END IF;
    RETURN NEW;
END
$notification_intent_immutable$;

DROP TRIGGER IF EXISTS notification_intent_core_immutable ON notification_intents;
CREATE TRIGGER notification_intent_core_immutable
BEFORE UPDATE ON notification_intents
FOR EACH ROW EXECUTE FUNCTION notification_intent_core_immutable_guard();

CREATE OR REPLACE FUNCTION notification_recipient_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $notification_recipient_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'relationship_status', 'channel_index', 'status', 'attempts',
        'next_attempt_at', 'leased_until', 'fencing_token', 'last_error_code',
        'delivered_at', 'delivered_channel', 'cancelled_reason', 'cancelled_at',
        'updated_at'
    ]) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY[
        'relationship_status', 'channel_index', 'status', 'attempts',
        'next_attempt_at', 'leased_until', 'fencing_token', 'last_error_code',
        'delivered_at', 'delivered_channel', 'cancelled_reason', 'cancelled_at',
        'updated_at'
    ]) THEN
        RAISE EXCEPTION 'notification recipient identity is immutable';
    END IF;
    RETURN NEW;
END
$notification_recipient_immutable$;

DROP TRIGGER IF EXISTS notification_recipient_core_immutable
ON notification_recipients;
CREATE TRIGGER notification_recipient_core_immutable
BEFORE UPDATE ON notification_recipients
FOR EACH ROW EXECUTE FUNCTION notification_recipient_core_immutable_guard();

CREATE OR REPLACE FUNCTION notification_attempt_core_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $notification_attempt_immutable$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['status', 'finished_at', 'error_code'])
       IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['status', 'finished_at', 'error_code']) THEN
        RAISE EXCEPTION 'notification attempt identity is immutable';
    END IF;
    RETURN NEW;
END
$notification_attempt_immutable$;

DROP TRIGGER IF EXISTS notification_attempt_core_immutable
ON notification_delivery_attempts;
CREATE TRIGGER notification_attempt_core_immutable
BEFORE UPDATE ON notification_delivery_attempts
FOR EACH ROW EXECUTE FUNCTION notification_attempt_core_immutable_guard();

CREATE OR REPLACE FUNCTION notification_receipt_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $notification_receipt_immutable$
BEGIN
    RAISE EXCEPTION 'notification receipts are append-only';
END
$notification_receipt_immutable$;

DROP TRIGGER IF EXISTS notification_receipt_immutable ON notification_receipts;
CREATE TRIGGER notification_receipt_immutable
BEFORE UPDATE ON notification_receipts
FOR EACH ROW EXECUTE FUNCTION notification_receipt_immutable_guard();

-- ---------------------------------------------------------------------------
-- Delete guards: only the account-deletion pipeline may remove rows
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION notification_delete_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $notification_delete$
BEGIN
    IF current_setting('app.notification_account_deletion', true) = '1' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'notification records require the account deletion pipeline';
END
$notification_delete$;

DROP TRIGGER IF EXISTS notification_intents_delete_guard ON notification_intents;
CREATE TRIGGER notification_intents_delete_guard
BEFORE DELETE ON notification_intents
FOR EACH ROW EXECUTE FUNCTION notification_delete_guard();

DROP TRIGGER IF EXISTS notification_recipients_delete_guard ON notification_recipients;
CREATE TRIGGER notification_recipients_delete_guard
BEFORE DELETE ON notification_recipients
FOR EACH ROW EXECUTE FUNCTION notification_delete_guard();

DROP TRIGGER IF EXISTS notification_attempts_delete_guard
ON notification_delivery_attempts;
CREATE TRIGGER notification_attempts_delete_guard
BEFORE DELETE ON notification_delivery_attempts
FOR EACH ROW EXECUTE FUNCTION notification_delete_guard();

DROP TRIGGER IF EXISTS notification_receipts_delete_guard ON notification_receipts;
CREATE TRIGGER notification_receipts_delete_guard
BEFORE DELETE ON notification_receipts
FOR EACH ROW EXECUTE FUNCTION notification_delete_guard();

DROP TRIGGER IF EXISTS notification_audit_delete_guard ON notification_audit_events;
CREATE TRIGGER notification_audit_delete_guard
BEFORE DELETE ON notification_audit_events
FOR EACH ROW EXECUTE FUNCTION notification_delete_guard();

DROP TRIGGER IF EXISTS notification_outbox_delete_guard ON notification_outbox;
CREATE TRIGGER notification_outbox_delete_guard
BEFORE DELETE ON notification_outbox
FOR EACH ROW EXECUTE FUNCTION notification_delete_guard();

-- ---------------------------------------------------------------------------
-- Row-level security: transaction-level app context, minimal policies.
-- ---------------------------------------------------------------------------

ALTER TABLE notification_intents ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_delivery_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE notification_outbox ENABLE ROW LEVEL SECURITY;

ALTER TABLE notification_intents FORCE ROW LEVEL SECURITY;
ALTER TABLE notification_recipients FORCE ROW LEVEL SECURITY;
ALTER TABLE notification_delivery_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE notification_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE notification_audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE notification_outbox FORCE ROW LEVEL SECURITY;

DO $notification_policy$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_notification_api') AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_notification_worker') THEN
        -- P0-2: real role separation - the API role can never execute
        -- worker commands; GUCs only carry row context.
        -- P0-2/P0-5: column-level grants - the API can only move state
        -- (status/cancel fields) of its own subject's rows, never delivery
        -- internals; the worker may touch the delivery columns.
        GRANT SELECT, INSERT ON notification_intents
            TO memoria_notification_api, memoria_notification_worker;
        GRANT UPDATE (status, cancelled_reason, cancelled_at, delivered_at,
                      updated_at) ON notification_intents
            TO memoria_notification_api, memoria_notification_worker;
        GRANT SELECT, INSERT ON notification_recipients
            TO memoria_notification_api, memoria_notification_worker;
        GRANT UPDATE (relationship_status, channel_index, status, attempts,
                      next_attempt_at, leased_until, fencing_token,
                      last_error_code, delivered_at, delivered_channel,
                      cancelled_reason, cancelled_at, updated_at)
            ON notification_recipients TO memoria_notification_worker;
        GRANT UPDATE (status, cancelled_reason, cancelled_at, updated_at)
            ON notification_recipients TO memoria_notification_api;
        GRANT SELECT ON notification_delivery_attempts
            TO memoria_notification_api, memoria_notification_worker;
        GRANT INSERT, UPDATE ON notification_delivery_attempts
            TO memoria_notification_worker;
        GRANT SELECT ON notification_receipts
            TO memoria_notification_api, memoria_notification_worker;
        GRANT INSERT ON notification_receipts
            TO memoria_notification_worker;
        GRANT INSERT ON notification_audit_events
            TO memoria_notification_api, memoria_notification_worker;
        GRANT SELECT ON notification_audit_events
            TO memoria_notification_worker;
        -- The API appends audit rows inside its business transactions with
        -- ``ON CONFLICT (event_id) DO NOTHING`` for idempotency; PostgreSQL
        -- requires a SELECT grant for conflict detection.  Column-level
        -- SELECT on event_id only - no business-content column is exposed.
        GRANT SELECT (event_id) ON notification_audit_events
            TO memoria_notification_api;
        GRANT INSERT (outbox_id, event_id, topic, actor_person_id,
                      subject_person_id, payload_json, status, attempts, locked_until,
                      last_error_code, created_at, delivered_at, updated_at)
            ON notification_outbox
            TO memoria_notification_api, memoria_notification_worker;
        GRANT SELECT ON notification_outbox
            TO memoria_notification_worker;
        -- Same idempotent-insert conflict-detection grant for the API's
        -- own-subject outbox rows (event_id only).
        GRANT SELECT (event_id) ON notification_outbox
            TO memoria_notification_api;
        GRANT UPDATE (status, attempts, locked_until, last_error_code,
                      delivered_at, updated_at) ON notification_outbox
            TO memoria_notification_worker;

        -- intents: API role sees/creates only its own subject's intents;
        -- the worker may process any intent.
        DROP POLICY IF EXISTS notification_intents_select ON notification_intents;
        CREATE POLICY notification_intents_select ON notification_intents FOR SELECT
            TO memoria_notification_api, memoria_notification_worker
            USING (
                (
                    subject_person_id = current_setting(
                        'app.authenticated_subject', true
                    )
                    AND NULLIF(current_setting(
                        'app.authenticated_actor', true
                    ), '') IS NOT NULL
                )
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        DROP POLICY IF EXISTS notification_intents_insert ON notification_intents;
        CREATE POLICY notification_intents_insert ON notification_intents FOR INSERT
            TO memoria_notification_api, memoria_notification_worker
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
                OR (
                    subject_person_id = current_setting(
                        'app.authenticated_subject', true
                    )
                    AND actor_person_id = current_setting(
                        'app.authenticated_actor', true
                    )
                )
            );

        DROP POLICY IF EXISTS notification_intents_update ON notification_intents;
        CREATE POLICY notification_intents_update ON notification_intents FOR UPDATE
            TO memoria_notification_api, memoria_notification_worker
            USING (
                (
                    subject_person_id = current_setting(
                        'app.authenticated_subject', true
                    )
                    AND NULLIF(current_setting(
                        'app.authenticated_actor', true
                    ), '') IS NOT NULL
                )
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            )
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
                OR (
                    subject_person_id = current_setting(
                        'app.authenticated_subject', true
                    )
                    AND NULLIF(current_setting(
                        'app.authenticated_actor', true
                    ), '') IS NOT NULL
                )
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        -- recipients: API role sees/creates only its own person's rows; the
        -- worker may read/update every recipient it processes.
        DROP POLICY IF EXISTS notification_recipients_select
            ON notification_recipients;
        CREATE POLICY notification_recipients_select
            ON notification_recipients FOR SELECT
            TO memoria_notification_api, memoria_notification_worker
            USING (
                person_id = current_setting('app.notification.person_id', true)
                OR EXISTS (
                    SELECT 1 FROM notification_intents i
                    WHERE i.intent_id = notification_recipients.intent_id
                      AND i.subject_person_id = current_setting(
                          'app.authenticated_subject', true
                      )
                      AND NULLIF(current_setting(
                          'app.authenticated_actor', true
                      ), '') IS NOT NULL
                )
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        DROP POLICY IF EXISTS notification_recipients_insert
            ON notification_recipients;
        CREATE POLICY notification_recipients_insert
            ON notification_recipients FOR INSERT
            TO memoria_notification_api, memoria_notification_worker
            WITH CHECK (
                -- P0-E: recipients may only be written for an intent owned
                -- by the CURRENT subject context (the atomic create sets
                -- it); a bare role check would let anyone fabricate
                -- recipients for other subjects.
                EXISTS (
                    SELECT 1 FROM notification_intents i
                    WHERE i.intent_id = notification_recipients.intent_id
                      AND i.subject_person_id = current_setting(
                          'app.authenticated_subject', true
                      )
                      AND i.actor_person_id = current_setting(
                          'app.authenticated_actor', true
                      )
                )
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        DROP POLICY IF EXISTS notification_recipients_update
            ON notification_recipients;
        CREATE POLICY notification_recipients_update
            ON notification_recipients FOR UPDATE
            TO memoria_notification_api, memoria_notification_worker
            USING (
                person_id = current_setting('app.notification.person_id', true)
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            )
            WITH CHECK (
                person_id = current_setting('app.notification.person_id', true)
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        -- delivery attempts / receipts: scoped to the recipient being
        -- processed, or visible to the worker.
        DROP POLICY IF EXISTS notification_attempts_select
            ON notification_delivery_attempts;
        CREATE POLICY notification_attempts_select
            ON notification_delivery_attempts FOR SELECT
            TO memoria_notification_api, memoria_notification_worker
            USING (
                recipient_id = current_setting('app.notification.recipient_id', true)
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        DROP POLICY IF EXISTS notification_attempts_insert
            ON notification_delivery_attempts;
        CREATE POLICY notification_attempts_insert
            ON notification_delivery_attempts FOR INSERT
            TO memoria_notification_api, memoria_notification_worker
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
                AND recipient_id = current_setting(
                    'app.notification.recipient_id', true
                )
            );

        DROP POLICY IF EXISTS notification_attempts_update
            ON notification_delivery_attempts;
        CREATE POLICY notification_attempts_update
            ON notification_delivery_attempts FOR UPDATE
            TO memoria_notification_api, memoria_notification_worker
            USING (
                recipient_id = current_setting('app.notification.recipient_id', true)
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            )
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        DROP POLICY IF EXISTS notification_receipts_select ON notification_receipts;
        CREATE POLICY notification_receipts_select ON notification_receipts FOR SELECT
            TO memoria_notification_api, memoria_notification_worker
            USING (
                recipient_id = current_setting('app.notification.recipient_id', true)
                OR EXISTS (
                    SELECT 1 FROM notification_intents i
                    WHERE i.intent_id = notification_receipts.intent_id
                      AND EXISTS (
                          SELECT 1 FROM notification_recipients r
                          WHERE r.intent_id = i.intent_id
                            AND r.recipient_id = notification_receipts.recipient_id
                      )
                      AND i.subject_person_id = current_setting(
                          'app.authenticated_subject', true
                      )
                      AND NULLIF(current_setting(
                          'app.authenticated_actor', true
                      ), '') IS NOT NULL
                )
                OR pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        DROP POLICY IF EXISTS notification_receipts_insert ON notification_receipts;
        CREATE POLICY notification_receipts_insert ON notification_receipts FOR INSERT
            TO memoria_notification_api, memoria_notification_worker
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
                AND recipient_id = current_setting(
                    'app.notification.recipient_id', true
                )
            );

        DROP POLICY IF EXISTS notification_receipts_update ON notification_receipts;
        CREATE POLICY notification_receipts_update ON notification_receipts FOR UPDATE
            TO memoria_notification_api, memoria_notification_worker
            USING (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
            )
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        -- audit: API appends ONLY events of its own subject's intents (the
        -- intent-id join), the worker appends delivery facts; only the
        -- worker reads.
        DROP POLICY IF EXISTS notification_audit_insert ON notification_audit_events;
        CREATE POLICY notification_audit_insert ON notification_audit_events FOR INSERT
            TO memoria_notification_api, memoria_notification_worker
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
                OR EXISTS (
                    SELECT 1 FROM notification_intents i
                    WHERE i.intent_id = notification_audit_events.intent_id
                      AND i.subject_person_id = current_setting(
                          'app.authenticated_subject', true
                      )
                      AND i.actor_person_id = current_setting(
                          'app.authenticated_actor', true
                      )
                      AND notification_audit_events.actor_person_id = current_setting(
                          'app.authenticated_actor', true
                      )
                )
            );

        DROP POLICY IF EXISTS notification_audit_select ON notification_audit_events;
        CREATE POLICY notification_audit_select ON notification_audit_events FOR SELECT
            TO memoria_notification_api, memoria_notification_worker
            USING (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        -- outbox: the API appends ONLY its own subject's business events
        -- (row is subject-scoped) and can never read or alter outbox rows;
        -- the worker owns delivery state.
        DROP POLICY IF EXISTS notification_outbox_insert ON notification_outbox;
        CREATE POLICY notification_outbox_insert ON notification_outbox FOR INSERT
            TO memoria_notification_api, memoria_notification_worker
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
                OR (
                    subject_person_id = current_setting(
                        'app.authenticated_subject', true
                    )
                    AND actor_person_id = current_setting(
                        'app.authenticated_actor', true
                    )
                )
            );

        DROP POLICY IF EXISTS notification_outbox_select ON notification_outbox;
        CREATE POLICY notification_outbox_select ON notification_outbox FOR SELECT
            TO memoria_notification_api, memoria_notification_worker
            USING (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

        DROP POLICY IF EXISTS notification_outbox_update ON notification_outbox;
        CREATE POLICY notification_outbox_update ON notification_outbox FOR UPDATE
            TO memoria_notification_api, memoria_notification_worker
            USING (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
            )
            WITH CHECK (
                pg_has_role(current_user, 'memoria_notification_worker', 'member')
            );

    END IF;
END
$notification_policy$;
