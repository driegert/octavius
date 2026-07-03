import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import local_tool_documents


class CheckDocumentStatusTests(unittest.TestCase):
    def test_requires_job_id(self):
        result = asyncio.run(local_tool_documents.check_document_status({}, _mcp_manager=object()))
        self.assertEqual(result, "Error: job_id is required.")

    def test_no_manager(self):
        result = asyncio.run(local_tool_documents.check_document_status({"job_id": "job-1"}))
        self.assertIn("MCP manager is unavailable", result)

    def test_unknown_job(self):
        with patch.object(
            local_tool_documents.docproc_client,
            "get_job_status",
            new=_async_return({"id": "job-1", "status": "unknown"}),
        ):
            result = asyncio.run(
                local_tool_documents.check_document_status({"job_id": "job-1"}, _mcp_manager=object())
            )
        self.assertIn("No document conversion job found", result)

    def test_still_running_includes_stage(self):
        with patch.object(
            local_tool_documents.docproc_client,
            "get_job_status",
            new=_async_return({"id": "job-1", "status": "running", "stage": "extracting text"}),
        ):
            result = asyncio.run(
                local_tool_documents.check_document_status({"job_id": "job-1"}, _mcp_manager=object())
            )
        self.assertIn("still in progress", result)
        self.assertIn("extracting text", result)
        self.assertIn("job-1", result)

    def test_error_status_includes_message(self):
        with patch.object(
            local_tool_documents.docproc_client,
            "get_job_status",
            new=_async_return({"id": "job-1", "status": "error", "error_msg": "layout model crashed"}),
        ):
            result = asyncio.run(
                local_tool_documents.check_document_status({"job_id": "job-1"}, _mcp_manager=object())
            )
        self.assertIn("failed", result)
        self.assertIn("layout model crashed", result)

    def test_done_includes_md_path_and_excerpt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            md_path = Path(tmpdir) / "out.md"
            md_path.write_text("# Converted Title\n\nBody text.")
            with patch.object(
                local_tool_documents.docproc_client,
                "get_job_status",
                new=_async_return({"id": "job-1", "status": "done", "result_md_path": str(md_path)}),
            ):
                result = asyncio.run(
                    local_tool_documents.check_document_status({"job_id": "job-1"}, _mcp_manager=object())
                )
        self.assertIn("complete", result)
        self.assertIn(str(md_path), result)
        self.assertIn("Converted Title", result)

    def test_done_without_readable_file_still_reports_path(self):
        with patch.object(
            local_tool_documents.docproc_client,
            "get_job_status",
            new=_async_return({"id": "job-1", "status": "done", "result_md_path": "/nonexistent/out.md"}),
        ):
            result = asyncio.run(
                local_tool_documents.check_document_status({"job_id": "job-1"}, _mcp_manager=object())
            )
        self.assertIn("complete", result)
        self.assertIn("/nonexistent/out.md", result)


def _async_return(value):
    async def _inner(_mcp, _job_id):
        return value
    return _inner


if __name__ == "__main__":
    unittest.main()
