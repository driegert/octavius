import sqlite3
import unittest
from unittest.mock import patch

import history_store as store


class HistoryStoreTests(unittest.TestCase):
    def test_save_and_get_saved_item_round_trip(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE saved_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_url TEXT,
                metadata TEXT,
                status TEXT NOT NULL,
                chat_conversation_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )"""
        )
        with patch.object(store, "store_embedding", return_value=None):
            item_id = store.save_item(
                conn,
                item_type="note",
                title="Title",
                content="Body",
                metadata={"a": 1},
            )
        item = store.get_saved_item(conn, item_id)
        self.assertEqual(item["title"], "Title")
        self.assertEqual(item["metadata"], {"a": 1})

    @staticmethod
    def _messages_conn():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE messages (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id   INTEGER NOT NULL,
                role              TEXT    NOT NULL,
                content           TEXT    NOT NULL,
                created_at        TEXT    NOT NULL,
                model             TEXT,
                input_tokens      INTEGER,
                output_tokens     INTEGER,
                latency_ms        INTEGER,
                stt_model         TEXT,
                stt_confidence    REAL,
                audio_duration_ms INTEGER,
                tts_model         TEXT,
                error             TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE tool_calls (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id      INTEGER NOT NULL,
                tool_name       TEXT    NOT NULL,
                server_name     TEXT,
                arguments       TEXT,
                status          TEXT    NOT NULL DEFAULT 'success',
                result_summary  TEXT,
                result_size     INTEGER,
                duration_ms     INTEGER,
                created_at      TEXT    NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE attachments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id      INTEGER NOT NULL,
                type            TEXT    NOT NULL,
                reference       TEXT    NOT NULL,
                title           TEXT,
                created_at      TEXT    NOT NULL
            )"""
        )
        return conn

    def test_get_conversation_messages_includes_attachments_when_present(self):
        conn = self._messages_conn()
        conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, created_at)
               VALUES (1, 1, 'user', '[image: cat.png]', 't1')"""
        )
        conn.execute(
            """INSERT INTO attachments (message_id, type, reference, title, created_at)
               VALUES (1, 'image', '/spool/cat.png', 'cat.png', 't1')"""
        )
        conn.commit()

        messages = store.get_conversation_messages(conn, 1)

        self.assertEqual(len(messages), 1)
        self.assertEqual(
            messages[0]["attachments"],
            [{"type": "image", "reference": "/spool/cat.png", "title": "cat.png"}],
        )

    def test_get_conversation_messages_omits_attachments_key_when_absent(self):
        conn = self._messages_conn()
        conn.execute(
            """INSERT INTO messages (id, conversation_id, role, content, created_at)
               VALUES (1, 1, 'user', 'hello', 't1')"""
        )
        conn.commit()

        messages = store.get_conversation_messages(conn, 1)

        self.assertEqual(len(messages), 1)
        self.assertNotIn("attachments", messages[0])

    def test_update_saved_item_status_returns_true_for_existing_item(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE saved_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER,
                item_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source_url TEXT,
                metadata TEXT,
                status TEXT NOT NULL,
                chat_conversation_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO saved_items
               (item_type, title, content, status, created_at)
               VALUES ('note', 'T', 'C', 'pending', 'now')"""
        )
        conn.commit()
        self.assertTrue(store.update_saved_item_status(conn, 1, "done"))
        self.assertEqual(conn.execute("SELECT status FROM saved_items WHERE id = 1").fetchone()[0], "done")


class ConversationLookupTests(unittest.TestCase):
    @staticmethod
    def _conversations_conn():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE conversations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL UNIQUE,
                started_at    TEXT NOT NULL,
                ended_at      TEXT,
                service       TEXT NOT NULL,
                source        TEXT NOT NULL,
                summary       TEXT,
                model         TEXT,
                message_count INTEGER DEFAULT 0
            )"""
        )
        conn.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute(
            """CREATE TABLE conversation_tags (
                conversation_id INTEGER, tag_id INTEGER
            )"""
        )
        rows = [
            ("aaaa1111", "2026-07-19T12:00:00+00:00", "octavius", "voice", "Gutter talk"),
            ("bbbb2222", "2026-07-20T13:00:00+00:00", "octavius", "voice", "Multitaper chat"),
            ("cccc3333", "2026-07-20T14:00:00+00:00", "octavius", "matrix", "Matrix thread"),
            ("dddd4444", "2026-07-20T15:00:00+00:00", "claude-code", "cli", "Other service"),
        ]
        for session_id, started, service, source, summary in rows:
            conn.execute(
                """INSERT INTO conversations
                   (session_id, started_at, service, source, summary, message_count)
                   VALUES (?, ?, ?, ?, ?, 2)""",
                (session_id, started, service, source, summary),
            )
        # orphan row left by a WS connect that attached elsewhere / never spoke
        conn.execute(
            """INSERT INTO conversations
               (session_id, started_at, service, source, summary, message_count)
               VALUES ('eeee5555', '2026-07-20T16:00:00+00:00', 'octavius', 'voice',
                       NULL, 0)"""
        )
        conn.commit()
        return conn

    def test_list_conversations_orders_and_filters(self):
        conn = self._conversations_conn()
        results = store.list_conversations(conn, service="octavius")
        self.assertEqual(
            [r["summary"] for r in results],
            ["Matrix thread", "Multitaper chat", "Gutter talk"],
        )
        voice_only = store.list_conversations(conn, service="octavius", source="voice")
        self.assertEqual(
            [r["summary"] for r in voice_only], ["Multitaper chat", "Gutter talk"]
        )
        today = store.list_conversations(
            conn, service="octavius", since="2026-07-20T00:00:00+00:00"
        )
        self.assertEqual(
            [r["summary"] for r in today], ["Matrix thread", "Multitaper chat"]
        )

    def test_get_conversation_round_trip_and_missing(self):
        conn = self._conversations_conn()
        meta = store.get_conversation(conn, 2)
        self.assertEqual(meta["summary"], "Multitaper chat")
        self.assertEqual(meta["source"], "voice")
        self.assertEqual(meta["session_id"], "bbbb2222"[:8])
        self.assertIsNone(store.get_conversation(conn, 999))

    def test_search_conversations_like_path_applies_filters(self):
        conn = self._conversations_conn()
        with patch.object(store, "embed_text", return_value=None):
            hits = store.search_conversations(
                conn, "a", service="octavius", source="voice",
                since="2026-07-20T00:00:00+00:00",
            )
        self.assertEqual([r["summary"] for r in hits], ["Multitaper chat"])


class MemoryWatermarkTests(unittest.TestCase):
    """The watermark helpers moved here from memory.store when the memory
    service was extracted to the agent-memory repo — history.py's push path
    imports them from history_store, so they must exist and behave."""

    @staticmethod
    def _conn():
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                last_extracted_message_id INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL
            )"""
        )
        conn.execute("INSERT INTO conversations (last_extracted_message_id) VALUES (NULL)")
        for role, content in [
            ("user", "hi"),
            ("assistant", "hello"),
            ("tool", "SECRET tool payload"),
            ("user", "bye"),
        ]:
            conn.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (1, ?, ?)",
                (role, content),
            )
        conn.commit()
        return conn

    def test_watermark_defaults_to_zero_and_round_trips(self):
        conn = self._conn()
        self.assertEqual(store.get_memory_watermark(conn, 1), 0)
        self.assertEqual(store.get_memory_watermark(conn, 999), 0)  # unknown conv
        store.set_memory_watermark(conn, 1, 4)
        self.assertEqual(store.get_memory_watermark(conn, 1), 4)

    def test_messages_after_watermark_excludes_tool_turns(self):
        conn = self._conn()
        msgs, max_id = store.messages_after_watermark(conn, 1, 0)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(max_id, 4)
        self.assertNotIn("SECRET", " ".join(m["content"] for m in msgs))

    def test_messages_after_watermark_respects_watermark(self):
        conn = self._conn()
        msgs, max_id = store.messages_after_watermark(conn, 1, 2)
        self.assertEqual([m["content"] for m in msgs], ["bye"])
        self.assertEqual(max_id, 4)
        # Nothing new past the watermark: max_id stays at the watermark.
        msgs, max_id = store.messages_after_watermark(conn, 1, 4)
        self.assertEqual(msgs, [])
        self.assertEqual(max_id, 4)


if __name__ == "__main__":
    unittest.main()
