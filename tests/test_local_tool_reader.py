import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import local_tool_reader


class LocalToolReaderTests(unittest.TestCase):
    def test_read_document_requires_database_connection(self):
        result = asyncio.run(local_tool_reader.read_document({"path": "/tmp/missing.pdf"}, session=None))
        self.assertEqual(result, "Error: file not found: /tmp/missing.pdf")

    def test_read_document_rejects_missing_path_arg(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/test.db")
        result = asyncio.run(local_tool_reader.read_document({}, session=session))
        self.assertEqual(result, "Error: path is required.")

    def test_read_document_schedules_markdown_ingest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "note.md"
            path.write_text("hello world")
            session = SimpleNamespace(conn=object(), db_path="/tmp/test.db")
            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with (
                patch.object(local_tool_reader, "create_document", return_value=55),
                patch.object(local_tool_reader, "read_text_file", return_value="hello world"),
                patch.object(local_tool_reader, "is_likely_html", return_value=False),
                patch.object(local_tool_reader.asyncio, "create_task", side_effect=fake_create_task),
            ):
                result = asyncio.run(local_tool_reader.read_document({"path": str(path), "title": "Note"}, session=session))

            self.assertIn("document #55", result)
            self.assertEqual(len(created_tasks), 1)

    def test_read_document_returns_error_when_pdf_has_no_mcp_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper.pdf"
            path.write_bytes(b"%PDF-1.3\nbinary")
            session = SimpleNamespace(conn=object(), db_path="/tmp/test.db")

            with patch.object(local_tool_reader, "create_document", return_value=77):
                result = asyncio.run(local_tool_reader.read_document({"path": str(path), "title": "Paper"}, session=session))

            self.assertEqual(result, "Error: MCP manager unavailable.")

    def test_process_pdf_background_rejects_non_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "note.txt"
            path.write_text("hello")
            session = SimpleNamespace(conn=object(), conv_id=1, db_path="/tmp/test.db")
            result = asyncio.run(local_tool_reader.process_pdf_background({"file_path": str(path)}, session=session))
            self.assertIn("is not a PDF file", result)

    def test_process_pdf_background_schedules_job(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper.pdf"
            path.write_bytes(b"%PDF-1.3\nbinary")
            session = SimpleNamespace(conn=object(), conv_id=99, db_path="/tmp/test.db")
            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with (
                patch("history.save_item", return_value=321),
                patch.object(local_tool_reader.asyncio, "create_task", side_effect=fake_create_task),
            ):
                result = asyncio.run(
                    local_tool_reader.process_pdf_background(
                        {"file_path": str(path), "title": "Paper"},
                        session=session,
                        mcp_manager=object(),
                    )
                )

            self.assertIn("stash item #321", result)
            self.assertEqual(len(created_tasks), 1)

    def test_process_pdf_background_accepts_pdf_without_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper"
            path.write_bytes(b"%PDF-1.3\nbinary")
            session = SimpleNamespace(conn=object(), conv_id=99, db_path="/tmp/test.db")
            created_tasks = []

            def fake_create_task(coro):
                created_tasks.append(coro)
                coro.close()
                return None

            with (
                patch("history.save_item", return_value=321),
                patch.object(local_tool_reader.asyncio, "create_task", side_effect=fake_create_task),
            ):
                result = asyncio.run(
                    local_tool_reader.process_pdf_background(
                        {"file_path": str(path), "title": "Paper"},
                        session=session,
                        mcp_manager=object(),
                    )
                )

            self.assertIn("stash item #321", result)
            self.assertEqual(len(created_tasks), 1)
            self.assertFalse(path.exists())
            self.assertTrue(Path(f"{path}.pdf").exists())

    def test_list_reader_documents_formats_rows(self):
        from datetime import datetime, timezone, timedelta
        recent = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        older = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        fake_conn = object()
        session = SimpleNamespace(conn=fake_conn, db_path="/tmp/test.db")
        fake_rows = [
            {"id": 22, "title": "Creative Data Literacy", "source_type": "pdf",
             "chunk_count": None, "status": "failed",
             "error": "Conversion failed: unhandled errors in a TaskGroup",
             "created_at": recent},
            {"id": 20, "title": "Nano Claude Code README", "source_type": "markdown",
             "chunk_count": 5, "status": "ready", "error": None, "created_at": older},
        ]
        with patch.object(local_tool_reader, "list_documents", return_value=fake_rows) as mock_list:
            result = local_tool_reader.list_reader_documents({"limit": 10}, session=session)
        mock_list.assert_called_once_with(fake_conn, limit=10, status=None)
        self.assertIn("Reader documents", result)
        self.assertIn("#22 [failed/pdf]", result)
        self.assertIn("Creative Data Literacy", result)
        self.assertIn("error: Conversion failed", result)
        self.assertIn("#20 [ready/markdown]", result)

    def test_list_reader_documents_requires_session(self):
        result = local_tool_reader.list_reader_documents({}, session=None)
        self.assertEqual(result, "Error: no database connection available.")

    def test_list_reader_documents_reports_empty(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/test.db")
        with patch.object(local_tool_reader, "list_documents", return_value=[]):
            result = local_tool_reader.list_reader_documents({"status": "processing"}, session=session)
        self.assertEqual(result, "No reader documents found (status=processing).")

    def test_run_pdf_processing_marks_item_failed_when_no_job_id(self):
        updates = []

        class _MCP:
            async def call_tool(self, name, arguments):
                return "conversion backend unavailable"

        async def run():
            with (
                patch.object(local_tool_reader, "_update_saved_item_content", side_effect=lambda *args: updates.append(args)),
            ):
                await local_tool_reader.run_pdf_processing("/tmp/test.db", 7, "/tmp/paper.pdf", "Paper", _MCP())

        asyncio.run(run())

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1], 7)
        self.assertEqual(updates[0][2], "Paper (failed)")
        self.assertIn("PDF conversion failed", updates[0][3])
