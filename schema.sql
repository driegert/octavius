-- Octavius Conversation History — SQLite + sqlite-vec Schema

-- Conversations (session-level grouping)
CREATE TABLE IF NOT EXISTS conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT    NOT NULL UNIQUE,
    started_at          TEXT    NOT NULL,         -- ISO 8601
    ended_at            TEXT,
    service             TEXT    NOT NULL,         -- 'octavius', 'claude-code', 'chatgpt', etc.
    source              TEXT    NOT NULL,         -- 'voice', 'text', 'api', 'web', 'cli'
    summary             TEXT,
    model               TEXT,                     -- primary model for this session
    message_count       INTEGER DEFAULT 0,
    total_input_tokens  INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_duration_ms   INTEGER DEFAULT 0,
    indexed             INTEGER                   -- 1 = summary belongs in semantic
                                                  -- search, 0 = deliberately skipped,
                                                  -- NULL = legacy/unknown (never swept)
);

CREATE INDEX IF NOT EXISTS idx_conversations_service
    ON conversations(service, started_at);

-- Messages (individual turns)
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   INTEGER NOT NULL REFERENCES conversations(id),
    role              TEXT    NOT NULL,           -- 'user', 'assistant', 'system', 'tool'
    content           TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,           -- ISO 8601
    model             TEXT,                       -- model that produced this turn
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    latency_ms        INTEGER,
    parent_message_id INTEGER REFERENCES messages(id),
    is_retry          INTEGER DEFAULT 0,
    error             TEXT,

    -- Voice-specific (NULL for non-voice turns)
    stt_model         TEXT,
    stt_confidence    REAL,
    audio_duration_ms INTEGER,
    tts_model         TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_role
    ON messages(role, created_at);

-- Tool calls (MCP tool invocations per message)
CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES messages(id),
    tool_name       TEXT    NOT NULL,
    server_name     TEXT,
    arguments       TEXT,                         -- JSON
    status          TEXT    NOT NULL DEFAULT 'success',  -- 'success', 'error', 'timeout'
    result_summary  TEXT,
    result_size     INTEGER,
    duration_ms     INTEGER,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_message
    ON tool_calls(message_id);

CREATE INDEX IF NOT EXISTS idx_tool_calls_name
    ON tool_calls(tool_name);

-- Topic tags (many-to-many)
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS conversation_tags (
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    tag_id          INTEGER NOT NULL REFERENCES tags(id),
    PRIMARY KEY (conversation_id, tag_id)
);

-- Attachments / references
CREATE TABLE IF NOT EXISTS attachments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      INTEGER NOT NULL REFERENCES messages(id),
    type            TEXT    NOT NULL,             -- 'url', 'file', 'document', 'image'
    reference       TEXT    NOT NULL,
    title           TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_message
    ON attachments(message_id);

-- Embeddings (sqlite-vec) — message-level semantic search
CREATE VIRTUAL TABLE IF NOT EXISTS message_embeddings USING vec0(
    message_id INTEGER PRIMARY KEY,
    embedding  float[1024]
);

-- Embeddings (sqlite-vec) — conversation summary search
CREATE VIRTUAL TABLE IF NOT EXISTS summary_embeddings USING vec0(
    conversation_id INTEGER PRIMARY KEY,
    embedding       float[1024]
);

-- Saved items (knowledge inbox)
CREATE TABLE IF NOT EXISTS saved_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   INTEGER REFERENCES conversations(id),
    item_type         TEXT    NOT NULL,        -- 'note', 'search_summary', 'article', 'email_draft'
    title             TEXT    NOT NULL,
    content           TEXT    NOT NULL,         -- full content, NOT truncated
    source_url        TEXT,
    metadata          TEXT,                     -- JSON for type-specific data (e.g. email recipients, subject)
    status            TEXT    NOT NULL DEFAULT 'pending',  -- 'pending', 'done', 'dismissed'
    chat_conversation_id INTEGER REFERENCES conversations(id),
    created_at        TEXT    NOT NULL,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_saved_items_status
    ON saved_items(status, created_at);

-- Embeddings (sqlite-vec) — saved item semantic search
CREATE VIRTUAL TABLE IF NOT EXISTS saved_item_embeddings USING vec0(
    saved_item_id INTEGER PRIMARY KEY,
    embedding     float[1024]
);

-- Reader documents (document-to-speech pipeline)
CREATE TABLE IF NOT EXISTS reader_documents (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT    NOT NULL,
    source_type       TEXT    NOT NULL,        -- 'pdf', 'markdown', 'url', 'inbox_item'
    source_path       TEXT,
    saved_item_id     INTEGER REFERENCES saved_items(id),
    speech_file       TEXT,                    -- path to speech-ready JSON on disk
    original_md_file  TEXT,
    chunk_count       INTEGER NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'processing', -- 'processing', 'ready', 'failed'
    error             TEXT,
    last_chunk        INTEGER NOT NULL DEFAULT 0,
    last_sentence     INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_reader_documents_status
    ON reader_documents(status, created_at);

-- ============================================================================
-- Long-term memory (v1) — graph-lite SPO facts + maintained profile doc.
-- See DESIGN-octavius-memory.md. All additive; the only non-idempotent change
-- (ALTER conversations) is applied by init_db's migration guard, not here.
-- ============================================================================

-- Durable facts: a temporal/provenance-tagged Subject-Predicate-Object row.
-- A row whose object is an entity (object_is_entity=1) IS a graph edge.
CREATE TABLE IF NOT EXISTS memory_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT    NOT NULL,              -- canonical entity string (alias-resolved at write)
    predicate       TEXT    NOT NULL REFERENCES predicates(name),
    object          TEXT    NOT NULL,              -- canonical entity string, or a literal
    object_is_entity INTEGER NOT NULL DEFAULT 0,   -- 1 => object is an entity node (edge), 0 => literal
    confidence      REAL    NOT NULL DEFAULT 0.7,  -- DERIVED: f(trust_tier, #distinct source convs, recency)
    trust_tier      TEXT    NOT NULL DEFAULT 'derived',  -- 'asserted' | 'derived' | 'untrusted' (untrusted empty in v1)
    valid_from      TEXT    NOT NULL,              -- transaction time: when learned (ISO 8601)
    valid_until     TEXT,                          -- set on supersession/forget; NULL => currently live
    superseded_by   INTEGER REFERENCES memory_facts(id),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT
);

-- Live-fact lookups (profile render, per-turn retrieval) filter on valid_until IS NULL.
CREATE INDEX IF NOT EXISTS idx_memory_facts_live
    ON memory_facts(valid_until, trust_tier, confidence);
CREATE INDEX IF NOT EXISTS idx_memory_facts_spo
    ON memory_facts(subject, predicate);

-- Predicate registry. cardinality drives reconciliation:
--   'functional' => a new object for the same (subject,predicate) SUPERSEDES the old.
--   'multi'      => co-true; a new object just INSERTS another row.
CREATE TABLE IF NOT EXISTS predicates (
    name        TEXT PRIMARY KEY,
    cardinality TEXT NOT NULL,                     -- 'functional' | 'multi'
    description TEXT
);

-- Starter predicate registry (idempotent seed; editable later, cardinality is the key knob).
-- Conservative: only genuinely single-valued relations are 'functional'; co-true ones are 'multi'.
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

-- Alias -> canonical entity string, resolved at write time.
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

-- Many-to-one provenance: which conversations asserted a fact. The count of
-- distinct conversations is the reinforcement/trust signal (Model A: 1 thread = 1 conv).
CREATE TABLE IF NOT EXISTS memory_fact_sources (
    fact_id         INTEGER NOT NULL REFERENCES memory_facts(id),
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    asserted_at     TEXT    NOT NULL,
    PRIMARY KEY (fact_id, conversation_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_fact_sources_conv
    ON memory_fact_sources(conversation_id);

-- Embeddings (sqlite-vec) — fact-level: write-time near-dup merge + read-time retrieval.
CREATE VIRTUAL TABLE IF NOT EXISTS fact_embeddings USING vec0(
    fact_id   INTEGER PRIMARY KEY,
    embedding float[1024]
);

-- Maintained profile doc (global synthesis). Single row (id=1):
--   Block 1 (identity) is re-rendered deterministically from live facts at injection time;
--   Block 2 (themes) is the LLM rollup, regenerated on the event counter.
CREATE TABLE IF NOT EXISTS memory_profile (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    content      TEXT,                             -- Block 2 prose (themes/direction); Block 1 rendered live
    generated_at TEXT,
    source_count INTEGER NOT NULL DEFAULT 0        -- salient convs closed since last Block-2 synthesis
);

INSERT OR IGNORE INTO memory_profile (id, content, generated_at, source_count)
    VALUES (1, NULL, NULL, 0);
