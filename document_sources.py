from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

TITLE_MAX_CHARS = 80


def is_pdf_bytes(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def derive_title_from_text(text: str, default: str = "Pasted Text") -> str:
    """Best-effort title for pasted text, which has no filename or URL to borrow.

    Takes the first markdown heading or first non-empty line, strips leading
    heading hashes and list markers, and truncates. Falls back to `default` for
    text that is empty or only punctuation.
    """
    for line in text.splitlines():
        stripped = re.sub(r"^\s*(#+|[-*+>]|\d+[.)])\s*", "", line).strip()
        if not stripped or not any(ch.isalnum() for ch in stripped):
            continue
        if len(stripped) > TITLE_MAX_CHARS:
            stripped = stripped[: TITLE_MAX_CHARS - 3].rstrip() + "..."
        return stripped
    return default


def is_likely_html(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith(("<!DOCTYPE", "<html", "<HTML"))


def is_pdf_response(url: str, content_type: str | None, content: bytes, content_disposition: str | None = None) -> bool:
    url_path = urlparse(url).path.lower()
    disposition = (content_disposition or "").lower()
    normalized_type = (content_type or "").lower()
    return (
        "pdf" in normalized_type
        or ".pdf" in url_path
        or ".pdf" in disposition
        or is_pdf_bytes(content)
    )


def decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_text_file(path: str | Path) -> str:
    return decode_text_bytes(Path(path).read_bytes())


def is_pdf_file(path: str | Path) -> bool:
    file_path = Path(path)
    try:
        return is_pdf_bytes(file_path.read_bytes()[:8])
    except OSError:
        return False


def ensure_pdf_suffix(path: str | Path) -> Path:
    file_path = Path(path)
    if file_path.suffix.lower() == ".pdf":
        return file_path
    return file_path.with_name(f"{file_path.name}.pdf")
