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
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'semantic' CHECK (
        memory_kind IN ('semantic', 'episodic', 'procedural', 'relationship')
    ),
    subject_key TEXT NOT NULL,
    predicate TEXT NOT NULL,
    value TEXT NOT NULL,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    sensitive_domain TEXT NOT NULL,
    entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    extractor_version TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    valid_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    stability DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (stability BETWEEN 0 AND 1),
    salience DOUBLE PRECISION NOT NULL DEFAULT 0.5 CHECK (salience BETWEEN 0 AND 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal' CHECK (
        sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')
    ),
    conflict_state TEXT NOT NULL DEFAULT 'none' CHECK (
        conflict_state IN ('none', 'potential', 'active', 'resolved')
    ),
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
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'episodic' CHECK (memory_kind = 'episodic'),
    consolidation_key TEXT NOT NULL,
    entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    event_start TIMESTAMPTZ NOT NULL,
    event_end TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    stability DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (stability BETWEEN 0 AND 1),
    salience DOUBLE PRECISION NOT NULL DEFAULT 0.6 CHECK (salience BETWEEN 0 AND 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal' CHECK (
        sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')
    ),
    conflict_state TEXT NOT NULL DEFAULT 'none' CHECK (
        conflict_state IN ('none', 'potential', 'active', 'resolved')
    ),
    evidence_count INTEGER NOT NULL DEFAULT 1 CHECK (evidence_count >= 1),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    UNIQUE (account_id, consolidation_key)
);

CREATE TABLE IF NOT EXISTS timeline_entries (
    timeline_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    episode_id UUID NOT NULL REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'episodic' CHECK (memory_kind = 'episodic'),
    entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    event_start TIMESTAMPTZ NOT NULL,
    event_end TIMESTAMPTZ,
    time_precision TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    salience DOUBLE PRECISION NOT NULL DEFAULT 0.6 CHECK (salience BETWEEN 0 AND 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal' CHECK (
        sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')
    ),
    conflict_state TEXT NOT NULL DEFAULT 'none' CHECK (
        conflict_state IN ('none', 'potential', 'active', 'resolved')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS episode_evidence (
    episode_id UUID NOT NULL REFERENCES life_episodes(episode_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    PRIMARY KEY (episode_id, source_event_id)
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    knowledge_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    memory_kind TEXT NOT NULL DEFAULT 'procedural' CHECK (memory_kind = 'procedural'),
    entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    applicability TEXT NOT NULL,
    counterexample TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    stability DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (stability BETWEEN 0 AND 1),
    salience DOUBLE PRECISION NOT NULL DEFAULT 0.55 CHECK (salience BETWEEN 0 AND 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal' CHECK (
        sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')
    ),
    conflict_state TEXT NOT NULL DEFAULT 'none' CHECK (
        conflict_state IN ('none', 'potential', 'active', 'resolved')
    )
);

CREATE TABLE IF NOT EXISTS memory_search_documents (
    document_id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    item_id UUID NOT NULL,
    kind TEXT NOT NULL,
    memory_kind TEXT NOT NULL CHECK (
        memory_kind IN ('semantic', 'episodic', 'procedural', 'relationship')
    ),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    category TEXT NOT NULL,
    domain_category TEXT NOT NULL,
    entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    status TEXT NOT NULL CHECK (
        status IN ('candidate', 'confirmed', 'disputed', 'retracted')
    ),
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    occurred_at TIMESTAMPTZ NOT NULL,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ,
    observed_at TIMESTAMPTZ NOT NULL,
    stability DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (stability BETWEEN 0 AND 1),
    salience DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK (salience BETWEEN 0 AND 1),
    sensitivity TEXT NOT NULL DEFAULT 'personal' CHECK (
        sensitivity IN ('public', 'personal', 'sensitive', 'highly_sensitive')
    ),
    conflict_state TEXT NOT NULL DEFAULT 'none' CHECK (
        conflict_state IN ('none', 'potential', 'active', 'resolved')
    ),
    search_vector TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(body, ''))
    ) STORED,
    UNIQUE (kind, item_id)
);

ALTER TABLE memory_claims
    ADD COLUMN IF NOT EXISTS domain_category TEXT,
    ADD COLUMN IF NOT EXISTS memory_kind TEXT NOT NULL DEFAULT 'semantic',
    ADD COLUMN IF NOT EXISTS entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stability DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS salience DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'personal',
    ADD COLUMN IF NOT EXISTS conflict_state TEXT NOT NULL DEFAULT 'none';

ALTER TABLE life_episodes
    ADD COLUMN IF NOT EXISTS domain_category TEXT,
    ADD COLUMN IF NOT EXISTS memory_kind TEXT NOT NULL DEFAULT 'episodic',
    ADD COLUMN IF NOT EXISTS consolidation_key TEXT,
    ADD COLUMN IF NOT EXISTS entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stability DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS salience DOUBLE PRECISION NOT NULL DEFAULT 0.6,
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'personal',
    ADD COLUMN IF NOT EXISTS conflict_state TEXT NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS evidence_count INTEGER NOT NULL DEFAULT 1;

ALTER TABLE timeline_entries
    ADD COLUMN IF NOT EXISTS domain_category TEXT,
    ADD COLUMN IF NOT EXISTS memory_kind TEXT NOT NULL DEFAULT 'episodic',
    ADD COLUMN IF NOT EXISTS entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS salience DOUBLE PRECISION NOT NULL DEFAULT 0.6,
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'personal',
    ADD COLUMN IF NOT EXISTS conflict_state TEXT NOT NULL DEFAULT 'none';

ALTER TABLE episode_evidence
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'candidate';

ALTER TABLE knowledge_items
    ADD COLUMN IF NOT EXISTS domain_category TEXT,
    ADD COLUMN IF NOT EXISTS memory_kind TEXT NOT NULL DEFAULT 'procedural',
    ADD COLUMN IF NOT EXISTS entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stability DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS salience DOUBLE PRECISION NOT NULL DEFAULT 0.55,
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'personal',
    ADD COLUMN IF NOT EXISTS conflict_state TEXT NOT NULL DEFAULT 'none';

ALTER TABLE memory_search_documents
    ADD COLUMN IF NOT EXISTS memory_kind TEXT NOT NULL DEFAULT 'semantic',
    ADD COLUMN IF NOT EXISTS domain_category TEXT,
    ADD COLUMN IF NOT EXISTS entity_ids UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
    ADD COLUMN IF NOT EXISTS valid_from TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS valid_to TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS observed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stability DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS salience DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS sensitivity TEXT NOT NULL DEFAULT 'personal',
    ADD COLUMN IF NOT EXISTS conflict_state TEXT NOT NULL DEFAULT 'none';

UPDATE memory_claims
SET domain_category = COALESCE(domain_category, category),
    valid_from = COALESCE(valid_from, valid_at),
    observed_at = COALESCE(observed_at, valid_at),
    stability = CASE WHEN stability = 0 THEN confidence ELSE stability END,
    sensitivity = CASE
        WHEN sensitive_domain IN ('health', 'finance', 'legal', 'biometric')
            THEN 'highly_sensitive'
        WHEN sensitive_domain IN ('relationship', 'private') THEN 'sensitive'
        WHEN sensitive_domain = 'public' THEN 'public'
        ELSE sensitivity
    END;

UPDATE life_episodes episode
SET domain_category = COALESCE(episode.domain_category, episode.category),
    consolidation_key = COALESCE(
        NULLIF(episode.consolidation_key, ''),
        'legacy:' || episode.episode_id::TEXT
    ),
    observed_at = COALESCE(episode.observed_at, episode.event_start),
    stability = CASE WHEN episode.stability = 0 THEN 0.5 ELSE episode.stability END,
    evidence_count = GREATEST(
        episode.evidence_count,
        (SELECT count(*) FROM episode_evidence evidence
         WHERE evidence.episode_id = episode.episode_id)
    );

UPDATE timeline_entries
SET domain_category = COALESCE(domain_category, category),
    observed_at = COALESCE(observed_at, event_start);

UPDATE episode_evidence evidence
SET status = COALESCE(
    (SELECT timeline.status FROM timeline_entries timeline
     WHERE timeline.episode_id = evidence.episode_id
       AND timeline.source_event_id = evidence.source_event_id
     ORDER BY timeline.timeline_id LIMIT 1),
    evidence.status
);

UPDATE knowledge_items
SET domain_category = COALESCE(domain_category, category),
    observed_at = COALESCE(observed_at, occurred_at),
    valid_from = COALESCE(valid_from, occurred_at),
    stability = CASE WHEN stability = 0 THEN 0.5 ELSE stability END;

UPDATE memory_search_documents
SET memory_kind = CASE kind
        WHEN 'timeline' THEN 'episodic'
        WHEN 'episode' THEN 'episodic'
        WHEN 'knowledge' THEN 'procedural'
        WHEN 'skill' THEN 'procedural'
        ELSE memory_kind
    END,
    domain_category = COALESCE(domain_category, category),
    observed_at = COALESCE(observed_at, occurred_at),
    valid_from = COALESCE(valid_from, occurred_at),
    stability = CASE WHEN stability = 0 THEN 0.5 ELSE stability END,
    salience = CASE
        WHEN salience != 0 THEN salience
        WHEN kind IN ('timeline', 'episode') THEN 0.6
        WHEN kind = 'knowledge' THEN 0.55
        ELSE 0.5
    END;

ALTER TABLE memory_claims
    ALTER COLUMN domain_category SET NOT NULL,
    ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE life_episodes
    ALTER COLUMN domain_category SET NOT NULL,
    ALTER COLUMN consolidation_key SET NOT NULL,
    ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE timeline_entries
    ALTER COLUMN domain_category SET NOT NULL,
    ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE knowledge_items
    ALTER COLUMN domain_category SET NOT NULL,
    ALTER COLUMN observed_at SET NOT NULL;
ALTER TABLE memory_search_documents
    ALTER COLUMN domain_category SET NOT NULL,
    ALTER COLUMN observed_at SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_pg_life_episode_consolidation
ON life_episodes(account_id, consolidation_key);

CREATE INDEX IF NOT EXISTS idx_pg_memory_search_account_status_time
ON memory_search_documents(account_id, status, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_pg_memory_search_typed
ON memory_search_documents(
    account_id, memory_kind, domain_category, status, observed_at DESC
);

CREATE INDEX IF NOT EXISTS idx_pg_memory_search_entities
ON memory_search_documents USING GIN(entity_ids);

CREATE INDEX IF NOT EXISTS idx_pg_memory_search_vector
ON memory_search_documents USING GIN(search_vector);

CREATE TABLE IF NOT EXISTS memory_search_document_sources (
    document_id UUID NOT NULL
        REFERENCES memory_search_documents(document_id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    source_event_id TEXT NOT NULL
        REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_pg_memory_search_source_account
ON memory_search_document_sources(account_id, source_event_id);

INSERT INTO memory_search_document_sources (
    document_id, account_id, source_event_id
)
SELECT document_id, account_id, source_event_id
FROM memory_search_documents
ON CONFLICT DO NOTHING;

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
ALTER TABLE memory_search_document_sources ENABLE ROW LEVEL SECURITY;

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
ALTER TABLE memory_search_document_sources FORCE ROW LEVEL SECURITY;

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

DROP POLICY IF EXISTS memory_search_source_account_policy
ON memory_search_document_sources;
CREATE POLICY memory_search_source_account_policy ON memory_search_document_sources
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
                item_id UUID NOT NULL,
                account_id TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                embedding_dimensions INTEGER NOT NULL CHECK (
                    embedding_dimensions BETWEEN 1 AND 2000
                ),
                embedding vector NOT NULL,
                source_event_id TEXT NOT NULL
                    REFERENCES archive_evidence_events(event_id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (item_id, embedding_model, embedding_dimensions)
            )
        $sql$;
        EXECUTE $sql$
            ALTER TABLE memory_vector_documents
            ADD COLUMN IF NOT EXISTS embedding_dimensions INTEGER
        $sql$;
        EXECUTE $sql$
            UPDATE memory_vector_documents
            SET embedding_dimensions = vector_dims(embedding)
            WHERE embedding_dimensions IS NULL
        $sql$;
        EXECUTE $sql$
            ALTER TABLE memory_vector_documents
            ALTER COLUMN embedding_dimensions SET NOT NULL
        $sql$;
        IF EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'memory_vector_documents'::regclass
              AND contype = 'p'
              AND pg_get_constraintdef(oid) NOT LIKE
                  '%(item_id, embedding_model, embedding_dimensions)%'
        ) THEN
            EXECUTE $sql$
                ALTER TABLE memory_vector_documents
                DROP CONSTRAINT memory_vector_documents_pkey
            $sql$;
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'memory_vector_documents'::regclass
              AND contype = 'p'
        ) THEN
            EXECUTE $sql$
                ALTER TABLE memory_vector_documents
                ADD PRIMARY KEY (item_id, embedding_model, embedding_dimensions)
            $sql$;
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conrelid = 'memory_vector_documents'::regclass
              AND conname = 'memory_vector_dimensions_match'
        ) THEN
            EXECUTE $sql$
                ALTER TABLE memory_vector_documents
                ADD CONSTRAINT memory_vector_dimensions_match
                CHECK (vector_dims(embedding) = embedding_dimensions)
            $sql$;
        END IF;
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
