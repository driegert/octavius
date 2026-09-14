"""Client media upload — `POST /api/media/upload`.

The non-sidecar-client half of the media contract (`docs/ws-media-contract.md`):
a client that isn't the Matrix sidecar (today, the Android app) POSTs a file
here first, then sends the existing frozen `image_input`/`file_input` WS frame
with this endpoint's response fields verbatim. The handler stays thin; the
streaming/sanitizing/cap logic lives in `media_uploads.py`.

Starlette's multipart parser reads the entire request body into its own
spooled temp file before this handler ever runs, so `media_uploads.save_upload`'s
size cap is logical (enforced while copying out of that temp file), not an
ingress limit. The `Content-Length` check below is the only pre-receipt
bound — a real ingress guard would be a body-size limit on Caddy's
`octavius.riegert.xyz` site block (needs sudo; not done).
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from media_uploads import FILE_MAX_BYTES, MediaUploadError, save_upload
from settings import settings

router = APIRouter()

# Module-level like reader_text.READER_PATH, and patched the same way in
# tests (`patch.object(media, "MEDIA_UPLOAD_DIR", ...)`).
MEDIA_UPLOAD_DIR = Path(settings.media_upload_dir)

# Reject on Content-Length alone before parsing starts. Larger than
# FILE_MAX_BYTES to leave room for multipart boundary/header overhead around
# the actual file part, so a file just under the real cap isn't rejected for
# its envelope.
_MAX_CONTENT_LENGTH = FILE_MAX_BYTES + 1024 * 1024


@router.post("/api/media/upload")
async def media_upload(request: Request):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_size = int(content_length)
        except ValueError:
            declared_size = None
        if declared_size is not None and declared_size > _MAX_CONTENT_LENGTH:
            return JSONResponse({"error": "File too large (max 50 MB)"}, status_code=413)

    try:
        # max_files/max_fields bound Starlette's own parsing work regardless
        # of our single expected `file` part; `save_upload` runs INSIDE the
        # block so the parsed form's SpooledTemporaryFile is closed by
        # __aexit__ before we return (previously left open, a ResourceWarning
        # under `-W error::ResourceWarning`).
        async with request.form(max_files=2, max_fields=4) as form:
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                return JSONResponse({"error": "Missing file"}, status_code=400)
            try:
                result = await save_upload(upload, MEDIA_UPLOAD_DIR)
            except MediaUploadError as exc:
                return JSONResponse({"error": exc.message}, status_code=exc.status)
            return JSONResponse(result)
    except MultiPartException as exc:
        # Raised directly when there's no ASGI "app" in scope (not our case
        # in production, but cheap insurance); Starlette normally converts
        # this to the HTTPException caught below.
        return JSONResponse({"error": exc.message}, status_code=400)
    except StarletteHTTPException as exc:
        # Starlette's Request._get_form wraps a MultiPartException (too many
        # files/fields, bad boundary, etc.) in this rather than letting it
        # propagate as a 500.
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse({"error": detail}, status_code=exc.status_code or 400)
