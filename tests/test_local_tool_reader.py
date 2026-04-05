import asyncio
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

import local_tool_reader


class LocalToolReaderTests(unittest.TestCase):
    def _runtime_module(self, get_mcp_manager):
        module = ModuleType("runtime")
        module.get_mcp_manager = get_mcp_manager
        return module

    def test_read_document_requires_database_connection(self):
        with patch.dict("sys.modules", {"runtime": self._runtime_module(lambda: None)}):
            result = asyncio.run(local_tool_reader.read_document({"path": "/tmp/missing.pdf"}, session=None))
        self.assertEqual(result, "Error: file not found: /tmp/missing.pdf")

    def test_read_document_rejects_missing_path_arg(self):
        session = SimpleNamespace(conn=object(), db_path="/tmp/test.db")
        with patch.dict("sys.modules", {"runtime": self._runtime_module(lambda: None)}):
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
                patch.dict("sys.modules", {"runtime": self._runtime_module(lambda: None)}),
            ):
                result = asyncio.run(local_tool_reader.read_document({"path": str(path), "title": "Note"}, session=session))

            self.assertIn("document #55", result)
            self.assertEqual(len(created_tasks), 1)

    def test_read_document_returns_error_when_pdf_has_no_mcp_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper.pdf"
            path.write_bytes(b"%PDF-1.3\nbinary")
            session = SimpleNamespace(conn=object(), db_path="/tmp/test.db")

            with (
                patch.object(local_tool_reader, "create_document", return_value=77),
                patch.dict("sys.modules", {"runtime": self._runtime_module(lambda: None)}),
            ):
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
                result = asyncio.run(local_tool_reader.process_pdf_background({"file_path": str(path), "title": "Paper"}, session=session))

            self.assertIn("inbox item #321", result)
            self.assertEqual(len(created_tasks), 1)

    def test_run_pdf_processing_marks_item_failed_when_no_job_id(self):
        updates = []

        class _MCP:
            async def call_tool(self, name, arguments):
                return "conversion backend unavailable"

        async def run():
            with (
                patch.dict("sys.modules", {"runtime": self._runtime_module(lambda: _MCP())}),
                patch.object(local_tool_reader, "_update_saved_item_content", side_effect=lambda *args: updates.append(args)),
            ):
                await local_tool_reader.run_pdf_processing("/tmp/test.db", 7, "/tmp/paper.pdf", "Paper")

        asyncio.run(run())

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0][1], 7)
        self.assertEqual(updates[0][2], "Paper (failed)")
        self.assertIn("PDF conversion failed", updates[0][3])
