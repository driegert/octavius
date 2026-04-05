from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from db import connect_db
from reader_ingest_handlers import (
    start_file_ingest,
    start_inbox_ingest,
    start_retry_task,
    start_text_ingest,
    start_url_ingest,
)
from reader_store import get_document, update_document

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

    start_retry_task(db_path, mcp_manager, doc, ReaderIngestError)
    return {"id": doc_id, "status": "processing"}
