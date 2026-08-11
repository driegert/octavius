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


class DetachedEmbeddingTests(unittest.IsolatedAsyncioTestCase):
    """add_message_async must not put a network round-trip on the turn path."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.db_path = Path(self.tmpdir.name) / "history.db"
        history.init_db(self.db_path).close()
        self.recorder = history.HistoryRecorder(self.db_path)

    async def asyncTearDown(self):
        still_pending = await enrichment.drain_inflight(timeout=5.0)
        leaked = set(enrichment._inflight)
        enrichment._inflight.clear()
        # Assert rather than silently clear: a task that outlives its drain is a
        # lifecycle bug, and clearing the set would hide it.
        self.assertEqual(still_pending, 0, "a detached embed did not finish")
        self.assertEqual(leaked, set(), "a detached embed did not deregister itself")

    def _session(self):
        session = self.recorder.start_conversation(source="voice")
        self.addCleanup(lambda: session.conn.close() if not session._closed else None)
        return session

    def _vector_count(self):
        with connect_db(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM message_embeddings").fetchone()[0]

    async def test_add_message_async_returns_before_the_embed_lands(self):
        session = self._session()
        gate = asyncio.Event()

        async def slow_embed(text):
            await gate.wait()
            return VECTOR

        with patch.object(enrichment, "embed_text_async", side_effect=slow_embed):
            msg_id = await session.add_message_async(role="user", content="hello")
            # The row is committed but the vector has not been written yet.
            with connect_db(self.db_path) as conn:
                row = conn.execute("SELECT content FROM messages WHERE id = ?", (msg_id,)).fetchone()
            self.assertEqual(row[0], "hello")
            self.assertEqual(self._vector_count(), 0)

            gate.set()
            await enrichment.drain_inflight(timeout=5.0)

        self.assertEqual(self._vector_count(), 1)

    async def test_embed_survives_cancellation_of_the_enclosing_turn(self):
        """handle_reset cancels turn_task; the embed is a root task, not a child."""
        session = self._session()
        gate = asyncio.Event()

        async def slow_embed(text):
            await gate.wait()
            return VECTOR

        async def turn():
            await session.add_message_async(role="assistant", content="reply")
            await asyncio.sleep(3600)  # stands in for the rest of the turn

        with patch.object(enrichment, "embed_text_async", side_effect=slow_embed):
            turn_task = asyncio.create_task(turn())
            await asyncio.sleep(0)  # let it reach the sleep
            turn_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await turn_task

            gate.set()
            await enrichment.drain_inflight(timeout=5.0)

        self.assertEqual(self._vector_count(), 1)

    async def test_embed_survives_the_session_connection_closing(self):
        """The detached task opens its own connection, so end_async closing
        self.conn must not break an in-flight embed."""
        session = self._session()
        gate = asyncio.Event()

        async def slow_embed(text):
            await gate.wait()
            return VECTOR

        with patch.object(enrichment, "embed_text_async", side_effect=slow_embed):
            await session.add_message_async(role="user", content="hello")
            session.conn.close()
            session._closed = True
            gate.set()
            await enrichment.drain_inflight(timeout=5.0)

        self.assertEqual(self._vector_count(), 1)

    async def test_failed_embed_writes_no_row(self):
        """No row is the sweeper's pending marker."""
        session = self._session()
        with patch.object(enrichment, "embed_text_async", return_value=None):
            await session.add_message_async(role="user", content="hello")
            await enrichment.drain_inflight(timeout=5.0)
        self.assertEqual(self._vector_count(), 0)

    async def test_backlog_cap_skips_rather_than_queues(self):
        session = self._session()
        with patch.object(enrichment, "MAX_INFLIGHT_EMBEDS", 0), \
             patch.object(enrichment, "embed_text_async", return_value=VECTOR) as embed:
            msg_id = await session.add_message_async(role="user", content="hello")
        self.assertIsNotNone(msg_id)
        embed.assert_not_called()
        self.assertEqual(self._vector_count(), 0)

    async def test_concurrent_detached_writes_all_land(self):
        """Up to MAX_INFLIGHT_EMBEDS workers each open their own sqlite-vec
        connection and write at once; WAL serializes them and none is lost."""
        session = self._session()
        ids = []
        with patch.object(enrichment, "embed_text_async", return_value=VECTOR):
            for i in range(enrichment.MAX_INFLIGHT_EMBEDS):
                ids.append(await session.add_message_async(role="user", content=f"msg {i}"))
            await enrichment.drain_inflight(timeout=15.0)

        self.assertEqual(len(set(ids)), enrichment.MAX_INFLIGHT_EMBEDS)
        self.assertEqual(self._vector_count(), enrichment.MAX_INFLIGHT_EMBEDS)

    async def test_tool_messages_are_never_embedded(self):
        session = self._session()
        with patch.object(enrichment, "embed_text_async", return_value=VECTOR) as embed:
            await session.add_message_async(role="tool", content="tool output")
            await enrichment.drain_inflight(timeout=5.0)
        embed.assert_not_called()
        self.assertEqual(self._vector_count(), 0)


if __name__ == "__main__":
    unittest.main()
