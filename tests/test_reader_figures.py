"""Tables and figures in the reader: spoken as caption + a short model overview,
never cell by cell or as a filename, with the caption paragraph read once."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import reader_text
from reader_text import _build_speech_chunks, clean_for_speech, parse_caption, split_into_chunks
from tests.test_reader import FakeReaderSettings

TABLE_DOC = """Intro paragraph.

**Table 2.** Mean squared error by method.

| Method | MSE |
|---|---|
| A | 0.1 |
| B | 0.3 |

After the table.
"""

VISION_CHAIN = [
    {"url": "http://vision1:1/v1/chat/completions", "model": "vision-a"},
    {"url": "http://vision2:2/v1/chat/completions", "model": "vision-b"},
]


class CaptionParsingTests(unittest.TestCase):
    def test_table_caption_forms(self):
        self.assertEqual(parse_caption("**Table 2.** Mean error.", "table"), ("2", "Mean error."))
        self.assertEqual(parse_caption("Tab. 3: Results", "table"), ("3", "Results"))
        self.assertEqual(parse_caption("TABLE IV\nSimulation settings", "table"), ("4", "Simulation settings"))
        self.assertEqual(parse_caption('<span id="page-3-1"></span>Table S1 | Extra', "table"), ("S1", "Extra"))

    def test_figure_caption_forms(self):
        self.assertEqual(parse_caption("Fig. 3: Spectrum.", "figure"), ("3", "Spectrum."))
        self.assertEqual(parse_caption("**Figure 1** The setup.", "figure"), ("1", "The setup."))
        self.assertEqual(parse_caption("fig 2. lower case", "figure"), ("2", "lower case"))

    def test_prose_that_mentions_a_table_is_not_a_caption(self):
        self.assertIsNone(parse_caption("Table 2 shows the results.", "table"))
        self.assertIsNone(parse_caption("Fig. 3 compares both.", "figure"))
        self.assertIsNone(parse_caption("Figure 1: x", "table"))


class ChunkingTests(unittest.TestCase):
    def test_caption_stays_in_the_tables_chunk_in_a_long_section(self):
        chunks = split_into_chunks(TABLE_DOC + "\nOne more.\n\nAnd another.\n")
        texts = [c["text"] for c in chunks]
        table_chunk = next(t for t in texts if "| A |" in t)
        self.assertIn("**Table 2.**", table_chunk)
        self.assertEqual(sum("Table 2." in t for t in texts), 1)

    def test_figure_caption_below_image_joins_it(self):
        md = "A.\n\nB.\n\n![](_page_1_Figure_2.jpeg)\n\nFig. 1: Spectrum.\n\nC.\n"
        texts = [c["text"] for c in split_into_chunks(md)]
        self.assertIn("![](_page_1_Figure_2.jpeg)\n\nFig. 1: Spectrum.", texts)

    def test_image_filename_is_never_spoken(self):
        self.assertEqual(clean_for_speech("See ![](_page_3_Figure_1.jpeg) here."), "See here.")
        self.assertEqual(clean_for_speech("A ![logo](logo.png) b"), "A b")


class TableSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def _convert(self, md, complete, fallback=None):
        fake = SimpleNamespace(reader=FakeReaderSettings(fallback), vision_llm_chain=VISION_CHAIN)
        with patch.object(reader_text.llm_client, "complete", complete), \
             patch.object(reader_text, "settings", fake):
            chunks = split_into_chunks(md)
            return chunks, await reader_text._convert_all_chunks(chunks)

    async def test_caption_then_overview_and_caption_not_read_twice(self):
        payloads = []

        async def complete(payload, urls=None):
            payloads.append(payload)
            return "B has three times the error of A."

        chunks, out = await self._convert(TABLE_DOC, complete)
        spoken = "\n\n".join(out)
        self.assertIn("Table 2: Mean squared error by method. B has three times the error of A.", spoken)
        self.assertEqual(spoken.count("Mean squared error"), 1)
        self.assertNotIn("|", spoken)
        self.assertEqual(payloads[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIn("| B | 0.3 |", payloads[0]["messages"][1]["content"])
        # the raw table survives in source_text for the reader chat
        speech = _build_speech_chunks(chunks, out)
        self.assertTrue(any("| B | 0.3 |" in c["source_text"] for c in speech))

    async def test_none_means_caption_only(self):
        async def complete(payload, urls=None):
            return "NONE"

        _, out = await self._convert(TABLE_DOC, complete)
        self.assertIn("\n\nTable 2: Mean squared error by method.\n\n", out[0])

    async def test_fails_over_then_degrades_to_caption(self):
        urls_seen = []

        async def complete(payload, urls=None):
            urls_seen.append((urls[0], payload["model"]))
            return None

        _, out = await self._convert(TABLE_DOC, complete, fallback=("http://fallback:2/v1", "fallback-model"))
        self.assertIn("\n\nTable 2: Mean squared error by method.\n\n", out[0])
        self.assertEqual([m for _, m in urls_seen], ["primary-model", "fallback-model"])

    async def test_no_caption(self):
        md = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"

        async def down(payload, urls=None):
            raise RuntimeError("down")

        _, out = await self._convert(md, down)
        self.assertEqual(out, ["A table with 2 rows."])

        async def up(payload, urls=None):
            return "Both columns rise together."

        _, out = await self._convert(md, up)
        self.assertEqual(out, ["A table. Both columns rise together."])

    async def test_caption_line_directly_above_the_table(self):
        md = "Table 1: Settings.\n| a | b |\n|---|---|\n| 1 | 2 |\n"

        async def complete(payload, urls=None):
            return "NONE"

        _, out = await self._convert(md, complete)
        self.assertEqual(out, ["Table 1: Settings."])


class FigureSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def _convert(self, md, vision_complete, base_dir=None):
        fake = SimpleNamespace(reader=FakeReaderSettings(), vision_llm_chain=VISION_CHAIN)

        async def no_reader_llm(payload, urls=None):
            raise AssertionError("reader LLM should not be called")

        with patch.object(reader_text.llm_client, "complete", no_reader_llm), \
             patch.object(reader_text.vision_llm_client, "complete", vision_complete), \
             patch.object(reader_text, "settings", fake):
            return await reader_text._convert_all_chunks(split_into_chunks(md), base_dir)

    async def test_unresolvable_image_is_caption_only(self):
        async def vision(payload, urls=None):
            raise AssertionError("no file, no vision call")

        out = await self._convert("![](_page_3_Figure_1.jpeg)\n\nFig. 3: Spectrum of the series.\n", vision)
        self.assertEqual(out, ["Figure 3: Spectrum of the series."])

    async def test_resolvable_image_gets_a_description_from_the_vision_chain(self):
        calls = []

        async def vision(payload, urls=None):
            calls.append((urls[0], payload["model"], payload["messages"][1]["content"]))
            return None if urls[0].startswith("http://vision1") else "A line falling steeply after the first lag."

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "fig 1.png").write_bytes(b"\x89PNG fake")
            out = await self._convert("Text.\n\n![](fig%201.png)\n\n**Figure 1.** Autocorrelation.\n", vision, Path(tmp))
        self.assertEqual(out, ["Text.\n\nFigure 1: Autocorrelation. A line falling steeply after the first lag."])
        self.assertEqual([(u, m) for u, m, _ in calls], [(VISION_CHAIN[0]["url"], "vision-a"), (VISION_CHAIN[1]["url"], "vision-b")])
        content = calls[0][2]
        self.assertIn("Autocorrelation", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    async def test_vision_failure_or_oversize_is_caption_only(self):
        async def vision(payload, urls=None):
            raise RuntimeError("down")

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.jpeg").write_bytes(b"x")
            out = await self._convert("![](a.jpeg)\n\nFig. 2: Map.\n", vision, Path(tmp))
            self.assertEqual(out, ["Figure 2: Map."])
            with patch.object(reader_text, "FIGURE_MAX_BYTES", 0):
                out = await self._convert("![](a.jpeg)\n\nFig. 2: Map.\n", vision, Path(tmp))
            self.assertEqual(out, ["Figure 2: Map."])

    async def test_no_caption_and_no_description_says_nothing(self):
        async def vision(payload, urls=None):
            return "NONE"

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "logo.png").write_bytes(b"x")
            out = await self._convert("Before.\n\n![](logo.png)\n\nAfter.\n", vision, Path(tmp))
        self.assertEqual(out, ["Before.\n\nAfter."])

    async def test_caption_in_alt_text(self):
        async def vision(payload, urls=None):
            raise AssertionError("unresolvable")

        out = await self._convert("![Figure 4: Two spectra.](http://example.com/f.png)\n", vision)
        self.assertEqual(out, ["Figure 4: Two spectra."])


if __name__ == "__main__":
    unittest.main()
