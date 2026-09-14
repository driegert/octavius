"""Streaming multipart upload handling for client media (Android app, etc).

Pure functions plus one async entrypoint (`save_upload`) so the streaming,
sanitizing, and cap-enforcement logic is unit-testable without spinning up
FastAPI. `routes/media.py` stays a thin adapter over this module: parse the
multipart form, call `save_upload`, translate `MediaUploadError` to a JSON
response.

This is the upload half of the "client media" contract
(`docs/ws-media-contract.md`): a non-sidecar client (the Android app) POSTs
bytes here first, then sends the existing `image_input`/`file_input` WS frame
with the returned `path`/`mime`/`filename`/`size_bytes` verbatim. The Matrix
sidecar does not use this — it spools attachments itself and sends the WS
frames directly.

Starlette's form parser reads the whole multipart body into its own spooled
temp file before `save_upload` ever runs, so the size caps enforced here are
logical caps applied while copying out of that temp file, not an ingress
limit — by the time we can reject an oversize file, it has already been
received once. `routes/media.py` adds a `Content-Length`-based fast reject
before parsing starts as the only pre-receipt bound; a real ingress guard
would be a body-size limit on Caddy's `octavius.riegert.xyz` site block
(needs sudo; not done).
"""
from __future__ import annotations

import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Protocol

IMAGE_MAX_BYTES = 20 * 1024 * 1024
FILE_MAX_BYTES = 50 * 1024 * 1024

_CHUNK_SIZE = 1024 * 1024
_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_NAME_LEN = 80


class MediaUploadError(Exception):
    """A client-facing upload failure, carrying the HTTP status to return."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class _ReadableUpload(Protocol):
    """The slice of `fastapi.UploadFile` this module actually needs.

    Kept as a Protocol (rather than importing UploadFile) so tests can pass a
    trivial fake without dragging FastAPI into unit tests of pure logic.
    """

    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...

    async def close(self) -> None: ...


def sanitize_filename(name: str | None) -> str:
    """Reduce a client-supplied filename to a safe basename for disk.

    `Path(name).name` drops any directory component first, so
    `../../etc/passwd` yields `passwd` before sanitization even runs — the
    client's name is never trusted for path construction. What's left is
    reduced to `[A-Za-z0-9._-]` (everything else collapses to `_`), capped at
    80 chars, and defaults to "upload" if nothing usable survives.
    """
    base = Path(name or "").name
    cleaned = _SANITIZE_RE.sub("_", base).strip("._")
    cleaned = cleaned[:_MAX_NAME_LEN]
    return cleaned or "upload"


def resolve_mime(content_type: str | None, filename: str) -> str:
    """The part's Content-Type if present and specific, else a guess from the name.

    `application/octet-stream` is treated as "the client didn't really know"
    (it's the generic fallback most HTTP clients send for anything unrecognized)
    and falls through to `mimetypes.guess_type`; an unresolvable extension
    still lands on `application/octet-stream`.
    """
    if content_type and content_type != "application/octet-stream":
        return content_type
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _cap_for(mime: str) -> tuple[int, str]:
    if mime.startswith("image/"):
        return IMAGE_MAX_BYTES, "max 20 MB for images"
    return FILE_MAX_BYTES, "max 50 MB"


def resolve_spooled_media(path: str, roots: list[str | Path]) -> Path | None:
    """Validate a WS media frame's `path` against a directory allowlist.

    `image_input`/`file_input` frames name a file already on disk (spooled by
    the Matrix sidecar, or previously returned by `save_upload`); the path is
    frame-supplied and not otherwise trusted, so a bare existence check would
    let a crafted frame reference any file the process can read. This resolves
    it and confirms it lands under one of `roots`.

    Rejects an empty path outright. Resolves with `strict=True` — symlinks are
    followed and a dangling path raises — BEFORE the containment check runs,
    so a symlink inside an allowed root that points outside it is caught
    rather than passing containment on its unresolved location. Each root is
    also resolved (and skipped, not fatal, if it doesn't exist), so a root
    that is itself a symlink still matches correctly. Requires the resolved
    path to be a regular file. Returns the resolved `Path` on success, `None`
    on any rejection — callers log and send the existing "couldn't find the
    file" status rather than distinguishing rejection reasons to the user.
    """
    if not path:
        return None
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file():
        return None
    for root in roots:
        try:
            resolved_root = Path(root).resolve(strict=True)
        except OSError:
            continue
        if resolved.is_relative_to(resolved_root):
            return resolved
    return None


async def save_upload(upload: _ReadableUpload, dest_dir: Path) -> dict:
    """Stream `upload` into `dest_dir`, enforcing size caps, and return the API dict.

    Streams in chunks (never buffers the whole body) to a temp file in
    `dest_dir`, then `os.replace`s it into place under the final
    `<12-hex>-<sanitized-name>` filename. Raises `MediaUploadError` on any
    client-facing failure (400 empty body, 413 over cap) and always cleans up
    the temp file first — a 413 must never leave a partial file behind.

    The cap enforced here is logical, not an ingress limit: by the time this
    function sees `upload`, Starlette's form parser has already received the
    entire body into its own spooled temp file. This only stops an oversize
    upload from being copied a second time onto our disk under our filename.
    """
    dest_dir = Path(dest_dir)
    existed = dest_dir.exists()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not existed:
        try:
            os.chmod(dest_dir, 0o755)
        except OSError:
            pass

    original_name = upload.filename or "upload"
    mime = resolve_mime(upload.content_type, original_name)
    cap, cap_label = _cap_for(mime)

    safe_name = sanitize_filename(original_name)
    final_name = f"{uuid.uuid4().hex[:12]}-{safe_name}"
    final_path = dest_dir / final_name
    tmp_path = dest_dir / f".{final_name}.part"

    size = 0
    try:
        with open(tmp_path, "wb") as fh:
            while True:
                chunk = await upload.read(_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > cap:
                    raise MediaUploadError(413, f"File too large ({cap_label})")
                fh.write(chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

    if size == 0:
        tmp_path.unlink(missing_ok=True)
        raise MediaUploadError(400, "Empty file")

    os.chmod(tmp_path, 0o644)
    os.replace(tmp_path, final_path)

    return {
        "path": str(final_path.resolve()),
        "mime": mime,
        "filename": original_name,
        "size_bytes": size,
    }
