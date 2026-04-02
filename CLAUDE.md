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
- **LLM**: Qwen3.5-35B-A3B at `lilripper:8020/v1/chat/completions` (llama.cpp, dedicated 2-GPU complex)
- **TTS**: Voxtral 4B at `triplestuffed:8020/v1/audio/speech` (vLLM-Omni), fallback Kokoro at `lilbuddy:8880`
- **MCP Servers**:
  - `evangeline-email`: streamable HTTP at `triplestuffed:8251/mcp`
  - `searxng`: stdio subprocess via `uv` (local)
  - `openalex`: stdio subprocess via npm (local)
  - `vikunja-tasks`: streamable HTTP at `triplestuffed:8252/mcp`
  - `document-processing`: stdio subprocess (local wrapper)

## Project Structure

- `config.py` — All endpoints, model config, MCP server definitions, system prompt
- `stt.py` — Whisper HTTP client
- `tts.py` — Voxtral HTTP client
- `mcp_manager.py` — MCP client lifecycle (stdio + HTTP), tool routing, result truncation (4000 chars)
- `conversation.py` — Chat history with trim/reset/load-from-history
- `agent.py` — Agentic loop (LLM <-> tool calls, `<think>` tag stripping, sentence-level streaming)
- `tools.py` — Local tools (download_file, save_to_inbox) that don't need an MCP server
- `history.py` — Conversation recording, summary/tag generation, knowledge inbox CRUD, embeddings
- `schema.sql` — SQLite+vec schema (conversations, messages, tool_calls, saved_items, embeddings)
- `main.py` — FastAPI app, WebSocket endpoint, REST API for inbox and conversation history
- `static/index.html` — Browser UI (push-to-talk, toggle-to-talk, markdown rendering, collapsible text, conversation history picker)
- `static/inbox.html` — Knowledge inbox review page

## Key Design Details

- Per-connection conversations (each WebSocket gets its own Conversation instance)
- WebSocket carries binary (audio) and JSON text (status/transcript/response/reset/load_conversation/conversation_loaded)
- Agent strips `<think>...</think>` tags from Qwen3.5 before sending to TTS
- Tool call IDs are generated as fallback UUIDs when llama.cpp omits them
- Tool results truncated to 4000 chars to protect 65K context window
- Conversation trims to 40 messages automatically
- Browser audio playback uses `preservesPitch` for speed control without chipmunk effect
- Silence trimming done client-side via Web Audio API
- Assistant text renders as markdown via marked.js (CDN)
- Mobile-optimized layout (max-width 448px) with collapsible user/assistant text areas

## Knowledge Inbox

Saved items table in `octavius_history.db` for content that should be reviewed later.
Items have types: `note`, `search_summary`, `article`, `email_draft`. Status flow:
`pending` -> `done` or `dismissed`.

- **Octavius saves via**: `save_to_inbox` local tool in `tools.py`
- **Claude Code saves via**: `save_to_inbox` MCP tool in `mcp-tools/server_history.py`
- **Review UI**: `static/inbox.html` served at `/inbox`, with search, filters, and action buttons
- **REST API**: `GET /api/inbox`, `GET /api/inbox/{id}`, `PATCH /api/inbox/{id}`
- **Semantic search**: Embeddings via bge-m3 on workhorse (Ollama)

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

Both Vikunja and conversation-history MCP servers are configured in `~/.claude.json`:
- `vikunja-tasks`: `http://triplestuffed:8252/mcp`
- `conversation-history`: `http://127.0.0.1:8203/mcp` (includes inbox tools)

## Planned: Android App

An Android companion app is under consideration. The app would wrap Octavius as a
native Kotlin client (WebSocket audio + JSON) and run a phone-side MCP server over
Tailscale, allowing Octavius to trigger phone actions (calls, SMS, etc.) using the
same MCP tool pattern as other servers. Design doc: `octavius-android-design.md`
