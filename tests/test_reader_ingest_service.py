import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reader_ingest_handlers as handlers
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
    def test_retry_reader_document_rejects_missing_document(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE reader_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_path TEXT,
                    saved_item_id INTEGER,
                    speech_file TEXT,
                    original_md_file TEXT,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'processing',
                    error TEXT,
                    last_chunk INTEGER NOT NULL DEFAULT 0,
                    last_sentence INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )"""
            )
            conn.commit()
            conn.close()

            with self.assertRaises(service.ReaderIngestError) as ctx:
                asyncio.run(service.retry_reader_document(db_path, _FakeMCP(), 99))
            self.assertEqual(ctx.exception.status_code, 404)

    def test_retry_reader_document_requeues_failed_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.db"
            pdf_path = Path(tmpdir) / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.3\nbinary")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """CREATE TABLE reader_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_path TEXT,
                    saved_item_id INTEGER,
                    speech_file TEXT,
                    original_md_file TEXT,
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'processing',
                    error TEXT,
                    last_chunk INTEGER NOT NULL DEFAULT 0,
                    last_sentence INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO reader_documents
                   (title, source_type, source_path, status, error, speech_file, original_md_file, chunk_count, last_chunk, last_sentence, created_at)
                   VALUES (?, ?, ?, 'failed', 'boom', 'old.json', NULL, 3, 2, 1, 'now')""",
                ("Paper", "pdf", str(pdf_path)),
            )
            conn.commit()
            conn.close()

            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with patch.object(handlers.asyncio, "create_task", side_effect=fake_create_task):
                result = asyncio.run(service.retry_reader_document(db_path, _FakeMCP(), 1))

            self.assertEqual(result, {"id": 1, "status": "processing"})
            self.assertEqual(len(created_tasks), 1)

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT status, error, speech_file, chunk_count, last_chunk, last_sentence FROM reader_documents WHERE id = 1"
            ).fetchone()
            conn.close()
            self.assertEqual(row, ("processing", None, None, 0, 0, 0))

    def test_extract_article_text_returns_none_when_both_attempts_fail(self):
        fake = _FakeTrafilatura()
        with patch.object(handlers, "get_trafilatura", return_value=fake):
            self.assertIsNone(handlers.extract_article_text("<html></html>", service.ReaderIngestError))

    def test_refine_title_from_web_page_keeps_explicit_title(self):
        fake = _FakeTrafilatura(metadata=None)
        with patch.object(handlers, "get_trafilatura", return_value=fake):
            title = handlers.refine_title_from_web_page(
                "<title>Paper - arXiv</title>",
                "Chosen title",
                "https://arxiv.org/abs/1",
                service.ReaderIngestError,
            )
        self.assertEqual(title, "Chosen title")

    def test_start_reader_ingest_rejects_missing_file(self):
        with self.assertRaises(service.ReaderIngestError) as ctx:
            asyncio.run(
                service.start_reader_ingest(
                    db_path=Path("/tmp/octavius-test.db"),
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
                patch.object(service, "start_file_ingest", return_value={"id": 12, "status": "processing"}) as start_file_ingest,
            ):
                result = asyncio.run(
                    service.start_reader_ingest(
                        db_path=Path("/tmp/octavius-test.db"),
                        mcp_manager=_FakeMCP(),
                        body={"source": "file", "path": str(path), "title": "Note"},
                    )
                )

            self.assertEqual(result, {"id": 12, "status": "processing"})
            start_file_ingest.assert_called_once()
            self.assertEqual(len(created_tasks), 0)

    def test_start_reader_ingest_detects_pdf_without_pdf_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "2604_1.02238"
            path.write_bytes(b"%PDF-1.3\nbinary")
            db_path = Path("/tmp/octavius-test.db")

            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with (
                patch.object(service, "start_file_ingest", return_value={"id": 21, "status": "processing"}) as start_file_ingest,
            ):
                result = asyncio.run(
                    service.start_reader_ingest(
                        db_path=db_path,
                        mcp_manager=_FakeMCP(),
                        body={"source": "file", "path": str(path), "title": "Paper"},
                    )
                )

            self.assertEqual(result, {"id": 21, "status": "processing"})
            start_file_ingest.assert_called_once()
            self.assertEqual(len(created_tasks), 0)

    def test_resolve_markdown_output_falls_back_to_only_md_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            actual = out_dir / "2604.md"
            actual.write_text("content")
            resolved = handlers.resolve_markdown_output(out_dir / "2604.02238_1.md")
            self.assertEqual(resolved, actual)


if __name__ == "__main__":
    unittest.main()
