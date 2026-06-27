import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import history


class ResumeOrStartConversationTests(unittest.TestCase):
    """attach-by-key: a stable client key gives a permanent 1:1 conversation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_history.db"
        history.init_db(self.db_path).close()
        self.history = history.HistoryRecorder(self.db_path)
        # Embeddings reach out to a model server; stub them for the unit test.
        self._embed = patch.object(history, "store_embedding", return_value=None)
        self._embed.start()

    def tearDown(self):
        self._embed.stop()
        self._tmp.cleanup()

    def test_new_key_creates_conversation_with_key_as_session_id(self):
        session = self.history.resume_or_start_conversation("thread-1")
        self.assertEqual(session.session_id, "thread-1")
        row = session.conn.execute(
            "SELECT session_id FROM conversations WHERE id = ?", (session.conv_id,)
        ).fetchone()
        self.assertEqual(row[0], "thread-1")
        session.conn.close()

    def test_same_key_returns_same_conversation_id(self):
        first = self.history.resume_or_start_conversation("thread-1")
        first.add_message("user", "remember the banana")
        first_id = first.conv_id
        first.conn.close()

        second = self.history.resume_or_start_conversation("thread-1")
        self.assertEqual(second.conv_id, first_id)
        # The earlier message is still attached to the same record.
        rows = second.conn.execute(
            "SELECT content FROM messages WHERE conversation_id = ? ORDER BY id", (first_id,)
        ).fetchall()
        self.assertIn(("remember the banana",), rows)
        second.conn.close()

    def test_distinct_keys_are_isolated(self):
        a = self.history.resume_or_start_conversation("thread-a")
        b = self.history.resume_or_start_conversation("thread-b")
        self.assertNotEqual(a.conv_id, b.conv_id)
        a.conn.close()
        b.conn.close()

    def test_resume_clears_ended_at(self):
        first = self.history.resume_or_start_conversation("thread-1")
        conv_id = first.conv_id
        first.conn.execute(
            "UPDATE conversations SET ended_at = '2026-01-01T00:00:00Z' WHERE id = ?", (conv_id,)
        )
        first.conn.commit()
        first.conn.close()

        resumed = self.history.resume_or_start_conversation("thread-1")
        row = resumed.conn.execute(
            "SELECT ended_at FROM conversations WHERE id = ?", (conv_id,)
        ).fetchone()
        self.assertIsNone(row[0])
        resumed.conn.close()

    def test_start_conversation_still_defaults_to_random_session_id(self):
        a = self.history.start_conversation()
        b = self.history.start_conversation()
        self.assertNotEqual(a.session_id, b.session_id)
        self.assertEqual(len(a.session_id), 32)  # uuid4().hex
        a.conn.close()
        b.conn.close()


if __name__ == "__main__":
    unittest.main()
