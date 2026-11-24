import tempfile
import unittest
from unittest import mock
from pathlib import Path

import history
import history_enrichment


class ResumeOrStartConversationTests(unittest.TestCase):
    """attach-by-key: a stable client key gives a permanent 1:1 conversation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_history.db"
        history.init_db(self.db_path).close()
        self.history = history.HistoryRecorder(self.db_path)
        # No embedding stub needed any more: recording a message is pure SQLite
        # since the embed path was retired (2026-09-15) in favour of
        # history-index.timer, so nothing here reaches a model server.

    def tearDown(self):
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





class ResumeConversationByIdTests(unittest.TestCase):
    """restore_session / load_conversation: reopen a voice record by id and append."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test_history.db"
        history.init_db(self.db_path).close()
        self.history = history.HistoryRecorder(self.db_path)
        self._summary = mock.patch.object(
            history, "generate_summary",
            side_effect=lambda msgs: history_enrichment.SummaryResult(
                summary=f"{len(msgs)} messages" if msgs else None, index=False),
        )
        self._tags = mock.patch.object(history, "generate_tags", return_value=[])
        self.summary_calls = self._summary.start()
        self._tags.start()

    def tearDown(self):
        mock.patch.stopall()
        self._tmp.cleanup()

    def _row(self, conv_id):
        with self.history.connect() as conn:
            return conn.execute(
                "SELECT message_count, summary, ended_at FROM conversations WHERE id = ?",
                (conv_id,),
            ).fetchone()

    def _voice_conversation(self):
        session = self.history.start_conversation(source="voice")
        session.add_message("user", "one")
        session.add_message("assistant", "two")
        session.end()
        return session.conv_id

    def test_two_reconnects_keep_one_record(self):
        conv_id = self._voice_conversation()

        first = self.history.resume_conversation(conv_id)
        self.assertEqual(first.conv_id, conv_id)
        first.add_message("user", "three")
        first.end()

        second = self.history.resume_conversation(conv_id)
        self.assertEqual(second.conv_id, conv_id)
        msgs = history.get_conversation_messages(second.conn, conv_id)
        self.assertEqual([m["content"] for m in msgs], ["one", "two", "three"])
        second.end()

    def test_summary_covers_the_whole_conversation_not_only_new_turns(self):
        conv_id = self._voice_conversation()
        session = self.history.resume_conversation(conv_id)
        session.add_message("user", "three")
        session.end()
        self.assertEqual(self._row(conv_id)[:2], (3, "3 messages"))

    def test_resume_with_no_new_turn_rewrites_nothing(self):
        conv_id = self._voice_conversation()
        before = self._row(conv_id)
        calls = self.summary_calls.call_count
        self.history.resume_conversation(conv_id).end()
        self.assertEqual(self._row(conv_id), before)
        self.assertEqual(self.summary_calls.call_count, calls)

    def test_unknown_and_non_voice_records_are_not_resumed(self):
        self.assertIsNone(self.history.resume_conversation(9999))
        self.assertIsNone(self.history.resume_conversation("1"))
        matrix = self.history.resume_or_start_conversation("thread-1")
        matrix.add_message("user", "hi")
        matrix.conn.close()
        self.assertIsNone(self.history.resume_conversation(matrix.conv_id))
        with self.history.connect() as conn:
            conn.execute(
                "INSERT INTO conversations (session_id, started_at, service, source) "
                "VALUES ('other', '2026-01-01', 'claude-code', 'voice')")
            other = conn.execute("SELECT id FROM conversations WHERE session_id = 'other'").fetchone()[0]
            conn.commit()
        self.assertIsNone(self.history.resume_conversation(other))


if __name__ == "__main__":
    unittest.main()
