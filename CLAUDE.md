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

### Configuration and secrets

**Nothing loads a `.env` file.** `settings.py` reads `os.environ` directly, so
config only reaches the app through the process environment. `.env.example` is
a reference for variable names and defaults, not a file the app consumes.

- **Service**: `~/.config/systemd/user/octavius.service.d/env.conf` adds
  `EnvironmentFile=-/home/dave/.config/octavius/env` (mode 0600, outside the
  repo). After editing either file: `systemctl --user daemon-reload && systemctl --user restart octavius`.
- **Foreground**: `set -a; source ~/.config/octavius/env; set +a; uv run python main.py`.

**LLM endpoint auth**: `OCTAVIUS_LLM_API_KEYS` is a JSON object mapping endpoint
*origin* (`scheme://host:port`) to a bearer token, e.g.
`{"http://lilripper:8010":"sk-..."}`. `service_clients.auth_headers()` resolves
it by URL for every `LLMChainClient` request path (`stream_chat`, `complete`,
`complete_with_tools`); endpoints absent from the map are called with no
`Authorization` header, exactly as before. Keys are held per origin, not per
chain entry, because one endpoint is reached from several chains — `lilripper:8010`
serves the reader LLM, the vision chain, *and* the subagent fallback tier, and the
reader calls it through a client whose own chain doesn't list it. In a systemd
`EnvironmentFile`, single-quote the value so the JSON survives:
`OCTAVIUS_LLM_API_KEYS='{"http://lilripper:8010":"sk-..."}'`.
Auth failures surface as ordinary chain failures — a 401 is an `HTTPStatusError`,
so it burns a failover hop and shows up in `/health`'s `llm_chain` section.

Primary UI routes:

- `/` main voice UI
- `/inbox` legacy stash review UI (see "Stash (retired as a notes store; kept for non-note payloads)" below)
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
Browser (WebSocket) -> FastAPI app -> main agent (streaming, ~20 core tools)
                                        │
                                        ├─ direct: web search, web page read, vault (notes), reader, PDF, download, memory
                                        │
                                        └─ consult_specialist(domain, task) → subagent
                                             (non-streaming, scoped tools, runs INLINE)
                                             ├─ email domain (evangeline-email)
                                             ├─ research domain (openalex)
                                             └─ tasks domain (vikunja-tasks)
```

The main agent never sees email/research/task tool schemas. It calls
`consult_specialist(domain, task)` which runs a separate non-streaming LLM
loop with only the tools for that domain, using the same MCP sessions. This
keeps the main agent's context lean (~20 tools instead of ~55) and prevents
tool-schema-heavy payloads from causing LLM 500 errors.

`consult_specialist` is **synchronous/inline**: the specialist runs to
completion inside the main agent's tool round (via
`WebSocketSessionHandler.run_inline_subagent`) and its result is returned into
the same turn, so Octavius speaks the answer immediately — no badge, no pull.
A `subagent_dispatcher` ticket is reserved per call so inline consults respect
subagent endpoint capacity, and per-step progress is forwarded to the UI
`status` line so the user isn't left in silence.

**Async delegation is reserved, not removed.** The async path
(`delegate_task` / `pull_delegation` / `list_pending_delegations` /
`cancel_delegation`, the "Agents at Work" badge, the
`spawn_delegation`/`_run_and_announce` lifecycle, the `proactive_speak` setting,
and the `delegation_*` WebSocket messages) is kept in the codebase but is
currently **unexposed** to the agent (no tool specs/handlers registered). It is
reserved for a future `deep_research` domain that will shell out to the **pi
harness headless** (`pi --mode json -p --no-session ...`, parsing the final
assistant `message_end`), which already has a parallel `deep_research`
orchestrator. Re-enabling it is one tool spec + one registry line. Quick
domains (email/tasks/research) deliberately stay inline (low voice latency,
warm MCP sessions); only long-running deep research is backgrounded.

The email subagent prompt defaults to evangeline's `hybrid_search` (RRF fusion
of semantic + BM25) and passes `folder=null` / `date_after="1970-01-01"` to
defeat the Inbox-only and 6-month-lookback defaults on the other search tools.

External services currently expected:

- **STT**: faster-whisper at `lilripper:8552/api/transcribe` (large-v3, int8_float16, CUDA)
- **LLM chain (main agent)**: Qwen3.6-35B-A3B via `OCTAVIUS_LLM_CHAIN`, defaulting to:
  - primary: `lilripper:8020/v1/chat/completions`
  - first fallback: `127.0.0.1:8001/v1/chat/completions` on lilbuddy
  - second fallback: `triplestuffed:8010/v1/chat/completions`
- **Subagent LLM chain**: separate routing for delegated subagents via `OCTAVIUS_SUBAGENT_LLM_CHAIN`, defaulting to:
  - primary: `lilripper:8020/v1/chat/completions` running `qwen3.6-35b-a3b` — the main agent's dedicated, always-warm 35B. Inline consults block the main loop, so there's no real contention, and this avoids a cold-load/swap on the first consult.
  - fallback: `lilripper:8010/v1/chat/completions` running `qwen3.6-35b-a3b-general` — HTTP-level failover only.
  - The dispatcher (`subagent_dispatcher.py`) routes by `role`. Only two roles matter per call: `primary` (first-try / concurrency routing, with `secondary` as an optional concurrency-overflow tier) and `fallback` (the single per-call HTTP-failover target passed alongside the assigned URL). Per-endpoint `capacity` controls how many concurrent subagents may share an endpoint.
  - **Model-alias gotcha**: `lilripper:8010` is a llama-swap that serves `qwen3.6-35b-a3b-general` / `-code` and the reader's `qwen3.5-9b` — **not** the bare `qwen3.6-35b-a3b` alias (that alias only exists on `lilripper:8020`, `triplestuffed:8010`, and `lilbuddy:8010`). `complete_with_tools` uses each chain *entry's* model (the payload model is ignored) and fails over on any 4xx/5xx, so a wrong alias hard-400s and silently costs a failover hop. `lilripper:8010` is also shared with the reader, so interleaving document reading and email/task consults pays a llama-swap cost.
  - **KNOWN LIMITATION (handle later):** both tiers now live on `lilripper` (`:8020` primary, `:8010` fallback), so there is **no cross-host failover** — if `lilripper` is fully down, `consult_specialist` has nowhere to go. `lilbuddy:8010` / `triplestuffed:8010` were dropped from the subagent chain. Because the dispatcher only tries `[assigned_url, fallback_url]` per call, restoring cross-host resilience means putting a remote host (e.g. `triplestuffed:8010`, local; or `lilbuddy:8010`) in the **`fallback`** slot — a `secondary` entry only absorbs concurrency overflow, not error-failover. See `docs/status.md`.
- **TTS**: Kokoro at `lilbuddy:8880/v1/audio/speech` (voice `bm_lewis`) — the live
  default. `TTSSettings.voxtral_enabled` is **False**, so every synth call goes
  straight to Kokoro (Voxtral-only voices remap to the fallback voice). Voxtral 4B
  (`OCTAVIUS_TTS_URL`) is wired but disabled — its inconsistent output levels make
  it unsuitable as the live primary. Set `OCTAVIUS_TTS_VOXTRAL_ENABLED=1` to restore
  the Voxtral-primary → Kokoro-fallback path (with circuit breaker).
- **Reader LLM**: Qwen3.5-9B at `lilripper:8010/v1/chat/completions` (**behind auth** — needs a bearer token; see "Configuration and secrets")
- **Summary/tag generation**: summary chain defaults to `127.0.0.1:8001/v1/chat/completions` with fallback `triplestuffed:8010/v1/chat/completions`
- **Embeddings**: bge-m3 chain via `OCTAVIUS_EMBEDDING_CHAIN`, defaulting to:
  - primary: `lilbuddy:8020/v1/embeddings` (standalone llama.cpp bge-m3 server → Caddy :8020 → 127.0.0.1:8002, OpenAI schema)
  - fallback: `workhorse:11434/api/embeddings` (Ollama schema)
- **Vision LLM chain**: image-input turns (Matrix `image_input` frames) via `OCTAVIUS_VISION_LLM_CHAIN`, defaulting to `lilripper:8010/v1/chat/completions` (`qwen3.6-35b-a3b-general`) — the only chain endpoint with image input enabled, and **behind auth** (see "Configuration and secrets"). Separate `LLMChainClient` instance (`vision_llm_client` in `service_clients.py`); see `agent.py`'s `use_vision` routing in `stream_agent_turn`. Vision routing is sticky per thread (`Conversation.has_images`): after the first image the whole thread stays on the vision chain and image content arrays stay in memory; on thread re-attach they re-hydrate from the spool via the `attachments` table when the file still exists. Persisted history/memory only ever see text placeholders.
- **PDF → markdown conversion**: driven through the `document-processing` MCP server already registered in `DEFAULT_MCP_SERVERS` (mcp-tools' documents wrapper: scp to lilripper, convert at `lilripper:8251/mcp`, download the .md back to local paths). `docproc_client.py` wraps its `convert_pdf_to_md` / `get_conversion_result` tools via `MCPManager.call_tool`; poll pacing via `OCTAVIUS_DOCPROC_POLL_INTERVAL`/`_TIMEOUT`. Triggered by Matrix `file_input` frames with `mime=application/pdf`; see `docs/ws-media-contract.md`.

Configured MCP servers:

- `evangeline-email`: streamable HTTP at `triplestuffed:8251/mcp`
- `web-search`: stdio subprocess (mcp-tools' `server_serper.py`, run via its own venv). Exposes a single `web_search` tool — the "search" half of the search → read → reason pipeline — that tries self-hosted SearXNG first (`searxng.riegert.xyz`, free/private) and falls back to the **Serper.dev** Google API when SearXNG is unreachable, rate-limited, or returns nothing. Surfaced directly to the main agent, not behind a specialist. `server_serper.py` reads `SERPER_API_KEY` from `mcp-tools/.env` and trusts the system CA bundle for SearXNG's Caddy cert on its own (no env needed in the server config). **Without `SERPER_API_KEY` the fallback arm is inert — web search is SearXNG-only until a key is added.** Replaced the old varlabz `searxng-mcp` (`search` tool, SearXNG-only, no fallback).
- `web-reader`: streamable HTTP at `lilripper:8254/mcp` (mcp-tools' `server_reader.py`, wrapping a self-hosted Crawl4AI `/md` endpoint; same deployed instance the pi agents use). Exposes `read_url` — the "read" half of the search → read → reason pipeline. Surfaced directly to the main agent (like `web-search`), not behind a specialist.
- `vault-search`: streamable HTTP at `triplestuffed:8254/mcp` (mcp-tools' `server_vault.py` — sqlite-vec + FTS5 BM25 over the Obsidian vault, RRF-fused; co-located with the vault). Exposes a single `search_vault` tool, surfaced directly to the main agent. The `03-personal/Journaling/` subtree is excluded server-side. Search is the only vault operation that goes through MCP — note reads/writes are local file I/O (see "Vault" under Feature Notes).
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
- Response style is channel-aware. `stream_agent_turn` takes a `source` and
  folds a per-turn style directive into `messages[0]` (same mechanism as the
  memory block): `source="voice"` → short/spoken/no-markdown; every other
  source (`text`/`matrix`/`image`/`file`/`inbox_chat`) → may use light markdown
  and give a complete answer. The base `settings.system_prompt` is
  channel-neutral; the directive (`VOICE_STYLE`/`TEXT_STYLE` in `agent.py`) is
  the tuning knob. Defaults to `"voice"` if a caller omits it.
- Spoken text is markdown-normalized before TTS. `tts.synthesize` runs every
  string through `speechify` (the single choke point all TTS callers share),
  stripping `**bold**`/`*italic*`/`` `code` ``/links/headings and line-leading
  list markers so the engine doesn't verbalize "asterisk asterisk". It also
  strips ORPHAN emphasis left when a bold/italic span is split across streamed
  sentences, while preserving meaningful characters (`3 * 4`, `foo_bar`). It is
  deliberately lighter than `reader_text.clean_for_speech` (which also strips
  citations/LaTeX for converted journal PDFs).
- Conversation history trims automatically to 40 messages.
- `/health` distinguishes `alive`, `ready`, and `degraded` states.
- `/health` exposes per-server MCP connection status plus `llm_chain` observability including configured endpoints, failover count, terminal failures, and the last successful endpoint.

WebSocket message families:

- Voice: `status`, `transcript`, `transcript_partial`, `response`, `reset`, `restore_session`, `session_id`, `load_conversation`, `conversation_loaded`, `stt_start`, `stt_stop`, `stt_auto_stop`
- Matrix media (client→server, same session/threading semantics as `text_input`): `image_input`, `file_input` — frozen contract, see `docs/ws-media-contract.md`. Both repos (`octavius`, `matrix-agent-sidecar`) implement against that doc.
- Reader: `reader_play`, `reader_pause`, `reader_stop`, `reader_position`, `reader_audio_done`
- Item chat: `item_chat`, `item_chat_load`, `item_chat_reset`, `item_chat_response`, `item_chat_loaded`, `item_chat_status`
- Delegations (DORMANT — reserved for future `deep_research`, not currently emitted): `delegation_update` (server→client; status running/ready/failed + preview), `delegation_removed` (server→client; record cleared), `delegation_list` (client→server; resync request), `delegation_pull` (client→server; mode=merge|new), `delegation_dismiss` (client→server)

## Code Map

Core runtime:

- `main.py` - FastAPI app creation, startup wiring, shared top-level routes, WebSocket entrypoint
- `db.py` - SQLite connection helpers and short-lived connection context manager
- `settings.py` - env-backed runtime settings and defaults
- `service_clients.py` - core HTTP clients for STT, TTS, the main LLM chat chain, summary generation, and embeddings
- `stt.py` - thin STT wrapper
- `tts.py` - thin TTS wrapper; `speechify` markdown→speech normalization applied at the `synthesize` choke point
- `vad.py` - Silero VAD ONNX wrapper for server-side voice activity detection

Route modules:

- `routes/inbox.py` - inbox page and inbox REST API routes
- `routes/conversations.py` - conversation history API routes
- `routes/reader_api.py` - reader page and reader REST API routes
- `routes/vault.py` - vault REST API (`/api/vault/{recent,note,search}`); search proxies the `search_vault` MCP, everything else is local file I/O via `vault_files.py`

Conversation and tool loop:

- `conversation.py` - chat history state with trim/reset/load support
- `agent.py` - LLM loop, tool calling, output cleanup, tool-spiral prevention
- `websocket_session.py` - WebSocket session state, message dispatch, item-chat lifecycle, and STT/TTS turn handling
- `mcp_manager.py` - MCP client lifecycle, routing, truncation, reconnect handling
- `tools.py` - local tool schemas and dispatch entrypoint used by the agent loop
- `subagent.py` - internal scoped subagent for specialist domains (email, research, tasks); invoked inline via `consult_specialist` (`run_inline_subagent`)
- `local_tool_specs.py` - local tool schemas
- `local_tool_registry.py` - compatibility wrapper for older local-tool imports
- `local_tool_downloads.py` - local download filename logic and download tool execution
- `local_tool_vault.py` - `save_note` / `read_note` / `edit_note` / `commit_edit` tool handlers over `vault_files.py`
- `vault_files.py` - pure file I/O over the Obsidian vault (path-safe, journaling-denied, atomic hash-guarded writes); search never goes through this module
- `local_tool_inbox.py` - legacy stash save/read helpers (`save_to_stash` / `list_stash_items` — retired/unwired, no longer registered as tools)
- `stash_to_obsidian.py` - watermark-based one-way exporter of `saved_items` rows to the vault inbox (run via the `obsidian-stash-export.timer` user unit)
- `local_tool_reader.py` - local reader handoff and background PDF-processing helpers
- `local_tool_documents.py` - `check_document_status` local tool (polls a docproc job by id)
- `docproc_client.py` - loopback HTTP client for the docproc web queue (submit/poll a PDF conversion job); Octavius never imports the `docproc` package

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
- `history_store.py` - conversation queries, inbox CRUD/search, memory-push watermarks, stats
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
- `tests/test_local_tool_inbox.py`
- `tests/test_subagent.py`
- `tests/test_agent.py` - vision-chain routing and image-turn history downgrade in `stream_agent_turn`
- `tests/test_service_clients.py` - LLM chain failover/health, TTS circuit breaker, embedding schemas, and LLM endpoint auth headers
- `tests/test_docproc_client.py`
- `tests/test_local_tool_documents.py`

## Feature Notes

### Voice Interaction

Three talk modes, selectable in the settings panel:

- **Hold to talk** (default): press and hold to record, release to send.
- **Tap to talk**: tap to start recording, tap again to stop.
- **Continuous conversation**: press "Start Conversation" to begin a hands-free
  loop. The user speaks, Silero VAD detects 1.5s of silence and auto-stops,
  Octavius responds via TTS, then the mic automatically reopens for the next
  turn. Press "End Conversation" to exit the loop.

Streaming STT: the browser captures PCM at 16kHz via Web Audio API
ScriptProcessor and sends binary chunks every 250ms over the WebSocket. The
server accumulates a buffer, runs background faster-whisper transcription, and
sends `transcript_partial` messages. On stop (manual or VAD auto-stop), the
server uses the latest partial text immediately — no re-transcription needed.

Server-side VAD: Silero VAD v6 ONNX model (`models/silero_vad.onnx`) runs on
CPU via onnxruntime. Each 512-sample window (32ms at 16kHz) is prepended with
a 64-sample context buffer before inference. Per-session LSTM state is carried
between chunks and reset at the start of each turn. The `SileroVAD` class in
`vad.py` wraps the ONNX model; each WebSocket session gets its own instance.

### Vault (Obsidian notes — the single note store)

As of 2026-07-09, Dave's Obsidian vault is the single source of truth for
notes; the DB stash write path is retired. The vault (`VAULT_PATH`, default
`~/Documents/Personal`) is plain `.md` files on triplestuffed.

- Agent tools (local, in `local_tool_vault.py` over `vault_files.py`):
  `save_note`, `read_note`, `edit_note`, `commit_edit`. Search is the
  `search_vault` MCP tool (vault-search server), which reads a derived
  sqlite-vec + FTS5 index, never the files directly.
- Frozen vault API contract rules, enforced in `vault_files.py`: new notes
  land in `00-zettelkasten/001-Fleeting/` only (filename frozen at creation);
  `03-personal/Journaling/` is never listed, read, or written; paths are
  vault-relative POSIX with traversal/symlink escapes rejected; writes are
  atomic (temp file + `os.replace`, umask-honoring 0664) and hash-guarded
  (`base_hash` = sha256 of file bytes, optimistic concurrency — `commit_edit`
  409s on conflict).
- REST surface for UI/clients (e.g. Android): `GET /api/vault/recent`,
  `GET/POST/PUT /api/vault/note`, `GET /api/vault/search` (`routes/vault.py`).
- Agents never rename or move notes — Dave files them in Obsidian himself.

### Stash (retired as a notes store; kept for non-note payloads)

The old DB capture area (`saved_items` in `octavius_history.db`). The
*notes* write path is retired: `save_to_stash` / `list_stash_items` still
exist in `local_tool_inbox.py` but are unwired (no tool specs/handlers
registered), and `stash_to_obsidian.py` exported existing items to the vault
one-way (watermark-based, via the `obsidian-stash-export.timer` user unit).
This supersedes the old "Stash rename" TODO (routes `/inbox` → `/stash`).

The stash is NOT being deleted, though: Dave wants it kept for payloads that
don't belong in Obsidian — first planned use is a transcription/dictation
mode that saves raw transcripts to `saved_items` (see `docs/status.md`
Near-Term Work #5; "there are use-cases for the Stash database still — just
not typical notes").

Still live:

- `process_pdf` background conversions write their result to a stash item.
- The `/inbox` review UI and `/api/inbox/*` routes still work
  (list/update/`DELETE /api/inbox/{id}`, bge-m3 semantic search).
- Item chat still works, but the item's (capped) content is now inlined into
  the prompt — the `read_item_content` tool was removed.

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

### Matrix media (image / PDF turns)

The Matrix sidecar (`../matrix-agent-sidecar`) spools attachments to
`/media/extra_stuff/octavius/matrix_media/` and sends `image_input` /
`file_input` WS frames — see `docs/ws-media-contract.md` for the frozen
wire contract both repos implement against.

- **Images** (`image_input`): `websocket_session.handle_image_input`
  base64-reads the spool file and builds an OpenAI-style multimodal content
  array. The turn routes through `settings.vision_llm_chain` instead of the
  default chain (see `agent.py`'s `use_vision` handling in
  `stream_agent_turn`). Once the turn completes, the in-memory conversation's
  image content is downgraded back to a text placeholder
  (`Conversation.replace_last_user_content`) — later turns go back to the
  default text chain. Persisted history and the memory extractor only ever
  see the placeholder/caption text, never base64.
- **PDFs** (`file_input`, `mime=application/pdf`): submitted to the docproc
  web queue deterministically (plain code, not an LLM tool call) via
  `docproc_client.py`. A non-empty caption is treated as instructions —
  Octavius polls the job in a background task (bounded, doesn't block the WS
  loop or other sessions) and then runs an agent turn with the instructions
  plus the converted markdown (inlined under
  `OCTAVIUS_DOCPROC_INLINE_CHAR_BUDGET`, else path + head excerpt). An empty
  caption gets an immediate acknowledgement mentioning the docproc job id.
  The `check_document_status` local tool lets the model check status / fetch
  the markdown path for a job id later in the conversation.
- **Non-PDF files** (`file_input`, other `mime`): a brief acknowledgement
  turn only — Octavius can't process other file types yet.
- Both flows record an `attachments` row (`type` `image`/`file`) against the
  persisted user message.

### Conversation History

- Conversations are recorded in `octavius_history.db`.
- Summaries and topic tags are generated when a conversation ends. The summary
  prompt asks for a one-sentence, action-oriented summary *and* an `index`
  flag; conversations the LLM judges as purely read-only retrieval (e.g.
  listing emails/tasks, weather lookups) get the summary stored but skip the
  embedding write, so they don't pollute semantic search results. Tags are
  still generated for all conversations.
- The main agent can search prior Octavius conversations via the
  `search_conversation_history` local tool, which wraps
  `history_store.search_conversations()` (semantic-first against
  `summary_embeddings`, with a text-LIKE fallback). Filtered to
  `service="octavius"` and excludes the current conversation. Optional
  `source` (`voice`/`matrix`/`text`) and `since` filters; with a filter and
  no query it becomes a recency listing (`history_store.list_conversations`),
  which also surfaces retrieval-only chats that skip embedding.
- The `read_conversation` local tool returns the full transcript of a prior
  conversation by id (user/assistant turns only), channel-agnostic — this is
  what lets a Matrix thread pull in a past voice conversation and continue it
  in text. Transcripts are paged (page 1 = most recent, ~3.5k chars/page,
  long single messages capped) because local tool results bypass the MCP
  4000-char truncator. Handlers live in `local_tool_history.py`.
- History can be resumed from the browser UI through `load_conversation`.
- The same DB is shared with other AI services and exposed through the conversation-history MCP server.
- Request handlers and background reader jobs use short-lived SQLite connections; live conversation history sessions keep their own dedicated connection until the session ends.

## Contributor Guidance

Prefer these refactor directions:

- keep core STT/TTS/LLM chat boundary code in `service_clients.py` and related wrappers
- keep `main.py` focused on routing and startup, not orchestration
- keep local tool schemas in `local_tool_specs.py`, with dispatch centered in `tools.py`
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
- local tool additions belong in `local_tool_specs.py` plus the appropriate `local_tool_*` execution module, then get wired through `tools.py`
- new outbound service integrations should go behind `service_clients.py` or a closely related wrapper, not inline in feature code

Common extension patterns:

1. Add a new local tool.
   Update `local_tool_specs.py`, implement the behavior in the right `local_tool_*` module, wire it through `tools.py`, and add tests for both the handler behavior and dispatch path.

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
- `docs/ws-media-contract.md` - frozen WS media contract (`image_input`/`file_input`) shared with `matrix-agent-sidecar`
- `octavius-prd.md` - broader product/design document
- `octavius-android-design.md` - Android companion app design exploration

## Native Android client

A working native client lives in the sibling repo `../octavius-android` (Kotlin/Compose
foreground-service app; its own `CLAUDE.md`). It is an **independent client of the same
`/ws`** as the browser PWA — each WS connection gets its own `Conversation`, so both run
side by side with no server change. It speaks the exact `static/index-app.js` protocol
(Float32 LE PCM @16k up; `transcript`/`response`/`status`(incl. `audio_done`)/`stt_auto_stop`
+ WAV down).

**Before changing the WS protocol or STT/VAD/audio_done behavior, know the app depends on:**
the server VAD only auto-stops *after* speech (pure silence never auto-stops), and an empty
transcription sends **no `audio_done`** (just a "Couldn't hear anything" status). The app's
continuous-conversation loop and silence watchdog are built around exactly this (its wake
word is shelved as of 2026-07-11; the app only holds a mic during a capture or an active
conversation). If you change it, the PWA *and* the Android client change together.

**Barge-in:** `handle_stt_start` now cancels any in-flight `turn_task` — starting a new
capture means the user is talking, so the current reply (LLM stream + TTS) is stopped. This
is a no-op in normal flows (the turn is already done by the time the mic reopens) and lets a
client interrupt a long reply by opening capture mid-stream. The Android client uses this for
its "interrupt while speaking" feature (energy-gated detector during playback). A future PWA
Stop button could send `stt_start`/cancel the same way.

## Claude Code Access

Claude Code MCP access for this repo is configured in `.mcp.json` (committed; you
approve the server on first launch in this repo).

Configured server:

- `conversation-history`: `http://127.0.0.1:8203/mcp` — the conversation-history
  MCP server (`mcp-tools/server_history.py`), running as the
  `conversation-history.service` user unit on triplestuffed.

The conversation-history server includes inbox-related tools such as `save_to_inbox`, `search_inbox`, `list_inbox`, `get_inbox_item`, and `update_inbox_item`.

`vikunja-tasks` is intentionally **not** exposed to Claude Code.
