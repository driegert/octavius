import asyncio
import re
import unittest
from unittest.mock import AsyncMock, patch

import tts
from tts import speechify


class SpeechifyTests(unittest.TestCase):
    def test_et_al_loses_its_period(self):
        self.assertEqual(speechify("Smith et al. (2020) and Jones Et Al., 1982."),
                         "Smith et al (2020) and Jones Et Al, 1982.")
        self.assertEqual(speechify("Thomson et al."), "Thomson et al")
        self.assertEqual(speechify("pet al. fetal."), "pet al. fetal.")

    def test_multiplier_x_is_read_as_times(self):
        self.assertEqual(speechify("a 4.6x speedup"), "a 4 point 6 times speedup")
        self.assertEqual(speechify("It is 10x faster, about 1,000x."), "It is 10 times faster, about 1,000 times.")
        self.assertEqual(speechify("roughly 3×"), "roughly 3 times")
        self.assertEqual(speechify("4.6 x 2"), "4 point 6 x 2")  # a spaced x is not "times"
        for text in ("a 2x2 matrix", "1920x1080", "address 0x1F", "the x axis"):
            self.assertEqual(speechify(text), text)

    def test_dash_ranges_are_read_with_to(self):
        cases = {
            "2– 8%": "2 to 8%",
            "rose 2–8% last year": "rose 2 to 8% last year",
            "2 – 8": "2 to 8",
            "1990–2000": "1990 to 2000",
            "pp. 12–15": "pp. 12 to 15",
            "2.5—3.5 kg": "2 point 5 to 3 point 5 kg",
            "2–3x faster": "2 to 3 times faster",
        }
        for text, expected in cases.items():
            self.assertEqual(speechify(text), expected)

    def test_hyphen_ranges_only_where_unambiguous(self):
        cases = {
            "a 5-10% drop": "a 5 to 10% drop",
            "5 - 10%": "5 to 10%",
            "see pp. 12-15": "see pp. 12 to 15",
            "from 1990-2000": "from 1990 to 2000",
        }
        for text, expected in cases.items():
            self.assertEqual(speechify(text), expected)

    def test_hyphens_that_are_not_ranges_are_kept(self):
        for text in ("COVID-19", "x-1", "-5", "from -3 to 4", "2026-10-31", "1-800-555",
                     "1-800-555-1234", "5 - 3 = 2", "-2-8%", "v1.2-3%", "2020-2021-2022",
                     "ages 3-5", "1990-95", "temp –5"):
            self.assertEqual(speechify(text), text)

    def test_dotted_initialisms_lose_their_dots(self):
        self.assertEqual(speechify("published by U.S."), "published by US.")
        self.assertEqual(speechify("by U.S. state governors"), "by US state governors")
        self.assertEqual(speechify("the U.K., and U.S.A.)"), "the UK, and USA)")
        self.assertEqual(speechify("e.g. D. J. Thomson"), "e.g. D. J. Thomson")

    def test_strips_bold_and_leading_list_marker(self):
        # The motivating case: "**Important news:** \n1) the weather ..."
        out = speechify("**Important news:**\n1) the weather looks clear")
        self.assertNotIn("*", out)
        self.assertNotIn("1)", out)
        self.assertIn("Important news:", out)
        self.assertIn("the weather looks clear", out)

    def test_inline_enumeration_is_preserved(self):
        # "1)" mid-sentence is prose, not a list item, and should be spoken.
        out = speechify("Check the map, see 1) the route before leaving")
        self.assertIn("1)", out)

    def test_strips_all_list_marker_styles(self):
        self.assertEqual(speechify("- first"), "first")
        self.assertEqual(speechify("* first"), "first")
        self.assertEqual(speechify("+ first"), "first")
        self.assertEqual(speechify("1. first"), "first")
        self.assertEqual(speechify("2) first"), "first")

    def test_strips_inline_code(self):
        self.assertEqual(speechify("run `uv sync` now"), "run uv sync now")

    def test_strips_links_and_images(self):
        self.assertEqual(speechify("see [the docs](https://x.test/y)"), "see the docs")
        self.assertEqual(speechify("![a cat](https://x.test/c.png) here"), "a cat here")

    def test_strips_headings(self):
        self.assertEqual(speechify("## Weather"), "Weather")
        self.assertEqual(speechify("### Today's forecast"), "Today's forecast")

    def test_strips_italic_emphasis(self):
        self.assertEqual(speechify("that is *really* important"), "that is really important")
        self.assertEqual(speechify("that is _really_ important"), "that is really important")

    def test_strips_orphan_bold_from_split_span(self):
        # A bold span split across streamed sentences leaves a dangling "**".
        self.assertEqual(speechify("**Here is a summary"), "Here is a summary")
        self.assertEqual(speechify("** More details follow."), "More details follow.")
        self.assertEqual(speechify("the end is **"), "the end is")

    def test_strips_orphan_single_asterisk_at_word_edge(self):
        self.assertEqual(speechify("*first point"), "first point")
        self.assertEqual(speechify("last point*"), "last point")

    def test_preserves_multiplication_asterisks(self):
        # Lone asterisks that don't hug a word edge are arithmetic, not markup.
        self.assertEqual(speechify("compute 3 * 4 exactly"), "compute 3 * 4 exactly")
        self.assertEqual(speechify("compute 2*3 exactly"), "compute 2*3 exactly")

    def test_preserves_meaning_bearing_characters(self):
        # Snake_case identifiers and lone asterisks (e.g. multiplication) must survive.
        self.assertEqual(speechify("call foo_bar_baz please"), "call foo_bar_baz please")
        self.assertEqual(speechify("compute 3 * 4 exactly"), "compute 3 * 4 exactly")

    def test_words_joined_by_a_hyphen_are_read_as_two(self):
        # Phonon said "document dash level" and "domain dash and" (docs/pronunciation.md).
        self.assertEqual(speechify("document-level sentiment"), "document level sentiment")
        self.assertEqual(speechify("domain- and task-specific"), "domain and task specific")
        self.assertEqual(speechify("aspect–sentiment pairs"), "aspect sentiment pairs")
        for kept in ("COVID-19", "1-800-555", "a - b", "-5 degrees", "well -- no"):
            self.assertEqual(speechify(kept), kept)

    def test_housing_is_respelled(self):
        self.assertEqual(
            speechify("including Public Housing and Rent Supplement."),
            "including Public Howzing and Rent Supplement.",
        )
        self.assertEqual(speechify("Housing is expensive; rental housings too."),
                         "Howzing is expensive; rental howzings too.")
        self.assertEqual(speechify("A Rousing speech, arousing interest"), "A Rowzing speech, arowzing interest")
        self.assertEqual(speechify("HOUSING"), "Howzing")  # "HOWZING" is heard as "pausing"
        # These read fine, and "rehousing"/"mousing" are left alone.
        self.assertEqual(speechify("Houses, rehousing, mousing"), "Houses, rehousing, mousing")

    def test_quarter_before_a_year_gets_of(self):
        # Phonon read "Q1 2019" as "Q one two zero one nine".
        self.assertEqual(speechify("quarters from Q1 2019 to Q2 2026, with no gaps"),
                         "quarters from Q1 of 2019 to Q2 of 2026, with no gaps")
        self.assertEqual(speechify("Q4, 2025 was weak."), "Q4 of 2025 was weak.")
        # Already fine, or not a quarter and a year: left alone.
        for text in ("2023 Q3", "in Q3.", "Q3 of 2023", "Q5 2019", "Q1 20 units", "SQ1 2019"):
            self.assertEqual(speechify(text), text)

    def test_decimals_are_read_with_point(self):
        # In a long sentence Phonon read "57.7%" as "57. 7%".
        self.assertEqual(speechify("in our data: 57.7% of the 241,309"), "in our data: 57 point 7% of the 241,309")
        self.assertEqual(speechify("at the 0.05 level"), "at the 0 point 0 5 level")
        for kept in ("costs $3.50", "version 1.2.3", "v1.2 shipped", "in 2020. 5 people", "241,309 reviews"):
            self.assertEqual(speechify(kept), kept)

    def test_reader_chat_citations_are_silent(self):
        self.assertEqual(speechify("They compare three classes [§14]."), "They compare three classes.")
        self.assertEqual(speechify("Both [§3, §7] agree [§2–5]."), "Both agree.")
        self.assertEqual(speechify("Under § 4 of the act [1]."), "Under § 4 of the act [1].")

    def test_idempotent(self):
        s = "**Bold** and `code` and [link](http://x.test)\n- one\n- two"
        once = speechify(s)
        self.assertEqual(speechify(once), once)

    def test_clean_prose_unchanged(self):
        s = "The weather in Peterborough is clear and cold today."
        self.assertEqual(speechify(s), s)

    def test_empty_string(self):
        self.assertEqual(speechify(""), "")

    def test_never_empty_for_nonempty_input(self):
        # A line that is nothing but a stripped marker still yields something.
        self.assertTrue(speechify("### ").strip() != "" or speechify("### ") == "###")


if __name__ == "__main__":
    unittest.main()


class PiRespellingTests(unittest.TestCase):
    def test_pi_is_respelled_where_it_is_the_word(self):
        self.assertEqual(speechify("from negative pi to pi of e"), "from negative pie to pie of e")
        self.assertEqual(speechify("Pi is about 3, and so is π."), "pie is about 3, and so is pie.")
        for text in ("The PI signed it.", "a pig and a pin", "pi_harness runs"):
            self.assertEqual(speechify(text), text)


class SpeakableTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tts._speech_math_down_until = 0.0
        self.addCleanup(setattr, tts, "_speech_math_down_until", 0.0)

    async def test_text_without_math_never_reaches_the_llm(self):
        convert = AsyncMock()
        with patch("reader_text._llm_convert_math", convert):
            for text in ("Plain words.", "It costs $5 and then $10.", "Use `$HOME` here."):
                self.assertEqual(await tts.speakable(text), text)
        convert.assert_not_called()

    async def test_math_is_rewritten_without_its_citation(self):
        convert = AsyncMock(return_value="x squared is fine.")
        with patch("reader_text._llm_convert_math", convert):
            self.assertEqual(await tts.speakable("$x^2$ is fine [§4]."), "x squared is fine.")
        self.assertNotIn("§", convert.call_args.args[1])

    async def test_the_letter_z_is_zed_only_in_a_math_sentence(self):
        convert = AsyncMock(return_value="where Z has mean z, for Gen Zs.")
        with patch("reader_text._llm_convert_math", convert):
            self.assertEqual(await tts.speakable("where $Z$ has mean $z$, for Gen Zs."), "where zed has mean zed, for Gen Zs.")
            self.assertEqual(await tts.speakable("Gen Z likes it."), "Gen Z likes it.")

    async def test_slow_rewrite_falls_back_and_later_sentences_skip_the_wait(self):
        async def hang(_client, _text):
            await asyncio.sleep(30)

        convert = AsyncMock(side_effect=hang)
        with patch("reader_text._llm_convert_math", convert), patch.object(tts, "SPEECH_MATH_TIMEOUT_S", 0.01):
            self.assertEqual(await tts.speakable(r"So $\mu$ here."), "So mu here.")
            self.assertEqual(await tts.speakable(r"And $\sigma$ there."), "And sigma there.")
        self.assertEqual(convert.call_count, 1)

    def test_breaks_skip_equations(self):
        pattern = re.compile(r"(?<=[.!?])\s+")
        text = "See $a. b$ and $$c. d$$ here. Next."
        self.assertEqual([text[m.end():] for m in tts.breaks_outside_math(pattern, text)], ["Next."])
