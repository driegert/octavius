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

## Matrix Media / Docproc Fixes (2026-07-20)

These were fixed in **mcp-tools** (`server_documents_voice_wrapper.py` /
`server_documents_wrapper.py`, commits `cc9c82b` + `98191f1`) but the symptom
appears in Octavius, so they're recorded here; should not regress:

- **Matrix PDF conversions failed with a permission error** at the download
  step: the wrapper created the output dir *next to the source PDF*, and the
  Matrix spool (`/media/extra_stuff/octavius/matrix_media/`, owned by
  `octavius-matrix`, mode 755) is read-only for the service user. The wrapper
  now falls back to `~/docproc-output/<stem>-<source-path-hash>/` when the
  source's parent isn't writable (`W_OK|X_OK`), pre-creates the target, and
  scp's the remote dir's *contents* (`/.` suffix — plain `scp -r` nests into
  a pre-existing target and breaks re-conversions).
- **Remote dedup hits returned a wrong md path**: the cached output's `.md`
  carries the *original* upload's filename stem, and the Matrix sidecar
  prefixes every upload with a unique id — so re-sending the same PDF always
  mismatched and Octavius logged "Could not read converted markdown" (the
  agent then silently compensated via web search). The wrapper now globs the
  downloaded dir for the actual `.md` instead of assuming the stem.
- Cross-channel history access shipped the same day: `read_conversation` +
  `source`/`since` filters on `search_conversation_history` (see CLAUDE.md
  "Conversation History").

## Voice / TTS Fixes

These behaviors were fixed recently and should not regress:

- spoken text is markdown-normalized before TTS via `speechify` in `tts.py`,
  applied at the shared `synthesize` choke point (main voice, proactive,
  item-chat, reader playback). It strips conversational markdown AND orphan
  emphasis left when a bold/italic span is split across streamed sentences (the
  cause of TTS reading "asterisk asterisk"), while preserving `3 * 4` / `foo_bar`.
- response style is channel-aware: `stream_agent_turn(source=...)` injects a
  per-turn directive (`VOICE_STYLE`/`TEXT_STYLE`), so voice replies are short and
  markdown-free while typed/Matrix replies may use light markdown and be fuller.
  Note: this needs the service restarted to take effect (in-memory prompt).

## Web Search

- The main-agent web search moved from the varlabz `searxng-mcp` (`search` tool,
  SearXNG-only, no fallback) to mcp-tools' `server_serper.py`, registered as the
  `web-search` MCP server exposing `web_search` (SearXNG-first → Serper.dev
  fallback; page reading stays on Crawl4AI via `web-reader`). The agent scopes it
  by the `web-search` server key in `agent.py`. See CLAUDE.md "Configured MCP
  servers".
- The Serper fallback needs `SERPER_API_KEY` in `mcp-tools/.env`; without it,
  `web_search` degrades to SearXNG-only. The stdio subprocess reads `.env` at
  spawn, so a service restart is required to pick up a newly-added key.
- **Cert gotcha (should not regress):** SearXNG is fronted by Caddy's internal CA,
  which Python's bundled certifi does not trust. The SearXNG client must point at
  the system CA bundle (`/etc/ssl/certs/ca-certificates.crt`), NOT
  `/etc/ssl/cert.pem` (absent on this Ubuntu host) — a wrong path silently fails
  every SearXNG call with `CERTIFICATE_VERIFY_FAILED`. `server_serper.py` handles
  this itself (honors `SSL_CERT_FILE`, else the system bundle). This was the cause
  of the post-triplestuffed-migration "web search returns nothing" outage.

## Stability Notes

Operational assumptions worth keeping in mind during debugging:

- external service reachability problems can look like application bugs if STT, TTS, LLM, or MCP endpoints are unavailable
- `/health` now exposes `alive`, `ready`, `degraded`, per-server MCP status, and `llm_chain` failover information, so degraded runtime behavior should be checked there first
- reader ingest jobs are in-memory background tasks and do not survive restart
- docproc job ids are in-process state in the document-processing stdio wrapper and also do not survive an Octavius restart — `check_document_status` on a pre-restart id reports it unknown (graceful, but the model may go hunting)
- restart recovery is now manual requeue rather than automatic job resurrection
- live conversation and item-chat history sessions still keep their own dedicated SQLite connection until they are ended
- the browser UIs are less script-heavy than before, but layout and markup are still concentrated in large static HTML files
- Silero VAD requires `models/silero_vad.onnx` to be present; if the file is missing, VAD is skipped and auto-stop will not work
- STT failover (lilripper primary, lilbuddy fallback) is not yet implemented — switching requires a settings change
- **`lilripper:8010` is behind auth (2026-07-13).** It 401s without a bearer token. The key lives in `~/.config/octavius/env` as `OCTAVIUS_LLM_API_KEYS` (JSON, origin → token) and reaches the service through the `EnvironmentFile` drop-in; `service_clients.auth_headers()` attaches it by URL on every `LLMChainClient` request path. Three consumers depend on it: the reader LLM, the vision chain, and the subagent fallback tier. Two failure modes to keep apart when debugging: a **missing/wrong key** is a 401 → `HTTPStatusError` → burns a failover hop and shows up in `/health`'s `llm_chain` (not an obvious auth error), while **empty content with no failover** is usually Qwen think-mode eating a small `max_tokens`, not auth. Nothing loads a `.env` file, so a key placed there is silently ignored.
- **Memory push was silently dead 2026-07-02 → 2026-07-13 (fixed; should not regress).** When the memory service was extracted to the `agent-memory` repo, `history.py`'s push path kept doing `import memory` for three watermark helpers, so every conversation end logged "Memory client unavailable; skipping push" and skipped the push. The helpers (`get_memory_watermark` / `set_memory_watermark` / `messages_after_watermark`) now live in `history_store.py` — they only touch Octavius's own tables (`conversations.last_extracted_message_id` + `messages`), so Octavius no longer imports anything from agent-memory except over HTTP via `memory_client.py`. Conversations that *ended* during the gap were never mined for facts (push happens at conversation end; watermarks stayed put but closed conversations don't re-push) — a backfill would need a one-off script.
- **WS disconnects arrive as messages, not exceptions (fixed; should not regress).** Starlette's `ws.receive()` returns a `websocket.disconnect` message; calling `receive()` again raises `RuntimeError`. The run loop used to reach cleanup *through* that RuntimeError, which chained the traceback into every `exc_info` warning logged during cleanup (confusing journal noise). `websocket_session.run` now breaks on the disconnect message itself.
- **Caddy leaks upstream WS sockets when a client stalls silently (mitigated 2026-07-13).** A downstream client that freezes without dying (phone in Doze: app stops reading, kernel keeps ACKing) blocks Caddy's copy goroutines; when uvicorn's WS ping timeout then closes the upstream leg, Caddy never reaps its side — one CLOSE_WAIT socket to `127.0.0.1:8030` per stalled client (~3/day observed; clean closes and RSTs do NOT leak — verified by live probe). Mitigation: `stream_timeout 24h` in the octavius `reverse_proxy` block in the Caddyfile (safe because the app and PWA both auto-reconnect). A Caddy restart clears any backlog.
- **Subagent chain has no cross-host failover.** `consult_specialist` now routes primary `lilripper:8020` (`qwen3.6-35b-a3b`, warm) → fallback `lilripper:8010` (`qwen3.6-35b-a3b-general`). Both tiers are on `lilripper`, so if that host is down the specialist has nowhere to go (the `lilbuddy:8010` / `triplestuffed:8010` tiers were dropped for latency). The dispatcher only tries `[assigned_url, fallback_url]` per call, and `secondary` is concurrency-overflow only — so re-adding resilience means putting a remote host in the **`fallback`** slot, not `secondary`. Watch the model alias: `lilripper:8010` serves `-general`/`-code`, not the bare `qwen3.6-35b-a3b` (see CLAUDE.md "Subagent LLM chain"). To handle later.

## Near-Term Work

Likely refactor targets, in rough priority order:

1. Further narrow `main.py` so it remains a routing layer rather than a coordination module.
2. Reduce the size of the remaining static HTML shells by extracting reusable frontend structure or templates.
3. Continue replacing coarse integration paths with narrower behavior-level tests where the boundary is now stable.
4. Restore cross-host failover for the subagent chain (see Stability Notes): decide whether a remote host should occupy the `fallback` slot, and/or extend the dispatcher so more than one host is tried per call. Consider whether `secondary`/`fallback` role semantics should be reworked so cross-host resilience and concurrency overflow aren't mutually exclusive.
5. Transcription/dictation mode (Dave, 2026-07-12): capture speech (Android app first), transcribe, and save the transcript to the `saved_items` stash — deliberately NOT the vault. No agent turn, no TTS. Revives the stash write path for non-note payloads; needs a WS message or REST route for STT-to-stash (the unwired `save_to_stash` helper in `local_tool_inbox.py` is a starting point). App-side sketch in `../octavius-android/docs/HANDOFF.md` NEXT WORK #3.

## Migration Note

- `reader_documents.saved_item_id` is still enforced as a plain foreign key. If inbox deletion should eventually null out that reference automatically, that will require a real SQLite migration to add `ON DELETE SET NULL`, not just a schema-file edit.

## Related Design Work

The Android companion app remains exploratory rather than committed implementation work. See `octavius-android-design.md` for that design thread.
