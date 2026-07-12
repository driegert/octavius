import json
import unittest

import vault_files

try:
    from test_vault_files import VaultTestCase  # unittest discover -s tests
except ImportError:
    from tests.test_vault_files import VaultTestCase

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes.vault import router as vault_router
except ModuleNotFoundError:
    TestClient = None


class _FakeMCP:
    def __init__(self, payload: str):
        self.payload = payload
        self.calls = []

    async def call_tool(self, name, arguments, max_chars=None):
        self.calls.append((name, arguments, max_chars))
        return self.payload


@unittest.skipUnless(TestClient, "fastapi not installed")
class VaultRoutesTests(VaultTestCase):
    def setUp(self):
        super().setUp()
        self.app = FastAPI()
        self.app.include_router(vault_router)
        self.client = TestClient(self.app)

    def test_create_note(self):
        resp = self.client.post(
            "/api/vault/note", json={"title": "Route Note", "content": "body"}
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["path"].startswith("00-zettelkasten/001-Fleeting/"))
        self.assertTrue((self.vault / body["path"]).is_file())

    def test_create_requires_title(self):
        resp = self.client.post("/api/vault/note", json={"content": "body"})
        self.assertEqual(resp.status_code, 400)

    def test_read_note_and_404(self):
        res = vault_files.create_note("Fetch Me", "body")
        resp = self.client.get("/api/vault/note", params={"path": res["path"]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["base_hash"], res["base_hash"])
        resp = self.client.get(
            "/api/vault/note", params={"path": "00-zettelkasten/001-Fleeting/nope.md"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_journaling_read_forbidden(self):
        self.write("03-personal/Journaling/secret.md", "private")
        resp = self.client.get(
            "/api/vault/note", params={"path": "03-personal/Journaling/secret.md"}
        )
        self.assertEqual(resp.status_code, 403)

    def test_update_conflict_maps_to_409_with_current_hash(self):
        res = vault_files.create_note("Route Conflict", "original")
        resp = self.client.put(
            "/api/vault/note",
            json={"path": res["path"], "content": "new", "base_hash": "deadbeef"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["current_base_hash"], res["base_hash"])

    def test_update_success(self):
        res = vault_files.create_note("Route Edit", "original")
        resp = self.client.put(
            "/api/vault/note",
            json={"path": res["path"], "content": "new", "base_hash": res["base_hash"]},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual((self.vault / res["path"]).read_text(), "new")

    def test_update_requires_all_fields(self):
        resp = self.client.put("/api/vault/note", json={"path": "x.md"})
        self.assertEqual(resp.status_code, 400)

    def test_recent_lists_fleeting(self):
        vault_files.create_note("Recent One", "body")
        resp = self.client.get("/api/vault/recent")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()), 1)

    def test_search_maps_shape_and_drops_journaling(self):
        payload = json.dumps(
            {
                "results": [
                    {
                        "path": "00-zettelkasten/001-Fleeting/hit.md",
                        "title": "Hit",
                        "folder": "00-zettelkasten/001-Fleeting",
                        "heading": None,
                        "snippet": "…",
                        "score": 0.9,
                    },
                    {"path": "03-personal/Journaling/private.md", "score": 0.8},
                ]
            }
        )
        self.app.state.mcp_manager = _FakeMCP(payload)
        resp = self.client.get("/api/vault/search", params={"q": "hit"})
        self.assertEqual(resp.status_code, 200)
        results = resp.json()
        self.assertEqual(
            [r["path"] for r in results], ["00-zettelkasten/001-Fleeting/hit.md"]
        )
        self.assertEqual(results[0]["title"], "Hit")
        # Untruncated proxy call against the search_vault MCP tool.
        name, arguments, max_chars = self.app.state.mcp_manager.calls[0]
        self.assertEqual(name, "search_vault")
        self.assertEqual(arguments["query"], "hit")
        self.assertIsNone(max_chars)

    def test_search_without_mcp_returns_empty(self):
        resp = self.client.get("/api/vault/search", params={"q": "anything"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_search_bad_payload_returns_empty(self):
        self.app.state.mcp_manager = _FakeMCP("Error: server 'vault-search' not connected")
        resp = self.client.get("/api/vault/search", params={"q": "hit"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


if __name__ == "__main__":
    unittest.main()
