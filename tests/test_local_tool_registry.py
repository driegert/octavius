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
