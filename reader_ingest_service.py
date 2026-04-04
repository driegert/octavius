from __future__ import annotations

import asyncio
import importlib
import logging
import re
import sqlite3
from typing import TYPE_CHECKING
from pathlib import Path
from urllib.parse import urlparse

import httpx
from config import DOWNLOADS_DIR
from document_sources import (
    decode_text_bytes,
    ensure_pdf_suffix,
    is_likely_html,
    is_pdf_file,
    is_pdf_response,
    read_text_file,
)
import reader

if TYPE_CHECKING:
    from mcp_manager import MCPManager

log = logging.getLogger(__name__)

PDF_POLL_INTERVAL_SECONDS = 5
PDF_POLL_MAX_ATTEMPTS = 120


class ReaderIngestError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _get_trafilatura():
    try:
        return importlib.import_module("trafilatura")
    except ModuleNotFoundError as exc:
        raise ReaderIngestError("trafilatura is required for HTML/article ingestion", status_code=500) from exc


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


def _downloads_dir() -> Path:
    download_dir = Path(DOWNLOADS_DIR)
    download_dir.mkdir(parents=True, exist_ok=True)
    return download_dir


def _ensure_pdf_path_for_processor(file_path: str | Path) -> Path:
    original = Path(file_path)
    if original.suffix.lower() == ".pdf":
        return original
    renamed = ensure_pdf_suffix(original)
    if not renamed.exists():
        original.rename(renamed)
    return renamed


def _resolve_markdown_output(md_path: str | Path) -> Path | None:
    candidate = Path(md_path)
    if candidate.exists():
        return candidate
    parent = candidate.parent
    if not parent.exists():
        return None

    md_files = sorted(parent.glob("*.md"))
    if not md_files:
        return None
    if len(md_files) == 1:
        return md_files[0]

    exact_stem_match = parent / f"{candidate.stem}.md"
    if exact_stem_match.exists():
        return exact_stem_match
    return md_files[0]


async def start_reader_ingest(conn: sqlite3.Connection, mcp_manager: "MCPManager", body: dict) -> dict:
    source = body.get("source", "file")
    title = body.get("title", "Untitled")
    path = body.get("path")
    saved_item_id = body.get("saved_item_id")
    text = body.get("text")

    if source == "inbox" and saved_item_id:
        from history import get_saved_item
        item = get_saved_item(conn, saved_item_id)
        if not item:
            raise ReaderIngestError("Inbox item not found", status_code=404)
        markdown = item["content"]
        title = title or item["title"]
        doc_id = reader.create_document(conn, title, "inbox_item", saved_item_id=saved_item_id)
        asyncio.create_task(reader.ingest_document(conn, doc_id, markdown, title))
        return {"id": doc_id, "status": "processing"}

    if source == "text" and text:
        doc_id = reader.create_document(conn, title, "markdown")
        asyncio.create_task(reader.ingest_document(conn, doc_id, text, title))
        return {"id": doc_id, "status": "processing"}

    if source == "url" and (body.get("url") or path):
        url = body.get("url") or path
        doc_id = reader.create_document(conn, title, "url", source_path=url)
        asyncio.create_task(ingest_url_document(conn, mcp_manager, doc_id, url, title))
        return {"id": doc_id, "status": "processing"}

    if source == "file" and path:
        file_path = Path(path)
        if not file_path.exists():
            raise ReaderIngestError(f"File not found: {path}", status_code=404)
        if file_path.suffix.lower() == ".pdf" or is_pdf_file(file_path):
            pdf_path = _ensure_pdf_path_for_processor(file_path)
            doc_id = reader.create_document(conn, title, "pdf", source_path=str(pdf_path))
            asyncio.create_task(ingest_pdf_document(conn, mcp_manager, doc_id, str(pdf_path), title))
            return {"id": doc_id, "status": "processing"}

        raw = read_text_file(file_path)
        markdown = raw
        if is_likely_html(raw):
            markdown = _extract_article_text(raw)
            if not markdown:
                raise ReaderIngestError("Could not extract article content")
            title = _refine_title_from_html(raw, title, file_path.name)

        doc_id = reader.create_document(conn, title, "markdown", source_path=path)
        asyncio.create_task(reader.ingest_document(conn, doc_id, markdown, title))
        return {"id": doc_id, "status": "processing"}

    raise ReaderIngestError("Provide source + path/url, text, or saved_item_id")


async def ingest_pdf_document(
    conn: sqlite3.Connection,
    mcp_manager: "MCPManager",
    doc_id: int,
    pdf_path: str,
    title: str,
):
    try:
        result = await mcp_manager.call_tool("convert_pdf_to_md", {"file_path": pdf_path})
        job_match = re.search(r"Job ID:\s*(\S+)", result)
        if not job_match:
            reader.update_document(conn, doc_id, status="failed", error=f"PDF conversion failed: {result}")
            return

        job_id = job_match.group(1)
        log.info("Reader: PDF conversion job %s started for document %d", job_id, doc_id)

        for _ in range(PDF_POLL_MAX_ATTEMPTS):
            await asyncio.sleep(PDF_POLL_INTERVAL_SECONDS)
            poll_result = await mcp_manager.call_tool("get_conversion_result", {"job_id": job_id})
            lowered = poll_result.lower()
            if "still processing" in lowered or "not yet" in lowered:
                continue
            md_match = re.search(r"(/\S+\.md)", poll_result)
            if md_match:
                md_path = md_match.group(1)
                resolved_md_path = _resolve_markdown_output(md_path)
                if not resolved_md_path:
                    reader.update_document(
                        conn,
                        doc_id,
                        status="failed",
                        error=f"Markdown output not found after conversion: {md_path}",
                    )
                    return
                markdown = read_text_file(resolved_md_path)
                await reader.ingest_document(
                    conn,
                    doc_id,
                    markdown,
                    title,
                    original_md_path=str(resolved_md_path),
                )
                return
            if "error" in lowered or "failed" in lowered:
                reader.update_document(conn, doc_id, status="failed", error=poll_result[:500])
                return

        reader.update_document(conn, doc_id, status="failed", error="PDF conversion timed out")
    except Exception as exc:
        log.exception("Reader: PDF ingest failed for document %d", doc_id)
        reader.update_document(conn, doc_id, status="failed", error=str(exc))


async def ingest_url_document(
    conn: sqlite3.Connection,
    mcp_manager: "MCPManager",
    doc_id: int,
    url: str,
    title: str,
):
    try:
        log.info("Reader: downloading URL for document %d: %s", doc_id, url)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        content_disposition = resp.headers.get("content-disposition", "")
        is_pdf = is_pdf_response(url, content_type, resp.content, content_disposition)

        dest_dir = _downloads_dir()
        ext = ".pdf" if is_pdf else ".md"
        dest = dest_dir / f"reader_{doc_id}{ext}"
        dest.write_bytes(resp.content)
        log.info("Reader: downloaded %s (%d KB)", dest, len(resp.content) // 1024)

        if is_pdf:
            await ingest_pdf_document(conn, mcp_manager, doc_id, str(dest), title)
            return

        html = decode_text_bytes(resp.content)
        title = _refine_title_from_web_page(html, title, url)
        if title:
            reader.update_document(conn, doc_id, title=title)

        extracted = _extract_article_text(html)
        if not extracted:
            reader.update_document(conn, doc_id, status="failed", error="Could not extract article content from page")
            return

        dest_txt = dest_dir / f"reader_{doc_id}.txt"
        dest_txt.write_text(extracted)
        log.info("Reader: extracted %d chars of article text from %s", len(extracted), url)
        await reader.ingest_document(conn, doc_id, extracted, title, original_md_path=str(dest_txt))
    except Exception as exc:
        log.exception("Reader: URL ingest failed for document %d", doc_id)
        reader.update_document(conn, doc_id, status="failed", error=str(exc))
