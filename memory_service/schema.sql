-- Memory service DB — the cross-harness memory brain (facts + profile + a
-- lightweight conversation-summary corpus). Agents keep their own full message
-- history; they push only (user+assistant) transcripts + summaries here.
--
-- The memory_* tables below are kept IN SYNC with octavius schema.sql's
-- "Long-term memory (v1)" section — same DDL, different (standalone) DB.

-- Lightweight conversations: provenance for facts + the corpus Block-2 synthesis
-- rolls up. Keyed by (service, key) so a durable thread re-push hits one row.
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT    NOT NULL,          -- 'octavius','pi-agent','claude-code',...
    conv_key    TEXT    NOT NULL,          -- agent-supplied stable key (thread/session id)
    summary     TEXT,
    started_at  TEXT    NOT NULL,
    ended_at    TEXT,
    UNIQUE (service, conv_key)
);
CREATE INDEX IF NOT EXISTS idx_ms_conversations_service
    ON conversations(service, started_at);

CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS conversation_tags (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    tag_id          INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (conversation_id, tag_id)
);

-- Cross-harness episodic recall (search over pushed summaries).
CREATE VIRTUAL TABLE IF NOT EXISTS summary_embeddings USING vec0(
    conversation_id INTEGER PRIMARY KEY,
    embedding       float[1024]
);

-- ============================================================================
-- memory_* — IN SYNC with octavius schema.sql.
-- ============================================================================
CREATE TABLE IF NOT EXISTS memory_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT    NOT NULL,
    predicate       TEXT    NOT NULL REFERENCES predicates(name),
    object          TEXT    NOT NULL,
    object_is_entity INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL DEFAULT 0.7,
    trust_tier      TEXT    NOT NULL DEFAULT 'derived',
    valid_from      TEXT    NOT NULL,
    valid_until     TEXT,
    superseded_by   INTEGER REFERENCES memory_facts(id),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_memory_facts_live
    ON memory_facts(valid_until, trust_tier, confidence);
CREATE INDEX IF NOT EXISTS idx_memory_facts_spo
    ON memory_facts(subject, predicate);

CREATE TABLE IF NOT EXISTS predicates (
    name        TEXT PRIMARY KEY,
    cardinality TEXT NOT NULL,
    description TEXT
);
INSERT OR IGNORE INTO predicates (name, cardinality, description) VALUES
    ('lives_in',      'functional', 'Where the subject currently resides'),
    ('has_role',      'multi',      'A role/title the subject holds (can hold several)'),
    ('works_at',      'multi',      'An organization the subject works at'),
    ('studies_at',    'multi',      'An institution the subject studies at'),
    ('researches',    'multi',      'A research area/topic of the subject'),
    ('teaches',       'multi',      'A subject/course the subject teaches'),
    ('works_on',      'multi',      'A project/effort the subject is working on'),
    ('uses_tool',     'multi',      'A tool/technology the subject uses'),
    ('prefers',       'multi',      'A stated preference (co-true across topics)'),
    ('avoids',        'multi',      'Something the subject avoids/dislikes'),
    ('owns',          'multi',      'A machine/asset the subject owns'),
    ('current_focus', 'functional', 'The subject''s single current primary focus'),
    ('has_name',      'functional', 'The canonical name of the subject');

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_fact_sources (
    fact_id         INTEGER NOT NULL REFERENCES memory_facts(id),
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    asserted_at     TEXT    NOT NULL,
    PRIMARY KEY (fact_id, conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_fact_sources_conv
    ON memory_fact_sources(conversation_id);

CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0(
    fact_id   INTEGER PRIMARY KEY,
    embedding float[1024]
);

CREATE TABLE IF NOT EXISTS memory_profile (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    content      TEXT,
    generated_at TEXT,
    source_count INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO memory_profile (id, content, generated_at, source_count)
    VALUES (1, NULL, NULL, 0);
