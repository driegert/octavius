# Octavius — Voice Assistant

Self-hosted voice assistant running on Dave's homelab. No cloud APIs, everything stays on the Tailnet.

## Quick Start

```bash
uv sync
systemctl --user start octavius
```

Runs as a systemd user service on `127.0.0.1:8030`, accessed via Caddy reverse proxy at `https://octavius.riegert.xyz`. Restart with `systemctl --user restart octavius`.

## Architecture

Browser (WebSocket) -> FastAPI agent -> STT/LLM/TTS + MCP tools

- **STT**: Whisper at `127.0.0.1:8502/api/transcribe` (local on lilbuddy)
- **LLM**: Qwen3.5-35B-A3B at `lilripper:8020/v1/chat/completions` (llama.cpp, 2 slots)
- **TTS**: Voxtral 4B at `triplestuffed:8020/v1/audio/speech` (vLLM-Omni), fallback Kokoro at `lilbuddy:8880`
- **Reader LLM**: Qwen3.5-9B at `lilripper:8010/v1/chat/completions` (math-to-speech conversion)
- **MCP Servers**:
  - `evangeline-email`: streamable HTTP at `triplestuffed:8251/mcp`
  - `searxng`: stdio subprocess via `uv` (local)
  - `openalex`: stdio subprocess via npm (local)
  - `vikunja-tasks`: streamable HTTP at `triplestuffed:8252/mcp`
  - `document-processing`: stdio subprocess (local wrapper, processes on lilripper:8251/mcp)

## Project Structure

- `config.py` — All endpoints, model config, MCP server definitions, system prompt, tool labels
- `stt.py` — Whisper HTTP client
- `tts.py` — Voxtral HTTP client (with Kokoro fallback)
- `mcp_manager.py` — MCP client lifecycle (stdio + HTTP), tool routing, result truncation (4000 chars), auto-reconnect on `ClosedResourceError`/broken pipe/EOF
- `conversation.py` — Chat history with trim/reset/load-from-history
- `agent.py` — Agentic loop (LLM <-> tool calls, `<think>` tag stripping, sentence buffering, tool-spiral prevention)
- `tools.py` — Local tools: `download_file`, `save_to_inbox`, `read_document`, `process_pdf` (background), `read_item_content`
- `reader.py` — Document reader: ingest pipeline, markdown chunking, LLM math-to-speech, HTML extraction via trafilatura, audio streaming
- `history.py` — Conversation recording, summary/tag generation (max_tokens >= 1024 for `<think>` tags), knowledge inbox CRUD, embeddings
- `schema.sql` — SQLite+vec schema (conversations, messages, tool_calls, saved_items, reader_documents, embeddings)
- `main.py` — FastAPI app, WebSocket endpoint, REST APIs for inbox/conversations/reader, session restore
- `static/index.html` — Main voice UI (push-to-talk, toggle-to-talk, markdown rendering, collapsible text, conversation history picker, full conversation viewer)
- `static/inbox.html` — Knowledge inbox review page with inline item chat, hard delete, marked.js
- `static/reader.html` — Document reader with playback controls, section nav, progress bar, auto-poll for processing status
- `static/manifest.json` — PWA manifest for Android homescreen icon (192px + 512px icons)

## Key Design Details

- Per-connection conversations (each WebSocket gets its own Conversation instance)
- Session persistence: conversation ID stored in localStorage, restored on reconnect/page navigation via `restore_session` WebSocket message
- WebSocket carries binary (audio) and JSON text messages for multiple features:
  - Voice: status, transcript, response, reset, restore_session, session_id, load_conversation, conversation_loaded
  - Reader: reader_play, reader_pause, reader_stop, reader_position, reader_audio_done
  - Item chat: item_chat, item_chat_load, item_chat_reset, item_chat_response, item_chat_loaded, item_chat_status
- Agent buffers sentences during tool-call rounds; discards them if tools fire (prevents duplicate speech). Only yields sentences when the LLM produces a final text response.
- Tool-spiral prevention: on rounds 5-6 of 7, a system message nudges the LLM to summarize and stop calling tools
- Tool status shows friendly labels (e.g. "Web Search...", "Creating Task...") via `TOOL_LABELS` map in config.py
- MCP reconnection detects `ClosedResourceError`, broken pipe, EOF (not just "session terminated")
- Agent strips `<think>...</think>` tags from Qwen3.5 before sending to TTS
- Tool call IDs are generated as fallback UUIDs when llama.cpp omits them
- Tool results truncated to 4000 chars to protect 65K context window
- Conversation trims to 40 messages automatically
- Browser audio playback uses `preservesPitch` for speed control without chipmunk effect
- Silence trimming done client-side via Web Audio API
- Assistant text renders as markdown via marked.js (CDN)
- Mobile-optimized layout (max-width 448px) with collapsible user/assistant text areas
- `process_pdf` tool runs PDF conversion in the background (non-blocking) and saves result to inbox
- Full conversation viewer: overlay panel showing all prior turns in the session

## Knowledge Inbox

Saved items table in `octavius_history.db` for content that should be reviewed later.
Items have types: `note`, `search_summary`, `article`, `email_draft`. Status flow:
`pending` -> `done` or `dismissed`. Hard delete available via `DELETE /api/inbox/{id}`.

- **Octavius saves via**: `save_to_inbox` local tool in `tools.py`
- **Claude Code saves via**: `save_to_inbox` MCP tool in `mcp-tools/server_history.py`
- **Review UI**: `static/inbox.html` served at `/inbox`, with search, filters, action buttons, and delete
- **REST API**: `GET /api/inbox`, `GET /api/inbox/{id}`, `PATCH /api/inbox/{id}`, `DELETE /api/inbox/{id}`
- **Semantic search**: Embeddings via bge-m3 on workhorse (Ollama)
- **Inline chat**: Each item has a persistent chat conversation with Octavius. The LLM
  sees a preview of the item content (first 500 chars) and can use `read_item_content`
  to fetch chunks of long content on demand (avoids blowing context window). Conversations
  are linked via `chat_conversation_id` on the saved item. Reset clears and starts fresh.

## Document Reader

Converts documents (PDF, markdown, HTML articles) to speech with math-to-speech conversion.
Accessible at `/reader`.

- **Ingest**: File path, URL, or from inbox items. PDFs converted via document-processing MCP.
  HTML pages extracted via trafilatura (strips boilerplate, ads, navigation).
- **Titles**: Extracted from HTML `<title>` tag / trafilatura metadata with site name appended
  in parentheses. Falls back to first heading or first line for plain text.
- **Math conversion**: Only paragraphs containing `$...$` are sent to Qwen3.5-9B at
  `lilripper:8010`. Non-math paragraphs just get markdown stripped. Citations (author-year
  and numeric), HTML artifacts, and LaTeX remnants are cleaned by `_clean_for_speech()`.
- **Storage**: Speech-ready JSON files in `/home/dave/octavius-reader/`. Metadata in
  `reader_documents` table. Position (last_chunk, last_sentence) persisted to DB.
- **Playback**: Server streams TTS sentence-by-sentence over WebSocket. Client queues audio
  with paired position data so read-along text syncs with actual playback (not receipt).
- **Controls**: Play/pause (with 100ms debounce for clean state transitions), section
  navigation, progress bar seeking, speed control, voice selection.
- **Auto-poll**: Document list refreshes every 5s while any document is "processing".

## Conversation History

All conversations are recorded to `octavius_history.db` (SQLite+vec). See `history.py`
for the recording API and `schema.sql` for the database schema. Summaries and topic tags
are auto-generated via Qwen 3.5 when a conversation ends (requires max_tokens >= 1024
to accommodate `<think>` tags, timeout 60s). The database is shared across all AI services
(Octavius, Claude Code, etc.) and queryable via the conversation-history MCP server at
`mcp-tools/server_history.py`.

Previous conversations can be resumed from the browser UI via the history picker,
which sends a `load_conversation` WebSocket message to restore the LLM context.
Conversations persist across page navigations via localStorage + `restore_session`.

## UI Layout

- **Top left**: Settings (gear icon), Conversation History (chat bubbles icon)
- **Top right**: Knowledge Inbox (brain/inbox icon), Document Reader (book/speaker icon)
- All icons are 32x32px images with 44x44px tap targets for mobile usability
- Custom icons in `static/icon-*.png`
- Favicon and PWA icon: `octavius-dapper_cyber_punk.png`

## MCP Server Access from Claude Code

Configured in `~/.claude.json`:
- `vikunja-tasks`: `http://triplestuffed:8252/mcp`
- `conversation-history`: `http://127.0.0.1:8203/mcp` (includes inbox tools: save_to_inbox, search_inbox, list_inbox, get_inbox_item, update_inbox_item)

## Vikunja Integration

System prompt includes project name/ID mapping to avoid extra tool calls. Key projects:
Inbox (1), Teaching and Trent (9), math1052 (10), amod5240 (2), math3560 (3),
Email Tasks (14), Personal and Professional (13), PhD (4), AI Projects (6).
Tasks are searched with `done=false` by default.

## Planned: Android App

An Android companion app is under consideration. The app would wrap Octavius as a
native Kotlin client (WebSocket audio + JSON) and run a phone-side MCP server over
Tailscale, allowing Octavius to trigger phone actions (calls, SMS, etc.) using the
same MCP tool pattern as other servers. Design doc: `octavius-android-design.md`
