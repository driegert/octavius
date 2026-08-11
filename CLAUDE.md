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

**LLM endpoint auth**: only `lilripper:8010` is behind auth. Two env vars feed
`settings.llm_api_keys`, which `service_clients.auth_headers()` resolves by URL
for every `LLMChainClient` request path (`stream_chat`, `complete`,
`complete_with_tools`):

- **`OCTAVIUS_8010_API_KEY`** (preferred) — a bare token for `lilripper:8010`.
  This is the value that rotates, so it **wins** over the JSON map. The var name
  is stable across rotations by design. It is scoped to lilripper, *not* to port
  8010: `lilbuddy:8010` and `triplestuffed:8010` also listen on 8010 and are
  open, so they must never receive the header (`settings.KEYED_8010_ORIGIN`
  pins this; there are tests).
- **`OCTAVIUS_LLM_API_KEYS`** — the general mechanism: a JSON object mapping
  endpoint *origin* (`scheme://host:port`) to a bearer token, e.g.
  `{"http://lilripper:8010":"sk-..."}`. Use it if another endpoint goes behind
  auth. In a systemd `EnvironmentFile`, single-quote the value so the JSON
  survives: `OCTAVIUS_LLM_API_KEYS='{"http://lilripper:8010":"sk-..."}'`.

Endpoints absent from both are called with no `Authorization` header. Keys are
held per origin, not per chain entry, because one endpoint is reached from
several chains — `lilripper:8010` serves the reader LLM, the vision fallback,
*and* the subagent primary tier, and the reader calls it through a client whose
own chain doesn't list it.

A 401 still burns a failover hop (it is an `HTTPStatusError` like any other),
but as of 2026-08-08 it no longer *hides*: `service_clients.classify_chain_error`
buckets every chain failure into `auth` / `client_error` / `server_error` /
`connect` / `connect_timeout` / `timeout` / `bad_response`, and `/health`'s `llm_chain` reports
`endpoints_rejecting_credentials`, `auth_failures`, `last_failure_kind`, and
per-endpoint `last_error_kind` / `last_error_status` / `authenticated`. A 401
also logs at ERROR naming the origin and which env var to check. `last_error_*`
clears on that endpoint's next success, so a non-null value means "currently
believed broken, this way"; the counters are lifetime. The practical split:
`auth` = key problem; `connect`/`connect_timeout` = host down (no TCP
handshake); `timeout` = host accepted the connection but never generated (a
"zombie" — `/v1/models` answers, completions hang); `client_error` (usually
400/404) = model alias missing from that endpoint's catalog.

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
- **LLM chain (main agent)**: via `OCTAVIUS_LLM_CHAIN`, defaulting to:
  - primary: `lilripper:8020/v1/chat/completions` running `qwen3.6-35b-a3b-mtp-general` — a llama.cpp **router**, so the model id selects the model (see "Router model ids" below). Accepts image input. No auth. Shared with pi-agent (which pulls the 27B), and that contention has produced a live `500 model ... failed to load`.
  - first fallback: `lilripper:8010/v1/chat/completions` (`qwen3.6-35b-a3b-mtp-q4-general`) — deliberately the alias **already resident** on `:8010`, not the Q6 `mtp-general`. Failing over to a different alias would pin `:8010` to it for the length of a `:8020` outage and make every `consult_specialist` and reader call swap the model back, turning one degraded endpoint into three. Behind auth.
  - second fallback: `lilbuddy:8010/v1/chat/completions` (`qwen3.6-35b-a3b`) — plain single-model server, text-only, so an image turn must never land on it (hence the separate vision chain below).
  - `triplestuffed:8010` was **removed** from this chain on 2026-08-08: its GPUs serve Positron autocomplete/NES models, and it accepts connections without ever generating, so failing into it burned the full 120 s read timeout. See `docs/status.md`.
- **Subagent LLM chain**: separate routing for delegated subagents via `OCTAVIUS_SUBAGENT_LLM_CHAIN`, defaulting to:
  - primary: `lilripper:8010/v1/chat/completions` running `qwen3.6-35b-a3b-mtp-q4-general`, `capacity: 3` — served with `--parallel 3`, so consults no longer queue behind the main agent's own turn on the single-slot `:8020`. `consult_specialist` is the dominant Matrix first-turn cost (~15 s average, 50 s worst), and it reserves a dispatcher ticket, so the old `capacity: 1` also serialised concurrent consults against each other. The q4 MTP variant is chosen for speculative-decoding speed: on tool-calling work, latency beats Q5 weights. Same alias as the reader, so sharing `:8010` costs no extra model swap.
  - fallback: `lilripper:8020/v1/chat/completions` running `qwen3.6-35b-a3b-mtp-general` — HTTP-level failover only.
  - The dispatcher (`subagent_dispatcher.py`) routes by `role`. Only two roles matter per call: `primary` (first-try / concurrency routing, with `secondary` as an optional concurrency-overflow tier) and `fallback` (the single per-call HTTP-failover target passed alongside the assigned URL). Per-endpoint `capacity` controls how many concurrent subagents may share an endpoint.
  - **Model is per endpoint, not per domain.** `subagent.py::_model_for_url` resolves the model from the chain entry matching the assigned URL, so all three specialist domains sharing `:8010` share one model. Per-domain models would need a `model` key on `SUBAGENT_DOMAINS` overriding that lookup.
  - **Router model ids (both ports are routers now; the alias is load-bearing).** `complete_with_tools` uses each chain *entry's* model (the payload model is ignored) and fails over on any 4xx/5xx, so an alias absent from that endpoint's catalog hard-400s and silently burns a failover hop. The bare `qwen3.6-35b-a3b` alias exists on **neither** lilripper port — only on `triplestuffed:8010` and `lilbuddy:8010`. Catalogs as of 2026-08-08:
    - `:8020` (5 aliases): `qwen3.6-27b-mtp-{code,general}`, `qwen3.6-35b-a3b-mtp-{code,general}`, `unsloth/Qwen3-Coder-30B-A3B-Instruct-1M-GGUF:Q8_0`. All but the unsloth coder accept images.
    - `:8010` (14 aliases): the above plus `qwen3.6-35b-a3b-{code,general}`, `qwen3.6-35b-a3b-mtp-q4-{code,general}`, `gemma4-26b-a4b`, `gemma4-31b`, `glm-4.7-flash`, `ministral-14b`, `qwen3.5-9b`.
  - **Keep `:8010`'s consumers on one alias.** The subagent chain, the reader, and the vision fallback all point at `:8010`; if they disagree on the alias, interleaving a document read with a consult thrashes the router between resident models. Same applies to `:8020` (main chain + vision primary + subagent fallback all on `qwen3.6-35b-a3b-mtp-general`).
  - **KNOWN LIMITATION (handle later):** both tiers live on `lilripper` (`:8010` primary, `:8020` fallback), so there is **no cross-host failover** — if `lilripper` is fully down, `consult_specialist` has nowhere to go. `lilbuddy:8010` / `triplestuffed:8010` were dropped from the subagent chain. Because the dispatcher only tries `[assigned_url, fallback_url]` per call, restoring cross-host resilience means putting a remote host (e.g. `triplestuffed:8010`, local; or `lilbuddy:8010`) in the **`fallback`** slot — a `secondary` entry only absorbs concurrency overflow, not error-failover. See `docs/status.md`.
- **TTS**: Kokoro at `lilbuddy:8880/v1/audio/speech` (voice `bm_lewis`) — the live
  default. `TTSSettings.voxtral_enabled` is **False**, so every synth call goes
  straight to Kokoro (Voxtral-only voices remap to the fallback voice). Voxtral 4B
  (`OCTAVIUS_TTS_URL`) is wired but disabled — its inconsistent output levels make
  it unsuitable as the live primary. Set `OCTAVIUS_TTS_VOXTRAL_ENABLED=1` to restore
  the Voxtral-primary → Kokoro-fallback path (with circuit breaker).
- **Reader LLM**: `qwen3.6-35b-a3b-mtp-q4-general` at `lilripper:8010/v1/chat/completions` (**behind auth** — needs a bearer token; see "Configuration and secrets"). `qwen3.5-9b` is still in `:8010`'s catalog but went stale — it lists in `/v1/models` and then hangs on completion, which silently degraded every math chunk to dollar-stripping. Alias deliberately matches the subagent chain's `:8010` entry.
- **Summary/tag generation**: `lilripper:8020` with fallback `lilripper:8010`, model `qwen3.6-35b-a3b-mtp-general` (moved off the dead lilbuddy/triplestuffed pair 2026-08-08). `SummaryClient` is **not** an `LLMChainClient`: it sends one model to both URLs, so the alias must exist on both ports. It does attach `auth_headers` (added 2026-08-08 — previously it sent none, so any authed endpoint here would have 401'd silently). A failed summary is invisible to the user; history just ends up unsummarised and untagged.
- **Embeddings**: bge-m3 chain via `OCTAVIUS_EMBEDDING_CHAIN`, defaulting to:
  - primary: `workhorse:11434/api/embeddings` (Ollama schema)
  - fallback: `lilbuddy:8020/v1/embeddings` (standalone llama.cpp bge-m3 server → Caddy :8020 → 127.0.0.1:8002, OpenAI schema)
  - **Order reversed 2026-08-10.** lilbuddy was primary and went unreachable; because a
    dead host drops packets rather than refusing them, every embed burned the full
    connect budget on it first. `EmbeddingClient` now also has a **per-endpoint circuit
    breaker** (2 consecutive failures → skipped for 300 s, then a single half-open
    probe) and no longer retries `ConnectTimeout` — it subclasses the generic timeout
    class, so a dead host was being retried, doubling the cost. `/health`'s
    `embedding_chain` shows per-endpoint `tripped` / `consecutive_failures` /
    `cooldown_remaining`. It deliberately does **not** feed the top-level `degraded`:
    every search path falls back to keyword matching, so Octavius still answers.
  - **Message embeds are detached from the turn path.** `add_message_async` commits the
    row and then *spawns* the embed (`history_enrichment.spawn_embedding`) instead of
    awaiting it — awaiting put a network round-trip in front of the LLM call for the
    user message and in front of `audio_done` for the assistant message, which is what
    made a dead embedder cost ~10 s twice per turn. The spawned task is a **root** task,
    so cancelling a turn (`handle_reset`) cannot lose it, and it does its sqlite write
    via `asyncio.to_thread` on its own connection. Cap: 8 in flight;
    `drain_inflight()` runs at shutdown. Consequence: message-level semantic search is
    **eventually consistent**.
  - **Embed input is capped at `EMBED_MAX_CHARS` (4000)** in `history_enrichment`, the
    choke point every embed call goes through. Embedders reject over-long input
    outright — Ollama's default `num_ctx` is 2048 tokens and workhorse 500s somewhere
    between 4000 and 6000 characters. Raising `num_ctx` is not a fix: bge-m3 tops out at
    8192 tokens (a 20k-char input still 500s), and a full-context embed took 4.5 s versus
    0.25 s.
  - **`history_sweeper.py`** re-embeds anything that never landed (startup + every
    15 min, behind `OCTAVIUS_EMBEDDING_SWEEPER`). The pending marker is simply the
    absence of a row in the vec0 table. Three non-obvious rules: the `role IN
    ('user','assistant')` filter is load-bearing (tool results are never embedded, and
    without it the sweeper re-selects the whole tool-call history forever); a pass gives
    up when the breaker reports every endpoint tripped, or after
    `MAX_CONSECUTIVE_FAILURES`, rather than burning the timeout budget row by row; but a
    *single* failing row is **skipped, not fatal**. That last one is load-bearing too —
    aborting on the first `None` meant one unembeddable row at the head of the
    newest-first batch blocked every row behind it, pass after pass, forever.
  - **`conversations.indexed`** (added 2026-08-10, additive migration) records the
    summariser's index decision, which was previously unpersisted — so "skipped on
    purpose" and "the embedder was down" looked identical and the summary sweeper
    couldn't tell which rows to repair. `NULL` = legacy/unknown, never swept.
    `_write_summary` also **deletes any existing summary vector** in the same
    transaction: conversations are resumed in place (every Matrix thread), so a rewrite
    whose re-embed fails would otherwise leave a vector for superseded text that looks
    complete forever.
- **Vision LLM chain**: image-input turns (Matrix `image_input` frames) via `OCTAVIUS_VISION_LLM_CHAIN`, defaulting to `lilripper:8020` (`qwen3.6-35b-a3b-mtp-general`) with `lilripper:8010` (`qwen3.6-35b-a3b-mtp-q4-general`) as fallback — the `:8010` entry is **behind auth** (see "Configuration and secrets"). The primary is now the *same endpoint and model as the main chain*, since `:8020` gained image input; the chain stays separate purely so image turns can't fail over onto the main chain's text-only fallbacks on lilbuddy/triplestuffed. Separate `LLMChainClient` instance (`vision_llm_client` in `service_clients.py`); see `agent.py`'s `use_vision` routing in `stream_agent_turn`. Vision routing is sticky per thread (`Conversation.has_images`): after the first image the whole thread stays on the vision chain and image content arrays stay in memory; on thread re-attach they re-hydrate from the spool via the `attachments` table when the file still exists. Persisted history/memory only ever see text placeholders.
- **PDF → markdown conversion**: driven through the `document-processing` MCP server already registered in `DEFAULT_MCP_SERVERS` (mcp-tools' documents wrapper: scp to lilripper, convert at `lilripper:8251/mcp`, download the .md back to local paths). `docproc_client.py` wraps its `convert_pdf_to_md` / `get_conversion_result` tools via `MCPManager.call_tool`; poll pacing via `OCTAVIUS_DOCPROC_POLL_INTERVAL`/`_TIMEOUT`. Triggered by Matrix `file_input` frames with `mime=application/pdf`; see `docs/ws-media-contract.md`.

Configured MCP servers:

- `evangeline-email`: streamable HTTP at `triplestuffed:8251/mcp`
- `web-search`: stdio subprocess (mcp-tools' `server_serper.py`, run via its own venv). Exposes a single `web_search` tool — the "search" half of the search → read → reason pipeline — that tries self-hosted SearXNG first (`searxng.riegert.xyz`, free/private) and falls back to the **Serper.dev** Google API when SearXNG is unreachable, rate-limited, or returns nothing. Surfaced directly to the main agent, not behind a specialist. `server_serper.py` reads `SERPER_API_KEY` from `mcp-tools/.env` and trusts the system CA bundle for SearXNG's Caddy cert on its own (no env needed in the server config). **Without `SERPER_API_KEY` the fallback arm is inert — web search is SearXNG-only until a key is added.** Replaced the old varlabz `searxng-mcp` (`search` tool, SearXNG-only, no fallback).
- `web-reader`: streamable HTTP at `lilripper:8254/mcp` (mcp-tools' `server_reader.py`, wrapping a self-hosted Crawl4AI `/md` endpoint; same deployed instance the pi agents use). Exposes `read_url` — the "read" half of the search → read → reason pipeline. Surfaced directly to the main agent (like `web-search`), not behind a specialist.
- `vault-search`: streamable HTTP at `triplestuffed:8254/mcp` (mcp-tools' `server_vault.py` — sqlite-vec + FTS5 BM25 over the Obsidian vault, RRF-fused; co-located with the vault). Exposes a single `search_vault` tool, surfaced directly to the main agent. The `03-personal/Journaling/` subtree is excluded server-side. Search is the only vault operation that goes through MCP — note reads/writes are local file I/O (see "Vault" under Feature Notes).
- `paper-search`: streamable HTTP at `127.0.0.1:8206/mcp` (mcp-tools'
  `server_papers.py` — sqlite-vec + FTS5 BM25 over Dave's converted Paperpile
  library at `/media/extra_stuff/papers/`, RRF-fused; runs as the
  `papers-mcp.service` user unit on this host, no Caddy hop). Exposes
  `search_papers` + `get_paper`, surfaced directly to the main agent.
  Conversion/indexing is owned by mcp-tools (`papers_convert.py` /
  `papers_indexer.py`, nightly `papers-sync.timer`) — see mcp-tools CLAUDE.md.
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
- `/health` exposes per-server MCP connection status plus `llm_chain` observability including configured endpoints, failover count, terminal failures, the last successful endpoint, and **failure classification** — `endpoints_rejecting_credentials` / `auth_failures` / `last_failure_kind`, plus per-endpoint `last_error_kind`, `last_error_status`, and `authenticated`. Check `endpoints_rejecting_credentials` first when a chain looks flaky: non-empty means a key problem, not an outage.

WebSocket message families:

- Voice: `status`, `transcript`, `transcript_partial`, `response`, `reset`, `restore_session`, `session_id`, `load_conversation`, `conversation_loaded`, `stt_start`, `stt_stop`, `stt_auto_stop`
- Text streaming (server→client): `response_delta` — one sentence of the reply as it streams, emitted before the final `response` (which stays authoritative). The Matrix sidecar edits its thread message in place from these; the browser ignores the frame.
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
- `local_tool_reader.py` - local reader handoff (file, and raw text via `read_document(text=...)`) and background PDF-processing helpers
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
- `history_enrichment.py` - embeddings, summaries, topic tags; also the detached-embed helpers (`spawn_embedding` / `drain_inflight`)
- `history_sweeper.py` - background re-embed of rows whose inline embed never landed
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
- `tests/test_history_attach.py`
- `tests/test_history_enrichment.py` - also detached-embed lifecycle (survives turn cancellation and the session connection closing)
- `tests/test_history_sweeper.py` - sweeper filters/convergence, the `indexed` migration, and summary stale-vector invalidation
- `tests/test_history_store.py`
- `tests/test_local_tool_handlers.py`
- `tests/test_local_tool_history.py` - search filters/list mode and `read_conversation` paging
- `tests/test_local_tool_reader.py`
- `tests/test_local_tool_registry.py`
- `tests/test_local_tool_inbox.py`
- `tests/test_local_tool_vault.py`
- `tests/test_routes_vault.py`
- `tests/test_vault_files.py`
- `tests/test_subagent.py`
- `tests/test_subagent_dispatcher.py`
- `tests/test_agent.py` - vision-chain routing and image-turn history downgrade in `stream_agent_turn`
- `tests/test_service_clients.py` - LLM chain failover/health, TTS circuit breaker, embedding schemas, and LLM endpoint auth headers
- `tests/test_tts.py` - `speechify` markdown→speech normalization
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

- Accepts local files, URLs, inbox items, and **raw pasted text**.
- Converts PDF, markdown, and extracted HTML content into speech-oriented JSON.
- HTML extraction uses trafilatura.
- Math-heavy paragraphs are sent to the reader LLM; non-math paragraphs are cleaned locally.
- Playback is streamed sentence-by-sentence over WebSocket with position sync.
- Document list auto-polls while any document is still `processing`.
- Failed reader documents can be retried from the stored source metadata via `POST /api/reader/documents/{id}/retry`.

Reader storage:

- speech-ready JSON files: `/home/dave/octavius-reader/`
- pasted-text originals: `/home/dave/octavius-reader/pasted/<doc_id>-<slug>.md`
- metadata: `reader_documents` table

Pasted text (`source: "text"`) is the one source with no file or URL behind it,
so `start_text_ingest` writes it out and records the path as `source_path`.
That is what makes it retryable: `start_retry_task`'s existing `markdown`
branch re-reads `source_path`, so retry needed no new code. Writing it out is
best-effort — a failure costs retryability, never the document. Both entry
points (the `/reader` paste box and the agent's `read_document(text=...)`) go
through `start_text_ingest`, so both get title derivation and persistence.

### Matrix media (image / PDF turns)

The Matrix sidecar (`../matrix-agent-sidecar`) spools attachments to
`/media/extra_stuff/octavius/matrix_media/` and sends `image_input` /
`file_input` WS frames — see `docs/ws-media-contract.md` for the frozen
wire contract both repos implement against.

- **Images** (`image_input`): `websocket_session.handle_image_input`
  base64-reads the spool file and builds an OpenAI-style multimodal content
  array. The turn routes through `settings.vision_llm_chain` instead of the
  default chain (see `agent.py`'s `use_vision` handling in
  `stream_agent_turn`). The image content array **stays** in the in-memory
  conversation and the thread stays on the vision chain for its remaining
  turns (`Conversation.has_images` is sticky until reset/load), so follow-up
  questions about the same image still see the pixels rather than a summary.
  llama.cpp's prefix cache absorbs the re-sent image tokens; payload growth is
  bounded by `Conversation.trim()`. `Conversation.replace_last_user_content`
  implements a downgrade-to-placeholder but is **not called from production
  code** (tests only) — an earlier design that was dropped in favour of
  keeping the image. Persisted history and the memory extractor only ever see
  the placeholder/caption text, never base64.
- **PDFs** (`file_input`, `mime=application/pdf`): submitted to the docproc
  web queue deterministically (plain code, not an LLM tool call) via
  `docproc_client.py`. A non-empty caption is treated as instructions —
  Octavius polls the job in a background task (bounded, doesn't block the WS
  loop or other sessions) and then runs an agent turn with the instructions
  plus the converted markdown (inlined under
  `OCTAVIUS_DOCPROC_INLINE_CHAR_BUDGET`, else path + head excerpt). An empty
  caption gets an immediate acknowledgement mentioning the docproc job id.
  The `check_document_status` local tool lets the model check status / fetch
  the markdown path for a job id later in the conversation. Converted outputs
  normally land next to the source PDF; when the source dir is read-only for
  the service user (the Matrix spool), the mcp-tools wrapper falls back to
  `~/docproc-output/<stem>-<path-hash>/` on this host. Job ids are in-process
  wrapper state and do NOT survive an Octavius restart —
  `check_document_status` on an old id reports it unknown.
- **Non-PDF files** (`file_input`, other `mime`): a brief acknowledgement
  turn only — Octavius can't process other file types yet. Matrix audio
  (voice messages) and video never reach Octavius at all: the sidecar
  degrades them to descriptive `text_input` frames.
- Both flows record an `attachments` row (`type` `image`/`file`) against the
  persisted user message.

### Conversation History

- Conversations are recorded in `octavius_history.db`. **The live database is not the
  one in the repo**: the service unit sets `OCTAVIUS_DB_PATH=/media/extra_stuff/octavius/octavius_history.db`,
  and the repo-local file is a stale leftover. Query the former when inspecting real
  state — `systemctl --user show octavius -p Environment` is the authority.
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
- `docs/HANDOFF-matrix-latency.md` - Matrix first-turn latency: measurements, the streaming + subagent-routing changes, and the open 502 chase
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

Configured servers:

- `conversation-history`: `http://127.0.0.1:8203/mcp` — the conversation-history
  MCP server (`mcp-tools/server_history.py`), running as the
  `conversation-history.service` user unit on triplestuffed.
- `paper-search`: `http://127.0.0.1:8206/mcp` — Dave's paper library
  (`mcp-tools/server_papers.py`, `papers-mcp.service`). No PII; safe for
  cloud-connected clients, unlike vault-search.

The conversation-history server includes inbox-related tools such as `save_to_inbox`, `search_inbox`, `list_inbox`, `get_inbox_item`, and `update_inbox_item`.

`vikunja-tasks` is intentionally **not** exposed to Claude Code.
