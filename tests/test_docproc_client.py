import unittest
from unittest.mock import patch

import docproc_client


class FakeMCP:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if not self.outcomes:
            raise AssertionError("no scripted MCP outcome left")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SubmitJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_job_returns_job_id(self):
        mcp = FakeMCP(["Started conversion. Job ID: 17"])

        job_id = await docproc_client.submit_job(mcp, "/tmp/paper.pdf")

        self.assertEqual(job_id, "17")
        self.assertEqual(mcp.calls, [("convert_pdf_to_md", {"file_path": "/tmp/paper.pdf"})])

    async def test_submit_job_raises_for_missing_file(self):
        await self._assert_submit_failure("File does not exist at /tmp/missing.pdf")

    async def test_submit_job_raises_for_non_pdf(self):
        await self._assert_submit_failure("File does not appear to be a PDF: /tmp/file.txt")

    async def test_submit_job_raises_for_transport_error_text(self):
        await self._assert_submit_failure("Error: server 'document-processing' not connected")

    async def test_submit_job_raises_for_unparseable_text(self):
        await self._assert_submit_failure("started maybe")

    async def test_submit_job_evicts_stale_cached_outcome_for_reused_id(self):
        # Wrapper job ids are a per-process counter; after a stdio restart a
        # NEW job can reuse an id we cached a terminal outcome for.
        docproc_client._completed["17"] = {"id": "17", "status": "error", "error_msg": "old"}
        mcp = FakeMCP(["Started conversion. Job ID: 17"])

        job_id = await docproc_client.submit_job(mcp, "/tmp/paper.pdf")

        self.assertEqual(job_id, "17")
        self.assertNotIn("17", docproc_client._completed)

    async def _assert_submit_failure(self, text):
        mcp = FakeMCP([text])
        with self.assertRaises(docproc_client.DocprocError) as ctx:
            await docproc_client.submit_job(mcp, "/tmp/paper.pdf")
        self.assertEqual(str(ctx.exception), text)


class PollJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        docproc_client._completed.clear()

    async def test_poll_job_waits_until_done_and_parses_paths(self):
        done_text = _done_text("/tmp/out.md", "/tmp/out_meta.json")
        mcp = FakeMCP([
            "Job 17 is still in progress. Current stage: remote convert. Call this tool again to continue waiting.",
            done_text,
        ])

        with patch("docproc_client.asyncio.sleep", new=_instant_sleep):
            row = await docproc_client.poll_job(mcp, "17", interval=0.001, timeout=5)

        self.assertEqual(row, {
            "id": "17",
            "status": "done",
            "result_md_path": "/tmp/out.md",
            "result_meta_path": "/tmp/out_meta.json",
            "result_text": done_text,
        })
        self.assertEqual([call[0] for call in mcp.calls], ["get_conversion_result", "get_conversion_result"])

    async def test_poll_job_raises_and_caches_conversion_failure(self):
        text = "Conversion failed: remote converter crashed"
        mcp = FakeMCP([text])

        with self.assertRaises(docproc_client.DocprocError) as ctx:
            await docproc_client.poll_job(mcp, "17", interval=0.001, timeout=5)

        self.assertEqual(str(ctx.exception), text)
        self.assertEqual(docproc_client._completed["17"], {"id": "17", "status": "error", "error_msg": text})

    async def test_poll_job_times_out(self):
        mcp = FakeMCP([
            "Job 17 is still in progress. Current stage: upload. Call this tool again to continue waiting.",
            "Job 17 is still in progress. Current stage: convert. Call this tool again to continue waiting.",
        ])

        with patch("docproc_client.asyncio.sleep", new=_instant_sleep), \
             patch("docproc_client.time.monotonic", side_effect=[0.0, 0.0, 2.0]):
            with self.assertRaises(docproc_client.DocprocError) as ctx:
                await docproc_client.poll_job(mcp, "17", interval=0.001, timeout=1)

        self.assertIn("timed out", str(ctx.exception))
        # A timeout is NOT cached as terminal: the wrapper may still finish
        # the job, so a later get_job_status must ask it live.
        self.assertNotIn("17", docproc_client._completed)

    async def test_terminal_cache_avoids_second_mcp_read(self):
        done_text = _done_text("/tmp/out.md", "/tmp/out_meta.json")
        mcp = FakeMCP([done_text, "Unknown job ID: 17. Active jobs: none"])

        row = await docproc_client.poll_job(mcp, "17", interval=0.001, timeout=5)
        cached = await docproc_client.get_job_status(mcp, "17")

        self.assertEqual(cached, row)
        self.assertEqual(len(mcp.calls), 1)


class GetJobStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        docproc_client._completed.clear()

    async def test_get_job_status_done(self):
        text = _done_text("/tmp/out.md", "/tmp/out_meta.json")
        mcp = FakeMCP([text])

        row = await docproc_client.get_job_status(mcp, "17")

        self.assertEqual(row["status"], "done")
        self.assertEqual(row["result_md_path"], "/tmp/out.md")
        self.assertEqual(row["result_meta_path"], "/tmp/out_meta.json")
        self.assertEqual(row["result_text"], text)

    async def test_get_job_status_conversion_failed(self):
        text = "Conversion failed: bad pdf"
        mcp = FakeMCP([text])

        row = await docproc_client.get_job_status(mcp, "17")

        self.assertEqual(row, {"id": "17", "status": "error", "error_msg": text})

    async def test_get_job_status_running(self):
        mcp = FakeMCP([
            "Job 17 is still in progress. Current stage: extracting text. Call this tool again to continue waiting."
        ])

        row = await docproc_client.get_job_status(mcp, "17")

        self.assertEqual(row, {"id": "17", "status": "running", "stage": "extracting text"})

    async def test_get_job_status_unknown(self):
        mcp = FakeMCP(["Unknown job ID: 17. Active jobs: none"])

        row = await docproc_client.get_job_status(mcp, "17")

        self.assertEqual(row, {"id": "17", "status": "unknown"})

    async def test_get_job_status_transport_error_text(self):
        text = "Error: unknown tool 'get_conversion_result'"
        mcp = FakeMCP([text])

        row = await docproc_client.get_job_status(mcp, "17")

        self.assertEqual(row, {"id": "17", "status": "error", "error_msg": text})
        # Transport trouble is possibly transient — never cached as terminal.
        self.assertNotIn("17", docproc_client._completed)

    async def test_get_job_status_running_and_transport_error_not_cached(self):
        mcp = FakeMCP([
            "Error calling get_conversion_result: broken pipe",
            _done_text("/tmp/out.md", "/tmp/out_meta.json"),
        ])

        first = await docproc_client.get_job_status(mcp, "17")
        second = await docproc_client.get_job_status(mcp, "17")

        self.assertEqual(first["status"], "error")
        self.assertEqual(second["status"], "done")


async def _instant_sleep(_seconds):
    return None


def _done_text(md_path, meta_path):
    return (
        "PDF converted to markdown successfully\n"
        f"  Text file: {md_path}\n"
        f"  Metadata (JSON): {meta_path}\n"
        "  Output directory: /tmp"
    )


if __name__ == "__main__":
    unittest.main()
