# Octavius — Voice Assistant

Self-hosted voice assistant running on Dave's homelab. No cloud APIs, everything stays on the Tailnet.

## Quick Start

```bash
uv sync
uv run python main.py
```

Runs on `127.0.0.1:8030`, accessed via Caddy reverse proxy at `https://octavius.riegert.xyz`.

## Architecture

Browser (WebSocket) -> FastAPI agent -> STT/LLM/TTS + MCP tools

- **STT**: Whisper at `127.0.0.1:8502/api/transcribe` (local on lilbuddy)
- **LLM**: Qwen3.5-35B-A3B at `triplestuffed:8010/v1/chat/completions` (llama.cpp)
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
- `static/index.html` — Browser UI (hold-to-talk, audio playback with silence trimming and speed control)

## Key Design Details

- Single user, no session management
- WebSocket carries binary (audio) and JSON text (status/transcript/response/reset)
- Agent strips `<think>...</think>` tags from Qwen3.5 before sending to TTS
- Tool call IDs are generated as fallback UUIDs when llama.cpp omits them
- Tool results truncated to 2000 chars to protect 65K context window
- Conversation trims to 40 messages automatically
- Browser audio playback uses `preservesPitch` for speed control without chipmunk effect
- Silence trimming done client-side via Web Audio API
