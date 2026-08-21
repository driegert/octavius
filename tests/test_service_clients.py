import asyncio
import os
import time
import unittest
from unittest.mock import patch

import httpx
import numpy as np
import requests

import service_clients
from service_clients import (
    EmbeddingClient,
    LLMChainClient,
    SummaryClient,
    TTSClient,
    auth_headers,
    classify_chain_error,
)
from settings import _llm_api_keys


class _FakeAsyncClient:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _http_error(status: int, url: str = "http://x") -> httpx.HTTPStatusError:
    """An HTTPStatusError shaped like the ones httpx raises from raise_for_status."""
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request, text="denied")
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


class _AsyncNoop:
    async def __call__(self, *args, **kwargs):
        return None


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
    """Like _FakeAsyncClient but also records the URL and headers of each post."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls: list[str] = []
        self.headers: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append(url)
        self.headers.append(headers or {})
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _make_tts_client(kokoro_voices: list[str] | None = None) -> TTSClient:
    return TTSClient(
        primary={"url": "http://primary-tts", "model": "voxtral", "voice": "alice"},
        fallback={"url": "http://fallback-tts", "model": "kokoro", "voice": "bob"},
        response_format="wav",
        kokoro_voices=kokoro_voices,
    )


class ChainErrorClassificationTests(unittest.IsolatedAsyncioTestCase):
    """A stale bearer token must be distinguishable from an endpoint outage.

    Both arrive as exceptions from the same except clause and both burn a
    failover hop, so without classification /health reports them identically.
    """

    def test_classify_separates_auth_from_other_failures(self):
        cases = {
            401: ("auth", 401),
            403: ("auth", 403),
            400: ("client_error", 400),
            404: ("client_error", 404),
            500: ("server_error", 500),
            502: ("server_error", 502),
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                self.assertEqual(classify_chain_error(_http_error(status)), expected)

        self.assertEqual(classify_chain_error(httpx.ConnectError("down")), ("connect", None))
        self.assertEqual(classify_chain_error(KeyError("choices")), ("bad_response", None))

    def test_connect_timeout_is_not_confused_with_a_slow_generation(self):
        """Observed 2026-08-08: lilbuddy:8010 swallowed SYNs (host down) while
        triplestuffed:8010 accepted the connection and then never generated.
        Both raise httpx.TimeoutException; they need different responses, so
        ConnectTimeout must be tested before the generic case."""
        self.assertEqual(
            classify_chain_error(httpx.ConnectTimeout("no handshake")),
            ("connect_timeout", None),
        )
        self.assertEqual(
            classify_chain_error(httpx.ReadTimeout("accepted, never answered")),
            ("timeout", None),
        )
        self.assertTrue(issubclass(httpx.ConnectTimeout, httpx.TimeoutException))

    async def test_health_names_the_endpoint_rejecting_credentials(self):
        client = LLMChainClient(
            [
                {"url": KEYED, "model": "model-a"},
                {"url": OPEN, "model": "model-a"},
            ]
        )
        # Keyed endpoint 401s (stale token), open endpoint answers.
        outcomes = [_http_error(401, KEYED), _FakeResponse("ok")]
        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient(outcomes)):
            result = await client.complete({"messages": []})

        self.assertEqual(result, "ok")
        health = client.get_health()
        self.assertEqual(health["endpoints_rejecting_credentials"], [KEYED])
        self.assertEqual(health["auth_failures"], 1)
        self.assertEqual(health["endpoints"][0]["last_error_kind"], "auth")
        self.assertEqual(health["endpoints"][0]["last_error_status"], 401)
        self.assertEqual(health["endpoints"][0]["auth_failures"], 1)

    async def test_outage_is_not_reported_as_an_auth_problem(self):
        """The whole point: a down endpoint must NOT show up as a key problem."""
        client = LLMChainClient(
            [
                {"url": KEYED, "model": "model-a"},
                {"url": OPEN, "model": "model-a"},
            ]
        )
        outcomes = [httpx.ConnectError("host down"), _FakeResponse("ok")]
        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient(outcomes)):
            await client.complete({"messages": []})

        health = client.get_health()
        self.assertEqual(health["endpoints_rejecting_credentials"], [])
        self.assertEqual(health["auth_failures"], 0)
        self.assertEqual(health["endpoints"][0]["last_error_kind"], "connect")
        self.assertIsNone(health["endpoints"][0]["last_error_status"])

    async def test_dead_model_alias_reads_as_client_error_not_outage(self):
        """A model id absent from a router's catalog hard-400s. It should be
        distinguishable from both a key problem and the host being down."""
        client = LLMChainClient([{"url": OPEN, "model": "qwen3.6-35b-a3b"}])
        with patch(
            "service_clients.httpx.AsyncClient",
            return_value=_FakeAsyncClient([_http_error(400, OPEN)]),
        ):
            self.assertIsNone(await client.complete({"messages": []}))

        health = client.get_health()
        self.assertEqual(health["endpoints"][0]["last_error_kind"], "client_error")
        self.assertEqual(health["endpoints"][0]["last_error_status"], 400)
        self.assertEqual(health["endpoints_rejecting_credentials"], [])
        self.assertEqual(health["last_failure_kind"], "client_error")

    async def test_success_clears_stale_error_state(self):
        """last_error_* describes current belief, so a recovered endpoint must
        stop being listed as rejecting credentials."""
        client = LLMChainClient([{"url": KEYED, "model": "model-a"}])
        outcomes = [_http_error(401, KEYED), _FakeResponse("ok")]
        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient(outcomes)):
            await client.complete({"messages": []})   # 401, terminal
            await client.complete({"messages": []})   # key fixed, succeeds

        health = client.get_health()
        self.assertEqual(health["endpoints_rejecting_credentials"], [])
        self.assertIsNone(health["endpoints"][0]["last_error_kind"])
        # Lifetime counter survives; current-state field does not.
        self.assertEqual(health["auth_failures"], 1)
        self.assertEqual(health["endpoints"][0]["auth_failures"], 1)

    async def test_offchain_target_auth_failure_is_still_named(self):
        """The reader reaches lilripper:8010 via llm_client.complete(urls=[...]),
        an endpoint absent from llm_chain. Its 401 must name the endpoint, not
        just bump the chain-level counter."""
        client = LLMChainClient([{"url": OPEN, "model": "model-a"}])
        with patch(
            "service_clients.httpx.AsyncClient",
            return_value=_FakeAsyncClient([_http_error(401, KEYED)]),
        ):
            self.assertIsNone(await client.complete({"messages": []}, urls=[KEYED]))

        health = client.get_health()
        self.assertEqual(health["endpoints_rejecting_credentials"], [KEYED])
        offchain = [e for e in health["endpoints"] if e["off_chain"]]
        self.assertEqual([e["url"] for e in offchain], [KEYED])
        self.assertEqual(offchain[0]["last_error_status"], 401)
        # The configured endpoint was never attempted and stays clean.
        self.assertEqual(health["endpoints"][0]["url"], OPEN)
        self.assertEqual(health["endpoints"][0]["attempts"], 0)

    async def test_health_reports_which_endpoints_send_a_key(self):
        client = LLMChainClient(
            [{"url": KEYED, "model": "m"}, {"url": OPEN, "model": "m"}]
        )
        with patch.dict(
            service_clients.settings.llm_api_keys,
            {"http://lilripper:8010": "sk-abc"},
            clear=True,
        ):
            health = client.get_health()
        self.assertTrue(health["endpoints"][0]["authenticated"])
        self.assertFalse(health["endpoints"][1]["authenticated"])


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

    async def test_chain_connect_timeout_is_short_but_read_stays_long(self):
        """A host that swallows SYNs must not hold the chain for the read budget.

        Generation legitimately runs for minutes, so `read` stays at 120 s; but an
        unreachable host used to burn that same 120 s on connect before failover.
        """
        client = LLMChainClient([{"url": "http://primary", "model": "model-a"}])
        captured = {}

        def _capture(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            return _FakeAsyncClient([_FakeResponse("ok")])

        with patch("service_clients.httpx.AsyncClient", side_effect=_capture):
            await client.complete({"messages": []})

        timeout = captured["timeout"]
        self.assertEqual(timeout.connect, 5.0)
        self.assertEqual(timeout.read, 120.0)

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
        client = EmbeddingClient([
            {"url": "http://embed/api/embeddings", "model": "bge", "schema": "ollama"},
        ])
        response = _FakeResponse("ignored")
        response.json = lambda: {"embedding": [1.0, 2.0]}
        expected = np.array([1.0, 2.0], dtype=np.float32).tobytes()

        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient([response])):
            result = await client.aembed_text("hello", timeout=5)

        self.assertEqual(result, expected)

    async def test_embedding_client_retries_transient_500_on_same_endpoint(self):
        client = EmbeddingClient([
            {"url": "http://embed/api/embeddings", "model": "bge", "schema": "ollama"},
        ])
        success = _FakeResponse("ignored")
        success.json = lambda: {"embedding": [3.0, 4.0]}
        expected = np.array([3.0, 4.0], dtype=np.float32).tobytes()

        transient = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "http://embed/api/embeddings"),
            response=httpx.Response(500),
        )
        fake = _FakeAsyncClient([transient, success])

        with patch("service_clients.httpx.AsyncClient", return_value=fake), \
             patch("service_clients.asyncio.sleep", new=_AsyncNoop()):
            result = await client.aembed_text("hello", timeout=5)

        self.assertEqual(result, expected)

    async def test_embedding_client_falls_over_to_next_endpoint(self):
        client = EmbeddingClient([
            {"url": "http://primary/v1/embeddings", "model": "bge", "schema": "openai"},
            {"url": "http://fallback/api/embeddings", "model": "bge", "schema": "ollama"},
        ])
        # Primary fails both attempts with 500; fallback returns ollama-schema success.
        transient = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "http://primary/v1/embeddings"),
            response=httpx.Response(500),
        )
        success = _FakeResponse("ignored")
        success.json = lambda: {"embedding": [9.0]}
        expected = np.array([9.0], dtype=np.float32).tobytes()

        fake = _RecordingAsyncClient([transient, transient, success])
        with patch("service_clients.httpx.AsyncClient", return_value=fake), \
             patch("service_clients.asyncio.sleep", new=_AsyncNoop()):
            result = await client.aembed_text("hello", timeout=5)

        self.assertEqual(result, expected)
        self.assertEqual(fake.calls, [
            "http://primary/v1/embeddings",
            "http://primary/v1/embeddings",
            "http://fallback/api/embeddings",
        ])

    async def test_embedding_client_uses_openai_payload_and_response_shape(self):
        client = EmbeddingClient([
            {"url": "http://llama/v1/embeddings", "model": "bge-m3", "schema": "openai"},
        ])
        captured: dict = {}

        class _Capturing(_RecordingAsyncClient):
            async def post(self, url, json=None):
                captured["url"] = url
                captured["json"] = json
                resp = _FakeResponse("ignored")
                resp.json = lambda: {"data": [{"embedding": [7.0, 8.0]}]}
                return resp

        with patch("service_clients.httpx.AsyncClient", return_value=_Capturing([])):
            result = await client.aembed_text("hello", timeout=5)

        self.assertEqual(result, np.array([7.0, 8.0], dtype=np.float32).tobytes())
        self.assertEqual(captured["url"], "http://llama/v1/embeddings")
        self.assertEqual(captured["json"], {"model": "bge-m3", "input": "hello"})

    async def test_embedding_client_uses_ollama_payload(self):
        client = EmbeddingClient([
            {"url": "http://ollama/api/embeddings", "model": "bge-m3", "schema": "ollama"},
        ])
        captured: dict = {}

        class _Capturing(_RecordingAsyncClient):
            async def post(self, url, json=None):
                captured["json"] = json
                resp = _FakeResponse("ignored")
                resp.json = lambda: {"embedding": [1.0]}
                return resp

        with patch("service_clients.httpx.AsyncClient", return_value=_Capturing([])):
            await client.aembed_text("hello", timeout=5)

        self.assertEqual(captured["json"], {"model": "bge-m3", "prompt": "hello"})

    async def test_embedding_client_gives_up_after_all_endpoints(self):
        client = EmbeddingClient([
            {"url": "http://a/v1/embeddings", "model": "bge", "schema": "openai"},
            {"url": "http://b/api/embeddings", "model": "bge", "schema": "ollama"},
        ])
        transient = httpx.HTTPStatusError(
            "500",
            request=httpx.Request("POST", "http://x"),
            response=httpx.Response(500),
        )
        # 2 attempts × 2 endpoints = 4 transient failures.
        fake = _FakeAsyncClient([transient] * 4)

        with patch("service_clients.httpx.AsyncClient", return_value=fake), \
             patch("service_clients.asyncio.sleep", new=_AsyncNoop()):
            result = await client.aembed_text("hello", timeout=5)

        self.assertIsNone(result)

    async def test_embedding_client_does_not_retry_programmer_errors(self):
        client = EmbeddingClient([
            {"url": "http://embed/api/embeddings", "model": "bge", "schema": "ollama"},
        ])
        broken = _FakeResponse("ignored")
        broken.json = lambda: {"no_embedding_key": True}
        # Only one outcome provided — if the client retried we'd get IndexError.
        with patch("service_clients.httpx.AsyncClient", return_value=_FakeAsyncClient([broken])):
            result = await client.aembed_text("hello", timeout=5)
        self.assertIsNone(result)

    def test_embedding_client_rejects_unknown_schema(self):
        with self.assertRaises(ValueError):
            EmbeddingClient([{"url": "http://x", "model": "y", "schema": "bogus"}])

    def test_embedding_client_rejects_empty_chain(self):
        with self.assertRaises(ValueError):
            EmbeddingClient([])


PRIMARY = "http://primary/v1/embeddings"
FALLBACK = "http://fallback/api/embeddings"


def _embed_chain():
    return [
        {"url": PRIMARY, "model": "bge", "schema": "openai"},
        {"url": FALLBACK, "model": "bge", "schema": "ollama"},
    ]


def _embed_ok(value=1.0, schema="ollama"):
    """A success response in the shape the answering endpoint's schema expects."""
    resp = _FakeResponse("ignored")
    if schema == "ollama":
        resp.json = lambda: {"embedding": [value]}
    else:
        resp.json = lambda: {"data": [{"embedding": [value]}]}
    return resp


class EmbeddingConnectTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """A connect timeout means no handshake — retrying the same dead socket is
    pure latency, and it is what made a dead primary cost two connect budgets."""

    async def test_async_connect_timeout_does_not_retry_same_endpoint(self):
        client = EmbeddingClient(_embed_chain())
        dead = httpx.ConnectTimeout("no route")
        fake = _RecordingAsyncClient([dead, _embed_ok(9.0)])

        with patch("service_clients.httpx.AsyncClient", return_value=fake), \
             patch("service_clients.asyncio.sleep", new=_AsyncNoop()):
            result = await client.aembed_text("hello", timeout=5)

        self.assertEqual(result, np.array([9.0], dtype=np.float32).tobytes())
        self.assertEqual(fake.calls, [PRIMARY, FALLBACK])

    def test_sync_connect_timeout_does_not_retry_same_endpoint(self):
        client = EmbeddingClient(_embed_chain())
        calls = []

        def fake_post(url, json=None, timeout=None):
            calls.append(url)
            if url == PRIMARY:
                raise requests.exceptions.ConnectTimeout("no route")
            return _embed_ok(4.0)

        with patch("service_clients.requests.post", side_effect=fake_post):
            result = client.embed_text("hello", timeout=5)

        self.assertEqual(result, np.array([4.0], dtype=np.float32).tobytes())
        self.assertEqual(calls, [PRIMARY, FALLBACK])

    def test_read_timeout_still_retries(self):
        """The fix must not disable retry for genuine read timeouts."""
        self.assertTrue(EmbeddingClient._is_retry_worthy(httpx.ReadTimeout("slow")))
        self.assertFalse(EmbeddingClient._is_retry_worthy(httpx.ConnectTimeout("dead")))

    def test_sync_post_gets_split_connect_and_read_budget(self):
        client = EmbeddingClient([{"url": PRIMARY, "model": "bge", "schema": "openai"}])
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["timeout"] = timeout
            resp = _FakeResponse("ignored")
            resp.json = lambda: {"data": [{"embedding": [1.0]}]}
            return resp

        with patch("service_clients.requests.post", side_effect=fake_post):
            client.embed_text("hello", timeout=5)

        self.assertEqual(captured["timeout"], (1.5, 5))


class EmbeddingCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    """Per-endpoint failure memory: without it, a dead primary re-pays its full
    connect budget on every single embed."""

    async def _fail_primary_until_tripped(self, client):
        """Drive the primary to its threshold; the fallback answers each time."""
        for _ in range(EmbeddingClient.ENDPOINT_FAILURE_THRESHOLD):
            fake = _RecordingAsyncClient([httpx.ConnectTimeout("dead"), _embed_ok()])
            with patch("service_clients.httpx.AsyncClient", return_value=fake), \
                 patch("service_clients.asyncio.sleep", new=_AsyncNoop()):
                await client.aembed_text("x", timeout=5)

    async def test_tripped_endpoint_is_skipped_entirely(self):
        client = EmbeddingClient(_embed_chain())
        await self._fail_primary_until_tripped(client)

        fake = _RecordingAsyncClient([_embed_ok(7.0)])
        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.aembed_text("x", timeout=5)

        self.assertEqual(result, np.array([7.0], dtype=np.float32).tobytes())
        self.assertEqual(fake.calls, [FALLBACK])

    async def test_success_resets_only_that_endpoints_counter(self):
        client = EmbeddingClient(_embed_chain())
        fake = _RecordingAsyncClient([httpx.ConnectTimeout("dead"), _embed_ok()])
        with patch("service_clients.httpx.AsyncClient", return_value=fake), \
             patch("service_clients.asyncio.sleep", new=_AsyncNoop()):
            await client.aembed_text("x", timeout=5)

        self.assertEqual(client._consecutive_failures.get(PRIMARY), 1)
        self.assertEqual(client._consecutive_failures.get(FALLBACK, 0), 0)

        # The primary now succeeds; its counter clears and it never trips.
        fake = _RecordingAsyncClient([_embed_ok(schema="openai")])
        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            await client.aembed_text("x", timeout=5)
        self.assertEqual(client._consecutive_failures.get(PRIMARY, 0), 0)
        self.assertNotIn(PRIMARY, client._skip_until)

    async def test_all_tripped_returns_none_with_zero_http_calls(self):
        client = EmbeddingClient(_embed_chain())
        now = time.monotonic()
        client._skip_until = {PRIMARY: now + 300, FALLBACK: now + 300}

        fake = _RecordingAsyncClient([])  # any call would IndexError
        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.aembed_text("x", timeout=5)

        self.assertIsNone(result)
        self.assertEqual(fake.calls, [])

    async def test_half_open_probe_closes_breaker_on_success(self):
        client = EmbeddingClient(_embed_chain())
        client._skip_until = {PRIMARY: time.monotonic() - 1}  # cooldown elapsed
        client._consecutive_failures = {PRIMARY: 2}

        fake = _RecordingAsyncClient([_embed_ok(5.0, schema="openai")])
        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.aembed_text("x", timeout=5)

        self.assertEqual(result, np.array([5.0], dtype=np.float32).tobytes())
        self.assertEqual(fake.calls, [PRIMARY])
        self.assertNotIn(PRIMARY, client._skip_until)
        self.assertNotIn(PRIMARY, client._probe_in_flight)

    async def test_only_one_caller_claims_the_half_open_probe(self):
        """Concurrent callers must not stampede a host that is still down."""
        client = EmbeddingClient(_embed_chain())
        client._skip_until = {PRIMARY: time.monotonic() - 1}

        first_allowed, first_probing = client._claim_endpoint(PRIMARY)
        second_allowed, second_probing = client._claim_endpoint(PRIMARY)

        self.assertEqual((first_allowed, first_probing), (True, True))
        self.assertEqual((second_allowed, second_probing), (False, False))

        client._release_probe(PRIMARY)
        third_allowed, third_probing = client._claim_endpoint(PRIMARY)
        self.assertEqual((third_allowed, third_probing), (True, True))

    def test_breaker_state_is_shared_between_sync_and_async(self):
        """One singleton serves both paths; a trip on one must be seen by the other."""
        client = EmbeddingClient(_embed_chain())
        client._skip_until = {PRIMARY: time.monotonic() + 300}
        calls = []

        with patch("service_clients.requests.post", side_effect=lambda url, **kw: (
            calls.append(url), _embed_ok(2.0))[1]
        ):
            result = client.embed_text("x", timeout=5)

        self.assertEqual(result, np.array([2.0], dtype=np.float32).tobytes())
        self.assertEqual(calls, [FALLBACK])

    async def test_failed_half_open_probe_retrips_before_the_probe_is_released(self):
        """The failure must be recorded before the probe slot frees, or another
        caller can slip in and probe an endpoint that just failed again."""
        client = EmbeddingClient(_embed_chain())
        client._skip_until = {PRIMARY: time.monotonic() - 1}
        client._consecutive_failures = {PRIMARY: 2}

        fake = _RecordingAsyncClient([httpx.ConnectTimeout("still dead"), _embed_ok()])
        with patch("service_clients.httpx.AsyncClient", return_value=fake), \
             patch("service_clients.asyncio.sleep", new=_AsyncNoop()):
            await client.aembed_text("x", timeout=5)

        # Re-armed for another full cooldown, and the probe slot is free again.
        self.assertGreater(client._skip_until[PRIMARY], time.monotonic())
        self.assertEqual(client._probe_in_flight, set())
        # A follow-up caller is refused rather than allowed to probe again.
        self.assertEqual(client._claim_endpoint(PRIMARY), (False, False))

    async def test_cancellation_releases_the_probe_without_recording_a_failure(self):
        """A turn cancelled mid-embed is no evidence the endpoint is unhealthy,
        and it must not leave the endpoint stuck as 'probe in flight' forever."""
        client = EmbeddingClient(_embed_chain())
        client._skip_until = {PRIMARY: time.monotonic() - 1}  # half-open

        class _Hanging:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None):
                await asyncio.sleep(10)

        with patch("service_clients.httpx.AsyncClient", return_value=_Hanging()):
            task = asyncio.create_task(client.aembed_text("x", timeout=5))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(client._probe_in_flight, set())
        self.assertEqual(client._consecutive_failures, {})

    async def test_get_health_reports_tripped_endpoints(self):
        client = EmbeddingClient(_embed_chain())
        await self._fail_primary_until_tripped(client)

        health = client.get_health()
        by_url = {e["url"]: e for e in health["endpoints"]}
        self.assertEqual(health["configured_endpoints"], 2)
        self.assertFalse(health["all_tripped"])
        self.assertTrue(by_url[PRIMARY]["tripped"])
        self.assertGreater(by_url[PRIMARY]["cooldown_remaining"], 0)
        self.assertFalse(by_url[FALLBACK]["tripped"])


KEYED = "http://lilripper:8010/v1/chat/completions"
OPEN = "http://lilripper:8020/v1/chat/completions"


class LLMAuthHeaderTests(unittest.IsolatedAsyncioTestCase):
    """Endpoints behind auth get a Bearer header; open endpoints are untouched."""

    def setUp(self):
        patcher = patch.dict(
            service_clients.settings.llm_api_keys,
            {"http://lilripper:8010": "sk-abc"},
            clear=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # Clear the real auth vars for every test in this class. `_llm_api_keys`
        # reads os.environ directly, so a developer who has exported either var
        # in their shell would otherwise see ambient credentials leak in — and
        # because OCTAVIUS_LR_API_KEY deliberately *wins* over the JSON map,
        # the leak looks like a logic failure rather than a dirty environment.
        env_patcher = patch.dict(os.environ, {}, clear=False)
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        os.environ.pop("OCTAVIUS_LLM_API_KEYS", None)
        os.environ.pop("OCTAVIUS_LR_API_KEY", None)

    def test_env_keys_are_normalized_to_origin(self):
        env = {"OCTAVIUS_LLM_API_KEYS": f'{{"{KEYED}": "sk-abc"}}'}
        with patch.dict(os.environ, env):
            self.assertEqual(_llm_api_keys(), {"http://lilripper:8010": "sk-abc"})

    def test_no_keys_configured_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OCTAVIUS_LLM_API_KEYS", None)
            os.environ.pop("OCTAVIUS_LR_API_KEY", None)
            self.assertEqual(_llm_api_keys(), {})

    def test_dedicated_8010_var_supplies_the_key(self):
        with patch.dict(os.environ, {"OCTAVIUS_LR_API_KEY": "sk-direct"}):
            os.environ.pop("OCTAVIUS_LLM_API_KEYS", None)
            self.assertEqual(_llm_api_keys(), {"http://lilripper:8010": "sk-direct"})

    def test_dedicated_8010_var_wins_over_json_map(self):
        """The dedicated var is the one that rotates, so it takes precedence."""
        env = {
            "OCTAVIUS_LLM_API_KEYS": f'{{"{KEYED}": "sk-stale"}}',
            "OCTAVIUS_LR_API_KEY": "sk-fresh",
        }
        with patch.dict(os.environ, env):
            self.assertEqual(_llm_api_keys(), {"http://lilripper:8010": "sk-fresh"})

    def test_dedicated_8010_var_does_not_leak_to_other_8010_hosts(self):
        """lilbuddy:8010 and triplestuffed:8010 are open; they must stay unkeyed
        even though they share the port the env var is named for."""
        with patch.dict(os.environ, {"OCTAVIUS_LR_API_KEY": "sk-direct"}):
            os.environ.pop("OCTAVIUS_LLM_API_KEYS", None)
            keys = _llm_api_keys()
        self.assertNotIn("http://lilbuddy:8010", keys)
        self.assertNotIn("http://triplestuffed:8010", keys)

        with patch.dict(service_clients.settings.llm_api_keys, keys, clear=True):
            self.assertEqual(
                auth_headers("http://lilripper:8010/v1/chat/completions"),
                {"Authorization": "Bearer sk-direct"},
            )
            self.assertEqual(auth_headers("http://lilbuddy:8010/v1/chat/completions"), {})
            self.assertEqual(
                auth_headers("http://triplestuffed:8010/v1/chat/completions"), {}
            )

    def test_blank_dedicated_8010_var_is_ignored(self):
        """An unset-but-declared EnvironmentFile line must not register an
        empty token (which would send `Bearer ` and 401)."""
        env = {
            "OCTAVIUS_LLM_API_KEYS": f'{{"{KEYED}": "sk-abc"}}',
            "OCTAVIUS_LR_API_KEY": "   ",
        }
        with patch.dict(os.environ, env):
            self.assertEqual(_llm_api_keys(), {"http://lilripper:8010": "sk-abc"})

    def test_auth_headers_only_for_keyed_origin(self):
        self.assertEqual(auth_headers(KEYED), {"Authorization": "Bearer sk-abc"})
        self.assertEqual(auth_headers(OPEN), {})

    async def test_complete_with_tools_authenticates_keyed_entry_only(self):
        client = LLMChainClient(
            [
                {"url": OPEN, "model": "qwen3.6-35b-a3b"},
                {"url": KEYED, "model": "qwen3.6-35b-a3b-general"},
            ]
        )
        fake = _RecordingAsyncClient([_FakeResponse("ok"), _FakeResponse("ok")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            await client.complete_with_tools({"messages": []}, urls=[KEYED])
            await client.complete_with_tools({"messages": []}, urls=[OPEN])

        self.assertEqual(fake.calls, [KEYED, OPEN])
        self.assertEqual(fake.headers[0], {"Authorization": "Bearer sk-abc"})
        self.assertEqual(fake.headers[1], {})

    async def test_complete_authenticates_url_outside_its_own_chain(self):
        # The reader reaches lilripper:8010 through llm_client, whose chain does
        # not list it, so the key must resolve from the URL and not the entry.
        client = LLMChainClient([{"url": OPEN, "model": "qwen3.6-35b-a3b"}])
        fake = _RecordingAsyncClient([_FakeResponse("ok")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.complete({"messages": []}, urls=[KEYED])

        self.assertEqual(result, "ok")
        self.assertEqual(fake.calls, [KEYED])
        self.assertEqual(fake.headers, [{"Authorization": "Bearer sk-abc"}])


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

    async def test_kokoro_voice_bypasses_primary(self):
        client = _make_tts_client(kokoro_voices=["af_heart"])
        fake = _RecordingAsyncClient([_FakeTTSResponse(b"kokoro-audio")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("hi", voice="af_heart")

        self.assertEqual(result, b"kokoro-audio")
        self.assertEqual(fake.calls, ["http://fallback-tts"])
        self.assertEqual(client._primary_consecutive_failures, 0)
        self.assertFalse(client._primary_is_tripped())

    async def test_kokoro_voice_sends_user_voice_not_fallback_default(self):
        client = _make_tts_client(kokoro_voices=["af_heart"])

        captured: dict = {}

        class _Capturing(_RecordingAsyncClient):
            async def post(self, url, json=None):
                captured["url"] = url
                captured["json"] = json
                return _FakeTTSResponse(b"ok")

        with patch("service_clients.httpx.AsyncClient", return_value=_Capturing([])):
            await client.synthesize("hi", voice="af_heart")

        self.assertEqual(captured["url"], "http://fallback-tts")
        self.assertEqual(captured["json"]["voice"], "af_heart")

    async def test_kokoro_voice_ignores_tripped_breaker(self):
        client = _make_tts_client(kokoro_voices=["af_heart"])
        client._primary_consecutive_failures = TTSClient.PRIMARY_FAILURE_THRESHOLD
        client._primary_skip_until = time.monotonic() + 60.0
        fake = _RecordingAsyncClient([_FakeTTSResponse(b"kokoro-audio")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("hi", voice="af_heart")

        self.assertEqual(result, b"kokoro-audio")
        self.assertEqual(fake.calls, ["http://fallback-tts"])
        # Breaker state untouched by Kokoro-voice calls.
        self.assertTrue(client._primary_is_tripped())

    async def test_non_kokoro_voice_still_uses_primary(self):
        client = _make_tts_client(kokoro_voices=["af_heart"])
        fake = _RecordingAsyncClient([_FakeTTSResponse(b"primary-audio")])

        with patch("service_clients.httpx.AsyncClient", return_value=fake):
            result = await client.synthesize("hi", voice="de_male")

        self.assertEqual(result, b"primary-audio")
        self.assertEqual(fake.calls, ["http://primary-tts"])


if __name__ == "__main__":
    unittest.main()
