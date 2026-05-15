import unittest
from unittest.mock import patch

import history_enrichment as enrichment


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


if __name__ == "__main__":
    unittest.main()
