"""MCP client for PDF -> markdown conversion.

Octavius submits PDFs through its configured ``document-processing`` MCP server,
whose stdio wrapper lives at
``/home/dave/git_repos/mcp-tools/.venv/bin/python server_documents_voice_wrapper.py``.
That wrapper uploads PDFs to lilripper for conversion and returns LOCAL markdown
and metadata paths back to Octavius when conversion completes.
"""

from __future__ import annotations

import asyncio
import re
import time

from settings import settings

_JOB_ID_RE = re.compile(r"Job ID:\s*(\S+)")
_TEXT_FILE_RE = re.compile(r"Text file:\s*(.+)")
_META_FILE_RE = re.compile(r"Metadata \(JSON\):\s*(.+)")
_STAGE_RE = re.compile(r"still in progress\. Current stage:\s*(.+?)\.", re.IGNORECASE | re.DOTALL)

_completed: dict[str, dict] = {}


class DocprocError(RuntimeError):
    """Terminal failure: submission rejected, conversion failed, unknown job, or timeout."""


async def submit_job(mcp, source_path: str) -> str:
    """Start a conversion and return the parsed MCP job id."""
    text = await mcp.call_tool("convert_pdf_to_md", {"file_path": source_path})
    match = _JOB_ID_RE.search(str(text))
    if not match:
        raise DocprocError(str(text))
    job_id = match.group(1)
    # The wrapper's job ids are a per-process counter; if its stdio process
    # restarted, a fresh job can reuse an id we already cached a terminal
    # outcome for. Evict so status probes reflect the new job.
    _completed.pop(job_id, None)
    return job_id


async def poll_job(
    mcp,
    job_id: str,
    *,
    interval: float | None = None,
    timeout: float | None = None,
) -> dict:
    """Poll get_conversion_result until terminal success/failure or timeout."""
    interval = settings.docproc_poll_interval if interval is None else interval
    timeout = settings.docproc_poll_timeout if timeout is None else timeout
    start = time.monotonic()

    while True:
        text = str(await mcp.call_tool("get_conversion_result", {"job_id": job_id}))
        row = _classify_result_text(job_id, text, cache_terminal=True)
        status = row["status"]

        if status == "done":
            return row
        if status in ("error", "unknown"):
            raise DocprocError(row.get("error_msg") or text)

        if time.monotonic() - start >= timeout:
            # Deliberately NOT cached: the wrapper may still finish the job,
            # so a later get_job_status probe should ask it live.
            raise DocprocError(f"docproc job {job_id} timed out after {timeout:.0f}s")

        await asyncio.sleep(interval)


async def get_job_status(mcp, job_id: str) -> dict:
    """Probe one conversion job, using cached terminal outcomes when available."""
    if job_id in _completed:
        return _completed[job_id]

    text = str(await mcp.call_tool("get_conversion_result", {"job_id": job_id}))
    return _classify_result_text(job_id, text, cache_terminal=True)


def _classify_result_text(job_id: str, text: str, *, cache_terminal: bool) -> dict:
    if _is_done(text):
        row = {
            "id": job_id,
            "status": "done",
            "result_md_path": _first_match(_TEXT_FILE_RE, text),
            "result_meta_path": _first_match(_META_FILE_RE, text),
            "result_text": text,
        }
        if cache_terminal:
            _completed[job_id] = row
        return row

    if text.startswith("Conversion failed:"):
        return _cache_error(job_id, text, cache_terminal=cache_terminal)

    if text.startswith("Unknown job ID"):
        row = {"id": job_id, "status": "unknown"}
        if cache_terminal:
            _completed[job_id] = row
        return row

    if text.startswith("Error"):
        # Transport/server trouble (call_tool never raises — it returns
        # "Error calling ..." / "Error: server ... not connected"). Possibly
        # transient, so never cached: the job may still be alive.
        return {"id": job_id, "status": "error", "error_msg": text}

    stage_match = _STAGE_RE.search(text)
    if stage_match:
        return {"id": job_id, "status": "running", "stage": stage_match.group(1).strip()}

    # Unrecognized text — report as an error but don't cache it as terminal.
    return {"id": job_id, "status": "error", "error_msg": text}


def _is_done(text: str) -> bool:
    return "converted to markdown successfully" in text


def _first_match(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def _cache_error(job_id: str, text: str, *, cache_terminal: bool) -> dict:
    row = {"id": job_id, "status": "error", "error_msg": text}
    if cache_terminal:
        _completed[job_id] = row
    return row
