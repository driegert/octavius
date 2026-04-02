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

- `config.py` — All endpoints, model config, MCP server definitions, system prompt
- `stt.py` — Whisper HTTP client
- `tts.py` — Voxtral HTTP client (with Kokoro fallback)
- `mcp_manager.py` — MCP client lifecycle (stdio + HTTP), tool routing, result truncation (4000 chars)
- `conversation.py` — Chat history with trim/reset/load-from-history
- `agent.py` — Agentic loop (LLM <-> tool calls, `<think>` tag stripping, sentence-level streaming)
- `tools.py` — Local tools: `download_file`, `save_to_inbox`, `read_document`, `process_pdf`, `read_item_content`
- `reader.py` — Document reader: ingest pipeline, markdown chunking, LLM math-to-speech, audio streaming
- `history.py` — Conversation recording, summary/tag generation, knowledge inbox CRUD, embeddings
- `schema.sql` — SQLite+vec schema (conversations, messages, tool_calls, saved_items, reader_documents, embeddings)
- `main.py` — FastAPI app, WebSocket endpoint, REST APIs for inbox/conversations/reader
- `static/index.html` — Main voice UI (push-to-talk, toggle-to-talk, markdown rendering, collapsible text, conversation history picker)
- `static/inbox.html` — Knowledge inbox review page with inline item chat
- `static/reader.html` — Document reader with playback controls, section nav, progress bar
- `static/manifest.json` — PWA manifest for Android homescreen icon

## Key Design Details

- Per-connection conversations (each WebSocket gets its own Conversation instance)
- WebSocket carries binary (audio) and JSON text messages for multiple features:
  - Voice: status, transcript, response, reset, load_conversation, conversation_loaded
  - Reader: reader_play, reader_pause, reader_stop, reader_position, reader_audio_done
  - Item chat: item_chat, item_chat_load, item_chat_reset, item_chat_response, item_chat_loaded, item_chat_status
- Agent strips `<think>...</think>` tags from Qwen3.5 before sending to TTS
- Tool call IDs are generated as fallback UUIDs when llama.cpp omits them
- Tool results truncated to 4000 chars to protect 65K context window
- Conversation trims to 40 messages automatically
- Browser audio playback uses `preservesPitch` for speed control without chipmunk effect
- Silence trimming done client-side via Web Audio API
- Assistant text renders as markdown via marked.js (CDN)
- Mobile-optimized layout (max-width 448px) with collapsible user/assistant text areas
- `process_pdf` tool runs PDF conversion in the background (non-blocking) and saves result to inbox

## Knowledge Inbox

Saved items table in `octavius_history.db` for content that should be reviewed later.
Items have types: `note`, `search_summary`, `article`, `email_draft`. Status flow:
`pending` -> `done` or `dismissed`.

- **Octavius saves via**: `save_to_inbox` local tool in `tools.py`
- **Claude Code saves via**: `save_to_inbox` MCP tool in `mcp-tools/server_history.py`
- **Review UI**: `static/inbox.html` served at `/inbox`, with search, filters, and action buttons
- **REST API**: `GET /api/inbox`, `GET /api/inbox/{id}`, `PATCH /api/inbox/{id}`
- **Semantic search**: Embeddings via bge-m3 on workhorse (Ollama)
- **Inline chat**: Each item has a persistent chat conversation with Octavius. The LLM
  sees a preview of the item content and can use `read_item_content` to fetch chunks
  of long content on demand (avoids blowing context window). Conversations are linked
  via `chat_conversation_id` on the saved item.

## Document Reader

Converts documents (PDF, markdown, articles) to speech with math-to-speech conversion.
Accessible at `/reader`.

- **Ingest**: File path, URL, or from inbox items. PDFs converted via document-processing MCP.
- **Math conversion**: Paragraphs containing `$...$` sent to Qwen3.5-9B at `lilripper:8010`.
  Non-math paragraphs just get markdown stripped. Citations, HTML artifacts, and LaTeX
  remnants are cleaned by `_clean_for_speech()` in `reader.py`.
- **Storage**: Speech-ready JSON files in `/home/dave/octavius-reader/`. Metadata in
  `reader_documents` table. Position (last_chunk, last_sentence) persisted to DB.
- **Playback**: Server streams TTS sentence-by-sentence over WebSocket. Client queues audio
  with paired position data so read-along text syncs with actual playback.
- **Controls**: Play/pause, section navigation, progress bar seeking, speed control, voice selection.

## Conversation History

All conversations are recorded to `octavius_history.db` (SQLite+vec). See `history.py`
for the recording API and `schema.sql` for the database schema. Summaries and topic tags
are auto-generated via Qwen 3.5 when a conversation ends (requires max_tokens >= 1024
to accommodate `<think>` tags). The database is shared across all AI services (Octavius,
Claude Code, etc.) and queryable via the conversation-history MCP server at
`mcp-tools/server_history.py`.

Previous conversations can be resumed from the browser UI via the history picker,
which sends a `load_conversation` WebSocket message to restore the LLM context.

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
