# Octavius Product And Architecture Notes

## Status

This document reflects the current refactored application rather than the original v1 prototype. For short-term change notes and recent fixes, see `docs/status.md`. For contributor workflow and code ownership, see `CLAUDE.md`.

## What Octavius Is

Octavius is a self-hosted voice assistant running on Dave's homelab. It provides:

- browser-based voice interaction over WebSocket
- MCP-backed tool use for email, search, academic lookup, task management, and document processing
- a knowledge inbox for saved notes, summaries, articles, and drafts
- a document reader that converts PDFs, URLs, inbox items, and text into speech-oriented JSON for playback
- persistent conversation history, summaries, and topic tags in SQLite

Everything is intended to stay within the local/Tailnet environment.

## Current Architecture

```text
Browser UI
  -> FastAPI app
    -> WebSocket session / route handlers
      -> conversation + agent loop
        -> local tools + MCP tools
        -> STT / TTS / LLM / summary / embedding services
    -> inbox / reader / history REST APIs
    -> static UI assets
```

Core modules:

- `main.py`: app composition, startup wiring, `/health`, `/ws`
- `websocket_session.py`: voice-session lifecycle, item chat, STT/TTS turn handling
- `agent.py`: LLM loop and tool-call orchestration
- `service_clients.py`: STT, TTS, LLM-chain, summary, and embedding clients
- `mcp_manager.py`: MCP connection lifecycle and tool routing
- `tools.py`: local-tool dispatch and handler wiring
- `history.py`, `history_store.py`, `history_enrichment.py`: persistence, queries, summaries, tags, embeddings
- `reader_ingest_service.py`, `reader_ingest_handlers.py`, `reader_store.py`, `reader_text.py`, `reader_playback.py`: reader ingestion and playback pipeline

## Runtime Model

- FastAPI serves `/`, `/inbox`, `/reader`, `/health`, REST APIs, and `/ws`.
- Each WebSocket connection gets its own conversation state and history session.
- Conversation history is stored in SQLite and can be restored from the browser UI.
- Local tools and MCP tools are both exposed to the agent loop through OpenAI-style tool definitions.
- LLM requests use a configured failover chain.
- Summary/tag generation and embeddings are asynchronous in the live WebSocket path to avoid blocking the event loop.
- Reader ingest jobs run as in-process background tasks and do not survive restart.

## Main User-Facing Features

### Voice Assistant

- browser microphone capture using `audio/webm`
- STT transcription
- multi-round LLM tool use
- streamed TTS playback of final answer sentences
- conversation reset and history restore

### Knowledge Inbox

- save notes, summaries, articles, and drafts
- review and update item status
- semantic search over inbox content
- persistent item-specific chat threads

### Document Reader

- accepts local files, URLs, direct text, and inbox items
- detects PDFs by content, not only filename suffix
- converts source content into speech-ready JSON
- supports retry of failed ingest jobs from stored source metadata
- streams playback sentence by sentence over WebSocket

## Deployment Assumptions

- the app binds locally and is exposed through a reverse proxy
- external model and tool services are configured via `settings.py` environment-backed defaults
- SQLite is the system of record for conversations, inbox items, and reader metadata
- reader output files are stored on disk in the configured reader directory

## Design Priorities

- keep the voice interaction responsive
- make degraded external-service states visible through `/health` and logs
- preserve conversation and inbox history
- keep orchestration out of route handlers where possible
- make reader ingest retryable when work is interrupted

## Non-Goals

- multi-user tenancy
- cloud-hosted APIs
- a large chat-first web UI replacing the voice-first workflow
