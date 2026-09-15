"""The sweeper was retired on 2026-09-15; what is left here is its contract.

`history_sweeper` backfilled the legacy vec0 tables for rows whose detached
embed never landed. Both the tables and the detached embeds are gone, so the
~20 tests that covered backlog selection, convergence, abort semantics and
stale-vector invalidation went with them — they asserted behaviour over tables
that no longer exist, and keeping them green would have meant recreating those
tables just to have something to test.

What replaces them: one class asserting the retirement is loud (below), plus
`hybrid-corpus`'s own suite, which owns embed-debt healing now
(`tests/test_index.py`'s repair/pending coverage).

`MigrationTests` never had anything to do with the sweeper — it covers
`history._run_migrations`' idempotent ALTER TABLEs — and is left here rather
than moved so the move does not hide in a diff. The `indexed` column it used
to check was dropped from the schema on 2026-09-15 (the summariser stopped
writing it; nothing read it), so only `last_extracted_message_id` remains.
"""

import tempfile
import unittest
from pathlib import Path

import history
import history_sweeper as sweeper
from db import connect_db


class RetirementTests(unittest.IsolatedAsyncioTestCase):
    """Both entry points raise rather than quietly doing nothing.

    A no-op stub would let a caller that still starts a sweeper look healthy
    while indexing silently never happened — the exact failure mode the move to
    history-index.timer was meant to end.
    """

    async def test_sweep_once_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            await sweeper.sweep_once("/tmp/nonexistent.db")
        self.assertIn("history-index.timer", str(ctx.exception))

    async def test_run_sweeper_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            await sweeper.run_sweeper("/tmp/nonexistent.db")
        self.assertIn("history-index.timer", str(ctx.exception))

    def test_the_selection_helpers_are_gone(self):
        for name in ("find_unembedded_messages", "find_unembedded_summaries",
                     "_embed_rows"):
            self.assertFalse(hasattr(sweeper, name),
                             f"{name} queried a table that no longer exists")


class MigrationTests(unittest.TestCase):
    def test_migrations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.db"
            history.init_db(db_path).close()
            history.init_db(db_path).close()  # must not raise on the second run

            with connect_db(db_path) as conn:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(conversations)")]
            self.assertEqual(cols.count("last_extracted_message_id"), 1)
            self.assertNotIn("indexed", cols)

    def test_migration_preserves_existing_rows_and_columns(self):
        """A database from before the watermark column: real rows survive, and
        the column is backfilled as NULL."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.db"
            history.init_db(db_path).close()
            with connect_db(db_path) as conn:
                # Drop the migrated column to simulate a database from before it.
                conn.execute("ALTER TABLE conversations DROP COLUMN last_extracted_message_id")
                conn.execute(
                    "INSERT INTO conversations (id, session_id, started_at, service, source, "
                    "summary) "
                    "VALUES (7, 'old', '2026-01-01T00:00:00+00:00', 'octavius', 'voice', 'kept')"
                )
                conn.commit()

            history.init_db(db_path).close()  # migrate forward

            with connect_db(db_path) as conn:
                cols = [row[1] for row in conn.execute("PRAGMA table_info(conversations)")]
                row = conn.execute(
                    "SELECT summary, last_extracted_message_id "
                    "FROM conversations WHERE id = 7"
                ).fetchone()
            self.assertEqual(cols.count("last_extracted_message_id"), 1)
            self.assertEqual(row[0], "kept")
            self.assertIsNone(row[1])


if __name__ == "__main__":
    unittest.main()
