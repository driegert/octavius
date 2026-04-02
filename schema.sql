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
    total_duration_ms   INTEGER DEFAULT 0
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
