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
    {
        "type": "function",
        "function": {
            "name": "read_item_content",
            "description": (
                "Read a chunk of content from a saved inbox item. Use this to access "
                "the full content of an item you're discussing with Dave. Returns the "
                "content from the given offset with the specified character limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "integer",
                        "description": "The inbox item ID to read from.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Character offset to start reading from. Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum characters to return. Default 4000.",
                    },
                },
                "required": ["item_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_pdf",
            "description": (
                "Convert a PDF to markdown in the background. Returns immediately — "
                "the result will be saved to Dave's knowledge inbox when processing "
                "completes. Use this instead of convert_pdf_to_md for a non-blocking "
                "experience."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the PDF file to process.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Title for the inbox item.",
                    },
                },
                "required": ["file_path"],
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
    if name == "read_item_content":
        return _read_item_content(arguments, history_session)
    if name == "read_document":
        return await _read_document(arguments, history_session)
    if name == "process_pdf":
        return await _process_pdf_background(arguments, history_session)
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


def _read_item_content(args: dict, session: ConversationSession | None) -> str:
    from history import get_saved_item

    item_id = args.get("item_id")
    if not item_id:
        return "Error: item_id is required."

    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    item = get_saved_item(conn, item_id)
    if not item:
        return f"Error: inbox item {item_id} not found."

    content = item.get("content", "")
    offset = args.get("offset", 0)
    limit = args.get("limit", 4000)

    chunk = content[offset:offset + limit]
    total_len = len(content)
    remaining = max(0, total_len - offset - limit)

    return (
        f"[Item #{item_id}: {total_len} chars total, showing {offset}-{offset + len(chunk)}]"
        f"\n\n{chunk}"
        + (f"\n\n[{remaining} more characters available — use offset={offset + limit} to continue]"
           if remaining > 0 else "\n\n[End of content]")
    )


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


async def _process_pdf_background(args: dict, session: ConversationSession | None) -> str:
    """Kick off PDF processing in the background and return immediately.

    The result is saved to the knowledge inbox when processing completes.
    """
    import asyncio

    file_path = args.get("file_path", "")
    if not file_path:
        return "Error: file_path is required."

    p = Path(file_path)
    if not p.exists():
        return f"Error: file not found: {file_path}"
    if p.suffix.lower() != ".pdf":
        return f"Error: {file_path} is not a PDF file."

    title = args.get("title", p.stem)
    conn = session.conn if session else None
    if conn is None:
        return "Error: no database connection available."

    conv_id = session.conv_id if session else None

    # Save a placeholder inbox item
    item_id = save_item(
        conn=conn,
        item_type="article",
        title=f"{title} (processing...)",
        content=f"PDF is being converted to text. Source: {file_path}",
        conversation_id=conv_id,
    )

    # Kick off background task
    asyncio.create_task(_run_pdf_processing(conn, item_id, file_path, title))

    return (
        f"PDF '{title}' is being processed in the background (inbox item #{item_id}). "
        f"It will appear in the knowledge inbox when ready. You can keep talking to me in the meantime."
    )


async def _run_pdf_processing(conn, item_id: int, file_path: str, title: str):
    """Background task: call MCP tools to convert PDF, then update inbox item."""
    import asyncio
    from history import update_saved_item_status

    try:
        # Import mcp_manager at runtime to avoid circular import
        from main import mcp_manager

        # Start conversion
        result = await mcp_manager.call_tool("convert_pdf_to_md", {"file_path": file_path})

        import re as _re
        job_match = _re.search(r'Job ID:\s*(\S+)', result)
        if not job_match:
            conn.execute(
                "UPDATE saved_items SET title = ?, content = ? WHERE id = ?",
                (f"{title} (failed)", f"PDF conversion failed: {result}", item_id),
            )
            conn.commit()
            return

        job_id = job_match.group(1)
        log.info("Background PDF processing started: job %s for inbox item %d", job_id, item_id)

        # Poll for completion
        for _ in range(120):  # up to 10 minutes
            await asyncio.sleep(5)
            poll_result = await mcp_manager.call_tool("get_conversion_result", {"job_id": job_id})

            if "still processing" in poll_result.lower() or "not yet" in poll_result.lower():
                continue

            md_match = _re.search(r'(/\S+\.md)', poll_result)
            if md_match:
                md_path = md_match.group(1)
                markdown = Path(md_path).read_text()
                # Update inbox item with the converted content
                conn.execute(
                    "UPDATE saved_items SET title = ?, content = ? WHERE id = ?",
                    (title, markdown, item_id),
                )
                conn.commit()
                log.info("Background PDF processing complete: inbox item %d", item_id)
                return

            if "error" in poll_result.lower() or "failed" in poll_result.lower():
                conn.execute(
                    "UPDATE saved_items SET title = ?, content = ? WHERE id = ?",
                    (f"{title} (failed)", poll_result[:2000], item_id),
                )
                conn.commit()
                return

        # Timeout
        conn.execute(
            "UPDATE saved_items SET title = ?, content = ? WHERE id = ?",
            (f"{title} (timed out)", "PDF conversion timed out after 10 minutes.", item_id),
        )
        conn.commit()

    except Exception as e:
        log.exception("Background PDF processing failed for inbox item %d", item_id)
        conn.execute(
            "UPDATE saved_items SET title = ?, content = ? WHERE id = ?",
            (f"{title} (failed)", f"Error: {e}", item_id),
        )
        conn.commit()
