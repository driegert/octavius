from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from settings import settings

log = logging.getLogger(__name__)
DOWNLOAD_DIR = Path(settings.downloads_dir)


def safe_filename(url: str, filename: str | None) -> str:
    if filename:
        return Path(filename).name

    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name)
    if not name or name == "/":
        name = "download"
    name = re.sub(r"[?#].*", "", name)
    if "/pdf/" in parsed.path and not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return name


async def download_file(args: dict, _session=None) -> str:
    url = args.get("url", "")
    if not url:
        return "Error: url is required."

    filename = safe_filename(url, args.get("filename"))
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / filename
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
    except Exception as exc:
        log.exception("Download failed: %s", url)
        return f"Error downloading {url}: {exc}"
