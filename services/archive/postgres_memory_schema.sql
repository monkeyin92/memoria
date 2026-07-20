CREATE TABLE IF NOT EXISTS memory_compile_receipts (
    event_id TEXT PRIMARY KEY
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('compiled', 'ignored')),
    compiled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_claims (
    claim_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    sensitive_domain TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    valid_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    review_event_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_pg_memory_claim_account_subject
ON memory_claims(account_id, subject_key, predicate, status);

CREATE TABLE IF NOT EXISTS person_entities (
    person_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    relationship_to_owner TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (account_id, canonical_key)
);

CREATE TABLE IF NOT EXISTS person_aliases (
    person_id UUID NOT NULL REFERENCES person_entities(person_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    alias TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    PRIMARY KEY (person_id, alias, source_event_id)
);

ALTER TABLE person_aliases ADD COLUMN IF NOT EXISTS status TEXT NOT NULL
DEFAULT 'candidate' CHECK (
    status IN ('candidate', 'confirmed', 'disputed', 'retracted')
);

CREATE TABLE IF NOT EXISTS relationships (
    relationship_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    person_id UUID NOT NULL REFERENCES person_entities(person_id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    valid_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS life_episodes (
    episode_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    event_start TIMESTAMPTZ NOT NULL,
    event_end TIMESTAMPTZ,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS timeline_entries (
    timeline_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    episode_id UUID NOT NULL REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    event_start TIMESTAMPTZ NOT NULL,
    event_end TIMESTAMPTZ,
    time_precision TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episode_evidence (
    episode_id UUID NOT NULL REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    PRIMARY KEY (episode_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    applicability TEXT NOT NULL,
    counterexample TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_search_documents (
    document_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    item_id UUID NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(body, ''))
    ) STORED,
    UNIQUE (kind, item_id)
);

CREATE INDEX IF NOT EXISTS idx_pg_memory_search_account_status_time
ON memory_search_documents(account_id, status, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_pg_memory_search_vector
ON memory_search_documents USING GIN(search_vector);

ALTER TABLE memory_compile_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE person_entities ENABLE ROW LEVEL SECURITY;
ALTER TABLE person_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE relationships ENABLE ROW LEVEL SECURITY;
ALTER TABLE life_episodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE timeline_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE episode_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE knowledge_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_search_documents ENABLE ROW LEVEL SECURITY;

ALTER TABLE memory_compile_receipts FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_claims FORCE ROW LEVEL SECURITY;
ALTER TABLE person_entities FORCE ROW LEVEL SECURITY;
ALTER TABLE person_aliases FORCE ROW LEVEL SECURITY;
ALTER TABLE relationships FORCE ROW LEVEL SECURITY;
ALTER TABLE life_episodes FORCE ROW LEVEL SECURITY;
ALTER TABLE timeline_entries FORCE ROW LEVEL SECURITY;
ALTER TABLE episode_evidence FORCE ROW LEVEL SECURITY;
ALTER TABLE knowledge_items FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_search_documents FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS memory_receipt_account_policy ON memory_compile_receipts;
CREATE POLICY memory_receipt_account_policy ON memory_compile_receipts
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS memory_claim_account_policy ON memory_claims;
CREATE POLICY memory_claim_account_policy ON memory_claims
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS person_entity_account_policy ON person_entities;
CREATE POLICY person_entity_account_policy ON person_entities
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS person_alias_account_policy ON person_aliases;
CREATE POLICY person_alias_account_policy ON person_aliases
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS relationship_account_policy ON relationships;
CREATE POLICY relationship_account_policy ON relationships
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS life_episode_account_policy ON life_episodes;
CREATE POLICY life_episode_account_policy ON life_episodes
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS timeline_entry_account_policy ON timeline_entries;
CREATE POLICY timeline_entry_account_policy ON timeline_entries
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS episode_evidence_account_policy ON episode_evidence;
CREATE POLICY episode_evidence_account_policy ON episode_evidence
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS knowledge_item_account_policy ON knowledge_items;
CREATE POLICY knowledge_item_account_policy ON knowledge_items
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

DROP POLICY IF EXISTS memory_search_account_policy ON memory_search_documents;
CREATE POLICY memory_search_account_policy ON memory_search_documents
USING (account_id = current_setting('app.account_id', true))
WITH CHECK (account_id = current_setting('app.account_id', true));

-- pgvector is optional in developer PostgreSQL images. Production enables the
-- extension and receives this rebuildable semantic projection automatically.
DO $memory_vector$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')
       AND EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper)
    THEN
        CREATE EXTENSION IF NOT EXISTS vector;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        EXECUTE $sql$
            CREATE TABLE IF NOT EXISTS memory_vector_documents (
                item_id UUID PRIMARY KEY,
                account_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding vector NOT NULL,
                source_event_id TEXT NOT NULL
                    REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        $sql$;
        EXECUTE 'ALTER TABLE memory_vector_documents ENABLE ROW LEVEL SECURITY';
        EXECUTE 'ALTER TABLE memory_vector_documents FORCE ROW LEVEL SECURITY';
        EXECUTE 'DROP POLICY IF EXISTS memory_vector_account_policy ON memory_vector_documents';
        EXECUTE $sql$
            CREATE POLICY memory_vector_account_policy ON memory_vector_documents
            USING (account_id = current_setting('app.account_id', true))
            WITH CHECK (account_id = current_setting('app.account_id', true))
        $sql$;
    END IF;
END
$memory_vector$;
