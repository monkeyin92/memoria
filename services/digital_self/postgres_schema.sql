CREATE TABLE IF NOT EXISTS digital_self_versions (
    version_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'testing', 'approved', 'frozen', 'revoked')
    ),
    manifest_json TEXT NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL,
    source_summary_sha256 CHAR(64) NOT NULL,
    parent_version_id UUID,
    rollback_target_version_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, version_number),
    UNIQUE (account_id, version_id),
    FOREIGN KEY (account_id, parent_version_id)
        REFERENCES digital_self_versions(account_id, version_id),
    FOREIGN KEY (account_id, rollback_target_version_id)
        REFERENCES digital_self_versions(account_id, version_id)
);

CREATE INDEX IF NOT EXISTS idx_pg_digital_self_account_status
ON digital_self_versions(account_id, status, version_number DESC);

CREATE TABLE IF NOT EXISTS digital_self_lifecycle_audit_events (
    event_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    actor_account_id TEXT NOT NULL CHECK (actor_account_id = account_id),
    action TEXT NOT NULL CHECK (
        action IN ('build', 'begin_testing', 'approve', 'freeze', 'revoke', 'rollback')
    ),
    version_id UUID NOT NULL,
    manifest_sha256 CHAR(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    from_status TEXT CHECK (
        from_status IS NULL OR from_status IN ('draft', 'testing', 'approved', 'frozen', 'revoked')
    ),
    to_status TEXT NOT NULL CHECK (
        to_status IN ('draft', 'testing', 'approved', 'frozen', 'revoked')
    ),
    target_version_id UUID,
    new_version_id UUID,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pg_digital_self_lifecycle_audit_account_occurred
ON digital_self_lifecycle_audit_events(account_id, occurred_at);

CREATE OR REPLACE FUNCTION digital_self_manifest_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status') THEN
        RAISE EXCEPTION 'digital self manifest is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS digital_self_manifest_immutable ON digital_self_versions;
CREATE TRIGGER digital_self_manifest_immutable
BEFORE UPDATE ON digital_self_versions
FOR EACH ROW EXECUTE FUNCTION digital_self_manifest_immutable_guard();

CREATE OR REPLACE FUNCTION digital_self_lifecycle_audit_immutable_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'digital self lifecycle audit events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS digital_self_lifecycle_audit_immutable
ON digital_self_lifecycle_audit_events;
CREATE TRIGGER digital_self_lifecycle_audit_immutable
BEFORE UPDATE ON digital_self_lifecycle_audit_events
FOR EACH ROW EXECUTE FUNCTION digital_self_lifecycle_audit_immutable_guard();

ALTER TABLE digital_self_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE digital_self_versions FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS digital_self_version_account_policy ON digital_self_versions;
CREATE POLICY digital_self_version_account_policy ON digital_self_versions
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

ALTER TABLE digital_self_lifecycle_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE digital_self_lifecycle_audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS digital_self_lifecycle_audit_account_policy
ON digital_self_lifecycle_audit_events;
CREATE POLICY digital_self_lifecycle_audit_account_policy
ON digital_self_lifecycle_audit_events
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));
