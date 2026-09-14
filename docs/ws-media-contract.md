# WebSocket media contract (v1) — frozen

This is the frozen contract between the Rust Matrix sidecar
(`matrix-agent-sidecar`) and Octavius's `/ws` endpoint for image and file
turns. Both repos implement against this document; changing it requires
updating both sides. Recorded verbatim from the spec both implementations
were built against.

## New frames from sidecar, same session/threading semantics as `text_input`

- `{"type":"image_input","text":"<caption or ''>","path":"<abs path>","mime":"image/jpeg","filename":"<original>","size_bytes":N}`
- `{"type":"file_input","text":"<caption or ''>","path":"<abs path>","mime":"application/pdf","filename":"<original>","size_bytes":N}`

## Spool directory

`/media/extra_stuff/octavius/matrix_media/` — the sidecar writes files
`0644`; Octavius only reads.

## Caps

Enforced sidecar-side (images <= 20MB, files <= 50MB). The sidecar degrades
oversize-file failures to descriptive `text_input` frames, so Octavius never
sees oversize media.

## Robustness

Octavius must ignore/log unknown frame fields gracefully.

## Octavius-side implementation notes (not part of the frozen contract, but
recorded here for context)

- `image_input` -> `WebSocketSessionHandler.handle_image_input`
  (`websocket_session.py`): validates the path exists and `mime` starts with
  `image/`, base64-reads the spool file, and builds an OpenAI-style
  multimodal content array for the turn. The turn is routed through
  `settings.vision_llm_chain` (see `agent.py`'s `use_vision` handling in
  `stream_agent_turn`) instead of the default `llm_chain`. Vision routing is
  STICKY per thread: once a conversation has carried an image
  (`Conversation.has_images`), the rest of that thread stays on the vision
  chain and the image content array stays in the in-memory conversation so
  follow-up turns can still reference the image (payload growth bounded by
  the normal trim window; llama.cpp's prefix cache absorbs re-sent image
  tokens). On thread re-attach after an idle drop, image turns are
  re-hydrated from the spool file recorded in the `attachments` table when
  the file still exists (most recent few only), else they silently degrade
  to the text placeholder. Persisted history and the memory extractor only
  ever see the placeholder/caption text, never base64.
- `file_input` with `mime=application/pdf` -> `WebSocketSessionHandler.handle_file_input`
  deterministically (plain code, not an LLM tool call) submits the PDF via
  the already-registered `document-processing` MCP server (`docproc_client.py`
  drives `convert_pdf_to_md` / `get_conversion_result` through
  `MCPManager.call_tool`; the mcp-tools wrapper scp-uploads to lilripper,
  converts remotely, and downloads the .md back to local paths). If the
  caption is non-empty it's treated as
  instructions: Octavius polls for completion in a background task (bounded,
  does not block the WS loop or other sessions) and then runs an agent turn
  with the instructions plus the converted markdown (inlined if it fits
  `OCTAVIUS_DOCPROC_INLINE_CHAR_BUDGET`, else the path plus a head excerpt).
  If the caption is empty, Octavius acknowledges immediately and mentions the
  docproc job id. The `check_document_status` local tool lets the model poll
  status / fetch the markdown path for a job id later in the conversation.
- `file_input` with any other `mime` gets a brief acknowledgement turn (no
  docproc call): Octavius can only process PDFs so far.

## Client uploads (2026-09-13)

Not part of the frozen frame contract above — this is how a client that isn't
the Matrix sidecar (today, the Android app) gets a file onto the server
*before* sending the frames described above. The sidecar spools attachments
itself and never calls this.

`POST /api/media/upload`, multipart with one `file` part, streamed into
`settings.media_upload_dir` under a sanitized `<12-hex>-<name>` filename (20 MB
cap for images, 50 MB otherwise; 413 over cap, 400 on an empty/missing part).
These caps are logical, not an ingress limit — Starlette's multipart parser
has already received the whole body into its own spooled temp file before the
handler enforces them — so `routes/media.py` also rejects on `Content-Length`
before parsing starts, as the only pre-receipt bound. See `routes/media.py` /
`media_uploads.py`. The response's `path`, `mime`, `filename`, and
`size_bytes` are exactly the four fields the client then puts into an
`image_input`/`file_input` frame above. Those handlers don't take the frame's
`path`/`size_bytes` on faith, though: `path` must resolve under the
`OCTAVIUS_MEDIA_SPOOL_DIRS` allowlist (`media_uploads.resolve_spooled_media`),
and `handle_image_input` re-checks the actual file size against
`IMAGE_MAX_BYTES` — see AGENTS.md's "Media turns" section for the details and
the accompanying `audio_done`-on-rejection rule.
