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
        manager = MCPManager({"alpha": {"transport": "http"}})
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search")]))
        self.assertEqual(manager.get_server_for_tool("search"), "alpha")
        self.assertEqual(manager.tools[0]["function"]["name"], "search")
        self.assertEqual(manager.get_health()["servers"]["alpha"]["tool_count"], 1)

    def test_call_tool_truncates_large_results(self):
        manager = MCPManager({"alpha": {"transport": "http"}})
        manager._tool_route["search"] = "alpha"
        manager._sessions["alpha"] = _FakeSession(text="x" * 4505)
        result = asyncio.run(manager.call_tool("search", {"q": "test"}))
        self.assertIn("(truncated)", result)
        self.assertLessEqual(len(result), 4020)
        self.assertTrue(manager.get_health()["servers"]["alpha"]["connected"])

    def test_call_tool_reconnects_lost_session(self):
        manager = MCPManager({"alpha": {"transport": "http"}})
        manager._tool_route["search"] = "alpha"
        manager._sessions["alpha"] = _FakeSession(error=RuntimeError("connection closed"))

        async def fake_reconnect(server_name):
            manager._sessions[server_name] = _FakeSession(text="ok")
            manager._server_status[server_name]["connected"] = True
            manager._server_status[server_name]["error"] = None
            return True

        manager._reconnect = fake_reconnect
        result = asyncio.run(manager.call_tool("search", {"q": "test"}))
        self.assertEqual(result, "ok")

    def test_get_tools_for_servers_filters_by_server_name(self):
        manager = MCPManager({
            "alpha": {"transport": "http"},
            "beta": {"transport": "http"},
        })
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search"), _FakeTool("get")]))
        manager._register_tools("beta", _FakeListTools([_FakeTool("list_emails")]))
        alpha_tools = manager.get_tools_for_servers(["alpha"])
        self.assertEqual(len(alpha_tools), 2)
        names = {t["function"]["name"] for t in alpha_tools}
        self.assertEqual(names, {"search", "get"})

    def test_get_tools_for_servers_returns_empty_for_unknown(self):
        manager = MCPManager({"alpha": {"transport": "http"}})
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search")]))
        self.assertEqual(manager.get_tools_for_servers(["nonexistent"]), [])

    def test_collision_first_owner_keeps_name_newcomer_is_prefixed(self):
        manager = MCPManager({
            "alpha": {"transport": "http"},
            "beta-srv": {"transport": "http"},
        })
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search")]))
        with self.assertLogs("mcp_manager", level="WARNING") as cm:
            manager._register_tools("beta-srv", _FakeListTools([_FakeTool("search")]))
        self.assertTrue(any("collision" in msg for msg in cm.output))
        # The earlier owner is never overwritten...
        self.assertEqual(manager.get_server_for_tool("search"), "alpha")
        # ...and the newcomer stays reachable under a server-keyed name.
        self.assertEqual(manager.get_server_for_tool("beta_srv_search"), "beta-srv")
        names = [t["function"]["name"] for t in manager.tools]
        self.assertEqual(sorted(names), ["beta_srv_search", "search"])

    def test_collision_renamed_tool_calls_upstream_name(self):
        manager = MCPManager({
            "alpha": {"transport": "http"},
            "beta": {"transport": "http"},
        })
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search")]))
        with self.assertLogs("mcp_manager", level="WARNING"):
            manager._register_tools("beta", _FakeListTools([_FakeTool("search")]))
        seen = []

        class _Recording(_FakeSession):
            async def call_tool(self, name, arguments):
                seen.append(name)
                return _FakeResult("ok")

        manager._sessions["beta"] = _Recording()
        self.assertEqual(asyncio.run(manager.call_tool("beta_search", {})), "ok")
        self.assertEqual(seen, ["search"])

    def test_tool_prefix_names_and_routes_to_upstream(self):
        manager = MCPManager({"email-srv": {"transport": "http", "tool_prefix": "email"}})
        manager._register_tools(
            "email-srv",
            _FakeListTools([_FakeTool("keyword_search"), _FakeTool("stats"), _FakeTool("email_legacy")]),
        )
        names = {t["function"]["name"] for t in manager.tools}
        # Prefix is not doubled when the upstream name already carries it
        # (same rule as pi-mcp-adapter's formatToolName).
        self.assertEqual(names, {"email_keyword_search", "email_stats", "email_legacy"})
        seen = []

        class _Recording(_FakeSession):
            async def call_tool(self, name, arguments):
                seen.append(name)
                return _FakeResult("ok")

        manager._sessions["email-srv"] = _Recording()
        asyncio.run(manager.call_tool("email_stats", {}))
        self.assertEqual(seen, ["stats"])

    def test_call_server_tool_bypasses_display_names(self):
        manager = MCPManager({"vault": {"transport": "http", "tool_prefix": "vault"}})
        manager._register_tools("vault", _FakeListTools([_FakeTool("search")]))
        seen = []

        class _Recording(_FakeSession):
            async def call_tool(self, name, arguments):
                seen.append(name)
                return _FakeResult("hits")

        manager._sessions["vault"] = _Recording()
        self.assertEqual(asyncio.run(manager.call_server_tool("vault", "search", {"query": "x"})), "hits")
        self.assertEqual(seen, ["search"])
        self.assertIn("unknown server", asyncio.run(manager.call_server_tool("nope", "search", {})))

    def test_reregister_drops_tools_removed_upstream(self):
        manager = MCPManager({"alpha": {"transport": "http"}})
        manager._register_tools("alpha", _FakeListTools([_FakeTool("old_name"), _FakeTool("keep")]))
        manager._register_tools("alpha", _FakeListTools([_FakeTool("new_name"), _FakeTool("keep")]))
        self.assertEqual(manager.get_registered_tool_names(), {"new_name", "keep"})
        self.assertEqual(len(manager.tools), 2)

    def test_no_collision_warning_on_same_server_reregister(self):
        manager = MCPManager({"alpha": {"transport": "http"}})
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search")]))
        # Re-registration after reconnect is expected and must be silent.
        # assertNoLogs (3.10+) would be cleaner; this captures all and checks.
        import logging as _logging
        collision_logs = []

        class _Capture(_logging.Handler):
            def emit(self, record):
                if "collision" in record.getMessage():
                    collision_logs.append(record.getMessage())

        handler = _Capture(level=_logging.WARNING)
        _logging.getLogger("mcp_manager").addHandler(handler)
        try:
            manager._register_tools("alpha", _FakeListTools([_FakeTool("search")]))
        finally:
            _logging.getLogger("mcp_manager").removeHandler(handler)
        self.assertEqual(collision_logs, [])

    def test_allowlist_drift_logs_warning(self):
        manager = MCPManager({
            "alpha": {
                "transport": "stdio",
                "tool_allowlist": ["kept_tool", "vanished_tool", "also_gone"],
            },
        })
        # Only "kept_tool" is present upstream.
        with self.assertLogs("mcp_manager", level="WARNING") as cm:
            manager._register_tools("alpha", _FakeListTools([_FakeTool("kept_tool")]))
        joined = "\n".join(cm.output)
        self.assertIn("Allowlist drift", joined)
        self.assertIn("vanished_tool", joined)
        self.assertIn("also_gone", joined)

    def test_allowlist_no_drift_no_warning(self):
        manager = MCPManager({
            "alpha": {"transport": "stdio", "tool_allowlist": ["a", "b"]},
        })
        import logging as _logging
        drift_logs = []

        class _Capture(_logging.Handler):
            def emit(self, record):
                if "Allowlist drift" in record.getMessage():
                    drift_logs.append(record.getMessage())

        handler = _Capture(level=_logging.WARNING)
        _logging.getLogger("mcp_manager").addHandler(handler)
        try:
            manager._register_tools("alpha", _FakeListTools([_FakeTool("a"), _FakeTool("b"), _FakeTool("c")]))
        finally:
            _logging.getLogger("mcp_manager").removeHandler(handler)
        self.assertEqual(drift_logs, [])

    def test_get_registered_tool_names_returns_all_routed_names(self):
        manager = MCPManager({
            "alpha": {"transport": "http"},
            "beta": {"transport": "http"},
        })
        manager._register_tools("alpha", _FakeListTools([_FakeTool("search"), _FakeTool("get")]))
        manager._register_tools("beta", _FakeListTools([_FakeTool("list_emails")]))
        self.assertEqual(
            manager.get_registered_tool_names(),
            {"search", "get", "list_emails"},
        )

    def test_get_health_reports_degraded_when_some_servers_disconnected(self):
        manager = MCPManager(
            {
                "alpha": {"transport": "http"},
                "beta": {"transport": "stdio"},
            }
        )
        manager._server_status["alpha"]["connected"] = True
        manager._server_status["alpha"]["tool_count"] = 2
        health = manager.get_health()
        self.assertTrue(health["ready"])
        self.assertTrue(health["degraded"])
        self.assertEqual(health["connected_servers"], 1)


if __name__ == "__main__":
    unittest.main()
