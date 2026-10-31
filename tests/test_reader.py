import unittest
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import reader_text
from reader_store import fail_stale_processing_documents
from reader_text import clean_for_speech, has_math, split_into_chunks, strip_latex


class FakeReaderSettings:
    def __init__(self, fallback=None):
        self.directory = "/tmp/unused-reader-dir"
        self.llm_url = "http://primary:1/v1/chat/completions"
        self.llm_model = "primary-model"
        if fallback is None:
            self.llm_fallback_url = None
            self.llm_fallback_model = None
        else:
            self.llm_fallback_url, self.llm_fallback_model = fallback


class MathConversionTests(unittest.IsolatedAsyncioTestCase):
    """_llm_convert_math tries the reader's second endpoint before giving up
    to strip_latex: a dead :8010 used to silently degrade every math chunk to
    dollar-stripping, and the fallback exists to stop that."""

    async def _run(self, complete_impl, fallback):
        fake = SimpleNamespace(reader=FakeReaderSettings(fallback))
        with patch.object(reader_text, "llm_client") as client, \
             patch.object(reader_text, "settings", fake):
            client.complete = complete_impl
            return await reader_text._llm_convert_math(None, "$x^2$")

    async def test_uses_primary_when_it_answers(self):
        calls = []

        async def complete(payload, urls=None):
            calls.append((urls, payload["model"]))
            return "x squared"

        out = await self._run(complete, fallback=None)
        self.assertEqual(out, "x squared")
        self.assertEqual(calls, [( ["http://primary:1/v1/chat/completions"], "primary-model")])

    async def test_falls_over_to_second_entry_when_primary_fails(self):
        calls = []

        async def complete(payload, urls=None):
            calls.append((urls, payload["model"]))
            return None if urls[0].startswith("http://primary") else "x squared"

        out = await self._run(complete, fallback=("http://fallback:2/v1/chat/completions", "fallback-model"))
        self.assertEqual(out, "x squared")
        self.assertEqual(
            calls,
            [
                (["http://primary:1/v1/chat/completions"], "primary-model"),
                (["http://fallback:2/v1/chat/completions"], "fallback-model"),
            ],
        )

    async def test_strip_latex_when_every_endpoint_fails(self):
        async def complete(payload, urls=None):
            return None

        out = await self._run(complete, fallback=("http://fallback:2/v1/chat/completions", "fallback-model"))
        self.assertEqual(out, strip_latex("$x^2$"))
        self.assertIn("squared", out)


class ReaderTests(unittest.TestCase):
    def test_citation_abbreviations_do_not_end_sentences(self):
        split = reader_text.split_sentences
        self.assertEqual(split("Smith et al. (2020) found it. Jones et al. 2021 agreed."),
                         ["Smith et al. (2020) found it.", "Jones et al. 2021 agreed."])
        self.assertEqual(split("Methods, e.g. multitaper, i.e. Thomson's, help. See Fig. 3 and Eq. 2. A vs. B."),
                         ["Methods, e.g. multitaper, i.e. Thomson's, help.", "See Fig. 3 and Eq. 2.", "A vs. B."])
        # "et al." ending a sentence still breaks before the next one.
        self.assertEqual(split("Shown by Smith et al. The next differs."),
                         ["Shown by Smith et al.", "The next differs."])
        self.assertEqual(split("One. Two! Three? Four."), ["One.", "Two!", "Three?", "Four."])

    def test_page_break_mid_sentence_is_rejoined(self):
        join = reader_text.join_page_breaks
        self.assertEqual(join("The spectrum was estimated with the\n\nmultitaper method. Next."),
                         "The spectrum was estimated with the multitaper method. Next.")
        # A caption or page number between the halves moves after the sentence.
        self.assertEqual(
            join("We fit the model to the\n\n**Fig. 2.** Residuals.\n\n12\n\nsecond half of the data."),
            "We fit the model to the second half of the data.\n\n**Fig. 2.** Residuals.\n\n12")
        self.assertEqual(join("A word split across pages, multi-\n\ntaper."), "A word split across pages, multi-taper.")
        self.assertEqual(join("Ends on a comma,\n\nThomson showed."), "Ends on a comma, Thomson showed.")

    def test_page_break_join_leaves_real_paragraphs_alone(self):
        join = reader_text.join_page_breaks
        for md in ("A finished sentence.\n\nlowercase start.",
                   "An unpunctuated line\n\nThen a capitalised one.",
                   "## A heading\n\nbody text.",
                   "Text before\n## Heading\n\nlowercase body.",
                   "Intro to the list\n\n- item one",
                   "Leads into an equation\n\n$$x^2$$",
                   "```\nif (x) {\n\n  y();\n}\n```",
                   "if (x) {\n\nreturn y;"):
            with self.subTest(md=md):
                self.assertEqual(join(md), md)

    def test_split_into_chunks_keeps_a_page_split_sentence_together(self):
        paras = ["First paragraph.", "Second one.", "Third runs onto the", "next page here.", "Fifth."]
        chunks = split_into_chunks("\n\n".join(paras))
        self.assertIn("Third runs onto the next page here.", [c["text"] for c in chunks])


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
