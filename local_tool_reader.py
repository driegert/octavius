from __future__ import annotations

import asyncio
import importlib
import logging
import re
from pathlib import Path

from db import connect_db
from document_sources import ensure_pdf_suffix, is_likely_html, is_pdf_file, read_text_file
from local_tool_inbox import _format_age
from reader_store import create_document, list_documents
from reader_text import ingest_document
from reader_ingest_handlers import ingest_pdf_document

log = logging.getLogger(__name__)


def _load_trafilatura():
    return importlib.import_module("trafilatura")


async def _ingest_document_task(
    db_path: str | Path,
    doc_id: int,
    markdown: str,
    title: str,
    original_md_path: str | None = None,
):
    with connect_db(Path(db_path)) as conn:
        await ingest_document(conn, doc_id, markdown, title, original_md_path=original_md_path)


def _update_saved_item_content(db_path: str | Path, item_id: int, title: str, content: str):
    with connect_db(Path(db_path)) as conn:
        conn.execute(
            "UPDATE saved_items SET title = ?, content = ? WHERE id = ?",
            (title, content, item_id),
        )
        conn.commit()


async def read_document(args: dict, session=None, mcp_manager=None) -> str:
    path = args.get("path", "")
    if not path:
        return "Error: path is required."

    source_path = Path(path)
    if not source_path.exists():
        return f"Error: file not found: {path}"

    title = args.get("title", source_path.stem)
    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    if source_path.suffix.lower() == ".pdf" or is_pdf_file(source_path):
        pdf_path = source_path if source_path.suffix.lower() == ".pdf" else ensure_pdf_suffix(source_path)
        if pdf_path != source_path and not pdf_path.exists():
            source_path.rename(pdf_path)
        doc_id = create_document(conn, title, "pdf", source_path=str(pdf_path))
        if mcp_manager is None:
            return "Error: MCP manager unavailable."
        asyncio.create_task(ingest_pdf_document(session.db_path, mcp_manager, doc_id, str(pdf_path), title))
        return (
            f"Document '{title}' has been queued for processing (document #{doc_id}). "
            f"Since it's a PDF, it needs to be converted to text first, which takes a few minutes. "
            f"You can check the reader at /reader when it's ready."
        )

    raw = read_text_file(source_path)
    if is_likely_html(raw):
        trafilatura = _load_trafilatura()
        extracted = trafilatura.extract(
            raw,
            include_links=False,
            include_comments=False,
            include_tables=False,
            output_format="txt",
        )
        if not extracted:
            extracted = trafilatura.extract(
                raw,
                include_links=False,
                favor_recall=True,
                output_format="txt",
            )
        if not extracted:
            return f"Error: could not extract article content from {path}"
        markdown = extracted
        meta = trafilatura.extract_metadata(raw)
        if meta and meta.title:
            title = meta.title if not meta.sitename else f"{meta.title} ({meta.sitename})"
    else:
        markdown = raw

    doc_id = create_document(conn, title, "markdown", source_path=path)
    asyncio.create_task(_ingest_document_task(session.db_path, doc_id, markdown, title, original_md_path=path))
    return (
        f"Document '{title}' is being prepared for reading (document #{doc_id}). "
        f"It will be available at /reader in a minute or two."
    )


async def process_pdf_background(args: dict, session=None, mcp_manager=None) -> str:
    from history import save_item

    file_path = args.get("file_path", "")
    if not file_path:
        return "Error: file_path is required."

    source_path = Path(file_path)
    if not source_path.exists():
        return f"Error: file not found: {file_path}"
    if source_path.suffix.lower() != ".pdf" and not is_pdf_file(source_path):
        return f"Error: {file_path} is not a PDF file."
    if source_path.suffix.lower() != ".pdf":
        pdf_path = ensure_pdf_suffix(source_path)
        if not pdf_path.exists():
            source_path.rename(pdf_path)
        source_path = pdf_path

    title = args.get("title", source_path.stem)
    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    conv_id = session.conv_id if session else None
    item_id = save_item(
        conn=conn,
        item_type="article",
        title=f"{title} (processing...)",
        content=f"PDF is being converted to text. Source: {source_path}",
        conversation_id=conv_id,
    )
    asyncio.create_task(run_pdf_processing(session.db_path, item_id, str(source_path), title, mcp_manager))
    return (
        f"PDF '{title}' is being processed in the background (stash item #{item_id}). "
        f"It will appear in the stash when ready. You can keep talking to me in the meantime."
    )


def list_reader_documents(args: dict, session=None, _mcp_manager=None) -> str:
    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    status = args.get("status")
    if status == "all":
        status = None
    limit = max(1, min(int(args.get("limit", 20)), 50))

    docs = list_documents(conn, limit=limit, status=status)
    if not docs:
        suffix = f" (status={status})" if status else ""
        return f"No reader documents found{suffix}."

    header = f"Reader documents{' (status=' + status + ')' if status else ''}, showing {len(docs)}:"
    lines = [header]
    for doc in docs:
        age = _format_age(doc.get("created_at"))
        title = doc.get("title") or "(untitled)"
        lines.append(
            f"#{doc['id']} [{doc['status']}/{doc['source_type']}] ({age}) {title}"
        )
        if doc.get("status") == "failed" and doc.get("error"):
            err = doc["error"].splitlines()[0][:200]
            lines.append(f"    error: {err}")
    return "\n".join(lines)


async def run_pdf_processing(db_path: str | Path, item_id: int, file_path: str, title: str, mcp_manager=None):
    try:
        if mcp_manager is None:
            raise RuntimeError("MCP manager unavailable")

        result = await mcp_manager.call_tool("convert_pdf_to_md", {"file_path": file_path})
        job_match = re.search(r"Job ID:\s*(\S+)", result)
        if not job_match:
            _update_saved_item_content(db_path, item_id, f"{title} (failed)", f"PDF conversion failed: {result}")
            return

        job_id = job_match.group(1)
        log.info("Background PDF processing started: job %s for inbox item %d", job_id, item_id)
        for _ in range(120):
            await asyncio.sleep(5)
            poll_result = await mcp_manager.call_tool("get_conversion_result", {"job_id": job_id})
            lowered = poll_result.lower()
            if "still processing" in lowered or "not yet" in lowered:
                continue
            md_match = re.search(r"(/\S+\.md)", poll_result)
            if md_match:
                markdown = read_text_file(md_match.group(1))
                _update_saved_item_content(db_path, item_id, title, markdown)
                log.info("Background PDF processing complete: inbox item %d", item_id)
                return
            if "error" in lowered or "failed" in lowered:
                _update_saved_item_content(db_path, item_id, f"{title} (failed)", poll_result[:2000])
                return

        _update_saved_item_content(
            db_path,
            item_id,
            f"{title} (timed out)",
            "PDF conversion timed out after 10 minutes.",
        )
    except Exception as exc:
        log.exception("Background PDF processing failed for inbox item %d", item_id)
        _update_saved_item_content(db_path, item_id, f"{title} (failed)", f"Error: {exc}")
