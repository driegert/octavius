import tempfile
import unittest
from pathlib import Path

from document_sources import decode_text_bytes, ensure_pdf_suffix, is_pdf_file, is_pdf_response, read_text_file


class DocumentSourceTests(unittest.TestCase):
    def test_decode_text_bytes_falls_back_from_utf8(self):
        raw = "ÄrXiv note".encode("latin-1")
        decoded = decode_text_bytes(raw)
        self.assertEqual(decoded, "ÄrXiv note")

    def test_is_pdf_response_detects_pdf_by_magic_bytes(self):
        self.assertTrue(
            is_pdf_response(
                "https://arxiv.org/pdf/1234.5678",
                "application/octet-stream",
                b"%PDF-1.4 binary",
                "",
            )
        )

    def test_read_text_file_uses_robust_decoding(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sample.txt"
            path.write_bytes("Résumé".encode("latin-1"))
            self.assertEqual(read_text_file(path), "Résumé")

    def test_is_pdf_file_detects_magic_bytes_without_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "arxiv_blob"
            path.write_bytes(b"%PDF-1.4\nrest")
            self.assertTrue(is_pdf_file(path))

    def test_ensure_pdf_suffix_appends_extension(self):
        path = ensure_pdf_suffix("/tmp/arxiv_blob")
        self.assertEqual(path.name, "arxiv_blob.pdf")


if __name__ == "__main__":
    unittest.main()
