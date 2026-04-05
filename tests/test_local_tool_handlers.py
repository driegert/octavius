import tempfile
import unittest
from pathlib import Path

from local_tool_downloads import safe_filename


class LocalToolHandlerTests(unittest.TestCase):
    def test_safe_filename_adds_pdf_suffix_for_arxiv_pdf_url(self):
        self.assertEqual(
            safe_filename("https://arxiv.org/pdf/2604.02238", None),
            "2604.02238.pdf",
        )

    def test_safe_filename_keeps_explicit_filename_basename_only(self):
        self.assertEqual(
            safe_filename("https://example.com/a.pdf", "../unsafe.pdf"),
            "unsafe.pdf",
        )


if __name__ == "__main__":
    unittest.main()
