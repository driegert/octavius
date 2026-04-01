"""Local tools that don't need an MCP server."""

import logging
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx

from config import DOWNLOADS_DIR

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


async def call_tool(name: str, arguments: dict) -> str:
    """Execute a local tool by name. Returns result string."""
    if name == "download_file":
        return await _download_file(arguments)
    return f"Error: unknown local tool '{name}'"


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
