"""Local tool: check on a PDF submitted for document processing.

Companion to the deterministic file_input -> MCP submission handled in
websocket_session.py (see docs/ws-media-contract.md). That handler always
mentions the docproc job_id in the turn it generates, so the model can pass
it back here later when Dave asks "is that PDF ready yet?" or similar.
"""

from __future__ import annotations

import logging
from pathlib import Path

import docproc_client

log = logging.getLogger(__name__)

_EXCERPT_CHARS = 1500


async def check_document_status(args: dict, _session=None, _mcp_manager=None) -> str:
    job_id = (args.get("job_id") or "").strip()
    if not job_id:
        return "Error: job_id is required."
    if _mcp_manager is None:
        return "Error checking document status: document-processing MCP manager is unavailable."

    try:
        row = await docproc_client.get_job_status(_mcp_manager, job_id)
    except Exception as exc:
        log.exception("check_document_status failed for job %s", job_id)
        return f"Error checking document status: {exc}"

    status = row.get("status", "unknown")

    if status == "unknown":
        return f"No document conversion job found with id {job_id}."

    if status == "done":
        md_path = row.get("result_md_path")
        lines = [f"Document conversion complete (job {job_id})."]
        if md_path:
            lines.append(f"Markdown file: {md_path}")
            try:
                excerpt = Path(md_path).read_text(encoding="utf-8", errors="replace")[:_EXCERPT_CHARS]
            except OSError:
                excerpt = ""
            if excerpt:
                lines.append(f"Excerpt:\n{excerpt}")
        return "\n".join(lines)

    if status == "error":
        return f"Document conversion failed (job {job_id}): {row.get('error_msg') or 'no error message'}"

    if status == "running":
        stage = row.get("stage")
        if stage:
            return f"Document conversion still in progress (job {job_id}; stage: {stage})."
        return f"Document conversion still in progress (job {job_id})."

    return f"Document conversion still {status} (job {job_id})."
