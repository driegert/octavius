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


if __name__ == "__main__":
    unittest.main()
