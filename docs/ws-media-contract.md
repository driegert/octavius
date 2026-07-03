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
