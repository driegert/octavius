import time
import unittest
from unittest.mock import patch

import httpx
import numpy as np

from service_clients import EmbeddingClient, LLMChainClient, SummaryClient, TTSClient


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


class _FakeTTSResponse:
    def __init__(self, content: bytes = b"audio"):
        self.content = content

    def raise_for_status(self):
        return None


class _RecordingAsyncClient:
    """Like _FakeAsyncClient but also records the URL of each post."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append(url)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _make_tts_client() -> TTSClient:
    return TTSClient(
        primary={"url": "http://primary-tts", "model": "voxtral", "voice": "alice"},
        fallback={"url": "http://fallback-tts", "model": "kokoro", "voice": "bob"},
        response_format="wav",
    )


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

    async def test_summary_client_acomplete_uses_fallback(self):
        client = SummaryClient("http://primary", "http://fallback")
        outcomes = [
            httpx.ConnectError("boom"),
            _FakeResponse("summary"),
        ]

        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient(outcomes)):
            result = await client.acomplete({"messages": []}, timeout=5)

        self.assertEqual(result, "summary")

    async def test_embedding_client_aembed_text_returns_bytes(self):
        client = EmbeddingClient("http://embed", "bge")
        response = _FakeResponse("ignored")
        response.json = lambda: {"embedding": [1.0, 2.0]}
        expected = np.array([1.0, 2.0], dtype=np.float32).tobytes()

        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient([response])):
            result = await client.aembed_text("hello", timeout=5)

        self.assertEqual(result, expected)


class TTSCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_primary_success_keeps_breaker_closed(self):
        client = _make_tts_client()
        fake = _RecordingAsyncClient([_FakeTTSResponse(b"primary-audio")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("hello")

        self.assertEqual(result, b"primary-audio")
        self.assertEqual(fake.calls, ["http://primary-tts"])
        self.assertEqual(client._primary_consecutive_failures, 0)
        self.assertFalse(client._primary_is_tripped())

    async def test_primary_failure_falls_back_but_breaker_stays_closed(self):
        client = _make_tts_client()
        fake = _RecordingAsyncClient([
            httpx.ConnectError("primary down"),
            _FakeTTSResponse(b"fallback-audio"),
        ])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("hello")

        self.assertEqual(result, b"fallback-audio")
        self.assertEqual(fake.calls, ["http://primary-tts", "http://fallback-tts"])
        self.assertEqual(client._primary_consecutive_failures, 1)
        self.assertFalse(client._primary_is_tripped())

    async def test_breaker_trips_after_threshold_failures(self):
        client = _make_tts_client()
        # 3 failure/fallback pairs — on the 3rd failure the breaker should trip.
        outcomes = []
        for _ in range(TTSClient.PRIMARY_FAILURE_THRESHOLD):
            outcomes.extend([httpx.ConnectError("nope"), _FakeTTSResponse(b"fb")])

        with patch("service_clients.httpx.AsyncClient", return_value=_RecordingAsyncClient(outcomes)):
            for _ in range(TTSClient.PRIMARY_FAILURE_THRESHOLD):
                await client.synthesize("hi")

        self.assertTrue(client._primary_is_tripped())
        self.assertEqual(client._primary_consecutive_failures, TTSClient.PRIMARY_FAILURE_THRESHOLD)

    async def test_tripped_breaker_skips_primary_entirely(self):
        client = _make_tts_client()
        client._primary_consecutive_failures = TTSClient.PRIMARY_FAILURE_THRESHOLD
        client._primary_skip_until = time.monotonic() + 60.0
        fake = _RecordingAsyncClient([_FakeTTSResponse(b"fallback-only")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("hi")

        self.assertEqual(result, b"fallback-only")
        self.assertEqual(fake.calls, ["http://fallback-tts"])

    async def test_primary_success_resets_counter_after_partial_failures(self):
        client = _make_tts_client()
        # Two failures, then a success — counter should reset, breaker stay closed.
        with patch(
            "service_clients.httpx.AsyncClient",
            return_value=_RecordingAsyncClient([
                httpx.ConnectError("1"),
                _FakeTTSResponse(b"fb"),
                httpx.ConnectError("2"),
                _FakeTTSResponse(b"fb"),
                _FakeTTSResponse(b"primary-recovered"),
            ]),
        ):
            await client.synthesize("a")
            await client.synthesize("b")
            result = await client.synthesize("c")

        self.assertEqual(result, b"primary-recovered")
        self.assertEqual(client._primary_consecutive_failures, 0)
        self.assertFalse(client._primary_is_tripped())

    async def test_half_open_probe_after_cooldown(self):
        client = _make_tts_client()
        # Simulate: breaker tripped, cooldown already elapsed.
        client._primary_consecutive_failures = TTSClient.PRIMARY_FAILURE_THRESHOLD
        client._primary_skip_until = time.monotonic() - 1.0
        fake = _RecordingAsyncClient([_FakeTTSResponse(b"primary-back")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("probe")

        # Should have attempted the primary again and recovered.
        self.assertEqual(result, b"primary-back")
        self.assertEqual(fake.calls, ["http://primary-tts"])
        self.assertEqual(client._primary_consecutive_failures, 0)
        self.assertFalse(client._primary_is_tripped())

    async def test_half_open_failure_re_trips_breaker(self):
        client = _make_tts_client()
        client._primary_consecutive_failures = TTSClient.PRIMARY_FAILURE_THRESHOLD
        client._primary_skip_until = time.monotonic() - 1.0  # cooldown elapsed
        fake = _RecordingAsyncClient([
            httpx.ConnectError("still dead"),
            _FakeTTSResponse(b"fb"),
        ])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("probe")

        self.assertEqual(result, b"fb")
        self.assertEqual(fake.calls, ["http://primary-tts", "http://fallback-tts"])
        self.assertTrue(client._primary_is_tripped())
        self.assertEqual(
            client._primary_consecutive_failures,
            TTSClient.PRIMARY_FAILURE_THRESHOLD + 1,
        )

    async def test_primary_honors_per_call_voice(self):
        client = _make_tts_client()

        captured: dict = {}

        class _Capturing(_RecordingAsyncClient):
            async def post(self, url, json=None):
                captured["url"] = url
                captured["json"] = json
                return _FakeTTSResponse(b"ok")

        with patch("service_clients.httpx.AsyncClient", return_value=_Capturing([])):
            await client.synthesize("hi", voice="charlie")

        self.assertEqual(captured["url"], "http://primary-tts")
        self.assertEqual(captured["json"]["voice"], "charlie")


if __name__ == "__main__":
    unittest.main()
