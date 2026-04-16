# Octavius Status

This document holds change-oriented project status that is useful in the short to medium term:

- refactor progress
- current hotspots
- recent bug fixes that should not regress
- near-term design or implementation pressure

Keep durable architecture and contributor workflow in `CLAUDE.md`.

## Refactor Status

The codebase has been through a reliability and maintainability refactor focused on reducing orchestration-heavy modules and making external-service boundaries clearer.

Completed work:

- runtime settings moved to `settings.py` with env-backed defaults
- core STT, TTS, main LLM chat, summary-generation, and embedding HTTP integrations now live behind `service_clients.py`
- reader ingest orchestration extracted from `main.py` into `reader_ingest_service.py`
- WebSocket session and conversation handling extracted from `main.py` into `websocket_session.py`
- history responsibilities split across:
  - `history.py` for DB bootstrap, conversation/session recording, and compatibility re-exports
  - `history_enrichment.py` for embeddings, summaries, and tags
  - `history_store.py` for queries, inbox CRUD/search, and stats
- local tool responsibilities split across:
  - `tools.py` as the public entrypoint used by the agent loop
  - `local_tool_specs.py` for schemas
  - `local_tool_registry.py` for dispatch
  - `local_tool_downloads.py`, `local_tool_inbox.py`, and `local_tool_reader.py` for execution logic
- document source handling centralized in `document_sources.py`
- test coverage baseline added under `tests/` for the major subsystems
- request handlers and background reader jobs now use short-lived SQLite connections instead of sharing one app-wide connection
- route groups were split out of `main.py` into dedicated router modules for inbox, conversations, and reader APIs
- shared browser helpers were extracted into `static/app-common.js` to reduce duplicated WebSocket and voice-loading logic across inline pages
- the inbox and reader pages now load page-specific behavior from `static/inbox-app.js` and `static/reader-app.js` instead of keeping those scripts inline
- the main voice UI now loads page behavior from `static/index-app.js`, with streamed TTS queue and silence-trimming logic isolated in `static/index-audio.js`
- reader responsibilities were split across `reader_store.py`, `reader_text.py`, and `reader_playback.py`
- reader ingest entrypoints were narrowed in `reader_ingest_service.py`, with source-specific URL/PDF/file handling moved to `reader_ingest_handlers.py`
- local tool dispatch now routes through `tools.py` and `local_tool_registry.py`
- local tool execution was further split by domain into `local_tool_downloads.py`, `local_tool_inbox.py`, and `local_tool_reader.py`
- internal callers now use the concrete reader and local-tool modules directly; the old `reader.py`, `local_tool_handlers.py`, and `config.py` shims have been removed
- STT moved from batch record-then-transcribe to streaming partial transcription using faster-whisper on lilripper
- server-side Silero VAD added for automatic end-of-speech detection (1.5s silence threshold)
- continuous conversation mode added: hands-free multi-turn loop where the mic auto-reopens after TTS playback
- talk mode selector replaced the toggle-to-talk checkbox (hold / tap / continuous)

## Current Hotspots

These areas still carry the most complexity or coupling:

- `main.py` still owns startup wiring and top-level app composition, but the main REST route groups have been split into dedicated router modules
- reader ingest and playback are cleaner, but the overall reader flow still spans several modules and background-task boundaries
- frontend logic is now extracted into JS assets, but the UI still relies on large static HTML shells

## Reader And PDF Fixes

These behaviors were fixed recently and should not regress:

- local files are identified as PDFs by content, not only by `.pdf` suffix
- arXiv `/pdf/` downloads are saved with a `.pdf` suffix
- the `read_document` local tool now starts PDF conversion instead of only creating a DB row
- reader startup marks stale `reader_documents.status='processing'` rows as failed because in-memory jobs do not survive restart
- post-conversion markdown lookup is resilient to mismatched output filenames from the remote processor
- failed or interrupted reader documents can now be requeued from stored source metadata through the retry API/UI

## Stability Notes

Operational assumptions worth keeping in mind during debugging:

- external service reachability problems can look like application bugs if STT, TTS, LLM, or MCP endpoints are unavailable
- `/health` now exposes `alive`, `ready`, `degraded`, per-server MCP status, and `llm_chain` failover information, so degraded runtime behavior should be checked there first
- reader ingest jobs are in-memory background tasks and do not survive restart
- restart recovery is now manual requeue rather than automatic job resurrection
- live conversation and item-chat history sessions still keep their own dedicated SQLite connection until they are ended
- the browser UIs are less script-heavy than before, but layout and markup are still concentrated in large static HTML files
- Silero VAD requires `models/silero_vad.onnx` to be present; if the file is missing, VAD is skipped and auto-stop will not work
- STT failover (lilripper primary, lilbuddy fallback) is not yet implemented — switching requires a settings change

## Near-Term Work

Likely refactor targets, in rough priority order:

1. Further narrow `main.py` so it remains a routing layer rather than a coordination module.
2. Reduce the size of the remaining static HTML shells by extracting reusable frontend structure or templates.
3. Continue replacing coarse integration paths with narrower behavior-level tests where the boundary is now stable.

## Migration Note

- `reader_documents.saved_item_id` is still enforced as a plain foreign key. If inbox deletion should eventually null out that reference automatically, that will require a real SQLite migration to add `ON DELETE SET NULL`, not just a schema-file edit.

## Related Design Work

The Android companion app remains exploratory rather than committed implementation work. See `octavius-android-design.md` for that design thread.
