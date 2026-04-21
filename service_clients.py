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

from settings import settings

log = logging.getLogger(__name__)


@dataclass
class EndpointStats:
    attempts: int = 0
    successes: int = 0
    failures: int = 0


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
    Voice-routed TTS. The selected voice determines the engine:

    - Voices in `kokoro_voices` go straight to the fallback (Kokoro) endpoint,
      with no Voxtral attempt and no breaker interaction. Users pick these
      when they want reliable synthesis and don't care about Voxtral.
    - Any other voice is treated as a primary (Voxtral) voice and goes through
      the primary → fallback path with a circuit breaker on the primary.

    Breaker behavior: after PRIMARY_FAILURE_THRESHOLD consecutive failures the
    primary is "tripped": subsequent synth calls on primary voices skip it
    entirely and go to the fallback with its own voice for
    PRIMARY_COOLDOWN_SECONDS. When the cooldown elapses the next primary-voice
    call probes the primary again ("half-open"); a success closes the breaker,
    a failure re-trips it.
    """

    PRIMARY_FAILURE_THRESHOLD = 3
    PRIMARY_COOLDOWN_SECONDS = 300.0

    def __init__(
        self,
        primary: dict,
        fallback: dict,
        response_format: str,
        kokoro_voices: list[str] | None = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.response_format = response_format
        self._kokoro_voices = set(kokoro_voices or [])
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
        async with httpx.AsyncClient(timeout=120.0) as client:
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

    def get_health(self) -> dict:
        with self._lock:
            endpoints = [
                {
                    "url": entry["url"],
                    "model": entry["model"],
                    "attempts": self._endpoint_stats.get(entry["url"], EndpointStats()).attempts,
                    "successes": self._endpoint_stats.get(entry["url"], EndpointStats()).successes,
                    "failures": self._endpoint_stats.get(entry["url"], EndpointStats()).failures,
                }
                for entry in self.chain
            ]
            return {
                "configured_endpoints": len(self.chain),
                "total_requests": self._total_requests,
                "failover_requests": self._failover_requests,
                "terminal_failures": self._terminal_failures,
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
        async with httpx.AsyncClient(timeout=120.0) as client:
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
                    async with client.stream("POST", entry["url"], json=request_payload) as resp:
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
        async with httpx.AsyncClient(timeout=120.0) as client:
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
                    resp = await client.post(url, json=request_payload)
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
        async with httpx.AsyncClient(timeout=120.0) as client:
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
                    resp = await client.post(entry["url"], json=request_payload)
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
                resp = requests.post(url, json=payload, timeout=timeout)
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
                    resp = await client.post(url, json=payload)
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
    timeout). Connection errors (endpoint down) and programmer errors (bad
    response shape) do not retry — the chain moves on to the next endpoint
    immediately. Terminal failures after the whole chain is exhausted are
    logged at WARNING so they show up in journalctl without debug logging.
    """

    PER_ENDPOINT_ATTEMPTS = 2
    RETRY_BACKOFF_SECONDS = 0.3
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
        for entry in self.chain:
            for attempt in range(1, self.PER_ENDPOINT_ATTEMPTS + 1):
                try:
                    resp = requests.post(
                        entry["url"],
                        json=self._build_payload(entry, text),
                        timeout=timeout,
                    )
                    resp.raise_for_status()
                    vec = np.array(self._extract_vector(entry, resp.json()), dtype=np.float32)
                    return vec.tobytes()
                except Exception as exc:
                    last_exc = exc
                    if attempt < self.PER_ENDPOINT_ATTEMPTS and self._is_retry_worthy(exc):
                        log.debug("Embedding %s attempt %d failed (%s), retrying", entry["url"], attempt, exc)
                        time.sleep(self.RETRY_BACKOFF_SECONDS)
                        continue
                    log.debug("Embedding endpoint %s failed: %s", entry["url"], exc)
                    break
        log.warning("All embedding endpoints failed: %s", last_exc)
        return None

    async def aembed_text(self, text: str, *, timeout: int) -> bytes | None:
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            for entry in self.chain:
                for attempt in range(1, self.PER_ENDPOINT_ATTEMPTS + 1):
                    try:
                        resp = await client.post(
                            entry["url"],
                            json=self._build_payload(entry, text),
                        )
                        resp.raise_for_status()
                        vec = np.array(self._extract_vector(entry, resp.json()), dtype=np.float32)
                        return vec.tobytes()
                    except Exception as exc:
                        last_exc = exc
                        if attempt < self.PER_ENDPOINT_ATTEMPTS and self._is_retry_worthy(exc):
                            log.debug("Async embedding %s attempt %d failed (%s), retrying", entry["url"], attempt, exc)
                            await asyncio.sleep(self.RETRY_BACKOFF_SECONDS)
                            continue
                        log.debug("Async embedding endpoint %s failed: %s", entry["url"], exc)
                        break
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
)
llm_client = LLMChainClient(settings.llm_chain)
subagent_llm_client = LLMChainClient(settings.subagent_llm_chain)
summary_client = SummaryClient(settings.summary_url, settings.summary_fallback_url)
embedding_client = EmbeddingClient(settings.embedding_chain)
