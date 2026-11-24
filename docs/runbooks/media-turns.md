# Media turns (images and files)

Read this before changing `image_input`/`file_input`, the upload endpoint, the media spools, PDF conversion, or vision turns. Wire contract (frozen, shared with `matrix-agent-sidecar`): `docs/ws-media-contract.md`. Code: `ws_media.py`, `media_uploads.py`, `routes/media.py`, `docproc_client.py`.

## Where files come from

- **Matrix**: the sidecar (`../matrix-agent-sidecar`) spools attachments to `/media/extra_stuff/octavius/matrix_media/` and sends `image_input` / `file_input`. Matrix audio and video never reach Octavius: the sidecar degrades them to descriptive `text_input` frames.
- **Clients without a spool** (Android, `/reader`'s file picker): `POST /api/media/upload`, multipart with one `file` part, streamed in chunks into `settings.media_upload_dir` (`OCTAVIUS_MEDIA_UPLOAD_DIR`, default `/media/extra_stuff/octavius/client_media/`) as `<12-hex>-<name>`. Caps 20 MB image / 50 MB other (413, partial file removed); 400 on an empty or missing part; `max_files=2, max_fields=4`; Starlette `MultiPartException`/`HTTPException` become 400 JSON. The response (`path`/`mime`/`filename`/`size_bytes`) is sent verbatim in the WS frame.
- **The caps are logical, not an ingress limit**: Starlette has received the whole body into its own temp file before `save_upload` runs. The only pre-receipt bound is a `Content-Length` fast-413; the real guard is Caddy's `request_body { max_size 52MB }` in the `octavius.riegert.xyz` block of `/etc/caddy/Caddyfile`.
- **Frames are not trusted**: `path` is resolved against `OCTAVIUS_MEDIA_SPOOL_DIRS` (default: Matrix spool + upload dir) by `media_uploads.resolve_spooled_media`, symlinks resolved before the containment check; `handle_image_input` re-stats the file against `IMAGE_MAX_BYTES` rather than trusting `size_bytes`. Every rejection also sends `status: audio_done`.
- **Retention**: neither spool cleans itself. `scripts/octavius-media-gc` sweeps both (45-day mtime, dry run unless `--apply`; daily via `deploy/octavius-media-gc.{service,timer}`, not installed by default). A re-attached thread whose image was swept loses the pixels (`_rehydrate_image_content` falls back to the placeholder).

## Images

`handle_image_input` base64-reads the file into an OpenAI-style multimodal content array, and the turn routes through `settings.vision_llm_chain` (`agent.py` `use_vision`; chain and the ubatch rule in [model-routing.md](model-routing.md)). Vision routing is **sticky per thread** (`Conversation.has_images`, until reset/load): the image stays in the in-memory conversation so follow-ups see the pixels; llama.cpp's prefix cache absorbs the re-sent tokens and `Conversation.trim()` bounds growth. On re-attach, images re-hydrate from the spool via the `attachments` table when the file still exists. Persisted history and the memory extractor only see placeholder/caption text, never base64. `Conversation.replace_last_user_content` (downgrade-to-placeholder) is tests-only, from a dropped design.

## PDFs and other files

- `file_input` with `mime=application/pdf` is submitted to docproc deterministically (code, not an LLM tool call) through the `document-processing` MCP server: mcp-tools' wrapper scp's to lilripper, converts at `lilripper:8251/mcp`, downloads the `.md` back. `docproc_client.py` wraps `convert_pdf_to_md` / `get_conversion_result` via `MCPManager.call_tool`; pacing `OCTAVIUS_DOCPROC_POLL_INTERVAL` / `_TIMEOUT`.
- A non-empty caption is treated as instructions: a background task (bounded, not blocking the WS loop) polls the job, then runs an agent turn with the instructions plus the markdown (inlined under `OCTAVIUS_DOCPROC_INLINE_CHAR_BUDGET`, else path + head excerpt). An empty caption gets an immediate acknowledgement with the docproc job id.
- `check_document_status` (local tool) checks a job id later. Job ids are in-process wrapper state and **do not survive a restart** — an old id reports unknown.
- Outputs normally land next to the source PDF; when its dir is read-only for the service user (the Matrix spool, owned by `octavius-matrix`), the wrapper falls back to `~/docproc-output/<stem>-<path-hash>/` and globs for the actual `.md` (dedup hits carry the original upload's stem).
- Non-PDF files get a brief acknowledgement only.
- Both flows record an `attachments` row (`type` `image`/`file`) against the persisted user message.

Tests: `tests/test_ws_media.py`, `tests/test_media_upload.py`, `tests/test_media_gc.py`, `tests/test_docproc_client.py`.
