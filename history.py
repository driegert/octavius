"""Octavius conversation history — SQLite + sqlite-vec storage."""

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from db import DEFAULT_DB_PATH, connect as _connect, connect_db
from history_enrichment import (
    RESULT_SUMMARY_MAX_CHARS,
    generate_summary_async,
    generate_summary,
    generate_tags_async,
    generate_tags,
    store_embedding_async,
    store_embedding,
)
from history_store import (
    get_conversation_messages,
    get_item_chat_conversation_id,
    get_saved_item,
    get_stats,
    list_saved_items,
    save_item,
    search_conversations,
    search_messages,
    search_messages_text,
    search_saved_items,
    set_item_chat_conversation,
    update_saved_item_status,
)

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create tables if they don't exist and return a connection."""
    conn = _connect(db_path)
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    conn.commit()
    log.info("History database ready at %s", db_path)
    return conn


# -- Core recording API --------------------------------------------------------

class HistoryRecorder:
    """Records conversation turns to the history database.

    Usage:
        recorder = HistoryRecorder(conn)
        session = recorder.start_conversation(service="octavius", source="voice", model="qwen3.6-35b-a3b")

        msg_id = session.add_message(
            role="user", content="What is multitaper spectral estimation?",
            audio_duration_ms=3200, stt_model="whisper-large-v3", stt_confidence=0.95,
        )

        msg_id = session.add_message(
            role="assistant", content="Multitaper spectral estimation is...",
            model="qwen3.6-35b-a3b", latency_ms=1200,
            input_tokens=150, output_tokens=80, tts_model="voxtral-4b",
        )

        session.add_tool_call(
            message_id=msg_id, tool_name="search_works", server_name="openalex",
            arguments={"query": "multitaper"}, status="success",
            result_summary="Found 15 works...", result_size=4200, duration_ms=340,
        )

        await session.end_async()  # generates summary, tags, and embeddings
    """

    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    def start_conversation(self, service: str = "octavius",
                           source: str = "voice",
                           model: str | None = None) -> "ConversationSession":
        conn = self.connect()
        session_id = uuid.uuid4().hex
        now = _now()
        cursor = conn.execute(
            "INSERT INTO conversations (session_id, started_at, service, source, model) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, now, service, source, model),
        )
        conn.commit()
        conv_id = cursor.lastrowid
        log.info("Started %s conversation %s (session %s)", service, conv_id, session_id[:8])
        return ConversationSession(conn, conv_id, session_id, self.db_path)


class ConversationSession:
    """Tracks a single conversation's messages and metadata."""

    def __init__(self, conn: sqlite3.Connection, conv_id: int, session_id: str, db_path: Path):
        self.conn = conn
        self.conv_id = conv_id
        self.session_id = session_id
        self.db_path = Path(db_path)
        self._start_time = time.monotonic()
        self._messages_for_summary: list[dict] = []
        self._closed = False

    def connect(self) -> sqlite3.Connection:
        return _connect(self.db_path)

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
        msg_id = self._insert_message(
            role=role,
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            parent_message_id=parent_message_id,
            is_retry=is_retry,
            error=error,
            stt_model=stt_model,
            stt_confidence=stt_confidence,
            audio_duration_ms=audio_duration_ms,
            tts_model=tts_model,
        )

        # Embed user and assistant messages (best-effort, non-blocking)
        if role in ("user", "assistant") and content:
            store_embedding(self.conn, "message_embeddings", "message_id", msg_id, content)

        return msg_id

    async def add_message_async(
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
        msg_id = self._insert_message(
            role=role,
            content=content,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            parent_message_id=parent_message_id,
            is_retry=is_retry,
            error=error,
            stt_model=stt_model,
            stt_confidence=stt_confidence,
            audio_duration_ms=audio_duration_ms,
            tts_model=tts_model,
        )

        if role in ("user", "assistant") and content:
            await store_embedding_async(self.conn, "message_embeddings", "message_id", msg_id, content)

        return msg_id

    def _insert_message(
        self,
        *,
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
        if self._closed:
            return
        self._finalize_conversation_row()

        # Generate summary
        summary = generate_summary(self._messages_for_summary)
        if summary:
            self.conn.execute(
                "UPDATE conversations SET summary = ? WHERE id = ?",
                (summary, self.conv_id),
            )
            self.conn.commit()
            store_embedding(self.conn, "summary_embeddings", "conversation_id", self.conv_id, summary)
            log.info("Conversation %d summary: %s", self.conv_id, summary[:80])

        # Generate tags
        tags = generate_tags(self._messages_for_summary)
        self._store_tags(tags)
        self.conn.close()
        self._closed = True

    async def end_async(self):
        """Finalize the conversation without blocking the event loop on remote calls."""
        if self._closed:
            return
        self._finalize_conversation_row()

        summary = await generate_summary_async(self._messages_for_summary)
        if summary:
            self.conn.execute(
                "UPDATE conversations SET summary = ? WHERE id = ?",
                (summary, self.conv_id),
            )
            self.conn.commit()
            await store_embedding_async(self.conn, "summary_embeddings", "conversation_id", self.conv_id, summary)
            log.info("Conversation %d summary: %s", self.conv_id, summary[:80])

        tags = await generate_tags_async(self._messages_for_summary)
        self._store_tags(tags)
        self.conn.close()
        self._closed = True

    def _finalize_conversation_row(self):
        if self._closed:
            return
        elapsed_ms = int((time.monotonic() - self._start_time) * 1000)
        now = _now()

        self.conn.execute(
            "UPDATE conversations SET ended_at = ?, total_duration_ms = ? WHERE id = ?",
            (now, elapsed_ms, self.conv_id),
        )
        self.conn.commit()

    def _store_tags(self, tags: list[str]):
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
