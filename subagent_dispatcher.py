"""Endpoint routing for backgrounded subagents.

Strict-priority dispatcher: primary URL when it has no in-flight task; else
secondary; else wait for one to free. Fallback URL is exposed separately and
is only used for HTTP-level failover on a single call (not as a routing tier).

Designed for a small number of endpoints (2 active + 1 fallback). State is
protected by an asyncio.Lock; all public methods are coroutines.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class SubagentTicket:
    _dispatcher: "SubagentDispatcher"
    _future: asyncio.Future
    assigned_url: str | None = None
    released: bool = False
    cancelled: bool = False

    async def acquire(self) -> str:
        self.assigned_url = await self._future
        return self.assigned_url

    async def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.assigned_url is not None:
            await self._dispatcher._release(self.assigned_url)

    async def cancel_pending(self) -> bool:
        if self.released or self.cancelled:
            return False
        cancelled = await self._dispatcher._cancel_ticket(self)
        if cancelled:
            self.cancelled = True
        return cancelled


@dataclass
class _Endpoint:
    url: str
    model: str
    role: str


class SubagentDispatcher:
    def __init__(self, chain: list[dict]):
        primary = None
        secondary = None
        fallback = None
        for entry in chain:
            ep = _Endpoint(url=entry["url"], model=entry.get("model", ""), role=entry.get("role", ""))
            if ep.role == "primary" and primary is None:
                primary = ep
            elif ep.role == "secondary" and secondary is None:
                secondary = ep
            elif ep.role == "fallback" and fallback is None:
                fallback = ep
        if primary is None:
            raise ValueError("subagent_llm_chain must define a 'primary' endpoint")
        self.primary = primary
        self.secondary = secondary
        self.fallback = fallback
        self.in_flight: dict[str, int] = {primary.url: 0}
        if secondary is not None:
            self.in_flight[secondary.url] = 0
        self.queue: deque[SubagentTicket] = deque()
        self._lock = asyncio.Lock()

    def fallback_url(self) -> str | None:
        return self.fallback.url if self.fallback else None

    def model_for(self, url: str) -> str | None:
        for ep in (self.primary, self.secondary, self.fallback):
            if ep and ep.url == url:
                return ep.model
        return None

    async def reserve(self) -> SubagentTicket:
        loop = asyncio.get_running_loop()
        async with self._lock:
            ticket = SubagentTicket(self, loop.create_future())
            assigned = self._try_assign_locked()
            if assigned is not None:
                self.in_flight[assigned] += 1
                ticket._future.set_result(assigned)
                log.info(
                    "SubagentDispatcher assigned %s immediately (in_flight=%s)",
                    assigned, dict(self.in_flight),
                )
            else:
                self.queue.append(ticket)
                log.info(
                    "SubagentDispatcher queued ticket (queue_depth=%d, in_flight=%s)",
                    len(self.queue), dict(self.in_flight),
                )
            return ticket

    def _try_assign_locked(self) -> str | None:
        if self.in_flight.get(self.primary.url, 0) == 0:
            return self.primary.url
        if self.secondary is not None and self.in_flight.get(self.secondary.url, 0) == 0:
            return self.secondary.url
        return None

    async def _release(self, url: str) -> None:
        async with self._lock:
            if url in self.in_flight:
                self.in_flight[url] = max(0, self.in_flight[url] - 1)
            log.info(
                "SubagentDispatcher released %s (in_flight=%s, queue_depth=%d)",
                url, dict(self.in_flight), len(self.queue),
            )
            while self.queue:
                ticket = self.queue[0]
                if ticket._future.done() or ticket.cancelled:
                    self.queue.popleft()
                    continue
                assigned = self._try_assign_locked()
                if assigned is None:
                    break
                self.queue.popleft()
                self.in_flight[assigned] += 1
                ticket._future.set_result(assigned)
                log.info(
                    "SubagentDispatcher assigned %s from queue (in_flight=%s, queue_depth=%d)",
                    assigned, dict(self.in_flight), len(self.queue),
                )

    async def _cancel_ticket(self, ticket: SubagentTicket) -> bool:
        async with self._lock:
            try:
                self.queue.remove(ticket)
            except ValueError:
                return False
            if not ticket._future.done():
                ticket._future.cancel()
            log.info(
                "SubagentDispatcher cancelled queued ticket (queue_depth=%d)",
                len(self.queue),
            )
            return True

    def snapshot(self) -> dict:
        return {
            "primary": self.primary.url,
            "secondary": self.secondary.url if self.secondary else None,
            "fallback": self.fallback.url if self.fallback else None,
            "in_flight": dict(self.in_flight),
            "queue_depth": len(self.queue),
        }
