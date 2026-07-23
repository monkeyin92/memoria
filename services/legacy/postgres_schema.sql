DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolbypassrls) THEN
        RAISE EXCEPTION 'Legacy application role must be NOBYPASSRLS';
    END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS legacy_grants (
    grant_id UUID PRIMARY KEY,
    owner_account_id TEXT NOT NULL,
    grantee_account_id TEXT NOT NULL CHECK (grantee_account_id <> owner_account_id),
    version_id UUID NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    manifest_sha256 CHAR(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    relationship_profile_id UUID NOT NULL,
    relationship_profile_version INTEGER NOT NULL CHECK (relationship_profile_version > 0),
    relationship_id UUID NOT NULL,
    relationship_salutation TEXT NOT NULL,
    relationship_tone TEXT NOT NULL,
    relationship_advice_style TEXT NOT NULL,
    relationship_sharing_scope TEXT NOT NULL,
    relationship_boundaries_json JSONB NOT NULL,
    allowed_items_json JSONB NOT NULL,
    visibility TEXT NOT NULL CHECK (visibility IN ('family', 'public')),
    scope_sha256 CHAR(64) NOT NULL CHECK (scope_sha256 ~ '^[0-9a-f]{64}$'),
    voice_allowed BOOLEAN NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    activated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    grant_snapshot_sha256 CHAR(64) NOT NULL CHECK (grant_snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pg_legacy_grants_owner
ON legacy_grants(owner_account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pg_legacy_grants_grantee
ON legacy_grants(grantee_account_id, created_at);

CREATE TABLE IF NOT EXISTS legacy_relationship_shells (
    shell_id UUID PRIMARY KEY,
    grant_id UUID NOT NULL UNIQUE REFERENCES legacy_grants(grant_id) ON DELETE CASCADE,
    owner_account_id TEXT NOT NULL,
    grantee_account_id TEXT NOT NULL,
    preferences_json JSONB NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_shell_turns (
    shell_turn_id UUID PRIMARY KEY,
    shell_id UUID NOT NULL REFERENCES legacy_relationship_shells(shell_id) ON DELETE CASCADE,
    grant_id UUID NOT NULL REFERENCES legacy_grants(grant_id) ON DELETE CASCADE,
    actor_role TEXT NOT NULL CHECK (actor_role IN ('grantee', 'digital_self')),
    actual_heard_text TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    tool_epoch INTEGER NOT NULL CHECK (tool_epoch >= 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    UNIQUE (shell_id, actor_role, session_id, turn_id, generation_id, tool_epoch)
);

CREATE TABLE IF NOT EXISTS legacy_audit_events (
    event_id UUID PRIMARY KEY,
    grant_id UUID NOT NULL REFERENCES legacy_grants(grant_id) ON DELETE CASCADE,
    owner_account_id TEXT NOT NULL,
    grantee_account_id TEXT NOT NULL,
    actor_account_id TEXT NOT NULL,
    shell_id UUID,
    shell_turn_id UUID,
    session_id TEXT,
    turn_id TEXT,
    generation_id TEXT,
    tool_epoch INTEGER CHECK (tool_epoch IS NULL OR tool_epoch >= 0),
    target_kind TEXT CHECK (target_kind IS NULL OR target_kind IN (
        'memory_claim', 'persona_trait', 'cognitive_claim', 'decision_case',
        'relationship_profile', 'voice_profile'
    )),
    target_id TEXT,
    action TEXT NOT NULL CHECK (action IN (
        'issue', 'activate', 'revoke', 'resolve_access',
        'append_shell_turn', 'update_shell_preferences', 'read_source',
        'plan_answer', 'select_voice', 'refuse'
    )),
    decision TEXT NOT NULL CHECK (decision IN ('allowed', 'denied')),
    reason TEXT NOT NULL CHECK (reason IN (
        'issued', 'activated', 'revoked', 'owner_preview', 'grantee_session',
        'shell_turn_appended', 'shell_preferences_updated', 'source_read',
        'answer_planned', 'voice_selected', 'privacy_refusal', 'unknown_refusal'
    )),
    occurred_at TIMESTAMPTZ NOT NULL,
    CHECK (
        (session_id IS NULL AND turn_id IS NULL AND generation_id IS NULL AND tool_epoch IS NULL)
        OR
        (session_id IS NOT NULL AND turn_id IS NOT NULL
         AND generation_id IS NOT NULL AND tool_epoch IS NOT NULL)
    ),
    CHECK (
        (target_kind IS NULL AND target_id IS NULL)
        OR (target_kind IS NOT NULL AND target_id IS NOT NULL)
    ),
    CHECK (
        (action = 'issue' AND decision = 'allowed' AND reason = 'issued')
        OR (action = 'activate' AND decision = 'allowed' AND reason = 'activated')
        OR (action = 'revoke' AND decision = 'allowed' AND reason = 'revoked')
        OR (action = 'resolve_access' AND decision = 'allowed'
            AND reason IN ('owner_preview', 'grantee_session'))
        OR (action = 'append_shell_turn' AND decision = 'allowed'
            AND reason = 'shell_turn_appended')
        OR (action = 'update_shell_preferences' AND decision = 'allowed'
            AND reason = 'shell_preferences_updated')
        OR (action = 'read_source' AND decision = 'allowed' AND reason = 'source_read'
            AND session_id IS NOT NULL AND target_kind <> 'voice_profile')
        OR (action = 'plan_answer' AND decision = 'allowed' AND reason = 'answer_planned'
            AND session_id IS NOT NULL AND target_kind IS NULL)
        OR (action = 'select_voice' AND decision = 'allowed' AND reason = 'voice_selected'
            AND target_kind = 'voice_profile')
        OR (action = 'refuse' AND decision = 'denied'
            AND reason IN ('privacy_refusal', 'unknown_refusal')
            AND session_id IS NOT NULL AND target_kind IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_pg_legacy_audit_grant_occurred
ON legacy_audit_events(grant_id, occurred_at);

CREATE TABLE IF NOT EXISTS legacy_command_receipts (
    actor_account_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    command_type TEXT NOT NULL,
    command_sha256 CHAR(64) NOT NULL CHECK (command_sha256 ~ '^[0-9a-f]{64}$'),
    result_kind TEXT NOT NULL CHECK (result_kind IN ('grant', 'shell', 'shell_turn')),
    result_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (actor_account_id, idempotency_key)
);

CREATE OR REPLACE FUNCTION legacy_grant_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['activated_at','revoked_at','grant_snapshot_sha256','revision'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['activated_at','revoked_at','grant_snapshot_sha256','revision']) THEN
        RAISE EXCEPTION 'legacy grant core is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS legacy_grant_immutable ON legacy_grants;
CREATE TRIGGER legacy_grant_immutable BEFORE UPDATE ON legacy_grants
FOR EACH ROW EXECUTE FUNCTION legacy_grant_immutable_guard();

CREATE OR REPLACE FUNCTION legacy_shell_identity_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['preferences_json','revision','updated_at'])
       IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['preferences_json','revision','updated_at']) THEN
        RAISE EXCEPTION 'legacy shell identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS legacy_shell_identity_immutable ON legacy_relationship_shells;
CREATE TRIGGER legacy_shell_identity_immutable BEFORE UPDATE ON legacy_relationship_shells
FOR EACH ROW EXECUTE FUNCTION legacy_shell_identity_immutable_guard();

CREATE OR REPLACE FUNCTION legacy_shell_turn_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'legacy shell turns are immutable'; END;
$$;
DROP TRIGGER IF EXISTS legacy_shell_turn_immutable ON legacy_shell_turns;
CREATE TRIGGER legacy_shell_turn_immutable BEFORE UPDATE ON legacy_shell_turns
FOR EACH ROW EXECUTE FUNCTION legacy_shell_turn_immutable_guard();

CREATE OR REPLACE FUNCTION legacy_audit_event_immutable_guard()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'legacy audit events are immutable'; END;
$$;
DROP TRIGGER IF EXISTS legacy_audit_event_immutable ON legacy_audit_events;
CREATE TRIGGER legacy_audit_event_immutable BEFORE UPDATE ON legacy_audit_events
FOR EACH ROW EXECUTE FUNCTION legacy_audit_event_immutable_guard();

ALTER TABLE legacy_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_grants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS legacy_grants_select ON legacy_grants;
CREATE POLICY legacy_grants_select ON legacy_grants FOR SELECT USING (
    current_setting('app.actor_account_id', true) IN (owner_account_id, grantee_account_id)
);
DROP POLICY IF EXISTS legacy_grants_insert ON legacy_grants;
CREATE POLICY legacy_grants_insert ON legacy_grants FOR INSERT WITH CHECK (
    owner_account_id = current_setting('app.actor_account_id', true)
);
DROP POLICY IF EXISTS legacy_grants_update ON legacy_grants;
CREATE POLICY legacy_grants_update ON legacy_grants FOR UPDATE USING (
    owner_account_id = current_setting('app.actor_account_id', true)
) WITH CHECK (owner_account_id = current_setting('app.actor_account_id', true));
DROP POLICY IF EXISTS legacy_grants_delete ON legacy_grants;
CREATE POLICY legacy_grants_delete ON legacy_grants FOR DELETE USING (
    current_setting('app.actor_account_id', true) IN (owner_account_id, grantee_account_id)
);

ALTER TABLE legacy_relationship_shells ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_relationship_shells FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS legacy_shells_select ON legacy_relationship_shells;
CREATE POLICY legacy_shells_select ON legacy_relationship_shells FOR SELECT USING (
    current_setting('app.actor_account_id', true) IN (owner_account_id, grantee_account_id)
);
DROP POLICY IF EXISTS legacy_shells_insert ON legacy_relationship_shells;
CREATE POLICY legacy_shells_insert ON legacy_relationship_shells FOR INSERT WITH CHECK (
    grantee_account_id = current_setting('app.actor_account_id', true)
);
DROP POLICY IF EXISTS legacy_shells_update ON legacy_relationship_shells;
CREATE POLICY legacy_shells_update ON legacy_relationship_shells FOR UPDATE USING (
    grantee_account_id = current_setting('app.actor_account_id', true)
) WITH CHECK (grantee_account_id = current_setting('app.actor_account_id', true));

ALTER TABLE legacy_shell_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_shell_turns FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS legacy_turns_policy ON legacy_shell_turns;
CREATE POLICY legacy_turns_policy ON legacy_shell_turns USING (
    EXISTS (SELECT 1 FROM legacy_grants related_grant
            WHERE related_grant.grant_id = legacy_shell_turns.grant_id
              AND current_setting('app.actor_account_id', true)
                  IN (related_grant.owner_account_id, related_grant.grantee_account_id))
) WITH CHECK (
    EXISTS (SELECT 1 FROM legacy_grants related_grant
            WHERE related_grant.grant_id = legacy_shell_turns.grant_id
              AND current_setting('app.actor_account_id', true)
                  = related_grant.grantee_account_id)
);

ALTER TABLE legacy_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_audit_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS legacy_audit_policy ON legacy_audit_events;
CREATE POLICY legacy_audit_policy ON legacy_audit_events USING (
    current_setting('app.actor_account_id', true) IN (owner_account_id, grantee_account_id)
) WITH CHECK (
    actor_account_id = current_setting('app.actor_account_id', true)
    AND current_setting('app.actor_account_id', true) IN (owner_account_id, grantee_account_id)
);

ALTER TABLE legacy_command_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_command_receipts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS legacy_receipts_policy ON legacy_command_receipts;
DROP POLICY IF EXISTS legacy_receipts_select ON legacy_command_receipts;
CREATE POLICY legacy_receipts_select ON legacy_command_receipts FOR SELECT USING (
    actor_account_id = current_setting('app.actor_account_id', true)
    OR (result_kind = 'grant' AND EXISTS (
        SELECT 1 FROM legacy_grants related_grant
        WHERE related_grant.grant_id = legacy_command_receipts.result_id
          AND current_setting('app.actor_account_id', true)
              IN (
                  related_grant.owner_account_id,
                  related_grant.grantee_account_id
              )
    ))
    OR (result_kind = 'shell' AND EXISTS (
        SELECT 1 FROM legacy_relationship_shells shell
        WHERE shell.shell_id = legacy_command_receipts.result_id
          AND current_setting('app.actor_account_id', true)
              IN (shell.owner_account_id, shell.grantee_account_id)
    ))
    OR (result_kind = 'shell_turn' AND EXISTS (
        SELECT 1 FROM legacy_shell_turns turn
        JOIN legacy_grants related_grant
          ON related_grant.grant_id = turn.grant_id
        WHERE turn.shell_turn_id = legacy_command_receipts.result_id
          AND current_setting('app.actor_account_id', true)
              IN (
                  related_grant.owner_account_id,
                  related_grant.grantee_account_id
              )
    ))
);
DROP POLICY IF EXISTS legacy_receipts_insert ON legacy_command_receipts;
CREATE POLICY legacy_receipts_insert ON legacy_command_receipts FOR INSERT WITH CHECK (
    actor_account_id = current_setting('app.actor_account_id', true)
);
DROP POLICY IF EXISTS legacy_receipts_related_delete ON legacy_command_receipts;
CREATE POLICY legacy_receipts_related_delete ON legacy_command_receipts FOR DELETE USING (
    actor_account_id = current_setting('app.actor_account_id', true)
    OR (result_kind = 'grant' AND EXISTS (
        SELECT 1 FROM legacy_grants related_grant
        WHERE related_grant.grant_id = legacy_command_receipts.result_id
          AND current_setting('app.actor_account_id', true)
              IN (
                  related_grant.owner_account_id,
                  related_grant.grantee_account_id
              )
    ))
    OR (result_kind = 'shell' AND EXISTS (
        SELECT 1 FROM legacy_relationship_shells shell
        WHERE shell.shell_id = legacy_command_receipts.result_id
          AND current_setting('app.actor_account_id', true)
              IN (shell.owner_account_id, shell.grantee_account_id)
    ))
    OR (result_kind = 'shell_turn' AND EXISTS (
        SELECT 1 FROM legacy_shell_turns turn
        JOIN legacy_grants related_grant
          ON related_grant.grant_id = turn.grant_id
        WHERE turn.shell_turn_id = legacy_command_receipts.result_id
          AND current_setting('app.actor_account_id', true)
              IN (
                  related_grant.owner_account_id,
                  related_grant.grantee_account_id
              )
    ))
);
