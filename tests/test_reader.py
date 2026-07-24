import unittest
import sqlite3

from reader_store import fail_stale_processing_documents
from reader_text import clean_for_speech, has_math, split_into_chunks, strip_latex


class ReaderTests(unittest.TestCase):
    def test_clean_for_speech_strips_links_urls_and_citations(self):
        text = "See [paper](https://example.com) and https://x.test [1] (Smith et al., 2024)."
        cleaned = clean_for_speech(text)
        self.assertIn("paper", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("[1]", cleaned)
        self.assertNotIn("Smith et al.", cleaned)

    def test_split_into_chunks_respects_headings(self):
        markdown = "# Title\n\nIntro paragraph.\n\n## Section\n\nBody paragraph."
        chunks = split_into_chunks(markdown)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["heading"], "Title")
        self.assertEqual(chunks[1]["heading"], "Section")

    def test_has_math_catches_all_delimiter_styles(self):
        self.assertTrue(has_math("inline $x^2$ math"))
        self.assertTrue(has_math("display $$\\frac{a}{b}$$ math"))
        self.assertTrue(has_math("paren \\(x + y\\) math"))
        self.assertTrue(has_math("bracket \\[x + y\\] math"))
        self.assertTrue(has_math("\\begin{equation}\nx\n\\end{equation}"))
        self.assertFalse(has_math("costs 5 dollars, no math here"))

    def test_strip_latex_fallback_beats_command_soup(self):
        # The Thomson 2012 equation that was being read aloud as raw LaTeX.
        text = ("$$\\widehat{R_B}(\\tau) = \\frac{1}{N} "
                "\\sum_{n=0}^{N-1-|\\tau|} x_n \\, x_{n+\\tau} \\,, \\tag{1}$$")
        stripped = strip_latex(text)
        self.assertIn("hat", stripped)
        self.assertIn("1 over N", stripped)
        self.assertNotIn("$", stripped)
        self.assertNotIn("{", stripped)
        self.assertNotIn("frac", stripped)
        self.assertNotIn("tag", stripped)

    def test_strip_latex_handles_powers_and_subscripts(self):
        self.assertIn("squared", strip_latex("$x^2$"))
        self.assertIn("sub i", strip_latex("$x_{i}$"))
        self.assertIn("to the n", strip_latex("$x^{n}$"))

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

        count = fail_stale_processing_documents(conn, "interrupted")

        self.assertEqual(count, 1)
        rows = conn.execute("SELECT status, error FROM reader_documents ORDER BY id").fetchall()
        self.assertEqual(rows[0], ("failed", "interrupted"))
        self.assertEqual(rows[1], ("ready", None))


if __name__ == "__main__":
    unittest.main()
