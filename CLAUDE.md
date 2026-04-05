# Octavius

Self-hosted voice assistant running on Dave's homelab. No cloud APIs; runtime traffic stays on the Tailnet.

## Purpose

This file is the high-signal working context for contributors and coding agents:

- what Octavius is
- how to run and validate it
- where the main responsibilities live
- which areas are still risky or incomplete

It is not intended to be a release log. Keep transient change notes brief and move longer design or roadmap material into separate docs.

## Runbook

Install dependencies:

```bash
uv sync
```

Run locally in the foreground:

```bash
uv run python main.py
```

Run via the normal user service:

```bash
systemctl --user start octavius
systemctl --user restart octavius
```

Service endpoint:

- FastAPI binds to `127.0.0.1:8030`
- Caddy exposes it at `https://octavius.riegert.xyz`

Primary UI routes:

- `/` main voice UI
- `/inbox` knowledge inbox
- `/reader` document reader

## Validation Workflow

Before or after backend changes:

```bash
python -m unittest discover -s tests
```

After changes to request routing, WebSocket behavior, reader flows, or inbox flows:

1. Start the app.
2. Open `/`, `/inbox`, and `/reader`.
3. Confirm the WebSocket connects from the browser.
4. Confirm inbox list/load/update still works.
5. Confirm reader document listing and ingest path still work.
6. Check `/health` and confirm `alive`, `ready`, and `degraded` match expectations.
7. Confirm the `llm_chain` section matches the expected endpoint order and current failover state.
8. If startup is degraded, inspect the `mcp.servers` section to see which MCP backends failed to connect.

When touching external-service boundaries, verify the configured endpoints are reachable before assuming an application bug.

## Architecture

High-level path:

```text
Browser (WebSocket) -> FastAPI app -> agent/session logic -> local tools + MCP tools + STT/TTS/LLM services
```

External services currently expected:

- **STT**: Whisper at `127.0.0.1:8502/api/transcribe`
- **LLM chain**: Qwen3.5-35B-A3B via `OCTAVIUS_LLM_CHAIN`, defaulting to:
  - primary: `lilripper:8020/v1/chat/completions`
  - first fallback: `127.0.0.1:8001/v1/chat/completions` on lilbuddy
  - second fallback: `triplestuffed:8010/v1/chat/completions`
- **TTS**: Voxtral 4B at `triplestuffed:8020/v1/audio/speech`
- **TTS fallback**: Kokoro at `lilbuddy:8880`
- **Reader LLM**: Qwen3.5-9B at `lilripper:8010/v1/chat/completions`
- **Summary/tag generation**: summary chain defaults to `127.0.0.1:8001/v1/chat/completions` with fallback `triplestuffed:8010/v1/chat/completions`
- **Embeddings**: Ollama at `workhorse:11434/api/embeddings`

Configured MCP servers:

- `evangeline-email`: streamable HTTP at `triplestuffed:8251/mcp`
- `searxng`: stdio subprocess via `uv`
- `openalex`: stdio subprocess via `npm`
- `vikunja-tasks`: streamable HTTP at `triplestuffed:8252/mcp`
- `document-processing`: local stdio wrapper around remote processing on `lilripper:8251/mcp`

## Key Runtime Behavior

- Each WebSocket connection gets its own `Conversation` instance.
- Conversation IDs are persisted in browser `localStorage` and restored with `restore_session`.
- The WebSocket carries both binary audio and JSON messages.
- The agent buffers sentences during tool-call rounds and only emits final spoken text when tool use is complete.
- Tool-call rounds are capped and nudged to stop around rounds 5-6 of 7.
- Tool results are truncated to 4000 characters to protect context budget.
- Qwen `<think>...</think>` output is stripped before user-visible text or TTS.
- Conversation history trims automatically to 40 messages.
- `/health` distinguishes `alive`, `ready`, and `degraded` states.
- `/health` exposes per-server MCP connection status plus `llm_chain` observability including configured endpoints, failover count, terminal failures, and the last successful endpoint.

WebSocket message families:

- Voice: `status`, `transcript`, `response`, `reset`, `restore_session`, `session_id`, `load_conversation`, `conversation_loaded`
- Reader: `reader_play`, `reader_pause`, `reader_stop`, `reader_position`, `reader_audio_done`
- Item chat: `item_chat`, `item_chat_load`, `item_chat_reset`, `item_chat_response`, `item_chat_loaded`, `item_chat_status`

## Code Map

Core runtime:

- `main.py` - FastAPI app creation, startup wiring, shared top-level routes, WebSocket entrypoint
- `db.py` - SQLite connection helpers and short-lived connection context manager
- `settings.py` - env-backed runtime settings and defaults
- `runtime.py` - shared runtime handle for MCP manager access
- `service_clients.py` - core HTTP clients for STT, TTS, the main LLM chat chain, summary generation, and embeddings
- `stt.py` - thin STT wrapper
- `tts.py` - thin TTS wrapper

Route modules:

- `routes/inbox.py` - inbox page and inbox REST API routes
- `routes/conversations.py` - conversation history API routes
- `routes/reader_api.py` - reader page and reader REST API routes

Conversation and tool loop:

- `conversation.py` - chat history state with trim/reset/load support
- `agent.py` - LLM loop, tool calling, output cleanup, tool-spiral prevention
- `websocket_session.py` - WebSocket session state, message dispatch, item-chat lifecycle, and STT/TTS turn handling
- `mcp_manager.py` - MCP client lifecycle, routing, truncation, reconnect handling
- `tools.py` - public local-tool entrypoint used by the agent loop
- `local_tool_specs.py` - local tool schemas
- `local_tool_registry.py` - local tool handler registry and dispatch
- `local_tool_downloads.py` - local download filename logic and download tool execution
- `local_tool_inbox.py` - local inbox save/read helpers used by tool handlers
- `local_tool_reader.py` - local reader handoff and background PDF-processing helpers

Reader pipeline:

- `document_sources.py` - file/source sniffing, decoding, PDF detection
- `reader_ingest_service.py` - narrow entrypoints for starting and retrying reader ingest jobs
- `reader_ingest_handlers.py` - source-specific ingest handlers for files, URLs, PDFs, retry scheduling, and conversion polling
- `reader_store.py` - reader document CRUD, speech-file lookup, and stale-job cleanup
- `reader_text.py` - markdown chunking, math-to-speech conversion, and speech JSON generation
- `reader_playback.py` - sentence-by-sentence playback streaming over WebSocket

History and inbox:

- `history.py` - DB bootstrap, conversation/session recording, and compatibility re-exports for history/inbox helpers
- `history_enrichment.py` - embeddings, summaries, topic tags
- `history_store.py` - conversation queries, inbox CRUD/search, stats
- `schema.sql` - SQLite+vec schema

Frontend:

- `static/app-common.js` - shared browser helpers for WebSocket setup, HTML escaping, and voice-list loading
- `static/index-audio.js` - streamed TTS queue, silence trimming, and browser audio playback helper for the main voice UI
- `static/index-app.js` - main voice UI controller, settings/history overlays, transcript rendering, and WebSocket client logic
- `static/inbox-app.js` - inbox page behavior, filtering, expansion, and item-chat client logic
- `static/reader-app.js` - reader page behavior, document list, retry flow, and playback client logic
- `static/index.html` - main voice UI shell
- `static/inbox.html` - inbox review UI with inline item chat
- `static/reader.html` - reader UI with playback controls and polling
- `static/manifest.json` - PWA manifest

Tests:

- `tests/test_main.py`
- `tests/test_conversation.py`
- `tests/test_mcp_manager.py`
- `tests/test_reader.py`
- `tests/test_reader_ingest_handlers.py`
- `tests/test_reader_ingest_service.py`
- `tests/test_document_sources.py`
- `tests/test_websocket_session.py`
- `tests/test_history_enrichment.py`
- `tests/test_history_store.py`
- `tests/test_local_tool_handlers.py`
- `tests/test_local_tool_reader.py`
- `tests/test_local_tool_registry.py`

## Feature Notes

### Knowledge Inbox

- Stored in `octavius_history.db` as saved items for later review.
- Item types: `note`, `search_summary`, `article`, `email_draft`
- Status flow: `pending` -> `done` or `dismissed`
- Hard delete is supported through `DELETE /api/inbox/{id}`
- Inbox semantic search uses bge-m3 embeddings on workhorse via Ollama
- Each inbox item can have a persistent item-chat conversation

### Document Reader

- Accepts local files, URLs, and inbox items.
- Converts PDF, markdown, and extracted HTML content into speech-oriented JSON.
- HTML extraction uses trafilatura.
- Math-heavy paragraphs are sent to the reader LLM; non-math paragraphs are cleaned locally.
- Playback is streamed sentence-by-sentence over WebSocket with position sync.
- Document list auto-polls while any document is still `processing`.
- Failed reader documents can be retried from the stored source metadata via `POST /api/reader/documents/{id}/retry`.

Reader storage:

- speech-ready JSON files: `/home/dave/octavius-reader/`
- metadata: `reader_documents` table

### Conversation History

- Conversations are recorded in `octavius_history.db`.
- Summaries and topic tags are generated when a conversation ends.
- History can be resumed from the browser UI through `load_conversation`.
- The same DB is shared with other AI services and exposed through the conversation-history MCP server.
- Request handlers and background reader jobs use short-lived SQLite connections; live conversation history sessions keep their own dedicated connection until the session ends.

## Contributor Guidance

Prefer these refactor directions:

- keep core STT/TTS/LLM chat boundary code in `service_clients.py` and related wrappers
- keep `main.py` focused on routing and startup, not orchestration
- keep local tool schemas, registry/dispatch, and handlers separate
- keep inbox/history query logic out of route handlers

When adding a feature:

1. Decide whether it belongs in core voice flow, reader flow, inbox/history, or a tool/MCP boundary.
2. Add or update tests near the affected subsystem.
3. Update this file only if the change affects stable architecture, operational workflow, or contributor expectations.

## Extending Octavius

Adding functionality is straightforward now, but most changes still touch a few boundaries at once. The main design question is where the new behavior should live, not how to wire it into a monolith.

Use these placement rules:

- voice/session behavior belongs in `conversation.py`, `agent.py`, or `websocket_session.py`
- new HTTP routes belong in the relevant `routes/*.py` module, with orchestration pushed down into subsystem modules
- reader ingest and playback changes belong in the `reader_ingest_*`, `reader_store.py`, `reader_text.py`, or `reader_playback.py` modules
- inbox/history query and persistence changes belong in `history_store.py` or `history_enrichment.py`, not in route handlers
- local tool additions belong in `local_tool_specs.py` plus the appropriate `local_tool_*` execution module, then get registered in `local_tool_registry.py`
- new outbound service integrations should go behind `service_clients.py` or a closely related wrapper, not inline in feature code

Common extension patterns:

1. Add a new local tool.
   Update `local_tool_specs.py`, implement the behavior in the right `local_tool_*` module, register it in `local_tool_registry.py`, and add tests for both the handler behavior and dispatch path.

2. Add a new reader source or ingest mode.
   Start in `reader_ingest_service.py` for the entrypoint shape, put source-specific logic in `reader_ingest_handlers.py`, keep document metadata in `reader_store.py`, and keep markdown-to-speech logic in `reader_text.py`.

3. Add a new UI action or page.
   Put the route in the relevant router module, keep browser logic in the page-specific `static/*-app.js` file, and extend `static/app-common.js` only for behavior that is genuinely shared.

4. Add a new external dependency or backend call.
   Put timeouts, retries, fallback behavior, and health/observability hooks near the client boundary. If the dependency can fail independently, make sure `/health` or logs surface the degraded state clearly.

Keep these considerations in mind:

- avoid putting business logic back into `main.py`; use it for composition and top-level routes only
- preserve short-lived SQLite connection usage for request/background work; do not reintroduce a shared app-wide connection
- if a feature creates background tasks, decide explicitly what happens on restart and whether retry/requeue is needed
- if a feature depends on MCP or LLM availability, think through degraded behavior and user-visible failure messages
- if a feature changes a persisted shape or workflow, update both docs and tests in the same change
- prefer extending existing subsystem seams over adding another thin facade layer

Minimum completion bar for a new feature:

1. the code is placed in the correct subsystem boundary
2. the happy path works
3. at least one failure or degraded-path test exists where it matters
4. `/health`, logs, or user-visible status remain understandable if the feature depends on outside services
5. `CLAUDE.md` is updated if the stable architecture or contributor workflow changed

## Current Hotspots

These are still the main places where complexity is concentrated:

- `main.py` still owns a broad REST and startup surface
- frontend behavior is now split into dedicated JS assets, but the UI still uses large static HTML shells rather than smaller components/templates
- reader responsibilities are split more cleanly now, but ingest flow still spans several modules and background-task boundaries

For current refactor notes, recent fixes, and change-oriented status, see `docs/status.md`.

## Related Docs

- `README.md` - short setup and development commands
- `docs/status.md` - current refactor notes, recent fixes, and active hotspots
- `octavius-prd.md` - broader product/design document
- `octavius-android-design.md` - Android companion app design exploration

## Claude Code Access

Claude Code MCP access is configured in `~/.claude.json`.

Expected entries:

- `vikunja-tasks`: `http://triplestuffed:8252/mcp`
- `conversation-history`: `http://127.0.0.1:8203/mcp`

The conversation-history server includes inbox-related tools such as `save_to_inbox`, `search_inbox`, `list_inbox`, `get_inbox_item`, and `update_inbox_item`.
