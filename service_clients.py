import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import numpy as np
import requests

from settings import endpoint_origin, settings

log = logging.getLogger(__name__)

# Chain timeouts. `read` stays long because generation legitimately takes minutes,
# but `connect` must not: a flat 120 s meant a host that swallows SYNs (lilripper
# mid-reboot) burned two minutes per chain entry before failover, while a host that
# merely 502s failed over instantly. Connect is a LAN TCP handshake — 5 s is already
# ~40x the observed 0.12 s — so this only fires when the host is genuinely unreachable.
CHAIN_TIMEOUT = httpx.Timeout(120.0, connect=5.0)


def auth_headers(url: str) -> dict[str, str]:
    """Bearer header for an LLM endpoint behind auth, or {} for open endpoints."""
    key = settings.llm_api_keys.get(endpoint_origin(url))
    return {"Authorization": f"Bearer {key}"} if key else {}


# 401/403. Bucketed separately from other 4xx because a rejected credential is
# otherwise invisible: httpx raises HTTPStatusError for a 401 exactly as it does
# for a 500, so a stale bearer token burns a failover hop and lands in /health as
# a generic chain failure — indistinguishable from the endpoint being down.
AUTH_STATUSES = (401, 403)


def classify_chain_error(exc: Exception) -> tuple[str, int | None]:
    """Bucket a chain-attempt exception into (kind, http_status).

    Kinds are deliberately status-driven rather than body-sniffing, so they
    stay honest across llama.cpp / llama-swap / router versions:

      auth          401/403 — our key is missing, stale, or not accepted here
      client_error  other 4xx — most often a model alias absent from this
                    endpoint's catalog, which hard-400s (see CLAUDE.md
                    "Router model ids")
      server_error  5xx — endpoint reachable but failing
      connect       TCP refused/unreachable — endpoint is down
      connect_timeout  no TCP handshake within the connect budget — host is
                    swallowing SYNs (rebooting, firewalled, wedged NIC)
      timeout       handshake succeeded, but no response in budget — the
                    server is UP and accepting connections while failing to
                    generate. A "zombie" endpoint: /v1/models answers fine,
                    completions hang.
      bad_response  2xx whose JSON shape we could not parse

    The connect_timeout / timeout split matters operationally: they mean
    different hosts need different attention, and both otherwise present as a
    bare `httpx.TimeoutException` (ConnectTimeout subclasses it, so it must be
    tested first).
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
        status = exc.response.status_code
        if status in AUTH_STATUSES:
            return "auth", status
        if 400 <= status < 500:
            return "client_error", status
        return "server_error", status
    if isinstance(exc, httpx.ConnectError):
        return "connect", None
    if isinstance(exc, httpx.ConnectTimeout):
        return "connect_timeout", None
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", None
    return "bad_response", None


@dataclass
class EndpointStats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    auth_failures: int = 0
    # Last observed error for this endpoint, cleared on its next success, so
    # these describe *current* belief rather than lifetime history.
    last_error_kind: str | None = None
    last_error_status: int | None = None


@dataclass
class RequestOutcome:
    url: str | None
    model: str | None
    attempts: int
    failed_urls: list[str] = field(default_factory=list)
    error: str | None = None


class STTClient:
    def __init__(self, url: str):
        self.url = url

    async def transcribe(self, audio_bytes: bytes) -> str:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.url,
                content=audio_bytes,
                headers={"Content-Type": "audio/webm"},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip()

    async def transcribe_pcm(self, pcm_bytes: bytes) -> str:
        """Transcribe raw float32 PCM audio at 16kHz."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                self.url,
                content=pcm_bytes,
                headers={"Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
            return resp.json().get("text", "").strip()


class TTSClient:
    """
    Voice-routed TTS.

    When `primary_enabled` is False (the default), Voxtral is never attempted:
    every call goes to Kokoro. Kokoro voices speak in their own voice; voices
    that only exist on Voxtral (e.g. de_male) are remapped to the configured
    fallback voice. This is the practical setup because Voxtral requires too
    much VRAM to run reliably.

    When `primary_enabled` is True, the original voice-routed behavior applies:

    - Voices in `kokoro_voices` go straight to the fallback (Kokoro) endpoint,
      with no Voxtral attempt and no breaker interaction.
    - Any other voice is treated as a primary (Voxtral) voice and goes through
      the primary → fallback path with a circuit breaker on the primary.

    Breaker behavior (only relevant when `primary_enabled` is True): after
    PRIMARY_FAILURE_THRESHOLD consecutive failures the primary is "tripped":
    subsequent synth calls on primary voices skip it entirely and go to the
    fallback with its own voice for PRIMARY_COOLDOWN_SECONDS. When the cooldown
    elapses the next primary-voice call probes the primary again ("half-open");
    a success closes the breaker, a failure re-trips it.
    """

    PRIMARY_FAILURE_THRESHOLD = 3
    PRIMARY_COOLDOWN_SECONDS = 300.0

    def __init__(
        self,
        primary: dict,
        fallback: dict,
        response_format: str,
        kokoro_voices: list[str] | None = None,
        primary_enabled: bool = True,
    ):
        self.primary = primary
        self.fallback = fallback
        self.response_format = response_format
        self._kokoro_voices = set(kokoro_voices or [])
        self._primary_enabled = primary_enabled
        self._primary_consecutive_failures = 0
        self._primary_skip_until = 0.0  # monotonic; 0 means breaker closed

    def _primary_is_tripped(self) -> bool:
        return time.monotonic() < self._primary_skip_until

    def _record_primary_success(self) -> None:
        if self._primary_consecutive_failures or self._primary_skip_until:
            log.info("TTS primary recovered, closing breaker")
        self._primary_consecutive_failures = 0
        self._primary_skip_until = 0.0

    def _record_primary_failure(self) -> None:
        self._primary_consecutive_failures += 1
        if self._primary_consecutive_failures >= self.PRIMARY_FAILURE_THRESHOLD:
            self._primary_skip_until = time.monotonic() + self.PRIMARY_COOLDOWN_SECONDS
            log.warning(
                "TTS primary tripped breaker after %d consecutive failures; "
                "skipping primary for %.0fs",
                self._primary_consecutive_failures,
                self.PRIMARY_COOLDOWN_SECONDS,
            )

    async def synthesize(self, text: str, voice: str | None = None) -> bytes:
        async with httpx.AsyncClient(timeout=CHAIN_TIMEOUT) as client:
            # When primary (Voxtral) is disabled, route everything to Kokoro:
            # Kokoro voices speak in their voice; other voices fall back to
            # the configured fallback voice. The breaker is never touched.
            if not self._primary_enabled:
                kokoro_voice = (
                    voice if voice in self._kokoro_voices else self.fallback["voice"]
                )
                resp = await client.post(
                    self.fallback["url"],
                    json={
                        "input": text,
                        "voice": kokoro_voice,
                        "model": self.fallback["model"],
                        "response_format": self.response_format,
                    },
                )
                resp.raise_for_status()
                return resp.content

            if voice and voice in self._kokoro_voices:
                resp = await client.post(
                    self.fallback["url"],
                    json={
                        "input": text,
                        "voice": voice,
                        "model": self.fallback["model"],
                        "response_format": self.response_format,
                    },
                )
                resp.raise_for_status()
                return resp.content

            if not self._primary_is_tripped():
                try:
                    resp = await client.post(
                        self.primary["url"],
                        json={
                            "input": text,
                            "voice": voice or self.primary["voice"],
                            "model": self.primary["model"],
                            "response_format": self.response_format,
                        },
                    )
                    resp.raise_for_status()
                    self._record_primary_success()
                    return resp.content
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    self._record_primary_failure()
                    log.warning("Primary TTS failed (%s), falling back", exc)

            resp = await client.post(
                self.fallback["url"],
                json={
                    "input": text,
                    "voice": self.fallback["voice"],
                    "model": self.fallback["model"],
                    "response_format": self.response_format,
                },
            )
            resp.raise_for_status()
            return resp.content


class LLMChainClient:
    def __init__(self, chain: list[dict]):
        self.chain = chain
        self._lock = threading.Lock()
        self._total_requests = 0
        self._failover_requests = 0
        self._terminal_failures = 0
        self._auth_failures = 0
        self._last_failure_kind: str | None = None
        self._last_success_url: str | None = None
        self._last_success_model: str | None = None
        self._last_failure_error: str | None = None
        self._last_request_attempts = 0
        self._last_request_failed_urls: list[str] = []
        self._last_request_used_fallback = False
        self._endpoint_stats = {
            entry["url"]: EndpointStats()
            for entry in self.chain
        }

    def _record_success(self, outcome: RequestOutcome):
        if not outcome.url:
            return
        with self._lock:
            self._total_requests += 1
            if outcome.attempts > 1:
                self._failover_requests += 1
            stats = self._endpoint_stats.setdefault(outcome.url, EndpointStats())
            stats.successes += 1
            # This endpoint just answered, so whatever it last failed with no
            # longer describes it. Counters stay; current-state fields clear.
            stats.last_error_kind = None
            stats.last_error_status = None
            self._last_success_url = outcome.url
            self._last_success_model = outcome.model
            self._last_failure_error = None
            self._last_request_attempts = outcome.attempts
            self._last_request_failed_urls = list(outcome.failed_urls)
            self._last_request_used_fallback = outcome.attempts > 1

            for failed_url in outcome.failed_urls:
                failed_stats = self._endpoint_stats.setdefault(failed_url, EndpointStats())
                failed_stats.failures += 1

    def _record_failure(self, outcome: RequestOutcome):
        with self._lock:
            self._total_requests += 1
            self._terminal_failures += 1
            self._last_failure_error = outcome.error
            self._last_request_attempts = outcome.attempts
            self._last_request_failed_urls = list(outcome.failed_urls)
            self._last_request_used_fallback = outcome.attempts > 1
            for failed_url in outcome.failed_urls:
                failed_stats = self._endpoint_stats.setdefault(failed_url, EndpointStats())
                failed_stats.failures += 1

    def _mark_attempt(self, url: str):
        with self._lock:
            stats = self._endpoint_stats.setdefault(url, EndpointStats())
            stats.attempts += 1

    def _record_endpoint_error(self, url: str, exc: Exception) -> str:
        """Classify and record a single failed attempt against one endpoint.

        Returns the kind so callers can log it. Auth failures get an explicit
        error-level log naming the env var to check, because the whole point of
        this classification is that a stale key would otherwise read as "that
        endpoint is flaky".
        """
        kind, status = classify_chain_error(exc)
        with self._lock:
            stats = self._endpoint_stats.setdefault(url, EndpointStats())
            stats.last_error_kind = kind
            stats.last_error_status = status
            self._last_failure_kind = kind
            if kind == "auth":
                stats.auth_failures += 1
                self._auth_failures += 1
        if kind == "auth":
            origin = endpoint_origin(url)
            log.error(
                "LLM %s rejected our credentials (HTTP %s). This is an AUTH failure, "
                "not an outage — check the bearer token for origin %s "
                "(OCTAVIUS_8010_API_KEY, or OCTAVIUS_LLM_API_KEYS). Sending %s.",
                url, status, origin,
                "a key" if settings.llm_api_keys.get(origin) else "NO key",
            )
        return kind

    def get_health(self) -> dict:
        with self._lock:
            endpoints = []
            rejecting_credentials = []

            def row(url: str, model: str | None, off_chain: bool) -> dict:
                stats = self._endpoint_stats.get(url, EndpointStats())
                if stats.last_error_kind == "auth":
                    rejecting_credentials.append(url)
                return {
                    "url": url,
                    "model": model,
                    "attempts": stats.attempts,
                    "successes": stats.successes,
                    "failures": stats.failures,
                    "auth_failures": stats.auth_failures,
                    # Cleared on this endpoint's next success, so a non-null
                    # value means "currently believed broken, this way".
                    "last_error_kind": stats.last_error_kind,
                    "last_error_status": stats.last_error_status,
                    "authenticated": bool(
                        settings.llm_api_keys.get(endpoint_origin(url))
                    ),
                    "off_chain": off_chain,
                }

            configured = {entry["url"] for entry in self.chain}
            endpoints = [row(e["url"], e["model"], False) for e in self.chain]
            # Callers may target a URL outside this client's own chain via the
            # `urls=` argument — the reader does exactly that, reaching
            # lilripper:8010 through llm_client. Those endpoints still record
            # stats, so surface them here or their auth failures would bump the
            # chain-level counter without ever naming the endpoint.
            endpoints += [
                row(url, None, True)
                for url in sorted(self._endpoint_stats)
                if url not in configured
            ]
            return {
                "configured_endpoints": len(self.chain),
                "total_requests": self._total_requests,
                "failover_requests": self._failover_requests,
                "terminal_failures": self._terminal_failures,
                "auth_failures": self._auth_failures,
                # The headline: endpoints whose most recent attempt was a
                # 401/403. Non-empty means a key problem, not an outage.
                "endpoints_rejecting_credentials": rejecting_credentials,
                "last_failure_kind": self._last_failure_kind,
                "last_success_url": self._last_success_url,
                "last_success_model": self._last_success_model,
                "last_failure_error": self._last_failure_error,
                "last_request_attempts": self._last_request_attempts,
                "last_request_failed_urls": list(self._last_request_failed_urls),
                "last_request_used_fallback": self._last_request_used_fallback,
                "endpoints": endpoints,
            }

    @asynccontextmanager
    async def stream_chat(self, payload: dict) -> AsyncIterator[httpx.Response]:
        failed_urls: list[str] = []
        async with httpx.AsyncClient(timeout=CHAIN_TIMEOUT) as client:
            for i, entry in enumerate(self.chain):
                self._mark_attempt(entry["url"])
                try:
                    request_payload = dict(payload)
                    request_payload["model"] = entry["model"]
                    if i > 0:
                        log.warning(
                            "LLM failover attempt %d/%d via %s",
                            i + 1,
                            len(self.chain),
                            entry["url"],
                        )
                    async with client.stream(
                        "POST",
                        entry["url"],
                        json=request_payload,
                        headers=auth_headers(entry["url"]),
                    ) as resp:
                        if resp.status_code >= 400:
                            await resp.aread()
                        resp.raise_for_status()
                        self._record_success(
                            RequestOutcome(
                                url=entry["url"],
                                model=entry["model"],
                                attempts=i + 1,
                                failed_urls=failed_urls,
                            )
                        )
                        if failed_urls:
                            log.warning(
                                "LLM request succeeded via fallback %s after failures on %s",
                                entry["url"],
                                ", ".join(failed_urls),
                            )
                        yield resp
                        return
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    failed_urls.append(entry["url"])
                    self._record_endpoint_error(entry["url"], exc)
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                        try:
                            body = exc.response.text[:1000]
                        except Exception:
                            body = "(unreadable)"
                        log.warning(
                            "LLM %s returned %d; body: %s",
                            entry["url"], exc.response.status_code, body,
                        )
                    if i < len(self.chain) - 1:
                        log.warning("LLM %s failed (%s), trying next", entry["url"], exc)
                    else:
                        self._record_failure(
                            RequestOutcome(
                                url=None,
                                model=None,
                                attempts=i + 1,
                                failed_urls=failed_urls,
                                error=str(exc),
                            )
                        )
                        raise

    async def complete(self, payload: dict, *, urls: list[str] | None = None) -> str | None:
        target_urls = urls or [entry["url"] for entry in self.chain]
        model = payload.get("model") or self.chain[0]["model"]
        request_payload = dict(payload)
        request_payload["model"] = model
        failed_urls: list[str] = []
        async with httpx.AsyncClient(timeout=CHAIN_TIMEOUT) as client:
            for i, url in enumerate(target_urls):
                self._mark_attempt(url)
                try:
                    if i > 0:
                        log.warning(
                            "LLM failover attempt %d/%d via %s",
                            i + 1,
                            len(target_urls),
                            url,
                        )
                    resp = await client.post(url, json=request_payload, headers=auth_headers(url))
                    resp.raise_for_status()
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    self._record_success(
                        RequestOutcome(
                            url=url,
                            model=model,
                            attempts=i + 1,
                            failed_urls=failed_urls,
                        )
                    )
                    if failed_urls:
                        log.warning(
                            "LLM request succeeded via fallback %s after failures on %s",
                            url,
                            ", ".join(failed_urls),
                        )
                    return text
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError, KeyError, IndexError, json.JSONDecodeError) as exc:
                    failed_urls.append(url)
                    self._record_endpoint_error(url, exc)
                    log.debug("Completion failed via %s", url, exc_info=True)
                    continue
        self._record_failure(
            RequestOutcome(
                url=None,
                model=model,
                attempts=len(target_urls),
                failed_urls=failed_urls,
                error="All LLM endpoints failed",
            )
        )
        return None

    async def complete_with_tools(self, payload: dict, *, urls: list[str] | None = None) -> dict | None:
        """Non-streaming completion returning the full message dict (content + tool_calls).

        Used by the subagent loop which needs to inspect tool_calls in the response.
        When `urls` is provided, only chain entries whose url is in that list are
        tried, preserving chain order. Each attempt uses the model from its chain
        entry (payload["model"] is ignored when the entry carries a model).
        """
        if urls is not None:
            target_entries = [e for e in self.chain if e["url"] in urls]
        else:
            target_entries = list(self.chain)
        if not target_entries:
            log.warning("complete_with_tools called with urls=%s but no chain entries matched", urls)
            return None

        payload_model = payload.get("model") or self.chain[0]["model"]
        request_payload = dict(payload)
        request_payload["stream"] = False
        failed_urls: list[str] = []
        async with httpx.AsyncClient(timeout=CHAIN_TIMEOUT) as client:
            for i, entry in enumerate(target_entries):
                self._mark_attempt(entry["url"])
                model = entry.get("model") or payload_model
                request_payload["model"] = model
                try:
                    if i > 0:
                        log.warning(
                            "LLM failover attempt %d/%d via %s",
                            i + 1, len(target_entries), entry["url"],
                        )
                    resp = await client.post(
                        entry["url"],
                        json=request_payload,
                        headers=auth_headers(entry["url"]),
                    )
                    resp.raise_for_status()
                    message = resp.json()["choices"][0]["message"]
                    self._record_success(
                        RequestOutcome(
                            url=entry["url"],
                            model=model,
                            attempts=i + 1,
                            failed_urls=failed_urls,
                        )
                    )
                    if failed_urls:
                        log.warning(
                            "LLM request succeeded via fallback %s after failures on %s",
                            entry["url"], ", ".join(failed_urls),
                        )
                    return message
                except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError,
                        KeyError, IndexError, json.JSONDecodeError) as exc:
                    failed_urls.append(entry["url"])
                    self._record_endpoint_error(entry["url"], exc)
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
                        try:
                            body = exc.response.text[:1000]
                        except Exception:
                            body = "(unreadable)"
                        log.warning("LLM %s returned %d; body: %s", entry["url"], exc.response.status_code, body)
                    log.debug("Completion with tools failed via %s", entry["url"], exc_info=True)
                    continue
        self._record_failure(
            RequestOutcome(
                url=None, model=payload_model,
                attempts=len(target_entries),
                failed_urls=failed_urls,
                error="All LLM endpoints failed",
            )
        )
        return None


class SummaryClient:
    """Conversation-end summary/tag generation.

    Unlike LLMChainClient this carries one model for both URLs (the payload's
    model is sent as-is), so both endpoints must serve the same alias. It does
    attach `auth_headers` — without them an authenticated endpoint here 401s
    silently, since a failed summary only means a missing summary, never a
    user-visible error.
    """

    def __init__(self, primary_url: str, fallback_url: str):
        self.urls = [primary_url, fallback_url]

    def complete(self, payload: dict, *, timeout: int) -> str | None:
        failed_urls: list[str] = []
        for i, url in enumerate(self.urls):
            try:
                if i > 0:
                    log.warning(
                        "Summary fallback attempt %d/%d via %s",
                        i + 1,
                        len(self.urls),
                        url,
                    )
                resp = requests.post(
                    url, json=payload, timeout=timeout, headers=auth_headers(url)
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"].strip()
                if failed_urls:
                    log.warning(
                        "Summary request succeeded via fallback %s after failures on %s",
                        url,
                        ", ".join(failed_urls),
                    )
                return text
            except Exception:
                failed_urls.append(url)
                log.debug("Summary completion failed via %s", url, exc_info=True)
                continue
        return None

    async def acomplete(self, payload: dict, *, timeout: int) -> str | None:
        failed_urls: list[str] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for i, url in enumerate(self.urls):
                try:
                    if i > 0:
                        log.warning(
                            "Summary fallback attempt %d/%d via %s",
                            i + 1,
                            len(self.urls),
                            url,
                        )
                    resp = await client.post(url, json=payload, headers=auth_headers(url))
                    resp.raise_for_status()
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    if failed_urls:
                        log.warning(
                            "Summary request succeeded via fallback %s after failures on %s",
                            url,
                            ", ".join(failed_urls),
                        )
                    return text
                except Exception:
                    failed_urls.append(url)
                    log.debug("Async summary completion failed via %s", url, exc_info=True)
                    continue
        return None


class EmbeddingClient:
    """Chain of embedding endpoints with per-endpoint schema and transient retry.

    Each chain entry has `url`, `model`, and `schema` ("ollama" or "openai"):

    - `ollama`  — POST `{url}` with `{"model", "prompt"}`, response `{"embedding": [...]}`
    - `openai`  — POST `{url}` with `{"model", "input"}`, response `{"data": [{"embedding": [...]}]}`

    On each endpoint the client retries once for transient errors (HTTP 5xx,
    read timeout). Connection errors (endpoint down) and programmer errors (bad
    response shape) do not retry — the chain moves on to the next endpoint
    immediately. Terminal failures after the whole chain is exhausted are
    logged at WARNING so they show up in journalctl without debug logging.

    Circuit breaker (per endpoint, keyed by URL). After
    ENDPOINT_FAILURE_THRESHOLD consecutive failures an endpoint is "tripped" and
    skipped entirely for ENDPOINT_COOLDOWN_SECONDS. Without this, a chain whose
    primary is a dead host re-pays the full connect budget on *every* embed —
    there is no other failure memory. When the cooldown elapses the endpoint is
    half-open: exactly one caller claims the probe (`_probe_in_flight`) while
    everyone else keeps skipping, so concurrent callers can't stampede a host
    that is still down. A success closes the breaker, a failure re-trips it.

    Known race, deliberately accepted: a failure recorded by a slow call can
    re-trip an endpoint just after a newer call succeeded. The cost is one
    cooldown spent on an equivalent fallback model, which is not worth a
    generation counter.

    When every endpoint is tripped the call returns None with no HTTP traffic.
    That is safe because all callers degrade: the search paths fall back to
    LIKE, and the store paths write no row (which is exactly what the embedding
    sweeper looks for).
    """

    PER_ENDPOINT_ATTEMPTS = 2
    RETRY_BACKOFF_SECONDS = 0.3
    ENDPOINT_FAILURE_THRESHOLD = 2
    ENDPOINT_COOLDOWN_SECONDS = 300.0
    # A LAN TCP handshake is milliseconds; only a genuinely unreachable host
    # gets near this. Capped separately from the read budget so a dead endpoint
    # is detected fast instead of burning the full timeout.
    CONNECT_TIMEOUT_SECONDS = 1.5
    _VALID_SCHEMAS = ("ollama", "openai")

    def __init__(self, chain: list[dict]):
        if not chain:
            raise ValueError("embedding chain must have at least one endpoint")
        for entry in chain:
            if entry.get("schema") not in self._VALID_SCHEMAS:
                raise ValueError(
                    f"embedding endpoint {entry.get('url')!r} has unknown schema "
                    f"{entry.get('schema')!r}; must be one of {self._VALID_SCHEMAS}"
                )
            if not entry.get("url") or not entry.get("model"):
                raise ValueError(f"embedding endpoint missing url or model: {entry!r}")
        self.chain = list(chain)
        # threading.Lock, not asyncio.Lock: this is a module singleton reached
        # from the event loop *and* from asyncio.to_thread workers (agent.py's
        # episodic recall). Critical sections are dict reads/writes with no I/O.
        self._breaker_lock = threading.Lock()
        self._consecutive_failures: dict[str, int] = {}
        self._skip_until: dict[str, float] = {}  # monotonic; absent means closed
        self._probe_in_flight: set[str] = set()

    def _connect_timeout(self, timeout: float) -> float:
        return min(self.CONNECT_TIMEOUT_SECONDS, timeout)

    def _claim_endpoint(self, url: str) -> tuple[bool, bool]:
        """Returns (may_try, claimed_probe). Caller must release a claimed probe."""
        now = time.monotonic()
        with self._breaker_lock:
            skip_until = self._skip_until.get(url)
            if skip_until is None:
                return True, False
            if now < skip_until:
                return False, False
            # Cooldown elapsed: half-open. Exactly one caller probes.
            if url in self._probe_in_flight:
                return False, False
            self._probe_in_flight.add(url)
            return True, True

    def _release_probe(self, url: str) -> None:
        with self._breaker_lock:
            self._probe_in_flight.discard(url)

    def _record_endpoint_success(self, url: str) -> None:
        with self._breaker_lock:
            recovered = bool(self._consecutive_failures.get(url) or self._skip_until.get(url))
            self._consecutive_failures.pop(url, None)
            self._skip_until.pop(url, None)
        if recovered:
            log.info("Embedding endpoint %s recovered, closing breaker", url)

    def _record_endpoint_failure(self, url: str) -> None:
        with self._breaker_lock:
            failures = self._consecutive_failures.get(url, 0) + 1
            self._consecutive_failures[url] = failures
            tripped = failures >= self.ENDPOINT_FAILURE_THRESHOLD
            if tripped:
                self._skip_until[url] = time.monotonic() + self.ENDPOINT_COOLDOWN_SECONDS
        if tripped:
            log.warning(
                "Embedding endpoint %s tripped breaker after %d consecutive "
                "failures; skipping it for %.0fs",
                url,
                failures,
                self.ENDPOINT_COOLDOWN_SECONDS,
            )

    def get_health(self) -> dict:
        now = time.monotonic()
        with self._breaker_lock:
            endpoints = []
            for entry in self.chain:
                url = entry["url"]
                skip_until = self._skip_until.get(url, 0.0)
                endpoints.append({
                    "url": url,
                    "model": entry["model"],
                    "schema": entry["schema"],
                    "tripped": now < skip_until,
                    "consecutive_failures": self._consecutive_failures.get(url, 0),
                    "cooldown_remaining": round(max(0.0, skip_until - now), 1),
                })
        return {
            "configured_endpoints": len(endpoints),
            "all_tripped": all(e["tripped"] for e in endpoints),
            "endpoints": endpoints,
        }

    @staticmethod
    def _build_payload(entry: dict, text: str) -> dict:
        if entry["schema"] == "ollama":
            return {"model": entry["model"], "prompt": text}
        return {"model": entry["model"], "input": text}

    @staticmethod
    def _extract_vector(entry: dict, body: dict) -> list[float]:
        if entry["schema"] == "ollama":
            return body["embedding"]
        return body["data"][0]["embedding"]

    @staticmethod
    def _is_retry_worthy(exc: Exception) -> bool:
        """Should we retry the same endpoint? Only transient server-side issues."""
        # Must precede the timeout checks: ConnectTimeout subclasses
        # httpx.TimeoutException (and requests' subclasses Timeout), so without
        # this a host that swallows SYNs was retried and cost two full connect
        # budgets per embed instead of one.
        if isinstance(exc, (httpx.ConnectTimeout, requests.exceptions.ConnectTimeout)):
            return False
        if isinstance(exc, httpx.TimeoutException):
            return True
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            return exc.response.status_code >= 500
        if isinstance(exc, requests.exceptions.Timeout):
            return True
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            return exc.response.status_code >= 500
        return False

    def embed_text(self, text: str, *, timeout: int) -> bytes | None:
        last_exc: Exception | None = None
        tried_any = False
        for entry in self.chain:
            url = entry["url"]
            may_try, probing = self._claim_endpoint(url)
            if not may_try:
                continue
            tried_any = True
            try:
                for attempt in range(1, self.PER_ENDPOINT_ATTEMPTS + 1):
                    try:
                        resp = requests.post(
                            url,
                            json=self._build_payload(entry, text),
                            timeout=(self._connect_timeout(timeout), timeout),
                        )
                        resp.raise_for_status()
                        vec = np.array(self._extract_vector(entry, resp.json()), dtype=np.float32)
                        self._record_endpoint_success(url)
                        return vec.tobytes()
                    except Exception as exc:
                        last_exc = exc
                        if attempt < self.PER_ENDPOINT_ATTEMPTS and self._is_retry_worthy(exc):
                            log.debug("Embedding %s attempt %d failed (%s), retrying", url, attempt, exc)
                            time.sleep(self.RETRY_BACKOFF_SECONDS)
                            continue
                        log.debug("Embedding endpoint %s failed: %s", url, exc)
                        break
                # Reached only when the attempts were exhausted — a success
                # returns above. One failure per call, not per attempt, so the
                # retry budget and the breaker threshold stay independent knobs.
                # Recorded before the probe is released so a half-open probe
                # can't be re-claimed against an endpoint that just failed.
                # A cancellation propagates past this, and rightly so: an
                # abandoned call is no evidence the endpoint is unhealthy.
                self._record_endpoint_failure(url)
            finally:
                if probing:
                    self._release_probe(url)
        if not tried_any:
            log.warning("All embedding endpoints are tripped; skipping embed")
        else:
            log.warning("All embedding endpoints failed: %s", last_exc)
        return None

    async def aembed_text(self, text: str, *, timeout: int) -> bytes | None:
        last_exc: Exception | None = None
        tried_any = False
        client_timeout = httpx.Timeout(timeout, connect=self._connect_timeout(timeout))
        async with httpx.AsyncClient(timeout=client_timeout) as client:
            for entry in self.chain:
                url = entry["url"]
                may_try, probing = self._claim_endpoint(url)
                if not may_try:
                    continue
                tried_any = True
                try:
                    for attempt in range(1, self.PER_ENDPOINT_ATTEMPTS + 1):
                        try:
                            resp = await client.post(
                                url,
                                json=self._build_payload(entry, text),
                            )
                            resp.raise_for_status()
                            vec = np.array(self._extract_vector(entry, resp.json()), dtype=np.float32)
                            self._record_endpoint_success(url)
                            return vec.tobytes()
                        except Exception as exc:
                            last_exc = exc
                            if attempt < self.PER_ENDPOINT_ATTEMPTS and self._is_retry_worthy(exc):
                                log.debug("Async embedding %s attempt %d failed (%s), retrying", url, attempt, exc)
                                await asyncio.sleep(self.RETRY_BACKOFF_SECONDS)
                                continue
                            log.debug("Async embedding endpoint %s failed: %s", url, exc)
                            break
                    # See embed_text: recorded inside the try so a cancellation
                    # skips it, and before the probe is released.
                    self._record_endpoint_failure(url)
                finally:
                    if probing:
                        self._release_probe(url)
        if not tried_any:
            log.warning("All embedding endpoints are tripped; skipping embed")
        else:
            log.warning("All async embedding endpoints failed: %s", last_exc)
        return None


stt_client = STTClient(settings.stt_url)
tts_client = TTSClient(
    primary={
        "url": settings.tts.url,
        "model": settings.tts.model,
        "voice": settings.tts.voice,
    },
    fallback={
        "url": settings.tts.fallback_url,
        "model": settings.tts.fallback_model,
        "voice": settings.tts.fallback_voice,
    },
    response_format=settings.tts.format,
    kokoro_voices=settings.tts.kokoro_voices,
    primary_enabled=settings.tts.voxtral_enabled,
)
llm_client = LLMChainClient(settings.llm_chain)
subagent_llm_client = LLMChainClient(settings.subagent_llm_chain)
# Vision-capable chain for turns whose current message carries an OpenAI-style
# multimodal content array (image_input WS frames). See agent.py's use_vision
# routing in stream_agent_turn.
vision_llm_client = LLMChainClient(settings.vision_llm_chain)
summary_client = SummaryClient(settings.summary_url, settings.summary_fallback_url)
embedding_client = EmbeddingClient(settings.embedding_chain)
