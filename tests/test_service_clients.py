import unittest
from unittest.mock import patch

import httpx

from service_clients import LLMChainClient


class _FakeAsyncClient:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeResponse:
    def __init__(self, content: str):
        self._content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


class ServiceClientsTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_chain_records_failover_success(self):
        client = LLMChainClient(
            [
                {"url": "http://primary", "model": "model-a"},
                {"url": "http://fallback", "model": "model-a"},
            ]
        )
        outcomes = [
            httpx.ConnectError("boom"),
            _FakeResponse("ok"),
        ]

        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient(outcomes)):
            result = await client.complete({"messages": []})

        self.assertEqual(result, "ok")
        health = client.get_health()
        self.assertEqual(health["total_requests"], 1)
        self.assertEqual(health["failover_requests"], 1)
        self.assertEqual(health["terminal_failures"], 0)
        self.assertEqual(health["last_success_url"], "http://fallback")
        self.assertTrue(health["last_request_used_fallback"])
        self.assertEqual(health["last_request_failed_urls"], ["http://primary"])
        self.assertEqual(health["endpoints"][0]["failures"], 1)
        self.assertEqual(health["endpoints"][1]["successes"], 1)

    async def test_llm_chain_records_terminal_failure(self):
        client = LLMChainClient(
            [
                {"url": "http://primary", "model": "model-a"},
                {"url": "http://fallback", "model": "model-a"},
            ]
        )
        outcomes = [
            httpx.ConnectError("first"),
            httpx.ConnectError("second"),
        ]

        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient(outcomes)):
            result = await client.complete({"messages": []})

        self.assertIsNone(result)
        health = client.get_health()
        self.assertEqual(health["total_requests"], 1)
        self.assertEqual(health["failover_requests"], 0)
        self.assertEqual(health["terminal_failures"], 1)
        self.assertTrue(health["last_request_used_fallback"])
        self.assertEqual(
            health["last_request_failed_urls"],
            ["http://primary", "http://fallback"],
        )
        self.assertEqual(health["endpoints"][0]["failures"], 1)
        self.assertEqual(health["endpoints"][1]["failures"], 1)


if __name__ == "__main__":
    unittest.main()
