import unittest
import sqlite3
import tempfile
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    import main
except ModuleNotFoundError:
    TestClient = None
    main = None


class _FakeConn:
    def execute(self, *_args, **_kwargs):
        class _Cursor:
            rowcount = 0
        return _Cursor()

    def commit(self):
        return None

    def close(self):
        return None


class _FakeMCPManager:
    def __init__(self, _config):
        self.tools = [{"function": {"name": "search"}}]

    async def connect_all(self):
        return None

    async def disconnect_all(self):
        return None

    def get_registered_tool_names(self):
        return {t["function"]["name"] for t in self.tools}

    def get_health(self):
        return {
            "configured_servers": 2,
            "connected_servers": 2,
            "ready": True,
            "degraded": False,
            "servers": {
                "alpha": {"connected": True, "tool_count": 1, "error": None, "transport": "http"},
                "beta": {"connected": True, "tool_count": 0, "error": None, "transport": "stdio"},
            },
        }


class _DegradedMCPManager(_FakeMCPManager):
    def get_health(self):
        return {
            "configured_servers": 2,
            "connected_servers": 1,
            "ready": True,
            "degraded": True,
            "servers": {
                "alpha": {"connected": True, "tool_count": 1, "error": None, "transport": "http"},
                "beta": {"connected": False, "tool_count": 0, "error": "connect_failed", "transport": "stdio"},
            },
        }


class _StartingMCPManager(_FakeMCPManager):
    def get_health(self):
        return {
            "configured_servers": 2,
            "connected_servers": 0,
            "ready": False,
            "degraded": True,
            "servers": {
                "alpha": {"connected": False, "tool_count": 0, "error": "connect_failed", "transport": "http"},
                "beta": {"connected": False, "tool_count": 0, "error": "connect_failed", "transport": "stdio"},
            },
        }


def _init_inbox_delete_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE saved_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        CREATE TABLE saved_item_embeddings (
            saved_item_id INTEGER PRIMARY KEY,
            embedding BLOB
        );
        CREATE TABLE reader_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_path TEXT,
            saved_item_id INTEGER REFERENCES saved_items(id),
            speech_file TEXT,
            original_md_file TEXT,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ready',
            error TEXT,
            last_chunk INTEGER NOT NULL DEFAULT 0,
            last_sentence INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT 'now',
            updated_at TEXT
        );
        """
    )
    conn.commit()
    return conn


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
        self.assertTrue(body["alive"])
        self.assertTrue(body["ready"])
        self.assertFalse(body["degraded"])
        self.assertTrue(body["database_ready"])
        self.assertTrue(body["mcp_connected"])
        self.assertEqual(body["mcp_tool_count"], 1)
        self.assertIn("mcp", body)
        self.assertEqual(body["mcp"]["connected_servers"], 2)
        self.assertIn("llm_chain", body)
        self.assertEqual(body["llm_chain"]["configured_endpoints"], 3)

    def test_health_reports_degraded_when_only_partial_runtime_is_ready(self):
        original_factory = main.app.state.mcp_manager_factory
        original_db_init = main.app.state.db_init
        main.app.state.mcp_manager_factory = _DegradedMCPManager
        main.app.state.db_init = lambda: _FakeConn()
        try:
            with TestClient(main.app) as client:
                response = client.get("/health")
        finally:
            main.app.state.mcp_manager_factory = original_factory
            main.app.state.db_init = original_db_init

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "degraded")
        self.assertTrue(body["alive"])
        self.assertTrue(body["ready"])
        self.assertTrue(body["degraded"])
        self.assertEqual(body["mcp"]["connected_servers"], 1)

    def test_health_reports_starting_when_runtime_not_ready(self):
        original_factory = main.app.state.mcp_manager_factory
        original_db_init = main.app.state.db_init
        main.app.state.mcp_manager_factory = _StartingMCPManager
        main.app.state.db_init = lambda: _FakeConn()
        try:
            with TestClient(main.app) as client:
                response = client.get("/health")
        finally:
            main.app.state.mcp_manager_factory = original_factory
            main.app.state.db_init = original_db_init

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "starting")
        self.assertTrue(body["alive"])
        self.assertFalse(body["ready"])
        self.assertTrue(body["degraded"])
        self.assertEqual(body["mcp"]["connected_servers"], 0)

    def test_inbox_delete_returns_conflict_when_reader_document_references_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "history.db"
            app = main.create_app(
                mcp_manager_factory=_FakeMCPManager,
                db_init=lambda: _init_inbox_delete_db(db_path),
                db_path=db_path,
            )

            with TestClient(app) as client:
                with sqlite3.connect(str(db_path)) as conn:
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute("INSERT INTO saved_items (id) VALUES (1)")
                    conn.execute(
                        """
                        INSERT INTO reader_documents (title, source_type, saved_item_id)
                        VALUES ('Paper', 'inbox_item', 1)
                        """
                    )
                    conn.commit()

                response = client.delete("/api/inbox/1")

            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json(),
                {"error": "item is referenced by a reader document; delete the reader document first"},
            )


if __name__ == "__main__":
    unittest.main()
