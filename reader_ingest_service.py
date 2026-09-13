from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from db import connect_db
from reader_ingest_handlers import (
    resolve_append_text,
    start_append_ingest,
    start_file_ingest,
    start_inbox_ingest,
    start_retry_task,
    start_text_ingest,
    start_url_ingest,
)
from reader_store import get_document, update_document
from reader_text import rename_document

if TYPE_CHECKING:
    from mcp_manager import MCPManager


class ReaderIngestError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def start_reader_ingest(db_path: str | Path, mcp_manager: "MCPManager", body: dict) -> dict:
    db_path = Path(db_path)
    source = body.get("source", "file")
    title = body.get("title", "Untitled")
    path = body.get("path")
    saved_item_id = body.get("saved_item_id")
    text = body.get("text")

    if source == "inbox" and saved_item_id:
        return await start_inbox_ingest(db_path, saved_item_id, title, ReaderIngestError)

    if source == "text":
        if not (text and text.strip()):
            raise ReaderIngestError("Text is empty")
        # Pass the raw title (not the "Untitled" default) so start_text_ingest
        # can derive one from the content when the caller didn't supply one.
        return await start_text_ingest(db_path, text, body.get("title"))

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

    start_retry_task(db_path, mcp_manager, doc, ReaderIngestError)
    return {"id": doc_id, "status": "processing"}


def _as_bool(value, field: str) -> bool:
    """Strict: tool calls sometimes serialise booleans as strings, and
    bool("false") is True — which would destructively replace a document."""
    if value is None or value is False or value in (0, "", "false", "False", "0", "no"):
        return False
    if value is True or value in (1, "true", "True", "1", "yes"):
        return True
    raise ReaderIngestError(f"{field} must be a boolean")


async def rename_reader_document(db_path: str | Path, doc_id: int, body: dict) -> dict:
    """Rename document `doc_id` to `body["title"]`. Allowed in any status."""
    if not isinstance(body, dict):
        raise ReaderIngestError("Expected a JSON object")
    title = body.get("title")
    if not isinstance(title, str):
        raise ReaderIngestError("Title is required")
    title = title.strip()
    if not title:
        raise ReaderIngestError("Title is empty")
    if len(title) > 200:
        raise ReaderIngestError("Title is too long (max 200 characters)")
    with connect_db(Path(db_path)) as conn:
        doc = get_document(conn, doc_id)
        if not doc:
            raise ReaderIngestError("Document not found", status_code=404)
        rename_document(conn, doc_id, title)
    return {"id": doc_id, "title": title}


async def append_reader_document(db_path: str | Path, doc_id: int, body: dict) -> dict:
    """Append `body["text"]` (or the contents of `body["path"]`) to document `doc_id`.
    `body["replace"]` rebuilds the document from the new content instead."""
    if not isinstance(body, dict):
        raise ReaderIngestError("Expected a JSON object")
    replace = _as_bool(body.get("replace"), "replace")
    text = resolve_append_text(body.get("text"), body.get("path"), ReaderIngestError)
    return await start_append_ingest(Path(db_path), doc_id, text, ReaderIngestError, replace=replace)
