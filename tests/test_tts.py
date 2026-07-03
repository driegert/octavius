import unittest

from tts import speechify


class SpeechifyTests(unittest.TestCase):
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

    def test_preserves_meaning_bearing_characters(self):
        # Snake_case identifiers and lone asterisks (e.g. multiplication) must survive.
        self.assertEqual(speechify("call foo_bar_baz please"), "call foo_bar_baz please")
        self.assertEqual(speechify("compute 3 * 4 exactly"), "compute 3 * 4 exactly")

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
