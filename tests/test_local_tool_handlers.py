import asyncio
import tempfile
import unittest
from pathlib import Path

import tools
from local_tool_downloads import safe_filename


class ConsultSpecialistTests(unittest.TestCase):
    def test_returns_inline_subagent_result(self):
        class FakeSession:
            async def run_inline_subagent(self, domain, task):
                return f"result for {domain}: {task}"

        result = asyncio.run(
            tools._consult_specialist(
                {"domain": "email", "task": "find tax emails"},
                session=FakeSession(),
            )
        )
        self.assertEqual(result, "result for email: find tax emails")

    def test_requires_domain_and_task(self):
        result = asyncio.run(
            tools._consult_specialist({"domain": "email"}, session=object())
        )
        self.assertEqual(result, "Error: domain and task are required.")

    def test_requires_session(self):
        result = asyncio.run(
            tools._consult_specialist(
                {"domain": "email", "task": "x"}, session=None
            )
        )
        self.assertEqual(result, "Error: specialist session unavailable.")


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
