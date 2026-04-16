import asyncio
import unittest
from unittest.mock import patch

import local_tool_registry


class LocalToolRegistryTests(unittest.TestCase):
    def test_call_local_tool_routes_sync_handler(self):
        with patch.object(local_tool_registry, "get_local_tool_handlers", return_value={"x": lambda args, session: f"sync:{args['v']}"}):
            result = asyncio.run(local_tool_registry.call_local_tool("x", {"v": "ok"}))
        self.assertEqual(result, "sync:ok")

    def test_call_local_tool_routes_async_handler(self):
        async def handler(args, session):
            return f"async:{args['v']}"

        with patch.object(local_tool_registry, "get_local_tool_handlers", return_value={"x": handler}):
            result = asyncio.run(local_tool_registry.call_local_tool("x", {"v": "ok"}))
        self.assertEqual(result, "async:ok")

    def test_call_local_tool_rejects_unknown_name(self):
        with patch.object(local_tool_registry, "get_local_tool_handlers", return_value={}):
            result = asyncio.run(local_tool_registry.call_local_tool("missing", {}))
        self.assertEqual(result, "Error: unknown local tool 'missing'")


class ValidateLocalToolRegistryTests(unittest.TestCase):
    def test_real_registry_has_no_drift(self):
        """The shipped local_tool_specs.TOOLS and tools.get_local_tool_handlers
        must stay in sync. This is the drift guard: if someone adds a new spec
        or handler without wiring the other side, this test fails."""
        import tools
        self.assertEqual(tools.validate_local_tool_registry(), [])

    def test_spec_without_handler_reported(self):
        import tools
        fake_tools = tools.TOOLS + [
            {"type": "function", "function": {"name": "orphan_spec", "description": "", "parameters": {}}},
        ]
        with patch.object(tools, "TOOLS", fake_tools):
            issues = tools.validate_local_tool_registry()
        self.assertTrue(any("orphan_spec" in i and "no handler" in i for i in issues))

    def test_handler_without_spec_reported(self):
        import tools
        real_handlers = tools.get_local_tool_handlers()
        extra_handlers = {**real_handlers, "orphan_handler": lambda args, session: ""}
        with patch.object(tools, "get_local_tool_handlers", return_value=extra_handlers):
            issues = tools.validate_local_tool_registry()
        self.assertTrue(any("orphan_handler" in i and "no spec" in i for i in issues))
