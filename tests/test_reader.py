import unittest
import sqlite3

import reader


class ReaderTests(unittest.TestCase):
    def test_clean_for_speech_strips_links_urls_and_citations(self):
        text = "See [paper](https://example.com) and https://x.test [1] (Smith et al., 2024)."
        cleaned = reader._clean_for_speech(text)
        self.assertIn("paper", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("[1]", cleaned)
        self.assertNotIn("Smith et al.", cleaned)

    def test_split_into_chunks_respects_headings(self):
        markdown = "# Title\n\nIntro paragraph.\n\n## Section\n\nBody paragraph."
        chunks = reader._split_into_chunks(markdown)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["heading"], "Title")
        self.assertEqual(chunks[1]["heading"], "Section")

    def test_fail_stale_processing_documents_marks_rows_failed(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE reader_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                error TEXT,
                updated_at TEXT
            )"""
        )
        conn.execute("INSERT INTO reader_documents (status, error, updated_at) VALUES ('processing', NULL, NULL)")
        conn.execute("INSERT INTO reader_documents (status, error, updated_at) VALUES ('ready', NULL, NULL)")
        conn.commit()

        count = reader.fail_stale_processing_documents(conn, "interrupted")

        self.assertEqual(count, 1)
        rows = conn.execute("SELECT status, error FROM reader_documents ORDER BY id").fetchall()
        self.assertEqual(rows[0], ("failed", "interrupted"))
        self.assertEqual(rows[1], ("ready", None))


if __name__ == "__main__":
    unittest.main()
