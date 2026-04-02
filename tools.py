"""Local tools that don't need an MCP server."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse, unquote

import httpx

from config import DOWNLOADS_DIR
from history import save_item
import reader

if TYPE_CHECKING:
    from history import ConversationSession

log = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(DOWNLOADS_DIR)

# Tool definition in OpenAI function-calling format
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": (
                "Download a file from a URL to local storage. "
                "Useful for fetching PDFs, documents, or other files that can "
                "then be processed with other tools (e.g., convert_pdf_to_md). "
                "Returns the local file path on success."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the file to download.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional filename to save as. If not provided, inferred from the URL.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_to_inbox",
            "description": (
                "Save content to Dave's knowledge inbox for later review. "
                "Use for: saving search summaries, article content, freeform notes, "
                "or email drafts that Dave wants to review or act on later."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short descriptive title for the saved item.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full content to save.",
                    },
                    "item_type": {
                        "type": "string",
                        "enum": ["note", "search_summary", "article", "email_draft"],
                        "description": "Type of content being saved.",
                    },
                    "source_url": {
                        "type": "string",
                        "description": "Source URL if applicable.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Type-specific data. For email_draft: {to, subject, in_reply_to}.",
                    },
                },
                "required": ["title", "content", "item_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": (
                "Start the document reader for a PDF, markdown file, or article. "
                "Ingests the document, converts math expressions to speech-friendly text, "
                "and prepares it for audio playback in the reader UI at /reader."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path to the document (PDF or markdown).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the document.",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


def _safe_filename(url: str, filename: str | None) -> str:
    """Derive a safe filename from URL or explicit name."""
    if filename:
        # Strip path separators for safety
        return Path(filename).name

    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    if not name or name == "/":
        name = "download"
    # Remove query strings from filename
    name = re.sub(r'[?#].*', '', name)
    return name


async def call_tool(
    name: str,
    arguments: dict,
    history_session: ConversationSession | None = None,
) -> str:
    """Execute a local tool by name. Returns result string."""
    if name == "download_file":
        return await _download_file(arguments)
    if name == "save_to_inbox":
        return _save_to_inbox(arguments, history_session)
    if name == "read_document":
        return await _read_document(arguments, history_session)
    return f"Error: unknown local tool '{name}'"


def _save_to_inbox(args: dict, session: ConversationSession | None) -> str:
    title = args.get("title", "")
    content = args.get("content", "")
    item_type = args.get("item_type", "note")
    if not title or not content:
        return "Error: title and content are required."

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    conversation_id = session.conv_id if session else None
    item_id = save_item(
        conn=conn,
        item_type=item_type,
        title=title,
        content=content,
        conversation_id=conversation_id,
        source_url=args.get("source_url"),
        metadata=args.get("metadata"),
    )
    return f"Saved to inbox (item #{item_id}): {title}"


async def _download_file(args: dict) -> str:
    url = args.get("url", "")
    if not url:
        return "Error: url is required."

    filename = _safe_filename(url, args.get("filename"))
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / filename

    # Avoid overwriting — append a number if file exists
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        i = 1
        while dest.exists():
            dest = DOWNLOAD_DIR / f"{stem}_{i}{suffix}"
            i += 1

    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            size_kb = len(resp.content) / 1024
            return f"Downloaded to {dest} ({size_kb:.0f} KB)"
    except Exception as e:
        log.exception("Download failed: %s", url)
        return f"Error downloading {url}: {e}"


async def _read_document(args: dict, session: ConversationSession | None) -> str:
    import asyncio

    path = args.get("path", "")
    if not path:
        return "Error: path is required."

    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"

    title = args.get("title", p.stem)
    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    if p.suffix.lower() == ".pdf":
        doc_id = reader.create_document(conn, title, "pdf", source_path=path)
        # PDF ingest is handled by main.py's _ingest_pdf — just create the record
        # and let the user know to check /reader
        return (
            f"Document '{title}' has been queued for processing (document #{doc_id}). "
            f"Since it's a PDF, it needs to be converted to text first, which takes a few minutes. "
            f"You can check the reader at /reader when it's ready."
        )
    else:
        markdown = p.read_text()
        doc_id = reader.create_document(conn, title, "markdown", source_path=path)
        asyncio.create_task(reader.ingest_document(conn, doc_id, markdown, title, original_md_path=path))
        return (
            f"Document '{title}' is being prepared for reading (document #{doc_id}). "
            f"It will be available at /reader in a minute or two."
        )
