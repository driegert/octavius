"""Async/sync HTTP client for the Octavius memory service (v2).

Octavius is a thin client of the shared memory brain: it pushes closed
(user+assistant) transcripts and reads the always-on profile + per-turn facts over
loopback, instead of poking the memory tables in-process. Two invariants:

- **Best-effort.** Every call swallows failures, logs, and returns a null result.
  A down/slow service degrades Octavius to memory-less behaviour but NEVER breaks a
  turn or a conversation close.
- **The watermark stays client-side.** Octavius owns ``conversations.last_extracted
  _message_id`` (what it has already pushed); the service owns the facts. So the
  trust boundary is enforced before the wire: only user+assistant turns are sent.

Tests inject an ``httpx.ASGITransport`` (``transport=``) to run the real client
against an in-process service with no network; production leaves it None.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)


class MemoryClient:
    def __init__(self, base_url: str, *, service: str = "octavius",
                 read_timeout: float = 10.0, write_timeout: float = 120.0,
                 transport=None):
        self.base_url = (base_url or "").rstrip("/")
        self.service = service
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self._transport = transport   # httpx.ASGITransport in tests; None in prod

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def _aclient(self, timeout: float) -> httpx.AsyncClient:
        kw = {"base_url": self.base_url or "http://memory", "timeout": timeout}
        if self._transport is not None:
            kw["transport"] = self._transport
        return httpx.AsyncClient(**kw)

    async def _call(self, method: str, path: str, *, params=None, json=None,
                    timeout: float | None = None):
        if not self.enabled:
            return None
        try:
            async with self._aclient(timeout or self.read_timeout) as c:
                r = await c.request(method, path, params=params, json=json)
                r.raise_for_status()
                return r.json()
        except Exception:
            log.warning("memory %s %s failed", method, path, exc_info=True)
            return None

    # --- read path (per turn) ------------------------------------------------

    async def fetch_injection(self, user_text: str, *, k: int | None = None):
        """Return ``(profile_str, [fact_line, ...])`` from concurrent GET /profile +
        GET /facts. Degrades to ``("", [])`` on any failure or when disabled."""
        if not self.enabled:
            return "", []
        params = {"q": user_text}
        if k:
            params["k"] = k
        try:
            async with self._aclient(self.read_timeout) as c:
                prof_r, facts_r = await asyncio.gather(
                    c.get("/profile"), c.get("/facts", params=params),
                    return_exceptions=True)
            return _field(prof_r, "profile", "") or "", _field(facts_r, "facts", []) or []
        except Exception:
            log.warning("memory fetch_injection failed", exc_info=True)
            return "", []

    # --- write path (conversation close) -------------------------------------

    async def push_conversation(self, conv_key: str, transcript: list[dict], *,
                                summary: str | None = None,
                                ended_at: str | None = None, index: bool = True):
        return await self._call("POST", "/conversations", timeout=self.write_timeout,
                                json=self._push_body(conv_key, transcript, summary,
                                                     ended_at, index))

    def push_conversation_sync(self, conv_key: str, transcript: list[dict], *,
                               summary: str | None = None,
                               ended_at: str | None = None, index: bool = True):
        """Sync sibling of push_conversation for the (legacy, unused-in-live-flow)
        sync ``ConversationSession.end()`` path."""
        if not self.enabled:
            return None
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.write_timeout) as c:
                r = c.post("/conversations",
                           json=self._push_body(conv_key, transcript, summary,
                                                ended_at, index))
                r.raise_for_status()
                return r.json()
        except Exception:
            log.warning("memory push_conversation_sync failed (conv_key=%s)",
                        conv_key, exc_info=True)
            return None

    def _push_body(self, conv_key, transcript, summary, ended_at, index) -> dict:
        return {"service": self.service, "conv_key": conv_key,
                "transcript": transcript, "summary": summary,
                "ended_at": ended_at, "index": index}

    # --- control tools -------------------------------------------------------

    async def remember(self, statement: str, *, conv_key: str = "manual"):
        return await self._call("POST", "/facts/remember", timeout=self.write_timeout,
                                json={"service": self.service, "conv_key": conv_key,
                                      "statement": statement})

    async def forget(self, query: str):
        return await self._call("POST", "/facts/forget", json={"query": query})

    async def correct(self, old: str, new: str, *, conv_key: str = "manual"):
        return await self._call("POST", "/facts/correct", timeout=self.write_timeout,
                                json={"service": self.service, "conv_key": conv_key,
                                      "old": old, "new": new})

    async def what_do_you_know(self, about: str | None = None):
        return await self._call("GET", "/facts/all",
                                params={"about": about} if about else None)


def _field(resp, key, default):
    if isinstance(resp, httpx.Response) and resp.status_code == 200:
        try:
            return resp.json().get(key, default)
        except Exception:
            return default
    return default


# Module-level singleton, configured from settings. Imported by the write path
# (history.py), read path (agent.py) and the control tools (local_tool_memory.py).
from settings import settings  # noqa: E402

memory_client = MemoryClient(
    settings.memory_service_url,
    read_timeout=settings.memory_read_timeout,
    write_timeout=settings.memory_write_timeout,
)
