from __future__ import annotations

import asyncio
import importlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from db import connect_db
from document_sources import (
    decode_text_bytes,
    ensure_pdf_suffix,
    is_likely_html,
    is_pdf_file,
    is_pdf_response,
    read_text_file,
)
from reader_store import create_document, update_document
from reader_text import ingest_document
from settings import settings

if TYPE_CHECKING:
    from mcp_manager import MCPManager
    from reader_ingest_service import ReaderIngestError

log = logging.getLogger(__name__)

PDF_POLL_INTERVAL_SECONDS = 5
PDF_POLL_MAX_ATTEMPTS = 120


def get_trafilatura(reader_ingest_error_type: type["ReaderIngestError"]):
    try:
        return importlib.import_module("trafilatura")
    except ModuleNotFoundError as exc:
        raise reader_ingest_error_type(
            "trafilatura is required for HTML/article ingestion",
            status_code=500,
        ) from exc


def extract_article_text(raw: str, reader_ingest_error_type: type["ReaderIngestError"]) -> str | None:
    trafilatura = get_trafilatura(reader_ingest_error_type)
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


def refine_title_from_html(
    raw_html: str,
    title: str,
    fallback_name: str,
    reader_ingest_error_type: type["ReaderIngestError"],
) -> str:
    trafilatura = get_trafilatura(reader_ingest_error_type)
    meta = trafilatura.extract_metadata(raw_html)
    if meta and meta.title and title in ("Untitled", fallback_name):
        refined = meta.title
        if meta.sitename:
            refined = f"{refined} ({meta.sitename})"
        return refined
    return title


def refine_title_from_web_page(
    html: str,
    title: str,
    url: str,
    reader_ingest_error_type: type["ReaderIngestError"],
) -> str:
    trafilatura = get_trafilatura(reader_ingest_error_type)
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


def downloads_dir() -> Path:
    download_dir = Path(settings.downloads_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    return download_dir


def ensure_pdf_path_for_processor(file_path: str | Path) -> Path:
    original = Path(file_path)
    if original.suffix.lower() == ".pdf":
        return original
    renamed = ensure_pdf_suffix(original)
    if not renamed.exists():
        original.rename(renamed)
    return renamed


def resolve_markdown_output(md_path: str | Path) -> Path | None:
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


async def ingest_document_task(
    db_path: str | Path,
    doc_id: int,
    markdown: str,
    title: str,
    original_md_path: str | None = None,
):
    with connect_db(Path(db_path)) as conn:
        await ingest_document(conn, doc_id, markdown, title, original_md_path=original_md_path)


async def ingest_pdf_document(
    db_path: str | Path,
    mcp_manager: "MCPManager",
    doc_id: int,
    pdf_path: str,
    title: str,
):
    db_path = Path(db_path)
    try:
        result = await mcp_manager.call_tool("convert_pdf_to_md", {"file_path": pdf_path})
        job_match = re.search(r"Job ID:\s*(\S+)", result)
        if not job_match:
            with connect_db(db_path) as conn:
                update_document(conn, doc_id, status="failed", error=f"PDF conversion failed: {result}")
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
                resolved_md_path = resolve_markdown_output(md_path)
                if not resolved_md_path:
                    with connect_db(db_path) as conn:
                        update_document(
                            conn,
                            doc_id,
                            status="failed",
                            error=f"Markdown output not found after conversion: {md_path}",
                        )
                    return
                markdown = read_text_file(resolved_md_path)
                await ingest_document_task(
                    db_path,
                    doc_id,
                    markdown,
                    title,
                    original_md_path=str(resolved_md_path),
                )
                return
            if "error" in lowered or "failed" in lowered:
                with connect_db(db_path) as conn:
                    update_document(conn, doc_id, status="failed", error=poll_result[:500])
                return

        with connect_db(db_path) as conn:
            update_document(conn, doc_id, status="failed", error="PDF conversion timed out")
    except Exception as exc:
        log.exception("Reader: PDF ingest failed for document %d", doc_id)
        with connect_db(db_path) as conn:
            update_document(conn, doc_id, status="failed", error=str(exc))


async def ingest_url_document(
    db_path: str | Path,
    mcp_manager: "MCPManager",
    doc_id: int,
    url: str,
    title: str,
    reader_ingest_error_type: type["ReaderIngestError"],
):
    db_path = Path(db_path)
    try:
        log.info("Reader: downloading URL for document %d: %s", doc_id, url)
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        content_disposition = resp.headers.get("content-disposition", "")
        is_pdf = is_pdf_response(url, content_type, resp.content, content_disposition)

        dest_dir = downloads_dir()
        ext = ".pdf" if is_pdf else ".md"
        dest = dest_dir / f"reader_{doc_id}{ext}"
        dest.write_bytes(resp.content)
        log.info("Reader: downloaded %s (%d KB)", dest, len(resp.content) // 1024)

        if is_pdf:
            await ingest_pdf_document(db_path, mcp_manager, doc_id, str(dest), title)
            return

        html = decode_text_bytes(resp.content)
        title = refine_title_from_web_page(html, title, url, reader_ingest_error_type)
        if title:
            with connect_db(db_path) as conn:
                update_document(conn, doc_id, title=title)

        extracted = extract_article_text(html, reader_ingest_error_type)
        if not extracted:
            with connect_db(db_path) as conn:
                update_document(conn, doc_id, status="failed", error="Could not extract article content from page")
            return

        dest_txt = dest_dir / f"reader_{doc_id}.txt"
        dest_txt.write_text(extracted)
        log.info("Reader: extracted %d chars of article text from %s", len(extracted), url)
        await ingest_document_task(db_path, doc_id, extracted, title, original_md_path=str(dest_txt))
    except Exception as exc:
        log.exception("Reader: URL ingest failed for document %d", doc_id)
        with connect_db(db_path) as conn:
            update_document(conn, doc_id, status="failed", error=str(exc))


def start_retry_task(
    db_path: Path,
    mcp_manager: "MCPManager",
    doc: dict,
    reader_ingest_error_type: type["ReaderIngestError"],
):
    doc_id = doc["id"]
    title = doc["title"]
    source_type = doc["source_type"]
    source_path = doc.get("source_path")
    saved_item_id = doc.get("saved_item_id")

    if source_type == "pdf":
        if not source_path:
            raise reader_ingest_error_type("Cannot retry PDF document without source_path", status_code=400)
        asyncio.create_task(ingest_pdf_document(db_path, mcp_manager, doc_id, source_path, title))
        return

    if source_type == "url":
        if not source_path:
            raise reader_ingest_error_type("Cannot retry URL document without source_path", status_code=400)
        asyncio.create_task(ingest_url_document(db_path, mcp_manager, doc_id, source_path, title, reader_ingest_error_type))
        return

    if source_type == "inbox_item":
        if not saved_item_id:
            raise reader_ingest_error_type("Cannot retry inbox document without saved_item_id", status_code=400)
        with connect_db(db_path) as conn:
            from history import get_saved_item
            item = get_saved_item(conn, saved_item_id)
        if not item:
            raise reader_ingest_error_type("Inbox item not found", status_code=404)
        asyncio.create_task(ingest_document_task(db_path, doc_id, item["content"], title))
        return

    if source_type == "markdown":
        retry_path = source_path or doc.get("original_md_file")
        if not retry_path:
            raise reader_ingest_error_type("Cannot retry markdown document without a stored source path", status_code=400)
        retry_file = Path(retry_path)
        if not retry_file.exists():
            raise reader_ingest_error_type(f"Retry source file not found: {retry_path}", status_code=404)
        markdown = read_text_file(retry_file)
        asyncio.create_task(
            ingest_document_task(db_path, doc_id, markdown, title, original_md_path=str(retry_file))
        )
        return

    raise reader_ingest_error_type(f"Unsupported source_type for retry: {source_type}", status_code=400)


async def start_file_ingest(
    db_path: Path,
    mcp_manager: "MCPManager",
    path: str,
    title: str,
    reader_ingest_error_type: type["ReaderIngestError"],
) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        raise reader_ingest_error_type(f"File not found: {path}", status_code=404)

    if file_path.suffix.lower() == ".pdf" or is_pdf_file(file_path):
        pdf_path = ensure_pdf_path_for_processor(file_path)
        with connect_db(db_path) as conn:
            doc_id = create_document(conn, title, "pdf", source_path=str(pdf_path))
        asyncio.create_task(ingest_pdf_document(db_path, mcp_manager, doc_id, str(pdf_path), title))
        return {"id": doc_id, "status": "processing"}

    raw = read_text_file(file_path)
    markdown = raw
    if is_likely_html(raw):
        markdown = extract_article_text(raw, reader_ingest_error_type)
        if not markdown:
            raise reader_ingest_error_type("Could not extract article content")
        title = refine_title_from_html(raw, title, file_path.name, reader_ingest_error_type)

    with connect_db(db_path) as conn:
        doc_id = create_document(conn, title, "markdown", source_path=path)
    asyncio.create_task(ingest_document_task(db_path, doc_id, markdown, title))
    return {"id": doc_id, "status": "processing"}


async def start_url_ingest(
    db_path: Path,
    mcp_manager: "MCPManager",
    url: str,
    title: str,
    reader_ingest_error_type: type["ReaderIngestError"],
) -> dict:
    with connect_db(db_path) as conn:
        doc_id = create_document(conn, title, "url", source_path=url)
    asyncio.create_task(ingest_url_document(db_path, mcp_manager, doc_id, url, title, reader_ingest_error_type))
    return {"id": doc_id, "status": "processing"}


async def start_text_ingest(db_path: Path, text: str, title: str) -> dict:
    with connect_db(db_path) as conn:
        doc_id = create_document(conn, title, "markdown")
    asyncio.create_task(ingest_document_task(db_path, doc_id, text, title))
    return {"id": doc_id, "status": "processing"}


async def start_inbox_ingest(
    db_path: Path,
    saved_item_id: int,
    title: str,
    reader_ingest_error_type: type["ReaderIngestError"],
) -> dict:
    from history import get_saved_item

    with connect_db(db_path) as conn:
        item = get_saved_item(conn, saved_item_id)
    if not item:
        raise reader_ingest_error_type("Inbox item not found", status_code=404)

    markdown = item["content"]
    title = title or item["title"]
    with connect_db(db_path) as conn:
        doc_id = create_document(conn, title, "inbox_item", saved_item_id=saved_item_id)
    asyncio.create_task(ingest_document_task(db_path, doc_id, markdown, title))
    return {"id": doc_id, "status": "processing"}
