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
    spawn_embedding,
    store_embedding_async,
    store_embedding,
)
from history_store import (
    get_conversation_messages,
    get_item_chat_conversation_id,
    get_memory_watermark,
    get_saved_item,
    get_stats,
    list_saved_items,
    messages_after_watermark,
    save_item,
    search_conversations,
    search_messages,
    search_messages_text,
    search_saved_items,
    set_item_chat_conversation,
    set_memory_watermark,
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
    _run_migrations(conn)
    conn.commit()
    log.info("History database ready at %s", db_path)
    return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent column additions that ``CREATE TABLE IF NOT EXISTS`` can't express.

    ``ALTER TABLE ... ADD COLUMN`` errors if the column already exists, so it can't
    live in schema.sql's executescript (which runs on every startup). Guard each
    add with a PRAGMA table_info check instead.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    if "last_extracted_message_id" not in cols:
        # Watermark for the memory write path: each extraction pass only mines
        # messages.id > this value, so re-entered durable threads don't re-extract.
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN last_extracted_message_id INTEGER"
        )
        log.info("Migration: added conversations.last_extracted_message_id")
    if "indexed" not in cols:
        # Records whether the summariser judged this conversation worth indexing.
        # Without it a missing summary_embeddings row is ambiguous — "skipped on
        # purpose" and "the embedder was down" look identical, so the sweeper
        # can't tell which rows to repair. NULL means legacy/unknown and is
        # never swept.
        conn.execute("ALTER TABLE conversations ADD COLUMN indexed INTEGER")
        log.info("Migration: added conversations.indexed")


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
                           model: str | None = None,
                           session_id: str | None = None) -> "ConversationSession":
        conn = self.connect()
        session_id = session_id or uuid.uuid4().hex
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

    def resume_or_start_conversation(self, key: str,
                                     service: str = "octavius",
                                     source: str = "matrix",
                                     model: str | None = None) -> "ConversationSession":
        """Resume a conversation identified by a client-supplied stable key, or
        start a fresh one keyed on it.

        The key is stored in the UNIQUE conversations.session_id column, giving a
        permanent 1:1 between an external thread and one conversation record. A
        reconnect re-attaches to the *same* record and appends, so context
        survives idle drops, restarts, and long gaps with no fragmentation.
        """
        conn = self.connect()
        row = conn.execute(
            "SELECT id FROM conversations WHERE session_id = ?", (key,)
        ).fetchone()
        if row is None:
            conn.close()
            return self.start_conversation(service, source, model, session_id=key)
        conv_id = row[0]
        conn.execute(
            "UPDATE conversations SET ended_at = NULL WHERE id = ?", (conv_id,)
        )
        conn.commit()
        log.info("Resumed %s conversation %s (session %s)", service, conv_id, key[:16])
        return ConversationSession(conn, conv_id, key, self.db_path)


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

        # Embeds inline and therefore blocks. That is left as-is: there is no
        # running loop to detach into from a sync caller. Anything on the voice
        # turn path must use add_message_async, which spawns the embed instead.
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
            # Detached on purpose. The message row is already committed, and the
            # vector is not needed by this turn — awaiting it here put a network
            # round-trip in front of the LLM call (user message) and in front of
            # audio_done (assistant message). A dropped embed leaves no
            # message_embeddings row, which is exactly what the sweeper looks
            # for. Note there is no await between the insert and this spawn, so
            # a cancelled turn cannot lose the embed.
            spawn_embedding(self.db_path, "message_embeddings", "message_id", msg_id, content)

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

    def _write_summary(self, result) -> None:
        """Persist the summary and the index decision, dropping any stale vector.

        The DELETE is what keeps "no summary_embeddings row" an honest pending
        marker. Conversations are resumed in place (every Matrix thread), so this
        runs repeatedly on one conversation_id and rewrites `summary`. If the
        re-embed then fails, store_embedding_bytes never reaches its own DELETE,
        the previous vector survives, and a sweeper keyed on absence would call
        the row done forever while it points at superseded text. Dropping it here
        also un-indexes a conversation whose `indexed` flips 1 -> 0.
        """
        self.conn.execute(
            "UPDATE conversations SET summary = ?, indexed = ? WHERE id = ?",
            (result.summary, 1 if result.index else 0, self.conv_id),
        )
        self.conn.execute(
            "DELETE FROM summary_embeddings WHERE conversation_id = ?", (self.conv_id,)
        )
        self.conn.commit()

    def end(self):
        """Finalize the conversation: set ended_at, generate summary and tags."""
        if self._closed:
            return
        self._finalize_conversation_row()

        # Generate summary
        result = generate_summary(self._messages_for_summary)
        if result.summary:
            self._write_summary(result)
            if result.index:
                store_embedding(
                    self.conn, "summary_embeddings", "conversation_id", self.conv_id, result.summary
                )
                log.info("Conversation %d summary: %s", self.conv_id, result.summary[:80])
            else:
                log.info(
                    "Conversation %d not indexed (summary kept): %s",
                    self.conv_id,
                    result.summary[:80],
                )

        # Generate tags
        tags = generate_tags(self._messages_for_summary)
        self._store_tags(tags)

        # Long-term memory: push this (salient) conversation to the memory service.
        if result.index:
            self._push_memory(self.conv_id)

        self.conn.close()
        self._closed = True

    async def end_async(self):
        """Finalize the conversation without blocking the event loop on remote calls."""
        if self._closed:
            return
        self._finalize_conversation_row()

        result = await generate_summary_async(self._messages_for_summary)
        if result.summary:
            self._write_summary(result)
            if result.index:
                await store_embedding_async(
                    self.conn, "summary_embeddings", "conversation_id", self.conv_id, result.summary
                )
                log.info("Conversation %d summary: %s", self.conv_id, result.summary[:80])
            else:
                log.info(
                    "Conversation %d not indexed (summary kept): %s",
                    self.conv_id,
                    result.summary[:80],
                )

        tags = await generate_tags_async(self._messages_for_summary)
        self._store_tags(tags)

        # Long-term memory: push this (salient) conversation to the memory service.
        if result.index:
            await self._push_memory_async(self.conv_id)

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

    def _gather_unsent(self, conn, conv_id: int):
        """Read the user+assistant turns past the local watermark, plus the
        conversation's stable key and summary, for a push to the memory service.

        TRUST BOUNDARY: ``messages_after_watermark`` returns user+assistant turns
        ONLY — tool/email content never crosses to the extractor. Returns
        ``(transcript, max_id, watermark, conv_key, summary)``.
        """
        watermark = get_memory_watermark(conn, conv_id)
        msgs, max_id = messages_after_watermark(conn, conv_id, watermark)
        row = conn.execute(
            "SELECT session_id, summary FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        conv_key = row[0] if row else None
        summary = row[1] if row else None
        return msgs, max_id, watermark, conv_key, summary

    async def _push_memory_async(self, conv_id: int):
        """Push this conversation's new user+assistant turns to the memory service
        and, only on a confirmed push, advance the local watermark. Best-effort:
        a failure leaves the watermark put so the next close retries. Uses its OWN
        short-lived connection (never races the about-to-close ``self.conn``)."""
        try:
            from memory_client import memory_client
        except Exception:
            log.warning("Memory client unavailable; skipping push", exc_info=True)
            return
        if not memory_client.enabled:
            return
        conn = None
        try:
            conn = _connect(self.db_path)
            self._do_push(conn, conv_id, await self._maybe_push_async(conn, conv_id))
        except Exception:
            log.warning("Memory push failed for conversation %d", conv_id, exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    async def _maybe_push_async(self, conn, conv_id):
        from memory_client import memory_client
        msgs, max_id, watermark, conv_key, summary = self._gather_unsent(conn, conv_id)
        if conv_key is None:
            return None
        res = await memory_client.push_conversation(
            conv_key, msgs, summary=summary, ended_at=_now(), index=True)
        return (res, msgs, max_id, watermark, conv_id) if res is not None else None

    def _push_memory(self, conv_id: int):
        """Sync sibling of ``_push_memory_async`` for the legacy sync ``end()``."""
        try:
            from memory_client import memory_client
        except Exception:
            log.warning("Memory client unavailable; skipping push", exc_info=True)
            return
        if not memory_client.enabled:
            return
        conn = None
        try:
            conn = _connect(self.db_path)
            msgs, max_id, watermark, conv_key, summary = self._gather_unsent(conn, conv_id)
            if conv_key is None:
                return
            res = memory_client.push_conversation_sync(
                conv_key, msgs, summary=summary, ended_at=_now(), index=True)
            self._do_push(conn, conv_id,
                          (res, msgs, max_id, watermark, conv_id) if res is not None else None)
        except Exception:
            log.warning("Memory push failed for conversation %d", conv_id, exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    def _do_push(self, conn, conv_id, pushed):
        """Advance the watermark only when the service confirmed ingest of new turns."""
        if pushed is None:
            return
        res, msgs, max_id, watermark, _ = pushed
        if msgs:
            set_memory_watermark(conn, conv_id, max_id)
            conn.commit()
            log.info("Memory: pushed conv %d (%d msgs id>%d) → +%s ~%s",
                     conv_id, len(msgs), watermark, res.get("added"), res.get("reinforced"))

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
