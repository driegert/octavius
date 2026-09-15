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

`MigrationTests` stays as-is. It never had anything to do with the sweeper —
it covers `history._run_migrations`' idempotent ALTER TABLEs — and it is left
in place rather than moved so the move does not hide in this diff.
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
