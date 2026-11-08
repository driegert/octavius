import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import history
import history_enrichment as enrichment
from db import connect_db


class HistoryEnrichmentTests(unittest.TestCase):
    def test_build_transcript_skips_system_and_truncates(self):
        transcript = enrichment.build_transcript(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "x" * 10},
                {"role": "assistant", "content": "ok"},
            ],
            max_content_chars=5,
        )
        self.assertEqual(transcript, "user: xxxxx...\nassistant: ok")

    def test_generate_tags_returns_empty_on_invalid_json(self):
        messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
            {"role": "assistant", "content": "four"},
        ]
        with patch.object(enrichment.summary_client, "complete", return_value="not json"):
            self.assertEqual(enrichment.generate_tags(messages), [])

    def test_embed_text_uses_embedding_client(self):
        with patch.object(enrichment.embedding_client, "embed_text", return_value=b"abc") as mock_embed:
            result = enrichment.embed_text("hello")
        self.assertEqual(result, b"abc")
        mock_embed.assert_called_once()

    def test_oversized_text_is_truncated_before_embedding(self):
        """Embedders reject anything past their context limit outright (workhorse
        500s above ~4-6k chars), so an untruncated 20k-char message never embeds."""
        long_text = "x" * 20034
        with patch.object(enrichment.embedding_client, "embed_text", return_value=b"v") as mock_embed:
            enrichment.embed_text(long_text)
        sent = mock_embed.call_args.args[0]
        self.assertEqual(len(sent), enrichment.EMBED_MAX_CHARS)
        self.assertTrue(long_text.startswith(sent))

    def test_text_within_the_limit_is_passed_through_unchanged(self):
        with patch.object(enrichment.embedding_client, "embed_text", return_value=b"v") as mock_embed:
            enrichment.embed_text("short message")
        self.assertEqual(mock_embed.call_args.args[0], "short message")


class GenerateSummaryTests(unittest.TestCase):
    def _messages(self):
        return [
            {"role": "user", "content": "what tasks do I have?"},
            {"role": "assistant", "content": "Listed 3 tasks."},
        ]

    def test_empty_transcript_returns_skip(self):
        result = enrichment.generate_summary([])
        self.assertIsNone(result.summary)
        self.assertFalse(result.index)

    def test_valid_json_index_true(self):
        raw = '{"summary": "Designed conversation-history search.", "index": true}'
        with patch.object(enrichment.summary_client, "complete", return_value=raw):
            result = enrichment.generate_summary(self._messages())
        self.assertEqual(result.summary, "Designed conversation-history search.")
        self.assertTrue(result.index)

    def test_valid_json_index_false(self):
        raw = '{"summary": "Listed open Vikunja tasks.", "index": false}'
        with patch.object(enrichment.summary_client, "complete", return_value=raw):
            result = enrichment.generate_summary(self._messages())
        self.assertEqual(result.summary, "Listed open Vikunja tasks.")
        self.assertFalse(result.index)

    def test_json_with_think_prefix(self):
        raw = (
            "<think>weighing whether this is novel</think>\n"
            '{"summary": "Discussed Qwen3.6 thinking-mode.", "index": true}'
        )
        with patch.object(enrichment.summary_client, "complete", return_value=raw):
            result = enrichment.generate_summary(self._messages())
        self.assertEqual(result.summary, "Discussed Qwen3.6 thinking-mode.")
        self.assertTrue(result.index)

    def test_malformed_json_falls_back_to_indexed_text(self):
        raw = "Designed conversation-history search."
        with patch.object(enrichment.summary_client, "complete", return_value=raw):
            result = enrichment.generate_summary(self._messages())
        self.assertEqual(result.summary, "Designed conversation-history search.")
        self.assertTrue(result.index)

    def test_string_index_flag_parsed(self):
        raw = '{"summary": "Listed emails.", "index": "false"}'
        with patch.object(enrichment.summary_client, "complete", return_value=raw):
            result = enrichment.generate_summary(self._messages())
        self.assertFalse(result.index)

    def test_empty_completion_returns_no_summary(self):
        with patch.object(enrichment.summary_client, "complete", return_value=""):
            result = enrichment.generate_summary(self._messages())
        self.assertIsNone(result.summary)
        self.assertFalse(result.index)


VECTOR = np.zeros(1024, dtype=np.float32).tobytes()


class RetiredEmbedPathTests(unittest.IsolatedAsyncioTestCase):
    """Recording a message is pure SQLite now.

    This class replaces DetachedEmbeddingTests, which covered the detached
    embed machinery (`spawn_embedding`, the `_inflight` backlog cap, the
    root-task lifecycle that survived turn cancellation). All of it was retired
    on 2026-09-15: history-index.timer owns indexing, so there is no round-trip
    on the turn path left to detach. What is worth asserting now is the
    negative — that nothing embeds, nothing is spawned, and the retired helpers
    fail loudly rather than quietly doing nothing.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "history.db"
        history.init_db(self.db_path).close()
        self.recorder = history.HistoryRecorder(self.db_path)

    def _session(self):
        session = self.recorder.start_conversation(source="voice")
        self.addCleanup(lambda: session.conn.close() if not session._closed else None)
        return session

    async def test_add_message_async_records_without_embedding(self):
        session = self._session()
        with patch.object(enrichment, "embed_text_async") as embed:
            msg_id = await session.add_message_async(role="user", content="hello")
        embed.assert_not_called()
        with connect_db(self.db_path) as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE id = ?", (msg_id,)
            ).fetchone()
        self.assertEqual(row[0], "hello")

    def test_add_message_records_without_embedding(self):
        session = self._session()
        with patch.object(enrichment, "embed_text") as embed:
            msg_id = session.add_message(role="user", content="hello")
        embed.assert_not_called()
        self.assertIsNotNone(msg_id)

    async def test_nothing_is_left_in_flight(self):
        session = self._session()
        await session.add_message_async(role="user", content="hello")
        self.assertEqual(enrichment._inflight, set())
        self.assertEqual(await enrichment.drain_inflight(timeout=1.0), 0)

    def test_the_legacy_schema_no_longer_creates_the_vec0_tables(self):
        """init_db must not resurrect a table the migration is about to drop."""
        with connect_db(self.db_path) as conn:
            names = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        for legacy in ("message_embeddings", "summary_embeddings",
                       "saved_item_embeddings", "fact_embeddings"):
            self.assertNotIn(legacy, names)

    def test_retired_helpers_raise(self):
        for call in (
            lambda: enrichment.store_embedding(None, "t", "c", 1, "x"),
            lambda: enrichment.store_embedding_bytes(None, "t", "c", 1, b"x"),
            lambda: enrichment.spawn_embedding(self.db_path, "t", "c", 1, "x"),
        ):
            with self.assertRaises(RuntimeError):
                call()

    async def test_retired_async_helper_raises(self):
        with self.assertRaises(RuntimeError):
            await enrichment.store_embedding_async(None, "t", "c", 1, "x")


if __name__ == "__main__":
    unittest.main()
