import asyncio
import unittest

try:
    from mcp_manager import MCPManager
except ModuleNotFoundError:
    MCPManager = None


class _FakeTool:
    def __init__(self, name, description="desc", input_schema=None):
        self.name = name
        self.description = description
        self.inputSchema = input_schema or {"type": "object", "properties": {}}


class _FakeListTools:
    def __init__(self, tools):
        self.tools = tools


class _FakeBlock:
    def __init__(self, text):
        self.text = text


class _FakeResult:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeSession:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error

    async def call_tool(self, name, arguments):
        if self.error:
            raise self.error
        return _FakeResult(self.text)


@unittest.skipIf(MCPManager is None, "mcp dependency not installed")
class MCPManagerTests(unittest.TestCase):
    def test_register_tools_maps_tool_to_server(self):
        manager = MCPManager({})
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search")]))
        self.assertEqual(manager.get_server_for_tool("search"), "alpha")
        self.assertEqual(manager.tools[0]["function"]["name"], "search")

    def test_call_tool_truncates_large_results(self):
        manager = MCPManager({})
        manager._tool_route["search"] = "alpha"
        manager._sessions["alpha"] = _FakeSession(text="x" * 4505)
        result = asyncio.run(manager.call_tool("search", {"q": "test"}))
        self.assertIn("(truncated)", result)
        self.assertLessEqual(len(result), 4020)

    def test_call_tool_reconnects_lost_session(self):
        manager = MCPManager({})
        manager._tool_route["search"] = "alpha"
        manager._sessions["alpha"] = _FakeSession(error=RuntimeError("connection closed"))

        async def fake_reconnect(server_name):
            manager._sessions[server_name] = _FakeSession(text="ok")
            return True

        manager._reconnect = fake_reconnect
        result = asyncio.run(manager.call_tool("search", {"q": "test"}))
        self.assertEqual(result, "ok")


if __name__ == "__main__":
    unittest.main()
