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
- **TTS**: Voxtral 4B at `triplestuffed:8020/v1/audio/speech` (vLLM-Omni)
- **MCP Servers**:
  - `evangeline-email`: streamable HTTP at `triplestuffed:8251/mcp`
  - `searxng`: stdio subprocess via `uv` (local)

## Project Structure

- `config.py` — All endpoints, model config, MCP server definitions, system prompt
- `stt.py` — Whisper HTTP client
- `tts.py` — Voxtral HTTP client
- `mcp_manager.py` — MCP client lifecycle (stdio + HTTP), tool routing
- `conversation.py` — Chat history with trim/reset
- `agent.py` — Agentic loop (LLM <-> tool calls, `<think>` tag stripping)
- `main.py` — FastAPI app, WebSocket endpoint
- `static/index.html` — Browser UI (push-to-talk or toggle-to-talk, audio playback with silence trimming and speed control)

## Key Design Details

- Single user, no session management
- WebSocket carries binary (audio) and JSON text (status/transcript/response/reset)
- Agent strips `<think>...</think>` tags from Qwen3.5 before sending to TTS
- Tool call IDs are generated as fallback UUIDs when llama.cpp omits them
- Tool results truncated to 2000 chars to protect 65K context window
- Conversation trims to 40 messages automatically
- Browser audio playback uses `preservesPitch` for speed control without chipmunk effect
- Silence trimming done client-side via Web Audio API

## Conversation History

All conversations are recorded to `octavius_history.db` (SQLite+vec). See `history.py`
for the recording API and `schema.sql` for the database schema. Summaries and topic tags
are auto-generated via Qwen 3.5 when a conversation ends. The database is shared across
all AI services (Octavius, Claude Code, etc.) and queryable via the conversation-history
MCP server at `mcp-tools/server_history.py`.

## Planned: Android App

An Android companion app is under consideration. The app would wrap Octavius as a
native Kotlin client (WebSocket audio + JSON) and run a phone-side MCP server over
Tailscale, allowing Octavius to trigger phone actions (calls, SMS, etc.) using the
same MCP tool pattern as other servers. Design doc: `octavius-android-design.md`
