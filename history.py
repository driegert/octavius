"""Octavius conversation history — SQLite + sqlite-vec storage."""

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
import sqlite_vec
from settings import settings

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = Path(__file__).parent / "octavius_history.db"

# Embedding config — same as Evangeline (Ollama bge-m3 on workhorse)
OLLAMA_BASE_URL = settings.ollama_base_url
OLLAMA_MODEL = settings.ollama_model
EMBEDDING_TIMEOUT = settings.embedding_timeout

# Summary generation config
SUMMARY_URL = settings.summary_url
SUMMARY_MODEL = settings.summary_model
SUMMARY_FALLBACK_URL = settings.summary_fallback_url
SUMMARY_TIMEOUT = settings.summary_timeout

# Truncation limits
RESULT_SUMMARY_MAX_CHARS = settings.result_summary_max_chars
TAG_GENERATION_MIN_MESSAGES = settings.tag_generation_min_messages


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create tables if they don't exist and return a connection."""
    conn = _connect(db_path)
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    conn.commit()
    log.info("History database ready at %s", db_path)
    return conn


# -- Embedding helpers ---------------------------------------------------------

def _embed(text: str) -> bytes | None:
    """Get a bge-m3 embedding from Ollama. Returns raw bytes or None."""
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/embeddings",
            json={"model": OLLAMA_MODEL, "prompt": text},
            timeout=EMBEDDING_TIMEOUT,
        )
        resp.raise_for_status()
        vec = np.array(resp.json()["embedding"], dtype=np.float32)
        return vec.tobytes()
    except Exception:
        log.debug("Embedding request failed", exc_info=True)
        return None


def _store_embedding(conn: sqlite3.Connection, table: str, id_col: str,
                     row_id: int, text: str):
    """Embed text and store in the given vec0 table. Best-effort."""
    emb = _embed(text)
    if emb is None:
        return
    try:
        conn.execute(f"DELETE FROM {table} WHERE {id_col} = ?", (row_id,))
        conn.execute(
            f"INSERT INTO {table}({id_col}, embedding) VALUES (?, ?)",
            (row_id, emb),
        )
        conn.commit()
    except Exception:
        log.debug("Failed to store embedding in %s", table, exc_info=True)


# -- Summary generation --------------------------------------------------------

SUMMARY_SYSTEM_PROMPT = (
    "Summarize the following conversation in 2-3 sentences. "
    "Focus on the key topics discussed, decisions made, and any actions taken. "
    "Be concise and factual. Do not use markdown formatting. "
    "Do not include any preamble like 'Here is a summary' — just the summary itself."
)


def _generate_summary(messages: list[dict]) -> str | None:
    """Generate a conversation summary using the LLM. Returns text or None."""
    # Build a condensed transcript for the LLM
    transcript_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "system" or not content:
            continue
        # Truncate very long messages in the transcript
        if len(content) > 1000:
            content = content[:1000] + "..."
        transcript_parts.append(f"{role}: {content}")

    if not transcript_parts:
        return None

    transcript = "\n".join(transcript_parts)
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
    }

    for url in (SUMMARY_URL, SUMMARY_FALLBACK_URL):
        try:
            resp = requests.post(url, json=payload, timeout=SUMMARY_TIMEOUT)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            # Strip <think> tags if present (Qwen3.5)
            import re
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            return text if text else None
        except Exception:
            log.debug("Summary generation failed via %s", url, exc_info=True)
            continue

    return None


# -- Tag generation ------------------------------------------------------------

TAG_SYSTEM_PROMPT = (
    "Extract 1-5 short topic tags from this conversation. "
    "Return ONLY a JSON array of lowercase strings, e.g. [\"statistics\", \"email\"]. "
    "No explanation, no markdown, just the JSON array."
)


def _generate_tags(messages: list[dict]) -> list[str]:
    """Generate topic tags for a conversation. Returns list of tag strings."""
    transcript_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "system" or not content:
            continue
        if len(content) > 500:
            content = content[:500] + "..."
        transcript_parts.append(f"{role}: {content}")

    if len(transcript_parts) < TAG_GENERATION_MIN_MESSAGES:
        return []

    transcript = "\n".join(transcript_parts)
    payload = {
        "model": SUMMARY_MODEL,
        "messages": [
            {"role": "system", "content": TAG_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        "max_tokens": 768,
        "temperature": 0.2,
    }

    for url in (SUMMARY_URL, SUMMARY_FALLBACK_URL):
        try:
            resp = requests.post(url, json=payload, timeout=SUMMARY_TIMEOUT)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            import re
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
            tags = json.loads(text)
            if isinstance(tags, list):
                return [str(t).lower().strip() for t in tags if t][:5]
        except Exception:
            log.debug("Tag generation failed via %s", url, exc_info=True)
            continue

    return []


# -- Core recording API --------------------------------------------------------

class HistoryRecorder:
    """Records conversation turns to the history database.

    Usage:
        recorder = HistoryRecorder(conn)
        session = recorder.start_conversation(service="octavius", source="voice", model="qwen3.5-35b-a3b")

        msg_id = session.add_message(
            role="user", content="What is multitaper spectral estimation?",
            audio_duration_ms=3200, stt_model="whisper-large-v3", stt_confidence=0.95,
        )

        msg_id = session.add_message(
            role="assistant", content="Multitaper spectral estimation is...",
            model="qwen3.5-35b-a3b", latency_ms=1200,
            input_tokens=150, output_tokens=80, tts_model="voxtral-4b",
        )

        session.add_tool_call(
            message_id=msg_id, tool_name="search_works", server_name="openalex",
            arguments={"query": "multitaper"}, status="success",
            result_summary="Found 15 works...", result_size=4200, duration_ms=340,
        )

        session.end()  # generates summary, tags, and embeddings
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start_conversation(self, service: str = "octavius",
                           source: str = "voice",
                           model: str | None = None) -> "ConversationSession":
        session_id = uuid.uuid4().hex
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO conversations (session_id, started_at, service, source, model) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, now, service, source, model),
        )
        self.conn.commit()
        conv_id = cursor.lastrowid
        log.info("Started %s conversation %s (session %s)", service, conv_id, session_id[:8])
        return ConversationSession(self.conn, conv_id, session_id)


class ConversationSession:
    """Tracks a single conversation's messages and metadata."""

    def __init__(self, conn: sqlite3.Connection, conv_id: int, session_id: str):
        self.conn = conn
        self.conv_id = conv_id
        self.session_id = session_id
        self._start_time = time.monotonic()
        self._messages_for_summary: list[dict] = []

    def add_message(
        self,
        role: str,
        content: str,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        latency_ms: int | None = None,
        parent_message_id: int | None = None,
        is_retry: bool = False,
        error: str | None = None,
        stt_model: str | None = None,
        stt_confidence: float | None = None,
        audio_duration_ms: int | None = None,
        tts_model: str | None = None,
    ) -> int:
        """Record a message and return its ID."""
        now = _now()
        cursor = self.conn.execute(
            """INSERT INTO messages (
                conversation_id, role, content, created_at, model,
                input_tokens, output_tokens, latency_ms,
                parent_message_id, is_retry, error,
                stt_model, stt_confidence, audio_duration_ms, tts_model
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.conv_id, role, content, now, model,
                input_tokens, output_tokens, latency_ms,
                parent_message_id, int(is_retry), error,
                stt_model, stt_confidence, audio_duration_ms, tts_model,
            ),
        )
        self.conn.commit()
        msg_id = cursor.lastrowid

        # Update conversation counters
        self.conn.execute(
            "UPDATE conversations SET message_count = message_count + 1 WHERE id = ?",
            (self.conv_id,),
        )
        if input_tokens:
            self.conn.execute(
                "UPDATE conversations SET total_input_tokens = total_input_tokens + ? WHERE id = ?",
                (input_tokens, self.conv_id),
            )
        if output_tokens:
            self.conn.execute(
                "UPDATE conversations SET total_output_tokens = total_output_tokens + ? WHERE id = ?",
                (output_tokens, self.conv_id),
            )
        self.conn.commit()

        # Track for summary generation
        self._messages_for_summary.append({"role": role, "content": content})

        # Embed user and assistant messages (best-effort, non-blocking)
        if role in ("user", "assistant") and content:
            _store_embedding(self.conn, "message_embeddings", "message_id",
                             msg_id, content)

        return msg_id

    def add_tool_call(
        self,
        message_id: int,
        tool_name: str,
        server_name: str | None = None,
        arguments: dict | None = None,
        status: str = "success",
        result_summary: str | None = None,
        result_size: int | None = None,
        duration_ms: int | None = None,
    ) -> int:
        """Record a tool call and return its ID."""
        now = _now()
        args_json = json.dumps(arguments) if arguments else None
        if result_summary and len(result_summary) > RESULT_SUMMARY_MAX_CHARS:
            result_summary = result_summary[:RESULT_SUMMARY_MAX_CHARS] + "..."
        cursor = self.conn.execute(
            """INSERT INTO tool_calls (
                message_id, tool_name, server_name, arguments,
                status, result_summary, result_size, duration_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id, tool_name, server_name, args_json,
                status, result_summary, result_size, duration_ms, now,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def add_attachment(
        self,
        message_id: int,
        type: str,
        reference: str,
        title: str | None = None,
    ) -> int:
        """Record an attachment/reference and return its ID."""
        now = _now()
        cursor = self.conn.execute(
            "INSERT INTO attachments (message_id, type, reference, title, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (message_id, type, reference, title, now),
        )
        self.conn.commit()
        return cursor.lastrowid

    def end(self):
        """Finalize the conversation: set ended_at, generate summary and tags."""
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        now = _now()

        self.conn.execute(
            "UPDATE conversations SET ended_at = ?, total_duration_ms = ? WHERE id = ?",
            (now, elapsed_ms, self.conv_id),
        )
        self.conn.commit()

        # Generate summary
        summary = _generate_summary(self._messages_for_summary)
        if summary:
            self.conn.execute(
                "UPDATE conversations SET summary = ? WHERE id = ?",
                (summary, self.conv_id),
            )
            self.conn.commit()
            _store_embedding(self.conn, "summary_embeddings",
                             "conversation_id", self.conv_id, summary)
            log.info("Conversation %d summary: %s", self.conv_id, summary[:80])

        # Generate tags
        tags = _generate_tags(self._messages_for_summary)
        for tag_name in tags:
            self.conn.execute(
                "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag_name,)
            )
            tag_row = self.conn.execute(
                "SELECT id FROM tags WHERE name = ?", (tag_name,)
            ).fetchone()
            if tag_row:
                self.conn.execute(
                    "INSERT OR IGNORE INTO conversation_tags (conversation_id, tag_id) "
                    "VALUES (?, ?)",
                    (self.conv_id, tag_row[0]),
                )
        self.conn.commit()
        if tags:
            log.info("Conversation %d tags: %s", self.conv_id, tags)


# -- Query API -----------------------------------------------------------------

def search_messages(conn: sqlite3.Connection, query: str,
                    limit: int = 20) -> list[dict]:
    """Semantic search over message history."""
    query_bytes = _embed(query)
    if query_bytes is None:
        # Fall back to text search
        return search_messages_text(conn, query, limit)

    rows = conn.execute(
        """SELECT m.id, m.conversation_id, m.role, m.content, m.created_at,
                  m.model, vec_distance_cosine(me.embedding, ?) as distance
           FROM messages m
           JOIN message_embeddings me ON m.id = me.message_id
           WHERE me.embedding IS NOT NULL
           ORDER BY distance ASC
           LIMIT ?""",
        (query_bytes, limit),
    ).fetchall()

    return [
        {
            "message_id": r[0], "conversation_id": r[1], "role": r[2],
            "content": r[3][:300], "created_at": r[4], "model": r[5],
            "distance": r[6],
        }
        for r in rows
    ]


def search_messages_text(conn: sqlite3.Connection, query: str,
                         limit: int = 20) -> list[dict]:
    """Keyword search over message content."""
    pattern = f"%{query}%"
    rows = conn.execute(
        """SELECT id, conversation_id, role, content, created_at, model
           FROM messages
           WHERE content LIKE ?
           ORDER BY created_at DESC
           LIMIT ?""",
        (pattern, limit),
    ).fetchall()

    return [
        {
            "message_id": r[0], "conversation_id": r[1], "role": r[2],
            "content": r[3][:300], "created_at": r[4], "model": r[5],
        }
        for r in rows
    ]


def search_conversations(conn: sqlite3.Connection, query: str,
                         service: str | None = None,
                         limit: int = 10) -> list[dict]:
    """Semantic search over conversation summaries. Optionally filter by service."""
    query_bytes = _embed(query)
    if query_bytes is None:
        pattern = f"%{query}%"
        sql = """SELECT id, session_id, started_at, ended_at, service, source,
                        summary, model, message_count
                 FROM conversations
                 WHERE summary LIKE ?"""
        params: list = [pattern]
        if service:
            sql += " AND service = ?"
            params.append(service)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    else:
        sql = """SELECT c.id, c.session_id, c.started_at, c.ended_at,
                        c.service, c.source, c.summary, c.model, c.message_count,
                        vec_distance_cosine(se.embedding, ?) as distance
                 FROM conversations c
                 JOIN summary_embeddings se ON c.id = se.conversation_id
                 WHERE se.embedding IS NOT NULL"""
        params = [query_bytes]
        if service:
            sql += " AND c.service = ?"
            params.append(service)
        sql += " ORDER BY distance ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

    results = []
    for r in rows:
        d = {
            "conversation_id": r[0], "session_id": r[1][:8],
            "started_at": r[2], "ended_at": r[3],
            "service": r[4], "source": r[5],
            "summary": r[6], "model": r[7], "message_count": r[8],
        }
        if len(r) > 9:
            d["distance"] = r[9]
        # Fetch tags
        tags = conn.execute(
            """SELECT t.name FROM tags t
               JOIN conversation_tags ct ON t.id = ct.tag_id
               WHERE ct.conversation_id = ?""",
            (r[0],),
        ).fetchall()
        d["tags"] = [t[0] for t in tags]
        results.append(d)
    return results


def get_conversation_messages(conn: sqlite3.Connection,
                              conversation_id: int) -> list[dict]:
    """Get all messages for a conversation, with tool calls."""
    rows = conn.execute(
        """SELECT id, role, content, created_at, model, input_tokens,
                  output_tokens, latency_ms, stt_model, stt_confidence,
                  audio_duration_ms, tts_model, error
           FROM messages
           WHERE conversation_id = ?
           ORDER BY created_at""",
        (conversation_id,),
    ).fetchall()

    messages = []
    for r in rows:
        msg = {
            "message_id": r[0], "role": r[1], "content": r[2],
            "created_at": r[3], "model": r[4],
            "input_tokens": r[5], "output_tokens": r[6],
            "latency_ms": r[7],
        }
        # Include voice metadata if present
        if r[8]:
            msg["stt_model"] = r[8]
        if r[9] is not None:
            msg["stt_confidence"] = r[9]
        if r[10]:
            msg["audio_duration_ms"] = r[10]
        if r[11]:
            msg["tts_model"] = r[11]
        if r[12]:
            msg["error"] = r[12]

        # Fetch tool calls for this message
        tc_rows = conn.execute(
            """SELECT tool_name, server_name, arguments, status,
                      result_summary, result_size, duration_ms
               FROM tool_calls WHERE message_id = ?""",
            (r[0],),
        ).fetchall()
        if tc_rows:
            msg["tool_calls"] = [
                {
                    "tool_name": tc[0], "server_name": tc[1],
                    "arguments": tc[2], "status": tc[3],
                    "result_summary": tc[4], "result_size": tc[5],
                    "duration_ms": tc[6],
                }
                for tc in tc_rows
            ]

        messages.append(msg)
    return messages


# -- Saved Items (Knowledge Inbox) API -------------------------------------------

def save_item(
    conn: sqlite3.Connection,
    item_type: str,
    title: str,
    content: str,
    conversation_id: int | None = None,
    source_url: str | None = None,
    metadata: dict | None = None,
) -> int:
    """Save an item to the knowledge inbox. Returns the item ID."""
    now = _now()
    metadata_json = json.dumps(metadata) if metadata else None
    cursor = conn.execute(
        """INSERT INTO saved_items
           (conversation_id, item_type, title, content, source_url, metadata, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (conversation_id, item_type, title, content, source_url, metadata_json, now),
    )
    conn.commit()
    item_id = cursor.lastrowid
    log.info("Saved inbox item %d: [%s] %s", item_id, item_type, title[:60])

    # Embed title + content for semantic search
    embed_text = f"{title}\n{content[:500]}"
    _store_embedding(conn, "saved_item_embeddings", "saved_item_id", item_id, embed_text)

    return item_id


def list_saved_items(
    conn: sqlite3.Connection,
    status: str | None = None,
    item_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """List saved items, optionally filtered by status and/or type."""
    sql = "SELECT id, conversation_id, item_type, title, content, source_url, metadata, status, created_at, updated_at FROM saved_items WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if item_type:
        sql += " AND item_type = ?"
        params.append(item_type)
    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": r[0], "conversation_id": r[1], "item_type": r[2],
            "title": r[3], "content": r[4][:200], "source_url": r[5],
            "metadata": json.loads(r[6]) if r[6] else None,
            "status": r[7], "created_at": r[8], "updated_at": r[9],
        }
        for r in rows
    ]


def search_saved_items(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Semantic search over saved items."""
    query_bytes = _embed(query)
    if query_bytes is None:
        # Fall back to text search
        pattern = f"%{query}%"
        rows = conn.execute(
            """SELECT id, conversation_id, item_type, title, content, source_url,
                      metadata, status, created_at
               FROM saved_items
               WHERE (title LIKE ? OR content LIKE ?) AND status != 'dismissed'
               ORDER BY created_at DESC LIMIT ?""",
            (pattern, pattern, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.id, s.conversation_id, s.item_type, s.title, s.content,
                      s.source_url, s.metadata, s.status, s.created_at,
                      vec_distance_cosine(e.embedding, ?) as distance
               FROM saved_items s
               JOIN saved_item_embeddings e ON s.id = e.saved_item_id
               WHERE s.status != 'dismissed'
               ORDER BY distance ASC LIMIT ?""",
            (query_bytes, limit),
        ).fetchall()

    results = []
    for r in rows:
        d = {
            "id": r[0], "conversation_id": r[1], "item_type": r[2],
            "title": r[3], "content": r[4][:200], "source_url": r[5],
            "metadata": json.loads(r[6]) if r[6] else None,
            "status": r[7], "created_at": r[8],
        }
        if len(r) > 9:
            d["distance"] = r[9]
        results.append(d)
    return results


def get_saved_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    """Get a single saved item with full content."""
    row = conn.execute(
        """SELECT id, conversation_id, item_type, title, content, source_url,
                  metadata, status, created_at, updated_at
           FROM saved_items WHERE id = ?""",
        (item_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "conversation_id": row[1], "item_type": row[2],
        "title": row[3], "content": row[4], "source_url": row[5],
        "metadata": json.loads(row[6]) if row[6] else None,
        "status": row[7], "created_at": row[8], "updated_at": row[9],
    }


def update_saved_item_status(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
) -> bool:
    """Update a saved item's status. Returns True if the item existed."""
    now = _now()
    cursor = conn.execute(
        "UPDATE saved_items SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, item_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def set_item_chat_conversation(conn: sqlite3.Connection, item_id: int,
                                conversation_id: int):
    """Link a chat conversation to a saved item."""
    conn.execute(
        "UPDATE saved_items SET chat_conversation_id = ? WHERE id = ?",
        (conversation_id, item_id),
    )
    conn.commit()


def get_item_chat_conversation_id(conn: sqlite3.Connection, item_id: int) -> int | None:
    """Get the chat conversation ID for a saved item, if any."""
    row = conn.execute(
        "SELECT chat_conversation_id FROM saved_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    return row[0] if row and row[0] else None


def get_stats(conn: sqlite3.Connection) -> dict:
    """Overview stats for the history database."""
    total_convs = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    total_msgs = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    total_tool_calls = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    total_embeddings = conn.execute(
        "SELECT COUNT(*) FROM message_embeddings"
    ).fetchone()[0]

    by_service = conn.execute(
        "SELECT service, COUNT(*) FROM conversations GROUP BY service"
    ).fetchall()

    by_source = conn.execute(
        "SELECT source, COUNT(*) FROM conversations GROUP BY source"
    ).fetchall()

    by_role = conn.execute(
        "SELECT role, COUNT(*) FROM messages GROUP BY role"
    ).fetchall()

    top_tools = conn.execute(
        """SELECT tool_name, COUNT(*) as cnt FROM tool_calls
           GROUP BY tool_name ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()

    top_tags = conn.execute(
        """SELECT t.name, COUNT(*) as cnt FROM tags t
           JOIN conversation_tags ct ON t.id = ct.tag_id
           GROUP BY t.name ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()

    return {
        "total_conversations": total_convs,
        "total_messages": total_msgs,
        "total_tool_calls": total_tool_calls,
        "total_embeddings": total_embeddings,
        "embedding_coverage": (
            f"{total_embeddings / total_msgs * 100:.1f}%"
            if total_msgs else "0%"
        ),
        "conversations_by_service": {r[0]: r[1] for r in by_service},
        "conversations_by_source": {r[0]: r[1] for r in by_source},
        "messages_by_role": {r[0]: r[1] for r in by_role},
        "top_tools": {r[0]: r[1] for r in top_tools},
        "top_tags": {r[0]: r[1] for r in top_tags},
    }
