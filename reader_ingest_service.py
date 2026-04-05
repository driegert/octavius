from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING
import re
from urllib.parse import urlparse

from db import connect_db
from reader_store import get_document, update_document
from reader_ingest_handlers import (
    ensure_pdf_path_for_processor as _ensure_pdf_path_for_processor_impl,
    extract_article_text as _extract_article_text_impl,
    get_trafilatura as _get_trafilatura_impl,
    ingest_document_task as _ingest_document_task,
    ingest_pdf_document,
    ingest_url_document as _ingest_url_document_impl,
    refine_title_from_html as _refine_title_from_html_impl,
    refine_title_from_web_page as _refine_title_from_web_page_impl,
    resolve_markdown_output as _resolve_markdown_output_impl,
    start_file_ingest,
    start_inbox_ingest,
    start_retry_task as _schedule_document_retry_task,
    start_text_ingest,
    start_url_ingest,
)

if TYPE_CHECKING:
    from mcp_manager import MCPManager

log = logging.getLogger(__name__)


class ReaderIngestError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _get_trafilatura():
    return _get_trafilatura_impl(ReaderIngestError)


def _extract_article_text(raw: str) -> str | None:
    trafilatura = _get_trafilatura()
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
    return extracted


def _refine_title_from_html(raw_html: str, title: str, fallback_name: str) -> str:
    trafilatura = _get_trafilatura()
    meta = trafilatura.extract_metadata(raw_html)
    if meta and meta.title and title in ("Untitled", fallback_name):
        refined = meta.title
        if meta.sitename:
            refined = f"{refined} ({meta.sitename})"
        return refined
    return title


def _refine_title_from_web_page(html: str, title: str, url: str) -> str:
    trafilatura = _get_trafilatura()
    meta = trafilatura.extract_metadata(html)
    if meta and meta.title:
        page_title = meta.title
    else:
        title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        page_title = title_match.group(1).strip() if title_match else None

    if not page_title:
        return title

    site_name = urlparse(url).netloc.replace("www.", "")
    if title not in ("Untitled", url.split("/")[-1], url):
        return title

    parts = re.split(r"\s*[|\-–—]\s*", page_title)
    if len(parts) >= 2:
        article_title = " — ".join(parts[:-1]).strip()
        site_suffix = parts[-1].strip()
        return f"{article_title} ({site_suffix})"
    return f"{page_title} ({site_name})"


def _ensure_pdf_path_for_processor(file_path: str | Path) -> Path:
    return _ensure_pdf_path_for_processor_impl(file_path)


def _resolve_markdown_output(md_path: str | Path) -> Path | None:
    return _resolve_markdown_output_impl(md_path)


async def start_reader_ingest(db_path: str | Path, mcp_manager: "MCPManager", body: dict) -> dict:
    db_path = Path(db_path)
    source = body.get("source", "file")
    title = body.get("title", "Untitled")
    path = body.get("path")
    saved_item_id = body.get("saved_item_id")
    text = body.get("text")

    if source == "inbox" and saved_item_id:
        return await start_inbox_ingest(db_path, saved_item_id, title, ReaderIngestError)

    if source == "text" and text:
        return await start_text_ingest(db_path, text, title)

    if source == "url" and (body.get("url") or path):
        url = body.get("url") or path
        return await start_url_ingest(db_path, mcp_manager, url, title, ReaderIngestError)

    if source == "file" and path:
        return await start_file_ingest(db_path, mcp_manager, path, title, ReaderIngestError)

    raise ReaderIngestError("Provide source + path/url, text, or saved_item_id")


async def retry_reader_document(db_path: str | Path, mcp_manager: "MCPManager", doc_id: int) -> dict:
    db_path = Path(db_path)
    with connect_db(db_path) as conn:
        doc = get_document(conn, doc_id)
        if not doc:
            raise ReaderIngestError("Document not found", status_code=404)
        if doc["status"] == "processing":
            raise ReaderIngestError("Document is already processing", status_code=409)
        update_document(
            conn,
            doc_id,
            status="processing",
            error=None,
            speech_file=None,
            chunk_count=0,
            last_chunk=0,
            last_sentence=0,
        )

    _schedule_document_retry_task(db_path, mcp_manager, doc, ReaderIngestError)
    return {"id": doc_id, "status": "processing"}


async def ingest_url_document(
    db_path: str | Path,
    mcp_manager: "MCPManager",
    doc_id: int,
    url: str,
    title: str,
):
    await _ingest_url_document_impl(db_path, mcp_manager, doc_id, url, title, ReaderIngestError)
