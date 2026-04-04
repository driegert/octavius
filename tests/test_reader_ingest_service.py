import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reader_ingest_service as service


class _FakeMCP:
    async def call_tool(self, name, arguments):
        return ""


class _FakeTrafilatura:
    def __init__(self, extract_result=None, metadata=None):
        self._extract_result = extract_result
        self._metadata = metadata

    def extract(self, *args, **kwargs):
        return self._extract_result

    def extract_metadata(self, *args, **kwargs):
        return self._metadata


class ReaderIngestServiceTests(unittest.TestCase):
    def test_extract_article_text_returns_none_when_both_attempts_fail(self):
        fake = _FakeTrafilatura()
        with patch.object(service, "_get_trafilatura", return_value=fake):
            self.assertIsNone(service._extract_article_text("<html></html>"))

    def test_refine_title_from_web_page_keeps_explicit_title(self):
        fake = _FakeTrafilatura(metadata=None)
        with patch.object(service, "_get_trafilatura", return_value=fake):
            title = service._refine_title_from_web_page("<title>Paper - arXiv</title>", "Chosen title", "https://arxiv.org/abs/1")
        self.assertEqual(title, "Chosen title")

    def test_start_reader_ingest_rejects_missing_file(self):
        with self.assertRaises(service.ReaderIngestError) as ctx:
            asyncio.run(
                service.start_reader_ingest(
                    conn=None,
                    mcp_manager=_FakeMCP(),
                    body={"source": "file", "path": "/does/not/exist.pdf", "title": "Missing"},
                )
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_start_reader_ingest_schedules_markdown_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "note.md"
            path.write_text("hello world")

            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with (
                patch.object(service.reader, "create_document", return_value=12),
                patch.object(service.asyncio, "create_task", side_effect=fake_create_task),
            ):
                result = asyncio.run(
                    service.start_reader_ingest(
                        conn=object(),
                        mcp_manager=_FakeMCP(),
                        body={"source": "file", "path": str(path), "title": "Note"},
                    )
                )

            self.assertEqual(result, {"id": 12, "status": "processing"})
            self.assertEqual(len(created_tasks), 1)

    def test_start_reader_ingest_detects_pdf_without_pdf_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "2604_1.02238"
            path.write_bytes(b"%PDF-1.3\nbinary")
            conn = object()

            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with (
                patch.object(service.reader, "create_document", return_value=21) as create_document,
                patch.object(service.asyncio, "create_task", side_effect=fake_create_task),
            ):
                result = asyncio.run(
                    service.start_reader_ingest(
                        conn=conn,
                        mcp_manager=_FakeMCP(),
                        body={"source": "file", "path": str(path), "title": "Paper"},
                    )
                )

            self.assertEqual(result, {"id": 21, "status": "processing"})
            create_document.assert_called_once_with(conn, "Paper", "pdf", source_path=str(path) + ".pdf")
            self.assertEqual(len(created_tasks), 1)
            self.assertFalse(path.exists())
            self.assertTrue(Path(str(path) + ".pdf").exists())

    def test_resolve_markdown_output_falls_back_to_only_md_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            actual = out_dir / "2604.md"
            actual.write_text("content")
            resolved = service._resolve_markdown_output(out_dir / "2604.02238_1.md")
            self.assertEqual(resolved, actual)


if __name__ == "__main__":
    unittest.main()
