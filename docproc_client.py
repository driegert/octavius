"""HTTP client for the docproc web queue (PDF -> markdown conversion).

Octavius talks to docproc purely over loopback HTTP (POST /api/jobs, GET
/api/jobs/lookup) and never imports the docproc package — the two repos stay
decoupled. The wire shape mirrors what docproc's own MCP wrapper
(mcp-tools/server_documents.py, `_convert_via_queue` / `submit_pdf_batch` /
`get_jobs_status`) already speaks against the same queue, so this client is a
minimal reimplementation of that same contract for Octavius's own use. See
docs/ws-media-contract.md for the WS side that triggers this.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from settings import settings

log = logging.getLogger(__name__)

_TERMINAL_FAILED = ("error", "canceled")


class DocprocError(RuntimeError):
    """Raised when a docproc submission or poll fails terminally (error,
    canceled, unknown job id, or timeout)."""


async def submit_job(source_path: str, mode: str = "full", caller: str = "octavius") -> dict:
    """POST /api/jobs — submit a PDF for conversion.

    Returns the job dict as created (has at least ``id`` and ``status``).
    Raises on connection failure or a non-2xx response.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{settings.docproc_url}/api/jobs",
            json={"source_path": source_path, "mode": mode, "caller": caller},
        )
        resp.raise_for_status()
        return resp.json()


async def get_job_status(job_id: str) -> dict:
    """GET /api/jobs/lookup?ids=<job_id> — fetch current status for one job.

    Returns ``{"id": job_id, "status": "unknown"}`` if the queue has no
    record of it (typo'd id, expired data) rather than raising, mirroring
    docproc's own `get_jobs_status` MCP tool behavior for unknown ids.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{settings.docproc_url}/api/jobs/lookup",
            params={"ids": job_id},
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            return {"id": job_id, "status": "unknown"}
        return rows[0]


async def poll_job(job_id: str, *, interval: float | None = None, timeout: float | None = None) -> dict:
    """Poll a docproc job until it reaches 'done' or the timeout elapses.

    Async (uses asyncio.sleep via caller's event loop is NOT required here —
    we sleep with time.sleep-free asyncio.sleep) and safe to run as a
    background task without blocking the WS event loop or other sessions.

    Returns the final job dict on 'done'. Raises DocprocError on 'error',
    'canceled', 'unknown', or timeout.
    """
    interval = settings.docproc_poll_interval if interval is None else interval
    timeout = settings.docproc_poll_timeout if timeout is None else timeout
    deadline = time.monotonic() + timeout

    while True:
        row = await get_job_status(job_id)
        status = row.get("status")
        if status == "done":
            return row
        if status in _TERMINAL_FAILED:
            raise DocprocError(
                f"docproc job {job_id} ended in status {status}: "
                f"{row.get('error_msg') or '(no error message)'}"
            )
        if status == "unknown":
            raise DocprocError(f"docproc job {job_id} not found (status=unknown)")
        if time.monotonic() > deadline:
            raise DocprocError(
                f"docproc job {job_id} timed out after {timeout:.0f}s (status={status})"
            )
        await asyncio.sleep(interval)
