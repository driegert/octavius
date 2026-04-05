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


if __name__ == "__main__":
    unittest.main()
