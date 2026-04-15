import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import subagent


class _FakeMCP:
    def __init__(self, tools=None, call_results=None):
        self._tools = [{"function": {"name": "search_emails"}}] if tools is None else tools
        self._call_results = call_results if call_results is not None else {}

    def get_tools_for_servers(self, server_names):
        return self._tools

    async def call_tool(self, name, arguments):
        return self._call_results.get(name, f"Result for {name}")


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_domain_returns_error(self):
        result = await subagent.run_subagent("do stuff", "bogus", _FakeMCP())
        self.assertIn("unknown delegation domain", result)

    async def test_no_tools_returns_error(self):
        mcp = _FakeMCP(tools=[])
        result = await subagent.run_subagent("do stuff", "email", mcp)
        self.assertIn("no tools available", result)

    async def test_text_only_response(self):
        """Subagent returns text when LLM responds without tool calls."""
        message = {"content": "Found 3 emails from the dean.", "tool_calls": None}
        with patch.object(subagent.llm_client, "complete_with_tools", new_callable=AsyncMock, return_value=message):
            result = await subagent.run_subagent("check email from the dean", "email", _FakeMCP())
        self.assertEqual(result, "Found 3 emails from the dean.")

    async def test_think_tags_stripped(self):
        message = {"content": "<think>planning...</think>Here are the results.", "tool_calls": None}
        with patch.object(subagent.llm_client, "complete_with_tools", new_callable=AsyncMock, return_value=message):
            result = await subagent.run_subagent("check email", "email", _FakeMCP())
        self.assertEqual(result, "Here are the results.")

    async def test_tool_call_round_then_text(self):
        """Subagent executes a tool call and then returns final text."""
        tool_message = {
            "content": None,
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_emails", "arguments": '{"query": "dean"}'},
            }],
        }
        final_message = {"content": "Found an email about the budget meeting.", "tool_calls": None}

        call_count = 0

        async def mock_complete(payload):
            nonlocal call_count
            call_count += 1
            return tool_message if call_count == 1 else final_message

        mcp = _FakeMCP(call_results={"search_emails": "3 emails found"})
        with patch.object(subagent.llm_client, "complete_with_tools", side_effect=mock_complete):
            result = await subagent.run_subagent("check email from the dean", "email", mcp)

        self.assertEqual(result, "Found an email about the budget meeting.")

    async def test_max_rounds_exhausted(self):
        """Returns last text when max rounds exceeded."""
        tool_message = {
            "content": "Still working...",
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_emails", "arguments": "{}"},
            }],
        }

        async def always_tool_call(payload):
            return tool_message

        mcp = _FakeMCP(call_results={"search_emails": "results"})
        with patch.object(subagent.llm_client, "complete_with_tools", side_effect=always_tool_call):
            result = await subagent.run_subagent("check email", "email", mcp)

        self.assertEqual(result, "Still working...")

    async def test_llm_failure_returns_error(self):
        with patch.object(subagent.llm_client, "complete_with_tools", new_callable=AsyncMock, return_value=None):
            result = await subagent.run_subagent("check email", "email", _FakeMCP())
        self.assertIn("all LLM endpoints failed", result)

    async def test_result_truncated_to_max_chars(self):
        long_text = "x" * 10000
        message = {"content": long_text, "tool_calls": None}
        with patch.object(subagent.llm_client, "complete_with_tools", new_callable=AsyncMock, return_value=message):
            result = await subagent.run_subagent("check email", "email", _FakeMCP())
        self.assertEqual(len(result), subagent.MAX_RESULT_CHARS)

    async def test_status_callback_forwarded(self):
        tool_message = {
            "content": None,
            "role": "assistant",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_emails", "arguments": "{}"},
            }],
        }
        final_message = {"content": "Done.", "tool_calls": None}

        call_count = 0

        async def mock_complete(payload):
            nonlocal call_count
            call_count += 1
            return tool_message if call_count == 1 else final_message

        statuses = []

        async def status_cb(text):
            statuses.append(text)

        mcp = _FakeMCP(call_results={"search_emails": "results"})
        with patch.object(subagent.llm_client, "complete_with_tools", side_effect=mock_complete):
            await subagent.run_subagent("check email", "email", mcp, status_callback=status_cb)

        self.assertTrue(len(statuses) > 0)

    async def test_tasks_domain_exists(self):
        self.assertIn("tasks", subagent.SUBAGENT_DOMAINS)
        self.assertEqual(subagent.SUBAGENT_DOMAINS["tasks"]["servers"], ["vikunja-tasks"])

    async def test_research_domain_exists(self):
        self.assertIn("research", subagent.SUBAGENT_DOMAINS)
        self.assertEqual(subagent.SUBAGENT_DOMAINS["research"]["servers"], ["openalex"])


if __name__ == "__main__":
    unittest.main()
