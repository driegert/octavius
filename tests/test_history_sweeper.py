import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

import history
import history_sweeper as sweeper
from history_enrichment import SummaryResult
from db import connect_db

VECTOR = np.zeros(1024, dtype=np.float32).tobytes()


def _iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


class SweeperTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "history.db"
        history.init_db(self.db_path).close()
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO conversations (id, session_id, started_at, service, source) "
                "VALUES (1, 's1', ?, 'octavius', 'voice')",
                (_iso(9999),),
            )
            conn.commit()

    def _add_message(self, msg_id, role, content, *, age_seconds=9999):
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO messages (id, conversation_id, role, content, created_at) "
                "VALUES (?, 1, ?, ?, ?)",
                (msg_id, role, content, _iso(age_seconds)),
            )
            conn.commit()

    def _pending_message_ids(self):
        with connect_db(self.db_path) as conn:
            return [row[0] for row in sweeper.find_unembedded_messages(conn)]

    def _vector_count(self, table="message_embeddings"):
        with connect_db(self.db_path) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class FindUnembeddedMessagesTests(SweeperTestBase):
    def test_selects_user_and_assistant_rows_without_vectors(self):
        self._add_message(1, "user", "hello")
        self._add_message(2, "assistant", "hi there")
        self.assertEqual(sorted(self._pending_message_ids()), [1, 2])

    def test_skips_tool_and_system_rows(self):
        """Tool results are never embedded by design; without this filter every
        pass would re-select the whole tool-call history and never converge."""
        self._add_message(1, "tool", "tool output")
        self._add_message(2, "system", "system prompt")
        self.assertEqual(self._pending_message_ids(), [])

    def test_skips_empty_content(self):
        self._add_message(1, "user", "")
        self.assertEqual(self._pending_message_ids(), [])

    def test_skips_rows_younger_than_the_min_age(self):
        """A live detached embed may still be in flight for a fresh row."""
        self._add_message(1, "user", "just now", age_seconds=0)
        self.assertEqual(self._pending_message_ids(), [])

    def test_skips_rows_that_already_have_a_vector(self):
        self._add_message(1, "user", "hello")
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO message_embeddings(message_id, embedding) VALUES (1, ?)",
                (VECTOR,),
            )
            conn.commit()
        self.assertEqual(self._pending_message_ids(), [])


class SweepOnceTests(SweeperTestBase):
    async def test_repairs_backlog_and_converges(self):
        self._add_message(1, "user", "hello")
        self._add_message(2, "assistant", "hi")

        with patch.object(sweeper, "embed_text_async", return_value=VECTOR), \
             patch.object(sweeper, "PAUSE_BETWEEN_ROWS", 0):
            first = await sweeper.sweep_once(self.db_path)
            second = await sweeper.sweep_once(self.db_path)

        self.assertEqual(first["messages"], 2)
        self.assertFalse(first["aborted"])
        # Convergence: a second pass must find nothing left to do.
        self.assertEqual(second["messages"], 0)
        self.assertEqual(self._vector_count(), 2)

    async def test_aborts_the_pass_on_the_first_chain_failure(self):
        """A None means the whole chain is down; grinding through the rest of the
        batch would just burn the timeout budget per row."""
        for i in range(1, 6):
            self._add_message(i, "user", f"msg {i}")

        with patch.object(sweeper, "embed_text_async", return_value=None) as embed, \
             patch.object(sweeper, "PAUSE_BETWEEN_ROWS", 0):
            result = await sweeper.sweep_once(self.db_path)

        self.assertEqual(embed.await_count, 1)
        self.assertTrue(result["aborted"])
        self.assertEqual(result["messages"], 0)

    async def test_empty_backlog_is_a_no_op(self):
        with patch.object(sweeper, "embed_text_async", return_value=VECTOR) as embed:
            result = await sweeper.sweep_once(self.db_path)
        embed.assert_not_called()
        self.assertEqual(result, {"messages": 0, "summaries": 0, "aborted": False})

    async def test_sweep_never_raises_on_a_bad_database(self):
        result = await sweeper.sweep_once(Path(self.tmpdir.name) / "does-not-exist.db")
        self.assertEqual(result["messages"], 0)


class SummarySweepTests(SweeperTestBase):
    def _set_conversation(self, indexed, summary="a summary"):
        with connect_db(self.db_path) as conn:
            conn.execute(
                "UPDATE conversations SET summary = ?, indexed = ? WHERE id = 1",
                (summary, indexed),
            )
            conn.commit()

    def _pending_summary_ids(self):
        with connect_db(self.db_path) as conn:
            return [row[0] for row in sweeper.find_unembedded_summaries(conn)]

    def test_sweeps_conversations_marked_for_indexing(self):
        self._set_conversation(1)
        self.assertEqual(self._pending_summary_ids(), [1])

    def test_skips_deliberately_unindexed_conversations(self):
        self._set_conversation(0)
        self.assertEqual(self._pending_summary_ids(), [])

    def test_skips_legacy_rows_with_unknown_intent(self):
        """NULL predates the flag: 'skipped on purpose' and 'embed failed' are
        indistinguishable, so those rows are left alone."""
        self._set_conversation(None)
        self.assertEqual(self._pending_summary_ids(), [])

    async def test_summary_rewritten_mid_embed_is_not_written_back(self):
        """The embed is an await; a Matrix thread can be re-summarised during it.
        Writing the old vector back would look complete forever and defeat the
        missing-row marker this sweeper depends on."""
        self._set_conversation(1, summary="version one")

        async def embed_then_rewrite(text):
            # Stands in for the live summariser landing while we were embedding.
            self._set_conversation(1, summary="version two")
            return VECTOR

        with patch.object(sweeper, "embed_text_async", side_effect=embed_then_rewrite), \
             patch.object(sweeper, "PAUSE_BETWEEN_ROWS", 0):
            result = await sweeper.sweep_once(self.db_path)

        self.assertEqual(result["summaries"], 0)
        self.assertEqual(self._vector_count("summary_embeddings"), 0)
        # Still pending, so the next pass picks up the *new* text.
        self.assertEqual(self._pending_summary_ids(), [1])

    async def test_summary_unindexed_mid_embed_is_not_written_back(self):
        self._set_conversation(1, summary="keep")

        async def embed_then_unindex(text):
            self._set_conversation(0, summary="keep")
            return VECTOR

        with patch.object(sweeper, "embed_text_async", side_effect=embed_then_unindex), \
             patch.object(sweeper, "PAUSE_BETWEEN_ROWS", 0):
            result = await sweeper.sweep_once(self.db_path)

        self.assertEqual(result["summaries"], 0)
        self.assertEqual(self._vector_count("summary_embeddings"), 0)

    async def test_sweep_repairs_summary_embeddings(self):
        self._set_conversation(1)
        with patch.object(sweeper, "embed_text_async", return_value=VECTOR), \
             patch.object(sweeper, "PAUSE_BETWEEN_ROWS", 0):
            result = await sweeper.sweep_once(self.db_path)
        self.assertEqual(result["summaries"], 1)
        self.assertEqual(self._vector_count("summary_embeddings"), 1)


class SummaryInvalidationTests(SweeperTestBase):
    """A rewritten summary must drop its old vector, or the missing-row pending
    marker lies and the sweeper can never repair a stale embedding."""

    def _session(self):
        recorder = history.HistoryRecorder(self.db_path)
        return recorder.start_conversation(source="matrix", session_id="thread-1")

    def test_rewriting_a_summary_drops_the_stale_vector(self):
        session = self._session()
        result_v1 = SummaryResult(summary="version one", index=True)
        session._write_summary(result_v1)
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO summary_embeddings(conversation_id, embedding) VALUES (?, ?)",
                (session.conv_id, VECTOR),
            )
            conn.commit()

        # Resumed conversation: new summary, and this time the embedder is down.
        session._write_summary(SummaryResult(summary="version two", index=True))
        session.conn.close()

        with connect_db(self.db_path) as conn:
            rows = conn.execute(
                "SELECT summary, indexed FROM conversations WHERE id = ?", (session.conv_id,)
            ).fetchone()
            pending = sweeper.find_unembedded_summaries(conn)
        self.assertEqual(rows[0], "version two")
        self.assertEqual(rows[1], 1)
        # The v1 vector is gone, so the row is correctly pending again.
        self.assertEqual([r[0] for r in pending], [session.conv_id])

    def test_flipping_indexed_to_zero_removes_the_vector(self):
        session = self._session()
        session._write_summary(SummaryResult(summary="keep me", index=True))
        with connect_db(self.db_path) as conn:
            conn.execute(
                "INSERT INTO summary_embeddings(conversation_id, embedding) VALUES (?, ?)",
                (session.conv_id, VECTOR),
            )
            conn.commit()

        session._write_summary(SummaryResult(summary="retrieval only", index=False))
        session.conn.close()

        self.assertEqual(self._vector_count("summary_embeddings"), 0)


class RunSweeperTests(SweeperTestBase):
    async def test_exits_cleanly_on_cancel(self):
        task = asyncio.create_task(
            sweeper.run_sweeper(self.db_path, interval=0.01, initial_delay=0)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class MigrationTests(unittest.TestCase):
    def test_migrations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.db"
            history.init_db(db_path).close()
            history.init_db(db_path).close()  # must not raise on the second run

            with connect_db(db_path) as conn:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(conversations)")]
            self.assertEqual(cols.count("indexed"), 1)
            self.assertEqual(cols.count("last_extracted_message_id"), 1)

    def test_migration_preserves_existing_rows_and_columns(self):
        """Mirrors the live DB: real rows, and last_extracted_message_id already
        present from the previous migration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.db"
            history.init_db(db_path).close()
            with connect_db(db_path) as conn:
                # Drop the new column to simulate a database from before it.
                conn.execute("ALTER TABLE conversations DROP COLUMN indexed")
                conn.execute(
                    "INSERT INTO conversations (id, session_id, started_at, service, source, "
                    "summary, last_extracted_message_id) "
                    "VALUES (7, 'old', '2026-01-01T00:00:00+00:00', 'octavius', 'voice', 'kept', 42)"
                )
                conn.commit()

            history.init_db(db_path).close()  # migrate forward

            with connect_db(db_path) as conn:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(conversations)")]
                row = conn.execute(
                    "SELECT summary, last_extracted_message_id, indexed "
                    "FROM conversations WHERE id = 7"
                ).fetchone()
            self.assertEqual(cols.count("indexed"), 1)
            self.assertEqual(cols.count("last_extracted_message_id"), 1)
            self.assertEqual(row[0], "kept")
            self.assertEqual(row[1], 42)
            # Backfilled as NULL = legacy/unknown, so the sweeper leaves it alone.
            self.assertIsNone(row[2])


if __name__ == "__main__":
    unittest.main()
