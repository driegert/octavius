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
