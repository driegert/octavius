import unittest

try:
    from fastapi.testclient import TestClient
    import main
except ModuleNotFoundError:
    TestClient = None
    main = None


class _FakeConn:
    def close(self):
        return None


class _FakeMCPManager:
    def __init__(self, _config):
        self.tools = [{"function": {"name": "search"}}]

    async def connect_all(self):
        return None

    async def disconnect_all(self):
        return None


@unittest.skipIf(TestClient is None or main is None, "fastapi dependency not installed")
class MainTests(unittest.TestCase):
    def test_health_reports_runtime_state(self):
        original_factory = main.app.state.mcp_manager_factory
        original_db_init = main.app.state.db_init
        main.app.state.mcp_manager_factory = _FakeMCPManager
        main.app.state.db_init = lambda: _FakeConn()
        try:
            with TestClient(main.app) as client:
                response = client.get("/health")
        finally:
            main.app.state.mcp_manager_factory = original_factory
            main.app.state.db_init = original_db_init

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertTrue(body["database_ready"])
        self.assertTrue(body["mcp_connected"])
        self.assertEqual(body["mcp_tool_count"], 1)


if __name__ == "__main__":
    unittest.main()
